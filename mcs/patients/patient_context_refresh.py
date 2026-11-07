"""Advance fair patient context work while preserving inference and publication evidence."""
from contextlib import closing as closing, contextmanager as contextmanager
from datetime import datetime as datetime, timedelta as timedelta
import copy as copy
import hashlib as hashlib
import json as json
import math as math
import os as os
from pathlib import Path as Path
import re as re
import sqlite3 as sqlite3
from types import SimpleNamespace as SimpleNamespace
import local_llm as local_llm
import initial_import_state as initial_import_state
import cloud_inference as cloud_inference
import inference_dependency_loss as dependency_loss
from clinical_chunking import PLAN_VERSION as PLAN_VERSION
from mcs_queries import JST as JST
from mcs_util import html_to_text as html_to_text, loads_dict as loads_dict, strict_json as strict_json, valid_hash as valid_hash
from inference_version import VERSION_KEY as VERSION_KEY, current_meta_pred as current_meta_pred, read_version as read_version
import patient_context_plan as context_plan
import patient_context_schedule as context_schedule
import mcs_queries as context_queries
import patient_context_refresh_state
import patient_context_quality_receipts
import patient_context_refresh_sources
import patient_context_refresh_readiness
import patient_context_quality_controller
from patient_context_refresh_state import (
    CALL_SECONDS as CALL_SECONDS,
    CHUNK as CHUNK,
    COMMIT_MARGIN as COMMIT_MARGIN,
    CONTRACT as CONTRACT,
    CURRENT_VERSION as CURRENT_VERSION,
    Deferred as Deferred,
    KIND as KIND,
    PROGRESS as PROGRESS,
    QUALITY as QUALITY,
    QUALITY_CLAIM as QUALITY_CLAIM,
    QUALITY_FAIL as QUALITY_FAIL,
    QUALITY_PASS as QUALITY_PASS,
    QUALITY_REPAIR as QUALITY_REPAIR,
    QUALITY_REPAIR_CANDIDATE as QUALITY_REPAIR_CANDIDATE,
    QUALITY_RETRY as QUALITY_RETRY,
    QUALITY_SKIP as QUALITY_SKIP,
    SCAN as SCAN,
    SOURCE_REPAIR as SOURCE_REPAIR,
    SOURCE_REPAIR_CANDIDATE as SOURCE_REPAIR_CANDIDATE,
    _CHECKS as _CHECKS,
    _DEPENDENCY_CODE as _DEPENDENCY_CODE,
    _FAILURE_CODE as _FAILURE_CODE,
    _INTERNAL_PROOF as _INTERNAL_PROOF,
    _STATE_CODE as _STATE_CODE,
    _adoption_artifact_matches as _adoption_artifact_matches,
    _adoption_epoch_expected as _adoption_epoch_expected,
    _artifact_meta as _artifact_meta,
    _cloud_input_binding as _cloud_input_binding,
    _comparison_context as _comparison_context,
    _contract as _contract,
    _digest as _digest,
    _facts_sig as _facts_sig,
    _latest as _latest,
    _out_of_scope as _out_of_scope,
    _published_version as _published_version,
    _quality_assessment as _quality_assessment,
    _quality_code as _quality_code,
    _quality_expected as _quality_expected,
    _quality_kind as _quality_kind,
    _replace as _replace,
    _route_contract as _route_contract,
    _save_source_chunk_tx as _save_source_chunk_tx,
    _scan_sig as _scan_sig,
    _source_quality_hold_key as _source_quality_hold_key,
    _sources_intact as _sources_intact,
    acquire_run_lock as acquire_run_lock,
    options as options,
    time as time,
    workday_window as workday_window
)
from patient_context_quality_receipts import (
    _has_prior_quality_failure as _has_prior_quality_failure,
    _new_summary_repair_response as _new_summary_repair_response,
    _other_summary_repair_spent as _other_summary_repair_spent,
    _profiled_summary_repair_exists as _profiled_summary_repair_exists,
    _quality_answers as _quality_answers,
    _quality_claim as _quality_claim,
    _quality_failed as _quality_failed,
    _quality_failure_allows_remainder as _quality_failure_allows_remainder,
    _quality_integrity as _quality_integrity,
    _quality_mark_pass as _quality_mark_pass,
    _quality_mark_skipped as _quality_mark_skipped,
    _quality_owns as _quality_owns,
    _quality_pass_content as _quality_pass_content,
    _quality_pass_present as _quality_pass_present,
    _quality_passed as _quality_passed,
    _quality_record_failure as _quality_record_failure,
    _quality_release as _quality_release,
    _quality_repair_spent as _quality_repair_spent,
    _quality_reserve_repair_tx as _quality_reserve_repair_tx,
    _quality_retry as _quality_retry,
    _quality_scope_allowed as _quality_scope_allowed,
    _quality_skip_evidence as _quality_skip_evidence,
    _quality_skip_receipt as _quality_skip_receipt,
    _quality_store_answers as _quality_store_answers,
    _repair_response as _repair_response,
    _stored_quality_answers as _stored_quality_answers,
    _summary_repair_response as _summary_repair_response
)
from patient_context_refresh_sources import (
    _ScanConnection as _ScanConnection,
    _ScanCursor as _ScanCursor,
    _ScanInterrupted as _ScanInterrupted,
    _all_facts as _all_facts,
    _candidate_reader as _candidate_reader,
    _coverage as _coverage,
    _current_scans as _current_scans,
    _current_scans_snapshot as _current_scans_snapshot,
    _hydrate_source_body as _hydrate_source_body,
    _quality_capture as _quality_capture,
    _quality_input as _quality_input,
    _scan_check as _scan_check,
    _scan_facts as _scan_facts,
    _scan_read_binding as _scan_read_binding,
    _scan_read_owner as _scan_read_owner,
    _scan_ready as _scan_ready,
    _scan_writer_wait as _scan_writer_wait,
    _scans_from_read as _scans_from_read,
    _snapshot as _snapshot,
    _source_chunk_marker_matches as _source_chunk_marker_matches,
    _source_chunk_rows as _source_chunk_rows,
    _source_chunks as _source_chunks,
    _source_occurrence as _source_occurrence,
    _source_occurrence_disposition as _source_occurrence_disposition,
    _source_quality_capture as _source_quality_capture,
    _source_quality_verified as _source_quality_verified,
    _source_repair_chunks as _source_repair_chunks,
    _source_repair_header as _source_repair_header,
    _source_repair_inputs as _source_repair_inputs,
    _source_repair_parts as _source_repair_parts,
    _source_repair_units as _source_repair_units,
    _source_repair_update_tx as _source_repair_update_tx,
    _summary_valid as _summary_valid,
    _verify_source_repair_rows as _verify_source_repair_rows
)
from patient_context_refresh_readiness import (
    _LANE_CODES as _LANE_CODES,
    _capture_readiness as _capture_readiness,
    _cloud_extraction_ready as _cloud_extraction_ready,
    _context_view_input as _context_view_input,
    _extraction_qc_ready as _extraction_qc_ready,
    _full_write_readiness as _full_write_readiness,
    _inference_coverage as _inference_coverage,
    _inference_ready as _inference_ready,
    _observe_readiness as _observe_readiness,
    _pending as _pending,
    _publication_contract as _publication_contract,
    _quality_publication_allowed as _quality_publication_allowed,
    _raw_extraction_current as _raw_extraction_current,
    _readiness_environment as _readiness_environment,
    _readiness_for_write as _readiness_for_write,
    _ready as _ready,
    _required_cloud_extraction_contract as _required_cloud_extraction_contract,
    _saved_cloud_contract as _saved_cloud_contract,
    _summary_repair_publication as _summary_repair_publication,
    context_view_input as context_view_input,
    context_view_read as context_view_read,
    inference_coverage as inference_coverage,
    lane_metrics as lane_metrics,
    pending_estimate as pending_estimate,
    status as status
)
from patient_context_quality_controller import (
    _ask as _ask,
    _quality_repair_candidate as _quality_repair_candidate,
    _quality_step as _quality_step,
    _scan as _scan,
    _source_quality_repair_step as _source_quality_repair_step,
    _source_repair_seal as _source_repair_seal,
    _summary as _summary
)


