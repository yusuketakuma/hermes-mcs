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
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger import Ledger, LedgerReader
from mcs_requests import payload_hash
from mcs_util import NoRedirect, acquire_run_lock, load_config, \
    no_proxy_opener
import semantic_jev as jev

HOME = os.path.expanduser("~/.mcs")
DB = os.path.join(HOME, "data", "ledger.db")
CONF_PATH = os.path.join(HOME, "config.json")

SCHEMA_VERSION = "2026-09-20"
POLICY_VERSION = "2026-09-20"
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
                  KIND_USAGE)

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
LLM_OPENER = no_proxy_opener(NoRedirect)


# ---------- config ----------

def semantic_config(cfg: dict) -> tuple[dict, list]:
    """Validate config.json's "semantic" block. Unknown/malformed values
    fail CLOSED — mode falls back to off and each error is reported, so a
    typo can never widen the rollout stage (spec §22.1, AT-067)."""
    errors = []
    out = {"mode": "off", "model": jev.JEV_MODEL,
           "daily_request_budget": 0,
           "attempt_timeout_seconds": 20.0,
           "job_budget_seconds": 45.0,
           "max_attempts_per_try": 3,
           "max_questions_per_request": 12,
           "match_threshold": jev.MATCH_THRESHOLD,
           "nomatch_threshold": jev.NOMATCH_THRESHOLD,
           "delayed_notice_seconds": 900,
           "project_ids": None}
    block = cfg.get("semantic")
    if block is None:
        return out, errors
    if not isinstance(block, dict):
        return out, ["config: semantic_not_object"]
    mode = block.get("mode", "off")
    if mode not in MODES:
        errors.append("config: semantic_mode_invalid")
        mode = "off"
    out["mode"] = mode
    if "model" in block:
        if block["model"] != jev.JEV_MODEL:
            errors.append("config: semantic_model_invalid")
            out["mode"] = "off"
        out["model"] = jev.JEV_MODEL
    for key, lo, hi in (("daily_request_budget", 0, 100000),
                        ("max_questions_per_request", 1, 48),
                        ("max_attempts_per_try", 1, 5),
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
    return out, errors


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
        "message_id": row["message_id"],
        "parent_id": row["parent_id"],
        "revision": row["content_hash"] or "",
        "posted_at": row["posted_at"] or "",
        "occurred_at": None,      # event time is a fact-level field
        "sender": {"type": row["sender_type"] or "",
                   "profession": row["profession"] or ""},
        "body_original": row["body_text"] or "",
        "body_state": row["body_state"] or "unknown",
    }


