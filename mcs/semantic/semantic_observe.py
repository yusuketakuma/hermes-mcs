#!/usr/bin/env python3
"""Shadow-mode observation snapshot — one command for the daily check.

Read-only: dumps semantic job states, audit-status distribution, finding
codes, Jev usage, and backlog progress. Use during the shadow period to
decide when assist/enforce is safe:

    python3 semantic_observe.py            # human-readable snapshot
    python3 semantic_observe.py --json     # one JSON line (appendable)

Gates to watch (phase-j-record §7):
- current audit completion/PASS rate; historical outcomes are labelled separately
- jev_requests_today vs daily_request_budget (read from config)
- pending drain rate vs new-arrival rate
- failed job count staying at 0
"""
import json
import os
import sqlite3
import sys
import time
from types import SimpleNamespace

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

HOME = os.path.expanduser("~/.mcs")
DB = os.path.join(HOME, "data", "ledger.db")


def observe(db_path: str = DB, cfg: dict | None = None) -> dict:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        return _observe(c, cfg)
    finally:
        c.close()


def _observe(c, cfg) -> dict:
    from semantic_metrics import audit_history, current_quality
    ledger = SimpleNamespace(db=c)

    def q(sql, p=()):
        return c.execute(sql, p).fetchall()

    jobs = {s: n for s, n in q(
        "SELECT state, COUNT(*) FROM fetch_jobs WHERE kind='semantic' "
        "GROUP BY state")}
    history = audit_history(ledger)
    eligible_pending = q(
        "SELECT COUNT(*) FROM fetch_jobs WHERE kind='semantic' "
        "AND state='pending' AND json_extract(payload,'$.eligible')=1")[0][0]
    # version-aware: counts the messages still lacking a CURRENT-schema
    # artifact — matches run_pending's `left`, so migration progress is
    # visible instead of reading 0 while the v2 backlog drains
    from extract_llm import EXTRACT_VERSION
    extract_left = q(
        "SELECT COUNT(*) FROM messages m WHERE m.body_text IS NOT NULL "
        "AND m.body_text != '' AND NOT EXISTS "
        "(SELECT 1 FROM artifacts a WHERE a.kind='extract_llm' "
        " AND a.message_id=m.message_id AND json_valid(a.meta) "
        " AND json_extract(a.meta,'$.error') IS NOT 1 "
        " AND json_extract(a.meta,'$.hash')=m.content_hash "
        " AND json_extract(a.meta,'$.extract_version')=?)",
        (EXTRACT_VERSION,))[0][0]
    from semantic_store import _jst_day_start
    jst_start = _jst_day_start(time.time())
    jev_today = q(
        "SELECT COALESCE(SUM(json_extract(meta,'$.jev_requests')),0) "
        "FROM artifacts WHERE kind='semantic_usage' AND created_at >= ?",
        (jst_start,))[0][0]
    return {
        "ts": int(time.time()),
        "jobs": jobs,
        "eligible_pending": eligible_pending,
        **history,
        "audit_statuses_scope": "history",
        "history": history,
        "current_quality": current_quality(ledger, cfg),
        "jev_requests_today": int(jev_today),
        "jev_daily_budget": _daily_budget(cfg),
        "extract_llm_left": extract_left,
    }


def _daily_budget(cfg) -> int:
    try:
        return int((cfg or {}).get("semantic", {})
                   .get("daily_request_budget", 0))
    except (TypeError, ValueError):
        return 0


def main() -> int:
    from mcs_util import load_config
    snap = observe(cfg=load_config())
    if "--json" in sys.argv:
        print(json.dumps(snap, ensure_ascii=False))
        return 0
    j = snap["jobs"]
    print(f"jobs: done={j.get('done',0)} pending={j.get('pending',0)} "
          f"failed={j.get('failed',0)} "
          f"(notify-eligible pending: {snap['eligible_pending']})")
    print(f"audit history: {snap['audit_statuses'] or 'none yet'}")
    if snap["repaired_audits"]:
        print(f"repaired history: {snap['repaired_audits']}")
    if snap["finding_codes"]:
        print(f"findings history: {snap['finding_codes']}")
    quality = snap["current_quality"]
    if quality["available"]:
        print(f"current audit coverage: {quality['complete']}/{quality['denominator']} "
              f"complete, {quality['incomplete']} incomplete; "
              f"{quality['audit_statuses']}")
    else:
        print(f"current audit coverage: unavailable ({quality['reason']})")
    print(f"jev today: {snap['jev_requests_today']}/"
          f"{snap['jev_daily_budget']} | "
          f"extract_llm backlog left: {snap['extract_llm_left']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
