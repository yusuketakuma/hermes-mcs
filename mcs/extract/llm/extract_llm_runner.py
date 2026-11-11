"""Run one bounded extraction pass with guarded transport and adoption."""
import contextlib
import copy
import json
import os
import queue
import sqlite3
import sys
import functools
import threading
import time
import writer_lock
import cloud_client
import cloud_backlog
import cloud_inference
import extract_schema
import extract_validation
import extract_quality
from extraction_provenance import (
    provider_evidence as _provider_evidence,
    EvidenceLeaf as _EvidenceLeaf,
    leaf_evidence as _leaf_evidence,
)
from mcs_util import load_config
from llm_availability import circuit_failure, circuit_open_s, circuit_streak, circuit_success, disk_floor_mb, disk_free_mb

from extract_llm_admission import (
    _llm_call_stats,
)
from extract_llm_call_context import (
    _CALL_NOTES,
)
from extract_llm_carry import (
    _settle_carry,
)
from extract_llm_context import (
    _chunk_context,
    _extraction_dispatch_current,
    _frame_medications,
    _frame_refs,
)
from extract_llm_engine import (
    llm_extract,
)
from extract_llm_errors import (
    _claim,
    _fail,
    _llm_up,
    _release,
    _renew,
    _write_lock,
)
from extract_llm_prepare import (
    _backlog_report,
    _ensure_v1,
    _plan_units,
    _prefilter_enabled,
    _prepare_jobs,
)
from extract_llm_prompts import (
    _BATCH_HEAD,
    _PROMPT_HEAD,
    _batch_prompt,
    _hint_coverage,
    _response_has_required_fields,
)
from extract_llm_proofs import (
    _capture_checkpoint_state,
)
import extract_llm_provider
from extract_llm_provider import (
    _actual_model,
    _cloud_compare,
    _comparison_extract,
    _integrity_summary,
    _llm_call,
    _note_list,
    _probe_format,
    _resolved_llm,
    pin_target,
)
from extract_llm_qc import (
    _current,
    _prior_extract,
    _prior_identity,
    _prior_matches,
    _protected_prior_current,
    _thin_retry_content,
)
from extract_llm_selection import (
    _CloudSelectionIncomplete,
    _batch_need_s,
    _cloud_priority_discard,
    _gc_stale_artifacts,
    _select_pending,
    _validate_run_args,
)
from extract_llm_source_contract import (
    KIND,
    TIMEOUT,
    _BATCH_MAX_TOKENS,
    _CARRY_MARGIN_S,
    _DEFERRED,
    _EXTRACT_LEASE_S,
    _MIN_CALL_S,
    _PREPARE_HOLD_S,
    _WRITER_STATE,
    _owns_current_source,
    _rule_hints,
    _source_provably_changed,
    _writer_current,
)
from extract_llm_store import (
    _commit_extraction_atomic,
    _persist_chunks,
)

def _run_batch_call(tups, target, deadline: float,
                    vital_policy: dict | None) -> tuple:
    """One shared call for context-free single-chunk rows ->
    (status, {job_index: validated}, call_meta). status "ok" may
    still omit indices (model skipped a target); "failed" means the
    envelope itself was unusable; "deferred" means the budget ended
    before/during the call."""
    notes = _note_list()
    notes.clear()   # batch-only runs never enter llm_extract's
                    # clear — unbounded in a resident worker
    start = len(notes)
    with pin_target(target):
        _CALL_NOTES.provider_response = None
        d = _llm_call(
            _batch_prompt([(r["body_text"], r["posted_at"], hints)
                           for _, r, _c, _s, hints, _q, _l in tups],
                          medication_hints=[_frame_medications(r) for _, r, *_ in tups]),
            deadline=deadline, schema=extract_schema.SCHEMA_BATCH,
            max_tokens=_BATCH_MAX_TOKENS,
            need_s=_batch_need_s(len(tups)))
    meta = _integrity_summary(notes[start:])
    batch_provider = getattr(_CALL_NOTES, "provider_response", None)
    meta["_provider_evidence"] = _provider_evidence(
        [batch_provider] if batch_provider else (), batch_provider == "local")
    meta["batch"] = len(tups)
    if d is _DEFERRED:
        return "deferred", {}, meta
    if not isinstance(d, dict) \
            or not isinstance(d.get("items"), list):
        return "failed", {}, meta
    out = {}
    seen = set()
    for item in d["items"]:
        if not isinstance(item, dict):
            continue
        i = item.get("i")
        # index must route to exactly one claimed row — an
        # unverifiable or duplicate item is dropped, never guessed
        if type(i) is not int or not (0 <= i < len(tups)):
            continue
        if i in seen:
            # Conflicting candidates have no authoritative first winner.
            # Leave this row to the existing single-message retry path.
            out.pop(tups[i][0], None)
            continue
        seen.add(i)
        if not _response_has_required_fields(item):
            continue
        index, r = tups[i][0], tups[i][1]
        drops: dict = {}
        v = extract_validation.validate({k: val for k, val in item.items() if k != "i"},
                      r["body_text"], drops)
        # an item that validates only with drops joins the residue:
        # the single lane's repair pass gets one feedback re-ask at
        # recovering the lost evidence/items — batch items must not
        # settle for the degraded output a single would have repaired
        if v is not None and not drops:
            v.pop("reply", None)   # batch rows are context-free
            if vital_policy:
                extract_validation.apply_vital_policy(v, r["body_text"], vital_policy)
            out[index] = v
    return "ok", out, meta


