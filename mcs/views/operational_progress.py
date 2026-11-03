"""Count scoped processing queues and recorded progress without exposing source rows."""
from __future__ import annotations

import json
import math
import sqlite3

from mcs_util import loads_dict
from semantic_policy import JOB_KIND, QC_JOB_KIND, KIND_DRAIN_RUN, runtime_config
from semantic_runtime import config_generation, payload_generation

RATE_WINDOW_S = 3600
RATE_FRESH_S = 600
ETA_MIN_WINDOW_S = 300
ETA_MIN_COMPLETIONS = 3
_STAGES = ("arrival", "backfill", "qc", "unclassified")
_MAX_JOB_PAYLOAD = 1024 * 1024  # same per-cell bound as the snapshot reader
_MAX_JOB_TEXT = 64 * 1024 * 1024


def _time(value):
    if type(value) not in (int, float):
        return None
    try:
        return value if value >= 0 and math.isfinite(value) else None
    except OverflowError:
        return None


def _cohort(kind, payload):
    if payload is None:
        return "unclassified"
    if kind == QC_JOB_KIND:
        return "qc"
    eligible = payload.get("eligible", False)
    return "arrival" if eligible is True or type(eligible) is int and eligible == 1 else "backfill"


def _queue():
    return {"unit": "jobs", "pending": 0, "failed": 0, "done": 0, "unknown": 0,
            "due_by_schedule": 0, "retry_wait": 0, "schedule_unknown": 0,
            "oldest_age_s": None, "oldest_enqueued_at": None,
            "oldest_acquired_at": None, "oldest_acquired_age_s": None,
            "acquisition_time_unknown": 0}


def _queue_counts(db, projects, now, checkpoint, payload_checkpoint):
    """Return at most four cohorts by four states, never retained source rows."""
    params = {"semantic": JOB_KIND, "qc": QC_JOB_KIND, "now": now,
              "projects": None if projects is None else json.dumps(projects),
              "max_payload": _MAX_JOB_PAYLOAD}
    scope = ("fetch_jobs.kind IN (:semantic,:qc) AND (:projects IS NULL OR fetch_jobs.project_id IN "
             "(SELECT value FROM json_each(:projects)))")
    # Preserve the observer's per-cell limit before a Python callback parses
    # payloads. The bounded offending sample is never included in the report.
    if db.execute(f"SELECT substr(payload,1,:max_payload+1) FROM fetch_jobs "
                  f"WHERE {scope} AND length(payload)>:max_payload LIMIT 1",
                  params).fetchone() is not None:
        return None
    oversized = False
    payload_text = 0
    callback_error = None

    def classify(kind, raw):
        nonlocal oversized, payload_text, callback_error
        try:
            # Charge payload text against the caller's shared byte budget,
            # without counting every historical row as a returned item.
            payload_checkpoint(raw)
        except Exception as exc:
            callback_error = exc
            raise
        # SQLite length(TEXT) stops at NUL; Python's length does not.
        if isinstance(raw, (str, bytes)):
            payload_text += len(raw)
            if len(raw) > _MAX_JOB_PAYLOAD or payload_text > _MAX_JOB_TEXT:
                oversized = True
                raise ValueError("progress_payload_bound")
        # Python accepts NaN and last-wins duplicate keys; SQLite JSON has
        # different semantics. Keep exactly the existing loads_dict boundary.
        return _cohort(kind, loads_dict(raw))

    def numeric(column):
        return (f"typeof({column}) IN ('integer','real') AND {column}>=0 "
                f"AND {column}<=1.7976931348623157e308")

    queues = {stage: _queue() for stage in _STAGES}
    db.create_function("_mcs_progress_cohort", 2, classify, deterministic=True)
    try:
        for row in db.execute(f"""
            SELECT _mcs_progress_cohort(kind,payload) AS cohort,
              CASE WHEN state IN ('pending','failed','done') THEN state
                   ELSE 'unknown' END AS status, COUNT(*) AS count,
              SUM(CASE WHEN state='pending' AND {numeric('next_try')}
                       AND next_try<=:now THEN 1 ELSE 0 END) AS due,
              SUM(CASE WHEN state='pending' AND {numeric('next_try')}
                       AND next_try>:now THEN 1 ELSE 0 END) AS waiting,
              MIN(CASE WHEN state='pending' AND {numeric('created_at')}
                       AND created_at<=:now THEN created_at END) AS oldest,
              MIN(CASE WHEN state='pending' AND {numeric('m.first_seen')}
                       AND m.first_seen<=:now THEN m.first_seen END) AS acquired,
              SUM(CASE WHEN state='pending' AND {numeric('m.first_seen')}
                       AND m.first_seen<=:now THEN 1 ELSE 0 END) AS acquired_known
            FROM fetch_jobs LEFT JOIN messages m ON fetch_jobs.state='pending'
              AND m.message_id=fetch_jobs.message_id AND m.project_id=fetch_jobs.project_id
            WHERE {scope} GROUP BY cohort,status
        """, params):
            checkpoint()
            slot = queues[row["cohort"]]
            slot[row["status"]] = row["count"]
            if row["status"] == "pending":
                slot["due_by_schedule"] = row["due"]
                slot["retry_wait"] = row["waiting"]
                slot["schedule_unknown"] = row["count"] - row["due"] - row["waiting"]
                slot["oldest_enqueued_at"] = row["oldest"]
                slot["oldest_age_s"] = None if row["oldest"] is None else now - row["oldest"]
                slot["oldest_acquired_at"] = row["acquired"]
                slot["oldest_acquired_age_s"] = None if row["acquired"] is None else now - row["acquired"]
                slot["acquisition_time_unknown"] = row["count"] - row["acquired_known"]
    except sqlite3.Error:
        if callback_error is not None:
            raise callback_error from None
        if oversized:
            return None
        raise
    finally:
        db.create_function("_mcs_progress_cohort", 2, None)
    return None if oversized else queues


