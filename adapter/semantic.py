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
SEMANTIC_KINDS = (KIND_BUNDLE, KIND_ASSESS, KIND_FACTS, KIND_SUMMARY,
                  KIND_AUDIT, KIND_LOOP, KIND_LOOP_EVENT, KIND_PLAN)

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
    budget too, so it is never unbounded (§13.5, AT-067)."""
    day = time.time() - (time.time() % 86400)
    total = 0
    for r in ledger.db.execute(
            "SELECT meta FROM artifacts WHERE kind=? AND created_at>=?",
            (KIND_ASSESS, day)):
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
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
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


def _locate_quote(body: str, quote: str) -> tuple[int, int] | None:
    """Find quote's UNIQUE codepoint span in body. Ambiguous or absent
    quotes get no span — never a guessed one (INV-07, AT-029)."""
    if not body or not quote:
        return None
    first = body.find(quote)
    if first < 0 or body.find(quote, first + 1) >= 0:
        return None
    return (first, first + len(quote))


def extract_facts(llm_fn, member: dict) -> list:
    """Fact candidates with verified evidence spans. Unlocatable or
    ambiguous quotes stay 'unverified' — never silently promoted
    (INV-07/08)."""
    body = member["body_original"]
    raw = _json_block(llm_fn(_FACT_PROMPT % body[:4000]) or "")
    items = raw.get("facts") if raw else None
    if not isinstance(items, list):
        return []
    facts = []
    for i, it in enumerate(items[:20]):
        if not isinstance(it, dict):
            continue
        stmt = it.get("statement")
        if not isinstance(stmt, str) or not stmt.strip():
            continue
        quote = it.get("evidence_quote")
        span = _locate_quote(body, quote) \
            if isinstance(quote, str) else None
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
            "status": status if status in FACT_STATUSES else "not_stated",
            "polarity": polarity if polarity in POLARITIES
                        else "uncertain",
            "occurred_at": None,
            "time_text": it.get("time_text")
                         if isinstance(it.get("time_text"), str) else None,
            "quantity": it.get("quantity")
                        if isinstance(it.get("quantity"), str) else None,
            "evidence_refs": [ev_id] if span else [],
            "validation_status": "candidate" if span else "unverified",
            "_evidence": {"evidence_id": ev_id,
                          "message_id": member["message_id"],
                          "revision_id": member["revision"],
                          "start_codepoint": span[0] if span else None,
                          "end_codepoint": span[1] if span else None,
                          "quote": quote} if span else None,
        })
    return facts


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


def summarize(llm_fn, bundle: dict, target_id: int, facts: list,
              verdicts: dict, feedback: list | None = None) -> dict | None:
    """Local-LLM summary in the common Claim schema. Jev verdicts are
    context for the writer, never forced truth (§15.3)."""
    target = next((m for m in bundle["members"]
                   if m["message_id"] == target_id), None)
    if target is None:
        return None
    ctx = "\n".join(m["body_original"] for m in bundle["members"]
                    if m["message_id"] != target_id
                    and m["body_original"])[:2000]
    prompt = _SUMMARY_PROMPT % (target["body_original"][:4000],
                                ctx, _facts_brief(facts))
    if feedback:
        prompt += _REPAIR_SUFFIX % "\n".join(feedback[:10])
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
        # numbers/units in a fact must trace to its quote, not merely
        # appear somewhere in the post (AT-025/026)
        if ev and f.get("quantity") and \
                f["quantity"] not in ev["quote"] \
                and f["quantity"] not in src["body_original"]:
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
            "provided source spans in state.context? Judge target, "
            "value, polarity and tense together — a same-topic claim "
            "with different drug/dose does not match (AT-024/025).",
            jev.CLAIM_SUPPORT_OPTIONS)
    ev_quotes = {}
    for f in summary.get("_facts", []):
        if f.get("_evidence"):
            ev_quotes[f["_evidence"]["evidence_id"]] = \
                f["_evidence"]["quote"]
    for c in claims:
        state = {"target": {"id": c["claim_id"], "text": c["text"]},
                 "context": [{"id": ev, "role": "evidence",
                              "text": ev_quotes.get(ev, "")}
                             for ev in c["evidence_refs"]]}
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
                                 "evidence_span_mismatch")]
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
                 deadline: float) -> int:
    """Candidate creation is automatic; promotion to a formal request is
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
                            "history": []}, ensure_ascii=False),
                project_id=project_id, message_id=target_id,
                model=jev.JEV_MODEL,
                meta={"fingerprint": bundle["source_fingerprint"],
                      "candidate_fp": cand_fp,
                      "registry": jev.REGISTRY_VERSION})
            created += 1
    if jev_client is None:
        return created
    # relate new arrivals to open candidates — bounded, one choice call
    # per (candidate, target) pair, rest deferred to the next job
    open_loops = ledger.db.execute("""
      SELECT message_id, content FROM artifacts
      WHERE kind=? AND project_id=? ORDER BY artifact_id DESC LIMIT 10
    """, (KIND_LOOP, project_id)).fetchall()
    targets = [m for m in bundle["members"] if m["role"] == "target"]
    for row in open_loops:
        try:
            cand = json.loads(row["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if cand.get("state") not in ("PROPOSED", "RESOLUTION_CANDIDATE"):
            continue
        for m in targets:
            if m["message_id"] == row["message_id"]:
                continue  # a candidate never relates to its own origin
            if time.monotonic() > deadline:
                return created
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
                return created
            rel = out["answers"][qid]["choice"]
            if rel in ("unrelated",):
                continue
            ledger.artifact_add(
                KIND_LOOP_EVENT,
                json.dumps({"loop_origin_id": row["message_id"],
                            "trigger_message_id": m["message_id"],
                            "relation": rel,
                            "confidence": out["answers"][qid]
                            .get("confidence"),
                            "candidate_state": cand.get("state")},
                           ensure_ascii=False),
                project_id=project_id, message_id=m["message_id"],
                model=jev.JEV_MODEL,
                meta={"fingerprint": bundle["source_fingerprint"],
                      "registry": jev.REGISTRY_VERSION})
    return created


# ---------- notification render (spec §20, §19.3) ----------

_SECTION_LABEL = {"medication": "薬剤・処方に関する情報",
                  "status": "現在の状況（対象投稿時点）",
                  "pharmacy": "薬局への影響", "followup": "未決事項／フォローアップ",
                  "progress": "前回からの進展", "flow": "投稿の流れ",
                  "other": "その他"}


def render_notice(ledger, project_id: int, root_id: int,
                  summary: dict, audit_status: str) -> str:
    """The §20.1 block. Patient label + coverage/audit status lines are
    code-generated; claim text comes from the audited summary. The MCS
    link is the stored patient URL — never a guessed permalink."""
    pat = ledger.db.execute(
        "SELECT patient_name,url FROM patients WHERE project_id=?",
        (project_id,)).fetchone()
    name = (pat["patient_name"] if pat else None) or str(project_id)
    url = (pat["url"] if pat else None) or ""
    members = ledger.db.execute("""
      SELECT COUNT(*) c, MAX(posted_at) latest FROM messages
      WHERE project_id=? AND (message_id=? OR parent_id=?)
    """, (project_id, root_id, root_id)).fetchone()
    lines = [f"【{name}】",
             f"対象新着：{members['c']}投稿"
             f"｜対象投稿の最終時刻：{(members['latest'] or '')[:16]}",
             f"要約：{'自動検査完了' if audit_status == 'PASS' else '要確認'}"]
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


def _emit_degraded(ledger, scfg: dict) -> int:
    """Enforce-only fallback: a new-messages intent whose thread still
    lacks a PASS-audited summary past delayed_notice_seconds gets ONE
    code-generated degraded notice through the existing outbox — no
    clinical claims, original-check instruction only (spec §19.3).
    Each (root, fingerprint) pair dedupes via delivery_key."""
    cutoff = time.time() - scfg["delayed_notice_seconds"]
    sent = 0
    events = ledger.db.execute(
        "SELECT project_id,payload FROM notify_outbox "
        "WHERE kind='new_messages' AND created_at<?", (cutoff,))
    for ev in events:
        try:
            ids = json.loads(ev["payload"]).get("message_ids") or []
        except (json.JSONDecodeError, TypeError):
            continue
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
                "degraded": True,
                "text": render_degraded(ledger, pid),
                "policy_version": POLICY_VERSION})
            sent += 1
    return sent