def run_pending(ledger, limit: int = 20, budget_s: float = 180,
                admitted_ids: set | None = None,
                per_write_lock: bool = False, workers: int = 1,
                shard: tuple[int, int] | None = None,
                oldest_first: bool = False,
                batch_k: int = 0,
                vital_policy: dict | None = None,
                write_wait_s: float = 60,
                carry: list | None = None,
                archived_first: bool = False,
                fair_oldest: bool = False, allow_cloud: bool = True,
                background: bool = False, cloud_only: bool = False,
                prioritize_patients: bool = False, run_lock_fd: int | None = None) -> dict:
    """Extract up to `limit` pending/stale messages with bounded inference.
    Returns {'done': n, 'left': n, 'failed': n}.

    ``write_wait_s`` bounds each per-write lock wait. With ``carry`` (a
    list from the previous call's ``result["carry"]``) a finished result
    whose lock wait timed out keeps its claim lease and rides to the
    next call instead of being deferred. A call with carry only saves or
    renews that previous pass: it admits no new inference, so a prolonged
    run-lock wait cannot accumulate more completed passes in memory.
    The result is dropped only when its lease nears expiry or its source
    moved, under the existing ownership and provider checks.

    `workers` bounds local calls; resident lanes may pin a local slot.
    Cloud configuration can widen the pool while local calls retain their
    separate semaphore. All DB access stays on the calling thread: cloud
    contexts are prepared as candidates are claimed, and writes land in
    the serial commit loop as each message/chunk completes. `budget_s`
    bounds inference and cloud claim admission; cloud-only selection also
    cooperatively interrupts SQL and receipt checks on a separate reader.

    `batch_k` groups context-free single-chunk rows K-to-a-call — the
    fixed per-call cost (queue wait, spec eval, decode ramp) amortizes
    across the backlog. Rows with thread context, saved chunk
    checkpoints, or multi-chunk bodies always run single; items the
    batch omits or fails per-item validation get an in-run single
    retry, so a bad group never buries a good row. Off by default —
    production callers (drainer CLI, the tick's derive stage) pass
    _BATCH_K, itself 0 since batching measured net negative; --batch K
    remains for explicit use."""
    _validate_run_args(limit, budget_s)
    if run_lock_fd is not None and (per_write_lock or workers != 1 or allow_cloud or cloud_only):
        raise ValueError("borrowed_run_lock_requires_serial_local")
    if ledger.db.in_transaction:
        return {"done": 0, "failed": 0, "left": None, "deferred": 0,
                "pids": [], "transaction_active": True, "carry": carry or []}
    if not _writer_current(full=True):
        return {"done": 0, "failed": 0, "left": None, "deferred": 0,
                "pids": [], "updated": True, "carry": carry or []}
    deadline = time.monotonic() + budget_s
    lock = functools.partial(_write_lock, wait_s=write_wait_s)
    carry_settled = dict.fromkeys(('done', 'held_prior', 'deferred'), 0)
    carry_count, carry_pids = 0, set()
    if carry:
        # A retained pass owns the admission window until it is saved or
        # rejected by the existing source/lease/provider checks. Never
        # append new paid results while its run-lock wait is unresolved.
        if deadline - time.monotonic() > 1:
            wait_s = min(write_wait_s, max(0.0, deadline - time.monotonic() - 1.0))
            carry_settled, carry_count, carry_pids = _settle_carry(
                ledger, carry, final=False, fence=_WRITER_STATE,
                lock_context=_write_lock(per_write_lock, wait_s=wait_s, deadline=deadline), report=False,
                preserve_on_fence_loss=True, deadline=deadline)
        if not _writer_current(full=True):
            return {'done': carry_settled['done'], 'failed': 0, 'left': None,
                    'deferred': carry_settled['deferred'], 'pids': sorted(carry_pids),
                    'defer_reasons': carry_settled.get('defer_reasons', {}),
                    'carry': carry, 'updated': True}
        return {**carry_settled, 'failed': 0, 'left': None, 'carried': carry_count,
                'carry': carry, 'carry_waiting': bool(carry), 'pids': sorted(carry_pids),
                'selected': 0, 'fresh_selected': 0, 'context_blocked': 0,
                'skipped': 0, 'lock_lost': 0, 'slow_deferred': 0,
                'left_estimate': None, 'left_estimate_age_s': None,
                'queue_ages_s': {'oldest': None, 'newest': None},
                'llm_calls': None}
    # A damaged maintenance cursor must not stop unrelated fresh extraction.
    # Only continue if the GC transaction was fully unwound.
    gc_error = None
    try:
        _gc_stale_artifacts(ledger, lock, per_write_lock, deadline=deadline)
    except (ValueError, RuntimeError, sqlite3.Error):
        if ledger.db.in_transaction:
            raise
        gc_error = 'extract_stale_gc_failed'
    try:
        rows, fresh_selected = _select_pending(ledger, limit * 64 if cloud_only else limit, admitted_ids,
                                               shard, oldest_first,
                                               archived_first=archived_first,
                                               fair_oldest=fair_oldest, cloud_only=cloud_only,
                                               prioritize_patients=prioritize_patients,
                                               **({'deadline': deadline} if cloud_only else {}))
    except _CloudSelectionIncomplete as error:
        return {"done": carry_settled['done'], "failed": 0, "left": None,
                "deferred": carry_settled['deferred'], "held_prior": carry_settled['held_prior'],
                "carried": carry_count, "pids": sorted(carry_pids),
                "carry": carry if carry is not None else [], "selected": None, "fresh_selected": None,
                "left_estimate": None, "left_estimate_age_s": None,
                "queue_ages_s": {"oldest": None, "newest": None},
                "cloud_selection_incomplete": True, "cloud_selection_reason": str(error),
                **({'gc_error': gc_error} if gc_error else {})}
    done, held_prior, deferred = (carry_settled[k] for k in ('done', 'held_prior', 'deferred'))
    failed = lock_lost = 0
    done_pids = set(carry_pids)
    endpoint_down = False
    breaker_fed = False
    slow_deferred = 0
    # Why units deferred this pass — a fixed vocabulary only; the worker's
    # pass record must never leak an error class, config value or id here.
    # Carry-settle reasons ride inside carry_settled and merge in here.
    defer_reasons: dict = dict(carry_settled.get("defer_reasons") or {})

    def _defer_note(reason, settled=None, count=1):
        if count <= 0:
            return
        if reason not in cloud_backlog._DEFER_REASONS:
            reason = "other"
        target = defer_reasons if settled is None else settled.setdefault("defer_reasons", {})
        target[reason] = target.get(reason, 0) + count

    def _feed_breaker():
        """One breaker observation per contiguous outage, not per queued row."""
        nonlocal breaker_fed
        if not breaker_fed:
            breaker_fed = True
            circuit_failure(ledger.db)

    last_seen = [float("-inf")]

    def _observe(kind, at):
        """Apply one call's endpoint observation. Parallel futures finish
        out of order, so a call that completed before the newest applied
        observation never overrides it."""
        nonlocal endpoint_down, breaker_fed
        at = time.monotonic() if at is None else at
        if at < last_seen[0]:
            return
        last_seen[0] = at
        if kind == "answered":
            circuit_success(ledger.db)
            # a later outage in this run is a new streak to feed
            endpoint_down = breaker_fed = False
        elif kind in ("unreachable", "unavailable"):
            _feed_breaker()
            endpoint_down = True
        elif kind == "transport":
            _feed_breaker()
    # finished results whose per-write lock wait timed out (the tick
    # holds the run lock for minutes) — written before returning, or
    # carried to the next call (see `carry`)
    pending_writes: list = []
    shadow_tasks = []
    carry_out: list = []
    carried = carry_count
    keep_claims: set = set()
    writer_stopped = False
    # thread contexts + any durable chunk checkpoints are loaded on the
    # calling thread — worker threads never touch the sqlite handle
    target = _resolved_llm()
    # Manifest-declared conversions (admitted_ids) were explicitly
    # requested — the prefilter never overrides them.
    prefilter = _prefilter_enabled() and admitted_ids is None
    prepare_reasons = {}
    jobs, skipped, context_blocked = [], 0, 0
    candidates = rows
    selected_count = len(rows)
    seen = {row["message_id"] for row in rows}
    if not cloud_only:
        rows = []  # local refill retains one candidate page, never all refused bodies
    while candidates and not cloud_only:
        refused = []
        prepared, row_skipped, row_blocked = _prepare_jobs(
            ledger, candidates, target, prefilter, allow_cloud=allow_cloud, background=background,
            cloud_only=False, load_checkpoints=False, deadline=deadline,
            blocked_reasons=prepare_reasons, blocked_rows=refused)
        jobs.extend(prepared)
        skipped += row_skipped
        context_blocked += row_blocked
        for refused_row, reason in refused:
            if (reason in cloud_backlog._PREPARE_CONTEXT_REJECTION_CODES
                    or reason in cloud_backlog._HOLD_DEFER_REASONS) \
                    and time.monotonic() < deadline and _writer_current(full=True):
                # This CAS is a finite preparation wait, not a sent/failed unit.
                # Never replace another owner's active claim or discard proof.
                _claim(ledger, refused_row, _PREPARE_HOLD_S)
        if jobs or not any(reason in cloud_backlog._PREPARE_CONTEXT_REJECTION_CODES
                           or reason in cloud_backlog._HOLD_DEFER_REASONS
                           for _, reason in refused) or time.monotonic() >= deadline:
            break
        # A rejected head must not spend the healthy-job limit. Seeing a source
        # once per pass and the existing deadline bound an all-held backlog.
        candidates, extra_fresh = _select_pending(
            ledger, limit, admitted_ids, shard, oldest_first,
            archived_first=archived_first, fair_oldest=fair_oldest,
            prioritize_patients=prioritize_patients, exclude_ids=seen)
        if not candidates:
            break
        if any(row["message_id"] in seen for row in candidates):
            break
        seen.update(row["message_id"] for row in candidates)
        selected_count += len(candidates)
        fresh_selected = (None if fresh_selected is None or extra_fresh is None
                          else fresh_selected + extra_fresh)
    deferred += context_blocked
    for reason, count in prepare_reasons.items():
        _defer_note(reason, count=count)
    _defer_note("prepare_blocked", count=context_blocked - sum(prepare_reasons.values()))
    metas = [None] * len(jobs)
    batch_metas = []
    checkpoints = queue.SimpleQueue()
    checkpoint_stopped = threading.Event()
    cfg = load_config()
    cloud_workers = cloud_inference.resolve_cloud_settings(cfg)["max_concurrency"] if cloud_only or any(
        r.get("_cloud_mode") == "on" for r, *_ in jobs) else 1
    pool_workers = max(workers, cloud_workers)
    local_slots = threading.BoundedSemaphore(max(1, workers))
    cloud_slots = threading.BoundedSemaphore(cloud_workers)
    parallel = pool_workers > 1 and len(jobs) > 1
    owner_thread = threading.get_ident()
    leases = {}

    def _checkpoint(index, chunk, value, split=False, receipt=None, replay=False):
        r, ctx = jobs[index][0], jobs[index][1]
        saved = False
        try:
            # Only a start precedes a dispatch; releasing an unsent start back
            # to pending stays durable after the deadline (owner check below).
            if (isinstance(value, dict) and value.get("_quality_status") == "started"
                    and (checkpoint_stopped.is_set() or time.monotonic() >= deadline)):
                return False
            # cloud chunks persist too, bound to their provider contract
            # (_chunk_context), so a deferred post resumes instead of re-paying
            if _owns_current_source(ledger, r, leases[index]):
                if split:
                    r["_splits"][chunk] = int(value)
                saved = _persist_chunks(ledger, r, {chunk: {} if split else value}, ctx,
                                chunk_size=r["_chunk_size"], splits=r["_splits"],
                                split_size=value if split else None, lease=leases[index],
                                guard=r.get("_checkpoint_guard"),
                                **({"replay": True} if replay else {}))
                if (saved and isinstance(value, _EvidenceLeaf)
                        and (value.get("_quality_status") in ("pending", "started") or value.repair_proof is not None)):
                    identity = r.get("_checkpoint_identity", {}).get(chunk)
                    value.quality_started_id = identity[0] if identity is not None else None
            if not saved:
                r["_checkpoint_conflict"] = True
            return saved
        finally:
            if receipt is not None:
                receipt["saved"] = saved
                receipt["event"].set()

    def _queue_checkpoint(index, chunk, value, split=False, replay=False):
        if (not replay and not split and "_quality_status" not in value
                and getattr(value, "repair_proof", None) is None):
            checkpoints.put((index, chunk, value, split, None, replay))
            return None
        receipt = {"event": threading.Event(), "saved": False}
        checkpoints.put((index, chunk, value, split, receipt, replay))
        while not receipt["event"].is_set():
            if checkpoint_stopped.is_set() or time.monotonic() >= deadline:
                return False
            receipt["event"].wait(min(0.05, max(0.0, deadline - time.monotonic())))
        return receipt["saved"] and not checkpoint_stopped.is_set() and time.monotonic() < deadline

    def _owner_call(fn, *, result=False):
        if threading.get_ident() == owner_thread:
            return fn()
        receipt = {"event": threading.Event()}
        checkpoints.put(("owner", fn, receipt, result))
        while not receipt["event"].wait(0.05):
            if checkpoint_stopped.is_set() or (not result and time.monotonic() >= deadline):
                raise cloud_inference.CloudFallback("cloud_owner_stopped")
        if "error" in receipt:
            raise receipt["error"]
        return receipt.get("value")

    def _flush_checkpoints():
        while True:
            try:
                item = checkpoints.get_nowait()
            except queue.Empty:
                return
            if item[0] == "owner":
                _, fn, receipt, result = item
                try:
                    if checkpoint_stopped.is_set() or (not result and time.monotonic() >= deadline):
                        raise cloud_inference.CloudFallback("cloud_owner_stopped")
                    receipt["value"] = fn()
                except BaseException as exc:
                    receipt["error"] = exc
                finally:
                    receipt["event"].set()
            else:
                _checkpoint(*item)

    def _extract(item):
        index, (r, ctx, saved, hints, qc) = item
        meta = {}
        metas[index] = meta
        chunks_out: dict = {}
        # last call's endpoint health, read by _handle on the calling
        # thread after this future resolves (thread-local per worker)
        _CALL_NOTES.transport = r["_transport"] = None
        _CALL_NOTES.transport_at = r["_transport_at"] = None
        _CALL_NOTES.defer_reason = r["_defer_reason"] = None
        r.pop("_quality_hold", None)
        if time.monotonic() > deadline:
            r["_defer_reason"] = "deadline"
            return _DEFERRED, chunks_out
        try:
            result = _extract_call(r, ctx, saved, hints, qc, index, meta, chunks_out)
            r["_provider_evidence"] = meta.pop("_provider_evidence", None)
            return result, chunks_out
        finally:
            if meta.pop("_quality_hold", False) is True:
                r["_quality_hold"] = True
            r["_transport"] = getattr(_CALL_NOTES, "transport", None)
            r["_transport_at"] = getattr(_CALL_NOTES, "transport_at", None)
            r["_defer_reason"] = getattr(_CALL_NOTES, "defer_reason", None)

    def _extract_call(r, ctx, saved, hints, qc, index, meta, chunks_out):
        provider = None
        if r.get("_cloud_mode") == "on":
            r["_cloud_lease"] = leases[index]
            provider = _owner_call(lambda: cloud_inference.CloudInferenceUnit(ledger, r["_cloud_config"], route="reextract",
                project_id=r["project_id"], message_id=r["message_id"],
                unit_key=str(r["message_id"]), deadline=deadline,
                current=lambda: (_owns_current_source(ledger, r, r["_cloud_lease"])
                                 and _prior_matches(ledger.db, r, r.get("_expected_prior"))
                                 and _protected_prior_current(ledger.db, r)),
                dispatch_current=lambda: not r.get("_checkpoint_conflict"),
                dispatch=_owner_call, owner={"lease": leases[index]}, selection=r["_cloud_selection"],
                cache_heads=(_PROMPT_HEAD, _BATCH_HEAD), input_binding=_chunk_context(r, ctx, checkpoint=True)))
        if provider is not None:
            try:
                with cloud_slots:
                    result = _infer(r, ctx, saved, hints, qc, index, meta, chunks_out, provider)
                    provider.commit_guard()
                    r["_cloud_guard"] = provider.commit_guard
                    r["_cloud_discard"] = provider.discard
                    r["_cloud_generation"] = provider.generation
                    if result is None:
                        raise cloud_inference.CloudFallback("cloud_validation_failed")
                    return result
            except cloud_client.CloudNotSent as error:
                _CALL_NOTES.defer_reason = cloud_client.not_sent_reason(error)
                if _CALL_NOTES.defer_reason == "checkpoint_unverified":
                    meta["_quality_hold"] = True
                return _DEFERRED
            except cloud_inference.CloudObservationIncomplete:
                # Unverifiable readiness is not a denial: keep a received paid
                # answer and the claim, defer instead of local fallback.
                _CALL_NOTES.defer_reason = "cloud_guard"
                return _DEFERRED
            except cloud_inference.CloudBusy:
                _CALL_NOTES.defer_reason = "busy"
                return _DEFERRED
            except cloud_inference.CloudFallback as error:
                if str(error) == "cloud_unit_changed":
                    provider.discard()
                # An unknown send keeps its source/backend claim; only a known outcome can fall back.
                r["_cloud_failed"] = provider.possibly_sent
                unresolved = getattr(provider, 'unresolved', provider.possibly_sent)
                if cloud_only or unresolved or (str(error) == "cloud_send_blocked" and (saved or chunks_out)):
                    _CALL_NOTES.defer_reason = "cloud_unit_unresolved" if unresolved else "cloud_fallback"
                    return _DEFERRED
                if str(error) == "cloud_unit_changed" and r.get("_cloud_owned"):
                    # The owned unit's history pin failed: keep the paid unit
                    # unresolved rather than restarting its claim locally.
                    _CALL_NOTES.defer_reason = "currentness_unverified"
                    return _DEFERRED
                # A posting is the provider unit: partial cloud chunks never seed local work.
                def reset_local():
                    r.pop("_provider_contract", None)
                    r.pop("_cloud_guard", None)
                    r.pop("_cloud_discard", None)
                    r.pop("_cloud_generation", None)
                    r["_cloud_mode"] = "off"
                    r["_target"] = target
                    fresh, _, _ = _prepare_jobs(ledger, [r], target, False, allow_cloud=False,
                                               load_checkpoints=False, deadline=deadline)
                    if not fresh:
                        raise cloud_inference.CloudFallback("cloud_local_context_unavailable")
                    local_r, local_ctx, local_saved, _, _ = fresh[0]
                    r.update(local_r)
                    local_saved = _capture_checkpoint_state(ledger.db, r, local_ctx)
                    jobs[index] = (r, local_ctx, local_saved, hints, qc)
                    return local_ctx, local_saved
                ctx, saved = _owner_call(reset_local)
                chunks_out.clear()
                meta.clear()
        with local_slots:
            # A plain local row was admitted under this run's breaker check; only
            # a cloud fallback re-reads it (a pure local row must not wait on the
            # owner thread for every call).
            if provider is not None and _owner_call(lambda: bool(circuit_open_s(ledger.db))):
                _CALL_NOTES.defer_reason = "circuit_open"
                return _DEFERRED
            return _infer(r, ctx, saved, hints, qc, index, meta, chunks_out)

    def _infer(r, ctx, saved, hints, qc, index, meta, chunks_out, provider=None):
        r["_proof_vital_policy"] = copy.deepcopy(vital_policy)
        with pin_target(r["_target"]):
            return llm_extract(r["body_text"], context=ctx, hints=hints,
                           **({"references": _frame_refs(r)} if _frame_refs(r) else {}),
                           quality_qc_src=r.get("qc_src"),
                           medication_hints=_frame_medications(r),
                           medication_guard=lambda: _owner_call(lambda: _extraction_dispatch_current(
                               ledger, r, leases[index], deadline)),
                           deadline=deadline, meta_out=meta,
                           posted_at=r["posted_at"],
                           **({"provider": provider} if provider is not None else {}),
                           chunks_in=saved,
                           chunks_out=chunks_out,
                           vital_policy=vital_policy,
                           chunk_size=r["_chunk_size"],
                           splits_in=r["_splits"],
                           on_split=(lambda key, size: _queue_checkpoint(index, key, size, True))
                           if parallel else
                           (lambda key, size: _checkpoint(index, key, size, True)),
                           feedback=qc["notes"] if qc else None,
                           on_chunk=(lambda i, v: _queue_checkpoint(index, i, v))
                           if parallel else
                           (lambda i, v: _checkpoint(index, i, v)),
                           on_chunk_replay=(lambda i, v: _queue_checkpoint(index, i, v, replay=True))
                           if parallel else
                           (lambda i, v: _checkpoint(index, i, v, replay=True)))

    def _commit(r, d, ctx, integrity, qc, used_keys=(), *, settled=None, shadow=None) -> bool:
        """Persist one validated result — the caller holds the lock.
        True when the artifact was written (a carried commit from an
        earlier pass counts it in the pass that finally wrote it)."""
        nonlocal done, deferred, held_prior
        r.pop("_commit_hold", None)
        if r.get("_cloud_guard"):
            try:
                r["_cloud_guard"]()
            except cloud_inference.CloudObservationIncomplete:
                r["_commit_hold"] = True
                deferred += 1
                _defer_note("cloud_guard", settled)
                if settled is not None:
                    settled['deferred'] += 1
                return False
            except cloud_inference.CloudFallback:
                if r.get("_cloud_discard"):
                    r["_cloud_discard"]()
                deferred += 1
                _defer_note("cloud_guard", settled)
                if settled is not None:
                    settled['deferred'] += 1
                return False
        # QC-flagged/thin/human-reported rows already hold a current
        # artifact — replacing it is the point of the feedback pass.
        extra = {"thin_retried": True} if r["thin_src"] else {}
        if r["human_src"]:
            extra["human_fix"] = {"feedback_id": r["human_src"],
                                  "applied": True}
        if not r.get("_kept_prior"):
            integrity = {**(integrity or {}), "validation": {
                key[1:]: d.get(key, 0) for key in ("_items_dropped", "_evidence_dropped")}}
        outcome = _commit_extraction_atomic(
            ledger, r, json.dumps(d, ensure_ascii=False), ctx, integrity,
            {"qc": qc["qc"], "applied": True} if qc else None,
            extra or None, used_keys,
            check_current=qc is None and not r["thin_src"] and not r["human_src"],
            expected_prior=r.get("_expected_prior"))
        if outcome != "written":
            if outcome == "rejected":
                r["_commit_hold"] = True
            if outcome == "held":
                held_prior += 1
                if settled is not None:
                    settled["held_prior"] += 1
            elif outcome != "current":
                deferred += 1
                reason = r.get("_commit_reject_reason")
                _defer_note(reason if type(reason) is str and reason in cloud_backlog._COMMIT_REJECTION_CODES
                            else "commit_rejected", settled)
                if settled is not None:
                    settled["deferred"] += 1
            return False
        done += 1
        if settled is not None:
            settled["done"] += 1
        done_pids.add(r["project_id"])
        if r.get("_cloud_mode") == "shadow":
            unit = r.pop("_shadow_unit", None)
            if unit:   # its pending row committed with the local result (_replace_current)
                # a carried commit reports into the pass that finally commits it
                (shadow_tasks if shadow is None else shadow).append((r, d, ctx, unit))
        return True

    def _handle(r, ctx, d, integrity, lease, chunks_out, qc=None):
        nonlocal done, failed, deferred, endpoint_down, lock_lost, slow_deferred
        nonlocal breaker_fed
        ctx = r.get("_context", ctx)
        keep_lease = False
        try:
            if not _owns_current_source(ledger, r, lease):
                if r.get("_cloud_discard"):
                    r["_cloud_discard"]()
                deferred += 1
                _defer_note("currentness_unverified")
                if (cloud_only or r.get("_cloud_owned")) \
                        and not _source_provably_changed(ledger, r):
                    # An unverifiable currentness keeps the bounded claim:
                    # a head that cannot be proven must not re-head the queue.
                    # A provably changed source instead follows the exact-owner
                    # CAS _release in the finally/tail below, so the new body
                    # can re-claim — owner, lease and paid-unknown holds are
                    # unchanged.
                    keep_lease = True
                    keep_claims.add(id(r))
                return
            transport = r.get("_transport")
            at = r.get("_transport_at")

            def cloud_retry():
                # the paid cloud attempt failed and local produced nothing: the
                # retry backoff keeps the next pass from re-sending the same body
                with _write_lock(per_write_lock) as held:
                    if held and not _current(ledger, r["message_id"], r["content_hash"]):
                        _fail(ledger, r, r["attempts"], lease=lease)
            if d is _DEFERRED:
                if (r.pop("_quality_hold", False) is True
                        or not cloud_only and r.get("_defer_reason") in ("cloud_unit_unresolved", "cloud_guard")):
                    # Reuse the exact source/owner/lease CAS wait in _release;
                    # an unverified resident unit must also yield to later rows.
                    r["_commit_hold"] = True
                deferred += 1
                _defer_note(r.get("_defer_reason") or "other")
                # Keep the existing finite claim for an unresolved paid unit;
                # repeated refusals must not monopolize a single-job cloud pass.
                if (cloud_only or r.get("_cloud_owned")) \
                        and r.get("_defer_reason") in cloud_backlog._HOLD_DEFER_REASONS:
                    keep_lease = True
                    keep_claims.add(id(r))
                if r.get("_cloud_failed"):
                    cloud_retry()
                # answered then deferred (no time left to repair) is still
                # a live endpoint; refused / 503 ran nothing, so no attempt
                # is charged, but the outage must reach the breaker and
                # the health marker the stall alert reads
                if transport in ("answered", "unreachable", "unavailable"):
                    _observe(transport, at)
                return  # budget ran out mid-message — stays pending
            if d is None:
                failed += 1
                if transport == "answered":
                    # the endpoint answered; only this message's output
                    # failed — an earlier outage in this run is over
                    _observe(transport, at)
                elif transport != "expired" and (
                        endpoint_down or not _llm_up(deadline=deadline)):
                    # Endpoint unreachable — don't burn budget/attempts
                    # on error rows. The failure feeds the breaker so
                    # a sustained outage closes the lane across drains —
                    # counted once per outage, not once per queued row.
                    _feed_breaker()
                    endpoint_down = True
                    if r.get("_cloud_failed"):
                        cloud_retry()
                    return
                if transport == "transport":
                    # /v1/models answers but the call timed out or was
                    # reset: a slow/stuck server, not this message's
                    # fault. Consecutive runs feed the breaker; once it
                    # has tripped (streak > 0) attempts are no longer
                    # charged, so an outage cannot walk every queued row
                    # to the permanent-failure ceiling. An answered call
                    # resets the streak, so a message that times out on
                    # a healthy server still reaches the ceiling.
                    _observe(transport, at)
                    if circuit_streak(ledger.db) > 0:
                        slow_deferred += 1
                        if r.get("_cloud_failed"):
                            cloud_retry()
                        return
                if qc is not None:
                    # A QC-flagged row that failed its feedback re-
                    # extract keeps the audited artifact — re-mint it
                    # with qc_fix so the flag settles instead of
                    # burning an LLM call every cycle.
                    src = ledger.db.execute(
                        "SELECT content FROM artifacts WHERE artifact_id=?",
                        (qc["src"],)).fetchone()
                    prior = _prior_extract(ledger.db, r)
                    if (src is not None and prior is not None and prior["artifact_id"] == qc["src"]
                            and prior["content"] == src["content"]):
                        # these writes are never carried: a skipped one
                        # re-selects the row for another paid call, so
                        # wait out the tick (default 60 s), not write_wait_s
                        with _write_lock(per_write_lock) as held:
                            if held:
                                _commit_extraction_atomic(
                                    ledger, r, src["content"], ctx, None,
                                    {"qc": qc["qc"], "applied": False}, None, (),
                                    check_current=False, mark_context=False,
                                    expected_prior=_prior_identity(prior))
                    return
                if r["human_src"]:
                    # A failed report re-extract settles like the thin
                    # retry: re-mint the reported content with
                    # human_fix.applied=false — one attempt per report.
                    src = ledger.db.execute(
                        "SELECT c.artifact_id,c.content FROM artifacts h JOIN artifacts c"
                        " ON c.artifact_id=json_extract(h.content,"
                        "'$.artifact_id') WHERE h.artifact_id=?"
                        " AND json_valid(h.content) AND c.kind=?"
                        " AND c.message_id=?",
                        (r["human_src"], KIND, r["message_id"])).fetchone()
                    prior = _prior_extract(ledger.db, r)
                    if (src is not None and prior is not None
                            and prior["artifact_id"] == src["artifact_id"]
                            and prior["content"] == src["content"]):
                        with _write_lock(per_write_lock) as held:
                            if held:
                                _commit_extraction_atomic(
                                    ledger, r, src["content"], ctx, None, None,
                                    {"human_fix": {"feedback_id": r["human_src"],
                                                   "applied": False}}, (),
                                    check_current=False, mark_context=False,
                                    expected_prior=_prior_identity(prior))
                    return
                if r["thin_src"]:
                    # A thin retry that failed still settles: re-mint
                    # the prior content with the retried marker so the
                    # row leaves pending (same contract as qc_fix —
                    # never burn an LLM call every cycle on a row the
                    # model cannot enrich).
                    content = _thin_retry_content(ledger, r)
                    prior = _prior_extract(ledger.db, r)
                    if (content is not None and prior is not None and prior["artifact_id"] == r["thin_src"]
                            and prior["content"] == content):
                        r["_kept_prior"] = True
                        with _write_lock(per_write_lock) as held:
                            if held:
                                _commit_extraction_atomic(
                                    ledger, r, content, ctx, None, None,
                                    {"thin_retried": True}, (),
                                    check_current=False, mark_context=False,
                                    expected_prior=_prior_identity(prior))
                    return
                with _write_lock(per_write_lock) as held:
                    if held and not _current(ledger, r["message_id"],
                                             r["content_hash"]):
                        # _fail carries auto_retry so a revived refail
                        # keeps the REVIVE_PER_INPUT cap
                        _fail(ledger, r, r["attempts"], lease=lease)
                return
            # a usable result whose last call (an optional repair) failed
            # still saw an outage; no call note at all means an answer
            if r.get("_cloud_mode") != "on":
                _observe(transport or "answered", at)
            if r["thin_src"] and qc is None and not r["human_src"]:
                content = _thin_retry_content(ledger, r)
                if content is None:
                    deferred += 1
                    _defer_note("prior_unavailable")
                    return
                prior_row = _prior_extract(ledger.db, r)
                if (prior_row is None or prior_row["artifact_id"] != r["thin_src"]
                        or prior_row["content"] != content):
                    deferred += 1
                    _defer_note("prior_unavailable")
                    return
                try:
                    prior = json.loads(content)
                except (ValueError, TypeError, RecursionError):
                    prior = None
                if not extract_quality.improves(d, prior) and not extract_quality.protected_lost(prior or {}, d, r["body_text"]):
                    d = prior
                    r["_kept_prior"] = True
                    r["_expected_prior"] = _prior_identity(prior_row)
            if not r.get("_kept_prior"):
                d["_model"] = _actual_model(r)
            with lock(per_write_lock) as held:
                if held:
                    _commit(r, d, ctx, integrity, qc, tuple(chunks_out))
                    return
            # never discard a paid-for result: keep it (and its lease)
            # for one more lock attempt before run_pending returns
            lock_lost += 1
            keep_lease = True
            pending_writes.append((r, d, ctx, integrity, qc, lease, tuple(chunks_out)))
        finally:
            if not keep_lease and (not cloud_only or _writer_current(full=True)):
                _release(ledger, r, lease)

    # stability gates before any claim: an open circuit breaker (dead
    # endpoint) or a nearly-full volume skips the LLM lane entirely —
    # rows stay pending for the next drain instead of churning leases
    # and timeouts. Prefilter markers above still settled no-signal
    # rows, since they never needed the endpoint.
    circuit_s = circuit_open_s(ledger.db)
    free_mb = disk_free_mb(ledger.db)
    disk_low = free_mb is not None and free_mb < disk_floor_mb()
    # claim each row before inference — a live lease held by another
    # drainer/tick makes the conflict-update a no-op, so two workers
    # never pay for the same LLM call (F14)
    claimed = []
    try:
        for index, candidate in enumerate(rows if cloud_only else jobs):
            if disk_low:
                break
            if cloud_only:
                if time.monotonic() >= deadline:
                    break
                # Lookahead replenishes lost claims without freezing unused attempt frames.
                row_reasons = {}
                prepared, row_skipped, row_blocked = _prepare_jobs(
                    ledger, [candidate], target, prefilter, allow_cloud=allow_cloud,
                    background=background, cloud_only=True, load_checkpoints=False, deadline=deadline,
                    blocked_reasons=row_reasons)
                skipped += row_skipped
                context_blocked += row_blocked
                deferred += row_blocked
                for reason, count in row_reasons.items():
                    _defer_note(reason, count=count)
                _defer_note("prepare_blocked", count=row_blocked - sum(row_reasons.values()))
                if not prepared:
                    if any(reason in cloud_backlog._HOLD_DEFER_REASONS for reason in row_reasons):
                        # An unverifiable owned head keeps a bounded claim —
                        # the next pass sees the tail instead of re-blocking.
                        _claim(ledger, candidate, _PREPARE_HOLD_S)
                    if prioritize_patients and time.monotonic() < deadline:
                        _cloud_priority_discard(ledger, candidate['message_id'])
                    continue
                index = len(jobs)
                candidate = prepared[0]
                jobs.append(candidate)
                metas.append(None)
                if time.monotonic() >= deadline:
                    deferred += 1
                    _defer_note("deadline")
                    break
            r, ctx, saved, hints, qc = candidate
            if circuit_s and r.get("_cloud_mode") != "on":
                continue
            lease = _claim(ledger, r, max(_EXTRACT_LEASE_S,
                                          deadline - time.monotonic() + TIMEOUT + 30))
            if lease is not None:
                leases[index] = lease
                claimed.append((index, r, ctx, saved, hints, qc, lease))
                # Preparation precedes the claim. Another owner may have
                # advanced a checkpoint in that interval, so read again only
                # after this worker owns the lease.
                saved = _capture_checkpoint_state(ledger.db, r, ctx)
                jobs[index] = (r, ctx, saved, hints, qc)
                claimed[-1] = (index, r, ctx, saved, hints, qc, lease)
                # the v3 pass performs the v1/v2 (rule) work simultaneously —
                # claimed rows mint/refresh their extract_v1 artifact here so
                # speed-lane readers never wait on the LLM queue
                _ensure_v1(ledger, r, hints)
                if cloud_only and len(claimed) >= limit:
                    break
            elif cloud_only and prioritize_patients and time.monotonic() < deadline:
                _cloud_priority_discard(ledger, r['message_id'])
    except BaseException:
        # Preparation precedes inference's finally; release its committed
        # claims too when a rule write fails or the worker is interrupted.
        if not cloud_only or _writer_current(full=True):
            for _index, r, _ctx, _saved, _hints, _qc, lease in claimed:
                _release(ledger, r, lease)
        raise

    if cloud_only:
        parallel = pool_workers > 1 and len(claimed) > 1

    def _exec_batch(tups):
        _CALL_NOTES.transport = _CALL_NOTES.transport_at = None
        with local_slots:   # cloud workers widen the pool, never the local backend
            def guard():
                return _owner_call(lambda: all(_extraction_dispatch_current(ledger, r, lease, deadline)
                    for _, r, _c, _s, _h, _q, lease in tups))
            if not guard():
                return "deferred", {}, {}, None, None
            previous = getattr(_CALL_NOTES, "medication_guard", None)
            previous_changed = getattr(_CALL_NOTES, "medication_source_changed", False)
            _CALL_NOTES.medication_source_changed = False
            _CALL_NOTES.medication_guard = guard if any(_frame_medications(r) is not None for _, r, *_ in tups) else None
            try:
                out = _run_batch_call(tups, target, deadline, vital_policy)
            finally:
                _CALL_NOTES.medication_guard = previous
                _CALL_NOTES.medication_source_changed = previous_changed
        return (*out, getattr(_CALL_NOTES, "transport", None),
                getattr(_CALL_NOTES, "transport_at", None))

    def _handle_batch(status, results, meta, tups, transport=None, at=None):
        """Persist batch outcomes row by row; return the residue —
        rows whose item was omitted/invalid or whose whole call
        failed — for an in-run single retry (with repair). A deferred
        batch's rows take the same lane — the per-call floor keeps it
        free when the remainder fits nothing."""
        if transport is not None:
            # residue retries reset each row's note, so the batch call's
            # own observation is applied here, before any of them run
            _observe(transport, at)
        residue = []
        provider_evidence = meta.pop("_provider_evidence", None)
        batch_metas.append(meta)
        for tup in tups:
            index, r, ctx, saved, hints, qc, lease = tup
            r["_transport"], r["_transport_at"] = transport, at
            if status == "ok" and index in results:
                if extract_quality.completeness_issues(results[index], hints, r["body_text"]):
                    # The batch paid for this valid first candidate. Persist it
                    # before the single lane attempts only its optional repair.
                    saved[0] = {**results[index], "_quality_status": "pending"}
                    if _leaf_evidence(_EvidenceLeaf({}, provider_evidence)) is not None:
                        saved[0] = _EvidenceLeaf(saved[0], provider_evidence)
                    _checkpoint(index, 0, saved[0])
                    residue.append(tup)
                else:
                    r["_provider_evidence"] = provider_evidence
                    metas[index] = meta
                    row_meta = {**meta, "completeness_chunks": 1,
                        "completeness": {"0": _hint_coverage(results[index], hints, r["body_text"])}}
                    _handle(r, ctx, results[index], row_meta, lease, {}, qc)
            elif transport == "expired":
                # Every row was sent in the batch; a residue retry would
                # lose its failure at the now-expired preflight floor.
                metas[index] = meta
                _handle(r, ctx, None, meta, lease, {}, qc)
            else:
                # a deferred batch's rows also fall through: the single
                # lane's preflight floor re-defers them for free when
                # nothing fits, but a lone single (90s) can still finish
                # inside a remainder too short for the batch (280s at
                # K=4)
                residue.append(tup)
        return residue

    previous_transport = getattr(_CALL_NOTES, "transport_context", contextlib.nullcontext)
    try:
        if run_lock_fd is not None:
            _CALL_NOTES.transport_context = functools.partial(
                writer_lock.unlocked_transport, ledger.db, run_lock_fd, deadline=deadline)
        # Work units preserve selection order: context-bearing, multi-
        # chunk, or checkpointed rows run single; context-free single-
        # chunk bodies batch batch_k-to-a-call so queue wait, spec
        # eval and decode overhead amortize across the backlog.
        units = _plan_units(claimed, batch_k)

        def _single_future(pool, tup):
            index, r, ctx, saved, hints, qc, _lease = tup
            return pool.submit(_extract,
                               (index, (r, ctx, saved, hints, qc)))

        pre_probes = 0
        if parallel and units:
            # settle the probed output format before fanning out — the
            # workers would otherwise race to mutate the global mode.
            # Below the smallest floor every unit defers anyway, so the
            # probe would be a call nobody uses.
            if any(r.get("_cloud_mode") != "on" for _, r, *_ in claimed) and deadline - time.monotonic() >= _MIN_CALL_S:
                probed_at = extract_llm_provider._FMT_TS
                _probe_format(deadline=deadline)
                pre_probes = int(extract_llm_provider._FMT_TS != probed_at)
            from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
            with ThreadPoolExecutor(
                    max_workers=min(pool_workers, len(units))) as pool, contextlib.ExitStack() as cleanup:
                cleanup.callback(checkpoint_stopped.set)  # release ack waiters before executor shutdown
                futs = {}
                for kind, payload in units:
                    if kind == "single":
                        futs[_single_future(pool, payload)] = \
                            ("single", [payload])
                    else:
                        futs[pool.submit(_exec_batch, payload)] = \
                            ("batch", payload)
                # as_completed -> every finished item is persisted at once,
                # not held until the whole batch resolves (F14)
                pending = set(futs)
                while pending:
                    finished, pending = wait(pending, timeout=0.05,
                                             return_when=FIRST_COMPLETED)
                    _flush_checkpoints()
                    for fut in finished:
                        kind, tups = futs.pop(fut)
                        try:
                            res = fut.result()
                        except Exception as e:
                            # one worker crash must not discard the
                            # other finished (paid) inferences: only its
                            # rows stay pending, claims are released
                            # below and no attempt is burned
                            print(json.dumps({"event": "extract_worker_error",
                                              "error": type(e).__name__,
                                              "pid": os.getpid(), "ts": time.time()}),
                                  file=sys.stderr, flush=True)
                            deferred += len(tups)
                            _defer_note("worker_error", count=len(tups))
                            continue
                        if kind == "single":
                            index, r, ctx, saved, hints, qc, lease = \
                                tups[0]
                            d, chunks_out = res
                            _handle(r, ctx, d, metas[index], lease,
                                    chunks_out, qc)
                        else:
                            for tup in _handle_batch(*res[:3], tups,
                                                     *res[3:]):
                                nfut = _single_future(pool, tup)
                                futs[nfut] = ("single", [tup])
                                pending.add(nfut)
        else:
            for kind, payload in units:
                if kind == "single":
                    index, r, ctx, saved, hints, qc, lease = payload
                    d, chunks_out = _extract(
                        (index, (r, ctx, saved, hints, qc)))
                    _handle(r, ctx, d, metas[index], lease, chunks_out,
                            qc)
                else:
                    res = _exec_batch(payload)
                    for tup in _handle_batch(*res[:3], payload,
                                             *res[3:]):
                        index, r, ctx, saved, hints, qc, lease = tup
                        d, chunks_out = _extract(
                            (index, (r, ctx, saved, hints, qc)))
                        _handle(r, ctx, d, metas[index], lease,
                                chunks_out, qc)
        if pending_writes:
            tail_lock = (_write_lock(per_write_lock, wait_s=write_wait_s, deadline=deadline)
                         if cloud_only else lock(per_write_lock))
            with tail_lock as held:
                if cloud_only and not _writer_current(full=True):
                    writer_stopped = True
                    for r, d, ctx, integrity, qc, lease, used_keys in pending_writes:
                        carry_out.append((r, lease, functools.partial(
                            _commit, r, d, ctx, integrity, qc, used_keys)))
                else:
                    for r, d, ctx, integrity, qc, lease, used_keys in pending_writes:
                        # still lock-bound, or the body changed/lease lapsed
                        # while we waited: the row stays pending for the
                        # next run — deferred, no attempt burned
                        if cloud_only and time.monotonic() >= deadline and carry is not None:
                            carry_out.append((r, lease, functools.partial(
                                _commit, r, d, ctx, integrity, qc, used_keys)))
                            keep_claims.add(id(r))
                        elif held and _owns_current_source(ledger, r, lease):
                            _commit(r, d, ctx, integrity, qc, used_keys)
                        elif carry is not None and not held \
                                and lease > time.time() + _CARRY_MARGIN_S \
                                and (renewed := _renew(ledger, r, lease)):
                            carry_out.append((r, renewed, functools.partial(
                                _commit, r, d, ctx, integrity, qc, used_keys)))
                            keep_claims.add(id(r))
                        else:
                            if r.get("_cloud_discard") and not _owns_current_source(ledger, r, lease):
                                r["_cloud_discard"]()
                            deferred += 1
                            _defer_note("write_unavailable" if not held else "currentness_unverified")
                            if held and (cloud_only or r.get("_cloud_owned")):
                                keep_claims.add(id(r))
        if cloud_only and not _writer_current(full=True):
            writer_stopped = True
    except writer_lock.RunLockLost:
        # A lost writer fence also forbids checkpoint and claim cleanup writes.
        writer_stopped = True
        raise
    finally:
        _CALL_NOTES.transport_context = previous_transport
        if cloud_only and not _writer_current(full=True):
            writer_stopped = True
        if not writer_stopped:
            try:
                _flush_checkpoints()
            finally:
                if cloud_only and not _writer_current(full=True):
                    writer_stopped = True
                if not writer_stopped:
                    for _index, r, _ctx, _saved, _hints, _qc, lease in claimed:
                        if id(r) not in keep_claims:
                            _release(ledger, r, lease)
    if writer_stopped:
        return {'done': done, 'failed': failed, 'held_prior': held_prior, 'left': None,
                'carried': carried, 'carry': carry_out, 'deferred': deferred,
                'defer_reasons': dict(defer_reasons),
                'pids': sorted(done_pids), 'updated': True}
    for r, local_result, ctx, unit in shadow_tasks:
        if deadline - time.monotonic() < _MIN_CALL_S:
            continue       # its pending row (written with the commit) stays uncompared
        comparison_meta = {}
        cloud_result = None
        provider = None
        try:
            provider = cloud_inference.CloudInferenceUnit(ledger, r["_cloud_config"], route="reextract", project_id=r["project_id"],
                message_id=r["message_id"], unit_key=str(r["message_id"]), deadline=deadline,
                current=lambda r=r: _current(ledger, r["message_id"], r["content_hash"]),
                dispatch=lambda fn, **kw: fn(), owner={"after_commit": True}, selection=r["_cloud_selection"],
                cache_heads=(_PROMPT_HEAD, _BATCH_HEAD), input_binding=_chunk_context(r, ctx, checkpoint=True))
            cloud_result = llm_extract(r["body_text"], context=ctx, hints=_rule_hints(r),
                deadline=deadline, meta_out=comparison_meta, posted_at=r["posted_at"],
                vital_policy=vital_policy, provider=provider,
                medication_hints=_frame_medications(r),
                **({"references": _frame_refs(r)} if _frame_refs(r) else {}))
            provider.commit_guard()
        except cloud_client.CloudNotSent:
            cloud_result = None
        except cloud_inference.CloudFallback as error:
            if provider is not None and str(error) == "cloud_unit_changed":
                provider.discard()
            cloud_result = None
        except ValueError:
            cloud_result = None
        valid = isinstance(cloud_result, dict)
        with lock(per_write_lock) as held:
            if not held:     # the audit row is a write: never without the run lock
                continue
            _cloud_compare(ledger, route="reextract", project_id=r["project_id"], unit=unit,
                local_facts=_comparison_extract(r, local_result),
                cloud_facts=_comparison_extract(r, cloud_result) if valid else None,
                rejected=sum(cloud_result.get(k, 0) for k in ("_items_dropped", "_evidence_dropped")) if valid else None,
                held=int(bool(extract_quality.protected_lost(local_result, cloud_result, r["body_text"]))) if valid else None,
                repairs=comparison_meta.get("repairs", 0 if valid else None))
    # Restricted selection cannot bound the global backlog, even when it is empty.
    scoped = cloud_only or admitted_ids is not None or shard is not None
    left, age_min, age_max, estimate = (None, None, None, None) if scoped else _backlog_report(
        ledger, selected_count, limit, done + skipped + held_prior)
    now_ts = time.time()
    llm_calls = _llm_call_stats([*metas, *batch_metas])
    if pre_probes:
        # the resident's real probe site is the pre-fanout one
        llm_calls = llm_calls or {"probe_runs": 0}
        llm_calls["probe_runs"] = (llm_calls.get("probe_runs") or 0) + pre_probes
    return {"done": done, "failed": failed, "held_prior": held_prior, "left": left,
            "carried": carried, "carry": carry_out,
            # pending as of the last full count, minus what this process
            # settled since; age says how stale the count is
            "left_estimate": max(0, estimate["left"] - estimate["done"]) if estimate is not None else None,
            "left_estimate_age_s": round(time.monotonic() - estimate["at"]) if estimate is not None else None,
            "fresh_selected": fresh_selected,
            "selected": selected_count, "deferred": deferred,
            "defer_reasons": dict(defer_reasons),
            "context_blocked": context_blocked,
            "skipped": skipped, "lock_lost": lock_lost,
            "slow_deferred": slow_deferred,
            "circuit_open_s": round(circuit_s) or None,
            "disk_low": disk_low,
            "disk_free_mb": round(free_mb) if free_mb is not None
                            else None,
            "pids": sorted(done_pids),
            "queue_ages_s": {
                "oldest": (now_ts - age_min) if age_min is not None
                          else None,
                "newest": (now_ts - age_max) if age_max is not None
                          else None},
            "llm_calls": llm_calls, **({'gc_error': gc_error} if gc_error else {})}
