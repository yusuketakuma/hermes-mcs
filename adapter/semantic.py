#!/usr/bin/env python3
"""MCS semantic layer — Jev-assisted meaning evaluation (Phase J).

Feature-gated by config.json "semantic" (default OFF):

- off:     no job seeding, no drain, no external communication — pending
           state and raw data are preserved untouched (INV-22, AT-053)
- shadow:  artifacts are recorded under semantic_*/loop_*/notify_plan
           kinds only. Existing extract_v1/extract_llm/patient_rollup
           selection, ACK policy, outbox, and requests are never touched
           (INV-16, AT-049/062)
- assist:  same artifacts; humans read them via mcs_view `semantic` /
           `loops` snapshot views
- enforce: additionally, an audited summary may attach a section to the
           existing new_messages notification and a degraded minimal
           notice may be enqueued when the audited path overruns its
           target delay — both through the SAME existing outbox sender
           (INV-14/23)

Storage reuses artifacts/fetch_jobs/notify_outbox — no schema change, so
snapshot readers (mcs_view v5-7 gate) keep working unchanged (AT-065).

Pipeline per durable job (kind='semantic', keyed by thread root):
  bundle snapshot -> per-target primary propositions (Jev noul)
  -> conditional med-detail (Jev choice) -> local-LLM fact candidates
  with verified evidence spans -> local-LLM summary with common Claim
  schema -> code audit + per-claim Jev support choice + coverage check
  -> at most ONE repair -> artifacts persisted in one transaction.

Invariants honored: evaluation never changes ACK eligibility (INV-03),
summary claims all carry evidence refs (INV-07), unknown/incomplete is
never presented as verified (INV-04/08), repair is once per input
generation (INV-10), Open Loop candidates never mutate formal requests
(INV-11/19), a stale input fingerprint demotes results instead of
publishing them (INV-15/21).
"""
import argparse
from functools import wraps
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger import Ledger, LedgerReader
from mcs_requests import payload_hash
from mcs_util import acquire_run_lock, load_config
import semantic_jev as jev
import semantic_runtime as runtime

HOME = os.path.expanduser("~/.mcs")
DB = os.path.join(HOME, "data", "ledger.db")
CONF_PATH = os.path.join(HOME, "config.json")

SCHEMA_VERSION = "2026-09-20"
POLICY_VERSION = "2026-09-21.2"
JOB_KIND = "semantic"

KIND_BUNDLE = "semantic_bundle"
KIND_ASSESS = "semantic_assess"
KIND_FACTS = "semantic_facts"
KIND_SUMMARY = "semantic_summary"
KIND_AUDIT = "semantic_audit"
KIND_LOOP = "loop_candidate"
KIND_LOOP_EVENT = "loop_event"
KIND_PLAN = "notify_plan"
KIND_USAGE = "semantic_usage"
SEMANTIC_KINDS = (KIND_BUNDLE, KIND_ASSESS, KIND_FACTS, KIND_SUMMARY,
                  KIND_AUDIT, KIND_LOOP, KIND_LOOP_EVENT, KIND_PLAN,
                  KIND_USAGE, "semantic_coverage", "semantic_extraction_chunk",
                  "semantic_repair", "semantic_policy")

MODES = ("off", "shadow", "assist", "enforce")
TECH_STATUSES = ("complete", "partial", "pending", "retry_wait",
                 "error", "stale")
VERDICTS = ("MATCH", "UNDETERMINED", "NO_MATCH")
AUDIT_STATUSES = ("PASS", "REPAIR_REQUIRED", "NEEDS_REVIEW", "PENDING",
                  "STALE")
FACT_KINDS = ("medication_event", "symptom", "explicit_request",
              "pending_item", "schedule", "preference", "observation",
              "other")
FACT_STATUSES = ("considered", "planned", "order_reported",
                 "execution_reported", "cancelled", "not_stated",
                 "conflicting")
POLARITIES = ("affirmed", "negated", "uncertain")
CLAIM_KINDS = ("reported_fact", "inference", "limitation")
CLAIM_SECTIONS = ("medication", "status", "pharmacy", "followup",
                  "progress", "flow", "other")

LLM_ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
LLM_MODEL = "Qwen3.5-9B"
LLM_TIMEOUT = 90


# ---------- config ----------

def semantic_config(cfg: dict) -> tuple[dict, list]:
    """Validate config.json's "semantic" block. Unknown/malformed values
    fail CLOSED — mode falls back to off and each error is reported, so a
    typo can never widen the rollout stage (spec §22.1, AT-067)."""
    errors = []
    out = {"mode": "off", "summary_mode": "off", "loop_mode": "off",
           "threshold_mode": "shadow_only", "calibration_version": None,
           "model": jev.JEV_MODEL,
           "daily_request_budget": 0,
           "attempt_timeout_seconds": 20.0,
           "job_budget_seconds": 45.0,
           "max_attempts_per_try": 3,
           "max_questions_per_request": 12,
           "match_threshold": jev.MATCH_THRESHOLD,
           "nomatch_threshold": jev.NOMATCH_THRESHOLD,
           "delayed_notice_seconds": 900,
           "project_ids": []}
    block = cfg.get("semantic")
    if block is None:
        return out, errors
    if not isinstance(block, dict):
        return out, ["config: semantic_not_object"]
    if block.keys() - out.keys():
        errors.append("config: semantic_unknown_field")
    mode = block.get("mode", "off")
    if mode not in MODES:
        errors.append("config: semantic_mode_invalid")
        mode = "off"
    out["mode"] = mode
    if mode != "off" and "project_ids" not in block:
        errors.append("config: semantic_project_scope_required")
    for feature in ("summary_mode", "loop_mode"):
        value = block.get(feature, "off")
        if value not in MODES:
            errors.append(f"config: semantic_{feature}_invalid")
        else:
            out[feature] = value
    threshold_mode = block.get("threshold_mode", "shadow_only")
    if threshold_mode not in ("shadow_only", "calibrated"):
        errors.append("config: semantic_threshold_mode_invalid")
    else:
        out["threshold_mode"] = threshold_mode
    calibration = block.get("calibration_version")
    if calibration is not None and (not isinstance(calibration, str)
                                    or not calibration.strip()
                                    or len(calibration) > 128):
        errors.append("config: semantic_calibration_version_invalid")
    else:
        out["calibration_version"] = calibration
    if ("enforce" in (mode, out["summary_mode"], out["loop_mode"])
            and (threshold_mode != "calibrated" or not calibration)):
        errors.append("config: semantic_calibration_required")
    if "model" in block:
        if block["model"] != jev.JEV_MODEL:
            errors.append("config: semantic_model_invalid")
            out["mode"] = "off"
        out["model"] = jev.JEV_MODEL
    for key, lo, hi in (("daily_request_budget", 0, 100000),
                        ("max_questions_per_request", 1, 48),
                        ("max_attempts_per_try", 1, 3),
                        ("delayed_notice_seconds", 60, 86400)):
        if key in block:
            v = block[key]
            if type(v) is not int or not lo <= v <= hi:
                errors.append(f"config: semantic_{key}_invalid")
            else:
                out[key] = v
    for key, lo, hi in (("attempt_timeout_seconds", 1.0, 60.0),
                        ("job_budget_seconds", 5.0, 300.0),
                        ("match_threshold", 0.0, 1.0),
                        ("nomatch_threshold", 0.0, 1.0)):
        if key in block:
            v = block[key]
            if type(v) not in (int, float) or not math.isfinite(v) \
                    or not lo <= v <= hi:
                errors.append(f"config: semantic_{key}_invalid")
            else:
                out[key] = float(v)
    if "project_ids" in block:
        v = block["project_ids"]
        if v is None:
            out["project_ids"] = None          # explicit null = all
        elif not isinstance(v, list) \
                or any(type(p) is not int or p <= 0 for p in v):
            errors.append("config: semantic_project_ids_invalid")
            out["project_ids"] = []
        else:
            out["project_ids"] = sorted(set(v))
    if out["nomatch_threshold"] >= out["match_threshold"]:
        errors.append("config: semantic_threshold_order_invalid")
    if errors:
        out["mode"] = "off"
    return out, errors


def policy_fingerprint(scfg: dict) -> str:
    """Only interpretation settings invalidate cached analysis, not retry budgets."""
    return payload_hash({key: scfg.get(key) for key in (
        "model", "match_threshold", "nomatch_threshold", "calibration_version")})


def _env(key: str) -> str | None:
    """TYPESAFE_API_KEY lookup: process env, then ~/.mcs/.env, then the
    shared ~/.hermes/.env — the key is never written to payloads/logs."""
    if os.environ.get(key):
        return os.environ[key]
    for path in (os.path.join(HOME, ".env"),
                 os.path.expanduser("~/.hermes/.env")):
        try:
            for line in open(path, encoding="utf-8"):
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return None


# ---------- fixed input bundle (spec §12.1) ----------

def _member(row) -> dict:
    return {
        "project_id": row["project_id"],
        "message_id": row["message_id"],
        "parent_id": row["parent_id"],
        "revision": row["content_hash"] or "",
        "posted_at": row["posted_at"] or "",
        "occurred_at": None,      # event time is a fact-level field
        "sender": {"id": row["sender_id"], "type": row["sender_type"] or "",
                   "profession": row["profession"] or ""},
        "source_metadata": {"sender_name": row["sender_name"] or "",
                            "organization": row["organization"] or ""},
        "body_original": row["body_text"] or "",
        "body_state": row["body_state"] or "unknown",
        "reply_count": row["reply_count"],
    }


def bundle_fingerprint(members: list, model: str = jev.JEV_MODEL) -> str:
    """Content+context revision fingerprint: any body edit, context
    change, model/registry/schema bump invalidates prior results
    (INV-15). Canonical JSON — never a lossy string concat."""
    return payload_hash({
        # Target roles are selection metadata; artifacts are selected per
        # target ID. Everything used to interpret the source is versioned.
        "members": sorted(({k: v for k, v in m.items() if k != "role"}
                           for m in members), key=lambda x: x["message_id"]),
        "model": model,
        "local_model": LLM_MODEL,
        "prompts": [_FACT_PROMPT, _SUMMARY_PROMPT, _REPAIR_SUFFIX],
        "registry": jev.REGISTRY_VERSION,
        "schema": SCHEMA_VERSION,
        "policy": POLICY_VERSION,
    })