def bundle_fingerprint(members: list, model: str = jev.JEV_MODEL) -> str:
    """Content+context revision fingerprint: any body edit, context
    change, model/registry/schema bump invalidates prior results
    (INV-15). Canonical JSON — never a lossy string concat."""
    return payload_hash({
        "members": sorted(({"m": m["message_id"], "r": m["revision"]}
                          for m in members), key=lambda x: x["m"]),
        "model": model,
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
      SELECT message_id,parent_id,sender_type,profession,posted_at,
             body_text,body_state,content_hash
      FROM messages WHERE project_id=? AND (message_id=? OR parent_id=?)
      ORDER BY posted_at_ts, message_id
    """, (project_id, root_id, root_id)).fetchall()
    root = [r for r in rows if r["message_id"] == root_id]
    if not root:
        return None
    members = [_member(r) for r in rows]
    for m in members:
        m["role"] = ("target" if target_ids
                     and m["message_id"] in target_ids else
                     "root" if m["parent_id"] is None else "context")
    quality = "full" if all(
        m["body_state"] in ("full", "deleted") for m in members) \
        else "partial"
    fp = bundle_fingerprint(members)
    return {"bundle_id": f"bundle_{project_id}_{root_id}_{fp[:12]}",
            "account_scope": "mcs",
            "project_id": project_id, "root_id": root_id,
            "members": members, "content_quality": quality,
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

def _current(ledger, kind: str, message_id: int, fp: str):
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
        if meta.get("fingerprint") == fp:
            try:
                content = json.loads(r["content"])
            except (json.JSONDecodeError, TypeError):
                return None
            return {"content": content, "meta": meta}
    return None


def jev_usage_today(ledger) -> int:
    """Durable daily Jev request count — shadow traffic spends real API
    budget too, so it is never unbounded (§13.5, AT-067). Counts the
    per-attempt semantic_usage rows written by run_due — one row per job
    attempt carrying that attempt's request DELTA, so every external
    call (primary, detail, claim-audit, loop-relation, failed) is
    counted exactly once."""
    day = time.time() - (time.time() % 86400)
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
    req = urllib.request.Request(
        LLM_ENDPOINT,
        data=json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1400, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with LLM_OPENER.open(req, timeout=timeout) as r:
            out = json.load(r)
    except (OSError, urllib.error.URLError, json.JSONDecodeError,
            http.client.HTTPException):
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

各候補のキー:
- "statement": 事実内容を15〜60字で（「〜の記載がある」等、報告としての表現）
- "kind": medication_event|symptom|explicit_request|pending_item|schedule|preference|observation|other
- "status": considered|planned|order_reported|execution_reported|cancelled|not_stated|conflicting
- "polarity": affirmed|negated|uncertain
- "time_text": 時間表現またはnull
- "quantity": 数量表現またはnull
- "evidence_quote": 根拠となる本文の完全一致引用（30字以内）

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
    return f"{y:04d}-{mo:02d}-{d:02d}"


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
                  deadline: float | None = None) -> tuple[list, bool, int]:
    """Fact candidates with verified evidence spans, extracted across
    the WHOLE body in bounded chunks — a long tail is never silently
    dropped (§12.3, AT-017). Returns (facts, complete, dropped):
    complete=False means the deadline hit mid-extraction OR a chunk's
    response was missing/unparseable — the caller must defer; the
    member is NOT 'processed' and no partial artifact is recorded, so a
    failed chunk can never silently narrow the extraction for this
    generation (§16.2 chunk ledger). dropped counts valid items lost to
    the fact cap so the artifact meta records the truncation instead of
    hiding it (§16.2). Unlocatable or ambiguous quotes stay
    'unverified' — never silently promoted (INV-07/08)."""
    body = member["body_original"]
    facts = []
    dropped = 0
    for ch in _chunks(body):
        if deadline is not None and time.monotonic() > deadline:
            return facts, False, dropped
        raw = _json_block(llm_fn(_FACT_PROMPT % ch) or "")
        items = raw.get("facts") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return facts, False, dropped
        for it in items:
            if not isinstance(it, dict):
                continue
            if len(facts) >= 40:
                dropped += 1
                continue
            stmt = it.get("statement")
            if not isinstance(stmt, str) or not stmt.strip():
                continue
            quote = it.get("evidence_quote")
            span = _locate_quote(body, quote) \
                if isinstance(quote, str) else None
            i = len(facts)
            ev_id = f"ev_{member['message_id']}_{i}"
            status = it.get("status")
            polarity = it.get("polarity")
            facts.append({
                "fact_id": f"fact_{member['message_id']}_{i}",
                "kind": it.get("kind") if it.get("kind") in FACT_KINDS
                        else "other",
                "statement": stmt.strip()[:200],
                "subject_ref": f"m{member['message_id']}",
                "drug_ref": None,
                "status": status if status in FACT_STATUSES
                          else "not_stated",
                "polarity": polarity if polarity in POLARITIES
                            else "uncertain",
                "occurred_at": _iso_date(it.get("time_text")),
                "time_text": it.get("time_text")
                             if isinstance(it.get("time_text"), str)
                             else None,
                "quantity": it.get("quantity")
                            if isinstance(it.get("quantity"), str)
                            else None,
                "evidence_refs": [ev_id] if span else [],
                "validation_status": "candidate" if span
                                     else "unverified",
                "_evidence": {"evidence_id": ev_id,
                              "message_id": member["message_id"],
                              "revision_id": member["revision"],
                              "start_codepoint": span[0],
                              "end_codepoint": span[1],
                              "quote": quote} if span else None,
            })
    return facts, True, dropped


_SUMMARY_PROMPT = """あなたは在宅医療チャット記録の要約器です。対象投稿と同一スレッド文脈、
検証済み事実候補から、全セクション共通のclaim構造を持つJSONのみ出力してください。

ルール:
- 各claimは {"section","text","claim_kind","fact_refs","status","polarity"}
- section: medication|status|pharmacy|followup|progress|flow|other
- claim_kind: reported_fact|inference|limitation
- fact_refs: 根拠となる候補の番号(0起き)の配列 — reported_factは必須、空は不可
- 対象時点の情報として書く（現在の診察結果と断定しない）
- 「対応不要」を既定にしない。未記載は limitations に書く
- 依頼の確認提案は原文の依頼と分け、claim_kind="inference" とする

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


# Safety ceiling for one local-LLM prompt. Above it the model's context
# window could silently drop input — an oversize target is flagged
# input_oversize -> NEEDS_REVIEW instead of being chopped (§12.3).
PROMPT_CHAR_LIMIT = 28000


def summarize(llm_fn, bundle: dict, target_id: int, facts: list,
              verdicts: dict, feedback: list | None = None) -> dict | None:
    """Local-LLM summary in the common Claim schema. Jev verdicts are
    context for the writer, never forced truth (§15.3). The FULL target
    body and thread context are passed untruncated; if the prompt would
    exceed PROMPT_CHAR_LIMIT no model call is made and the result is a
    stub flagged _input_oversize (audit forces NEEDS_REVIEW — never a
    silent partial PASS)."""
    target = next((m for m in bundle["members"]
                   if m["message_id"] == target_id), None)
    if target is None:
        return None
    ctx = "\n".join(m["body_original"] for m in bundle["members"]
                    if m["message_id"] != target_id
                    and m["body_original"])
    prompt = _SUMMARY_PROMPT % (target["body_original"],
                                ctx, _facts_brief(facts))
    if feedback:
        prompt += _REPAIR_SUFFIX % "\n".join(feedback[:10])
    if len(prompt) > PROMPT_CHAR_LIMIT:
        return {"summary_id": f"sum_{bundle['bundle_id']}",
                "input_bundle_id": bundle["bundle_id"],
                "schema_version": SCHEMA_VERSION,
                "target_message_id": target_id,
                "claims": [],
                "limitations": ["対象投稿または文脈が大きすぎるため"
                                "要約を生成できませんでした"],
                "audit_status": "pending", "_input_oversize": True}
    raw = _json_block(llm_fn(prompt) or "")
    if raw is None:
        return None
    claims_in = raw.get("claims")
    if not isinstance(claims_in, list):
        return None
    claims = []
    for i, c in enumerate(claims_in[:30]):
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
            "section": section, "text": text.strip()[:400],
            "claim_kind": kind, "fact_refs": refs,
            "evidence_refs": ev_refs,
            "status": c.get("status") if c.get("status")
                      in FACT_STATUSES else "not_stated",
            "polarity": c.get("polarity") if c.get("polarity")
                        in POLARITIES else "uncertain"})
    limitations = [str(x)[:200] for x in
                   (raw.get("limitations") or []) if isinstance(x, str)]
    # cap truncation is a finding, not a silent drop — the auditor and
    # the notice reader must see that claims were left out (§16.2)
    if len(claims_in) > 30:
        limitations = [f"claim数が上限を超えたため"
                       f"{len(claims_in) - 30}件省略"] + limitations
    return {"summary_id": f"sum_{bundle['bundle_id']}",
            "input_bundle_id": bundle["bundle_id"],
            "schema_version": SCHEMA_VERSION,
            "target_message_id": target_id,
            "claims": claims, "limitations": limitations[:10],
            "audit_status": "pending"}


