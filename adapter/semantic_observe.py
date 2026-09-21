#!/usr/bin/env python3
"""Shadow-mode observation snapshot — one command for the daily check.

Read-only: dumps semantic job states, audit-status distribution, finding
codes, Jev usage, and backlog progress. Use during the shadow period to
decide when assist/enforce is safe:

    python3 semantic_observe.py            # human-readable snapshot
    python3 semantic_observe.py --json     # one JSON line (appendable)

Gates to watch (phase-j-record §7):
- audit PASS rate and NEEDS_REVIEW reason distribution
- jev_requests_today vs daily_request_budget (read from config)
- pending drain rate vs new-arrival rate
- failed job count staying at 0
"""
import json
import os
import sqlite3
import sys
import time

HOME = os.path.expanduser("~/.mcs")
DB = os.path.join(HOME, "data", "ledger.db")


def observe(db_path: str = DB) -> dict:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    q = lambda sql, p=(): c.execute(sql, p).fetchall()

    jobs = {s: n for s, n in q(
        "SELECT state, COUNT(*) FROM fetch_jobs WHERE kind='semantic' "
        "GROUP BY state")}
    audits = {s or "unparsed": n for s, n in q(
        "SELECT json_extract(meta,'$.audit_status'), COUNT(*) "
        "FROM artifacts WHERE kind='semantic_audit' GROUP BY 1")}
    findings = {}
    for (code,) in q(
            "SELECT json_extract(f.value,'$.code') "
            "FROM artifacts a, json_each(a.content,'$.findings') f "
            "WHERE a.kind='semantic_audit' AND json_valid(a.content)"):
        if code:
            findings[code] = findings.get(code, 0) + 1
    eligible_pending = q(
        "SELECT COUNT(*) FROM fetch_jobs WHERE kind='semantic' "
        "AND state='pending' AND json_extract(payload,'$.eligible')=1")[0][0]
    # repair-path effectiveness: among audits that consumed a repair,
    # how many still landed NEEDS_REVIEW vs recovered to PASS
    repairs = {s or "?": n for s, n in q(
        "SELECT json_extract(meta,'$.audit_status'), COUNT(*) "
        "FROM artifacts WHERE kind='semantic_audit' "
        "AND json_extract(meta,'$.repair_count')=1 GROUP BY 1")}
    extract_left = q(
        "SELECT COUNT(*) FROM messages m WHERE m.body_text IS NOT NULL "
        "AND m.body_text != '' AND NOT EXISTS "
        "(SELECT 1 FROM artifacts a WHERE a.kind='extract_llm' "
        " AND a.message_id=m.message_id AND json_valid(a.meta) "
        " AND json_extract(a.meta,'$.error') IS NOT 1 "
        " AND json_extract(a.meta,'$.hash')=m.content_hash)")[0][0]
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
        "audit_statuses": audits,
        "repaired_audits": repairs,
        "finding_codes": findings,
        "jev_requests_today": int(jev_today),
        "jev_daily_budget": _daily_budget(),
        "extract_llm_left": extract_left,
    }


def _daily_budget() -> int:
    from mcs_util import load_config
    try:
        return int(load_config().get("semantic", {})
                   .get("daily_request_budget", 0))
    except (TypeError, ValueError):
        return 0


def main() -> int:
    snap = observe()
    if "--json" in sys.argv:
        print(json.dumps(snap, ensure_ascii=False))
        return 0
    j = snap["jobs"]
    print(f"jobs: done={j.get('done',0)} pending={j.get('pending',0)} "
          f"failed={j.get('failed',0)} "
          f"(notify-eligible pending: {snap['eligible_pending']})")
    print(f"audit: {snap['audit_statuses'] or 'none yet'}")
    if snap["repaired_audits"]:
        print(f"repaired: {snap['repaired_audits']}")
    if snap["finding_codes"]:
        print(f"findings: {snap['finding_codes']}")
    print(f"jev today: {snap['jev_requests_today']}/"
          f"{snap['jev_daily_budget']} | "
          f"extract_llm backlog left: {snap['extract_llm_left']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