def thread_bundle(ledger, project_id: int, root_id: int,
                  target_ids: list | None = None) -> dict | None:
    """One post + its same-thread stored replies — the atomic analysis
    unit (§12.3). A message whose row vanished makes the bundle
    unbuildable (None), never silently re-scoped (AT-019)."""
    rows = ledger.db.execute("""
      SELECT project_id,message_id,parent_id,sender_id,sender_name,organization,
             sender_type,profession,posted_at,
             body_text,body_state,content_hash,reply_count
      FROM messages WHERE project_id=? AND (message_id=? OR parent_id=?)
      ORDER BY posted_at_ts, message_id
    """, (project_id, root_id, root_id)).fetchall()
    root = [r for r in rows if r["message_id"] == root_id]
    if not root:
        return None
    member_ids = {r["message_id"] for r in rows}
    if target_ids is not None and (
            not isinstance(target_ids, list) or not target_ids
            or any(type(mid) is not int or mid not in member_ids
                   for mid in target_ids)):
        raise ValueError("semantic_target_scope")
    members = [_member(r) for r in rows]
    for m in members:
        m["attachments"] = [dict(a) for a in ledger.db.execute(
            "SELECT attachment_id,file_id,name,bytes,sha256,state FROM attachments "
            "WHERE message_id=? ORDER BY attachment_id", (m["message_id"],))]
        m["role"] = ("target" if target_ids
                     and m["message_id"] in target_ids else
                     "root" if m["parent_id"] is None else "context")
    missing_replies = max(0, (root[0]["reply_count"] or 0) - (len(rows) - 1))
    quality = "full" if not missing_replies and all(
        m["body_state"] in ("full", "deleted") for m in members) \
        else "partial"
    fp = bundle_fingerprint(members)
    return {"bundle_id": f"bundle_{project_id}_{root_id}_{fp[:12]}",
            "account_scope": "mcs",
            "project_id": project_id, "root_id": root_id,
            "members": members, "content_quality": quality,
            "context_complete": quality == "full",
            "missing_replies": missing_replies,
            "source_fingerprint": fp,
            "registry_version": jev.REGISTRY_VERSION,
            "schema_version": SCHEMA_VERSION,
            "notification_policy_version": POLICY_VERSION}


def jev_state(bundle: dict, target_id: int) -> dict | None:
    """Jev input: opaque ids, body text, sender type/profession —
    patient display names and unrelated threads are never sent
    (INV-05, §21.1)."""
    target = ctx = None
    members = bundle["members"]
    target = next((m for m in members if m["message_id"] == target_id),
                  None)
    if target is None:
        return None
    ctx = [{"id": f"m{m['message_id']}", "role": m["role"],
            "posted_at": m["posted_at"], "sender": m["sender"],
            "text": m["body_original"]}
           for m in members if m["message_id"] != target_id]
    return {"target": {"id": f"m{target_id}", "role": "target",
                       "posted_at": target["posted_at"],
                       "sender": target["sender"],
                       "text": target["body_original"]},
            "context": ctx}


# ---------- artifact helpers ----------