# ---------- audit (spec §16) ----------

def audit_code(bundle: dict, facts: list, summary: dict) -> list:
    """Deterministic checks: reference integrity, span equality,
    structural enums, fact->claim coverage. Returns findings list —
    [] means the code side found no blocker (model check still runs)."""
    findings = []
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
                or src["body_original"][s:e] != ev["quote"]:
            findings.append({"code": "evidence_span_mismatch",
                             "fact": f["fact_id"]})
        # numbers/units in a fact must trace to ITS quote — appearing
        # somewhere else in the post is not tracing (AT-025/026)
        if f.get("quantity") and f["quantity"] not in ev["quote"]:
            findings.append({"code": "quantity_untraced",
                             "fact": f["fact_id"]})
    covered = set()
    for c in summary["claims"]:
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
                 deadline: float) -> tuple[list, bool]:
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
            "text does NOT support the claim. Judge target, value, "
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
            win = body[max(0, s - 250):min(len(body), e + 250)]
        else:
            win = ""
        ev_ctx[ev["evidence_id"]] = (quote, win)
    for c in claims:
        ctx = []
        for ev in c["evidence_refs"]:
            quote, win = ev_ctx.get(ev, ("", ""))
            ctx.append({"id": ev, "role": "evidence_quote",
                        "text": quote})
            if win and win != quote:
                ctx.append({"id": f"{ev}_ctx",
                            "role": "evidence_context", "text": win})
        state = {"target": {"id": c["claim_id"], "text": c["text"]},
                 "context": ctx}
        try:
            out = jev_client.evaluate(
                state, {c["claim_id"]: questions[c["claim_id"]]},
                deadline)
        except jev.JevError:
            return findings + [{"code": "support_unevaluated",
                                "claim": c["claim_id"]}], False
        ans = out["answers"][c["claim_id"]]
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
    if not evaluated:
        return "PENDING"
    blocking = [f for f in code_findings
                if f["code"] in ("evidence_missing",
                                 "evidence_revision_mismatch",
                                 "evidence_span_mismatch",
                                 "input_oversize")]
    if blocking:
        return "NEEDS_REVIEW"
    repairable = [f for f in code_findings + jev_findings
                  if f["code"] in ("fact_dropped", "claim_not_supported",
                                   "quantity_untraced",
                                   "claim_without_evidence")]
    if repairable and not repaired:
        return "REPAIR_REQUIRED"
    if repairable or [f for f in jev_findings
                      if f["code"] in ("claim_contradicts",
                                       "claim_ambiguous")]:
        return "NEEDS_REVIEW"
    return "PASS"


# ---------- open loop candidates (spec §17) ----------

