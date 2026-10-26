#!/usr/bin/env python3
"""MCS semantic layer — Jev-assisted meaning evaluation (Phase J).

Jev is enabled from this MCS dotenv when mode is omitted; explicit modes remain authoritative:

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
from contextlib import closing
import json
import os
import sys
import time

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every subdir holding .py files (any depth) as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
import semantic_drain
import semantic_policy
import semantic_store
from ledger import Ledger, LedgerReader
from mcs_util import CONF_PATH, DB, load_config, loads_dict
from writer_lock import acquire_run_lock
import semantic_runtime as runtime


def seed(ledger, message_id: int, origin: str = "replay",
         cfg_path: str = CONF_PATH) -> int | None:
    """Explicit finite replay/retry seed — notification-free. OFF blocks
    even this (no new job generation, AT-053)."""
    cfg, _ = semantic_policy.runtime_config(load_config(cfg_path), root=os.path.dirname(cfg_path))
    if cfg["mode"] == "off":
        return None
    row = ledger.db.execute(
        "SELECT project_id, COALESCE(parent_id,message_id) r "
        "FROM messages WHERE message_id=?", (message_id,)).fetchone()
    from mcs_queries import paused
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
                                 KIND_FACT_PROJ)
    from semantic_v4 import KIND_V4
    from mcs_queries import json_object_or_null
    out = {"scope": "history", "fact_source": None, "shadow_v2_docs": 0,
           "v2_coverage_complete": 0, "v2_needs_review": 0,
           "fact_audits": {}, "canonical_projection": 0,
           "semantic_facts_v4": 0}
    if cfg is None:
        out["available"] = False
        return out
    scfg, errors = semantic_policy.runtime_config(cfg)
    if errors:
        out.update(available=False, reason="config_invalid")
        return out
    out["available"] = True
    out["fact_source"] = scfg["fact_source"]
    for r in ledger.db.execute(
            "SELECT meta FROM artifacts WHERE kind=?",
            (KIND_FACTS_V2,)):
        out["shadow_v2_docs"] += 1
        meta = loads_dict(r["meta"]) or {}
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
            "GROUP BY state", (semantic_policy.JOB_KIND,)):
        jobs[r["state"]] = r["c"]
    history = audit_history(ledger)
    loops = ledger.db.execute(
        "SELECT COUNT(*) c FROM artifacts WHERE kind=?",
        (semantic_policy.KIND_LOOP,)).fetchone()["c"]
    from mcs_queries import paused
    paused_projects = [r[0] for r in ledger.db.execute(
        "SELECT DISTINCT project_id FROM artifacts WHERE kind='semantic_control' "
        "AND project_id IS NOT NULL ORDER BY project_id") if paused(ledger.db, r[0])]
    oldest = ledger.db.execute(
        "SELECT MIN(created_at) FROM fetch_jobs WHERE kind=? AND state='pending'",
        (semantic_policy.JOB_KIND,)).fetchone()[0]
    try:
        usage = {"jev_requests_today": semantic_store.jev_usage_today(ledger)}
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
        with closing(reader):
            print(json.dumps(status_report(reader, load_config(CONF_PATH)),
                             ensure_ascii=False))
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
        out = semantic_drain.run_due(ledger, load_config(CONF_PATH), result,
                      time.monotonic() + 600, max_jobs=args.max_jobs,
                      cfg_path=CONF_PATH)
        out["errors"] = result["errors"]
        print(json.dumps(out, ensure_ascii=False))
        return 0 if not result["errors"] else 1
    finally:
        try:
            ledger.close()
        finally:
            os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
