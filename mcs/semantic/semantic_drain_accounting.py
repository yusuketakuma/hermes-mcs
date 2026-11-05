"""Persist semantic drain outcomes and per-job accounting."""
from __future__ import annotations
import semantic_transport
import json
import math
import time
import semantic_runtime as runtime
from semantic_policy import (KIND_DRAIN_RUN, QC_JOB_KIND)

from semantic_due_selection import (
    _merge_long_defers,
)
from semantic_job_processor import (
    NOT_SENT_BACKOFF_S,
    _jev_error_brief,
)
from semantic_scheduler_state import (
    _sched_update,
)

def _job_metric(job, token, cohort: str, status, job_age: float,
                job_elapsed: float, llm_s: float, jev_s: float, used: int,
                usage: dict, jev_client, *, diagnostics: dict) -> dict:
    """The bounded per-job metrics row of one drain pass."""
    return {
        "job_id": job["job_id"], "project_id": job["project_id"],
        "kind": job["kind"],
        "cohort": "qc" if job["kind"] == QC_JOB_KIND else cohort,
        "generation": token.generation,
        "payload_digest": token.payload_digest, "attempts": token.attempts,
        "status": status if status is not None else "error",
        # phase split — queue wait vs model call vs Jev evaluation
        # vs the residual validate/render phase (T14)
        "queue_wait_s": job_age,
        "llm_s": llm_s,
        "jev_s": jev_s,
        "post_s": max(0.0, job_elapsed - llm_s - jev_s),
        "elapsed_s": job_elapsed, "job_age_s": job_age,
        "jev_requests": used, "usage": usage,
        # last Jev error class/kind of this pass — a job that ends
        # "failed" after a few Jev calls was previously
        # undiagnosable from the run record (2026-09-30)
        "jev_error": _jev_error_brief(
            getattr(jev_client, "last_error", None))
        if status not in (None, "done") else None,
        # last local-model failure code of this pass (never
        # text) — a long call ending "retry" was undiagnosable
        # from the run record (2026-10-09). Module-level and
        # latest-wins: only a pass that actually called the
        # model may report it, or a cached/QC pass would wear
        # the previous job's code.
        "llm_error": semantic_transport.last_llm_failure()
        if status not in (None, "done") and llm_s > 0 else None,
        "retry_reason": diagnostics["code"]
        if status != "done" else None,
        "summary_validation_error": diagnostics["summary_validation_error"],
        "loops_rejected": diagnostics["loops_rejected"],
    }


def _defer_note(out: dict, reason: str) -> None:
    """Count a prepare-phase defer so its reason reaches defer_reasons."""
    out["deferred"] += 1
    counts = out.setdefault("defer_reasons", {})
    counts[reason] = counts.get(reason, 0) + 1


def _apply_job_status(ledger, token, limit: int, status: str,
                      budget_short: bool, out: dict) -> bool:
    """Map one finished job's status onto its queue transition and the
    pass counters; True when the pass must stop (no further call fits)."""
    if status == "done":
        out["done"] += 1
    elif status in ("deferred", "deferred_short"):
        out["deferred"] += 1
        runtime.transition(ledger, token, "defer", retry_in=60)
        if status == "deferred_short":
            # less than one call reserve is left: the next job's
            # first call is exempt from the reserve gate and would
            # be dispatched only to time out at the deadline
            return True
    elif status == "deferred_backoff":
        out["deferred"] += 1
        runtime.transition(ledger, token, "defer",
                           retry_in=NOT_SENT_BACKOFF_S)
    elif status == "retry":
        runtime.transition(ledger, token, "retry", retry_in=300,
                           max_attempts=limit)
        out["deferred"] += 1
        if budget_short:
            return True  # same reason as deferred_short: no call fits
    elif status == "failed":
        runtime.transition(ledger, token, "retry", max_attempts=1)
        out["failed"] += 1
    elif status == "stale":
        # The worker that observed this row no longer owns it.  Do not
        # defer/retry/done the replacement generation by ID alone.
        out["deferred"] += 1
    else:
        out["failed"] += 1
    return False


def _observation(started_at: float, finished_at: float, generation,
                 lane, metrics: list) -> dict:
    """The wall-clock window of one drain pass; ``complete`` only when
    every processed job fits the bounded artifact and the clock ran
    forward across the pass and each completion."""
    stamps = [started_at, *(m["completed_at"] for m in metrics
                            if "completed_at" in m), finished_at]
    complete = (len(metrics) <= 64
                and all(math.isfinite(t) for t in stamps)
                and all(a <= b for a, b in zip(stamps, stamps[1:])))
    return {"version": 1, "started_at": started_at, "finished_at": finished_at,
            "config_generation": generation, "complete": complete, "lane": lane}


def _persist_drain_accounting(ledger, scfg, out: dict, result: dict,
                              selected: dict, qc_active: bool,
                              sched_mut: dict, long_seen: dict | None = None,
                              long_defers: dict | None = None) -> None:
    """Persist the fairness counters and the bounded drain-run metrics
    artifact; either failure is recorded in ``result`` and never breaks
    the drain. This pass's long-output defers (``long_seen``) are folded
    into the persisted map alongside the fairness counters."""
    # persisted fairness accounting — survives restarts, feeds
    # semantic_observe's scheduler section (T14)
    try:
        with _sched_update(ledger) as sched:
            for cohort in ("arrival", "backfill"):
                sched[cohort + "_selected"] = \
                    sched.get(cohort + "_selected", 0) + selected[cohort]
            if qc_active:
                sched["qc_selected"] = \
                    sched.get("qc_selected", 0) + selected["qc"]
            if selected["backfill"]:
                sched["backfill_last_served_at"] = time.time()
            if selected["qc"]:
                sched["qc_last_served_at"] = time.time()
            if sched_mut.get("turn") and sched_mut.get("turn_consumed"):
                sched[sched_mut.get("turn_key", "turn")] = sched_mut["turn"]
            sched.update(sched_mut.get("consumed_turns", {}))
            if sched_mut.get('hold_cursor_changed'):
                cursors = dict(sched.get('payload_hold_cursors', {}))
                key = sched_mut['hold_cursor_lane']
                cursor = sched_mut.get('hold_cursor')
                if cursors.get(key) == sched_mut.get('hold_cursor_expected'):
                    if cursor is None:
                        cursors.pop(key, None)
                    else:
                        cursors[key] = cursor
                    if cursors:
                        sched['payload_hold_cursors'] = cursors
                    else:
                        sched.pop('payload_hold_cursors', None)
            if long_seen or long_defers:
                _merge_long_defers(ledger, sched, long_seen or {})
    except Exception:
        # accounting must never break the drain itself
        result["errors"].append("semantic: sched_persist_failed")
    # Bounded per-run metrics artifact — observe() aggregates the
    # recent ones; a drain that attempted work leaves durable
    # evidence instead of an in-memory-only result (T14).
    try:
        ledger.artifact_add(
            KIND_DRAIN_RUN,
            json.dumps({
                "v": 1, "mode": scfg["mode"],
                "done": out["done"], "deferred": out["deferred"],
                "failed": out["failed"],
                "budget_exhausted": out["budget_exhausted"],
                "left": out["left"], "elapsed_s": out["elapsed_s"],
                "oldest_pending_age_s":
                    out["oldest_pending_job_age_s"],
                "lanes": out["lanes"],
                "job_metrics": out["job_metrics"][:64],
                "observation": out.get("observation")},
                ensure_ascii=False),
            model="semantic_drain")
    except Exception:
        result["errors"].append("semantic: metrics_persist_failed")
