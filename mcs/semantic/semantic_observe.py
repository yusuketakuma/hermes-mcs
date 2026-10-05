#!/usr/bin/env python3
"""Shadow-mode observation snapshot — one command for the daily check.

Read-only: dumps semantic job states, audit-status distribution, finding
codes, Jev usage, and backlog progress. Use during the shadow period to
decide when assist/enforce is safe:

    python3 semantic_observe.py            # human-readable snapshot
    python3 semantic_observe.py --json     # one JSON line (appendable)
    python3 semantic_observe.py --days 14  # recent_drain window (default 14)

Gates to watch (phase-j-record §7):
- current audit completion/PASS rate; historical outcomes are labelled separately
- jev_requests_today vs daily_request_budget (read from config)
- pending drain rate vs new-arrival rate
- failed job count staying at 0
"""
import json
import math
import os
from pathlib import Path
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

from mcs_util import HOME  # noqa: E402 — honours MCS_ROOT
DB = os.path.join(HOME, "data", "ledger.db")


def observe(db_path: str = DB, cfg: dict | None = None, days: int = 14) -> dict:
    c = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        return _observe(c, cfg, days)
    finally:
        c.close()


def _observe(c, cfg, days: int = 14) -> dict:
    from semantic_metrics import audit_history, current_quality
    ledger = SimpleNamespace(db=c)

    def q(sql, p=()):
        return c.execute(sql, p).fetchall()

    jobs = dict(q(
        "SELECT state, COUNT(*) FROM fetch_jobs WHERE kind='semantic' "
        "GROUP BY state"))
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
    from semantic_store import jev_usage_today
    try:
        usage = {"jev_requests_today": jev_usage_today(ledger)}
    except ValueError:
        usage = {"jev_requests_today": None, "jev_usage_error": "semantic_usage_invalid"}
    # ---- T14: queue ages, cohort split, scheduler, recent rates ----
    queue_ages, cohorts, scheduler = _queue_stats(c)
    recent = _recent_runs(c, days)
    extract_recent = _extract_recent(c)
    return {
        "ts": int(time.time()),
        "jobs": jobs,
        "eligible_pending": eligible_pending,
        **history,
        "audit_statuses_scope": "history",
        "history": history,
        "current_quality": current_quality(ledger, cfg),
        **usage,
        "jev_daily_budget": _daily_budget(cfg),
        "extract_llm_left": extract_left,
        "queue_ages_s": queue_ages,
        "cohorts": cohorts,
        "scheduler": scheduler,
        "recent_drain": recent,
        "extract_recent": extract_recent,
    }


def _queue_stats(c):
    """T14: queue ages, cohort split, scheduler — every field stays
    None (unknown) when its data is absent; a missing measurement is
    never reported as zero."""
    def q(sql, p=()):
        return c.execute(sql, p).fetchall()

    from extract_llm import EXTRACT_VERSION
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
    cohorts.update(q(
        "SELECT CASE WHEN json_valid(payload) "
        "AND json_extract(payload,'$.eligible')=1 "
        "THEN 'arrival' ELSE 'backfill' END, COUNT(*) "
        "FROM fetch_jobs WHERE kind=? AND state='pending' "
        "GROUP BY 1", (JOB_KIND,)))
    sched_row = q("SELECT payload FROM fetch_jobs WHERE kind=? "
                  "AND project_id=0 AND message_id=0", (SCHED_KIND,))
    sched = {}
    if sched_row:
        try:
            sched = json.loads(sched_row[0][0] or "{}")
        except (json.JSONDecodeError, TypeError):
            sched = {}
    if not isinstance(sched, dict):
        sched = {}
    scheduler = {
        "arrival_selected": sched.get("arrival_selected"),
        "backfill_selected": sched.get("backfill_selected"),
        "backfill_last_served_at": sched.get("backfill_last_served_at"),
        "qc_selected": sched.get("qc_selected"),
        "qc_last_served_at": sched.get("qc_last_served_at"),
    }
    return queue_ages, cohorts, scheduler


