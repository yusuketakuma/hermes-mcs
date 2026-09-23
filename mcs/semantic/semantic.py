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
import os
import sys
import time

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
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
# facade surface (tests, semantic_extraction's `semantic` arg, notifier).
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

LLM_ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
LLM_MODEL = "Qwen3.5-9B"
LLM_TIMEOUT = 90


# ---------- local LLM (existing endpoint, same isolation) ----------

def llm_chat(prompt: str, timeout: int = LLM_TIMEOUT) -> str | None:
    """One local-llama.cpp chat call via the shared loopback adapter.
    The model gets no tools and no send capability; worker-isolated
    request, no proxy, no redirect, bounded bytes (INV-13, §15.3).
    Returns raw text or None."""
    import local_llm
    response = local_llm.chat(
        prompt, endpoint=LLM_ENDPOINT, model=LLM_MODEL, timeout=timeout,
        extra_payload={"id_slot": local_llm.request_slot()},
        request_fn=local_llm.bounded_request)
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
    return {"semantic_jobs": jobs, "audit_statuses": history["audit_statuses"],
            "audit_statuses_scope": "history", "history": history,
            "current_quality": current_quality(ledger, cfg),
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
