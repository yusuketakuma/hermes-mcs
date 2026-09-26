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

from semantic_policy import JOB_KIND, QC_JOB_KIND  # noqa: E402
from semantic_drain import SCHED_KIND  # noqa: E402

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
        "AND state='pending' AND CASE WHEN json_valid(payload) THEN "
        "json_extract(payload,'$.eligible')=1 ELSE 0 END")[0][0]
    # version-aware: counts the messages still lacking a CURRENT-schema
    # artifact — matches run_pending's `left`, so migration progress is
    # visible instead of reading 0 while the v2 backlog drains
    from extract_llm import EXTRACT_VERSION
    extract_left = q(
        "SELECT COUNT(*) FROM messages m WHERE m.body_text IS NOT NULL "
        "AND m.body_text != '' "
        "AND (m.body_state IS NULL OR m.body_state='full') "
        "AND NOT EXISTS "
        "(SELECT 1 FROM artifacts a WHERE a.kind='extract_llm' "
        " AND a.message_id=m.message_id AND json_valid(a.meta) "
        " AND json_extract(a.meta,'$.error') IS NOT 1 "
        " AND json_extract(a.meta,'$.hash')=m.content_hash "
        " AND json_extract(a.meta,'$.extract_version')=?)",
        (EXTRACT_VERSION,))[0][0]
    from semantic_store import _jst_day_start
    jst_start = _jst_day_start(time.time())
    jev_today = q(
        "SELECT COALESCE(SUM(CASE WHEN json_valid(meta) THEN "
        "json_extract(meta,'$.jev_requests') ELSE 0 END),0) "
        "FROM artifacts WHERE kind='semantic_usage' AND created_at >= ?",
        (jst_start,))[0][0]
    # ---- T14: queue ages, cohort split, scheduler, recent rates ----
    # every field stays None (unknown) when its data is absent — a
    # missing measurement is never reported as zero
    now = time.time()
    queue_ages = {}
    for name, kinds in (("semantic", (JOB_KIND,)),
                        ("extract_qc", (QC_JOB_KIND,))):
        oldest = q(
            "SELECT MIN(created_at) FROM fetch_jobs "
            "WHERE state='pending' AND kind=?",
            kinds)[0][0]
        queue_ages[name] = (max(0.0, now - oldest)
                            if oldest is not None else None)
    oldest_msg = q(
        "SELECT MIN(posted_at_ts) FROM messages m "
        "WHERE m.body_text IS NOT NULL AND m.body_text != '' "
        "AND (m.body_state IS NULL OR m.body_state='full') "
        "AND NOT EXISTS "
        "(SELECT 1 FROM artifacts a WHERE a.kind='extract_llm' "
        " AND a.message_id=m.message_id AND json_valid(a.meta) "
        " AND json_extract(a.meta,'$.error') IS NOT 1 "
        " AND json_extract(a.meta,'$.hash')=m.content_hash "
        " AND json_extract(a.meta,'$.extract_version')=?)",
        (EXTRACT_VERSION,))[0][0]
    queue_ages["extract_llm"] = (max(0.0, now - oldest_msg)
                                 if oldest_msg is not None else None)
    cohorts = {"arrival": 0, "backfill": 0}
    for cohort, n in q(
            "SELECT CASE WHEN json_valid(payload) "
            "AND json_extract(payload,'$.eligible')=1 "
            "THEN 'arrival' ELSE 'backfill' END, COUNT(*) "
            "FROM fetch_jobs WHERE kind=? AND state='pending' "
            "GROUP BY 1", (JOB_KIND,)):
        cohorts[cohort] = n
    sched_row = q("SELECT payload FROM fetch_jobs WHERE kind=? "
                  "AND project_id=0 AND message_id=0", (SCHED_KIND,))
    sched = {}
    if sched_row:
        try:
            sched = json.loads(sched_row[0][0] or "{}")
        except (json.JSONDecodeError, TypeError):
            sched = {}
    scheduler = {
        "arrival_selected": sched.get("arrival_selected"),
        "backfill_selected": sched.get("backfill_selected"),
        "backfill_last_served_at": sched.get("backfill_last_served_at"),
    }
    drain_rows = q("SELECT content FROM artifacts "
                   "WHERE kind='semantic_drain_run' "
                   "ORDER BY artifact_id DESC LIMIT 100")
    recent = {"runs": 0, "done": 0, "deferred": 0, "failed": 0,
              "llm_s": None, "jev_s": None, "post_s": None,
              "queue_wait_s_max": None, "usage_tokens": None}
    llm_s = jev_s = post_s = qw_max = tokens = 0.0
    have_phase = have_usage = False
    for (content,) in drain_rows:
        try:
            run = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(run, dict):
            continue
        recent["runs"] += 1
        for key in ("done", "deferred", "failed"):
            value = run.get(key)
            if type(value) is int:
                recent[key] += value
        for metric in run.get("job_metrics") or []:
            if not isinstance(metric, dict):
                continue
            for key, acc in (("llm_s", "llm"), ("jev_s", "jev"),
                             ("post_s", "post")):
                value = metric.get(key)
                if type(value) in (int, float):
                    if acc == "llm":
                        llm_s += value
                    elif acc == "jev":
                        jev_s += value
                    else:
                        post_s += value
                    have_phase = True
            value = metric.get("queue_wait_s")
            if type(value) in (int, float):
                qw_max = max(qw_max, value)
                have_phase = True
            usage = metric.get("usage")
            if isinstance(usage, dict):
                for key in ("input_tokens", "output_tokens"):
                    value = usage.get(key)
                    if type(value) in (int, float):
                        tokens += value
                        have_usage = True
    if have_phase:
        recent.update({"llm_s": llm_s, "jev_s": jev_s,
                       "post_s": post_s, "queue_wait_s_max": qw_max})
    if have_usage:
        recent["usage_tokens"] = tokens
    integrity_rows = q(
        "SELECT meta FROM artifacts WHERE kind='extract_llm' "
        "ORDER BY artifact_id DESC LIMIT 200")
    extract_recent = {"artifacts": 0, "calls": None,
                      "prompt_ms": None, "predicted_ms": None,
                      "tokens": None}
    calls = pms = dms = toks = 0
    have_calls = have_ms = have_toks = False
    for (meta,) in integrity_rows:
        try:
            integrity = (json.loads(meta) or {}).get("integrity")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(integrity, dict):
            continue
        extract_recent["artifacts"] += 1
        if type(integrity.get("calls")) is int:
            calls += integrity["calls"]
            have_calls = True
        timings = integrity.get("timings")
        if isinstance(timings, dict):
            for key, acc in (("prompt_ms", "p"), ("predicted_ms", "d")):
                value = timings.get(key)
                if type(value) in (int, float):
                    if acc == "p":
                        pms += value
                    else:
                        dms += value
                    have_ms = True
        usage = integrity.get("usage")
        if isinstance(usage, dict) \
                and type(usage.get("total_tokens")) is int:
            toks += usage["total_tokens"]
            have_toks = True
    if have_calls:
        extract_recent["calls"] = calls
    if have_ms:
        extract_recent.update({"prompt_ms": pms, "predicted_ms": dms})
    if have_toks:
        extract_recent["tokens"] = toks
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
        "queue_ages_s": queue_ages,
        "cohorts": cohorts,
        "scheduler": scheduler,
        "recent_drain": recent,
        "extract_recent": extract_recent,
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
    ages = snap["queue_ages_s"]
    print("queue ages: " + ", ".join(
        f"{k}={'%.0fs' % v if v is not None else 'unknown'}"
        for k, v in ages.items()))
    co = snap["cohorts"]
    sched = snap["scheduler"]
    print(f"cohorts pending: arrival={co['arrival']} "
          f"backfill={co['backfill']} | "
          f"selected arrival={sched['arrival_selected']} "
          f"backfill={sched['backfill_selected']}")
    r = snap["recent_drain"]
    print(f"recent drains: {r['runs']} runs, "
          f"done={r['done']} deferred={r['deferred']} "
          f"failed={r['failed']} "
          f"(llm_s={r['llm_s']}, jev_s={r['jev_s']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