def _metric_bindings(db, metrics, projects, checkpoint):
    """Read only this bounded run's metric IDs, not all retained jobs."""
    ids = {m["job_id"] for m in metrics if isinstance(m, dict)
           and m.get("status") == "done" and type(m.get("job_id")) is int
           and -(2**63) <= m["job_id"] < 2**63}
    if not ids:
        return {}
    bindings = {}
    for row in db.execute(
            "SELECT job_id,project_id,kind,state,payload FROM fetch_jobs "
            "WHERE kind IN (?,?) AND job_id IN (" + ",".join("?" * len(ids)) + ")",
            (JOB_KIND, QC_JOB_KIND, *sorted(ids))):
        checkpoint()
        if projects is not None and row["project_id"] not in projects:
            continue
        payload = loads_dict(row["payload"])
        if payload is not None:
            try:
                generation = payload_generation(payload)
            except (TypeError, ValueError, RecursionError):
                # A legacy payload with nonfinite JSON values has no stable
                # canonical generation and cannot verify a completion.
                continue
            bindings[(row["job_id"], generation)] = (
                row["kind"], _cohort(row["kind"], payload), row["state"], row["project_id"])
    return bindings


def pipeline_progress(db, cfg, *, now, checkpoint=lambda: None,
                      payload_checkpoint=lambda raw: None):
    """Read within the caller's bounded snapshot; scheduled due is not admission."""
    if _time(now) is None:
        raise ValueError("observation_time_invalid")
    if not isinstance(cfg, dict):
        return {"status": "unavailable", "reason": "config_not_supplied", "observed_at": now}
    policy, errors = runtime_config(cfg)
    if errors:
        return {"status": "unavailable", "reason": "config_invalid", "observed_at": now}
    projects = policy["project_ids"]
    queues = _queue_counts(db, projects, now, checkpoint, payload_checkpoint)
    if queues is None:
        return {"status": "unavailable", "reason": "observation_size_exceeded", "observed_at": now}
    rates = _rates(db, cfg, projects, now, checkpoint)
    enabled = policy["mode"] != "off"
    for cohort, slot in queues.items():
        rate = rates[cohort]
        slot["throughput"] = rate
        reason = ("processing_disabled" if not enabled else
                  "new_arrivals_not_forecast" if cohort == "arrival" else
                  "unclassified_jobs" if cohort == "unclassified" else
                  "queue_requires_attention" if slot["failed"] or slot["unknown"]
                  or slot["retry_wait"] or slot["schedule_unknown"] else
                  rate.get("reason") if rate["status"] != "recorded" else
                  "insufficient_samples" if rate["completed"] < ETA_MIN_COMPLETIONS
                  or rate["window_s"] < ETA_MIN_WINDOW_S else
                  "no_completions" if rate["per_hour"] <= 0 else None)
        slot["estimate"] = {
            "status": "unavailable" if reason else "conditional",
            "reason": reason,
            "seconds": None if reason else slot["pending"] * 3600 / rate["per_hour"],
            "assumptions": ["no_new_jobs", "unchanged_config_and_capacity",
                            "recorded_completion_rate_continues"],
        }
    return {"status": "recorded", "observed_at": now, "enabled": enabled,
            "scope": "stored_jobs_in_configured_projects", "queues": queues,
            "schedule_basis": "next_try_only_not_admission_or_lease",
            "queue_age_basis": "fetch_jobs.created_at",
            "acquisition_age_basis": "messages.first_seen_not_posted_at",
            "completion_basis": "recorded_current_job_completion_not_clinical_or_delivery",
            "last_scheduler_at": rates["arrival"]["latest_scheduler_at"],
            "scheduler_basis": "recorded_current_config_drain_pass_not_completion",
            "completion_observation_window_s": RATE_WINDOW_S,
            "slo": {"status": "target_not_defined", "target_s": None},
            "result_validity": {"status": "not_evaluated",
                "basis": "current_terminal_summary_audit_pair_not_clinical_accuracy"}}