def _completed_cloud_receipts(ledger, provider):
    """Recheck the existing paid receipts before retaining or adopting a unit."""
    import cloud_policy
    reservations = provider.completed_reservations
    wanted = {r["reservation_id"]: r for r in reservations}
    rows = ledger.db.execute(
        "SELECT artifact_id FROM artifacts WHERE kind='cloud_usage_reserve' AND "
        "CASE WHEN json_valid(meta) THEN json_extract(meta,'$.reservation_id') END "
        "IN (SELECT value FROM json_each(?))", (json.dumps(sorted(wanted)),)).fetchall()
    evidence = cloud_policy.reservation_evidence_batch(ledger.db, [r[0] for r in rows])
    if (not wanted or len(wanted) != len(reservations) or len(evidence) != len(wanted)
            or {r["reservation"]["reservation_id"] for r in evidence.values()} != set(wanted)
            or any(r["reservation"] != wanted[r["reservation"]["reservation_id"]]
                   for r in evidence.values())):
        raise ValueError("nightly_cloud_receipt_changed")
    return evidence


def run_due(ledger, cfg, *, deadline, admit, complete=None, local_admit=None):
    """Advance one fair idle-lane unit, freezing inputs across inference and commit.

    ``complete(project_id)`` gates legacy whole-patient work. Frozen source
    scopes prove their own post-level inference and acquisition evidence."""
    if ledger.db.in_transaction:
        return {"status": "deferred", "done": 0, "reason": "transaction_active"}
    unit = ledger.__dict__.pop("_patient_context_completed", None)
    resumed = unit is not None
    if unit is None:
        unit = _run_due(ledger, cfg, deadline=deadline, admit=admit,
                        complete=complete, local_admit=local_admit)
    try:
        outcome = unit.send((cfg, deadline, admit, complete)) if resumed else next(unit)
    except StopIteration as finished:
        return finished.value
    except BaseException:
        unit.close()
        raise
    # Only a completed paid A/B unit may yield. No TX, cursor, run lock,
    # request slot or transport is held across this suspension. Ledger.close
    # pops and closes this one unit without running any save/disposition code.
    ledger.__dict__["_patient_context_completed"] = unit
    return outcome


def _advance_patient_scan(ledger, expected, bookmark, deadline, admit):
    import extract_llm

    with patient_context_refresh_sources._scan_writer_wait(ledger.db, deadline):
        try:
            ledger.db.execute('BEGIN IMMEDIATE')
            if (patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.COMMIT_MARGIN >= deadline or not admit()
                    or not extract_llm._writer_current(full=True)
                    or patient_context_refresh_state._latest(ledger.db, patient_context_refresh_state.PROGRESS, 0) != expected):
                ledger.db.rollback()
                return None
            content, meta = expected
            content = {**content, 'patient_scan': bookmark}
            patient_context_refresh_state._replace(ledger, patient_context_refresh_state.PROGRESS, 0, content, meta)
            if not extract_llm._writer_current(full=True):
                ledger.db.rollback()
                return None
            ledger.db.commit()
            return content, patient_context_refresh_state._artifact_meta(patient_context_refresh_state.PROGRESS, meta, None)
        except BaseException:
            ledger.db.rollback()
            raise


def _scan_covered(db, seen):
    return db.execute('SELECT 1 FROM patients WHERE project_id>0 AND project_id NOT IN '
                      '(SELECT value FROM json_each(?)) ORDER BY project_id LIMIT 1',
                      (json.dumps(sorted(seen)),)).fetchone() is None


def _select_patient(ledger, cfg, now, deadline, admit, complete):
    """Advance a bounded scan before evaluating each patient's current evidence."""
    cloud_scan = patient_context_refresh_readiness._cloud_scan_for(ledger, cfg)
    scanned, waiting, seen = 0, 0, set()
    best, selected = {}, None
    rotation = patient_context_refresh_state._latest(ledger.db, patient_context_refresh_state.PROGRESS, 0)
    covered = False
    try:
        with patient_context_refresh_sources._candidate_reader(ledger, deadline) as reader:
            for _ in range(8):
                patient_context_refresh_sources._scan_check(reader.db)
                original = rotation[0].get('patient_scan')
                reserved = context_schedule.advance_turn(original)
                rotation = _advance_patient_scan(ledger, rotation, reserved, deadline, admit)
                if rotation is None:
                    raise patient_context_refresh_sources._ScanInterrupted('patient_context_scan_changed')
                # Expensive activity ordering must not consume the fair turn's chance.
                reader.db.scan_until = min(deadline - patient_context_refresh_state.COMMIT_MARGIN, patient_context_refresh_state.time.monotonic() + 1.0)
                try:
                    plan = context_schedule.plan(reader.db, original)
                finally:
                    reader.db.scan_until = deadline - patient_context_refresh_state.COMMIT_MARGIN
                rotation = _advance_patient_scan(ledger, rotation, plan['next'], deadline, admit)
                if rotation is None:
                    raise patient_context_refresh_sources._ScanInterrupted('patient_context_scan_changed')
                pid = plan['project_id']
                if pid is None or pid in seen:
                    if _scan_covered(reader.db, seen):
                        covered = True
                        break
                    continue
                seen.add(pid)
                scanned += 1
                published = patient_context_refresh_state._published_version(reader.db, pid)
                whole = patient_context_refresh_sources._snapshot(reader.db, pid, cfg, hydrate=False)
                keys = patient_context_refresh_readiness._eligible_source_keys(reader, pid, cfg, whole, deadline=deadline)
                if not keys and whole['sources']:
                    waiting += 1
                    continue
                source_keys = list(keys) if keys else None
                snapshot, progress, pending = patient_context_refresh_readiness._pending(
                    reader.db, pid, cfg, now=now, cloud_scan=cloud_scan(pid), source_keys=source_keys)
                if patient_context_refresh_state._out_of_scope(snapshot):
                    continue
                scopes = [(snapshot, progress, pending, source_keys, ('B', 'C'))]
                quality_required = bool(snapshot['contract'].get('provider')
                    or patient_context_quality_receipts._has_prior_quality_failure(reader.db, pid))
                scans = patient_context_refresh_sources._current_scans(reader.db, pid, snapshot['sources'],
                    snapshot['contract'], cfg=cfg) if quality_required else {}
                summary_keys = [context_plan.source_key(source) for source in snapshot['sources']
                    if not quality_required or not context_plan.source_usable(source)
                    or patient_context_refresh_sources._scan_ready(scans.get(context_plan.source_key(source), {}))]
                if summary_keys or not snapshot['sources']:
                    summary_keys = summary_keys or None
                    if summary_keys == source_keys:
                        scopes[0] = (snapshot, progress, pending, source_keys, ('A', 'B', 'C'))
                    else:
                        summary_snapshot, summary_progress, summary_pending = patient_context_refresh_readiness._pending(
                            reader.db, pid, cfg, now=now, cloud_scan=cloud_scan(pid), source_keys=summary_keys)
                        scopes.append((summary_snapshot, summary_progress, summary_pending, summary_keys, ('A',)))
                for candidate, state, due, scope_keys, lanes in scopes:
                    changes = ledger.db.total_changes
                    ready, proof, _ = patient_context_refresh_readiness._observe_readiness(reader, pid, cfg,
                        deadline=deadline, complete=complete, allow_dependency_repair=lanes != ('A',), source_keys=scope_keys)
                    if ledger.db.total_changes != changes:
                        proof = None
                    if proof is not None:
                        proof['writer'] = ledger.db
                        proof['writer_changes'] = ledger.db.total_changes
                    candidate['_readiness_proof'] = proof
                    patient_context_refresh_sources._scan_check(reader.db)
                    if not candidate['dependencies']['complete']:
                        due['A'] = False
                    waiting += int(candidate['unfinished'] or not ready)
                    if ready:
                        best.update({lane: (candidate, state, published) for lane in lanes if due[lane]})
                if best:
                    selected = pid
                    break
                if _scan_covered(reader.db, seen):
                    covered = True
                    break
    except (sqlite3.Error, OSError, patient_context_refresh_sources._ScanInterrupted):
        best, selected, covered = {}, None, False
    except ValueError as exc:
        if exc.args != ('patient_scan_bookmark_invalid',) and exc.args != ('patient_scan_source_invalid',):
            raise
        best, selected, covered = {}, None, False
    return best, selected, rotation, scanned, waiting, covered