def update_loops(ledger, project_id: int, bundle: dict,
                 facts_by_target: dict, jev_client, scfg: dict,
                 deadline: float) -> tuple[int, bool]:
    """Returns (candidates_created, complete). complete=False means the
    pair budget/deadline/a Jev error cut the relation pass short — the
    caller must reschedule so the remainder is carried forward (§17.2).
    Candidate creation is automatic; promotion to a formal request is
    NOT — that stays behind mcs_requests' human_confirmed contract
    (INV-11/19, AT-051)."""
    created = 0
    for target_id, facts in facts_by_target.items():
        member = next((m for m in bundle["members"]
                       if m["message_id"] == target_id), None)
        if member is None:
            continue
        for f in facts:
            if f["kind"] not in ("explicit_request", "pending_item",
                                 "schedule") or f["polarity"] == "negated":
                continue
            cand_fp = payload_hash({"p": project_id, "m": target_id,
                                    "s": f["statement"]})
            dup = False
            for r in ledger.db.execute(
                    "SELECT meta FROM artifacts WHERE kind=? "
                    "AND message_id=?", (KIND_LOOP, target_id)):
                try:
                    if json.loads(r["meta"] or "{}").get(
                            "candidate_fp") == cand_fp:
                        dup = True
                        break
                except (json.JSONDecodeError, TypeError):
                    continue
            if dup:
                continue
            ledger.artifact_add(
                KIND_LOOP,
                json.dumps({"loop_id": f"loop_{cand_fp[:12]}",
                            "project_id": project_id,
                            "kind": f["kind"],
                            "description": f["statement"],
                            "origin": {"message_id": target_id,
                                       "revision": member["revision"],
                                       "evidence_refs":
                                       f["evidence_refs"]},
                            "assignee_text": None,
                            "due_text": f.get("time_text"),
                            "state": "PROPOSED",
                            "history": [{
                                "state": "PROPOSED",
                                "at": int(time.time()),
                                "trigger_message_id": target_id}]},
                           ensure_ascii=False),
                project_id=project_id, message_id=target_id,
                model=jev.JEV_MODEL,
                meta={"fingerprint": bundle["source_fingerprint"],
                      "candidate_fp": cand_fp,
                      "registry": jev.REGISTRY_VERSION})
            created += 1
    if jev_client is None:
        return created, True
    # relate new arrivals to open candidates. EVERY open candidate is
    # eligible (no LIMIT — the spec forbids dropping the remainder,
    # §17.2); evaluated (candidate, target) pairs are deduped via their
    # recorded loop_event so re-runs only evaluate new pairs, and a
    # per-job pair budget carries the remainder to the next run of the
    # same job instead of truncating it.
    seen = set()
    for r in ledger.db.execute(
            "SELECT content FROM artifacts WHERE kind=? AND project_id=?",
            (KIND_LOOP_EVENT, project_id)):
        try:
            ev = json.loads(r["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(ev, dict):
            # loop_artifact_id identifies the candidate — two candidates
            # can share an origin message, so the bare origin id is not
            # a safe dedup key (legacy rows fall back to it)
            seen.add((ev.get("loop_artifact_id")
                      or ev.get("loop_origin_id"),
                      ev.get("trigger_message_id")))
    open_loops = ledger.db.execute("""
      SELECT artifact_id, message_id, content FROM artifacts
      WHERE kind=? AND project_id=? ORDER BY artifact_id DESC
    """, (KIND_LOOP, project_id)).fetchall()
    targets = [m for m in bundle["members"] if m["role"] == "target"]
    pairs = 0
    for row in open_loops:
        try:
            cand = json.loads(row["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cand, dict) or cand.get("state") not in \
                ("PROPOSED", "RESOLUTION_CANDIDATE"):
            continue
        for m in targets:
            if row["message_id"] == m["message_id"]:
                continue  # a candidate never relates to its own origin
            key = (row["artifact_id"], m["message_id"])
            if key in seen:
                continue
            if pairs >= 40 or time.monotonic() > deadline:
                return created, False   # remainder -> next run
            qid = f"rel_{row['message_id']}_{m['message_id']}"
            q = jev.choice_question(
                "state.context[0] is an open follow-up item recorded "
                "earlier. state.target is a newer message. Classify "
                "their relation — 'acknowledged'/'will confirm' is "
                "receipt, not resolution (AT-037/038).",
                jev.LOOP_RELATION_OPTIONS)
            try:
                out = jev_client.evaluate(
                    {"target": {"id": qid, "text": m["body_original"]},
                     "context": [{"id": "loop", "role": "open_item",
                                  "text": cand.get("description", "")}]},
                    {qid: q}, deadline)
            except jev.JevError:
                return created, False
            pairs += 1
            seen.add(key)
            ledger.artifact_add(
                KIND_LOOP_EVENT,
                json.dumps({"loop_origin_id": row["message_id"],
                            "loop_artifact_id": row["artifact_id"],
                            "trigger_message_id": m["message_id"],
                            "relation": out["answers"][qid]["choice"],
                            "confidence": out["answers"][qid]
                            .get("confidence"),
                            "candidate_state": cand.get("state")},
                           ensure_ascii=False),
                project_id=project_id, message_id=m["message_id"],
                model=jev.JEV_MODEL,
                meta={"fingerprint": bundle["source_fingerprint"],
                      "registry": jev.REGISTRY_VERSION,
                      "jev_requests": 1})
    return created, True


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
    pat = ledger.db.execute(
        "SELECT patient_name,url FROM patients WHERE project_id=?",
        (project_id,)).fetchone()
    name = (pat["patient_name"] if pat else None) or str(project_id)
    url = (pat["url"] if pat else None) or ""
    ids = [t for t in (targets or [root_id]) if type(t) is int] \
        or [root_id]
    marks = ",".join("?" * len(ids))
    members = ledger.db.execute(
        f"SELECT COUNT(*) c, MAX(posted_at) latest FROM messages "
        f"WHERE project_id=? AND message_id IN ({marks})",
        (project_id, *ids)).fetchone()
    latest = (members["latest"] or "")[:16].replace("T", " ") \
        .replace("-", "/")
    lines = [f"【{name}】",
             f"対象新着：{members['c']}投稿"
             f"｜対象投稿の最終時刻：{latest} JST",
             f"取得：{'完全' if quality == 'full' else '一部未取得'}",
             f"要約：{'自動検査完了' if audit_status == 'PASS' else '要確認'}"]
    if focus_mid is not None and len(ids) > 1:
        trow = ledger.db.execute(
            "SELECT posted_at FROM messages WHERE project_id=? "
            "AND message_id=?", (project_id, focus_mid)).fetchone()
        stamp = ((trow["posted_at"] or "")[:16].replace("T", " ")
                 .replace("-", "/")) if trow else ""
        lines.append(f"要約対象：{stamp} の投稿" if stamp
                     else f"要約対象：投稿#{focus_mid}")
    by_sec: dict[str, list] = {}
    for c in summary.get("claims", []):
        by_sec.setdefault(c["section"], []).append(c)
    if by_sec:
        lines.append("\n■ 今回の重要情報")
        for c in by_sec.get("medication", []) + by_sec.get("status", []):
            lines.append(f"・{c['text']}")
    for sec in ("pharmacy", "followup", "progress", "other"):
        if by_sec.get(sec):
            lines.append(f"\n■ {_SECTION_LABEL[sec]}")
            for c in by_sec[sec]:
                prefix = "【提案】" if c["claim_kind"] == "inference" else ""
                lines.append(f"・{prefix}{c['text']}")
    lims = summary.get("limitations") or []
    if lims:
        lines.append("\n■ 原文・制約")
        lines.extend(f"・{x}" for x in lims[:5])
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
    for r in ledger.db.execute(
            "SELECT event_id,payload FROM notify_outbox "
            "WHERE kind='new_messages' AND project_id=? "
            "AND state != 'suppressed' "
            "ORDER BY event_id DESC", (project_id,)):
        try:
            ids = json.loads(r["payload"]).get("message_ids") or []
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(ids, list) and want.intersection(ids):
            return r["event_id"]
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
        "SELECT event_id,project_id,payload FROM notify_outbox "
        "WHERE kind='new_messages' AND state IN ('pending','failed') "
        "AND created_at<?", (cutoff,))
    for ev in events:
        try:
            ids = json.loads(ev["payload"]).get("message_ids") or []
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(ids, list):
            continue
        ids = ids[:500]      # a malformed fat payload must not wedge the scan
        pid = ev["project_id"]
        if scfg["project_ids"] is not None and pid not in \
                scfg["project_ids"]:
            continue
        marks = ",".join("?" * len(ids)) or "NULL"
        roots = {r["r"] for r in ledger.db.execute(
            f"SELECT COALESCE(parent_id,message_id) r FROM messages "
            f"WHERE message_id IN ({marks})", ids)}
        for root in roots:
            bundle = thread_bundle(ledger, pid, root)
            if bundle is None:
                continue
            fp = bundle["source_fingerprint"]
            passed = False
            for a in ledger.db.execute(
                    "SELECT meta FROM artifacts WHERE kind=? AND "
                    "project_id=?", (KIND_AUDIT, pid)):
                try:
                    m = json.loads(a["meta"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                if m.get("fingerprint") == fp \
                        and m.get("audit_status") == "PASS":
                    passed = True
                    break
            if passed:
                continue
            dkey = payload_hash({"kind": "semantic_notice", "root": root,
                                 "fp": fp, "degraded": 1})
            if _outbox_has_delivery(ledger, dkey):
                continue
            ledger.outbox_add("semantic_notice", pid, {
                "delivery_key": dkey, "root_id": root,
                "degraded": True, "src_event_id": ev["event_id"],
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


def _plan_exists(ledger, message_id: int, fp: str,
                 status: str) -> bool:
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
                and m.get("audit_status") == status:
            return True
    return False


def _write_result(ledger, pid: int, mid: int, r: dict, fp: str,
                  members: dict, final_status: str) -> None:
    """Summary + audit artifact pair for one evaluated target — the
    durable record of this generation's outcome. The repair_count meta
    is the INV-10 budget: it survives restarts AND mid-job deferrals
    because it lives on this artifact, not in process memory."""
    ledger.artifact_add_tx(
        KIND_SUMMARY,
        json.dumps({k: v for k, v in r["summary"].items()
                    if not k.startswith("_")}, ensure_ascii=False),
        project_id=pid, message_id=mid, model=LLM_MODEL,
        meta={"fingerprint": fp, "schema": SCHEMA_VERSION,
              "audit_status": final_status,
              "stale": final_status == "STALE",
              "target_revision": members[mid]["revision"]})
    ledger.artifact_add_tx(
        KIND_AUDIT,
        json.dumps({"status": final_status,
                    "findings": r["findings"],
                    "target_message_id": mid}, ensure_ascii=False),
        project_id=pid, message_id=mid, model=jev.JEV_MODEL,
        meta={"fingerprint": fp, "schema": SCHEMA_VERSION,
              "audit_status": final_status,
              "repair_count": 1 if r["repaired"] else 0,
              "jev_requests": r.get("jev_requests", 0)})


def _process_job(ledger, scfg, job, jev_client, llm_fn, deadline) -> str:
    """One semantic job -> durable artifacts + job state transition.
    Returns 'done'|'deferred'|'retry'|'failed'."""
    pid, root = job["project_id"], job["message_id"]
    pl = {}
    try:
        pl = json.loads(job["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    raw_targets = pl.get("targets")
    targets = [t for t in raw_targets if type(t) is int] \
        if isinstance(raw_targets, list) else []
    bundle = thread_bundle(ledger, pid, root, targets or None)
    if bundle is None:
        # source vanished — nothing to preserve for it; mark done so
        # the row does not re-run as a no-op on every drain
        ledger.job_done(job["job_id"])
        return "done"
    fp = bundle["source_fingerprint"]
    # provenance on the recorded bundle (spec §12.1): which stored
    # origin event / capture path this evaluation descends from
    origin = pl.get("origin") if isinstance(pl.get("origin"), dict) \
        else {}
    bundle["origin_event_id"] = origin.get("event_id")
    bundle["capture_origin"] = origin.get("source")
    if _current(ledger, KIND_BUNDLE, root, fp) is None:
        ledger.artifact_add(
            KIND_BUNDLE, json.dumps(bundle, ensure_ascii=False),
            project_id=pid, message_id=root, model=jev.JEV_MODEL,
            meta={"fingerprint": fp, "schema": SCHEMA_VERSION})
    all_targets = [t for t in (targets or [root])
                   if any(m["message_id"] == t
                          for m in bundle["members"])]
    members = {m["message_id"]: m for m in bundle["members"]}
    facts_by_target: dict[int, list] = {}
    verdicts: dict[int, dict] = {}
    incomplete = False
    hard_fail = False   # non-retryable Jev error — bound the retries
    for mid in all_targets:
        member = members[mid]
        # restart-safe: a complete assessment for THIS fingerprint is
        # reused; retry_wait/error/pending ones are re-attempted
        prev = _current(ledger, KIND_ASSESS, mid, fp)
        if prev and prev["meta"].get("technical_status") == "complete":
            verdicts[mid] = prev["content"].get("verdicts", {})
        else:
            if time.monotonic() > deadline - 5:
                incomplete = True
                break
            state = jev_state(bundle, mid)
            meta_base = {"fingerprint": fp, "model": jev.JEV_MODEL,
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
                    if not e.retryable:
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
            # conditional drill-down: only when the medication-change
            # proposition did not return NO_MATCH — the detail questions
            # are medication-specific, so unrelated hits never spend the
            # extra calls (§14.3)
            detail = {}
            if verdicts[mid].get("P01", {}).get("verdict") \
                    != "NO_MATCH":
                for dim, (instr, options) in \
                        jev.MED_DETAIL_QUESTIONS.items():
                    if time.monotonic() > deadline - 5:
                        break
                    try:
                        d_out = jev_client.evaluate(
                            state,
                            {dim: jev.choice_question(instr, options)},
                            deadline)
                        detail[dim] = d_out["answers"][dim]["choice"]
                    except jev.JevError:
                        detail[dim] = None
            meta_base["jev_requests"] = (
                jev_client.requests_made - req0) if jev_client else 0
            ledger.artifact_add(
                KIND_ASSESS,
                json.dumps({"target_message_id": mid,
                            "verdicts": verdicts[mid],
                            "detail": detail}, ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={**meta_base, "technical_status": "complete"})
        # facts: reuse a stored set for this fingerprint, else extract
        prev_f = _current(ledger, KIND_FACTS, mid, fp)
        if prev_f is not None:
            facts = prev_f["content"].get("facts", [])
        else:
            if time.monotonic() > deadline - 5:
                incomplete = True
                break
            facts, f_complete, f_dropped = extract_facts(
                llm_fn, member, deadline - 5)
            if not f_complete:
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
                meta={"fingerprint": fp, "schema": SCHEMA_VERSION,
                      "chunks_total": len(_chunks(
                          member["body_original"])),
                      "dropped_by_cap": f_dropped})
        facts_by_target[mid] = facts
    if incomplete:
        if hard_fail:
            # deterministic failure (e.g. protocol_error, oversized
            # payload) — deferring forever would burn a Jev call every
            # tick on an input that can never pass; bounded retry ends
            # 'failed' where status_report can surface it
            ledger.job_retry(job["job_id"], retry_in=300,
                             max_attempts=6)
            return "retry"
        return "deferred"

    # summary + audit. A TERMINAL audit (PASS/NEEDS_REVIEW) for THIS
    # fingerprint is reused — its notification intent was already
    # committed atomically below. A PENDING one is NOT final: the
    # stored summary is re-audited so a mid-audit outage can never
    # wedge the thread on a stale PENDING marker (AT-058).
    results = {}
    for mid, facts in facts_by_target.items():
        existing = _current(ledger, KIND_SUMMARY, mid, fp)
        prev_audit = _current(ledger, KIND_AUDIT, mid, fp)
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
        if existing is not None:
            summary = existing["content"]
        else:
            summary = summarize(llm_fn, bundle, mid, facts,
                                verdicts.get(mid, {}))
        if summary is None:
            # same dedup as the assess wait-state above: a persistent
            # local-LLM outage defers without stacking identical
            # PENDING rows on every drain
            prev_a = _current(ledger, KIND_AUDIT, mid, fp)
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
                    meta={"fingerprint": fp, "schema": SCHEMA_VERSION,
                          "technical_status": "pending"})
            incomplete = True
            continue
        # at most one repair per input generation — the count survives
        # restarts because it lives on the prior audit artifact (INV-10)
        repaired = bool(prev_audit
                        and prev_audit["meta"].get("repair_count", 0) >= 1)
        summary["_facts"] = facts   # evidence context for the Jev audit
        req0 = jev_client.requests_made if jev_client else 0
        findings = []
        status = "PENDING"
        for _attempt in range(2):      # initial + at most one repair
            code_f = audit_code(bundle, facts, summary)
            jev_f, evaluated = audit_claims(jev_client, bundle,
                                            summary, deadline)
            findings = code_f + jev_f
            status = audit_status_for(code_f, jev_f, evaluated,
                                      repaired)
            if status == "REPAIR_REQUIRED" and not repaired:
                summary2 = summarize(
                    llm_fn, bundle, mid, facts, verdicts.get(mid, {}),
                    feedback=[f.get("code", "") + " " +
                              f.get("statement", f.get("claim", ""))
                              for f in code_f + jev_f])
                repaired = True
                if summary2 is not None:
                    summary = summary2
                    summary["_facts"] = facts
                    continue
            break
        summary["audit_status"] = status
        results[mid] = {"summary": summary, "status": status,
                        "findings": findings, "repaired": repaired,
                        "fresh": True,
                        "jev_requests": (jev_client.requests_made - req0)
                        if jev_client else 0}
    if incomplete:
        if results:
            # commit each completed target's outcome NOW — dropping it
            # would re-spend Jev calls on an identical input next run
            # AND silently reset the one-shot repair budget, whose
            # counter lives on these artifacts (INV-10, §18.2)
            with ledger.db:
                for mid, r in results.items():
                    if r["fresh"]:
                        _write_result(ledger, pid, mid, r, fp, members,
                                      r["status"])
        if hard_fail:
            ledger.job_retry(job["job_id"], retry_in=300,
                             max_attempts=6)
            return "retry"
        return "deferred"

    # generation guard: the bundle must still be current at commit, and
    # this job row must still be the live pending one — an older worker
    # result must never overwrite a newer generation (AT-035/057)
    fresh = thread_bundle(ledger, pid, root, targets or None)
    live = ledger.job_pending(JOB_KIND, pid, root)
    stale = (fresh is None
             or fresh["source_fingerprint"] != fp
             or live is None or live["job_id"] != job["job_id"])
    pending = not stale and any(r["status"] == "PENDING"
                                for r in results.values())
    loops_done = True
    if not stale:
        # loop candidates/relation events for the LIVE generation —
        # kept OUT of the commit tx because matching makes Jev calls
        # (no network inside a DB transaction, §6.2); replay-safe via
        # candidate_fp and pair dedup.
        _, loops_done = update_loops(ledger, pid, bundle,
                                     facts_by_target, jev_client,
                                     scfg, deadline)
    # notification eligibility is derived from the stored origin event,
    # not from this job's seed provenance (INV-20, §20.3) — evaluated
    # per target below: a covering new_messages intent must exist for
    # the message a notice reports on.
    with ledger.db:
        for mid, r in results.items():
            final_status = "STALE" if stale else r["status"]
            if r["fresh"]:
                _write_result(ledger, pid, mid, r, fp, members,
                              final_status)
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
            if scfg["mode"] == "enforce" and final_status == "PASS":
                # per-target delivery key — one generation emits one
                # notice PER audited target; a shared (root, fp) key
                # would dedup every later target's claims out of the
                # notice while the header still claims them
                dkey = payload_hash({"kind": "semantic_notice",
                                     "root": root, "fp": fp,
                                     "mid": mid})
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
                        "src_event_id": src_mid, "text": text,
                        "fingerprint": fp,
                        "policy_version": POLICY_VERSION})
                    enqueued = True
            if not _plan_exists(ledger, mid, fp, final_status):
                plan_meta = {"fingerprint": fp,
                             "audit_status": final_status,
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
            ledger.db.execute(
                "UPDATE fetch_jobs SET state='done',updated_at=? "
                "WHERE job_id=?", (time.time(), job["job_id"]))
    if stale:
        ledger.job_defer(job["job_id"], 0)   # re-run against new input
        return "deferred"
    if pending:
        # evaluation incomplete (e.g. Jev outage mid-audit) — the job
        # must not sit 'done' on an unfinished audit; consume a bounded
        # retry attempt so a persistent outage eventually fails rather
        # than busy-loops (AT-058)
        ledger.job_retry(job["job_id"], retry_in=300, max_attempts=6)
        return "retry"
    if not loops_done:
        # relation pass truncated by the pair budget/deadline — the
        # results committed above stand; the job re-runs to evaluate
        # the remaining (candidate, target) pairs (§17.2 持ち越し)
        ledger.job_defer(job["job_id"], 0)
        return "deferred"
    return "done"


def run_due(ledger, cfg: dict, result: dict, deadline: float,
            jev_client=None, llm_fn=None, max_jobs: int = 4,
            cfg_path: str | None = None) -> dict:
    """Drain due semantic jobs inside the tick's remaining budget.
    OFF returns immediately — no job creation, no external calls, no
    auto-drain (AT-053). The caller's shared run lock is held by
    run_check; a standalone CLI takes acquire_run_lock itself.
    cfg_path, when given, re-validates config between jobs so an OFF
    flip mid-run takes effect at the next job boundary (AT-060)."""
    scfg, errors = semantic_config(cfg)
    for e in errors:
        if e not in result["errors"]:
            result["errors"].append(e)
    out = {"mode": scfg["mode"], "done": 0, "deferred": 0,
           "failed": 0, "left": None, "budget_exhausted": False}
    if scfg["mode"] == "off":
        return out
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
      ORDER BY CASE WHEN payload LIKE '%"eligible": true%'
                    THEN 0 ELSE 1 END, job_id
      LIMIT ?
    """, (time.time(), JOB_KIND, max_jobs)).fetchall()
    for job in due:
        if cfg_path is not None:
            scfg, errs = semantic_config(load_config(cfg_path))
            for e in errs:
                if e not in result["errors"]:
                    result["errors"].append(e)
            if scfg["mode"] == "off":
                ledger.job_defer(job["job_id"], 300)
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
            ledger.job_defer(job["job_id"], 300)
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
        req0 = jev_client.requests_made if jev_client is not None else 0
        try:
            status = _process_job(ledger, scfg, job, jev_client,
                                  llm_fn, deadline)
        except Exception as e:
            ledger.job_retry(job["job_id"], retry_in=300,
                             max_attempts=6)
            result["errors"].append(
                f"semantic {job['message_id']}: {type(e).__name__}")
            out["failed"] += 1
            status = None
        # durable per-attempt request delta — the daily budget ledger
        # (jev_usage_today) is exact and covers claim-audit and loop
        # calls, not just the primary assessment
        used = (jev_client.requests_made - req0) \
            if jev_client is not None else 0
        if used:
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
            ledger.job_defer(job["job_id"], 60)
        elif status == "retry":
            out["deferred"] += 1   # job_retry already rescheduled it
        else:
            out["failed"] += 1
    if scfg["mode"] == "enforce":
        try:
            out["degraded_notices"] = _emit_degraded(ledger, scfg)
        except Exception:
            out["degraded_notices"] = 0
            result["errors"].append("semantic: degraded_scan_failed")
    out["left"] = len(ledger.job_due(limit=50, kind=JOB_KIND))
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
    if row is None:
        return None
    ledger.semantic_seed(row["project_id"], [message_id],
                         {"origin": origin})
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
    return {"semantic_jobs": jobs, "audit_statuses": audits,
            "loop_candidates": loops,
            "jev_requests_today": jev_usage_today(ledger)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--status", action="store_true",
                    help="read-only status (LedgerReader, no lock)")
    ap.add_argument("--drain", action="store_true",
                    help="process due semantic jobs now (writer lock)")
    ap.add_argument("--replay", type=int, metavar="MESSAGE_ID",
                    help="seed one finite evaluation job (writer lock)")
    ap.add_argument("--max-jobs", type=int, default=4)
    args = ap.parse_args()
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
        if args.replay:
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
