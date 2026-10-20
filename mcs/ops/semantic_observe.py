#!/usr/bin/env python3
"""Read bounded snapshots of processing queues, rates, coverage and interruptions.

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
# registers every subdir holding .py files (any depth) as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

from semantic_policy import JOB_KIND, KIND_DRAIN_RUN, QC_JOB_KIND, SCHED_KIND  # noqa: E402

from mcs_util import HOME, loads_dict  # noqa: E402 — honours MCS_ROOT
DB = os.path.join(HOME, "data", "ledger.db")


OBSERVE_BUDGET_S = 10.0
OBSERVE_MAX_ITEMS = 200_000
OBSERVE_MAX_TEXT = 64 * 1024 * 1024
OBSERVE_MAX_CELL = 1024 * 1024


class _ObservationLimit(Exception):
    """A complete snapshot could not be produced within its read budget."""


class _ReadBudget:
    def __init__(self, deadline):
        self.deadline = deadline
        self.items = self.text = 0

    def checkpoint(self):
        if time.monotonic() >= self.deadline:
            raise _ObservationLimit("observation_deadline_exceeded")
        self.items += 1
        if self.items > OBSERVE_MAX_ITEMS:
            raise _ObservationLimit("observation_size_exceeded")

    def payload(self, value):
        """Charge a text cell without consuming another observed item."""
        if isinstance(value, (str, bytes)):
            # Character limits also bound Python's Unicode allocation.
            if len(value) > OBSERVE_MAX_CELL:
                raise _ObservationLimit("observation_size_exceeded")
            self.text += len(value)
        if self.text > OBSERVE_MAX_TEXT:
            raise _ObservationLimit("observation_size_exceeded")

    def row(self, cursor, values):
        self.checkpoint()
        for value in values:
            self.payload(value)
        return sqlite3.Row(cursor, values)


def observe(db_path: str = DB, cfg: dict | None = None, days: int = 14, *,
            budget_s: float = OBSERVE_BUDGET_S) -> dict:
    if type(days) is not int or days < 1:
        raise ValueError("observation_days_invalid")
    if (type(budget_s) not in (int, float) or not math.isfinite(budget_s)
            or not 0 < budget_s <= OBSERVE_BUDGET_S):
        raise ValueError("observation_budget_invalid")
    started = time.time()
    deadline = time.monotonic() + budget_s
    budget = _ReadBudget(deadline)
    c = None
    try:
        c = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True,
                            timeout=min(0.25, budget_s))
        c.row_factory = budget.row
        # Bound a single SQLite row before allocating or parsing it in Python.
        c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2 * OBSERVE_MAX_CELL)
        c.execute("PRAGMA query_only=ON")
        c.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        c.execute("BEGIN")
        # Establish the read view before any of the component queries.
        c.execute("SELECT rootpage FROM sqlite_master LIMIT 1").fetchone()
        snapshot = _observe(c, cfg, days, now=started, checkpoint=budget.checkpoint,
                            payload_checkpoint=budget.payload, quality=False)
        budget.checkpoint()
        # The heaviest scan runs last: exhausting the budget there leaves
        # only this component unavailable, not the queues read before it.
        quality = snapshot["current_quality"] = _bounded_quality(
            c, cfg, budget.checkpoint, deadline)
        limited = quality.get("reason") in _LIMIT_REASONS
        snapshot["observation"] = {"status": "partial" if limited else "complete",
                                   "at": started,
                                   "consistency": "single_read_transaction"}
        if limited:
            snapshot["observation"]["unavailable_components"] = ["current_quality"]
        return snapshot
    except _ObservationLimit as exc:
        return {"status": "unavailable", "reason": str(exc), "ts": int(started)}
    except sqlite3.Error as exc:
        return {"status": "unavailable", "ts": int(started),
                "reason": _limit_reason(exc, deadline) or "observation_database_unavailable"}
    finally:
        if c is not None:
            c.close()


_LIMIT_REASONS = ("observation_deadline_exceeded", "observation_size_exceeded")


def _limit_reason(exc, deadline):
    """The budget reason behind a SQLite error, or None for other failures."""
    if time.monotonic() >= deadline:
        return "observation_deadline_exceeded"
    if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_TOOBIG:
        return "observation_size_exceeded"
    return None


def _bounded_quality(c, cfg, checkpoint, deadline):
    from semantic_metrics import current_quality
    try:
        quality = current_quality(SimpleNamespace(db=c), cfg, checkpoint=checkpoint)
        checkpoint()   # its last lookup and decode may finish past the deadline
        return quality
    except _ObservationLimit as exc:
        return {"available": False, "reason": str(exc)}
    except sqlite3.Error as exc:
        reason = _limit_reason(exc, deadline)
        if reason is None:
            raise
        return {"available": False, "reason": reason}


def _observe(c, cfg, days: int = 14, *, now: float | None = None,
             checkpoint=lambda: None, payload_checkpoint=lambda raw: None,
             quality: bool = True) -> dict:
    now = time.time() if now is None else now
    from operational_status import interrupted_runs
    from operational_progress import collection_progress, pipeline_progress
    from semantic_metrics import audit_history, current_quality
    ledger = SimpleNamespace(db=c)

    def q(sql, p=()):
        return c.execute(sql, p).fetchall()

    jobs = dict(q(
        "SELECT state, COUNT(*) FROM fetch_jobs WHERE kind=? "
        "GROUP BY state", (JOB_KIND,)))
    history = audit_history(ledger, checkpoint=checkpoint)
    eligible_pending = q(
        "SELECT COUNT(*) FROM fetch_jobs WHERE kind=? "
        "AND state='pending' AND CASE WHEN json_valid(payload) THEN "
        "json_extract(payload,'$.eligible')=1 ELSE 0 END", (JOB_KIND,))[0][0]
    # run_pending's own `left` predicate, so the backlog and its age
    # drain exactly as the extractor works it (v4-current rows excluded)
    from extract_llm import EXTRACT_VERSION, pending_pred
    extract_left, oldest_msg = q(
        "SELECT COUNT(*), MIN(m.posted_at_ts) FROM messages m WHERE "
        + pending_pred())[0]
    from semantic_store import jev_usage_today
    try:
        usage = {"jev_requests_today": jev_usage_today(ledger)}
    except ValueError:
        usage = {"jev_requests_today": None, "jev_usage_error": "semantic_usage_invalid"}
    # ---- T14: queue ages, cohort split, scheduler, recent rates ----
    queue_ages, cohorts, scheduler = _queue_stats(c, oldest_msg, now=now)
    recent = _recent_runs(c, days, now=now, checkpoint=checkpoint)
    extract_recent = _extract_recent(c, checkpoint=checkpoint)
    return {
        "ts": int(now),
        "jobs": jobs,
        "eligible_pending": eligible_pending,
        **history,
        "audit_statuses_scope": "history",
        "history": history,
        "current_quality": (current_quality(ledger, cfg, checkpoint=checkpoint)
                            if quality else None),
        **usage,
        "jev_daily_budget": _daily_budget(cfg),
        "extract_llm_left": extract_left,
        "legacy_extraction": {"predicate_pending": extract_left,
                              "extract_version": EXTRACT_VERSION,
                              "scope": "stored_body_current_predicate",
                              "runnable": None,
                              "reason": "admission_lease_retry_not_evaluated"},
        "queue_ages_s": queue_ages,
        "cohorts": cohorts,
        "scheduler": scheduler,
        "recent_drain": recent,
        "extract_recent": extract_recent,
        "last_interrupt": interrupted_runs(c, now=now),
        "pipeline_progress": pipeline_progress(c, cfg, now=now, checkpoint=checkpoint,
                                               payload_checkpoint=payload_checkpoint),
        "collection_progress": collection_progress(c, now=now, checkpoint=checkpoint),
    }


def _queue_stats(c, oldest_msg, *, now=None):
    """T14: queue ages, cohort split, scheduler — every field stays
    None (unknown) when its data is absent; a missing measurement is
    never reported as zero."""
    def q(sql, p=()):
        return c.execute(sql, p).fetchall()

    now = time.time() if now is None else now

    def age(value):
        return max(0.0, now - value) if _nonnegative(value) else None

    queue_ages = {}
    for name, kinds in (("semantic", (JOB_KIND,)),
                        ("extract_qc", (QC_JOB_KIND,))):
        oldest = q(
            "SELECT MIN(created_at) FROM fetch_jobs "
            "WHERE state='pending' AND kind=?",
            kinds)[0][0]
        queue_ages[name] = age(oldest)
    queue_ages["extract_llm"] = age(oldest_msg)
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
        sched = loads_dict(sched_row[0][0]) or {}
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


def _complete_total(values, *, reducer=sum, integer=False, checkpoint=lambda: None):
    """A total needs every observation; missing phases stay unknown."""
    if not values:
        return None
    for value in values:
        checkpoint()
        if not _nonnegative(value, integer=integer):
            return None
    result = reducer(values)
    return result if _nonnegative(result, integer=integer) else None


def _recent_runs(c, days: int = 14, *, now=None, checkpoint=lambda: None) -> dict:
    """Aggregate measured phases over the semantic drain runs of the last
    `days` days (capacity gate: llm_s per window + per-job llm_s p90)."""
    from stats_math import percentile
    rows = c.execute(
        "SELECT content FROM artifacts WHERE kind=? "
        "AND created_at >= ? ORDER BY artifact_id DESC",
        (KIND_DRAIN_RUN, (time.time() if now is None else now) - days * 86400))
    recent = {"runs": 0, "done": 0, "deferred": 0, "failed": 0}
    phases = {key: [] for key in ("llm_s", "jev_s", "post_s", "queue_wait_s")}
    tokens = []
    for (content,) in rows:
        checkpoint()
        run = loads_dict(content)
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
            checkpoint()
            metric = metric if isinstance(metric, dict) else {}
            for key, values in phases.items():
                values.append(metric.get(key))
            usage = metric.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            tokens.append(_complete_total(
                [usage.get("input_tokens"), usage.get("output_tokens")],
                integer=True) if type(usage.get("unreported_requests")) is int
                and usage["unreported_requests"] == 0 else None)
    checkpoint()
    recent.update({key: _complete_total(phases[key], checkpoint=checkpoint)
                   for key in ("llm_s", "jev_s", "post_s")})
    recent["queue_wait_s_max"] = _complete_total(phases["queue_wait_s"], reducer=max,
                                                checkpoint=checkpoint)
    # same completeness rule as the total: any job without a valid llm_s
    # leaves both gate inputs unknown instead of a p90 over a subset
    recent["llm_s_p90"] = (None if recent["llm_s"] is None
                           else percentile(phases["llm_s"], 0.90))
    checkpoint()
    recent["usage_tokens"] = _complete_total(tokens, integer=True, checkpoint=checkpoint)
    return recent


def _extract_recent(c, *, checkpoint=lambda: None) -> dict:
    """Aggregate independently measured extraction metrics over 200 rows."""
    rows = c.execute(
        "SELECT meta FROM artifacts WHERE kind='extract_llm' "
        "ORDER BY artifact_id DESC LIMIT 200")
    recent = {"artifacts": 0}
    values = {key: [] for key in ("calls", "prompt_ms", "predicted_ms", "tokens")}
    for (raw,) in rows:
        checkpoint()
        meta = loads_dict(raw)
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


def _incomplete(snap) -> bool:
    return (snap.get("status") == "unavailable"
            or snap.get("observation", {}).get("status") == "partial")


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
        return 2 if _incomplete(snap) else 0
    if snap.get("status") == "unavailable":
        print(f"observation: unavailable ({snap['reason']})")
        return 2
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
          f"legacy extraction predicate pending: {snap['extract_llm_left']} "
          "(admission/lease/retry not evaluated)")
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
    pipeline = snap["pipeline_progress"]
    if pipeline["status"] == "recorded":
        for name, title in (("arrival", "新着"), ("backfill", "履歴"),
                            ("qc", "抽出品質チェック"), ("unclassified", "分類不明")):
            slot = pipeline["queues"][name]
            rate, estimate = slot["throughput"], slot["estimate"]
            speed = (f"{rate['per_hour']:.1f}件/時（観測{rate['window_s']:.0f}秒）"
                     if rate["status"] == "recorded" else f"算出不能: {rate['reason']}")
            eta = (f"{estimate['seconds'] / 3600:.1f}時間（追加なし・条件不変の場合）"
                   if estimate["status"] == "conditional" else f"算出不能: {estimate['reason']}")
            print(f"{title}: 待ち{slot['pending']}件、失敗{slot['failed']}件、"
                  f"再試行時刻待ち{slot['retry_wait']}件 | 速度 {speed} | 見通し {eta}")
    else:
        print(f"工程別の進捗: 未確認（{pipeline['reason']}）")
    collection = snap["collection_progress"]
    print(f"履歴収集: 記録上の遅延{collection['lagging']}件 / {collection['rooms']}室 "
          f"（理由内訳: {collection['by_reason']}）")
    # a partial snapshot printed its parts but is not a complete observation
    return 2 if _incomplete(snap) else 0


if __name__ == "__main__":
    sys.exit(main())