# ---------- drain ----------

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
        return "done"   # source vanished — nothing to preserve for it
    fp = bundle["source_fingerprint"]
    ledger.artifact_add(
        KIND_BUNDLE, json.dumps(bundle, ensure_ascii=False),
        project_id=pid, message_id=root, model=jev.JEV_MODEL,
        meta={"fingerprint": fp, "schema": SCHEMA_VERSION})
    all_targets = [t for t in (targets or [root])
                   if any(m["message_id"] == t
                          for m in bundle["members"])]
    facts_by_target: dict[int, list] = {}
    verdicts: dict[int, dict] = {}
    incomplete = False
    for mid in all_targets:
        member = next(m for m in bundle["members"]
                      if m["message_id"] == mid)
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
            if jev_client is not None and state is not None:
                qs = {k: jev.noul_question(p["instructions"], p["true"],
                                           p["false"])
                      for k, p in jev.PROPOSITIONS.items()}
                try:
                    out = jev_client.evaluate(state, qs, deadline)
                    answers = out["answers"]
                except jev.JevError as e:
                    meta_base["technical_status"] = (
                        "retry_wait" if e.retryable else "error")
                    meta_base["error_kind"] = e.kind
            if answers is None:
                meta_base.setdefault("technical_status", "pending")
                meta_base.setdefault("error_kind", "jev_unavailable")
                ledger.artifact_add(
                    KIND_ASSESS, json.dumps({"target_message_id": mid,
                                             "verdicts": {}},
                                            ensure_ascii=False),
                    project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                    meta={**meta_base,
                          "jev_requests": jev_client.requests_made
                          if jev_client else 0})
                incomplete = True
                continue
            verdicts[mid] = {k: {"noul": a["noul"],
                                 "verdict": jev.verdict_for(
                                     a["noul"], scfg["match_threshold"],
                                     scfg["nomatch_threshold"])}
                             for k, a in answers.items()}
            # conditional drill-down: only where the primary pass did
            # not return NO_MATCH — source text is never dropped
            detail = {}
            if any(v["verdict"] != "NO_MATCH"
                   for v in verdicts[mid].values()):
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
            ledger.artifact_add(
                KIND_ASSESS,
                json.dumps({"target_message_id": mid,
                            "verdicts": verdicts[mid],
                            "detail": detail}, ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={**meta_base, "technical_status": "complete",
                      "jev_requests": jev_client.requests_made})
        # facts: reuse a stored set for this fingerprint, else extract
        prev_f = _current(ledger, KIND_FACTS, mid, fp)
        if prev_f is not None:
            facts = prev_f["content"].get("facts", [])
        else:
            if time.monotonic() > deadline - 5:
                incomplete = True
                break
            facts = extract_facts(llm_fn, member)
            ledger.artifact_add(
                KIND_FACTS,
                json.dumps({"facts": facts,
                            "evidence": {f["_evidence"]["evidence_id"]:
                                         f["_evidence"] for f in facts
                                         if f.get("_evidence")}},
                           ensure_ascii=False),
                project_id=pid, message_id=mid, model=LLM_MODEL,
                meta={"fingerprint": fp, "schema": SCHEMA_VERSION})
        facts_by_target[mid] = facts
    if incomplete:
        return "deferred"

    # summary + audit for targets lacking a current audited summary —
    # a prior crash after assess/facts resumes here, not from scratch
    audit_results = {}
    for mid, facts in facts_by_target.items():
        existing = _current(ledger, KIND_SUMMARY, mid, fp)
        prev_audit = _current(ledger, KIND_AUDIT, mid, fp)
        if existing is not None and prev_audit is not None:
            continue
        if time.monotonic() > deadline - 5:
            incomplete = True
            break
        summary = summarize(llm_fn, bundle, mid, facts,
                            verdicts.get(mid, {}))
        if summary is None:
            ledger.artifact_add(
                KIND_AUDIT,
                json.dumps({"status": "PENDING",
                            "findings": [{"code": "summary_unavailable"}],
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
        audit_results[mid] = (summary, status, findings, repaired)
    if incomplete:
        return "deferred"

    # generation guard: the bundle must still be current at commit, and
    # this job row must still be the live pending one — an older worker
    # result must never overwrite a newer generation (AT-035/057)
    fresh = thread_bundle(ledger, pid, root, targets or None)
    live = ledger.job_pending(JOB_KIND, pid, root)
    stale = (fresh is None
             or fresh["source_fingerprint"] != fp
             or live is None or live["job_id"] != job["job_id"])
    with ledger.db:
        for mid, (summary, status, findings, repaired) \
                in audit_results.items():
            final_status = "STALE" if stale else status
            ledger.artifact_add_tx(
                KIND_SUMMARY,
                json.dumps({k: v for k, v in summary.items()
                            if k != "_facts"}, ensure_ascii=False),
                project_id=pid, message_id=mid, model=LLM_MODEL,
                meta={"fingerprint": fp, "schema": SCHEMA_VERSION,
                      "audit_status": final_status, "stale": stale})
            ledger.artifact_add_tx(
                KIND_AUDIT,
                json.dumps({"status": final_status,
                            "findings": findings,
                            "target_message_id": mid},
                           ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={"fingerprint": fp, "schema": SCHEMA_VERSION,
                      "audit_status": final_status,
                      "repair_count": 1 if repaired else 0})
    if stale:
        ledger.job_defer(job["job_id"], 0)   # re-run against new input
        return "deferred"

    update_loops(ledger, pid, bundle, facts_by_target,
                 jev_client, scfg, deadline)

    # notification path — shadow records the plan only; enforce may queue
    # a real outbox intent through the existing sender (INV-14/16)
    for mid, (summary, status, _f, _r) in audit_results.items():
        text = render_notice(ledger, pid, root, summary, status)
        plan_meta = {"fingerprint": fp, "audit_status": status,
                     "mode": scfg["mode"], "origin": pl.get("origin")}
        if scfg["mode"] == "enforce" and status == "PASS":
            dkey = payload_hash({"kind": "semantic_notice",
                                 "root": root, "fp": fp})
            if not _outbox_has_delivery(ledger, dkey):
                ledger.outbox_add("semantic_notice", pid, {
                    "delivery_key": dkey, "root_id": root,
                    "target_message_id": mid, "text": text,
                    "policy_version": POLICY_VERSION})
                plan_meta["enqueued"] = True
        ledger.artifact_add(KIND_PLAN, json.dumps(
            {"root_id": root, "target_message_id": mid,
             "text": text}, ensure_ascii=False),
            project_id=pid, message_id=mid,
            meta=plan_meta)
    ledger.job_done(job["job_id"])
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
           "failed": 0, "left": None}
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
    due = ledger.job_due(limit=max_jobs, kind=JOB_KIND)
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
            continue
        if jev_client is not None and \
                jev_usage_today(ledger) >= scfg["daily_request_budget"]:
            result["errors"].append("semantic: daily_budget_exhausted")
            break
        try:
            status = _process_job(ledger, scfg, job, jev_client,
                                  llm_fn, deadline)
        except Exception as e:
            ledger.job_retry(job["job_id"], retry_in=300,
                             max_attempts=6)
            result["errors"].append(
                f"semantic {job['message_id']}: {type(e).__name__}")
            out["failed"] += 1
            continue
        if status == "done":
            out["done"] += 1
        elif status == "deferred":
            out["deferred"] += 1
            ledger.job_defer(job["job_id"], 60)
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