def _current(ledger, kind: str, message_id: int, fp: str, policy=None):
    """Latest artifact of `kind` for `message_id` whose meta fingerprint
    still matches the live input — older generations stay recorded as
    history but are never 'current' (AT-035/056)."""
    for r in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? "
            "AND message_id=? ORDER BY artifact_id DESC",
            (kind, message_id)):
        try:
            meta = json.loads(r["meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if kind == KIND_ASSESS and meta.get("fact_id") is not None:
            continue
        if meta.get("fingerprint") == fp and (
                policy is None or meta.get("policy_fingerprint") == policy):
            try:
                content = json.loads(r["content"])
            except (json.JSONDecodeError, TypeError):
                return None
            return {"content": content, "meta": meta}
    return None


def _jst_day_start(now: float) -> float:
    """Epoch of the current JST midnight — the operation's actual 'day'
    boundary (UTC midnight would reset the budget at 09:00 JST)."""
    return math.floor((now + 9 * 3600) / 86400) * 86400 - 9 * 3600


def jev_usage_today(ledger) -> int:
    """Durable daily Jev request count — shadow traffic spends real API
    budget too, so it is never unbounded (§13.5, AT-067). Counts the
    per-attempt semantic_usage rows written by run_due — one row per job
    attempt carrying that attempt's request DELTA, so every external
    call (primary, detail, claim-audit, loop-relation, failed) is
    counted exactly once."""
    day = _jst_day_start(time.time())
    total = 0
    for r in ledger.db.execute(
            "SELECT meta FROM artifacts WHERE kind=? AND created_at>=?",
            (KIND_USAGE, day)):
        try:
            total += int(json.loads(r["meta"] or "{}")
                         .get("jev_requests", 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return total


# ---------- local LLM (existing endpoint, same isolation) ----------

def llm_chat(prompt: str, timeout: int = LLM_TIMEOUT) -> str | None:
    """One local-llama.cpp chat call. The model gets no tools and no
    send capability; loopback-only opener, no proxy, no redirect
    (INV-13, §15.3). Returns raw text or None."""
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1400, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        status, _headers, raw = jev.bounded_http_request(
            LLM_ENDPOINT, "POST", body, timeout, api_key=None)
        if status != 200 or len(raw) > jev.MAX_RESPONSE_BYTES:
            return None
        out = json.loads(raw.decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError,
            json.JSONDecodeError, UnicodeDecodeError, http.client.HTTPException):
        return None
    try:
        text = out["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return text if isinstance(text, str) else None


def _json_block(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


_FACT_PROMPT = """あなたは在宅医療チャット記録の事実候補抽出器です。以下の投稿本文から、
記載されている事実候補をJSONのみで列挙してください。推測や外部知識は禁止です。
引用は本文から一字一句そのままコピーしてください。
薬剤・対象・事象・時点ごとに候補を分け、別薬剤の変更なしと増量を混ぜないでください。

各候補のキー:
- "statement": 対象・値・否定・時制を省略せず簡潔に（報告としての表現）
- "drug_ref": 原文に明示された対象薬剤名そのまま、特定不能ならnull
- "kind": medication_event|symptom|explicit_request|pending_item|schedule|preference|observation|other
- "status": considered|planned|order_reported|execution_reported|cancelled|not_stated|conflicting
- "polarity": affirmed|negated|uncertain
- "time_text": 時間表現またはnull
- "quantity": 数量表現またはnull
- "evidence_quote": 根拠となる本文の完全一致引用（必要な範囲を省略しない）

{"facts": [ ... ]} の形のみ出力。該当なしなら {"facts": []}。

本文:
<<<
%s
>>>
JSON:"""


def _iso_date(text: str) -> str | None:
    """occurred_at only when the original expression already carries an
    absolute date — relative/ambiguous text stays null and the raw
    time_text preserves it (spec §15.2)."""
    if not isinstance(text, str):
        return None
    m = re.match(r"^\s*(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if not m:
        return None
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    if not (2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return None
    from datetime import date
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _locate_quote(body: str, quote: str) -> tuple[int, int] | None:
    """Find quote's UNIQUE codepoint span in body. Ambiguous or absent
    quotes get no span — never a guessed one (INV-07, AT-029)."""
    if not body or not quote:
        return None
    first = body.find(quote)
    if first < 0 or body.find(quote, first + 1) >= 0:
        return None
    return (first, first + len(quote))


def _chunks(text: str, size: int = 3000) -> list:
    """Split into <=size chunks at line/sentence boundaries, hard-
    splitting only as a last resort. The concatenation of all chunks is
    the original text — full coverage, never head-only processing
    (§12.3, AT-017)."""
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out, buf = [], ""
    for seg in re.split(r"(?<=\n)", text):
        if len(buf) + len(seg) <= size:
            buf += seg
            continue
        if buf:
            out.append(buf)
            buf = ""
        while len(seg) > size:
            cut = max(seg.rfind("。", 0, size), seg.rfind("\n", 0, size))
            if cut <= 0:
                cut = size
            out.append(seg[:cut])
            seg = seg[cut:]
        buf = seg
    if buf:
        out.append(buf)
    return out


def extract_facts(llm_fn, member: dict,
                  deadline: float | None = None,
                  return_reason: bool = False, *, ledger=None,
                  source_fingerprint=None, project_id=None) -> tuple:
    """Whole-source extraction; durable chunks when called by the worker."""
    from semantic_extraction import extract_facts_resumable
    result = extract_facts_resumable(
        llm_fn, member, deadline, ledger=ledger,
        source_fingerprint=source_fingerprint, project_id=project_id)
    values = (result["facts"], result["complete"], result["dropped"])
    return (*values, result["failure_reason"]) if return_reason else values


_SUMMARY_PROMPT = """あなたは在宅医療チャット記録の要約器です。対象投稿と同一スレッド文脈、
検証済み事実候補から、全セクション共通のclaim構造を持つJSONのみ出力してください。

ルール:
- 各claimは {"section","text","claim_kind","fact_refs","status","polarity"}
- 一つのclaimは一つの対象・事象・時点。複数薬剤や複数時点は別claimに分割する
- section: medication|status|pharmacy|followup|progress|flow|other
- claim_kind: reported_fact|inference|limitation
- fact_refs: 根拠となる候補の番号(0起き)の配列 — reported_factは必須、空は不可
- 数値・単位・1回量/1日量は参照する原文の表記と対応を保つ。換算や合算を新たに推測しない
- 対象時点の情報として書く（現在の診察結果と断定しない）
- 「対応不要」を既定にしない。未記載は limitations に書く
- 依頼の確認提案は原文の依頼と分け、claim_kind="inference" とする
- 命題評価は別系統の参考信号 — そのまま断定に使わず、claimの真偽は
  必ず本文と事実候補から判断する

対象投稿:
<<<
%s
>>>
同一スレッド文脈:
<<<
%s
>>>
事実候補(番号付き):
%s
命題評価(参考):
%s

{"claims":[...], "limitations":[...]} のみ出力。
JSON:"""

_REPAIR_SUFFIX = """
前回の出力は監査で不合格でした。修正点:
%s
同じ入力で全claimを再生成してください。原文を書き換えず、支持されない
表現を削り、落とした候補を拾ってください。
JSON:"""


def _facts_brief(facts: list) -> str:
    lines = []
    for i, f in enumerate(facts):
        lines.append(f"{i}. [{f['kind']}/{f['status']}/{f['polarity']}] "
                     f"{f['statement']}"
                     + (f"（{f['time_text']}）" if f.get("time_text") else ""))
    return "\n".join(lines) or "(なし)"


def _verdicts_brief(verdicts: dict) -> str:
    """Jev proposition verdicts rendered as a reference signal for the
    writer — advisory context, never forced truth (§15.3)."""
    lines = []
    for pid, v in (verdicts or {}).items():
        label = jev.PROPOSITIONS.get(pid, {}).get("label_ja", pid)
        lines.append(f"{pid} {label}: {v.get('verdict', '?')}")
    return "\n".join(lines) or "(なし)"


# Safety ceiling for one local-LLM prompt. Above it the model's context
# window could silently drop input — an oversize target is flagged
# input_oversize -> NEEDS_REVIEW instead of being chopped (§12.3).
PROMPT_CHAR_LIMIT = 28000


def summarize(llm_fn, bundle: dict, target_id: int, facts: list,
              verdicts: dict, feedback: list | None = None,
              deadline: float | None = None,
              return_reason: bool = False):
    """Local-LLM summary in the common Claim schema. Jev verdicts are
    context for the writer, never forced truth (§15.3). The FULL target
    body and thread context are passed untruncated; if the prompt would
    exceed PROMPT_CHAR_LIMIT no model call is made and the result is a
    stub flagged _input_oversize (audit forces NEEDS_REVIEW — never a
    silent partial PASS).

    ``return_reason=True`` preserves the normal result while identifying a
    deadline/resource wait versus an unsuccessful model response.  Existing
    callers keep the original ``dict | None`` return shape by default."""

    def result(value, reason: str | None = None):
        return (value, reason) if return_reason else value

    target = next((m for m in bundle["members"]
                   if m["message_id"] == target_id), None)
    if target is None:
        return result(None, "invalid")
    inputs = [{"message_id": m["message_id"], "text": m["body_original"],
               "posted_at": m["posted_at"], "sender": m["sender"],
               "unparsed_attachment_count": len(m.get("attachments", []))}
              for m in bundle["members"]]
    target_input = next(m for m in inputs if m["message_id"] == target_id)
    ctx = json.dumps([m for m in inputs if m["message_id"] != target_id],
                     ensure_ascii=False)
    prompt = _SUMMARY_PROMPT % (json.dumps(target_input, ensure_ascii=False),
                                ctx, _facts_brief(facts),
                                _verdicts_brief(verdicts))
    if feedback:
        prompt += _REPAIR_SUFFIX % "\n".join(feedback[:10])
    if deadline is not None and time.monotonic() > deadline:
        return result(None, "deadline")
    if len(prompt) > PROMPT_CHAR_LIMIT:
        return result({"summary_id": f"sum_{bundle['bundle_id']}",
                       "input_bundle_id": bundle["bundle_id"],
                       "schema_version": SCHEMA_VERSION,
                       "target_message_id": target_id,
                       "claims": [],
                       "limitations": ["対象投稿または文脈が大きすぎるため"
                                       "要約を生成できませんでした"],
                       "audit_status": "pending", "_input_oversize": True})
    response = llm_fn(prompt)
    if deadline is not None and time.monotonic() > deadline:
        return result(None, "deadline")
    raw = _json_block(response or "")
    if raw is None:
        return result(None, "model")
    claims_in = raw.get("claims")
    if not isinstance(claims_in, list):
        return result(None, "model")
    claims = []
    for i, c in enumerate(claims_in):
        if not isinstance(c, dict):
            continue
        text, section = c.get("text"), c.get("section")
        kind = c.get("claim_kind", "reported_fact")
        refs = c.get("fact_refs")
        if not isinstance(text, str) or not text.strip():
            continue
        if section not in CLAIM_SECTIONS:
            section = "other"
        if kind not in CLAIM_KINDS:
            kind = "reported_fact"
        if not isinstance(refs, list):
            refs = []
        refs = [r for r in refs
                if type(r) is int and 0 <= r < len(facts)]
        # a reported_fact without evidence is INV-07's exact failure —
        # keep it flagged so the audit can fail it, never drop silently
        ev_refs = sorted({e for r in refs
                          for e in facts[r]["evidence_refs"]})
        claims.append({
            "claim_id": f"claim_{target_id}_{i}",
            "section": section, "text": text.strip(),
            "claim_kind": kind, "fact_refs": refs,
            "evidence_refs": ev_refs,
            "status": c.get("status") if c.get("status")
                      in FACT_STATUSES else "not_stated",
            "polarity": c.get("polarity") if c.get("polarity")
                        in POLARITIES else "uncertain"})
    limitations = [x for x in
                   (raw.get("limitations") or []) if isinstance(x, str)]
    attachment_count = sum(len(m.get("attachments", [])) for m in bundle["members"])
    if attachment_count:
        limitations.append(f"対象投稿・文脈に添付{attachment_count}件があります。添付内容は解析していません。")
    return result({"summary_id": f"sum_{bundle['bundle_id']}",
                   "input_bundle_id": bundle["bundle_id"],
                   "schema_version": SCHEMA_VERSION,
                   "target_message_id": target_id,
                   "claims": claims, "limitations": limitations,
                   "audit_status": "pending"})


# ---------- audit (spec §16) ----------

def audit_code(bundle: dict, facts: list, summary: dict) -> list:
    """Deterministic checks: reference integrity, span equality,
    structural enums, fact->claim coverage. Returns findings list —
    [] means the code side found no blocker (model check still runs)."""
    findings = []
    if bundle.get("content_quality") != "full":
        findings.append({"code": "input_incomplete"})
    if summary.get("_input_oversize"):
        findings.append({"code": "input_oversize"})
    members = {m["message_id"]: m for m in bundle["members"]}
    for f in facts:
        ev = f.get("_evidence")
        if f["evidence_refs"] and not ev:
            findings.append({"code": "evidence_missing",
                             "fact": f["fact_id"]})
            continue
        if not ev:
            continue
        src = members.get(ev["message_id"])
        if src is None or ev["revision_id"] != src["revision"]:
            findings.append({"code": "evidence_revision_mismatch",
                             "fact": f["fact_id"]})
            continue
        s, e = ev["start_codepoint"], ev["end_codepoint"]
        if type(s) is not int or type(e) is not int or s < 0 or s >= e \
                or e > len(src["body_original"]) \
                or src["body_original"][s:e] != ev["quote"]:
            findings.append({"code": "evidence_span_mismatch",
                             "fact": f["fact_id"]})
        # numbers/units in a fact must trace to ITS quote — appearing
        # somewhere else in the post is not tracing (AT-025/026)
        if f.get("quantity") and f["quantity"] not in ev["quote"]:
            findings.append({"code": "quantity_untraced",
                             "fact": f["fact_id"]})
    from semantic_quantities import claim_quantity_findings
    covered = set()
    for c in summary["claims"]:
        findings.extend(claim_quantity_findings(c, facts))
        for r in c["fact_refs"]:
            covered.add(r)
        if c["claim_kind"] == "reported_fact" and not c["evidence_refs"]:
            findings.append({"code": "claim_without_evidence",
                             "claim": c["claim_id"]})
    for i, f in enumerate(facts):
        if f["validation_status"] == "candidate" and i not in covered:
            findings.append({"code": "fact_dropped",
                             "fact": f["fact_id"],
                             "statement": f["statement"]})
    return findings


def audit_claims(jev_client, bundle: dict, summary: dict,
                 deadline: float, match_threshold: float = jev.MATCH_THRESHOLD) -> tuple[list, bool]:
    """Per-claim Jev support check (supports/contradicts/not_supported/
    ambiguous). Returns (findings, evaluated) — evaluated=False means
    the check could not run, i.e. PENDING rather than a silent PASS."""
    findings = []
    if jev_client is None:
        return [{"code": "support_unevaluated"}], False
    claims = [c for c in summary["claims"]
              if c["claim_kind"] != "limitation"]
    if not claims:
        return findings, True
    # one request per claim keeps question scope unambiguous (§13.2)
    questions = {}
    for c in claims:
        questions[c["claim_id"]] = jev.choice_question(
            "Does the claim in state.target.text follow from the "
            "provided source spans in state.context? Each evidence "
            "entry pairs the exact quote (role=evidence_quote) with its "
            "surrounding original-text window (role=evidence_context) — "
            "a verbatim quote negated or conditioned by neighboring "
            "text does NOT support the claim. evidence_metadata identifies "
            "the source sender, posting time, revision and parent relation; "
            "use it to check attribution and relative dates. Judge target, value, "
            "polarity and tense together — a same-topic claim with "
            "different drug/dose does not match (AT-024/025/028).",
            jev.CLAIM_SUPPORT_OPTIONS)
    members = {m["message_id"]: m for m in bundle["members"]}
    ev_ctx = {}
    for f in summary.get("_facts", []):
        ev = f.get("_evidence")
        if not ev:
            continue
        quote = ev["quote"]
        src = members.get(ev["message_id"])
        body = src["body_original"] if src else ""
        s, e = ev["start_codepoint"], ev["end_codepoint"]
        # the claim's support is judged against the quote PLUS its
        # surrounding原文 window — a bare quote cannot reveal a
        # negation or condition sitting next to it (§16.2, AT-028)
        if type(s) is int and type(e) is int and 0 <= s < e <= len(body):
            win = body
        else:
            win = ""
        metadata = {key: src.get(key) for key in
                    ("message_id", "parent_id", "revision", "posted_at", "sender")} if src else None
        ev_ctx[ev["evidence_id"]] = (quote, win, metadata)
    for c in claims:
        ctx = []
        for ev in c["evidence_refs"]:
            quote, win, metadata = ev_ctx.get(ev, ("", "", None))
            ctx.append({"id": ev, "role": "evidence_quote",
                        "text": quote})
            if win and win != quote:
                ctx.append({"id": f"{ev}_ctx",
                            "role": "evidence_context", "text": win})
            if metadata is not None:
                ctx.append({"id": f"{ev}_meta", "role": "evidence_metadata",
                            "text": json.dumps(metadata, ensure_ascii=False)})
        state = {"target": {"id": c["claim_id"], "text": c["text"]},
                 "context": ctx}
        try:
            out = jev_client.evaluate(
                state, {c["claim_id"]: questions[c["claim_id"]]},
                deadline)
        except jev.JevError as error:
            try:
                jev_client.last_error = error
            except Exception:
                pass
            return findings + [{"code": "support_unevaluated",
                                "claim": c["claim_id"]}], False
        ans = out["answers"][c["claim_id"]]
        if ans["confidence"] < match_threshold:
            findings.append({"code": "claim_low_confidence",
                             "claim": c["claim_id"],
                             "confidence": ans["confidence"]})
        if ans["choice"] in ("contradicts", "not_supported"):
            findings.append({"code": "claim_" + ans["choice"],
                             "claim": c["claim_id"],
                             "confidence": ans["confidence"]})
        elif ans["choice"] == "ambiguous":
            findings.append({"code": "claim_ambiguous",
                             "claim": c["claim_id"],
                             "confidence": ans["confidence"]})
    return findings, True


def audit_status_for(code_findings: list, jev_findings: list,
                     evaluated: bool, repaired: bool) -> str:
    if not evaluated or any(f["code"] == "input_incomplete"
                            for f in code_findings):
        return "PENDING"
    blocking = [f for f in code_findings
                if f["code"] in ("evidence_missing",
                                 "evidence_revision_mismatch",
                                 "evidence_span_mismatch",
                                 "input_oversize", "source_fact_coverage_missing",
                                 "source_fact_coverage_ambiguous",
                                 "source_fact_coverage_low_confidence",
                                 "medication_detail_low_confidence")
                or f["code"].startswith("medication_detail_")]
    if blocking:
        return "NEEDS_REVIEW"
    repairable = [f for f in code_findings + jev_findings
                  if f["code"] in ("fact_dropped", "claim_not_supported",
                                   "quantity_untraced",
                                   "claim_without_evidence")
                  or f["code"].startswith("claim_quantity_")]
    if repairable and not repaired:
        return "REPAIR_REQUIRED"
    if repairable or [f for f in jev_findings
                      if f["code"] in ("claim_contradicts",
                                       "claim_ambiguous",
                                       "claim_low_confidence")]:
        return "NEEDS_REVIEW"
    return "PASS"


# ---------- open loop candidates (spec §17) ----------
# Implementation lives in semantic_loops.py; this facade keeps the public name.
from semantic_loops import update_loops


# ---------- notification render (spec §20, §19.3) ----------

_SECTION_LABEL = {"medication": "薬剤・処方に関する情報",
                  "status": "現在の状況（対象投稿時点）",
                  "pharmacy": "薬局への影響", "followup": "未決事項／フォローアップ",
                  "progress": "前回からの進展", "flow": "投稿の流れ",
                  "other": "その他"}


def render_notice(ledger, project_id: int, root_id: int,
                  summary: dict, audit_status: str,
                  targets: list | None = None,
                  quality: str | None = None,
                  focus_mid: int | None = None) -> str:
    """The §20.1 block. Patient label + coverage/audit status lines are
    code-generated; claim text comes from the audited summary. The MCS
    link is the stored patient URL — never a guessed permalink.
    対象新着 counts only THIS generation's target posts — the rest of
    the thread is context, not arrivals. A multi-target generation
    emits one notice per target; focus_mid names WHICH covered post
    this notice's claims belong to."""
    from datetime import datetime, timedelta, timezone

    def instant(value):
        try:
            parsed = datetime.fromisoformat(value or "")
            return parsed if parsed.tzinfo is not None else None
        except ValueError:
            return None

    def stamp(value):
        return value.astimezone(timezone(timedelta(hours=9))).strftime(
            "%Y/%m/%d %H:%M JST") if value else "時刻不明"

    pat = ledger.db.execute(
        "SELECT patient_name,url FROM patients WHERE project_id=?",
        (project_id,)).fetchone()
    name = (pat["patient_name"] if pat else None) or str(project_id)
    url = (pat["url"] if pat else None) or ""
    ids = [t for t in (targets or [root_id]) if type(t) is int] \
        or [root_id]
    marks = ",".join("?" * len(ids))
    members = ledger.db.execute(
        f"SELECT message_id,posted_at FROM messages "
        f"WHERE project_id=? AND message_id IN ({marks})",
        (project_id, *ids)).fetchall()
    times = [instant(row["posted_at"]) for row in members]
    latest = stamp(max((value for value in times if value is not None),
                       default=None))
    lines = [f"【{name}】",
             f"対象新着：{len(members)}投稿"
             f"｜対象投稿の最終時刻：{latest}",
             f"取得：{'完全' if quality == 'full' else '一部未取得'}",
             f"要約：{'自動検査完了' if audit_status == 'PASS' else '要確認'}"]
    if focus_mid is not None and len(ids) > 1:
        trow = ledger.db.execute(
            "SELECT posted_at FROM messages WHERE project_id=? "
            "AND message_id=?", (project_id, focus_mid)).fetchone()
        focus_stamp = stamp(instant(trow["posted_at"])) if trow else "時刻不明"
        lines.append(f"要約対象：{focus_stamp} の投稿#{focus_mid}")
    by_sec: dict[str, list] = {}
    for c in summary.get("claims", []):
        by_sec.setdefault(c["section"], []).append(c)
    if by_sec:
        lines.append("\n■ 今回の重要情報")
        for c in by_sec.get("medication", []) + by_sec.get("status", []):
            prefix = "【提案】" if c["claim_kind"] == "inference" else ""
            lines.append(f"・{prefix}{c['text']}")
    for sec in ("pharmacy", "followup", "progress", "flow", "other"):
        if by_sec.get(sec):
            lines.append(f"\n■ {_SECTION_LABEL[sec]}")
            for c in by_sec[sec]:
                prefix = "【提案】" if c["claim_kind"] == "inference" else ""
                lines.append(f"・{prefix}{c['text']}")
    lims = summary.get("limitations") or []
    if lims:
        lines.append("\n■ 原文・制約")
        lines.extend(f"・{x}" for x in lims)
    if url.startswith("https://"):
        lines.append("\n▶ MCSで確認\n" + url)
    return "\n".join(lines)


def render_degraded(ledger, project_id: int) -> str:
    """Minimal code-generated notice for enforce-mode overruns — carries
    no unverified clinical claims (§19.3)."""
    pat = ledger.db.execute(
        "SELECT patient_name,url FROM patients WHERE project_id=?",
        (project_id,)).fetchone()
    name = (pat["patient_name"] if pat else None) or str(project_id)
    url = (pat["url"] if pat else None) or ""
    text = (f"【{name}】新着を取り込みました\n"
            "取得：保存済み\n"
            "要約：意味検査が完了していないため保留\n"
            "確認方法：MCS原文を確認してください")
    if url.startswith("https://"):
        text += "\n\n▶ MCSで確認\n" + url
    return text


def _outbox_has_delivery(ledger, delivery_key: str) -> bool:
    for r in ledger.db.execute(
            "SELECT payload FROM notify_outbox WHERE kind=? AND state IN "
            "('pending','failed','accepted','suppressed')",
            ("semantic_notice",)):
        try:
            if json.loads(r["payload"]).get("delivery_key") \
                    == delivery_key:
                return True
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    return False


def _notify_src_event(ledger, project_id: int,
                      message_ids: list) -> int | None:
    """The newest new_messages outbox intent covering any of these
    message ids — the stored origin event this evaluation descends
    from. Notification eligibility is decided by the recorded origin
    event, never by the semantic pipeline (INV-20, §20.3): a history
    import, archive-suppressed path, replay, or after-the-fact seed
    whose targets were never in a real arrival intent returns None and
    can produce artifacts only — no notice is generated (AT-055).
    A suppressed origin (archived/retracted arrival) counts as no
    origin — the send-time gate in notifier re-checks the same
    condition in case suppression lands after enqueue. Replay of a
    genuinely notified thread re-derives the same eligibility, which
    is what lets a crash-lost intent heal."""
    if not message_ids:
        return None
    want = set(message_ids)
    # A delivery attempt consumes its arrival event, not every future arrival
    # for the same message. A correction can have a separately recorded origin.
    attempted = set()
    for row in ledger.db.execute(
            "SELECT payload,state,attempts,progress FROM notify_outbox "
            "WHERE kind='semantic_notice' AND project_id=?", (project_id,)):
        try:
            payload = json.loads(row["payload"])
            progress = json.loads(row["progress"] or "{}")
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or not isinstance(progress, dict):
            return None
        if payload.get("degraded"):
            continue
        if (row["state"] in ("accepted", "in_flight")
                or row["attempts"] > 0 or progress.get("sent")):
            attempted.add((payload.get("src_event_id"), payload.get("target_message_id")))
    for r in ledger.db.execute(
            "SELECT event_id,payload FROM notify_outbox "
            "WHERE kind='new_messages' AND project_id=? "
            "AND state != 'suppressed' "
            "ORDER BY event_id DESC", (project_id,)):
        try:
            ids = json.loads(r["payload"]).get("message_ids") or []
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(ids, list):
            covered = want.intersection(ids)
            if any((r["event_id"], mid) not in attempted for mid in covered):
                return r["event_id"]
            want.difference_update(covered)
            if not want:
                return None
    return None


def _emit_degraded(ledger, scfg: dict) -> int:
    """Enforce-only fallback: a new-messages intent whose thread still
    lacks a PASS-audited summary past delayed_notice_seconds gets ONE
    code-generated degraded notice through the existing outbox — no
    clinical claims, original-check instruction only (spec §19.3).
    Each (root, fingerprint) pair dedupes via delivery_key."""
    cutoff = time.time() - scfg["delayed_notice_seconds"]
    sent = 0
    # Only events still undelivered qualify — a base notification that
    # already reached Discord (accepted) or was dropped (suppressed)
    # must never get an extra "新着取得" degraded notice (§19.3). A
    # pending/failed event this old means delivery is genuinely stuck,
    # so the minimal code-generated notice covers the silence.
    events = ledger.db.execute(
        "SELECT event_id,project_id,payload,progress FROM notify_outbox "
        "WHERE kind='new_messages' AND state IN ('pending','failed') AND attempts=0 "
        "AND created_at<?", (cutoff,))
    for ev in events:
        try:
            progress = json.loads(ev["progress"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(progress, dict) or progress.get("sent"):
            continue
        try:
            ids = json.loads(ev["payload"]).get("message_ids") or []
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(ids, list):
            continue
        ids = ids[:500]      # a malformed fat payload must not wedge the scan
        pid = ev["project_id"]
        from mcs_operations import paused
        if paused(ledger.db, pid):
            continue
        if scfg["project_ids"] is not None and pid not in \
                scfg["project_ids"]:
            continue
        marks = ",".join("?" * len(ids)) or "NULL"
        roots = {r["r"] for r in ledger.db.execute(
            f"SELECT COALESCE(parent_id,message_id) r FROM messages "
            f"WHERE project_id=? AND message_id IN ({marks})", (pid, *ids))}
        for root in roots:
            bundle = thread_bundle(ledger, pid, root)
            if bundle is None:
                continue
            fp = bundle["source_fingerprint"]
            arrivals = [m["message_id"] for m in bundle["members"]
                        if m["message_id"] in ids]
            audits = [_current(ledger, KIND_AUDIT, mid, fp, policy_fingerprint(scfg))
                      for mid in arrivals]
            if arrivals and all(a and a["meta"].get("audit_status") == "PASS"
                                for a in audits):
                continue
            dkey = payload_hash({"kind": "semantic_notice", "root": root,
                                 "fp": fp, "degraded": 1})
            if _outbox_has_delivery(ledger, dkey):
                continue
            ledger.outbox_add("semantic_notice", pid, {
                "delivery_key": dkey, "root_id": root,
                "degraded": True, "src_event_id": ev["event_id"],
                "fingerprint": fp, "policy_fingerprint": policy_fingerprint(scfg),
                "target_message_ids": arrivals,
                "text": render_degraded(ledger, pid),
                "policy_version": POLICY_VERSION})
            sent += 1
    return sent


# ---------- drain ----------

def _eval_chunked(jev_client, state: dict, questions: dict,
                  deadline: float, limit: int) -> dict:
    """evaluate() with the question set split into
    max_questions_per_request-sized chunks — the configured bound is
    enforced, not just validated (§13.4)."""
    keys = list(questions)
    merged = {"answers": {}}
    for i in range(0, len(keys), max(1, limit)):
        part = {k: questions[k] for k in keys[i:i + limit]}
        out = jev_client.evaluate(state, part, deadline)
        merged["answers"].update(out["answers"])
    return merged


def _jev_failure_class(error) -> str:
    """Classify an evaluation failure for durable job scheduling.

    ``resource`` means no external request should be counted as a failed
    input (daily cap, missing key, or exhausted job budget).  All other Jev
    retryable failures use finite job retries; permanent failures stop the
    generation until explicitly reseeded.
    """
    if getattr(error, "kind", "") in {"budget_exceeded", "no_api_key"}:
        return "resource"
    return "retry" if getattr(error, "retryable", False) else "failed"


def _plan_exists(ledger, message_id: int, fp: str,
                 status: str, policy=None) -> bool:
    """A notify_plan for THIS (generation, audit outcome) already
    recorded — replay and crash-retry must not stack duplicate plan
    rows. Keyed on status too: a plan left by a PENDING run does not
    satisfy a later PASS on the same fingerprint."""
    for r in ledger.db.execute(
            "SELECT meta FROM artifacts WHERE kind=? AND message_id=?",
            (KIND_PLAN, message_id)):
        try:
            m = json.loads(r["meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if m.get("fingerprint") == fp \
                and m.get("audit_status") == status \
                and (policy is None or m.get("policy_fingerprint") == policy):
            return True
    return False


def _write_result(ledger, pid: int, mid: int, r: dict, fp: str,
                  members: dict, final_status: str, policy: str, publication_mode: str) -> None:
    """Summary + audit artifact pair for one evaluated target — the
    durable record of this generation's outcome. The repair_count meta
    is the INV-10 budget: it survives restarts AND mid-job deferrals
    because it lives on this artifact, not in process memory."""
    ledger.artifact_add_tx(
        KIND_SUMMARY,
        json.dumps({k: v for k, v in r["summary"].items()
                    if not k.startswith("_")}, ensure_ascii=False),
        project_id=pid, message_id=mid, model=LLM_MODEL,
        meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
              "audit_status": final_status, "publication_mode": publication_mode,
              "stale": final_status == "STALE",
              "target_revision": members[mid]["revision"],
              # the stored content drops _-keys — the oversize marker
              # must survive storage or a re-audit of a PENDING-stored
              # stub would lose its blocking finding
              "input_oversize": bool(r["summary"].get("_input_oversize"))})
    ledger.artifact_add_tx(
        KIND_AUDIT,
        json.dumps({"status": final_status,
                    "findings": r["findings"],
                    "target_message_id": mid}, ensure_ascii=False),
        project_id=pid, message_id=mid, model=jev.JEV_MODEL,
        meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
              "audit_status": final_status, "publication_mode": publication_mode,
              "repair_count": 1 if r["repaired"] else 0,
              "jev_requests": r.get("jev_requests", 0)})


def _process_job_inner(ledger, scfg, job, jev_client, llm_fn, deadline,
                       cfg_path: str | None = None,
                       config_generation: str | None = None,
                       reserve_fn=None) -> str:
    """One semantic job -> durable artifacts + job state transition.
    Returns 'done'|'deferred'|'retry'|'failed'."""
    pid, root = job["project_id"], job["message_id"]
    token = runtime.JobToken.from_row(job)
    deadline = runtime.job_deadline(scfg, deadline)
    try:
        pl = json.loads(job["payload"])
    except (json.JSONDecodeError, TypeError):
        return "failed"
    if not isinstance(pl, dict):
        return "failed"
    targets = pl.get("targets", [root])
    if targets is None:
        return "failed"
    try:
        bundle = thread_bundle(ledger, pid, root, targets)
    except ValueError:
        return "failed"
    if bundle is None:
        # source vanished — nothing to preserve for it; mark done so
        # the row does not re-run as a no-op on every drain
        return "done" if runtime.transition(ledger, token, "done") \
            else "stale"
    fp = bundle["source_fingerprint"]
    policy = policy_fingerprint(scfg)

    def current_source_fp():
        fresh = thread_bundle(ledger, pid, root, targets or [root])
        return fresh["source_fingerprint"] if fresh else None

    def guard(stage):
        try:
            runtime.guard(
                ledger, token, deadline=deadline,
                expected_config_generation=config_generation,
                expected_mode=scfg["mode"], cfg_path=cfg_path,
                load_cfg=load_config, parse_cfg=semantic_config,
                source_fingerprint=fp, current_source=current_source_fp,
                stage=stage)
        except runtime.RuntimeStale as error:
            # Keep the stale marker bound to the worker's original
            # fingerprint.  A fresh bundle must never be used to label an
            # old worker's result.
            if error.stage.endswith(":source"):
                ledger.artifact_add(
                    KIND_AUDIT,
                    json.dumps({"status": "STALE", "findings": [],
                                "target_message_id": root},
                               ensure_ascii=False),
                    project_id=pid, message_id=root, model=jev.JEV_MODEL,
                    meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
                          "audit_status": "STALE",
                          "technical_status": "stale"})
            raise

    # The adapters below make every actual Jev/local-model boundary pass
    # through the same identity check.  A real Jev client additionally gets
    # a hook before each internal retry attempt and a durable reservation
    # immediately before the POST.
    jev_client, _ = runtime.bind_jev(jev_client, guard, reserve_fn)
    llm_fn = runtime.guarded_llm(llm_fn, guard, deadline,
                                 timeout_cap=LLM_TIMEOUT)
    # provenance on the recorded bundle (spec §12.1): which stored
    # origin event / capture path this evaluation descends from
    origin = pl.get("origin") if isinstance(pl.get("origin"), dict) \
        else {}
    bundle["origin_event_id"] = origin.get("event_id")
    bundle["capture_origin"] = origin.get("source")
    if _current(ledger, KIND_BUNDLE, root, fp, policy) is None:
        ledger.artifact_add(
            KIND_BUNDLE, json.dumps(bundle, ensure_ascii=False),
            project_id=pid, message_id=root, model=jev.JEV_MODEL,
            meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION})
    all_targets = [t for t in (targets or [root])
                   if any(m["message_id"] == t
                          for m in bundle["members"])]
    members = {m["message_id"]: m for m in bundle["members"]}
    facts_by_target: dict[int, list] = {}
    coverage_by_target: dict[int, list] = {}
    detail_findings: dict[int, list] = {}
    verdicts: dict[int, dict] = {}
    incomplete = False
    hard_fail = False   # non-retryable Jev error — bound the retries
    retryable_failure = False
    resource_wait = False
    for mid in all_targets:
        member = members[mid]
        # restart-safe: a complete assessment for THIS fingerprint is
        # reused; retry_wait/error/pending ones are re-attempted
        prev = _current(ledger, KIND_ASSESS, mid, fp, policy)
        if prev and prev["meta"].get("technical_status") == "complete":
            verdicts[mid] = prev["content"].get("verdicts", {})
        else:
            if time.monotonic() > deadline - 5:
                incomplete = True
                break
            state = jev_state(bundle, mid)
            meta_base = {"fingerprint": fp, "policy_fingerprint": policy, "model": jev.JEV_MODEL,
                         "registry": jev.REGISTRY_VERSION,
                         "schema": SCHEMA_VERSION}
            answers = None
            req0 = jev_client.requests_made if jev_client else 0
            if jev_client is not None and state is not None:
                qs = {k: jev.noul_question(p["instructions"], p["true"],
                                           p["false"])
                      for k, p in jev.PROPOSITIONS.items()}
                try:
                    out = _eval_chunked(
                        jev_client, state, qs, deadline,
                        scfg["max_questions_per_request"])
                    answers = out["answers"]
                except jev.JevError as e:
                    meta_base["technical_status"] = (
                        "retry_wait" if e.retryable else "error")
                    meta_base["error_kind"] = e.kind
                    if _jev_failure_class(e) == "resource":
                        resource_wait = True
                    elif e.retryable:
                        retryable_failure = True
                    else:
                        hard_fail = True
            meta_base["jev_requests"] = (
                jev_client.requests_made - req0) if jev_client else 0
            if answers is None:
                meta_base.setdefault("technical_status", "pending")
                meta_base.setdefault("error_kind", "jev_unavailable")
                # an identical wait/error record adds no information —
                # only a status CHANGE earns a new row, so a durable
                # resource wait (Jev down, no key, budget hold) does not
                # append one artifact per drain forever
                if prev is None or (
                        prev["meta"].get("technical_status"),
                        prev["meta"].get("error_kind")) != (
                        meta_base["technical_status"],
                        meta_base.get("error_kind")):
                    ledger.artifact_add(
                        KIND_ASSESS,
                        json.dumps({"target_message_id": mid,
                                    "verdicts": {}},
                                   ensure_ascii=False),
                        project_id=pid, message_id=mid,
                        model=jev.JEV_MODEL, meta=meta_base)
                incomplete = True
                continue
            verdicts[mid] = {k: {"noul": a["noul"],
                                 "verdict": jev.verdict_for(
                                     a["noul"], scfg["match_threshold"],
                                     scfg["nomatch_threshold"])}
                             for k, a in answers.items()}
            detail = {}
            meta_base["jev_requests"] = (
                jev_client.requests_made - req0) if jev_client else 0
            ledger.artifact_add(
                KIND_ASSESS,
                json.dumps({"target_message_id": mid,
                            "verdicts": verdicts[mid],
                            "detail": detail}, ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={**meta_base, "technical_status": "complete"})
        if scfg["summary_mode"] == "off" and scfg["loop_mode"] == "off":
            continue
        # facts: reuse a stored set for this fingerprint, else extract
        prev_f = _current(ledger, KIND_FACTS, mid, fp)
        if prev_f is not None:
            facts = prev_f["content"].get("facts", [])
        else:
            if time.monotonic() > deadline - 5:
                incomplete = True
                break
            facts, f_complete, f_dropped, f_reason = extract_facts(
                llm_fn, member, deadline - 5, return_reason=True,
                ledger=ledger, source_fingerprint=fp, project_id=pid)
            if not f_complete:
                if f_reason == "model":
                    retryable_failure = True
                incomplete = True
                break
            ledger.artifact_add(
                KIND_FACTS,
                json.dumps({"facts": facts,
                            "evidence": {f["_evidence"]["evidence_id"]:
                                         f["_evidence"] for f in facts
                                         if f.get("_evidence")}},
                           ensure_ascii=False),
                project_id=pid, message_id=mid, model=LLM_MODEL,
                meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
                      "chunks_total": len(_chunks(
                          member["body_original"])),
                      "dropped_by_cap": f_dropped})
        facts_by_target[mid] = facts
        from semantic_assessment import evaluate_medication_events
        event_details = evaluate_medication_events(
            ledger, bundle, mid, facts, jev_client, scfg, deadline - 5)
        guard("medication_detail_result")
        detail_findings[mid] = event_details["findings"]
        if not event_details["complete"] and event_details["failure_reason"] != "invalid":
            incomplete = True
            error = getattr(jev_client, "last_error", None)
            if error is not None and _jev_failure_class(error) == "failed":
                hard_fail = True
            elif event_details["failure_reason"] != "deadline" and (
                    error is None or _jev_failure_class(error) != "resource"):
                retryable_failure = True
            break
        if scfg["loop_mode"] != "off":
            guard("loop_candidates")
            update_loops(ledger, pid, bundle, {mid: facts}, None, scfg, deadline)
        if scfg["summary_mode"] != "off":
            previous = _current(ledger, "semantic_coverage", mid, fp, policy)
            coverage = previous["content"] if previous else None
            if not coverage or not coverage.get("evaluated"):
                from semantic_extraction import evaluate_source_fact_coverage
                coverage = evaluate_source_fact_coverage(
                    jev_client, member["body_original"], facts, deadline,
                    target_id=f"m{mid}", match_threshold=scfg["match_threshold"])
                guard("coverage_result")
                ledger.artifact_add(
                    "semantic_coverage", json.dumps(coverage, ensure_ascii=False),
                    project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                    meta={"fingerprint": fp, "policy_fingerprint": policy,
                          "technical_status": "complete" if coverage["evaluated"] else "pending"})
            coverage_by_target[mid] = coverage["findings"]
            if not coverage["evaluated"]:
                incomplete = True
                error = getattr(jev_client, "last_error", None)
                if error is not None and _jev_failure_class(error) == "failed":
                    hard_fail = True
                elif error is not None and _jev_failure_class(error) == "retry":
                    retryable_failure = True
                elif coverage.get("failure_reason") == "model":
                    retryable_failure = True
                break
    if incomplete:
        if hard_fail:
            return "failed"
        if retryable_failure:
            # deterministic failure (e.g. protocol_error, oversized
            # payload) — deferring forever would burn a Jev call every
            # tick on an input that can never pass; bounded retry ends
            # 'failed' where status_report can surface it
            return "retry"
        return "deferred"

    # summary + audit. A TERMINAL audit (PASS/NEEDS_REVIEW) for THIS
    # fingerprint is reused — its notification intent was already
    # committed atomically below. A PENDING one is NOT final: the
    # stored summary is re-audited so a mid-audit outage can never
    # wedge the thread on a stale PENDING marker (AT-058).
    results = {}
    for mid, facts in facts_by_target.items():
        if scfg["summary_mode"] == "off":
            continue
        existing = _current(ledger, KIND_SUMMARY, mid, fp, policy)
        prev_audit = _current(ledger, KIND_AUDIT, mid, fp, policy)
        if existing and existing["meta"].get("publication_mode") != scfg["summary_mode"]:
            existing = None
            prev_audit = None
        prev_status = (prev_audit["meta"].get("audit_status")
                       if prev_audit else None)
        if existing is not None and prev_status in ("PASS",
                                                    "NEEDS_REVIEW"):
            results[mid] = {"summary": existing["content"],
                            "status": prev_status, "findings": [],
                            "repaired": False, "fresh": False}
            continue
        if time.monotonic() > deadline - 5:
            incomplete = True
            break
        summary_reason = None
        if existing is not None:
            summary = existing["content"]
            if existing["meta"].get("input_oversize"):
                summary["_input_oversize"] = True
        else:
            candidate = _current(ledger, "semantic_candidate", mid, fp, policy)
            if candidate and candidate["meta"].get("publication_mode") == scfg["summary_mode"]:
                summary = candidate["content"]
            else:
                summary, summary_reason = summarize(
                    llm_fn, bundle, mid, facts, verdicts.get(mid, {}),
                    deadline=deadline, return_reason=True)
        if summary is None:
            # same dedup as the assess wait-state above: a persistent
            # local-LLM outage defers without stacking identical
            # PENDING rows on every drain
            if summary_reason == "model":
                retryable_failure = True
            prev_a = _current(ledger, KIND_AUDIT, mid, fp, policy)
            if not (prev_a
                    and prev_a["meta"].get("technical_status")
                    == "pending"
                    and prev_a["content"].get("status") == "PENDING"):
                ledger.artifact_add(
                    KIND_AUDIT,
                    json.dumps({"status": "PENDING",
                                "findings":
                                    [{"code": "summary_unavailable"}],
                                "target_message_id": mid},
                               ensure_ascii=False),
                    project_id=pid, message_id=mid, model=LLM_MODEL,
                    meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
                          "technical_status": "pending"})
            incomplete = True
            continue
        # Preserve the pre-audit output for the fixed-bundle comparison (§24.1).
        # It is never eligible for publication or automatic adoption.
        if existing is None and _current(ledger, "semantic_candidate", mid, fp, policy) is None:
            guard("candidate_snapshot")
            ledger.artifact_add(
                "semantic_candidate", json.dumps(summary, ensure_ascii=False),
                project_id=pid, message_id=mid, model=LLM_MODEL,
                meta={"fingerprint": fp, "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION, "stage": "pre_audit",
                      "publication_mode": scfg["summary_mode"],
                      "target_revision": members[mid]["revision"]})
        # Reserve before dispatch: a crash cannot refund the repair (INV-10).
        repaired = bool(_current(ledger, "semantic_repair", mid, fp)
                        or (prev_audit
                            and prev_audit["meta"].get("repair_count", 0) >= 1))
        summary["_facts"] = facts   # evidence context for the Jev audit
        req0 = jev_client.requests_made if jev_client else 0
        findings = []
        status = "PENDING"
        for _attempt in range(2):      # initial + at most one repair
            code_f = (audit_code(bundle, facts, summary) + coverage_by_target.get(mid, [])
                      + detail_findings.get(mid, []))
            jev_f, evaluated = audit_claims(jev_client, bundle,
                                            summary, deadline, scfg["match_threshold"])
            if not evaluated and jev_client is not None:
                error = getattr(jev_client, "last_error", None)
                if error is not None:
                    if _jev_failure_class(error) == "resource":
                        resource_wait = True
                    elif getattr(error, "retryable", False):
                        retryable_failure = True
                    else:
                        hard_fail = True
            findings = code_f + jev_f
            status = audit_status_for(code_f, jev_f, evaluated,
                                      repaired)
            if status == "REPAIR_REQUIRED" and not repaired:
                guard("repair_reservation")
                ledger.artifact_add(
                    "semantic_repair", json.dumps({"target_message_id": mid}),
                    project_id=pid, message_id=mid, model=LLM_MODEL,
                    meta={"fingerprint": fp, "policy_fingerprint": policy, "repair_count": 1})
                repaired = True
                summary2 = summarize(
                    llm_fn, bundle, mid, facts, verdicts.get(mid, {}),
                    feedback=[f.get("code", "") + " " +
                              f.get("statement", f.get("claim", ""))
                              for f in code_f + jev_f])
                if summary2 is not None:
                    summary = summary2
                    summary["_facts"] = facts
                    continue
                status = "NEEDS_REVIEW"
                findings.append({"code": "repair_unavailable"})
            break
        summary["audit_status"] = status
        results[mid] = {"summary": summary, "status": status,
                        "findings": findings, "repaired": repaired,
                        "fresh": True,
                        "jev_requests": (jev_client.requests_made - req0)
                        if jev_client else 0}
    if incomplete:
        if results:
            try:
                guard("partial_promote")
            except runtime.RuntimeGuardError:
                return "stale"
            # commit each completed target's outcome NOW — dropping it
            # would re-spend Jev calls on an identical input next run
            # AND silently reset the one-shot repair budget, whose
            # counter lives on these artifacts (INV-10, §18.2)
            with ledger.db:
                for mid, r in results.items():
                    if r["fresh"]:
                        _write_result(ledger, pid, mid, r, fp, members,
                                      r["status"], policy, scfg["summary_mode"])
        if hard_fail:
            return "failed"
        if retryable_failure:
            return "retry"
        return "deferred"

    # generation guard: the bundle, config, and complete job identity must
    # still be current before promotion.  The same guard also runs before
    # every Jev/local-model call through the wrappers above.
    stale = False
    try:
        guard("promote")
    except runtime.RuntimeGuardError:
        stale = True
    pending = not stale and any(r["status"] == "PENDING"
                                for r in results.values())
    loops_done = True
    if not stale and scfg["loop_mode"] != "off":
        # loop candidates/relation events for the LIVE generation —
        # kept OUT of the commit tx because matching makes Jev calls
        # (no network inside a DB transaction, §6.2); replay-safe via
        # candidate_fp and pair dedup.
        _, loops_done = update_loops(ledger, pid, bundle,
                                     facts_by_target, jev_client,
                                     scfg, deadline)
        if not loops_done and jev_client is not None:
            error = getattr(jev_client, "last_error", None)
            if error is not None:
                if _jev_failure_class(error) == "resource":
                    resource_wait = True
                elif getattr(error, "retryable", False):
                    retryable_failure = True
                else:
                    hard_fail = True
    # notification eligibility is derived from the stored origin event,
    # not from this job's seed provenance (INV-20, §20.3) — evaluated
    # per target below: a covering new_messages intent must exist for
    # the message a notice reports on.
    with ledger.db:
        for mid, r in results.items():
            final_status = "STALE" if stale else r["status"]
            if r["fresh"]:
                _write_result(ledger, pid, mid, r, fp, members,
                              final_status, policy, scfg["summary_mode"])
            if stale:
                continue
            # notification plan + outbox intent commit in the SAME
            # transaction as the analysis result — a crash can never
            # leave a saved summary without its notification intent
            # (§18.3). Reused results re-enter here so an intent lost
            # by an older crash is recreated on the next run. The two
            # dedupes are independent: the plan row keys on
            # (fingerprint, audit_status), the outbox row on its
            # delivery_key — a plan written by an earlier PENDING/shadow
            # run must not block a now-PASS enforce enqueue.
            summary_clean = {k: v for k, v in r["summary"].items()
                             if not k.startswith("_")}
            text = render_notice(ledger, pid, root, summary_clean,
                                 final_status, targets=all_targets,
                                 quality=bundle["content_quality"],
                                 focus_mid=mid)
            enqueued = False
            if (scfg["mode"] == "enforce" and scfg["summary_mode"] == "enforce"
                    and final_status == "PASS" and not pl.get("notification_free")):
                # per-target delivery key — one generation emits one
                # notice PER audited target; a shared (root, fp) key
                # would dedup every later target's claims out of the
                # notice while the header still claims them
                dkey = payload_hash({"kind": "semantic_notice",
                                     "root": root, "fp": fp,
                                     "mid": mid, "policy": policy})
                # per-target eligibility: THIS message must be covered
                # by a stored new_messages intent — a target that only
                # ever arrived via import/replay produces artifacts but
                # no notice, even when a sibling target was notified
                # (INV-20, AT-055)
                src_mid = _notify_src_event(ledger, pid, [mid])
                if src_mid is not None \
                        and not _outbox_has_delivery(ledger, dkey):
                    ledger.outbox_add_tx("semantic_notice", pid, {
                        "delivery_key": dkey, "root_id": root,
                        "target_message_id": mid,
                        "target_revision": members[mid]["revision"],
                        "src_event_id": src_mid, "text": text,
                        "fingerprint": fp,
                        "policy_version": POLICY_VERSION,
                        "policy_fingerprint": policy})
                    enqueued = True
            if not _plan_exists(ledger, mid, fp, final_status, policy):
                plan_meta = {"fingerprint": fp, "policy_fingerprint": policy,
                             "audit_status": final_status, "publication_mode": scfg["summary_mode"],
                             "mode": scfg["mode"],
                             "origin": pl.get("origin")}
                if enqueued:
                    plan_meta["enqueued"] = True
                ledger.artifact_add_tx(KIND_PLAN, json.dumps(
                    {"root_id": root, "target_message_id": mid,
                     "text": text}, ensure_ascii=False),
                    project_id=pid, message_id=mid,
                    meta=plan_meta)
        if not stale and not pending and loops_done:
            if not runtime.transition_tx(ledger, token, "done"):
                raise runtime.RuntimeStale("promote")
    if stale:
        return "stale"
    if hard_fail:
        return "failed"
    if pending:
        # evaluation incomplete (e.g. Jev outage mid-audit) — the job
        # must not sit 'done' on an unfinished audit; consume a bounded
        # retry attempt so a persistent outage eventually fails rather
        # than busy-loops (AT-058)
        return "deferred" if resource_wait and not (
            hard_fail or retryable_failure) else "retry"
    if not loops_done:
        # relation pass truncated by the pair budget/deadline — the
        # results committed above stand; the job re-runs to evaluate
        # the remaining (candidate, target) pairs (§17.2 持ち越し)
        return "retry" if hard_fail or retryable_failure else "deferred"
    return "done"


def _process_job(ledger, scfg, job, jev_client, llm_fn, deadline,
                 cfg_path: str | None = None,
                 config_generation: str | None = None,
                 reserve_fn=None) -> str:
    """Preserve stale workers while bounding timeouts after dispatched work."""
    requests_before = jev_client.requests_made if jev_client is not None else 0
    llm_started = False

    @wraps(llm_fn)
    def tracked_llm(*args, **kwargs):
        nonlocal llm_started
        llm_started = True
        return llm_fn(*args, **kwargs)

    try:
        return _process_job_inner(
            ledger, scfg, job, jev_client, tracked_llm, deadline,
            cfg_path=cfg_path, config_generation=config_generation,
            reserve_fn=reserve_fn)
    except runtime.RuntimeStale:
        return "stale"
    except runtime.RuntimeOff:
        return "stale"
    except runtime.RuntimeBudget:
        sent = (jev_client is not None
                and jev_client.requests_made > requests_before)
        return "retry" if sent or llm_started else "deferred"


def run_due(ledger, cfg: dict, result: dict, deadline: float,
            jev_client=None, llm_fn=None, max_jobs: int = 4,
            cfg_path: str | None = None) -> dict:
    """Drain due semantic jobs inside the tick's remaining budget.
    OFF returns immediately — no job creation, no external calls, no
    auto-drain (AT-053). The caller's shared run lock is held by
    run_check; a standalone CLI takes acquire_run_lock itself.
    cfg_path, when given, re-validates config between jobs so an OFF
    flip mid-run takes effect at the next job boundary (AT-060)."""
    if type(max_jobs) is not int or not 1 <= max_jobs <= 32:
        raise ValueError("semantic_max_jobs_invalid")
    scfg, errors = semantic_config(cfg)
    cfg_generation = runtime.config_generation(cfg)
    for e in errors:
        if e not in result["errors"]:
            result["errors"].append(e)
    out = {"mode": scfg["mode"], "done": 0, "deferred": 0,
           "failed": 0, "left": None, "budget_exhausted": False}
    if scfg["mode"] == "off":
        return out
    drain_started = time.perf_counter()
    out["job_metrics"] = []
    oldest = ledger.db.execute(
        "SELECT MIN(created_at) FROM fetch_jobs WHERE kind=? AND state='pending'",
        (JOB_KIND,)).fetchone()[0]
    out["oldest_pending_job_age_s"] = (
        max(0.0, time.time() - oldest) if oldest is not None else None)
    policy = policy_fingerprint(scfg)
    previous_policy = ledger.db.execute(
        "SELECT content FROM artifacts WHERE kind='semantic_policy' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    if previous_policy is None or previous_policy["content"] != policy:
        ledger.artifact_add("semantic_policy", policy)
    if llm_fn is None:
        llm_fn = llm_chat
    if jev_client is None and scfg["daily_request_budget"] > 0:
        jev_client = jev.JevClient(
            api_key=_env("TYPESAFE_API_KEY"), model=scfg["model"],
            attempt_timeout=scfg["attempt_timeout_seconds"],
            job_budget=scfg["job_budget_seconds"],
            max_attempts=scfg["max_attempts_per_try"])
    # arrival-descended seeds outrank import/replay seeds: a deep
    # backfill must never starve a fresh notification's evaluation
    # (§19.1's existing-work-first ordering applied inside the queue
    # too). 'eligible' is set at seed time and survives payload merges,
    # so a merged arrival+history job keeps its priority.
    due = ledger.db.execute("""
      SELECT * FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind=?
      ORDER BY CASE WHEN json_valid(payload)
                    AND json_extract(payload, '$.eligible') = 1
                    THEN 0 ELSE 1 END, job_id
      LIMIT ?
    """, (time.time(), JOB_KIND, max_jobs)).fetchall()
    for job in due:
        token = runtime.JobToken.from_row(job)
        payload = runtime.parse_payload(job)
        limit = runtime.attempt_limit(payload)
        if runtime.circuit_open(ledger):
            out["circuit_open"] = True
            break
        from mcs_operations import paused
        if paused(ledger.db, job["project_id"]):
            runtime.transition(ledger, token, "defer", retry_in=300)
            out["deferred"] += 1
            continue
        if cfg_path is not None:
            current_cfg = load_config(cfg_path)
            scfg, errs = semantic_config(current_cfg)
            cfg_generation = runtime.config_generation(current_cfg)
            for e in errs:
                if e not in result["errors"]:
                    result["errors"].append(e)
            if scfg["mode"] == "off":
                runtime.transition(ledger, token, "defer", retry_in=300)
                out["deferred"] += 1
                out["mode"] = "off"
                break
        if time.monotonic() > deadline - 15:
            out["deferred"] += 1
            break
        if scfg["project_ids"] is not None \
                and job["project_id"] not in scfg["project_ids"]:
            # outside the rollout scope — defer instead of leaving it
            # due-now, so a backlog of out-of-scope rows cannot fill
            # the whole max_jobs window and starve in-scope work. The
            # row stays pending: a later project_ids change resumes it.
            runtime.transition(ledger, token, "defer", retry_in=300)
            out["deferred"] += 1
            continue
        if token.attempts < 0 or token.attempts >= limit:
            # A row can be re-seeded or manually edited while it is waiting
            # in this due-list snapshot.  Close an exhausted row with the
            # complete token CAS before any client, budget, or network path.
            # Keep OFF/paused/out-of-scope rows untouched; they retain the
            # existing queue hold contract until the feature can run again.
            if runtime.transition(ledger, token, "failed"):
                out["failed"] += 1
            else:
                out["deferred"] += 1
            continue
        if jev_client is not None:
            # the daily cap binds at REQUEST granularity, not just job
            # granularity — one job's claim-audit + loop-relation calls
            # must not overshoot it mid-flight (§13.5)
            remaining = (scfg["daily_request_budget"]
                         - jev_usage_today(ledger))
            if remaining <= 0:
                result["errors"].append(
                    "semantic: daily_budget_exhausted")
                out["budget_exhausted"] = True
                break
            jev_client.request_cap = (jev_client.requests_made
                                      + remaining)
            if hasattr(jev_client, "attempt_timeout"):
                jev_client.attempt_timeout = scfg["attempt_timeout_seconds"]
            if hasattr(jev_client, "job_budget"):
                jev_client.job_budget = scfg["job_budget_seconds"]
        reserve_fn = None
        if (jev_client is not None
                and getattr(jev_client, "_mcs_jev_hookable", False)):
            reserve_fn = runtime.usage_reserver(
                ledger, token, kind=KIND_USAGE, model=jev.JEV_MODEL,
                project_id=job["project_id"], message_id=job["message_id"])
        req0 = jev_client.requests_made if jev_client is not None else 0
        usage_before = dict(getattr(jev_client, "usage_totals", {}))
        job_started = time.perf_counter()
        job_age = max(0.0, time.time() - job["created_at"])
        try:
            status = _process_job(ledger, scfg, job, jev_client,
                                  llm_fn, deadline, cfg_path=cfg_path,
                                  config_generation=cfg_generation,
                                  reserve_fn=reserve_fn)
        except Exception as e:
            runtime.transition(ledger, token, "retry", retry_in=300,
                               max_attempts=limit)
            result["errors"].append(
                f"semantic {job['message_id']}: {type(e).__name__}")
            out["failed"] += 1
            status = None
        job_elapsed = time.perf_counter() - job_started
        # durable per-attempt request delta — the daily budget ledger
        # (jev_usage_today) is exact and covers claim-audit and loop
        # calls, not just the primary assessment
        used = (jev_client.requests_made - req0) \
            if jev_client is not None else 0
        usage_after = getattr(jev_client, "usage_totals", {})
        usage = {key: usage_after.get(key, 0) - usage_before.get(key, 0)
                 for key in ("input_tokens", "output_tokens", "reported_requests")}
        usage["unreported_requests"] = used - usage["reported_requests"]
        out["job_metrics"].append({
            "job_id": job["job_id"], "project_id": job["project_id"],
            "generation": token.generation,
            "status": status if status is not None else "error",
            "elapsed_s": job_elapsed, "job_age_s": job_age,
            "jev_requests": used, "usage": usage,
        })
        if used:
            runtime.record_circuit_result(ledger, getattr(jev_client, "last_error", None))
        # Real Jev calls reserve one usage row before POST.  A crash after
        # that commit therefore still spends the daily cap; adding the old
        # post-job delta would double count it.  Injected fake clients keep
        # the delta row for compatibility with offline tests.
        if used and reserve_fn is None:
            ledger.artifact_add(
                KIND_USAGE, json.dumps({"job_id": job["job_id"]}),
                project_id=job["project_id"],
                message_id=job["message_id"], model=jev.JEV_MODEL,
                meta={"jev_requests": used})
        if status is None:
            continue
        if status == "done":
            out["done"] += 1
        elif status == "deferred":
            out["deferred"] += 1
            runtime.transition(ledger, token, "defer", retry_in=60)
        elif status == "retry":
            runtime.transition(ledger, token, "retry", retry_in=300,
                               max_attempts=limit)
            out["deferred"] += 1
        elif status == "failed":
            runtime.transition(ledger, token, "retry", max_attempts=1)
            out["failed"] += 1
        elif status == "stale":
            # The worker that observed this row no longer owns it.  Do not
            # defer/retry/done the replacement generation by ID alone.
            out["deferred"] += 1
        else:
            out["failed"] += 1
    if scfg["mode"] == "enforce" and scfg["summary_mode"] == "enforce":
        try:
            out["degraded_notices"] = _emit_degraded(ledger, scfg)
        except Exception:
            out["degraded_notices"] = 0
            result["errors"].append("semantic: degraded_scan_failed")
    out["left"] = len(ledger.job_due(limit=50, kind=JOB_KIND))
    out["elapsed_s"] = time.perf_counter() - drain_started
    return out


def seed(ledger, message_id: int, origin: str = "replay",
         cfg_path: str = CONF_PATH) -> int | None:
    """Explicit finite replay/retry seed — notification-free. OFF blocks
    even this (no new job generation, AT-053)."""
    cfg, _ = semantic_config(load_config(cfg_path))
    if cfg["mode"] == "off":
        return None
    row = ledger.db.execute(
        "SELECT project_id, COALESCE(parent_id,message_id) r "
        "FROM messages WHERE message_id=?", (message_id,)).fetchone()
    from mcs_operations import paused
    if row is None or paused(ledger.db, row["project_id"]):
        return None
    if cfg["project_ids"] is not None \
            and row["project_id"] not in cfg["project_ids"]:
        # out of the rollout scope — seeding it would only churn in
        # drain-time defers
        return None
    ledger.semantic_seed(row["project_id"], [message_id],
                         {"source": origin, "notification_free": True})
    return row["r"]


def status_report(ledger) -> dict:
    jobs = {"pending": 0, "failed": 0, "done": 0}
    for r in ledger.db.execute(
            "SELECT state,COUNT(*) c FROM fetch_jobs WHERE kind=? "
            "GROUP BY state", (JOB_KIND,)):
        jobs[r["state"]] = r["c"]
    audits = {}
    for r in ledger.db.execute(
            "SELECT meta FROM artifacts WHERE kind=?", (KIND_AUDIT,)):
        try:
            s = json.loads(r["meta"] or "{}").get("audit_status")
        except (json.JSONDecodeError, TypeError):
            s = None
        audits[s or "unparsed"] = audits.get(s or "unparsed", 0) + 1
    loops = ledger.db.execute(
        "SELECT COUNT(*) c FROM artifacts WHERE kind=?",
        (KIND_LOOP,)).fetchone()["c"]
    from mcs_operations import paused
    paused_projects = [r[0] for r in ledger.db.execute(
        "SELECT DISTINCT project_id FROM artifacts WHERE kind='semantic_control' "
        "AND project_id IS NOT NULL ORDER BY project_id") if paused(ledger.db, r[0])]
    oldest = ledger.db.execute(
        "SELECT MIN(created_at) FROM fetch_jobs WHERE kind=? AND state='pending'",
        (JOB_KIND,)).fetchone()[0]
    return {"semantic_jobs": jobs, "audit_statuses": audits,
            "oldest_pending_job_age_s": (max(0.0, time.time() - oldest)
                                         if oldest is not None else None),
            "semantic_paused_projects": paused_projects,
            "loop_candidates": loops,
            "jev_requests_today": jev_usage_today(ledger),
            "jev_circuit_open": runtime.circuit_open(ledger)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    actions = ap.add_mutually_exclusive_group(required=True)
    actions.add_argument("--status", action="store_true",
                    help="read-only status (LedgerReader, no lock)")
    actions.add_argument("--drain", action="store_true",
                    help="process due semantic jobs now (writer lock)")
    actions.add_argument("--replay", type=int, metavar="MESSAGE_ID",
                    help="seed one finite evaluation job (writer lock)")
    ap.add_argument("--max-jobs", type=int, default=4)
    ap.add_argument("--offline", action="store_true",
                    help="forbid network; status and local replay seeding only")
    args = ap.parse_args()
    if not 1 <= args.max_jobs <= 32:
        ap.error("max-jobs must be between 1 and 32")
    if args.replay is not None and not 0 < args.replay < 2**63:
        ap.error("replay message ID must be a positive SQLite integer")
    if args.offline and args.drain:
        ap.error("offline mode cannot drain model jobs")
    if args.status:
        try:
            reader = LedgerReader(DB)
        except Exception as e:
            print(json.dumps({"ok": False,
                              "error": type(e).__name__}))
            return 1
        print(json.dumps(status_report(reader), ensure_ascii=False))
        reader.close()
        return 0
    lock_fd = acquire_run_lock()
    if lock_fd is None:
        print(json.dumps({"ok": False, "error": "lock_held"}))
        return 3
    try:
        ledger = Ledger(DB)
    except Exception:
        os.close(lock_fd)
        print(json.dumps({"ok": False, "error": "ledger_init_failed"}))
        return 1
    try:
        if args.replay is not None:
            root = seed(ledger, args.replay)
            print(json.dumps({"ok": root is not None, "root": root}))
            return 0 if root else 1
        result = {"errors": []}
        out = run_due(ledger, load_config(CONF_PATH), result,
                      time.monotonic() + 300, max_jobs=args.max_jobs,
                      cfg_path=CONF_PATH)
        out["errors"] = result["errors"]
        print(json.dumps(out, ensure_ascii=False))
        return 0 if not result["errors"] else 1
    finally:
        ledger.close()
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