def _run_due(ledger, cfg, *, deadline, admit, complete=None, local_admit=None):
    """Keep one completed unit's existing save path resumable within its deadline."""
    import extract_llm
    import cloud_inference
    import cloud_client
    import cloud_policy
    if ledger.db.in_transaction:
        return {"status": "deferred", "done": 0, "reason": "transaction_active"}
    local_admit = local_admit or admit
    now = patient_context_refresh_state.time.time()
    window = patient_context_refresh_state.workday_window(now)
    if not patient_context_refresh_state.options(cfg)["enabled"] or not window:
        return {"status": "off", "done": 0}
    night_id, _ = window
    if patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.COMMIT_MARGIN >= deadline or not admit():
        return {"status": "deferred", "done": 0}
    started = patient_context_refresh_state.time.monotonic()
    best, pid, scan_rotation, scanned, waiting, covered = _select_patient(
        ledger, cfg, now, deadline, admit, complete)
    stats = {"scanned": scanned, "scan_ms": int((patient_context_refresh_state.time.monotonic() - started) * 1000)}
    if scan_rotation is None:
        return {"status": "deferred", "done": 0, "reason": "patient_context_scan_incomplete", **stats}
    rotation = scan_rotation[0]
    lane, index, next_cursor = context_plan.lane_rotation(best, rotation)
    if lane is None:
        return {"status": "deferred" if waiting or not covered else "complete", "done": 0,
                **({"reason": "patient_context_waiting" if waiting else "patient_context_scan_incomplete"}
                   if waiting or not covered else {}), **stats}
    snapshot, progress, frozen_ver = best[lane]
    source_keys = snapshot.get('source_scope')
    selected_proof = snapshot.pop("_readiness_proof", None)
    # Like extraction claims, scheduling is fenced by SQLite, not the tick's run lock.
    try:
        with patient_context_refresh_sources._scan_writer_wait(ledger.db, deadline):
            ledger.db.execute("BEGIN IMMEDIATE")
            ready, actual, _ = patient_context_refresh_readiness._readiness_for_write(ledger, pid, cfg, selected_proof,
                deadline=deadline, complete=complete, allow_dependency_repair=lane != "A", source_keys=source_keys)
            current = (patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.COMMIT_MARGIN < deadline and admit()
                and extract_llm._writer_current(full=True)
                and patient_context_refresh_state._published_version(ledger.db, pid) == frozen_ver
                and patient_context_refresh_state._latest(ledger.db, patient_context_refresh_state.PROGRESS, 0) == scan_rotation
                and ready == "verified" and actual is not None and actual['source_sig'] == snapshot['source_sig'])
            if not current:
                ledger.db.rollback()
                changed = actual is not None and actual['source_sig'] != snapshot['source_sig']
                return {"status": "source_changed" if changed else "deferred", "done": 0,
                        **({"reason": "patient_context_readiness_unverified"} if not changed else {}), **stats}
            if lane != "A":
                # B/C may use a wider cohort than the last overview attempt.
                # Keep that attempt's retry state bound to its original input.
                progress, _ = patient_context_refresh_state._latest(
                    ledger.db, patient_context_refresh_state.PROGRESS, pid)
            progress = {**progress, "last_attempt": now,
                        "last_attempt_unit": rotation.get("units", 0) + 1,
                        "night_id": night_id,
                        **({"source_sig": snapshot["source_sig"], "source_scope": source_keys} if lane == "A" else {})}
            patient_context_refresh_state._replace(ledger, patient_context_refresh_state.PROGRESS, pid, progress, {})
            patient_context_refresh_state._replace(ledger, patient_context_refresh_state.PROGRESS, 0, {**rotation, "cursor": next_cursor, "units": rotation.get("units", 0) + 1}, scan_rotation[1])
            if not extract_llm._writer_current(full=True):
                ledger.db.rollback()
                return {"status": "source_changed", "done": 0, **stats}
            ledger.db.commit()
        # Our scheduling write participates in the same publisher fence as later work.
        frozen_ver = patient_context_refresh_state._published_version(ledger.db, pid)
    except (sqlite3.Error, OSError, patient_context_refresh_sources._ScanInterrupted):
        ledger.db.rollback()
        return {"status": "deferred", "done": 0, "reason": "patient_context_scan_incomplete", **stats}
    except BaseException:
        ledger.db.rollback()
        raise
    if lane == "A":
        # Facts need hydrated items; the hydrated read must still match the
        # selection-time signature the reused progress reset came from.
        hydrated = patient_context_refresh_sources._snapshot(ledger.db, pid, cfg, source_keys=source_keys)
        if hydrated["source_sig"] != snapshot["source_sig"]:
            return {"status": "source_changed", "done": 0, **stats}
        snapshot = hydrated
    frozen_scan_sig = patient_context_refresh_state._scan_sig(ledger.db, pid, source_keys=source_keys)
    meta = {"source_sig": snapshot["source_sig"], "contract": snapshot["contract"],
            "model": snapshot["contract"]["model"], "night_id": night_id}
    if lane == 'A' and source_keys is not None:
        meta.update(source_scope=source_keys, source_coverage=snapshot['source_coverage'])
    result = None
    failure = None
    deferred_reason = None
    provider = None
    mode = extract_llm._cloud_mode(ledger, cfg, "patient_context", pid)
    cloud_requested = bool(snapshot["contract"].get("provider"))
    quality_required = cloud_requested or patient_context_quality_receipts._has_prior_quality_failure(ledger.db, pid)
    cloud_settings = cloud_inference.resolve_cloud_settings(cfg) if mode in ("on", "shadow", "dry_run") else None
    cloud_selection = None
    if cloud_settings is not None:
        cloud_selection, errors = cloud_policy.resolve_profile(
            cfg, route="patient_context", purpose="patient_context", root=extract_llm.HOME)
        if errors or cloud_selection is None:
            return {"status": "source_changed", "done": 0, **stats}
    estimated_unit = False
    shadow_call = None
    shadow_unit = None
    quality_identity = None
    quality_keys = None
    source_quality_identity = None
    source_quality_keys = None
    quality_skipped = False
    source_quality_skipped = False
    write_proof = [selected_proof]
    readiness_state = ["verified"]
    selected_source = [None]
    selected_occurrence = [None]
    cloud_input = patient_context_refresh_state._cloud_input_binding(ledger.db, pid, snapshot) if cloud_settings is not None else None

    def current(*, after_commit=False):
        readiness_state[0] = 'unknown'
        try:
            if ledger.db.in_transaction:
                ready, latest, write_state = patient_context_refresh_readiness._readiness_for_write(ledger, pid, cfg, write_proof[0], deadline=deadline,
                    complete=complete, allow_dependency_repair=lane != "A", source_keys=source_keys)
                readiness_state[0] = ready
                if ready != 'verified' or latest is None:
                    if latest is not None and latest['source_sig'] != snapshot['source_sig']:
                        readiness_state[0] = 'changed'
                    return False
            else:
                write_state = None
                latest = patient_context_refresh_sources._snapshot(ledger.db, pid, cfg, hydrate=False, source_keys=source_keys)
                ready = patient_context_refresh_readiness._inference_ready(ledger, pid, cfg, deadline=deadline, complete=complete,
                                         allow_dependency_repair=lane != "A", source_keys=source_keys)
                readiness_state[0] = 'verified' if ready else 'unknown'
                if latest['source_sig'] != snapshot['source_sig']:
                    readiness_state[0] = 'changed'
                ready = 'verified' if ready else 'unknown'
            actual = next((row for row in latest["sources"] if selected_source[0] is not None
                           and context_plan.source_key(row) == context_plan.source_key(selected_source[0])), None)
            cached = (write_state is not None and write_state['cloud'] == (cloud_input is not None)
                and write_state['source_key'] == (context_plan.source_key(selected_source[0]) if selected_source[0] is not None else None))
            unchanged = (latest["source_sig"] == snapshot["source_sig"]
                    and (cloud_input is None or (write_state['cloud_input'] if cached
                         else patient_context_refresh_state._cloud_input_binding(ledger.db, pid, latest)) == cloud_input)
                    and ready == "verified"
                    and (after_commit or (write_state['scan_sig'] if cached else patient_context_refresh_state._scan_sig(ledger.db, pid, source_keys=source_keys)) == frozen_scan_sig
                         and patient_context_refresh_state._published_version(ledger.db, pid) == frozen_ver)
                    and (selected_source[0] is None or actual is not None
                         and (write_state['occurrence'] if cached else patient_context_refresh_state._digest(patient_context_refresh_sources._source_occurrence(ledger.db, pid, actual)))
                             == patient_context_refresh_state._digest(selected_occurrence[0])))
            if not unchanged:
                if ready == 'verified':
                    readiness_state[0] = 'changed'
                return False
            if not admit():
                readiness_state[0] = 'unknown'
                return False
            return True
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError,
                RuntimeError, OverflowError, TimeoutError, patient_context_refresh_sources._ScanInterrupted, cloud_inference.CloudFallback):
            readiness_state[0] = 'unknown'
            return False

    def cloud_for(unit_key, mid=None, *, after_commit=False):
        def provider_current():
            try:
                allowed = current(after_commit=after_commit)
            except Exception:
                raise cloud_inference.CloudObservationIncomplete('patient_context_readiness_unverified') from None
            if allowed:
                return True
            if readiness_state[0] != 'changed':
                raise cloud_inference.CloudObservationIncomplete('patient_context_readiness_unverified')
            return False

        return cloud_inference.CloudInferenceUnit(ledger, cfg, route="patient_context", project_id=pid,
            message_id=mid, unit_key=unit_key, deadline=deadline,
            current=provider_current,
            dispatch=lambda fn, **kw: fn(), owner={"night_id": night_id, "source_sig": snapshot["source_sig"]},
            selection=cloud_selection, input_binding=cloud_input)

    def guard_provider(unit):
        readiness_state[0] = 'not_checked'
        unit.commit_guard()

    def discard_if_changed():
        if provider is not None:
            if not current():
                if readiness_state[0] == 'changed':
                    provider.discard()
                return
            try:
                guard_provider(provider)
            except cloud_inference.CloudObservationIncomplete:
                return
            except cloud_inference.CloudFallback:
                provider.discard()

    def local_route():
        # the unit's record is written under the local backend; a cloud route
        # contract is kept beside it so the selection still recognises the row
        meta.pop("provider", None)
        if "provider" in snapshot["contract"]:
            meta["route_contract"] = snapshot["contract"]
            meta["contract"] = {key: value for key, value in snapshot["contract"].items() if key != "provider"}
        meta["model"] = local_llm.resolve(cfg)[1]

    def infer(fn, *, unit_key, mid=None, saved=None):
        nonlocal provider, shadow_call, estimated_unit
        # A local cursor cannot stand in for cloud inspection of the original.
        use_cloud = mode == "on"
        local_saved = dict(saved) if saved and not saved.get("_provider_contract") else None
        if use_cloud:
            provider = cloud_for(unit_key, mid)
            try:
                reset = bool(saved and not saved.get("_provider_contract"))
                if reset:
                    saved.clear()
                value = fn(provider, reset)
                guard_provider(provider)
                meta["provider"] = provider.contract
                meta["model"] = provider.model
                return value
            except cloud_client.CloudNotSent as error:
                # nothing left the wire: defer the unit — it must not take a
                # local slot or re-send next tick as if the refusal never ran
                raise patient_context_refresh_state.Deferred("nightly_cloud_not_sent") from error
            except cloud_inference.CloudObservationIncomplete as error:
                raise patient_context_refresh_state.Deferred('patient_context_readiness_unverified') from error
            except (cloud_inference.CloudFallback, ValueError) as error:
                changed = isinstance(error, cloud_inference.CloudFallback) and str(error) == "cloud_unit_changed"
                paid = provider.possibly_sent
                unresolved = getattr(provider, 'unresolved', paid)
                if changed:
                    provider.discard()
                    if paid:
                        meta['provider'] = provider.contract
                        meta['model'] = provider.model
                    provider = None
                    if paid:
                        raise ValueError('nightly_cloud_failed') from error
                    raise patient_context_refresh_state.Deferred("nightly_cloud_source_changed") from error
                if (str(error) == "cloud_send_blocked" and saved and saved.get("_provider_contract")):
                    # A fresh unsent remainder does not release a persisted cloud cursor.
                    raise patient_context_refresh_state.Deferred("nightly_cloud_budget") from error
                if paid:
                    meta["provider"] = provider.contract
                    meta["model"] = provider.model
                provider = None
                if paid and (unresolved or not local_admit()):
                    # a paid attempt failed and local cannot take over now: record
                    # the retry instead of re-sending the same unit every tick.
                    # Partial cloud progress keeps its cloud pin (a local record
                    # would resume the cloud cursor locally); a fresh unit is
                    # recorded under the local route, where selection finds it.
                    if saved and saved.get("_provider_contract"):
                        meta["provider"] = saved["_provider_contract"]
                        meta["model"] = saved["_provider_contract"]["model"]
                    elif not cloud_requested:
                        local_route()
                    if isinstance(error, ValueError):
                        raise
                    raise ValueError("nightly_cloud_failed") from error
        if local_saved:
            saved.update(local_saved)
            if saved.get("complete"):
                raise patient_context_refresh_state.Deferred("nightly_cloud_pending")
        if not local_admit():
            raise patient_context_refresh_state.Deferred("nightly_local_capacity")
        local_route()
        reset = bool(saved and saved.get("_provider_contract"))
        if reset:
            saved.clear()
        value = fn(None, reset)
        estimated_unit = mode == "dry_run"
        if mode == "shadow":
            shadow_call = (fn, unit_key, mid, value, set())
        return value
    try:
        if lane == "A":
            facts, total, projected = patient_context_refresh_sources._all_facts(ledger.db, pid, snapshot, night_id, projected=True, cfg=cfg)
            old, old_meta = patient_context_refresh_state._latest(ledger.db, patient_context_refresh_state.KIND, pid)
            changed = patient_context_refresh_state._facts_sig(facts)
            summary_scans = patient_context_refresh_sources._current_scans(
                ledger.db, pid, snapshot["sources"], snapshot["contract"], cfg=cfg)
            cloud_sources_ready = all(scan.get("_provider_contract") for scan in summary_scans.values())
            same_backend = (mode != "on" or old_meta.get("provider") == snapshot["contract"].get("provider")
                            or not cloud_sources_ready and not old_meta.get("provider"))
            same = (old.get("facts_sig") == changed and old_meta.get("source_sig") == snapshot["source_sig"]
                    and patient_context_refresh_state._route_contract(old_meta) == snapshot["contract"]
                    and same_backend)
            if (same_backend and old_meta.get("quality_audit_key") and old_meta.get("source_sig") == snapshot["source_sig"]
                    and old_meta.get("scan_sig") == frozen_scan_sig and patient_context_refresh_state._route_contract(old_meta) == snapshot["contract"]
                    and patient_context_refresh_sources._summary_valid(ledger.db, pid, old, {context_plan.source_key(s): s for s in snapshot["sources"]},
                                       allow_historical=True)):
                same = True
            if same:
                meta["contract"] = old_meta["contract"]
                if "route_contract" in old_meta:
                    meta["route_contract"] = old_meta["route_contract"]
                if "quality_repair_ref" in old_meta:
                    meta["quality_repair_ref"] = old_meta["quality_repair_ref"]
            if same and quality_required:
                if old_meta.get("provider"):
                    meta["provider"] = old_meta["provider"]
                    meta["model"] = old_meta["provider"]["model"]
                else:
                    meta['model'] = old_meta['model']
                try:
                    quality_status, quality_result = patient_context_quality_controller._quality_step(
                        ledger, pid, cfg, night_id, old, deadline, admit, base_meta=old_meta)
                    repaired = None
                    if quality_status == "candidate":
                        if quality_result.get("repair_ref") is not None:
                            meta["quality_repair_ref"] = quality_result["repair_ref"]
                        by_id = {fact["id"]: fact for fact in quality_result["facts"]}
                        chosen = [by_id[fid] for fid in quality_result["fact_ids"]]
                        chosen_ids = {fact["id"] for fact in chosen}
                        selected_facts = (chosen + [fact for fact in facts if fact["id"] not in chosen_ids])[:context_plan.MAX_FACTS]
                        repaired = {**old, "facts": selected_facts, "facts_sig": patient_context_refresh_state._facts_sig(selected_facts),
                            "summary": [{"text": context_plan.summary_text(fact), "fact_ids": [fact["id"]]}
                                        for fact in chosen],
                            "temporal_view": {key: quality_result["state"][key] for key in ("groups", "relations")},
                            "facts_total": len(quality_result["facts"]),
                            "truncated": len(quality_result["facts"]) > len(selected_facts)}
                        quality_status, quality_result = patient_context_quality_controller._quality_step(
                            ledger, pid, cfg, night_id, repaired, deadline, admit, base_doc=old, base_meta=old_meta)
                except (patient_context_refresh_state.Deferred, ValueError, RuntimeError):
                    raise patient_context_refresh_state.Deferred("summary_quality_unavailable") from None
                if quality_status not in ("passed", "skipped"):
                    raise patient_context_refresh_state.Deferred("summary_quality_" + quality_status)
                quality_keys = quality_result.pop("_keys")
                quality_skipped = quality_status == "skipped"
                quality_result.pop("_skip_reason", None)
                quality_identity = quality_result
                if repaired is not None:
                    facts, total = repaired["facts"], repaired["facts_total"]
                    changed = repaired["facts_sig"]
                    projected = repaired["temporal_view"]
                import semantic_policy
                quality_policy, problems = semantic_policy.runtime_config(cfg)
                if problems or not patient_context_quality_receipts._quality_scope_allowed(ledger.db, pid, quality_policy, snapshot["sources"]):
                    raise patient_context_refresh_state.Deferred("summary_quality_promotion")
                meta["quality_audit_key"] = quality_identity["audit_key"]
                if quality_skipped:
                    meta["quality_audit_status"] = "SKIPPED"
                else:
                    meta.pop("quality_audit_status", None)
                meta["quality_schedule_key"] = patient_context_refresh_state._digest([cfg, quality_identity["assessment"]["code"]])
            summary = (repaired["summary"] if same and quality_required and repaired is not None
                       else old["summary"] if same else infer(
                lambda backend, reset, metrics=None, comparison=False: patient_context_quality_controller._summary(cfg, facts, deadline,
                    admit if backend else local_admit, source_sig=snapshot["source_sig"], **({"provider": backend} if backend else {}),
                    **({"metrics": metrics} if metrics is not None else {})),
                unit_key="summary"))
            meta["scan_backends"] = sorted({
                (scan.get("_provider_contract") or {}).get("backend", "local")
                for scan in summary_scans.values()})
            result = {"version": 1, "facts": facts, "facts_sig": changed, "summary": summary,
                      "temporal_view": {key: projected[key] for key in ("groups", "relations")},
                      "facts_total": total, "truncated": total > len(facts),
                      "as_of": now, "generated_at": old.get("generated_at", now) if same else now,
                      "source_sig": snapshot["source_sig"],
                      "coverage": patient_context_refresh_sources._coverage(ledger.db, pid, snapshot["sources"], snapshot["contract"], facts, cfg=cfg)}
        else:
            scans = patient_context_refresh_sources._current_scans(ledger.db, pid, snapshot["sources"], snapshot["contract"], cfg=cfg)
            for source in snapshot["sources"]:
                if not context_plan.source_usable(source):
                    continue
                saved = scans.get(context_plan.source_key(source), {})
                cloud_pending = lane == "B" and mode == "on" and not saved.get("_provider_contract")
                if not patient_context_refresh_readiness._ready({} if cloud_pending else saved, now):
                    continue
                if (lane == "B" and (not saved.get("complete") or cloud_pending)) or (
                        lane == "C" and saved.get("complete") and not saved.get("audited")):
                    meta["source_sig"] = context_plan.source_signature(source)
                    meta["original_source_sig"] = context_plan.original_source_signature(source)
                    meta["source_key"] = context_plan.source_key(source)
                    source = next(context_queries.patient_context_sources(ledger.db, pid, mid=context_plan.source_key(source), cfg=cfg), None)
                    if source is None:
                        return {"status": "source_changed", "done": 0}
                    selected_source[0] = source
                    selected_occurrence[0] = patient_context_refresh_sources._source_occurrence(ledger.db, pid, source)
                    if lane == 'C' and isinstance(saved.get('_source_meta'), dict):
                        prior_meta = saved['_source_meta']
                        for key in ('contract', 'route_contract', 'provider', 'model', 'source_binding',
                                    'source_occurrence', 'source_occurrence_version'):
                            if key in prior_meta:
                                meta[key] = prior_meta[key]
                            else:
                                meta.pop(key, None)
                    if lane == "B":
                        # A legacy partial checkpoint starts the new storage
                        # occurrence at zero; retained legacy chunks are not lost.
                        if saved.get("source_occurrence_version") != 1:
                            saved = {key:value for key,value in saved.items() if key.startswith("_")}
                        saved.update(source_occurrence_version=1, source_occurrence=selected_occurrence[0])
                        meta.update(source_occurrence_version=1, source_occurrence=selected_occurrence[0],
                                    source_binding=selected_occurrence[0]["source_binding"])
                        planned, planned_cursor, _, _, _ = context_plan.scan_plan(source, saved)
                        planned_span = [sum(map(len, planned[:planned_cursor])), sum(map(len, planned[:planned_cursor + 1]))]
                        prior_unit = patient_context_refresh_state._digest([meta["source_sig"], planned_span, meta["contract"],
                                              snapshot["contract"].get("provider"), selected_occurrence[0]])
                        del planned
                        if ledger.db.execute("SELECT 1 FROM artifacts WHERE kind=? AND project_id=? AND "
                                + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                                + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.unit_key') END=? LIMIT 1",
                                (patient_context_refresh_state.CHUNK, pid, prior_unit)).fetchone():
                            # A past equal input is not a new dispatch right. A
                            # lost checkpoint cannot infer a completed cursor.
                            raise ValueError("nightly_original_unit_unverified")
                        result = infer(lambda backend, reset, metrics=None, comparison=False: patient_context_quality_controller._scan(cfg, source,
                            {} if reset else {**saved, "facts": []} if comparison else saved, night_id, deadline,
                            admit if backend else local_admit,
                            **({"provider": backend} if backend else {}),
                            **({"metrics": metrics} if metrics is not None else {})),
                            unit_key="scan:" + str(context_plan.source_key(source)), mid=source["message_id"] or None, saved=saved)
                        result.update(source_occurrence_version=1, source_occurrence=selected_occurrence[0])
                    else:
                        result = {**saved, "audited": True, "audited_chunks_sig": saved["_chunks_sig"]}
                        if not saved.get("_provider_contract") and "provider" in snapshot["contract"]:
                            meta["route_contract"] = snapshot["contract"]
                            meta["contract"] = {key: value for key, value in snapshot["contract"].items() if key != "provider"}
                        if saved.get("_provider_contract"):
                            meta["provider"] = saved["_provider_contract"]
                            meta["model"] = saved["_provider_contract"]["model"]
                        # Reconstruct all quotations, refusing stored polarity or
                        # subject rewrites; confidence scores are not evidence.
                        audited_ids = []
                        for part in patient_context_refresh_sources._source_chunks(ledger.db, pid, source, snapshot["contract"], saved):
                            if len(context_plan.validated_source_facts(source, part.get("facts"))) != len(part.get("facts", [])):
                                raise ValueError("nightly_audit_mismatch")
                            audited_ids.append(part["_artifact_id"])
                        result["_audited_chunk_ids"] = audited_ids
                        if cloud_requested or patient_context_quality_receipts._has_prior_quality_failure(ledger.db, pid, purpose='source'):
                            try:
                                quality_status, quality_result = patient_context_quality_controller._quality_step(
                                    ledger, pid, cfg, night_id, {}, deadline, admit, source=source)
                            except (patient_context_refresh_state.Deferred, ValueError, RuntimeError):
                                result = None
                                raise patient_context_refresh_state.Deferred("source_quality_unavailable") from None
                            if quality_status == "source_candidate":
                                quality_status, quality_result = patient_context_quality_controller._quality_step(
                                    ledger, pid, cfg, night_id, {}, deadline, admit, source=source,
                                    source_candidate_ref=quality_result)
                            if quality_status not in ("passed", "skipped"):
                                result = None
                                raise patient_context_refresh_state.Deferred("source_quality_" + quality_status)
                            source_quality_keys = quality_result.pop("_keys")
                            source_quality_skipped = quality_status == "skipped"
                            quality_result.pop("_skip_reason", None)
                            accepted = quality_result.pop("_candidate_ref", None)
                            result["audited_chunks_sig"] = quality_result.pop("_source_chunks_sig")
                            if accepted is not None:
                                result["accepted_candidate_ref"] = accepted
                                result["source_occurrence_version"] = 1
                                result["source_occurrence"] = patient_context_refresh_sources._source_occurrence(ledger.db, pid, source)
                                staged = patient_context_refresh_sources._source_repair_chunks(ledger.db, pid, source, snapshot["contract"], accepted)
                                result["_audited_chunk_ids"] = [part["_artifact_id"] for part in staged]
                                result["facts"] = [fact for part in staged for fact in part["facts"]][:context_plan.MAX_SOURCE_FACTS]
                                result["_chunks_sig"] = accepted["chunks_sig"]
                            source_quality_identity = quality_result
                            result["source_quality_audit_key"] = source_quality_identity["audit_key"]
                            if source_quality_skipped:
                                result["audited"] = False
                                result["source_quality_state"] = "skipped"
                            else:
                                result.pop("source_quality_state", None)
                            result.pop("quality_state", None)
                            result.pop("quality_hold_key", None)
                    break
    except patient_context_refresh_state.Deferred as exc:
        discard_if_changed()
        deferred_reason = str(exc) or "patient_context_waiting"
    except (ValueError, TypeError, KeyError, TimeoutError) as exc:
        code = str(exc)
        failure = code if type(exc) is ValueError and patient_context_refresh_state._FAILURE_CODE.fullmatch(code) else type(exc).__name__
    if result is None and failure is None and deferred_reason is None:
        return {"status": "deferred", "done": 0}
    if patient_context_refresh_state.time.monotonic() >= deadline or not admit():
        discard_if_changed()
        return {"status": "deferred", "done": 0}
    # Rebuild the source signature outside the write lock. A data-version
    # fence seals the read against any concurrent writer before publication.
    quality_fence = None
    source_pass_content = None
    if source_quality_identity is not None:
        before = (ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes)
        ledger.db.execute("BEGIN")
        try:
            if not patient_context_refresh_sources._source_quality_verified(ledger.db, pid, source, snapshot["contract"], cfg, result):
                return {"status": "source_changed", "done": 0}
            source_pass_content = (patient_context_quality_receipts._quality_pass_content(ledger.db, pid, source_quality_identity)
                                   if not source_quality_skipped else None)
            if not source_quality_skipped and source_pass_content is None:
                return {"status": "source_changed", "done": 0}
        finally:
            ledger.db.rollback()
        quality_fence = (ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes)
        if quality_fence != before:
            return {"status": "source_changed", "done": 0}
    if quality_identity is not None:
        before = (ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes)
        ledger.db.execute("BEGIN")
        try:
            refreshed_quality = patient_context_refresh_sources._quality_capture(ledger.db, pid, cfg, night_id, result["summary"],
                                                  quality_identity["assessment"], result["temporal_view"], source_keys=source_keys)
            if refreshed_quality["identity"] != quality_identity:
                return {"status": "source_changed", "done": 0}
        except (ValueError, KeyError, TypeError):
            return {"status": "source_changed", "done": 0}
        finally:
            ledger.db.rollback()
        quality_fence = (ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes)
        if quality_fence != before:
            return {"status": "source_changed", "done": 0}
    version = ledger.db.execute("PRAGMA data_version").fetchone()[0]
    changes = ledger.db.total_changes
    if quality_fence is not None and quality_fence != (version, changes):
        return {"status": "source_changed", "done": 0}
    _, write_proof[0], ready_snapshot = patient_context_refresh_readiness._capture_readiness(ledger, pid, cfg, deadline=deadline,
        complete=complete, allow_dependency_repair=lane != "A", source_keys=source_keys,
        write_scope={'source_key': context_plan.source_key(selected_source[0]) if selected_source[0] is not None else None,
                     'cloud': cloud_input is not None})
    fresh = ready_snapshot or patient_context_refresh_sources._snapshot(ledger.db, pid, cfg, hydrate=False, source_keys=source_keys)
    scans_now = patient_context_refresh_state._scan_sig(ledger.db, pid, source_keys=source_keys)
    # A finished inference is worth a short wait: the tick and lane 2 hold
    # the run lock for minutes, and dropping the result would send the
    # same prompt again on the next iteration.
    waited = 0
    completed_receipts = None
    completed_cfg = None
    while (fd := patient_context_refresh_state.acquire_run_lock()) is None:
        retainable = (lane in ("A", "B") and failure is None and deferred_reason is None
                      and result is not None and provider is not None
                      and bool(getattr(provider, "completed_reservations", ())))
        if retainable and completed_receipts is None:
            try:
                completed_receipts = _completed_cloud_receipts(ledger, provider)
            except ValueError:
                return {"status": "source_changed", "done": 0}
            completed_cfg = copy.deepcopy(cfg)
        # the cap, the unit deadline and a withdrawn
        # admission (updater, config, breaker, disk) all end the wait
        if waited >= 60 or patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.COMMIT_MARGIN >= deadline or not admit():
            if (retainable and waited >= 60 and patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.COMMIT_MARGIN < deadline
                    and admit()):
                resume_cfg, resume_deadline, admit, complete = yield {
                    "status": "deferred", "done": 0, "reason": "run_lock", "lock_wait_s": waited}
                # A new tick grants time to save, never more time to keep this
                # old response. Expiry drops only the in-memory body; its paid
                # receipt remains and cannot authorize another POST.
                deadline = min(deadline, resume_deadline)
                if patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.COMMIT_MARGIN >= deadline:
                    return {"status": "deferred", "done": 0, "reason": "nightly_completed_expired"}
                import notify_spool
                if (not admit() or not extract_llm._writer_current(full=True)
                        or not extract_llm._same_but_operating_limits(resume_cfg, completed_cfg)
                        or notify_spool.restore_writer_hold(notify_spool.data_root(ledger)) is not None):
                    return {"status": "source_changed", "done": 0}
                try:
                    guard_provider(provider)
                    if _completed_cloud_receipts(ledger, provider) != completed_receipts:
                        raise ValueError("nightly_cloud_receipt_changed")
                except cloud_inference.CloudObservationIncomplete:
                    return {"status": "deferred", "done": 0, "reason": "patient_context_readiness_unverified"}
                except cloud_inference.CloudFallback:
                    discard_if_changed()
                    return {"status": "source_changed", "done": 0}
                except ValueError:
                    return {"status": "source_changed", "done": 0}
                version = ledger.db.execute("PRAGMA data_version").fetchone()[0]
                changes = ledger.db.total_changes
                fresh = patient_context_refresh_sources._snapshot(ledger.db, pid, cfg, hydrate=False, source_keys=source_keys)
                scans_now = patient_context_refresh_state._scan_sig(ledger.db, pid, source_keys=source_keys)
                waited = 0
                continue
            discard_if_changed()
            return {"status": "deferred", "done": 0, "reason": "run_lock", "lock_wait_s": waited}
        patient_context_refresh_state.time.sleep(1)
        waited += 1
    shadow_unit = None
    try:
        if not admit():
            discard_if_changed()
            return {"status": "deferred", "done": 0}
        # BEGIN IMMEDIATE seals the signature check and writes in one brief
        # transaction; no inference is performed while holding a DB/run lock.
        ledger.db.execute("BEGIN IMMEDIATE")
        try:
            if (quality_identity is not None or source_quality_identity is not None) and (ledger.db.execute("PRAGMA data_version").fetchone()[0] != version
                                                 or ledger.db.total_changes != changes):
                ledger.db.rollback()
                return {"status": "source_changed", "done": 0}
            if quality_identity is not None:
                live_cfg = extract_llm.load_config()
                live_policy, problems = semantic_policy.runtime_config(live_cfg)
                import semantic_jev
                try:
                    repair_verified = patient_context_refresh_readiness._summary_repair_publication(
                        ledger.db, pid, cfg, result, meta, refreshed_quality,
                        quality_identity['assessment'], snapshot['contract'], live_policy['match_threshold'])
                except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, RuntimeError):
                    repair_verified = False
                if (problems or not patient_context_quality_receipts._quality_scope_allowed(ledger.db, pid, live_policy, fresh["sources"])
                        or quality_keys != (semantic_jev.api_key(), cloud_policy.selected_key(extract_llm.HOME, live_cfg))
                        or quality_identity["assessment"] != patient_context_refresh_state._quality_assessment(live_policy)
                        or (not patient_context_quality_receipts._quality_skip_receipt(ledger.db, pid, quality_identity,
                                {row["fact_ids"][0] for row in result["summary"]}, live_policy["match_threshold"])
                            if quality_skipped else not patient_context_quality_receipts._quality_passed(ledger.db, pid, quality_identity))
                        or patient_context_quality_receipts._quality_failed(ledger.db, pid, quality_identity) or not repair_verified):
                    ledger.db.rollback()
                    return {"status": "source_changed", "done": 0}
            if source_quality_identity is not None:
                import semantic_policy
                live_cfg = extract_llm.load_config()
                live_policy, problems = semantic_policy.runtime_config(live_cfg)
                import semantic_jev
                if (problems or not patient_context_quality_receipts._quality_scope_allowed(ledger.db, pid, live_policy, fresh["sources"])
                        or source_quality_keys != (semantic_jev.api_key(), cloud_policy.selected_key(extract_llm.HOME, live_cfg))
                        or source_quality_identity["assessment"] != patient_context_refresh_state._quality_assessment(live_policy)
                        or (not patient_context_quality_receipts._quality_skip_receipt(ledger.db, pid, source_quality_identity,
                                set(), live_policy["match_threshold"]) if source_quality_skipped else
                            not patient_context_quality_receipts._quality_passed(ledger.db, pid, source_quality_identity, expected=source_pass_content))
                        or patient_context_quality_receipts._quality_failed(ledger.db, pid, source_quality_identity)):
                    ledger.db.rollback()
                    return {"status": "source_changed", "done": 0}
            if provider is not None:
                if completed_receipts is not None:
                    import notify_spool
                    if (not extract_llm._writer_current(full=True)
                            or notify_spool.restore_writer_hold(notify_spool.data_root(ledger)) is not None):
                        ledger.db.rollback()
                        return {"status": "source_changed", "done": 0}
                try:
                    guard_provider(provider)
                    if completed_receipts is not None:
                        if _completed_cloud_receipts(ledger, provider) != completed_receipts:
                            raise cloud_inference.CloudFallback("cloud_unit_changed")
                except cloud_inference.CloudObservationIncomplete:
                    ledger.db.rollback()
                    return {"status": "deferred", "done": 0, "reason": "patient_context_readiness_unverified"}
                except cloud_inference.CloudFallback:
                    ledger.db.rollback()
                    if (completed_receipts is None or extract_llm._writer_current(full=True)
                            and notify_spool.restore_writer_hold(notify_spool.data_root(ledger)) is None):
                        provider.discard()
                    return {"status": "deferred", "done": 0}
                except ValueError:
                    ledger.db.rollback()
                    return {"status": "source_changed", "done": 0}
            ready, actual, write_state = patient_context_refresh_readiness._readiness_for_write(ledger, pid, cfg, write_proof[0], deadline=deadline,
                complete=complete, allow_dependency_repair=lane != "A", source_keys=source_keys)
            if actual is not None:
                fresh = actual
            if write_state is not None:
                scans_now = write_state['scan_sig']
            elif ledger.db.execute("PRAGMA data_version").fetchone()[0] != version:
                scans_now = patient_context_refresh_state._scan_sig(ledger.db, pid, source_keys=source_keys)
            selected_now = next((row for row in fresh["sources"] if selected_source[0] is not None
                                 and context_plan.source_key(row) == context_plan.source_key(selected_source[0])), None)
            if ready != 'verified':
                ledger.db.rollback()
                changed = actual is not None and actual['source_sig'] != snapshot['source_sig']
                if changed and provider is not None:
                    provider.discard()
                return {"status": "source_changed" if changed else "deferred", "done": 0,
                        **({"reason": "patient_context_readiness_unverified"} if not changed else {})}
            moved = (fresh["source_sig"] != snapshot["source_sig"] or scans_now != frozen_scan_sig
                     or patient_context_refresh_state._published_version(ledger.db, pid) != frozen_ver
                     or snapshot["contract"].get(VERSION_KEY) != patient_context_refresh_state.CURRENT_VERSION
                     or selected_source[0] is not None and (selected_now is None
                         or (write_state['occurrence'] if write_state is not None
                             and write_state['source_key'] == context_plan.source_key(selected_source[0])
                             else patient_context_refresh_state._digest(patient_context_refresh_sources._source_occurrence(ledger.db, pid, selected_now))) != patient_context_refresh_state._digest(selected_occurrence[0])))
            if moved or patient_context_refresh_state.time.monotonic() >= deadline:
                # another publisher (a second lane-3 process) wrote this
                # patient's context or progress since selection: our
                # inputs are stale, the next unit re-selects
                ledger.db.rollback()
                if provider is not None and moved:
                    provider.discard()
                return {"status": "source_changed", "done": 0}
            adoption_writes = ([0] if lane == "C" and source_quality_identity is not None else None)
            adoption_before = ledger.db.total_changes
            adoption_epoch = None
            if adoption_writes is not None:
                try:
                    adoption_epoch = patient_context_refresh_state._adoption_epoch_expected(ledger.db)
                except ValueError:
                    ledger.db.rollback()
                    return {"status": "source_changed", "done": 0}
                adoption_cfg = patient_context_refresh_state._digest(live_cfg)
            prior = progress if lane == "A" else saved
            attempts = prior.get("attempts", 0) + 1 if failure else 0
            retry = {"failure": failure, "held": failure in ("nightly_unsplittable_assertion", "nightly_original_unit_unverified"),
                     "attempts": attempts, "checks": patient_context_refresh_state._CHECKS,
                     "next_try": now + min(86400, 300 * 2 ** min(attempts, 8)) if failure else 0}
            progress = {**progress, "last_attempt": now, "last_attempt_unit": rotation.get("units", 0) + 1,
                        "night_id": night_id,
                        **({"source_sig": snapshot["source_sig"], "source_scope": source_keys} if lane == "A" else {}),
                        **(retry if lane == "A" and deferred_reason is None else {})}
            if lane == "A" and deferred_reason is not None:
                progress["next_try"] = now if deferred_reason == "summary_quality_pending" else now + 60
                progress["quality_state"] = (deferred_reason[len("summary_quality_"):]
                    if deferred_reason.startswith("summary_quality_") else None)
            elif lane == "A":
                progress.pop("quality_state", None)
            if failure and lane != "A":
                patient_context_refresh_state._replace(ledger, patient_context_refresh_state.SCAN, pid, {**{k: v for k, v in saved.items() if not k.startswith("_")}, **retry}, meta, source["message_id"])
            if lane == "C" and deferred_reason and deferred_reason.startswith("source_quality_"):
                patient_context_refresh_state._replace(ledger, patient_context_refresh_state.SCAN, pid, {**{k: v for k, v in saved.items() if not k.startswith("_")},
                    "audited": False, "quality_state": deferred_reason[len("source_quality_"):],
                    "quality_hold_key": patient_context_refresh_state._source_quality_hold_key(ledger.db, pid, cfg, source, saved["_chunks_sig"]),
                    "next_try": now + (0 if deferred_reason == "source_quality_pending" else 60)}, meta, source["message_id"])
            if not failure and result is not None:
                proof_changed = lane != "A"
                if lane == "A":
                    prior_doc, prior_meta = patient_context_refresh_state._latest(ledger.db, patient_context_refresh_state.KIND, pid)
                    proof_changed = (prior_meta.get("summary_checked") is not True
                        or prior_meta.get("source_sig") != snapshot["source_sig"]
                        or prior_meta.get("scan_sig") != frozen_scan_sig
                        or patient_context_refresh_state._route_contract(prior_meta) != snapshot["contract"]
                        or prior_doc.get("facts_sig") != result.get("facts_sig")
                        or prior_doc.get("summary") != result.get("summary"))
                    saved_scans = patient_context_refresh_sources._current_scans(ledger.db, pid, snapshot["sources"], snapshot["contract"], cfg=cfg)
                    audited = all(patient_context_refresh_sources._scan_ready(saved_scans.get(context_plan.source_key(s), {}))
                                  for s in snapshot["sources"] if context_plan.source_usable(s))
                    intact = patient_context_refresh_state._sources_intact(snapshot["sources"]) and snapshot["attachment_coverage"]["complete"]
                    prior_failure = patient_context_quality_receipts._has_prior_quality_failure(ledger.db, pid)
                    quality_ok = (not (quality_required or prior_failure) or quality_identity is not None
                                  and (not quality_skipped or not prior_failure))
                    patient_context_refresh_state._replace(ledger, patient_context_refresh_state.KIND, pid, result, {**meta, "summary_checked": True,
                        "scan_sig": frozen_scan_sig, "publication_requested": patient_context_refresh_state.options(cfg)["publication"],
                        "published": bool(patient_context_refresh_state.options(cfg)["publication"] and audited and intact
                                          and snapshot["dependencies"]["complete"]
                                          and (source_keys is not None or snapshot["acquisition"]["complete"]) and quality_ok)})
                    progress["summary_pending"] = bool(quality_required and quality_identity is None)
                else:
                    if "source_occurrence" in result:
                        meta.update(source_occurrence_version=1, source_occurrence=result["source_occurrence"],
                                    source_binding=result["source_occurrence"]["source_binding"])
                    if lane == "B" and result.get("chunk_span") is not None:
                        patient_context_refresh_state._save_source_chunk_tx(ledger, pid, source["message_id"], context_plan.source_key(source),
                                              result["chunk_span"], result["facts"], meta)
                    checkpoint = {k: v for k, v in result.items() if not k.startswith("_")}
                    checkpoint["facts"] = result.get("facts", [])[:context_plan.MAX_SOURCE_FACTS]
                    adopted_scan = patient_context_refresh_state._replace(ledger, patient_context_refresh_state.SCAN, pid, {**checkpoint, **retry}, meta, source["message_id"],
                                            write_count=adoption_writes)
                    if lane == "C" and (source["source"] == "attachment_ocr"
                            or source["source"] == "karte_summary" and source.get("historical")):
                        prior_support = dependency_loss.capture_tx(ledger.db, pid, 0,
                            source_key=context_plan.source_key(source), artifact_ids=result["_audited_chunk_ids"])
                        written = dependency_loss.record_tx(ledger.db, prior_support, audited_source=True)
                        if adoption_writes is not None:
                            adoption_writes[0] += written
                    if lane == "C" or lane == "B" and result["complete"]:
                        progress["summary_pending"] = True
                        if any(f.get("discovery") == "backfill_found" for f in result.get("facts", [])):
                            progress.update(attempts=0, next_try=0, failure=None)
                if proof_changed:
                    ledger.bump_inference_analysis_epoch_tx()
                    if adoption_writes is not None:
                        adoption_writes[0] += int(adoption_epoch is not None)
            adopted_progress = patient_context_refresh_state._replace(ledger, patient_context_refresh_state.PROGRESS, pid, progress, {}, write_count=adoption_writes)
            if adoption_writes is not None:
                # Candidate QA was completed before the writer lock; allow
                # only the known adoption writes here.
                epoch_row = ledger.db.execute("SELECT job_id,payload FROM fetch_jobs WHERE kind='semantic_sched' "
                    "AND project_id=0 AND message_id=0").fetchone()
                final_source = next(context_queries.patient_context_sources(ledger.db, pid, hydrate=False, mid=context_plan.source_key(source), cfg=cfg), None)
                valid_adoption = (
                    (tuple(epoch_row) if epoch_row is not None else None) == adoption_epoch
                    and patient_context_refresh_state._adoption_artifact_matches(ledger.db, adopted_scan, patient_context_refresh_state.SCAN, pid, source['message_id'],
                                                   {**checkpoint, **retry}, meta)
                    and patient_context_refresh_state._adoption_artifact_matches(ledger.db, adopted_progress, patient_context_refresh_state.PROGRESS, pid, None, progress, {})
                    and patient_context_refresh_state._published_version(ledger.db, pid) == adopted_progress
                    and final_source is not None and context_plan.source_signature(final_source) == context_plan.source_signature(source)
                    and patient_context_refresh_state._digest(patient_context_refresh_sources._source_occurrence(ledger.db, pid, final_source)) == patient_context_refresh_state._digest(selected_occurrence[0])
                    and patient_context_refresh_state._digest(extract_llm.load_config()) == adoption_cfg
                    and source_quality_keys == (semantic_jev.api_key(), cloud_policy.selected_key(extract_llm.HOME, extract_llm.load_config()))
                    and patient_context_quality_receipts._quality_scope_allowed(ledger.db, pid, live_policy, [final_source])
                    and context_queries.patient_context_scope(ledger.db, pid).get('fetch_reason') not in ('forbidden', 'permission_denied', 'access_revoked')
                    and admit() and extract_llm._writer_current(full=True) and patient_context_refresh_state.time.monotonic() < deadline
                    and ledger.db.execute('PRAGMA data_version').fetchone()[0] == version
                    and ledger.db.total_changes == adoption_before + adoption_writes[0])
                if not valid_adoption:
                    ledger.db.rollback()
                    return {"status": "source_changed", "done": 0}
            if not failure and estimated_unit:
                cloud_policy.dryrun_tx(ledger, route="patient_context", project_id=pid,
                    unit_key=lane, input_chars=sum(source.get("body_length", 0) for source in snapshot["sources"]),
                    max_tokens=cloud_selection["max_tokens"] or 1600, model=cloud_selection["model"])
            if not failure and shadow_call is not None:
                shadow_unit = os.urandom(16).hex()
                comparison_sources = [source] if lane != "A" else snapshot["sources"]
                try:
                    left = patient_context_refresh_state._comparison_context(shadow_call[3], comparison_sources,
                        facts=facts if lane == "A" else None, prefix_ids=shadow_call[4])
                except ValueError:
                    left = None
                extract_llm._cloud_compare_tx(ledger, route="patient_context", project_id=pid,
                    left=left, right=None, rejected=None, held=None, repairs=None, unit=shadow_unit, pending=True)
            ledger.db.commit()
        except BaseException:
            ledger.db.rollback()
            raise
    finally:
        os.close(fd)
    if not failure and shadow_call is not None and patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.CALL_SECONDS + patient_context_refresh_state.COMMIT_MARGIN < deadline:
        fn, unit_key, mid, local_value, prefix_ids = shadow_call
        cloud_value = None
        measured = {}
        comparison = None
        try:
            comparison = cloud_for(unit_key, mid, after_commit=True)
            cloud_value = fn(comparison, False, measured, True)
            guard_provider(comparison)
        except cloud_client.CloudNotSent:
            cloud_value = None
        except cloud_inference.CloudObservationIncomplete:
            cloud_value = None
        except cloud_inference.CloudFallback as error:
            if comparison is not None and str(error) == "cloud_unit_changed":
                comparison.discard()
            cloud_value = None
        except (ValueError, patient_context_refresh_state.Deferred):
            cloud_value = None
        sources = [source] if lane != "A" else snapshot["sources"]
        try:
            local_comparison = patient_context_refresh_state._comparison_context(local_value, sources, facts=facts if lane == "A" else None,
                                                   prefix_ids=prefix_ids)
        except ValueError:
            local_comparison = None
        try:
            cloud_comparison = None if cloud_value is None else patient_context_refresh_state._comparison_context(
                cloud_value, sources, facts=facts if lane == "A" else None)
        except ValueError:
            cloud_comparison = None
        if patient_context_refresh_state.time.monotonic() < deadline:
            audit_fd = patient_context_refresh_state.acquire_run_lock()
            if audit_fd is not None:
                try:
                    if patient_context_refresh_state.time.monotonic() < deadline:
                        extract_llm._cloud_compare(ledger, route="patient_context", project_id=pid,
                            local_facts=local_comparison, cloud_facts=cloud_comparison, unit=shadow_unit,
                            rejected=measured.get("rejected"), repairs=measured.get("repairs"))
                finally:
                    os.close(audit_fd)
    return {"status": "failed" if failure else "deferred" if deferred_reason else "progressed",
            "done": int(not failure and deferred_reason is None), "lane": lane,
            **({"reason": failure or deferred_reason} if failure or deferred_reason else {}),
            "lock_wait_s": waited, **stats}