def _rates(db, cfg, projects, now, checkpoint):
    expected = config_generation(cfg)
    first = last = None
    completed = {stage: set() for stage in _STAGES}
    latest_completed = {stage: None for stage in _STAGES}
    latest_scheduler = None
    incomplete = False
    seen_records = False
    obsolete = 0
    for row in db.execute(
            "SELECT content FROM artifacts WHERE kind=? AND created_at>=? "
            "AND created_at<=? ORDER BY created_at DESC,artifact_id DESC",
            (KIND_DRAIN_RUN, now - RATE_WINDOW_S, now)):
        checkpoint()
        seen_records = True
        record = loads_dict(row["content"])
        obs = record.get("observation") if record else None
        metrics = record.get("job_metrics") if record else None
        if (not isinstance(obs, dict) or type(obs.get("version")) is not int
                or obs["version"] != 1):
            incomplete = True
            continue
        if obs.get("config_generation") != expected:
            # A -> B -> A consists of separate observation periods. Older
            # matching records do not describe the current continuous period.
            break
        start, end = _time(obs.get("started_at")), _time(obs.get("finished_at"))
        if start is not None and end is not None and start < end <= now:
            latest_scheduler = max(latest_scheduler or end, end)
        if (start is None or end is None or not start < end <= now
                or obs.get("complete") is not True or not isinstance(metrics, list)
                or len(metrics) > 64):
            incomplete = True
            continue
        if start < now - RATE_WINDOW_S:
            # A valid boundary-crossing run is outside this complete-window
            # sample; it is not evidence that the in-window records are corrupt.
            continue
        first = start if first is None else min(first, start)
        last = end if last is None else max(last, end)
        bindings = _metric_bindings(db, metrics, projects, checkpoint)
        for metric in metrics:
            checkpoint()
            if not isinstance(metric, dict):
                incomplete = True
                continue
            if metric.get("status") != "done":
                continue
            at = _time(metric.get("completed_at"))
            job, generation = metric.get("job_id"), metric.get("generation")
            if (type(job) is not int or not isinstance(generation, str) or not generation
                    or at is None or not start <= at <= end):
                incomplete = True
                continue
            key = job, generation
            binding = bindings.get(key)
            if (binding is None or binding[2] != "done"
                    or metric.get("kind") != binding[0]
                    or metric.get("cohort") != binding[1]
                    or metric.get("project_id") != binding[3]):
                obsolete += 1
                continue
            completed[binding[1]].add(key)
            latest_completed[binding[1]] = max(latest_completed[binding[1]] or at, at)
    reason = ("incomplete_or_legacy_metrics" if incomplete else
              "no_recorded_window" if not seen_records else
              "no_matching_generation" if first is None else
              "stale_metrics" if now - last > RATE_FRESH_S else None)
    window = now - first if first is not None else None
    return {stage: {
        "status": "unavailable" if reason else "recorded", "reason": reason,
        "unit": "distinct_completed_jobs", "binding": "current_job_generation_and_config",
        "completed": None if reason else len(keys),
        "window_started_at": first, "window_finished_at": now,
        "latest_record_at": last, "latest_scheduler_at": latest_scheduler,
        "latest_completed_at": latest_completed[stage], "window_s": window,
        "per_hour": None if reason else len(keys) * 3600 / window,
        "excluded_obsolete_or_out_of_scope": obsolete,
    } for stage, keys in completed.items()}


def collection_progress(db, *, now, checkpoint=lambda: None):
    """Count recorded coverage gaps; no gap does not prove complete history."""
    from health_policy import COVERAGE_STALL_S
    if _time(now) is None:
        raise ValueError("observation_time_invalid")
    counts = {}
    rooms = lagging = 0
    max_lag = None
    for row in db.execute("""
        SELECT p.coverage_ts,
          (SELECT MAX(m.posted_at_ts) FROM messages m WHERE m.project_id=p.project_id) newest,
          j.state,j.updated_at,j.next_try
        FROM patients p LEFT JOIN fetch_jobs j ON j.project_id=p.project_id
          AND j.kind='history_head' AND j.message_id=0
        WHERE COALESCE(p.is_archived,0)=0
    """):
        checkpoint()
        rooms += 1
        coverage, newest = _time(row["coverage_ts"]), _time(row["newest"])
        if newest is None or newest > now or coverage is None or coverage > now:
            reason = "coverage_unobserved"
        elif newest <= coverage:
            reason = "no_recorded_gap"
        else:
            lagging += 1
            lag = newest - coverage
            max_lag = max(max_lag or 0, lag)
            updated, due = _time(row["updated_at"]), _time(row["next_try"])
            reason = ("history_failed" if row["state"] == "failed" else
                      "history_job_missing" if row["state"] is None else
                      "history_state_unknown" if row["state"] != "pending" else
                      "history_checkpoint_unknown" if updated is None or updated > now else
                      "history_not_advancing" if now - updated > COVERAGE_STALL_S else
                      "history_schedule_unknown" if due is None else
                      "history_retry_wait" if due > now else "history_recent_checkpoint")
        counts[reason] = counts.get(reason, 0) + 1
    return {"status": "recorded", "observed_at": now, "scope": "active_stored_projects",
            "rooms": rooms, "lagging": lagging, "max_lag_s": max_lag,
            "by_reason": counts, "basis": "stored_newest_minus_verified_coverage"}
