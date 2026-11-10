"""Semantic drain engine: the durable-job worker (spec §14, §18).

Claims jobs, runs staged extraction/summary/audit, wires the Jev budget
and circuit, records plans, and emits notices. Transport, source bundles
and audit decisions are consumed from their defining modules. This worker
owns claims, guarded stage commits and job outcomes."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401  registers every subdir as import root
from inference_version import CURRENT_VERSION as CURRENT_VERSION
from contextlib import ExitStack as ExitStack
from semantic_policy import KIND_ASSESS as KIND_ASSESS
from semantic_policy import KIND_AUDIT as KIND_AUDIT
from semantic_policy import KIND_BUNDLE as KIND_BUNDLE
from semantic_policy import KIND_CANDIDATE as KIND_CANDIDATE
from semantic_policy import KIND_COVERAGE as KIND_COVERAGE
from semantic_policy import KIND_DRAIN_RUN as KIND_DRAIN_RUN
from semantic_policy import KIND_FACTS as KIND_FACTS
from semantic_policy import KIND_FACTS_V2 as KIND_FACTS_V2
from semantic_policy import KIND_FACT_AUDIT as KIND_FACT_AUDIT
from semantic_policy import KIND_FACT_PROJ as KIND_FACT_PROJ
from semantic_policy import KIND_FACT_REPAIR as KIND_FACT_REPAIR
from semantic_policy import KIND_LOOP as KIND_LOOP
from semantic_policy import KIND_LOOP_EVENT as KIND_LOOP_EVENT
from semantic_policy import KIND_PLAN as KIND_PLAN
from semantic_policy import KIND_REPAIR as KIND_REPAIR
from semantic_policy import KIND_SHADOW_V2 as KIND_SHADOW_V2
from semantic_policy import KIND_SUMMARY as KIND_SUMMARY
from semantic_policy import KIND_SUMMARY_FORMAT_REPAIR as KIND_SUMMARY_FORMAT_REPAIR
from inference_version import LEGACY_VERSION as LEGACY_VERSION
from semantic_policy import SCHED_KIND as SCHED_KIND
from semantic_policy import SCHEMA_VERSION as SCHEMA_VERSION
from inference_version import VERSION_KEY as VERSION_KEY
from semantic_audit import audit_code as audit_code
from semantic_audit import audit_status_for as audit_status_for
import semantic_conversion as cohort  # noqa: F401 - retained facade module alias
from contextlib import contextmanager as contextmanager
import copy as copy
from semantic_store import current_artifact as current_artifact
from inference_version import current_meta_pred as current_meta_pred
import hashlib as hashlib
import heapq as heapq
from semantic_store import jev_state as jev_state
import mcs_util as mcs_util
from contextlib import nullcontext as nullcontext
import patient_priority as patient_priority
from inference_version import read_version as read_version
import semantic_audit as semantic_audit
import semantic_extraction as semantic_extraction
import semantic_facts as semantic_facts
import semantic_render as semantic_render
from mcs_util import strict_json as strict_json
from semantic_llm import summarize as summarize
import threading as threading
from semantic_loops import update_loops as update_loops
import writer_lock
import llm_availability  # noqa: E402
import semantic_store
import semantic_transport
from semantic_summary_stages import (
    _shadow_v2_generation as _shadow_v2_generation,
    _shadow_v2_tried as _shadow_v2_tried,
    _jev_fail_flag as _jev_fail_flag,
    _job_diagnostics as _job_diagnostics,
    _why as _why,
    _summary_provenance as _summary_provenance,
    _summary_producer as _summary_producer,
    _summarize_observed as _summarize_observed,
    _claim_input_binding as _claim_input_binding,
    _summary_repair_pending as _summary_repair_pending,
    _summary_repair_evidence as _summary_repair_evidence,
    _empty_repair_prior as _empty_repair_prior,
    _empty_repair_candidate as _empty_repair_candidate,
    _store_empty_repair as _store_empty_repair,
    _reused_pass_current as _reused_pass_current,
    _prior_pending_findings as _prior_pending_findings,
    _write_result as _write_result,
    _park_needs_review as _park_needs_review,
)
from semantic_fact_stages import (
    _fact_stage as _fact_stage,
    _fact_v2_document as _fact_v2_document,
    _fact_canonical_audit as _fact_canonical_audit,
    _fact_legacy as _fact_legacy,
    _prefiltered as _prefiltered,
    proposition_quality_report as proposition_quality_report,
)
from semantic_job_processor import (
    _assess_target as _assess_target,
    _obtain_summary as _obtain_summary,
    _promote_loops as _promote_loops,
    _process_job_inner as _process_job_inner,
    _process_job as _process_job,
    _jev_error_brief as _jev_error_brief,
    _last_stage_artifact as _last_stage_artifact,
    _timed_llm as _timed_llm,
    NOT_SENT_BACKOFF_S as NOT_SENT_BACKOFF_S,
    _LLM_DOWN_REASONS as _LLM_DOWN_REASONS,
    _STAGE_KINDS as _STAGE_KINDS,
)
from semantic_scheduler_state import (
    _sched_state as _sched_state,
    _sched_write as _sched_write,
    _sched_update as _sched_update,
    _bounded_job_payload as _bounded_job_payload,
    _reservation_sched as _reservation_sched,
    _held_cursor_valid as _held_cursor_valid,
)
from semantic_reprocess import (
    _reprocess_patient_state as _reprocess_patient_state,
    _reprocess_proof_generation as _reprocess_proof_generation,
    _reprocess_proof_time_valid as _reprocess_proof_time_valid,
    _reprocess_bookmark_sealed as _reprocess_bookmark_sealed,
    reprocess_step as reprocess_step,
    reprocess_job as reprocess_job,
    _REPROCESS_PROOF_CONTRACT as _REPROCESS_PROOF_CONTRACT,
    _REALTIME_ORDER as _REALTIME_ORDER,
    _REALTIME_KEY as _REALTIME_KEY,
    LONG_FREE_DEFER_CAP as LONG_FREE_DEFER_CAP,
    _LONG_DEFERS_MAX as _LONG_DEFERS_MAX,
    _LONG_DEFERS_SCAN_BYTES as _LONG_DEFERS_SCAN_BYTES,
)
from semantic_due_selection import (
    _long_defers as _long_defers,
    _known_long_defer_generation as _known_long_defer_generation,
    _reserve_long_defer as _reserve_long_defer,
    _refund_long_defer as _refund_long_defer,
    _merge_long_defers as _merge_long_defers,
    _held_cursor_for as _held_cursor_for,
    _class_scope as _class_scope,
    _adopt_pending_arrival as _adopt_pending_arrival,
    _due_lanes as _due_lanes,
    _backlog_commit_reserve as _backlog_commit_reserve,
    _SchedulerUnavailable as _SchedulerUnavailable,
    _job_lock as _job_lock,
    _select_due as _select_due,
    _select_due_class as _select_due_class,
    _CloudSelectionIncomplete as _CloudSelectionIncomplete,
    _cloud_selection_identity as _cloud_selection_identity,
    _cloud_selection_memory_guard as _cloud_selection_memory_guard,
    _select_cloud_due as _select_cloud_due,
    _cloud_selection_page as _cloud_selection_page,
    _refill_due as _refill_due,
    _ARCHIVED_JOB as _ARCHIVED_JOB,
    _ELIGIBLE_SQL as _ELIGIBLE_SQL,
    _MALFORMED_JOB_SQL as _MALFORMED_JOB_SQL,
    BACKLOG_COMMIT_RESERVE_S as BACKLOG_COMMIT_RESERVE_S,
    _LONG_START_S as _LONG_START_S,
    _BACKLOG_TURN as _BACKLOG_TURN,
    _JOB_LOCK_BUCKETS as _JOB_LOCK_BUCKETS,
    _cloud_selection_lock as _cloud_selection_lock,
    _cloud_selection_progress as _cloud_selection_progress,
    _CLOUD_SELECTION_PAGE as _CLOUD_SELECTION_PAGE,
)
from semantic_drain_accounting import (
    _job_metric as _job_metric,
    _defer_note as _defer_note,
    _apply_job_status as _apply_job_status,
    _observation as _observation,
    _persist_drain_accounting as _persist_drain_accounting,
)

from functools import wraps, partial
import json
import math
import sqlite3
import time

from mcs_util import load_config, loads_dict
from inference_version import (is_current)
import semantic_jev as jev
import semantic_runtime as runtime
import semantic_publication as v4
from semantic_policy import (BACKLOG_BUDGET_RESERVE, JOB_KIND, KIND_POLICY, KIND_USAGE,
                             QC_JOB_KIND, policy_fingerprint, runtime_config)
import semantic_notice
from semantic_store import jev_usage_today

import semantic_qc

KIND_LOOP_COMPLETION = semantic_store.KIND_LOOP_COMPLETION

_partial_assessment = semantic_store._partial_assessment
_verified_prefilter = semantic_store._verified_prefilter
_valid_primary_assessment = semantic_store._valid_primary_assessment
_primary_assessment_history = semantic_store._primary_assessment_history
_valid_primary_history = semantic_store._valid_primary_history
_reprocess_job_payload = semantic_store._reprocess_job_payload
_valid_detail_assessment = semantic_store._valid_detail_assessment
_loop_skip_ref = semantic_store._loop_skip_ref
_loop_completion_manifest = semantic_store._loop_completion_manifest
_classification_only_state = semantic_store._classification_only_state
_loop_only_state = semantic_store._loop_only_state
_reprocess_target_state = semantic_store.reprocess_target_state


# LLMNotSent reasons that observe the endpoint itself down (refused
# connection / 503 while the model loads) — never an admission verdict
# or the long-output budget gate, which say nothing about its health.


# guard stages raised before a local-model request leaves (runtime)

# Artifact kinds a semantic pass persists as durable stage results.
# Scoped (kind + project) so a concurrent drainer's extract_llm rows or
# another project's work never read as this job's progress (2026-09-30:
# a job looped 19 passes 'progressing' on foreign writes).


_REPROCESS_JOB_MAX_BYTES = semantic_store._REPROCESS_JOB_MAX_BYTES


# Free long-output defers (the call needs LONG_MIN_CALL_S and the pass
# had less) are tracked per job generation in the sched row: such a job
# leaves the realtime lane, and after this many free defers of the same
# generation the next one charges a normal attempt so a budget that can
# never fit the call ends in a visible failure instead of looping (M6).


# an arrival-seeded job: a real notification event spawned it
# Malformed rows have no usable version/arrival metadata. Select these only
# for the bounded raw-CAS quarantine before any admission or model call.


_BACKLOG_INVALIDATE_S = 300.0
# Per-pass cap for the inference reprocess keyset walk: it is resumable by
# cursor, so a short slice each drain still advances it without letting one
# pass consume the window the model jobs and the snapshot tail need.
REPROCESS_STEP_BUDGET_S = 5.0
# Single-job repair outcomes that route the row to the bounded held cursor
# instead of the normal token/model path. Guard-owned statuses (not_stale,
# paused, excluded, off, deadline) fall through to the existing guards.
_REPAIR_HOLD = frozenset({
    'unverified', 'held', 'intake_hold_unverified', 'invalid_source_epoch',
    'source_changed', 'error'})
# A backlog worker releases the run lock around every model/Jev call and
# must retake it before persisting the result. The tick (run_check) can
# hold that lock for its whole RUN_DEADLINE_S (480 s) after waiting
# LOCK_WAIT_S (150 s) itself; with the shared 150 s reacquire bound a call
# ending inside that window was abandoned (run_lock_lost), leaving the
# job untouched and exiting the resident (2026-10-09). Outwait a full
# tick instead — never past the pass deadline run_due was given.
RUN_LOCK_REACQUIRE_S = 660.0
# A tick can hold the run lock across its work window. Backlog work
# stops early enough for a result to wait out a tick and still be
# written before the job deadline.
# what a resumed pass may spend before its first (long-output) call


# kind this resident backlog worker served last (in-process; a restart
# simply starts with semantic again)
_invalidated_at: float | None = None


    # Keep the inode: unlinking a flock file can create two lock owners.


# One non-authoritative traversal per process. Only IDs and ordering metadata
# survive a tick; receipt, source and job data are always read again.


def run_due(ledger, cfg: dict, result: dict, deadline: float,
            jev_client=None, llm_fn=None, max_jobs: int = 4,
            cfg_path: str | None = None, run_lock_fd=None,
            lane: str | None = None, cloud_only: bool = False,
            prioritize_patients: bool = False) -> dict:
    """Drain due semantic jobs inside the tick's remaining budget.
    OFF returns immediately — no job creation, no external calls, no
    auto-drain (AT-053). The caller's shared run lock is held by
    run_check; a standalone CLI takes acquire_run_lock itself.
    cfg_path, when given, re-validates config between jobs so an OFF
    flip mid-run takes effect at the next job boundary (AT-060)."""
    started_at = time.perf_counter()
    started_wall = time.time()
    if lane not in (None, "realtime", "backlog"):
        raise ValueError("semantic_lane_invalid")
    if cloud_only and (lane != "backlog" or llm_fn is not None):
        raise ValueError("semantic_cloud_lane_invalid")
    if type(max_jobs) is not int or not 1 <= max_jobs <= 32:
        raise ValueError("semantic_max_jobs_invalid")
    scfg, errors = runtime_config(cfg)
    cfg_generation = runtime.config_generation(cfg)
    observation_config_changed = False
    for e in errors:
        if e not in result["errors"]:
            result["errors"].append(e)
    out = {"mode": scfg["mode"], "done": 0, "deferred": 0,
           "failed": 0, "left": None, "budget_exhausted": False,
           # jobs whose pass persisted at least one new stage artifact
           # (usage receipts excluded) — "no completion" is not "no
           # progress" for the tick's starvation guard (2026-09-30)
           "progressed": 0,
           # fixed-shape phase timings for the preamble maintenance calls
           "invalidate_s": None, "reprocess_s": None, "reproject_s": None}
    if scfg['mode'] != 'off':
        try:
            initial_sched, _ = _reservation_sched(ledger)
        except Exception:
            result['errors'].append('semantic: long_defer_reservation_failed')
            out['long_defer_reservation_failed'] = True
            return out
    global _invalidated_at
    # The scan is O(corpus) (thread_bundle per canonical thread) and the
    # backlog resident calls run_due once per job under the run lock, so
    # that lane refreshes at most every _BACKLOG_INVALIDATE_S; the tick
    # (_semantic_enabled + realtime lane) still runs it every pass.
    now_m = time.monotonic()
    if not cloud_only and (lane != "backlog" or _invalidated_at is None \
            or now_m - _invalidated_at >= _BACKLOG_INVALIDATE_S):
        from semantic_publication import invalidate_projections
        _phase_t = time.perf_counter()
        invalidate_projections(ledger.db, scfg)
        out["invalidate_s"] = round(time.perf_counter() - _phase_t, 3)
        _invalidated_at = time.monotonic()
    if scfg["mode"] == "off":
        return out
    if not cloud_only:
        _phase_t = time.perf_counter()
        try:
            out['inference_reprocess'] = reprocess_step(
                ledger, cfg,
                deadline=min(deadline,
                             time.monotonic() + REPROCESS_STEP_BUDGET_S))
        except Exception as error:
            out['inference_reprocess'] = {'status': 'error', 'complete': False}
            result['errors'].append(f'semantic_reprocess: {type(error).__name__}')
        out["reprocess_s"] = round(time.perf_counter() - _phase_t, 3)
    # Stability guards shared with the extract lane: a nearly-full
    # volume turns per-job artifact writes into an I/O error storm, and
    # an open endpoint breaker means every llm_fn call pays a dead
    # timeout — defer the whole window instead of churning jobs.
    free_mb = llm_availability.disk_free_mb(ledger.db)
    if free_mb is not None \
            and free_mb < llm_availability.disk_floor_mb():
        out["disk_free_mb"] = round(free_mb)
        return out
    # Deterministic maintenance (no model, no Jev — not gated by the
    # endpoint breaker or LLM admission): bring current projection/v4
    # rows minted by an older projection version up to date, a bounded
    # slice per tick, right after this tick's invalidation pass.
    if not cloud_only:
        _phase_t = time.perf_counter()
        try:
            out["reproject"] = v4.reproject_stale(ledger, scfg)
        except Exception as e:
            result["errors"].append(f"semantic_reproject: {type(e).__name__}")
        out["reproject_s"] = round(time.perf_counter() - _phase_t, 3)
    circuit_s = llm_availability.circuit_open_s(ledger.db)
    if circuit_s and not cloud_only:
        out["circuit_open_s"] = round(circuit_s)
        return out
    drain_started = time.perf_counter()
    # maintenance ahead of the job loop, and jobs skipped without a
    # transition — both invisible from the run record until 2026-10-09
    out["preamble_s"] = round(drain_started - started_at, 3)
    out["lock_skipped"] = {}
    out["floor_skipped"] = {}
    out["job_metrics"] = []
    oldest = ledger.db.execute(
        "SELECT MIN(created_at) FROM fetch_jobs "
        "WHERE kind IN (?,?) AND state='pending'",
        (JOB_KIND, QC_JOB_KIND)).fetchone()[0]
    out["oldest_pending_job_age_s"] = (
        max(0.0, time.time() - oldest) if oldest is not None else None)
    out["pending_by_kind"] = {
        r["kind"]: r["n"] for r in ledger.db.execute(
            "SELECT kind, COUNT(*) AS n FROM fetch_jobs "
            "WHERE kind IN (?,?) AND state='pending' GROUP BY kind",
            (JOB_KIND, QC_JOB_KIND)).fetchall()}
    out["done_by_kind"] = {}
    policy = policy_fingerprint(scfg)
    previous_policy = ledger.db.execute(
        "SELECT content FROM artifacts WHERE kind=? "
        "ORDER BY artifact_id DESC LIMIT 1", (KIND_POLICY,)).fetchone()
    if previous_policy is None or previous_policy["content"] != policy:
        ledger.artifact_add(KIND_POLICY, policy)
    cloud_backlog = llm_fn is None and lane == "backlog"
    if llm_fn is None:
        llm_fn = semantic_transport.llm_chat
    if jev_client is None and scfg["daily_request_budget"] > 0:
        jev_key = jev.api_key()
        jev_client = jev.JevClient(
            api_key=jev_key, key_current=lambda: bool(jev_key) and jev.api_key() == jev_key,
            model=scfg["model"],
            attempt_timeout=scfg["attempt_timeout_seconds"],
            job_budget=scfg["job_budget_seconds"],
            max_attempts=scfg["max_attempts_per_try"])
    from writer_lock import unlocked_transport
    original_llm = llm_fn
    # the tick keeps the shared default: it never has a tick to outwait
    reacquire_s = RUN_LOCK_REACQUIRE_S if lane == "backlog" else None
    # Log-only (never in the drain_run artifact, no job identity): seconds
    # in transport vs. waiting to retake the run lock, and the window a
    # lost reacquire had — llm_s includes both (2026-10-10: losses landed
    # inside a tick's hold with ~30 s of the pass left).
    transport = out["transport"] = {
        "llm_transport_s": 0.0, "llm_reacquire_s": 0.0,
        "jev_transport_s": 0.0, "jev_reacquire_s": 0.0}
    # Log-only totals belong to this pass output; only its explicit stage chain may add to them.
    shadow_stats = out["shadow_v2"] = {"calls": 0, "incomplete": 0, "s": 0.0}

    def _unlocked(kind, call, *args, **kwargs):
        started, ended = time.monotonic(), None
        try:
            with unlocked_transport(ledger.db, run_lock_fd, wait_s=reacquire_s,
                                    deadline=deadline):
                try:
                    return call(*args, **kwargs)
                finally:
                    ended = time.monotonic()
        except writer_lock.RunLockLost:
            if ended is not None:
                wait = writer_lock.RUN_LOCK_REACQUIRE_S if reacquire_s is None \
                    else reacquire_s
                out["run_lock_lost_detail"] = {
                    "kind": kind, "transport_s": round(ended - started, 1),
                    "reacquire_s": round(time.monotonic() - ended, 1),
                    "window_s": round(max(0.0, min(wait, deadline - ended)), 1)}
            raise
        finally:
            if ended is not None:
                # raw sums: rounding each call would drop short calls
                transport[f"{kind}_transport_s"] += ended - started
                transport[f"{kind}_reacquire_s"] += time.monotonic() - ended

    # Endpoint health shared with the extract lane's breaker: a refused
    # connection / 503 never consumes an attempt here (LLMNotSent -> free
    # defer, which also refreshes updated_at), so without this feed an
    # outage of any length stayed invisible to the breaker and the stall
    # alert. Counted once per pass; an answered call resets the streak.
    llm_health = {"down": False, "fed": False}
    marks_unsaved0 = semantic_transport.long_mark_failures()
    long_defers = _long_defers(ledger)

    @wraps(original_llm)
    def unlocked_llm(*args, **kwargs):
        try:
            text = _unlocked("llm", original_llm, *args, **kwargs)
        except runtime.LLMNotSent as e:
            if getattr(e, "stage", "") in _LLM_DOWN_REASONS:
                if not llm_health["fed"]:
                    llm_availability.circuit_failure(ledger.db)
                llm_health["down"] = llm_health["fed"] = True
            raise
        if isinstance(text, str):
            llm_availability.circuit_success(ledger.db)
            llm_health["down"] = False
        return text

    llm_fn = unlocked_llm
    def unlocked_jev_request(call, *args, **kwargs):
        return _unlocked("jev", call, *args, **kwargs)

    jev_transport = unlocked_jev_request if run_lock_fd is not None else None
    if not cloud_only and scfg["mode"] != "off" and scfg["extract_qc"] == "annotate":
        try:
            qc_cap = scfg["daily_request_budget"]
            if lane == "backlog":
                qc_cap -= math.ceil(qc_cap * BACKLOG_BUDGET_RESERVE)
            qc_skip_mode = (jev_client is None or
                            not getattr(jev_client, "api_key", True) or
                            runtime.circuit_open(ledger) or jev_usage_today(ledger) >= qc_cap)
            if qc_skip_mode:
                semantic_qc._qc_seed(ledger, time.time(), skip_mode=True)
            else:
                semantic_qc._qc_seed(ledger, time.time())
        except Exception:
            result["errors"].append("semantic: qc_seed_failed")
    qc_active = scfg["extract_qc"] == "annotate"
    kinds = (JOB_KIND, QC_JOB_KIND) if qc_active else (JOB_KIND,)
    # arrival-descended seeds outrank import/replay seeds: a deep
    # backfill must never starve a fresh notification's evaluation
    # (§19.1's existing-work-first ordering applied inside the queue
    # too). 'eligible' is set at seed time and survives payload merges,
    # so a merged arrival+history job keeps its priority.
    # T14: the backfill cohort additionally gets a guaranteed share of
    # every window — a continuous arrival stream cannot starve runnable
    # backlog (persisted accounting row keeps it restart-safe).
    # These shares apply within each patient class; archived patients
    # receive only capacity left by active patients.
    # QC dedicated frame: kind ordering puts semantic rows ahead of QC
    # in both cohorts. Long generations can consume the whole window
    # and starve QC without attempting it. QC gets a bounded share,
    # served FIRST
    # (each pass is one bounded chunked eval), expanding into whatever
    # the semantic lanes leave unused. A 1-job window cannot be split —
    # QC falls back to the persisted cohort alternation inside the
    # non-eligible lane.
    hold_lane = lane or 'combined'
    held_after = initial_sched.get('payload_hold_cursors', {}).get(hold_lane)
    def select_cloud(*args, **kwargs):
        with unlocked_transport(ledger.db, run_lock_fd, wait_s=reacquire_s, deadline=deadline):
            return _select_cloud_due(*args, **kwargs, deadline=deadline,
                                     isolated=run_lock_fd is not None)
    try:
        cursor_arg = {'held_after': held_after} if held_after is not None else {}
        selector = (select_cloud if cloud_only else
                    partial(_select_due, prioritize_patients=True) if prioritize_patients else _select_due)
        first_batch = selector(ledger, lane, kinds, qc_active, max_jobs, long_defers,
                                  **cursor_arg)
    except _CloudSelectionIncomplete as error:
        out['cloud_selection_incomplete'] = True
        out['cloud_selection_reason'] = str(error)
        return out
    except _SchedulerUnavailable:
        result['errors'].append('semantic: long_defer_reservation_failed')
        out['long_defer_reservation_failed'] = True
        return out
    due = first_batch[0]
    consumed_turns = {}
    sched_mut = {"consumed_turns": consumed_turns, 'hold_cursor_expected': held_after}
    saw_held = False
    repair_attempted = False

    def hold_job(job, mutation, reason='semantic: job_payload_held'):
        nonlocal saw_held
        saw_held = True
        out['payload_hold_count'] = out.get('payload_hold_count', 0) + 1
        reasons = out.setdefault('hold_reasons', [])
        if reason not in reasons:
            reasons.append(reason)
        if reason not in result['errors']:
            result['errors'].append(reason)
        cursor = _held_cursor_for(job, lane)
        if cursor is not None:
            sched_mut.update(hold_cursor_changed=True, hold_cursor_lane=hold_lane,
                             hold_cursor=cursor)
        if mutation.get('turn') and job['job_id'] == mutation.get('turn_job_id'):
            consumed_turns[mutation.get('turn_key', 'turn')] = mutation['turn']
    out["lanes"] = {"arrival": 0, "backfill": 0}
    if qc_active:
        out["lanes"]["qc"] = 0
    selected = {"qc": 0, "arrival": 0, "backfill": 0}
    for job, cohort_name, mutation in _refill_due(
            ledger, lane, kinds, qc_active, max_jobs, long_defers,
            first_batch, selected, out, deadline, consumed_turns, held_after,
            selector=selector if cloud_only or prioritize_patients else None):
        if llm_health["down"] and job["kind"] == JOB_KIND:
            # the endpoint refused this pass: the remaining model jobs
            # would each only re-defer (and rewrite updated_at); they
            # stay due for the next pass. QC rows need no model.
            continue
        with _job_lock(ledger, job["job_id"]) as held:
            if not held:
                out["lock_skipped"][job["kind"]] = \
                    out["lock_skipped"].get(job["kind"], 0) + 1
                continue
            if cloud_only:
                # Selection ran without the writer lock. Re-read the full job
                # and its live admission before any transition or model call.
                import cloud_policy
                import cloud_inference
                fresh = ledger.db.execute('SELECT * FROM fetch_jobs WHERE job_id=?',
                                          (job['job_id'],)).fetchone()
                live_cfg = load_config(cfg_path) if cfg_path is not None else load_config()
                live_settings = cloud_inference.resolve_cloud_settings(live_cfg)
                live_scfg, live_errors = runtime_config(live_cfg)
                project = job['project_id']
                if (time.monotonic() >= deadline or fresh is None or tuple(fresh) != tuple(job) or live_errors
                        or live_scfg['mode'] == 'off' or live_settings['mode'] != 'on'
                        or 'semantic_backlog' not in live_settings['routes']
                        or not cloud_policy.send_route("semantic_backlog")
                        or not cloud_policy.selected_key()
                        or live_settings['project_ids'] is not None and project not in live_settings['project_ids']
                        or project in live_settings['exclude_project_ids']
                        or live_scfg['project_ids'] is not None and project not in live_scfg['project_ids']
                        or not cloud_policy.history_target_ready(ledger.db, project, job['message_id'])
                        or not mutation.get('cloud_snapshots', {}).get(job['job_id'])
                        or mutation['cloud_snapshots'][job['job_id']] != _cloud_selection_identity(
                            ledger.db, project, job['message_id'])):
                    out['cloud_selection_changed'] = True
                    continue
                job = fresh
            if job['kind'] == JOB_KIND:
                import semantic_intake_hold
                try:
                    semantic_intake_hold.validate_indexes(ledger.db)
                    if (_known_long_defer_generation(job['payload']) is None
                            and semantic_intake_hold.quarantine_invalid_job(ledger, job)):
                        out['failed'] += 1
                        continue
                    intake_held = semantic_intake_hold.has_held_root(
                        ledger.db, job['project_id'], job['message_id'])
                except sqlite3.Error:
                    result['errors'].append('semantic: intake_hold_unverified')
                    out['intake_hold_unverified'] = True
                    break
                if intake_held:
                    hold_job(job, mutation, 'semantic: intake_held')
                    continue
            if job['kind'] == JOB_KIND and _known_long_defer_generation(job['payload']) is None:
                hold_job(job, mutation)
                continue
            if job['kind'] == JOB_KIND and not is_current(runtime.parse_payload(job)):
                try:
                    job = _adopt_pending_arrival(ledger, job)
                except ValueError as error:
                    if str(error) != 'semantic_intake_hold_unverified':
                        raise
                    result['errors'].append('semantic: intake_hold_unverified')
                    out['intake_hold_unverified'] = True
                    break
                if job is None:
                    continue
            if job['kind'] == JOB_KIND and not repair_attempted:
                _repair_pl = runtime.parse_payload(job)
                if (_repair_pl.get('execution_contract') == 1
                        and _repair_pl.get('execution_generation') is not None
                        and _repair_pl.get('execution_generation')
                            != runtime.execution_generation(scfg)):
                    repair_attempted = True
                    _fresh = ledger.db.execute(
                        'SELECT * FROM fetch_jobs WHERE job_id=?',
                        (job['job_id'],)).fetchone()
                    if (_fresh is None or _fresh['payload'] != job['payload']
                            or _fresh['state'] != job['state']):
                        continue
                    try:
                        _repair = reprocess_job(ledger, cfg, dict(_fresh),
                                                deadline=deadline)
                    except Exception as _error:
                        _repair = {'status': 'error'}
                        result['errors'].append(
                            f'semantic_reprocess_job: {type(_error).__name__}')
                    if _repair.get('status') == 'repaired':
                        job = ledger.db.execute(
                            'SELECT * FROM fetch_jobs WHERE job_id=?',
                            (job['job_id'],)).fetchone()
                        if job is None:
                            continue
                    elif _repair.get('status') in _REPAIR_HOLD:
                        hold_job(job, mutation,
                                 'semantic: execution_repair_'
                                 + str(_repair.get('status')))
                        continue
                    # Other statuses fall through: the normal guards below
                    # (paused / out-of-scope / stale) own those rows.
            token = runtime.JobToken.from_row(job)
            payload = runtime.parse_payload(job)
            limit = runtime.attempt_limit(payload)
            job_skip = None
            if runtime.circuit_open(ledger):
                out["circuit_open"] = True
                circuit = runtime._circuit_state(ledger)
                if circuit["failure_class"] not in runtime._CIRCUIT_FAILURE_CLASSES:
                    break
                job_skip = "circuit_open"
            from mcs_queries import paused
            if paused(ledger.db, job["project_id"]):
                runtime.transition(ledger, token, "defer", retry_in=300)
                _defer_note(out, "paused")
                continue
            if cfg_path is not None:
                current_cfg = load_config(cfg_path)
                scfg, errs = runtime_config(current_cfg)
                errors = errs
                current_generation = runtime.config_generation(current_cfg)
                observation_config_changed |= current_generation != cfg_generation
                cfg_generation = current_generation
                for e in errs:
                    if e not in result["errors"]:
                        result["errors"].append(e)
                if scfg["mode"] == "off":
                    runtime.transition(ledger, token, "defer", retry_in=300)
                    _defer_note(out, "mode_off")
                    out["mode"] = "off"
                    break
            # A semantic job's first call is exempt from the in-call reserve
            # gate. Starting below that reserve can burn a doomed
            # generation and a retry attempt before any useful progress.
            # Only start one when a call can plausibly fit, using the same
            # reserve applied to follow-up calls. Further stages defer
            # via BudgetShort without charging an attempt. Trailing QC
            # rows use their separate start floor.
            floor = deadline - (15.0 if job["kind"] == QC_JOB_KIND else
                                min(runtime.LLM_CALL_RESERVE_S,
                                    float(scfg["job_budget_seconds"]) * 2 / 3))
            if lane == "backlog":
                floor -= _backlog_commit_reserve(scfg)
            if time.monotonic() > floor:
                out["floor_skipped"][job["kind"]] = \
                    out["floor_skipped"].get(job["kind"], 0) + 1
                if job["kind"] == QC_JOB_KIND:
                    _defer_note(out, "deadline")
                    break
                continue
            if scfg["project_ids"] is not None \
                    and job["project_id"] not in scfg["project_ids"]:
                # outside the rollout scope — defer instead of leaving it
                # due-now, so a backlog of out-of-scope rows cannot fill
                # the whole max_jobs window and starve in-scope work. The
                # row stays pending: a later project_ids change resumes it.
                runtime.transition(ledger, token, "defer", retry_in=300)
                _defer_note(out, "out_of_scope")
                continue
            if token.attempts < 0 or token.attempts >= limit:
                # A row can be re-seeded or manually edited while it is waiting
                # in this due-list snapshot.  Close an exhausted row with the
                # complete token CAS before any client, budget, or network path.
                # Keep OFF/paused/out-of-scope rows untouched; they retain the
                # existing queue hold contract until the feature can run again.
                if runtime.transition(ledger, token, "failed"):
                    out["failed"] += 1
                else:
                    _defer_note(out, "commit_rejected")
                continue
            lane_cap = scfg["daily_request_budget"]
            if lane == "backlog":
                lane_cap -= math.ceil(lane_cap * BACKLOG_BUDGET_RESERVE)
            if jev_client is None:
                job_skip = job_skip or "disabled"
            elif not getattr(jev_client, "api_key", True):
                job_skip = job_skip or "not_configured"
            if jev_client is not None and job_skip is None:
                # the daily cap binds at REQUEST granularity, not just job
                # granularity — one job's claim-audit + loop-relation calls
                # must not overshoot it mid-flight (§13.5)
                remaining = lane_cap - jev_usage_today(ledger)
                if remaining <= 0:
                    # Compatibility field: the Jev quota is unavailable;
                    # the semantic job itself may still continue.
                    out["budget_exhausted"] = True
                    job_skip = "backlog_reserved" if lane == "backlog" else "daily_budget"
                else:
                    jev_client.request_cap = (jev_client.requests_made
                                              + remaining)
                if hasattr(jev_client, "attempt_timeout"):
                    jev_client.attempt_timeout = scfg["attempt_timeout_seconds"]
                if hasattr(jev_client, "job_budget"):
                    jev_client.job_budget = scfg["job_budget_seconds"]
            if job_skip is not None:
                try:
                    if errors:
                        raise ValueError("semantic_config_invalid")
                    runtime._jev_window_check(config_path=cfg_path)
                    jev_usage_today(ledger)
                except (jev.JevError, ValueError, sqlite3.Error):
                    if "semantic: jev_skip_unverified" not in result["errors"]:
                        result["errors"].append("semantic: jev_skip_unverified")
                    runtime.transition(ledger, token, "defer", retry_in=300)
                    _defer_note(out, "jev_skip_unverified")
                    continue
            job_jev = None if job_skip is not None else jev_client
            reserve_fn = None
            if (job_jev is not None
                    and getattr(jev_client, "supports_attempt_hooks", False)):
                reserve_fn = runtime.usage_reserver(
                    ledger, token, kind=KIND_USAGE, model=jev.JEV_MODEL,
                    project_id=job["project_id"], message_id=job["message_id"],
                    daily_request_budget=lane_cap, config_path=cfg_path)
            reservation = None
            if job['kind'] == JOB_KIND:
                try:
                    reservation = _reserve_long_defer(ledger, token)
                except Exception as error:
                    if type(error) is ValueError and str(error) == 'semantic_intake_held':
                        hold_job(job, mutation, 'semantic: intake_held')
                        continue
                    if type(error) is ValueError and str(error) == 'semantic_intake_hold_unverified':
                        result['errors'].append('semantic: intake_hold_unverified')
                        out['intake_hold_unverified'] = True
                        break
                    if 'semantic: long_defer_reservation_failed' not in result['errors']:
                        result['errors'].append('semantic: long_defer_reservation_failed')
                    out['long_defer_reservation_failed'] = True
                    if type(error) is ValueError and str(error) in (
                            'semantic_job_payload_unreadable', 'semantic_long_defers_capacity',
                            'semantic_long_defer_generation_unknown'):
                        hold_job(job, mutation, 'semantic: long_defer_capacity'
                                 if str(error) == 'semantic_long_defers_capacity'
                                 else 'semantic: job_payload_held')
                    continue
                if reservation is None:
                    continue
            # only now has the job cleared every gate — the persisted
            # fairness counters count jobs that were actually served, not
            # selections lost to circuit/budget/pause breaks (T14)
            selected[cohort_name] += 1
            if job["job_id"] == mutation.get("turn_job_id"):
                consumed_turns[mutation.get("turn_key", "turn")] = mutation["turn"]
            if lane == "backlog":
                _BACKLOG_TURN["kind"] = job["kind"]
            req0 = jev_client.requests_made if jev_client is not None else 0
            usage_before = dict(getattr(jev_client, "usage_totals", {}))
            job_started = time.perf_counter()
            job_age = max(0.0, time.time() - job["created_at"])
            # T14 phase split: queue wait (job_age_s) | model calls | Jev
            # evaluation | post-processing (validate→facts→render). The
            # wrappers only time calls — all attribution stays honest, and
            # the residual post_s covers the work between them.
            jev_timing = {"s": 0.0}
            timed_llm, llm_acc = _timed_llm(llm_fn)
            art0 = _last_stage_artifact(ledger, job["project_id"])
            diagnostics = _job_diagnostics()
            if job_skip is not None:
                diagnostics["jev_skip_reason"] = job_skip
            try:
                if job["kind"] == QC_JOB_KIND:
                    status = semantic_qc._process_qc_job(
                        ledger, scfg, job, job_jev, deadline,
                        reserve_fn=reserve_fn, cfg_path=cfg_path,
                        config_generation=cfg_generation,
                        jev_transport=jev_transport, jev_timing=jev_timing,
                        skip_reason=job_skip, lane=lane, diagnostics=diagnostics)
                else:
                    status = _process_job(
                        ledger, scfg, job, job_jev, timed_llm, deadline,
                        cfg_path=cfg_path,
                        config_generation=cfg_generation,
                        reserve_fn=reserve_fn, lane=lane, diagnostics=diagnostics, shadow_stats=shadow_stats,
                        jev_skip=job_skip,
                        jev_transport=jev_transport, jev_timing=jev_timing,
                        cloud_transport=(lambda call, *a, **kw: _unlocked("llm", call, *a, **kw))
                        if cloud_backlog else None, llm_timing=llm_acc, cloud_only=cloud_only)
            except writer_lock.RunLockLost as e:
                # An updater won the lock in a transport window (or the
                # bounded reacquire expired). Make no further writes: the job
                # stays pending with its attempts untouched. Its previously
                # committed long-defer reservation remains conservatively charged;
                # the _job_lock flock is released on return.
                # later jobs are not started, and the caller ends the
                # tick/batch on out["run_lock_lost"].
                out["run_lock_lost"] = ("held" if e.held
                                        else "updated" if e.updated
                                        else "timeout")
                result["errors"].append("semantic: run_lock_lost")
                return out
            except Exception as e:
                _why(diagnostics, f"exception_{type(e).__name__}")
                runtime.transition(ledger, token, "retry", retry_in=300,
                                   max_attempts=limit)
                result["errors"].append(
                    f"semantic {job['message_id']}: {type(e).__name__}")
                out["failed"] += 1
                status = None
            job_elapsed = time.perf_counter() - job_started
            skip_observed = diagnostics.get("jev_skip_reason")
            if skip_observed and status == "done":
                counts = out.setdefault("jev_skipped", {})
                counts[skip_observed] = counts.get(skip_observed, 0) + 1
                if skip_observed in ("daily_budget", "backlog_reserved"):
                    out["budget_exhausted"] = True
            job_progressed = _last_stage_artifact(ledger, job["project_id"]) > art0
            if job_progressed:
                out["progressed"] += 1
            budget_short = status == "deferred_short"
            if budget_short and not job_progressed:
                # nothing persisted this pass: a free defer would loop
                # forever (and re-spend Jev every minute) — charge a bounded
                # attempt like any other overrun
                status = "retry"
            if status in ("deferred", "deferred_backoff") \
                    and diagnostics.get("not_sent") == "long_output_needs_budget":
                n = reservation[1]['n'] if reservation is not None else 0
                if n > LONG_FREE_DEFER_CAP:
                    # this generation keeps meeting passes too short for
                    # its long-output call — stop the free loop: charge
                    # the attempt (ends 'failed' at the ceiling, where
                    # revive_failed and the operator can see it)
                    _why(diagnostics, "long_output_defer_capped")
                    out["long_output_capped"] = \
                        out.get("long_output_capped", 0) + 1
                    status = "retry"
            elif reservation is not None and status is not None:
                try:
                    _refund_long_defer(ledger, token, reservation)
                except Exception:
                    result['errors'].append('semantic: long_defer_refund_failed')
            # requests this attempt made — real clients already reserved one
            # usage row per request before POST; the delta row below is only
            # for fake clients without attempt hooks (jev_usage_today)
            used = (jev_client.requests_made - req0) \
                if jev_client is not None else 0
            usage_after = getattr(jev_client, "usage_totals", {})
            usage = {key: usage_after.get(key, 0) - usage_before.get(key, 0)
                     for key in ("input_tokens", "output_tokens", "reported_requests")}
            usage["unreported_requests"] = used - usage["reported_requests"]
            llm_s = llm_acc["s"]
            jev_s = jev_timing["s"]
            metric = _job_metric(
                job, token, cohort_name, status,
                job_age, job_elapsed, llm_s, jev_s, used, usage,
                jev_client, diagnostics=diagnostics)
            if status == "done":
                # every "done" return has already passed the
                # generation-fenced transition
                metric["completed_at"] = time.time()
            out["job_metrics"].append(metric)
            if status == "done":
                out["done_by_kind"][job["kind"]] = \
                    out["done_by_kind"].get(job["kind"], 0) + 1
            if used:
                runtime.record_circuit_result(ledger, getattr(jev_client, "last_error", None))
            # Real Jev calls reserve one usage row before POST.  A crash after
            # that commit therefore still spends the daily cap; adding the old
            # post-job delta would double count it.  Injected fake clients keep
            # the delta row for compatibility with offline tests.
            if used and reserve_fn is None:
                ledger.artifact_add(
                    KIND_USAGE, json.dumps({"job_id": job["job_id"]}),
                    project_id=job["project_id"],
                    message_id=job["message_id"], model=jev.JEV_MODEL,
                    meta={"jev_requests": used})
            if status is None:
                continue
            if status in ("deferred", "deferred_short", "deferred_backoff", "retry", "stale"):
                import cloud_backlog
                reason = diagnostics.get("not_sent") or diagnostics.get("code")
                if reason not in cloud_backlog._DEFER_REASONS:
                    reason = "unclassified"
                counts = out.setdefault("defer_reasons", {})
                counts[reason] = counts.get(reason, 0) + 1
            if _apply_job_status(ledger, token, limit, status,
                                 budget_short, out):
                break
            # A binding hold with no new work must not starve independent due jobs.
            if (lane == 'backlog' and not cloud_only and job['kind'] == JOB_KIND
                    and status == 'deferred'
                    and diagnostics.get('code') == 'empty_repair_candidate_binding'
                    and used == 0 and llm_s == 0 and not job_progressed):
                selected[cohort_name] -= 1
            if getattr(getattr(jev_client, "last_error", None),
                       "kind", "") == "payment_required":
                # the service refuses every call until the account is
                # settled — stop this pass here rather than spend one
                # more 402 per remaining job (2026-10-08)
                result["errors"].append("semantic: jev_payment_required")
                out["jev_blocked"] = "payment_required"
                break
    if held_after and not saw_held and sum(selected.values()):
        sched_mut.update(hold_cursor_changed=True, hold_cursor_lane=hold_lane, hold_cursor=None)
    if llm_health["fed"]:
        out["llm_down"] = True
    if (out.get('long_defer_reservation_failed')
            and 'semantic: long_defer_reservation_failed' not in result['errors']):
        result['errors'].append('semantic: long_defer_reservation_failed')
    unsaved = {cause: n - marks_unsaved0.get(cause, 0)
               for cause, n in semantic_transport.long_mark_failures().items()
               if n > marks_unsaved0.get(cause, 0)}
    if unsaved:
        # long-output marks this pass could not persist (T8): the
        # suppression still holds in-process, but a restart repeats
        # the base-ceiling call — surfaced instead of swallowed
        out["long_mark_unsaved"] = unsaved
    if not cloud_only and scfg["mode"] == "enforce" and scfg["summary_mode"] == "enforce":
        try:
            out["degraded_notices"] = semantic_notice.emit_degraded(ledger, scfg)
        except Exception:
            out["degraded_notices"] = 0
            result["errors"].append("semantic: degraded_scan_failed")
    if not out.get('cloud_selection_incomplete'):
        kind_ph = ",".join("?" * len(kinds))
        out["left"] = ledger.db.execute(
            f"SELECT COUNT(*) FROM fetch_jobs WHERE kind IN ({kind_ph}) "
            "AND state='pending' AND next_try<=?", (*kinds, time.time())).fetchone()[0]
    out["elapsed_s"] = time.perf_counter() - drain_started
    out["observation"] = _observation(
        started_wall, time.time(), cfg_generation, lane, out["job_metrics"])
    if observation_config_changed:
        # One run must not attribute earlier jobs to its final config,
        # even if settings changed and later returned to the initial value.
        out["observation"]["complete"] = False
    if due:
        _persist_drain_accounting(ledger, scfg, out, result, selected,
                                  qc_active, sched_mut, None,
                                  long_defers)
    return out


# Bounded automatic retry of exhausted semantic jobs (owner request
# 2026-09-30). Bounded three ways so a permanently broken input cannot
# burn the Jev budget: at most REVIVE_MAX jobs per pass, each
# job at most REVIVE_PER_INPUT times for the same input
# generation, and only after REVIVE_COOLDOWN_S since it failed.
# Each revival grants exactly one more attempt (manual_attempt_limit =
# attempts + 1); an input change still resets everything as before.
REVIVE_MAX = 20
REVIVE_PER_INPUT = 3
REVIVE_COOLDOWN_S = 6 * 3600


def revive_failed(ledger, now: float | None = None) -> dict:
    """Give exhausted 'failed' semantic jobs one more bounded attempt.

    Oldest failures first. The job keeps its generation and accumulated
    attempts; ``auto_retry`` counts revivals of the current input so the
    per-input cap survives restarts, and ``retry_command_id`` fences a
    worker that captured the pre-revival payload (same mechanism as the
    human retry command)."""
    now = time.time() if now is None else now
    out = {"revived": 0, "skipped_cap": 0}
    # capped jobs are excluded BEFORE the LIMIT — selecting them first
    # let a capped prefix starve every eligible job behind it
    capped = ("(CASE WHEN json_valid(payload) AND "
              "json_type(payload,'$.auto_retry')='integer' "
              "THEN json_extract(payload,'$.auto_retry') ELSE 0 END)")
    where = ("WHERE kind=? AND state='failed' AND updated_at<=? "
             "AND reason_code IS NOT 'semantic_payload_invalid' AND ")
    rows = ledger.db.execute(
        "SELECT job_id,attempts,payload,updated_at FROM fetch_jobs " + where
        + capped + "<? ORDER BY updated_at LIMIT ?",
        (JOB_KIND, now - REVIVE_COOLDOWN_S, REVIVE_PER_INPUT,
         REVIVE_MAX * 4)).fetchall()
    out["skipped_cap"] = ledger.db.execute(
        "SELECT COUNT(*) FROM fetch_jobs " + where + capped + ">=?",
        (JOB_KIND, now - REVIVE_COOLDOWN_S,
         REVIVE_PER_INPUT)).fetchone()[0]
    for row in rows:
        if out["revived"] >= REVIVE_MAX:
            break
        pl = loads_dict(row["payload"] or "{}")
        if pl is None:
            continue
        n = pl.get("auto_retry")
        n = n if type(n) is int and n >= 0 else 0
        if n >= REVIVE_PER_INPUT:
            out["skipped_cap"] += 1
            continue
        attempts = int(row["attempts"] or 0)
        pl["auto_retry"] = n + 1
        pl["manual_attempt_limit"] = max(
            attempts + 1, runtime.attempt_limit(pl))
        pl["retry_command_id"] = f"auto-{int(now)}-{row['job_id']}"
        with ledger.db:
            changed = ledger.db.execute(
                "UPDATE fetch_jobs SET state='pending',payload=?,next_try=?,"
                "updated_at=? WHERE job_id=? AND state='failed' "
                "AND attempts IS ? AND payload IS ? AND updated_at IS ?",
                (json.dumps(pl, ensure_ascii=False, sort_keys=True),
                 now, now, row["job_id"], row["attempts"], row["payload"],
                 row["updated_at"])).rowcount
        out["revived"] += changed
    return out


def main() -> int:
    """Bounded background drain; transport releases the ingestion lock."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--drain", action="store_true",
                    help="loop run_due until the queue is empty or "
                         "--stop-after expires")
    ap.add_argument("--stop-after", type=float, default=3600.0)
    ap.add_argument("--max-jobs", type=int, default=8)
    ap.add_argument("--revive-failed", action="store_true",
                    help="before draining, give exhausted failed jobs one "
                         "more bounded attempt")
    args = ap.parse_args()
    if not args.drain:
        if not args.revive_failed:
            ap.error("--drain or --revive-failed required")
        # Bookkeeping only: revive_failed's per-row conditional UPDATE is
        # a CAS on the selected failed input, so it needs no run lock — taking one
        # made the 6-hourly maintenance report failure whenever a tick or
        # drain held the lock at that moment (2026-10).
        from ledger import Ledger
        from mcs_util import DB
        led = Ledger(DB)
        try:
            print(json.dumps(revive_failed(led)))
        finally:
            led.close()
        return 0
    if not 1 <= args.max_jobs <= 32:
        ap.error("max-jobs must be between 1 and 32")
    if not math.isfinite(args.stop_after) or args.stop_after < 0:
        ap.error("stop-after must be a finite nonnegative number")
    from ledger import Ledger
    from mcs_util import CONF_PATH, DB
    from writer_lock import acquire_run_lock
    led = None
    stop = time.monotonic() + args.stop_after
    totals = {"done": 0, "deferred": 0, "failed": 0, "batches": 0}
    try:
        while time.monotonic() < stop:
            lock_fd = acquire_run_lock()
            if lock_fd is None:
                time.sleep(min(10, max(0.0, stop - time.monotonic())))
                continue
            try:
                if led is None:
                    led = Ledger(DB)
                    if args.revive_failed:
                        totals["revive"] = revive_failed(led)
                cfg = load_config()
                # The batch must fit one full job budget so a canonical
                # generation can complete. A shorter batch can repeatedly
                # abandon useful work and exhaust the retry allowance of
                # revived jobs without letting the generation finish. Still
                # bounded: the tick waits at most job_budget+30 s.
                try:
                    batch_s = max(
                        120.0, float(runtime_config(cfg)[0]
                                     ["job_budget_seconds"]) + 30.0)
                except Exception:
                    batch_s = 480.0
                result = {"errors": []}
                out = run_due(
                    led, cfg, result,
                    deadline=min(stop, time.monotonic() + batch_s),
                    max_jobs=args.max_jobs, cfg_path=CONF_PATH,
                    run_lock_fd=lock_fd, lane="backlog")
            finally:
                os.close(lock_fd)
            totals["batches"] += 1
            for k in ("done", "deferred", "failed"):
                totals[k] += out.get(k) or 0
            totals["left"] = out.get("left")
            # ts/pid: a window cut short (e.g. gateway restart kills
            # the catchup) is judged later from mcs.log
            print(json.dumps({**out, "batches": totals["batches"],
                              "ts": time.time(), "pid": os.getpid()},
                             ensure_ascii=False, default=str),
                  flush=True)
            if out.get("run_lock_lost"):
                # an update/rollback ran under us: this process holds the
                # old code, so it must not take the lock again
                totals["stopped"] = "run_lock_lost"
                break
            if out.get("left") == 0:
                totals["stopped"] = "queue_empty"
                break
            if not out.get("done"):
                # deferred/failed-only batches mean every claimable job
                # is backed off — hot-looping re-claims the same rows.
                time.sleep(min(10, max(0.0, stop - time.monotonic())))
            else:
                time.sleep(min(1, max(0.0, stop - time.monotonic())))
    finally:
        if led is not None:
            led.close()
    print(json.dumps({**totals, "ts": time.time(), "pid": os.getpid()},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