def _nonnegative(value, *, integer=False) -> bool:
    if type(value) not in ((int,) if integer else (int, float)):
        return False
    try:
        return value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def _complete_total(values, *, reducer=sum, integer=False):
    """A total needs every observation; missing phases stay unknown."""
    if not values or not all(_nonnegative(v, integer=integer) for v in values):
        return None
    result = reducer(values)
    return result if _nonnegative(result, integer=integer) else None


def _recent_runs(c, days: int = 14) -> dict:
    """Aggregate measured phases over the semantic drain runs of the last
    `days` days (capacity gate: llm_s per window + per-job llm_s p90)."""
    from semantic_evaluation import _percentile
    rows = c.execute(
        "SELECT content FROM artifacts WHERE kind='semantic_drain_run' "
        "AND created_at >= ? ORDER BY artifact_id DESC",
        (time.time() - days * 86400,)).fetchall()
    recent = {"runs": 0, "done": 0, "deferred": 0, "failed": 0}
    phases = {key: [] for key in ("llm_s", "jev_s", "post_s", "queue_wait_s")}
    tokens = []
    for (content,) in rows:
        try:
            run = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(run, dict):
            continue
        recent["runs"] += 1
        for key in ("done", "deferred", "failed"):
            value = run.get(key)
            if _nonnegative(value, integer=True):
                recent[key] += value
        metrics = run.get("job_metrics")
        if not isinstance(metrics, list):
            metrics = [None]
        for metric in metrics:
            metric = metric if isinstance(metric, dict) else {}
            for key, values in phases.items():
                values.append(metric.get(key))
            usage = metric.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            tokens.append(_complete_total(
                [usage.get("input_tokens"), usage.get("output_tokens")],
                integer=True) if type(usage.get("unreported_requests")) is int
                and usage["unreported_requests"] == 0 else None)
    recent.update({key: _complete_total(phases[key])
                   for key in ("llm_s", "jev_s", "post_s")})
    recent["queue_wait_s_max"] = _complete_total(phases["queue_wait_s"], reducer=max)
    # same completeness rule as the total: any job without a valid llm_s
    # leaves both gate inputs unknown instead of a p90 over a subset
    recent["llm_s_p90"] = (None if recent["llm_s"] is None
                           else _percentile(phases["llm_s"], 0.90))
    recent["usage_tokens"] = _complete_total(tokens, integer=True)
    return recent


def _extract_recent(c) -> dict:
    """Aggregate independently measured extraction metrics over 200 rows."""
    rows = c.execute(
        "SELECT meta FROM artifacts WHERE kind='extract_llm' "
        "ORDER BY artifact_id DESC LIMIT 200").fetchall()
    recent = {"artifacts": 0}
    values = {key: [] for key in ("calls", "prompt_ms", "predicted_ms", "tokens")}
    for (raw,) in rows:
        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        integrity = meta.get("integrity") if isinstance(meta, dict) else None
        if not isinstance(integrity, dict):
            continue
        recent["artifacts"] += 1
        values["calls"].append(integrity.get("calls"))
        timings = integrity.get("timings")
        timings = timings if isinstance(timings, dict) else {}
        for key in ("prompt_ms", "predicted_ms"):
            values[key].append(timings.get(key))
        usage = integrity.get("usage")
        values["tokens"].append(usage.get("total_tokens")
                                if isinstance(usage, dict) else None)
    recent.update({key: _complete_total(observations, integer=key in ("calls", "tokens"))
                   for key, observations in values.items()})
    return recent


def _daily_budget(cfg) -> int:
    from semantic_policy import semantic_config
    return semantic_config(cfg if isinstance(cfg, dict) else {})[0]["daily_request_budget"]


def main() -> int:
    from mcs_util import load_config
    days = 14
    if "--days" in sys.argv:
        try:
            days = int(sys.argv[sys.argv.index("--days") + 1])
            if days < 1:
                raise ValueError(days)
        except (ValueError, IndexError):
            print("usage: semantic_observe.py [--json] [--days N>=1]",
                  file=sys.stderr)
            return 2
    snap = observe(cfg=load_config(), days=days)
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
        f"{k}={f'{v:.0f}s' if v is not None else 'unknown'}"
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
          f"(llm_s={r['llm_s']}, llm_s_p90={r['llm_s_p90']}, "
          f"jev_s={r['jev_s']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
