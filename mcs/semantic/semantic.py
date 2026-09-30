#!/usr/bin/env python3
"""MCS semantic layer — Jev-assisted meaning evaluation (Phase J).

Feature-gated by config.json "semantic" (default OFF):

- off:     no job seeding, no drain, no external communication — pending
           state and raw data are preserved untouched (INV-22, AT-053)
- shadow:  artifacts are recorded under semantic_*/loop_*/notify_plan
           kinds. With the default legacy fact source, existing extraction
           selection stays unchanged. The separately gated canonical fact
           source publishes audited projections even in shadow mode.
           ACK policy, outbox and formal requests stay unchanged
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
import os
import sys
import time

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
import local_llm
from ledger import Ledger, LedgerReader
from mcs_util import acquire_run_lock, load_config
import semantic_jev as jev  # noqa: F401 — facade patch point for tests
import semantic_runtime as runtime

# policy + config constants — semantic_policy.py owns them.
from semantic_policy import (JOB_KIND, KIND_ASSESS, KIND_AUDIT,
                             KIND_BUNDLE, KIND_FACTS, KIND_LOOP,
                             KIND_LOOP_EVENT, KIND_PLAN, KIND_SUMMARY,
                             KIND_USAGE, MODES, POLICY_VERSION,
                             SCHEMA_VERSION, policy_fingerprint,
                             semantic_config)
# audit gates (spec §16) — semantic_audit.py owns the checks; names stay
# re-exported here for the facade contract and monkeypatch points.
from semantic_audit import audit_claims, audit_code, audit_status_for
# notice rendering (spec §20/§19.3) — semantic_render.py owns the text.
from semantic_render import (_emit_degraded, _notify_src_event,
                             _outbox_has_delivery, render_notice)

# bundle + artifact helpers — semantic_store.py owns them.
from semantic_store import (_current, _jst_day_start, bundle_fingerprint,
                            jev_state, jev_usage_today, thread_bundle)

# fact extraction + summarization prompts — semantic_llm.py owns them;
# re-exported for the facade contract (semantic_extraction also reaches
# them through its `semantic` facade argument).
from semantic_llm import (_FACT_PROMPT, _REPAIR_SUFFIX, _SUMMARY_PROMPT,
                          _chunks, extract_facts, summarize)
# drain engine — semantic_drain.py owns the durable-job worker;
# _process_job stays re-exported because tests patch it on the facade.
# drain/loops reach back through `import semantic` inside functions, so
# top-level imports here are safe from module cycles.
from semantic_drain import (_eval_chunked, _jev_failure_class,
                            _plan_exists, _process_job, _process_job_inner,
                            _write_result, run_due)
from semantic_loops import update_loops
from semantic_llm import (CLAIM_KINDS, CLAIM_SECTIONS, FACT_KINDS,
                          FACT_STATUSES, POLARITIES, _iso_date,
                          _json_block, _locate_quote)

# Re-export contract: names below are imported for callers that use the
# facade surface (tests, semantic_extraction's `semantic` arg, notify_flush).
__all__ = [
    "CLAIM_KINDS", "CLAIM_SECTIONS", "FACT_KINDS", "FACT_STATUSES",
    "JOB_KIND", "KIND_ASSESS", "KIND_AUDIT", "KIND_BUNDLE",
    "KIND_FACTS", "KIND_LOOP", "KIND_LOOP_EVENT", "KIND_PLAN",
    "KIND_SUMMARY", "KIND_USAGE", "MODES", "POLARITIES",
    "POLICY_VERSION", "SCHEMA_VERSION",
    "_FACT_PROMPT", "_REPAIR_SUFFIX", "_SUMMARY_PROMPT", "_chunks",
    "_current", "_emit_degraded", "_eval_chunked", "_iso_date",
    "_jev_failure_class", "_json_block", "_jst_day_start",
    "_locate_quote", "_notify_src_event", "_outbox_has_delivery",
    "_plan_exists", "_process_job", "_process_job_inner",
    "_write_result", "audit_claims", "audit_code", "audit_status_for",
    "bundle_fingerprint", "extract_facts", "jev_state",
    "jev_usage_today", "policy_fingerprint", "render_notice",
    "run_due", "semantic_config", "summarize", "thread_bundle",
    "update_loops",
]

HOME = os.path.expanduser("~/.mcs")
DB = os.path.join(HOME, "data", "ledger.db")
CONF_PATH = os.path.join(HOME, "config.json")

# defaults — config.json local_llm.url/local_llm.model override them;
# use llm_conf()/llm_model() at call time so a config change needs no
# code edit (drain/v4 record llm_model() in artifact metadata). The
# constants stay a test patch point: a patched constant wins over both.
LLM_ENDPOINT = local_llm.ENDPOINT
LLM_MODEL = local_llm.MODEL
_LLM_EPIN, _LLM_MPIN = LLM_ENDPOINT, LLM_MODEL


def llm_conf(cfg: dict | None = None) -> tuple[str, str]:
    """(endpoint, model) — local_llm.url/local_llm.model over the pinned
    defaults; patched module constants take precedence (test seam)."""
    ep, mdl = local_llm.resolve(
        cfg if cfg is not None else load_config())
    if LLM_ENDPOINT != _LLM_EPIN:
        ep = LLM_ENDPOINT
    if LLM_MODEL != _LLM_MPIN:
        mdl = LLM_MODEL
    return ep, mdl


def llm_model(cfg: dict | None = None) -> str:
    return llm_conf(cfg)[1]
# 600s: the v2 fact document can emit ~2-4K tokens, and under dual-slot
# load decode runs ~3-5 t/s — a single call was measured at 322s
# (2026-09-27), so anything below ~450 still turns legitimate v2
# documents into retrying "model" failures.  The drain also uses this
# constant as its per-call timeout_cap.
LLM_TIMEOUT = 600

# v2 fact documents (facts + evidence + category_presence) and v4
# summaries routinely exceed the shared 1400-token default — a real
# clinical message hit finish_reason:"length" at exactly 1400 tokens
# mid-document (measured 2026-09-27), which the acceptance check then
# rejects as an eternal "model" failure.  The ceiling only bounds the
# worst case; the timeout still bounds wall time.
LLM_MAX_TOKENS = 4096


# ---------- local LLM (existing endpoint, same isolation) ----------

# Constrained JSON output for every semantic prompt (facts v2, legacy
# facts, summary, repairs all end in "JSON:"): the server's json_object
# grammar removes prose-wrapped/unterminated replies that parsed as
# nothing (11 of 104 shadow audits in the week to 2026-09-30 had no
# parseable summary). Same probe/cooldown/degrade ladder as
# extract_llm, minus the schema rung; a rejected format degrades to
# plain for one retry and re-probes after the cooldown.
_FMT_MODE = None      # None=unprobed | "object" | "plain"
_FMT_TS = 0.0
_PROBE_RETRY_S = 600
_FMT_REJECT_STATUSES = (400, 404, 422)


def _probe_format(endpoint: str, model: str, timeout: float = 10) -> str:
    global _FMT_MODE, _FMT_TS
    if _FMT_MODE == "object":
        return _FMT_MODE
    if _FMT_MODE is not None \
            and time.monotonic() - _FMT_TS < _PROBE_RETRY_S:
        return _FMT_MODE
    try:
        _FMT_MODE = local_llm.probe_format(
            endpoint, model, None, timeout=min(10, timeout),
            request_fn=local_llm.bounded_request,
            slot=local_llm.request_slot(),
            verify=lambda text: _json_block(text) is not None) or "plain"
    except Exception:
        _FMT_MODE = "plain"
    _FMT_TS = time.monotonic()
    return _FMT_MODE


# Long outputs (2026-10-01): a fact-rich message can need more than
# max_tokens — one 879-char post emits a 40-fact v2 doc of ~5.7k tokens.
# With temperature 0 every retry stopped at the same ceiling, spending
# ~5 min of a slot per attempt until the job failed. A length stop is
# retried ONCE with LLM_LONG_MAX_TOKENS in the remaining budget, and the
# prompt is remembered (1 = start at the long ceiling, 2 = even that
# stopped on length: fail fast without a model call) so later attempts
# spend the long call only once. Prompt + long ceiling fit one slot.
LLM_LONG_MAX_TOKENS = 8192
_LONG_PATH = os.path.join(HOME, "data", "semantic_long_output.json")
_LONG_MAX = 500
# a long-ceiling call measured ~320 s alone: with less budget it would
# only time out and burn an attempt, so it is not sent (free defer —
# the 450 s background lanes take it; a tick lane never can)
_LONG_MIN_CALL_S = 400.0


def _long_key(model: str, prompt: str) -> str:
    import hashlib
    return hashlib.sha256(f"{model}\0{prompt}".encode("utf-8")).hexdigest()


def _long_marks() -> dict:
    try:
        with open(_LONG_PATH, encoding="utf-8") as f:
            marks = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(marks, dict):
        return {}
    # hand-edited values (non-int) would raise at `level >= 2`
    return {k: v for k, v in marks.items() if type(v) is int}


def _long_mark(key: str, level: int) -> None:
    """Best effort — a lost write only costs one more length stop."""
    # ponytail: unlocked read-modify-write across processes; a lost
    # concurrent mark only re-spends one call — lock it if marks churn
    try:
        import maintenance
        marks = _long_marks()
        marks.pop(key, None)
        marks[key] = level
        while len(marks) > _LONG_MAX:
            marks.pop(next(iter(marks)))
        os.makedirs(os.path.dirname(_LONG_PATH), exist_ok=True)
        maintenance.atomic_publish_text(_LONG_PATH, json.dumps(marks))
    except Exception:
        pass


def llm_chat(prompt: str, timeout: int = LLM_TIMEOUT,
             max_tokens: int = LLM_MAX_TOKENS) -> str | None:
    """One local-llama.cpp chat call via the shared loopback adapter.
    The model gets no tools and no send capability; worker-isolated
    request, no proxy, no redirect, bounded bytes (INV-13, §15.3).
    Returns raw text or None (a model failure); raises
    ``semantic_runtime.LLMNotSent`` when the request never left — an
    admission hold/deferral or a refused connection — so callers can
    wait without consuming an attempt (mirrors extract_llm's
    ``_DEFERRED``)."""
    endpoint, model = llm_conf()
    err_out: dict = {}
    if local_llm.admission_enabled():
        # T20: route through the RT/BACKLOG admission boundary — the
        # registered route binds the class; the response shape carries
        # an `admission` verdict on denial/deferral (never model text)
        response = local_llm.admitted_chat(
            "mcs.semantic", prompt,
            endpoint=endpoint, model=model, timeout=timeout,
            max_tokens=max_tokens,
            request_fn=local_llm.bounded_request, error_out=err_out)
        if response is not None and response.get("admission"):
            raise runtime.LLMNotSent(f"llm_admission:{response['admission']}")
    else:
        global _FMT_MODE, _FMT_TS
        # the probe spends the caller's budget, never adds to it: the
        # runtime sized ``timeout`` to the job deadline (absolute)
        started = time.monotonic()
        rf = {"type": "json_object"} \
            if _probe_format(endpoint, model, timeout) == "object" else None
        timeout = max(0.5, timeout - (time.monotonic() - started))
        key = _long_key(model, prompt)
        level = _long_marks().get(key, 0)
        if level >= 2:
            return None       # even the long ceiling stopped on length
        if level and timeout < _LONG_MIN_CALL_S:
            raise runtime.LLMNotSent("long_output_needs_budget")
        while True:
            call_at = time.monotonic()
            response = local_llm.chat(
                prompt, endpoint=endpoint, model=model,
                timeout=timeout,
                max_tokens=max(max_tokens, LLM_LONG_MAX_TOKENS)
                if level else max_tokens,
                response_format=rf,
                extra_payload={"id_slot": local_llm.request_slot()},
                request_fn=local_llm.bounded_request, error_out=err_out)
            timeout = max(0.5, timeout - (time.monotonic() - call_at))
            if rf is not None and response is not None \
                    and response.get("status") in _FMT_REJECT_STATUSES:
                # the server rejected the constraint (restart / model
                # swap): degrade to plain for this and later calls —
                # the retry shares what is left of the caller's budget
                _FMT_MODE, _FMT_TS = "plain", time.monotonic()
                rf = None
                if level and timeout < _LONG_MIN_CALL_S:
                    # The rejected request already left: do not report a
                    # free not-sent defer, or send a long call that cannot
                    # fit. Keep the mark for a later adequately sized try.
                    return None
                continue
            if response is not None \
                    and response.get("finish_reason") == "length":
                level += 1
                _long_mark(key, level)
                if level == 1 and timeout >= _LONG_MIN_CALL_S:
                    continue  # one long-ceiling retry in what is left
            break
    if response is None and err_out.get("kind") == "unreachable":
        raise runtime.LLMNotSent("llm_unreachable")
    # canonical acceptance: a length-truncated or empty completion is an
    # incomplete result, never a success payload — even when its text
    # happens to parse (C05)
    if local_llm.acceptance_error(response) is not None:
        return None
    return response["text"]


# ---------- open loop candidates (spec §17) ----------
# Implementation lives in semantic_loops.py; this facade keeps the public name.
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


def _canonical_readiness(ledger, cfg: dict | None) -> dict:
    """Shadow -> canonical migration readout (read-only counts).

    Answers the promotion question — how much v2 shadow material
    exists, how much of it completed coverage, and how much has already
    passed the audit/publish stages — without running any model call.
    The promotion gate itself (fact_source=canonical needs human-
    labelled gate evidence) stays with `mcs_setup fact-source`."""
    from semantic_policy import (KIND_FACTS_V2, KIND_FACT_AUDIT,
                                 KIND_FACT_PROJ, semantic_config)
    from semantic_v4 import KIND_V4
    from mcs_queries import json_object_or_null
    out = {"scope": "history", "fact_source": None, "shadow_v2_docs": 0,
           "v2_coverage_complete": 0, "v2_needs_review": 0,
           "fact_audits": {}, "canonical_projection": 0,
           "semantic_facts_v4": 0}
    if cfg is None:
        out["available"] = False
        return out
    scfg, errors = semantic_config(cfg)
    if errors:
        out.update(available=False, reason="config_invalid")
        return out
    out["available"] = True
    out["fact_source"] = scfg["fact_source"]
    for r in ledger.db.execute(
            "SELECT meta FROM artifacts WHERE kind=?",
            (KIND_FACTS_V2,)):
        out["shadow_v2_docs"] += 1
        try:
            meta = json.loads(r["meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if meta.get("coverage_status") == "complete":
            out["v2_coverage_complete"] += 1
        if meta.get("needs_review"):
            out["v2_needs_review"] += 1
    for r in ledger.db.execute(
            f"SELECT json_extract({json_object_or_null('meta')},"
            "'$.audit_status') s, COUNT(*) c "
            "FROM artifacts WHERE kind=? GROUP BY s",
            (KIND_FACT_AUDIT,)):
        out["fact_audits"][r["s"] or "unknown"] = r["c"]
    for kind, key in ((KIND_FACT_PROJ, "canonical_projection"),
                      (KIND_V4, "semantic_facts_v4")):
        out[key] = ledger.db.execute(
            "SELECT COUNT(*) c FROM artifacts WHERE kind=?",
            (kind,)).fetchone()["c"]
    return out


def status_report(ledger, cfg: dict | None = None) -> dict:
    from semantic_metrics import audit_history, current_quality
    jobs = {"pending": 0, "failed": 0, "done": 0}
    for r in ledger.db.execute(
            "SELECT state,COUNT(*) c FROM fetch_jobs WHERE kind=? "
            "GROUP BY state", (JOB_KIND,)):
        jobs[r["state"]] = r["c"]
    history = audit_history(ledger)
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
    try:
        usage = {"jev_requests_today": jev_usage_today(ledger)}
    except ValueError:
        usage = {"jev_requests_today": None, "jev_usage_error": "semantic_usage_invalid"}
    return {"semantic_jobs": jobs, "audit_statuses": history["audit_statuses"],
            "canonical_readiness": _canonical_readiness(ledger, cfg),
            "audit_statuses_scope": "history", "history": history,
            "current_quality": current_quality(ledger, cfg),
            "oldest_pending_job_age_s": (max(0.0, time.time() - oldest)
                                         if oldest is not None else None),
            "semantic_paused_projects": paused_projects,
            "loop_candidates": loops,
            **usage,
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
        print(json.dumps(status_report(reader, load_config(CONF_PATH)),
                         ensure_ascii=False))
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
                      time.monotonic() + 600, max_jobs=args.max_jobs,
                      cfg_path=CONF_PATH)
        out["errors"] = result["errors"]
        print(json.dumps(out, ensure_ascii=False))
        return 0 if not result["errors"] else 1
    finally:
        ledger.close()
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
