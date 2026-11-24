"""Advance fair patient context work while preserving inference and publication evidence."""
from contextlib import ExitStack, closing as closing, contextmanager as contextmanager
from datetime import datetime as datetime
import copy as copy
import hashlib as hashlib
import json as json
import os as os
from pathlib import Path as Path
import re as re
import sqlite3 as sqlite3
import local_llm as local_llm
import semantic_policy
import initial_import_state as initial_import_state
import cloud_inference as cloud_inference
import cloud_backlog as cloud_backlog
import inference_dependency_loss as dependency_loss
import patient_context_artifacts as context_artifacts
from clinical_chunking import PLAN_VERSION as PLAN_VERSION
from mcs_queries import JST as JST
from mcs_util import html_to_text as html_to_text, loads_dict as loads_dict, safe_error_code as safe_error_code, strict_json as strict_json, valid_hash as valid_hash
from inference_version import VERSION_KEY as VERSION_KEY, current_meta_pred as current_meta_pred, read_version as read_version
import patient_context_plan as context_plan
import patient_context_selection as context_selection
import patient_context_schedule as context_schedule
import patient_context_quota as context_quota
import mcs_queries as context_queries
import patient_context_refresh_state
import patient_context_quality_receipts
import patient_context_refresh_sources
import patient_context_refresh_readiness
import patient_context_quality_controller
import writer_lock as writer_lock
from patient_context_refresh_state import (
    CALL_SECONDS as CALL_SECONDS,
    CHUNK as CHUNK,
    COMMIT_MARGIN as COMMIT_MARGIN,
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
    _INTERNAL_PROOF as _INTERNAL_PROOF,
    _SCAN_CODE as _SCAN_CODE,
    _SUMMARY_CODE as _SUMMARY_CODE,
    _checks as _checks,
    _cloud_input_binding as _cloud_input_binding,
    _comparison_context as _comparison_context,
    _contract as _contract,
    _digest as _digest,
    _facts_sig as _facts_sig,
    _latest as _latest,
    _local_provenance as _local_provenance,
    _out_of_scope as _out_of_scope,
    _published_version as _published_version,
    _quality_assessment as _quality_assessment,
    _quality_expected as _quality_expected,
    _quality_kind as _quality_kind,
    _replace as _replace,
    _route as _route,
    _save_source_chunk_tx as _save_source_chunk_tx,
    _scan_sig as _scan_sig,
    _source_quality_hold_key as _source_quality_hold_key,
    _sources_intact as _sources_intact,
    _summary_contract as _summary_contract,
    acquire_run_lock as acquire_run_lock,
    options as options,
    route_accepts as route_accepts,
    time as time,
    workday_window as workday_window
)
from patient_context_quality_receipts import (
    _new_summary_repair_response as _new_summary_repair_response,
    _quality_answers as _quality_answers,
    _quality_claim as _quality_claim,
    _quality_failed as _quality_failed,
    _quality_owns as _quality_owns,
    _quality_pass_content as _quality_pass_content,
    _quality_passed as _quality_passed,
    _quality_skip_receipt as _quality_skip_receipt,
    _quality_store_answers as _quality_store_answers
)
from patient_context_refresh_sources import (
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
    _scan_read_owner as _scan_read_owner,
    _scan_ready as _scan_ready,
    _scans_from_read as _scans_from_read,
    _snapshot as _snapshot,
    _source_chunks as _source_chunks,
    _source_occurrence as _source_occurrence,
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
    _capture_readiness as _capture_readiness,
    _cloud_extraction_ready as _cloud_extraction_ready,
    _context_view_input as _context_view_input,
    _extraction_qc_ready as _extraction_qc_ready,
    _full_write_readiness as _full_write_readiness,
    _inference_coverage as _inference_coverage,
    _inference_ready as _inference_ready,
    _observe_readiness as _observe_readiness,
    _pending as _pending,
    _quality_publication_allowed as _quality_publication_allowed,
    _raw_extraction_current as _raw_extraction_current,
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
    _quality_step as _quality_step,
    _scan as _scan,
    _source_repair_seal as _source_repair_seal,
    _summary as _summary
)


def _completed_cloud_receipts(ledger, provider, *, answered_only=False):
    """Recheck the existing paid receipts before retaining or adopting a unit."""
    import cloud_policy
    reservations = provider.answered_reservations if answered_only else provider.completed_reservations
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
        raise ValueError("patient_context_cloud_receipt_changed")
    return evidence


_FAILURE_DETAIL = re.compile(r"[a-z][a-z0-9_]{2,80}")


def _quality_unavailable_detail(exc):
    """Bounded cause for quality-unavailable deferrals: delay codes, coded
    snake_case ValueErrors and exception type names only — never payloads."""
    if isinstance(exc, patient_context_refresh_state.Deferred):
        return str(exc)
    if type(exc) is ValueError and _FAILURE_DETAIL.fullmatch(str(exc)):
        return str(exc)
    return type(exc).__name__


def _quality_save_refusal(db, pid, live_cfg, live_policy, problems, sources, identity, keys, skipped, selected,
                          *, expected=None):
    """The save-time gate that refuses one audited identity, or None while its policy and receipts hold."""
    import cloud_policy
    import extract_llm
    import semantic_jev
    if (problems or not patient_context_quality_receipts._quality_scope_allowed(db, pid, live_cfg, sources)
            or keys != patient_context_refresh_state._credential_tag(
                semantic_jev.api_key(), cloud_policy.selected_key(extract_llm.HOME, live_cfg))
            or identity["assessment"] != patient_context_refresh_state._quality_assessment(live_policy)):
        return "save_policy_changed"
    if ((not patient_context_quality_receipts._quality_skip_receipt(db, pid, identity, selected, live_policy["match_threshold"])
         if skipped else not patient_context_quality_receipts._quality_passed(db, pid, identity, expected=expected))
            or patient_context_quality_receipts._quality_failed(db, pid, identity)):
        return "save_receipt_invalid"
    return None


def _quality_source_changed(code):
    """The save-time quality refusal, naming the gate it stopped at."""
    return {"status": "source_changed", "done": 0,
            "quality_detail": patient_context_refresh_state.bounded_quality_detail(code)}


# Which patients a worker serves. ``cloud``: the context children, patients the cloud scope
# serves. ``local``: a local lane while a child serves the cloud lane, every other patient.
# ``local_takeover``: a local lane once no child has served for the grace period, every patient,
# with the cloud off for the unit. ``any``: every patient, the path without roles.
ROLES = ("any", "cloud", "local", "local_takeover")
# Each role family keeps its own patient-scan bookmark in the shared progress row.
_SCAN_KEYS = ("patient_scan", "patient_scan_local")
# A worker that progressed a patient whose rebuild hides its context finishes it first, for
# at most this many consecutive units.
PREFER_LIMIT = 32
_PREFER = "_patient_context_prefer"
# Under ``local_takeover`` a unit that holds cloud work waits for a context child with this
# reason. The wait belongs to that patient, not to the lane: the worker passes over the patient
# for as long as a child waits after a cloud defer (``cloud_backlog._context_retry_delay``) and
# moves on, in the same pass, to a patient it can serve, for at most this many units.
CHILD_WAIT_REASON = "patient_context_cloud_deferred:cloud_worker_unavailable"
CHILD_WAIT_S = 60
CHILD_WAIT_UNITS = 8
_CHILD_WAIT = "_patient_context_child_wait"
# A pass over more patients than its candidate budget that found nothing due, waiting or
# interrupted. ``cloud_backlog._context_retry_delay`` paces it like other idle waits.
NOTHING_DUE_REASON = "patient_context_nothing_due"


def _scan_key(role):
    return "patient_scan_local" if role in ("local", "local_takeover") else "patient_scan"


def run_due(ledger, cfg, *, deadline, admit, complete=None, local_admit=None, role="any"):
    """Advance one fair idle-lane unit, freezing inputs across inference and commit.

    ``complete(project_id)`` gates legacy whole-patient work. Frozen source
    scopes prove their own post-level inference and acquisition evidence.
    ``role`` names the patients this worker serves (``ROLES``)."""
    if role not in ROLES:
        raise ValueError("patient_context_role_invalid")
    if ledger.db.in_transaction:
        return {"status": "deferred", "done": 0, "reason": "transaction_active"}
    unit = ledger.__dict__.pop("_patient_context_completed", None)
    resumed = unit is not None
    if unit is None:
        unit = _run_due(ledger, cfg, deadline=deadline, admit=admit,
                        complete=complete, local_admit=local_admit, role=role)
    try:
        outcome = unit.send((cfg, deadline, admit, complete)) if resumed else next(unit)
    except StopIteration as finished:
        return finished.value
    except BaseException:
        unit.close()
        raise
    # Only a completed paid A/B unit may yield. No TX, cursor, run lock,
    # request slot, transport or patient lock is held across this suspension.
    # Ledger.close pops and closes this one unit without running any
    # save/disposition code.
    ledger.__dict__["_patient_context_completed"] = unit
    return outcome


def _advance_patient_scan(ledger, expected, bookmark, deadline, admit, *, observed_out=None,
                          key="patient_scan"):
    import extract_llm

    def _mark(reason):
        if observed_out is not None:
            observed_out['scan_refusal'] = reason

    # Admission may start external commands (the memory check): never under the write lock.
    if patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.COMMIT_MARGIN >= deadline:
        _mark('deadline')
        return None
    if not admit():
        _mark('admission')
        return None
    with patient_context_refresh_sources._scan_writer_wait(ledger.db, deadline):
        try:
            ledger.db.execute('BEGIN IMMEDIATE')
            if patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.COMMIT_MARGIN >= deadline:
                refusal = 'deadline'
            elif not extract_llm._writer_current(full=True):
                refusal = 'writer_generation'
            elif patient_context_refresh_state._latest(ledger.db, patient_context_refresh_state.PROGRESS, 0) != expected:
                refusal = 'bookmark_cas'
            else:
                refusal = None
            if refusal is not None:
                _mark(refusal)
                ledger.db.rollback()
                return None
            content, meta = expected
            # The other role family's bookmark is kept as it is.
            content = {**content, key: bookmark}
            patient_context_refresh_state._replace(ledger, patient_context_refresh_state.PROGRESS, 0, content, meta)
            if not extract_llm._writer_current(full=True):
                _mark('writer_generation')
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


def _generation_input(snapshot, facts):
    # A cloud route generates without the local model; a local one counts a
    # model change as a new generation against the quota.
    route = snapshot['route']
    producer = route['provider'] if route['provider'] else {'local_model': route['local_model']}
    return patient_context_refresh_state._digest([
        "patient_context_generation_v1", snapshot['source_sig'],
        patient_context_refresh_state._facts_sig(facts), snapshot['contract'], producer])


def _generation_plan(db, pid, cfg, snapshot, now, *, cohort=False):
    """Observe quota for the same hydrated input used by the overview caller.

    ``cohort`` says this overview covers every source the patient may use now. Such an
    overview, after local work replaced paid cloud scans (progress ``rebuild_summary``),
    skips the generation interval once, like the first full one after a partial one."""
    hydrated = patient_context_refresh_sources._snapshot(
        db, pid, cfg, route=snapshot['route'], source_keys=snapshot.get('source_scope'))
    if hydrated['source_sig'] != snapshot['source_sig']:
        raise ValueError('patient_context_source_changed')
    workday_id, _ = patient_context_refresh_state.workday_window(now)
    facts, _ = patient_context_refresh_sources._all_facts(db, pid, hydrated, workday_id, cfg=cfg)
    old, meta = patient_context_refresh_state._latest(db, patient_context_refresh_state.KIND, pid)
    same = (bool(old) and old.get('facts_sig') == patient_context_refresh_state._facts_sig(facts)
        and meta.get('source_sig') == snapshot['source_sig']
        and patient_context_refresh_state.route_accepts(snapshot['route'], meta))
    quality = patient_context_quality_receipts._summary_quality_required(db, pid, snapshot)
    key = _generation_input(snapshot, facts)
    prior_coverage = meta.get('source_coverage')
    if prior_coverage is not None and not isinstance(prior_coverage, dict):
        raise ValueError('patient_context_generation_unverified')
    prior_coverage = prior_coverage or {}
    partial = bool(prior_coverage and (prior_coverage.get('unreflected')
        or prior_coverage.get('acquisition_complete') is False))
    coverage = snapshot.get('source_coverage') or {}
    full = (snapshot['acquisition']['complete'] and not coverage.get('unreflected')
        and snapshot['dependencies']['complete'] and snapshot['attachment_coverage']['complete'])
    catchup = False
    if partial and full:
        scans = patient_context_refresh_sources._current_scans(
            db, pid, snapshot['sources'], snapshot['route'], cfg=cfg)
        full = all(patient_context_refresh_sources._scan_ready(scans.get(context_plan.source_key(source), {}))
                   for source in snapshot['sources'] if context_plan.source_usable(source))
        taken = db.execute("SELECT json_extract(content,'$.catchup_generation') FROM artifacts WHERE kind=? "
            "AND project_id=? AND CASE WHEN json_valid(content) THEN json_extract(content,'$.catchup_generation') "
            "IS NOT NULL ELSE 0 END ORDER BY artifact_id DESC LIMIT 1", (patient_context_refresh_state.PROGRESS, pid)).fetchone()
        if taken is not None and not valid_hash(taken[0]):
            raise ValueError('patient_context_generation_unverified')
        catchup = bool(full and (taken is None or taken[0] == key))
    progress, _ = patient_context_refresh_state._latest(db, patient_context_refresh_state.PROGRESS, pid)
    rebuilt = cohort and progress.get('rebuild_summary') is True
    if rebuilt and not catchup:
        # Arming the flag dropped any earlier binding, so this one names the rebuild's overview.
        taken = progress.get('catchup_generation')
        if taken is not None and not valid_hash(taken):
            raise ValueError('patient_context_generation_unverified')
        catchup = taken is None or taken == key
    return {'input_key': key, 'catchup': catchup, 'rebuilt': rebuilt,
            'needed': bool(facts) and not same or quality}


_WAIT_COUNTER_KEYS = (
    'wait_no_eligible_sources', 'wait_no_due_lane', 'wait_generation_quota',
    'wait_readiness_unverified', 'wait_dependency_incomplete',
    'wait_other_role', 'wait_patient_busy', 'wait_cloud_worker',
)
# Patients of the other worker role, and patients passed over while their unit waits for a
# context child, do not use the pass's candidate budget, up to this many.
_ROLE_SKIP_LIMIT = 64
_SCAN_COUNTER_KEYS = _WAIT_COUNTER_KEYS + ('skip_patient_invalid',)
_DENIAL_KEYS = ('generation_reason', 'unverified_records')
_GENERATION_UNVERIFIED = 'patient_context_generation_unverified'


def _note_generation_denial(observed_out, quota):
    """Keep one generation-quota denial per pass; an unreadable record outranks other reasons.

    The record count always belongs to the reason kept, never a mix of patients."""
    reason = quota.get('reason')
    if reason is None:
        return
    records = quota.get('unverified_records', 0)
    held = observed_out.get('generation_reason')
    if held is None or (reason == _GENERATION_UNVERIFIED and held != _GENERATION_UNVERIFIED):
        observed_out['generation_reason'], observed_out['unverified_records'] = reason, records
    elif held == reason:
        observed_out['unverified_records'] = max(observed_out['unverified_records'], records)


def _raise_if_systemic(unreadable, scanned):
    """Re-raise when no scanned patient was readable and two or more failed alike.

    The same code from every patient points at configuration or code, not at
    one patient's stored rows, so the pass reports an error instead of skips."""
    if (len(unreadable) >= 2 and len(unreadable) == scanned
            and len({(type(exc), safe_error_code(exc)) for exc in unreadable}) == 1):
        raise unreadable[0]


def _select_patient(ledger, cfg, now, deadline, admit, complete, *, observed_out=None, role="any",
                    patient_lock_out=None, prefer=None, passed_over=()):
    """Advance a bounded scan before evaluating each patient's current evidence.

    ``role`` skips the patients another worker role serves, a patient another worker holds
    is skipped as busy, and a patient in ``passed_over`` (its unit waits for a context child)
    is skipped as waiting. The selected patient's lock is appended to ``patient_lock_out`` as
    ``(pid, fd)``; without that list no lock outlives the selection. ``prefer`` is evaluated
    first, without moving the scan bookmark."""
    if observed_out is not None:
        observed_out.update(dict.fromkeys(_SCAN_COUNTER_KEYS, 0))
    cloud_scan = patient_context_refresh_readiness._cloud_scan_for(ledger, cfg)
    scan_key = _scan_key(role)
    scanned, waiting, seen = 0, 0, set()
    scan_reads, scan_interrupts = 0, 0
    unreadable = []
    best, selected = {}, None
    rotation = None
    covered = False

    def count(key):
        if observed_out is not None:
            observed_out[key] += 1

    def passed(pid):
        """Count a patient another role serves, or whose unit waits for a context child."""
        nonlocal waiting
        if role in ("local", "cloud") and cloud_scan(pid) != (role == "cloud"):
            count('wait_other_role')
            return True
        if pid in passed_over:
            count('wait_cloud_worker')
            waiting += 1
            return True
        return False

    def consider(reader, pid):
        """Evaluate one patient: ``selected``, ``checked`` (then test coverage) or ``skipped``."""
        nonlocal scanned, waiting, scan_reads
        seen.add(pid)
        if passed(pid):
            return 'other_role'
        scanned += 1
        lock = _patient_lock(reader.db, pid)
        if lock is None:
            # Another worker is on this patient (or its lock cannot be taken): not this pass's.
            count('wait_patient_busy')
            waiting += 1
            return 'checked'
        kept = False
        try:
            waiting_before = waiting
            counters_before = ({key: observed_out[key] for key in _WAIT_COUNTER_KEYS}
                               if observed_out is not None else None)
            denial_before = ({key: observed_out.get(key) for key in _DENIAL_KEYS}
                             if observed_out is not None else None)
            whole = None
            try:
                published = patient_context_refresh_state._published_version(reader.db, pid)
                whole = patient_context_refresh_sources._snapshot(reader.db, pid, cfg, hydrate=False)
                keys = patient_context_refresh_readiness._eligible_source_keys(reader, pid, cfg, whole, deadline=deadline)
                if not keys and whole['sources']:
                    count('wait_no_eligible_sources')
                    waiting += 1
                    return 'skipped'
                source_keys = list(keys) if keys else None
                observed = {}
                scan_reads += 1
                snapshot, progress, pending = patient_context_refresh_readiness._pending(
                    reader.db, pid, cfg, now=now, cloud_scan=cloud_scan(pid),
                    source_keys=source_keys, observed_out=observed)
                if patient_context_refresh_state._out_of_scope(snapshot):
                    return 'skipped'
                scopes = [(snapshot, progress, pending, source_keys, ('B', 'C'))]
                # Same candidate, connection, read transaction, scope,
                # contract, cfg and estimate: reuse the stored read
                # _pending just made instead of reading it again.
                scans = observed["scans"]
                summary_keys = [context_plan.source_key(source) for source in snapshot['sources']
                    if not context_plan.source_usable(source)
                    or patient_context_refresh_sources._scan_ready(scans.get(context_plan.source_key(source), {}))]
                if summary_keys or not snapshot['sources']:
                    summary_keys = summary_keys or None
                    if summary_keys == source_keys:
                        scopes[0] = (snapshot, progress, pending, source_keys, ('A', 'B', 'C'))
                    else:
                        scan_reads += 1
                        summary_snapshot, summary_progress, summary_pending = patient_context_refresh_readiness._pending(
                            reader.db, pid, cfg, now=now, cloud_scan=cloud_scan(pid), source_keys=summary_keys)
                        scopes.append((summary_snapshot, summary_progress, summary_pending, summary_keys, ('A',)))
                for candidate, state, due, scope_keys, lanes in scopes:
                    if ('A' in lanes and due['A']
                            and (candidate.get('source_coverage', {}).get('unreflected')
                                 or not candidate['acquisition']['complete'])
                            and not any(context_plan.source_usable(source) for source in candidate['sources'])):
                        due['A'] = False
                    if not any(due[lane] for lane in lanes):
                        count('wait_no_due_lane')
                        waiting += int(candidate['unfinished'])
                        continue
                    if 'A' in lanes and due['A']:
                        try:
                            generation = _generation_plan(reader.db, pid, cfg, candidate, now,
                                                          cohort=lanes != ('A',))
                            quota = (context_quota.inspect(reader.db, pid, generation['input_key'],
                                now=now, catchup=generation['catchup']) if generation['needed'] else {'allowed': True})
                        except ValueError as exc:
                            generation, quota = None, {'allowed': False}
                            if exc.args == (_GENERATION_UNVERIFIED,):
                                quota['reason'] = _GENERATION_UNVERIFIED
                        candidate['_generation_plan'] = generation
                        if not quota['allowed']:
                            if observed_out is not None:
                                observed_out['wait_generation_quota'] += 1
                                _note_generation_denial(observed_out, quota)
                            due['A'] = False
                            waiting += 1
                            if not any(due[lane] for lane in lanes):
                                continue
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
                        count('wait_dependency_incomplete')
                        due['A'] = False
                    if not ready:
                        count('wait_readiness_unverified')
                    waiting += int(candidate['unfinished'] or not ready)
                    if ready:
                        best.update({lane: (candidate, state, published) for lane in lanes if due[lane]})
            except (ValueError, TypeError, KeyError) as exc:
                # One patient's unreadable stored state skips only that patient;
                # SQLite, I/O and scan interruptions still abort the whole pass.
                unreadable.append(exc)
                best.clear()
                waiting = waiting_before
                if observed_out is not None:
                    observed_out.update(counters_before)
                    for key, value in denial_before.items():
                        if value is None:
                            observed_out.pop(key, None)
                        else:
                            observed_out[key] = value
                if patient_context_refresh_readiness._unreadable_in_scope(reader.db, pid, cfg, whole):
                    waiting += 1
                    count('skip_patient_invalid')
                    if observed_out is not None:
                        observed_out.setdefault('skip_patient_code', safe_error_code(exc))
            if best:
                if patient_lock_out is not None:
                    patient_lock_out.append((pid, lock))
                    kept = True
                return 'selected'
            return 'checked'
        finally:
            if not kept:
                os.close(lock)

    def persist(value):
        nonlocal rotation
        rotation = _advance_patient_scan(ledger, rotation, value, deadline, admit,
                                         observed_out=observed_out, key=scan_key)
        if rotation is None:
            raise patient_context_refresh_sources._ScanInterrupted('patient_context_scan_changed')

    def reserve(reader):
        """Reserve the next candidate from the live bookmark before reading its evidence."""
        nonlocal rotation, covered, role_skips, prefer
        rotation = patient_context_refresh_state._latest(reader.db, patient_context_refresh_state.PROGRESS, 0)
        if prefer is not None:
            pid, prefer = prefer, None
            return pid
        bookmark = rotation[0].get(scan_key)
        yield_after = patient_context_refresh_state.time.monotonic() + 1.0
        while True:
            patient_context_refresh_sources._scan_check(reader.db)
            # Expensive activity ordering must not consume the fair turn's chance.
            reader.db.scan_until = min(deadline - patient_context_refresh_state.COMMIT_MARGIN,
                                       patient_context_refresh_state.time.monotonic() + 1.0)
            try:
                plan = context_schedule.plan(reader.db, bookmark)
            except Exception:
                # A cancelled candidate query still uses its turn; retain the query error.
                try:
                    persist(context_schedule.advance_turn(bookmark))
                except Exception:
                    pass
                raise
            finally:
                reader.db.scan_until = deadline - patient_context_refresh_state.COMMIT_MARGIN
            bookmark, pid = plan['next'], plan['project_id']
            if pid is None or pid in seen or not passed(pid):
                persist(bookmark)
                return pid
            # Fold consecutive other-role patients into one bookmark write.
            seen.add(pid)
            role_skips += 1
            if role_skips >= _ROLE_SKIP_LIMIT or _scan_covered(reader.db, seen):
                covered = role_skips < _ROLE_SKIP_LIMIT
                persist(bookmark)
                return None
            if patient_context_refresh_state.time.monotonic() >= yield_after:
                persist(bookmark)
                return None

    try:
        candidates, role_skips = 0, 0
        while candidates < 8 and not covered and role_skips < _ROLE_SKIP_LIMIT:
            fd, reason = _selection_lock(ledger.db)
            if fd is None:
                if not candidates and not role_skips:
                    raise _SelectionUnavailable(reason)
                if observed_out is not None:
                    observed_out['selection_refusal'] = reason
                scan_interrupts += 1
                break
            with ExitStack() as scan:
                try:
                    reader = scan.enter_context(patient_context_refresh_sources._candidate_reader(ledger, deadline))
                    patient_context_refresh_sources._scan_check(reader.db)
                    skipped_before = role_skips
                    preferred = prefer is not None
                    pid = reserve(reader)
                finally:
                    os.close(fd)
                if pid is None and role_skips > skipped_before:
                    continue   # a batch of other-role skips uses no candidate
                candidates += 1
                if pid is not None and pid not in seen:
                    outcome = consider(reader, pid)
                    if outcome == 'selected':
                        selected = pid
                        # Peers may have claimed a different patient's lane during this read.
                        rotation = patient_context_refresh_state._latest(ledger.db, patient_context_refresh_state.PROGRESS, 0)
                        break
                    if preferred or outcome in ('skipped', 'other_role'):
                        continue
                if role_skips < _ROLE_SKIP_LIMIT and _scan_covered(reader.db, seen):
                    covered = True
        _raise_if_systemic(unreadable, scanned)
    # scan_interrupts counts every early abort (contention, deadline or
    # read failure) — finite-slice completion keeps it at 0.
    except patient_context_refresh_sources._ScanInterrupted:
        scan_interrupts += 1
        if observed_out is not None and 'scan_refusal' not in observed_out:
            observed_out['scan_refusal'] = 'scan_interrupted'
        best, selected, covered = {}, None, False
    except sqlite3.Error:
        scan_interrupts += 1
        if observed_out is not None:
            observed_out['scan_refusal'] = 'read_sqlite'
        best, selected, covered = {}, None, False
    except OSError:
        scan_interrupts += 1
        if observed_out is not None:
            observed_out['scan_refusal'] = 'read_io'
        best, selected, covered = {}, None, False
    except ValueError as exc:
        if exc.args == ('patient_scan_bookmark_invalid',):
            refusal = 'invalid_bookmark'
        elif exc.args == ('patient_scan_source_invalid',):
            refusal = 'invalid_source'
        else:
            raise
        scan_interrupts += 1
        if observed_out is not None:
            observed_out['scan_refusal'] = refusal
        best, selected, covered = {}, None, False
    if selected is None and patient_lock_out:
        # An aborted pass keeps no patient.
        while patient_lock_out:
            _release_patient_lock(patient_lock_out.pop())
    return best, selected, rotation, scanned, waiting, covered, scan_reads, scan_interrupts


def _run_due(ledger, cfg, *, deadline, admit, complete=None, local_admit=None, role="any"):
    """Run one unit; under a takeover, after a unit that waits for a context child, select again
    past its patient.

    A takeover pass that found nothing else to serve (no patient selected, and only waiting or
    nothing due) reports the child wait, so that the lane paces it; one that used up its units
    reports an incomplete scan. Any other result, such as another worker in the selection or a
    closed window, is reported as it is."""
    waited = None
    for _ in range(CHILD_WAIT_UNITS if role == "local_takeover" else 1):
        selected, result = yield from _run_one_unit(ledger, cfg, deadline=deadline, admit=admit,
                                                    complete=complete, local_admit=local_admit, role=role)
        if isinstance(result, dict) and result.get("reason") == CHILD_WAIT_REASON:
            waited = result
            continue
        if (role == "local_takeover" and selected is None and isinstance(result, dict)
                and result.get("status") in ("deferred", "complete")
                and result.get("reason") in (None, "patient_context_waiting")):
            if waited is not None:
                return waited
            if result.get("wait_cloud_worker"):
                # Every patient it could serve waits for a child: the patients it passed over.
                return {**result, "status": "deferred", "reason": CHILD_WAIT_REASON}
        return result
    # The pass ended on its unit budget, not on patients it could not serve: like a pass that
    # ended on its candidate budget, it continues from its bookmark without waiting.
    return {**waited, "reason": "patient_context_scan_incomplete"}


def _run_one_unit(ledger, cfg, *, deadline, admit, complete, local_admit, role):
    """Keep one completed unit's existing save path resumable within its deadline; returns the
    selected patient (None when no patient was selected) and the unit's result.

    The selected patient's lock is held while the unit runs, so that two workers never work on
    one patient. It is released while the completed unit waits suspended (the resident may
    extract for many minutes before it resumes) and taken again, without waiting, on resume:
    the publisher fence, not this lock, decides whether the save is still current."""
    returned = []
    patient_lock = []
    prefer = _preferred_patient(ledger, cfg)
    unit = _run_due_unit(ledger, cfg, deadline=deadline, admit=admit, complete=complete,
                         local_admit=local_admit, returned=returned, role=role,
                         patient_lock=patient_lock, prefer=prefer,
                         passed_over=_child_waits(ledger, role))
    try:
        try:
            outcome = next(unit)
            while True:
                for index, entry in enumerate(patient_lock):
                    patient_lock[index] = _release_patient_lock(entry)
                sent = yield outcome
                for index, (pid, _) in enumerate(patient_lock):
                    patient_lock[index] = (pid, _patient_lock(ledger.db, pid))
                outcome = unit.send(sent)
        except StopIteration as finished:
            result = finished.value
    finally:
        unit.close()
        selected = patient_lock[0][0] if patient_lock else None
        while patient_lock:
            _release_patient_lock(patient_lock.pop())
    _remember_patient(ledger, selected, result)
    # A normal return relinquishes only the usable quota lease. Suspended paid
    # work, exceptions and generator close retain their existing ownership.
    if returned:
        try:
            returned[0]('unknown')
        except Exception:
            # Unknown ownership remains reserved when its best-effort CAS
            # cannot be proved. Preserve the unit's existing return value.
            pass
    return selected, result


def _preferred_patient(ledger, cfg):
    """The patient this worker last progressed while that patient's rebuild hides its context.

    Local work that replaces paid cloud scans marks the patient's rebuild (progress
    ``rebuild_summary``) until its overview publishes, and the context is hidden from the
    first local commit until then. A worker finishes such a patient before it starts the
    next. Every other patient, and a rebuild whose context is still visible, keeps the fair
    turn."""
    remembered = ledger.__dict__.get(_PREFER)
    if remembered is None:
        return None
    pid, units = remembered
    try:
        rebuilding = (units < PREFER_LIMIT and patient_context_refresh_state._latest(
            ledger.db, patient_context_refresh_state.PROGRESS, pid)[0].get("rebuild_summary") is True
            and patient_context_refresh_readiness.context_view_input(ledger.db, pid, cfg=cfg) is None)
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError):
        rebuilding = False   # an unreadable state proves nothing: the fair turn decides
    if not rebuilding:
        ledger.__dict__.pop(_PREFER, None)
        return None
    return pid


def _child_waits(ledger, role):
    """The patients this worker passes over while their unit waits for a context child.

    Only a takeover waits for a child; any other role forgets the waits."""
    waits = ledger.__dict__.get(_CHILD_WAIT)
    if role != "local_takeover":
        ledger.__dict__.pop(_CHILD_WAIT, None)
        return frozenset()
    if not waits:
        return frozenset()
    now = patient_context_refresh_state.time.monotonic()
    for pid in [pid for pid, until in waits.items() if until <= now]:
        del waits[pid]
    return frozenset(waits)


def _remember_patient(ledger, pid, result):
    """Count consecutive source units of one patient selected by this worker, and remember a
    patient whose unit waits for a context child.

    Its overview, another patient's unit, or a selected unit of it that did not complete ends
    the count: such a wait (a cloud cap until the next day, an absent child) must not hold
    other patients' ready work."""
    if pid is None or not isinstance(result, dict):
        return
    if result.get("reason") == CHILD_WAIT_REASON:
        ledger.__dict__.setdefault(_CHILD_WAIT, {})[pid] = (
            patient_context_refresh_state.time.monotonic() + CHILD_WAIT_S)
    previous = ledger.__dict__.get(_PREFER)
    if not result.get("done"):
        if previous is not None and previous[0] == pid:
            ledger.__dict__.pop(_PREFER, None)
        return
    if result.get("lane") in ("B", "C"):
        units = previous[1] + 1 if previous is not None and previous[0] == pid else 1
        ledger.__dict__[_PREFER] = (pid, units)
    elif result.get("lane") == "A":
        ledger.__dict__.pop(_PREFER, None)


def _lock_beside_db(db, name):
    """Path of a lock file beside the canonical database path, or None when it is unknown."""
    try:
        row = next((item for item in db.execute("PRAGMA database_list") if item[1] == "main"), None)
        path = row[2] if row is not None else ""
        return os.path.join(os.path.dirname(os.path.realpath(path)), name) if path else None
    except (sqlite3.Error, OSError, ValueError):
        return None


def _patient_lock(db, pid):
    """Non-blocking per-patient mutex beside the database; None when busy or unavailable.

    Patients share 64 lock files, so a busy lock may belong to another patient: a skip, never
    a wrong grant."""
    lock_path = _lock_beside_db(db, f".patient_context_patient_{pid % 64:02d}.lock")
    if lock_path is None:
        return None
    try:
        return acquire_run_lock(lock_path)
    except (OSError, sqlite3.Error):
        return None


def _release_patient_lock(entry):
    pid, fd = entry
    if fd is not None:
        os.close(fd)
    return pid, None


def _guard_refusal_code(error):
    """A cloud error's fixed kind, else the exception type name — never its text."""
    import cloud_client
    kind = getattr(error, "kind", None)
    return kind if isinstance(error, cloud_client.CloudError) and isinstance(kind, str) \
        else type(error).__name__


class _SelectionUnavailable(Exception):
    """The next bookmark could not be reserved by this pass."""


def _selection_lock(db):
    """Non-blocking per-database mutex for reserving the next patient scan turn.

    Returns ``(fd, None)`` while held or ``(None, reason)`` carrying a fixed
    code: ``patient_context_selection_busy`` when another worker holds the
    lock, ``patient_context_selection_unavailable`` when the database path
    or the lock file cannot be established. The lock file lives beside the
    canonical database path, is never unlinked and survives database swaps.
    """
    lock_path = _lock_beside_db(db, ".patient_context_selection.lock")
    if lock_path is None:
        return None, "patient_context_selection_unavailable"
    try:
        fd = acquire_run_lock(lock_path)
    except (OSError, sqlite3.Error):
        return None, "patient_context_selection_unavailable"
    return (fd, None) if fd is not None else (None, "patient_context_selection_busy")


def _run_due_unit(ledger, cfg, *, deadline, admit, complete, local_admit, returned, role="any",
                  patient_lock=None, prefer=None, passed_over=()):
    import extract_llm
    import cloud_inference
    import cloud_client
    import cloud_policy
    import cloud_send_admission
    if ledger.db.in_transaction:
        return {"status": "deferred", "done": 0, "reason": "transaction_active"}
    local_admit = local_admit or admit
    now = patient_context_refresh_state.time.time()
    if not patient_context_refresh_state.options(cfg)["enabled"]:
        return {"status": "off", "done": 0}
    workday_id = patient_context_refresh_state.workday_window(now)[0]
    if patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.COMMIT_MARGIN >= deadline or not admit():
        return {"status": "deferred", "done": 0}
    if role == "local" and patient_context_refresh_readiness._cloud_serves_every_patient(ledger, cfg):
        # Nothing is left for a local lane while the cloud lane serves every patient: no scan.
        return {"status": "deferred", "done": 0, "reason": "patient_context_other_role"}
    scan_key = _scan_key(role)
    try:
        started = patient_context_refresh_state.time.monotonic()
        scan_observed = {}
        best, pid, scan_rotation, scanned, waiting, covered, scan_reads, scan_interrupts = _select_patient(
            ledger, cfg, now, deadline, admit, complete, observed_out=scan_observed, role=role,
            patient_lock_out=patient_lock, prefer=prefer, passed_over=passed_over)
        stats = {"scanned": scanned, "waiting": waiting, "covered": covered,
                 "scan_reads": scan_reads, "scan_interrupts": scan_interrupts,
                 "scan_ms": int((patient_context_refresh_state.time.monotonic() - started) * 1000)}
        stats.update({key: scan_observed[key] for key in _SCAN_COUNTER_KEYS if key in scan_observed})
        for key in ('scan_refusal', 'skip_patient_code', 'generation_reason', 'unverified_records'):
            if scan_observed.get(key) is not None:
                stats[key] = scan_observed[key]
        if 'selection_refusal' in scan_observed:
            return {"status": "deferred", "done": 0, "reason": scan_observed['selection_refusal'], **stats}
        if scan_rotation is None:
            return {"status": "deferred", "done": 0, "reason": "patient_context_scan_incomplete", **stats}
        rotation = scan_rotation[0]
        lane, index, next_cursor = context_schedule.lane_rotation(best, rotation)
        if lane is None:
            # A skipped patient keeps waiting >= 1, so a pass that skipped is never
            # complete; an interrupted pass reports the interruption, not the skip.
            skipped = scan_observed.get('skip_patient_invalid')
            if covered and not scanned and not scan_interrupts and scan_observed.get('wait_other_role'):
                # Every patient belongs to the other worker role. A pass that ended on its
                # budget first reports an incomplete scan, which keeps the caller's pacing.
                return {"status": "deferred", "done": 0, "reason": "patient_context_other_role", **stats}
            # A pass that covered every patient with nothing due or waiting and no interruption is a
            # full rotation with nothing to do, so callers back off. A partial slice never does: the
            # patients it did not reach may be due.
            idle = scanned and covered and not scan_interrupts and not waiting
            reason = ("patient_context_scan_incomplete" if skipped and scan_interrupts
                      else "patient_context_patient_invalid" if skipped
                      else "patient_context_waiting" if waiting
                      else NOTHING_DUE_REASON if idle else "patient_context_scan_incomplete")
            return {"status": "deferred" if waiting or not covered else "complete", "done": 0,
                    **({"reason": reason} if waiting or not covered or idle else {}), **stats}
        snapshot, progress, frozen_ver = best[lane]
        source_keys = snapshot.get('source_scope')
        generation_plan = snapshot.pop('_generation_plan', None) if lane == 'A' else None
        selected_proof = snapshot.pop("_readiness_proof", None)
        # Like extraction claims, scheduling is fenced by SQLite, not the tick's run lock.
        try:
            with patient_context_refresh_sources._scan_writer_wait(ledger.db, deadline):
                ledger.db.execute("BEGIN IMMEDIATE")
                ready, actual, _ = patient_context_refresh_readiness._readiness_for_write(ledger, pid, cfg, selected_proof,
                    deadline=deadline, complete=complete, allow_dependency_repair=lane != "A", source_keys=source_keys)
                latest_rotation = patient_context_refresh_state._latest(ledger.db, patient_context_refresh_state.PROGRESS, 0)
                try:
                    context_schedule._bookmark(latest_rotation[0].get(scan_key))
                except ValueError:
                    ledger.db.rollback()
                    return {"status": "deferred", "done": 0, "reason": "patient_context_scan_incomplete", **stats}
                # Another patient's scan, of either role, may advance without consuming this lane's turn.
                same_rotation = (latest_rotation[1] == scan_rotation[1]
                    and {k: v for k, v in latest_rotation[0].items() if k not in _SCAN_KEYS}
                        == {k: v for k, v in scan_rotation[0].items() if k not in _SCAN_KEYS})
                current = (patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.COMMIT_MARGIN < deadline and admit()
                    and extract_llm._writer_current(full=True)
                    and patient_context_refresh_state._published_version(ledger.db, pid) == frozen_ver
                    and same_rotation
                    and ready == "verified" and actual is not None and actual['source_sig'] == snapshot['source_sig'])
                if not current:
                    ledger.db.rollback()
                    changed = actual is not None and actual['source_sig'] != snapshot['source_sig']
                    return {"status": "source_changed" if changed else "deferred", "done": 0,
                            **({"reason": "patient_context_readiness_unverified"} if not changed else {}), **stats}
                rotation = latest_rotation[0]
                if lane != "A":
                    # B/C may use a wider cohort than the last overview attempt.
                    # Keep that attempt's retry state bound to its original input.
                    progress, _ = patient_context_refresh_state._latest(
                        ledger.db, patient_context_refresh_state.PROGRESS, pid)
                progress = {**progress, "last_attempt": now,
                            "last_attempt_unit": rotation.get("units", 0) + 1,
                            "workday_id": workday_id,
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
    except _SelectionUnavailable as conflict:
        return {"status": "deferred", "done": 0, "reason": conflict.args[0]}
    if lane == "A":
        # Facts need hydrated items; the hydrated read must still match the
        # selection-time signature the reused progress reset came from.
        hydrated = patient_context_refresh_sources._snapshot(ledger.db, pid, cfg, source_keys=source_keys)
        if hydrated["source_sig"] != snapshot["source_sig"]:
            return {"status": "source_changed", "done": 0, **stats}
        snapshot = hydrated
    frozen_scan_sig = patient_context_refresh_state._scan_sig(ledger.db, pid, source_keys=source_keys)
    meta = {"source_sig": snapshot["source_sig"], "workday_id": workday_id}
    patient_context_refresh_state._record_backend(meta, patient_context_refresh_state._local_provenance(cfg))
    meta["contract"] = snapshot["contract"]
    if lane == 'A' and source_keys is not None:
        meta.update(source_scope=source_keys, source_coverage=snapshot['source_coverage'])
    result = None
    failure = None
    deferred_reason = None
    provider = None
    generation_claim = None
    generation_decision = None
    generation_wait = None
    generation_uncertain = False
    generation_contacted = False
    generation_quality = False
    # One reading gives the mode and, when it is off, why: the unit's decisions and its quality
    # step all use this reading.
    mode, off_reason = extract_llm.cloud_route_mode(ledger, cfg, "patient_context", pid)
    if role == "local" and mode == "on":
        # The cloud took this patient between its selection and now: a local lane never sends.
        return {"status": "deferred", "done": 0, "reason": "patient_context_role_changed", **stats}
    takeover = role == "local_takeover" and mode == "on"
    if takeover:
        # No context child has served the cloud lane for the grace period. The unit runs with the
        # cloud off; the snapshot keeps its cloud route, so the unit's cloud work still counts.
        mode, off_reason = "off", "cloud_worker_unavailable"
    cloud_requested = bool(snapshot["route"]["provider"])
    quality_required = patient_context_quality_receipts._summary_quality_required(ledger.db, pid, snapshot)
    cloud_settings = cloud_inference.resolve_cloud_settings(cfg) if mode in ("on", "shadow", "dry_run") else None
    cloud_selection = None
    if cloud_settings is not None:
        cloud_selection, errors = cloud_policy.resolve_profile(
            cfg, route="patient_context", purpose="patient_context",
            root=extract_llm.HOME, ledger=ledger, ordered=True)
        if cloud_selection is None:
            cloud_selection, errors = cloud_policy.resolve_profile(
                cfg, route="patient_context", purpose="patient_context",
                root=extract_llm.HOME)
            if errors or cloud_selection is None:
                return {"status": "source_changed", "done": 0, **stats}
    estimated_unit = False
    shadow_call = None
    shadow_unit = None
    local_checks = []
    quality_identity = None
    quality_keys = None
    source_quality_identity = None
    source_quality_keys = None
    quality_skipped = False
    source_quality_skipped = False
    write_proof = [selected_proof]
    readiness_state = ["verified"]
    # Fixed code of the last guard refusal that became patient_context_cloud_not_sent.
    guard_error = [None]
    # The classified reason the cloud did not serve this unit (cloud_send_admission.REFUSAL_CODES).
    cloud_refusal = ["cloud_worker_unavailable" if takeover else None]
    # Paid requests this commit recorded as replaced by local work.
    replaced = []
    selected_source = [None]
    selected_occurrence = [None]
    # What a paid answer may be taken up for. The overview and the audit read every source of
    # the patient. A scan reads one source and binds to it once it is chosen (scan_input), so
    # that another message of the patient leaves its paid answer usable.
    cloud_input = (patient_context_refresh_state._cloud_input_binding(ledger.db, pid, snapshot)
                   if cloud_settings is not None and lane != "B" else None)
    scan_input = [None]

    # Set when a quality repair, which only the cloud makes, waits for a context child.
    repair_waits = [False]
    # The gate at which the last quality step stopped (observation only; never persisted).
    quality_detail = [None]

    def quality_step(*args, **kwargs):
        """Audit under this unit's cloud mode: a local lane, the takeover included, never sends a
        repair, which is deferred as with the cloud off. Only where the takeover turned the cloud
        off (a child would make the repair) does the pass report a wait for a child."""
        with patient_context_refresh_state.capture_quality_detail() as detail:
            try:
                status, value = patient_context_quality_controller._quality_step(*args, cloud_mode=mode, **kwargs)
            except Exception as exc:
                patient_context_refresh_state.record_quality_detail(
                    "deadline" if patient_context_refresh_state.time.monotonic()
                    + patient_context_refresh_state.COMMIT_MARGIN >= deadline else _quality_unavailable_detail(exc))
                quality_detail[0] = detail["code"]
                raise
        quality_detail[0] = None if status in ("passed", "skipped") else detail["code"]
        if status == "cloud_off":
            repair_waits[0] = takeover
            status = "deferred"
        return status, value

    def answer_stale(latest):
        """Whether a change of the patient's inputs reached the input this unit asked about.

        The overview and the audit read every source, so any change does. A scan asked about
        one source: while that source, its occurrence, the scan contract and the provider are as
        they were, its paid answer still answers the same input. The pass ends without saving
        and without ending that answer, and a later pass takes it up without sending again."""
        if lane != "B" or scan_input[0] is None:
            return True
        actual = next((row for row in latest["sources"]
                       if context_plan.source_key(row) == context_plan.source_key(selected_source[0])), None)
        if actual is None:
            return True
        try:
            occurrence = patient_context_refresh_sources._source_occurrence(ledger.db, pid, actual)
        except ValueError:
            return True   # the source's occurrence is gone
        except sqlite3.Error:
            return False   # a failed read proves nothing about the answer
        source_sig = context_plan.source_signature(actual)
        return scan_input[0] not in {
            patient_context_refresh_state._cloud_scan_binding(
                source_sig, latest["contract"], None, occurrence),
            # Bindings written before provider identity left the scan signature.
            patient_context_refresh_state._cloud_scan_binding(
                source_sig, latest["contract"], latest["route"]["provider"], occurrence)}

    def current(*, after_commit=False):
        """Whether this pass may still send and save. ``readiness_state`` names a refusal:
        ``changed`` when the unit's own input changed (its paid answers are stale), ``moved``
        when only something else of the patient did, ``unknown`` when neither is proven."""
        readiness_state[0] = 'unknown'
        try:
            if ledger.db.in_transaction:
                ready, latest, write_state = patient_context_refresh_readiness._readiness_for_write(ledger, pid, cfg, write_proof[0], deadline=deadline,
                    complete=complete, allow_dependency_repair=lane != "A", source_keys=source_keys)
                readiness_state[0] = ready
                if ready != 'verified' or latest is None:
                    if latest is not None and latest['source_sig'] != snapshot['source_sig']:
                        readiness_state[0] = 'changed' if answer_stale(latest) else 'moved'
                    return False
            else:
                write_state = None
                latest = patient_context_refresh_sources._snapshot(ledger.db, pid, cfg, hydrate=False, source_keys=source_keys)
                ready = patient_context_refresh_readiness._inference_ready(ledger, pid, cfg, deadline=deadline, complete=complete,
                                         allow_dependency_repair=lane != "A", source_keys=source_keys)
                readiness_state[0] = 'verified' if ready else 'unknown'
                if latest['source_sig'] != snapshot['source_sig']:
                    readiness_state[0] = 'changed' if answer_stale(latest) else 'moved'
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
                    readiness_state[0] = 'changed' if answer_stale(latest) else 'moved'
                return False
            if (not admit() or generation_claim is not None
                    and not context_quota.owns(ledger.db, pid, generation_claim,
                                               now=patient_context_refresh_state.time.time())):
                readiness_state[0] = 'unknown'
                return False
            return True
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError,
                RuntimeError, OverflowError, TimeoutError, patient_context_refresh_sources._ScanInterrupted, cloud_inference.CloudFallback):
            readiness_state[0] = 'unknown'
            return False

    def generation_admit():
        return admit() and (generation_claim is None or context_quota.owns(
            ledger.db, pid, generation_claim, now=patient_context_refresh_state.time.time()))

    def begin_generation(*, after_commit=False):
        nonlocal generation_claim, generation_decision, generation_wait, frozen_ver, progress
        if generation_plan is None or not generation_plan['needed'] or generation_claim is not None:
            return
        # Scheduling wrote progress after selection. Capture its clinical
        # observation outside the new quota writer fence, then replay the SQL.
        ready, write_proof[0], _ = patient_context_refresh_readiness._capture_readiness(ledger, pid, cfg,
            deadline=deadline, complete=complete, allow_dependency_repair=False, source_keys=source_keys,
            write_scope={'source_key': None, 'cloud': cloud_input is not None})
        if not ready or write_proof[0] is None:
            raise patient_context_refresh_state.Deferred('patient_context_readiness_unverified')
        with patient_context_refresh_sources._scan_writer_wait(ledger.db, deadline):
            try:
                ledger.db.execute('BEGIN IMMEDIATE')
                if not extract_llm._writer_current(full=True) or not current(after_commit=after_commit):
                    raise patient_context_refresh_state.Deferred('patient_context_readiness_unverified')
                stamp = patient_context_refresh_state.time.time()
                remaining = deadline - patient_context_refresh_state.time.monotonic()
                if remaining <= patient_context_refresh_state.COMMIT_MARGIN:
                    raise patient_context_refresh_state.Deferred('patient_context_capacity_or_deadline')
                decision = context_quota.reserve_tx(ledger.db, pid, generation_plan['input_key'], now=stamp,
                    lease_until=stamp + remaining + patient_context_refresh_state.COMMIT_MARGIN,
                    catchup=generation_plan['catchup'])
                if not decision['allowed']:
                    generation_wait = decision
                    raise patient_context_refresh_state.Deferred(decision['reason'])
                if not context_quota.begin_tx(ledger.db, pid, decision['claim'], now=stamp):
                    raise patient_context_refresh_state.Deferred('patient_context_generation_changed')
                if generation_plan['catchup']:
                    progress = {**progress, 'catchup_generation': generation_plan['input_key']}
                    patient_context_refresh_state._replace(ledger, patient_context_refresh_state.PROGRESS, pid, progress, {})
                if not extract_llm._writer_current(full=True):
                    raise patient_context_refresh_state.Deferred('patient_context_generation_changed')
                ledger.db.commit()
            except BaseException:
                ledger.db.rollback()
                raise
        generation_claim, generation_decision = decision['claim'], decision
        frozen_ver = patient_context_refresh_state._published_version(ledger.db, pid)
        _, write_proof[0], _ = patient_context_refresh_readiness._capture_readiness(ledger, pid, cfg,
            deadline=deadline, complete=complete, allow_dependency_repair=False, source_keys=source_keys,
            write_scope={'source_key': None, 'cloud': cloud_input is not None})
        meta['generation_input_key'] = generation_plan['input_key']

    def return_generation(outcome, *, proven_unsent=False):
        nonlocal generation_claim
        if generation_claim is None or ledger.db.in_transaction:
            return False
        # A proved-unsent call may have exhausted its inference deadline. A
        # zero-wait writer CAS can return the unused slot without another send.
        with patient_context_refresh_sources._scan_writer_wait(ledger.db, deadline):
            try:
                ledger.db.execute('BEGIN IMMEDIATE')
                if not extract_llm._writer_current(full=True):
                    ledger.db.rollback()
                    return False
                changed = context_quota.finish_tx(ledger.db, pid, generation_claim,
                    now=patient_context_refresh_state.time.time(), outcome=outcome, proven_unsent=proven_unsent)
                ledger.db.commit()
                if changed:
                    generation_claim = None
                return changed
            except (sqlite3.Error, OSError, ValueError):
                ledger.db.rollback()
                return False
            except BaseException:
                ledger.db.rollback()
                raise

    returned.append(return_generation)

    def unit_sig():
        """The signature of the input this pass's unit asks about; its requests carry it as
        their owner's ``source_sig``. The overview asks about the patient's sources, a scan
        about its own source."""
        return scan_input[0] if lane == "B" else snapshot["source_sig"]

    def cloud_for(unit_key, mid=None, *, after_commit=False):
        def provider_current():
            try:
                allowed = current(after_commit=after_commit)
            except Exception:
                raise cloud_inference.CloudObservationIncomplete('patient_context_readiness_unverified') from None
            if allowed:
                return True
            if readiness_state[0] != 'changed':
                # Not proven stale (a scan whose own source is as it was included): nothing is
                # sent or saved now, and an answer already paid for is not ended.
                raise cloud_inference.CloudObservationIncomplete('patient_context_readiness_unverified')
            return False

        return cloud_inference.CloudInferenceUnit(ledger, cfg, route="patient_context", project_id=pid,
            message_id=mid, unit_key=unit_key, deadline=deadline,
            current=provider_current,
            dispatch=lambda fn, **kw: fn(), owner={"workday_id": workday_id, "source_sig": unit_sig()},
            selection=cloud_selection, input_binding=scan_input[0] if lane == "B" else cloud_input)

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
        # provider is kept beside it so the selection still recognises the row
        meta.pop("cloud_receipts", None)
        patient_context_refresh_state._record_backend(meta, patient_context_refresh_state._local_provenance(cfg),
                                                      route_provider=snapshot["route"]["provider"])

    def cloud_route(unit):
        patient_context_refresh_state._record_backend(meta, patient_context_refresh_state._cloud_provenance(
            unit.contract, unit.model, cloud_selection), provider=unit.contract)

    def saved_route(saved_meta):
        # A reused row keeps the exact backend identity that produced it.
        for key in patient_context_refresh_state._BACKEND_KEYS:
            if key in saved_meta:
                meta[key] = saved_meta[key]
            else:
                meta.pop(key, None)

    def guard_summary_sources():
        nonlocal generation_uncertain
        try:
            with cloud_policy._accounting_snapshot(ledger.db):
                patient_context_refresh_sources.guard_cloud_sources(ledger, pid, snapshot['sources'],
                    snapshot['route'], cfg=cfg, deadline=deadline)
        except (cloud_client.CloudError, ValueError, TypeError, KeyError, sqlite3.Error,
                OSError, RecursionError, OverflowError) as error:
            generation_uncertain = True
            guard_error[0] = _guard_refusal_code(error)
            raise patient_context_refresh_state.Deferred("patient_context_cloud_not_sent") from None

    def guard_local_input(unit_key, mid, prompt, *, allow, supersede=False, recorded=False):
        """Check this prompt's paid history before local work answers it.

        ``allow`` (a definitive cloud stop) also passes the paid requests local work may
        replace; ``supersede`` records them as replaced, inside the commit transaction, and
        ``recorded`` then refuses while any of them is still without its record."""
        nonlocal generation_uncertain
        request = {"project_id": pid, "message_id": mid, "unit_key": unit_key,
                   "owner": {"workday_id": workday_id, "source_sig": unit_sig()}}
        try:
            with cloud_policy._accounting_snapshot(ledger.db):
                if supersede:
                    replaced.extend(cloud_inference.supersede_context_local(ledger, cfg, prompt, **request))
                elif (cloud_inference.guard_context_local(ledger, cfg, prompt, allow_supersede=allow, **request)
                        and recorded):
                    raise cloud_client.CloudError("budget_exceeded", "cloud_unit_unresolved")
        except (cloud_client.CloudError, ValueError, TypeError, KeyError, sqlite3.Error,
                OSError, RecursionError, OverflowError) as error:
            generation_uncertain = True
            guard_error[0] = _guard_refusal_code(error)
            refusal = getattr(error, "history_refusal", None)
            if cloud_refusal[0] is None or type(refusal) is str:
                cloud_refusal[0] = refusal if type(refusal) is str else "unknown"
            if allow and refusal == "result_missing" and getattr(error, "history_stale", None) is False:
                # The request may still be answered: wait for its result before replacing it.
                raise patient_context_refresh_state.Deferred("patient_context_cloud_deferred:result_pending") from None
            raise patient_context_refresh_state.Deferred("patient_context_cloud_not_sent") from None
        if not supersede and (unit_key, mid, prompt, allow) not in local_checks:
            local_checks.append((unit_key, mid, prompt, allow))

    def cloud_history(unit_key, mid, saved):
        """Whether the cloud was ever asked for this unit key: a stored cloud cursor, or any
        reservation of the key. Its paid requests are what local work may have to replace; the
        local check itself matches them to the prompt."""
        return bool(saved and saved.get("_provider_contract")) or cloud_policy.unit_reserved(
            ledger, route="patient_context", project_id=pid, message_id=mid, unit_key=unit_key)

    def cloud_progress(unit_key, mid, saved):
        """Whether the unit holds cloud work for the input it has now: a stored cloud cursor, or
        a reservation of its key made for this input that was sent or may have been.

        A unit key outlives its input (one per source, and one for every overview of the
        patient), and a request made for an earlier input can never be taken up again. A scan's
        input is its own source, whatever else the patient received since. The overview's input
        also changes while the sources stay the same, so its reservation counts only for the
        generation this pass resumes. A request refused between its reservation and its send
        (the tick withdrawn, the deadline, a circuit) left nothing with the provider: it is no
        work to wait for, although local work still checks it as part of the key's history."""
        if saved and saved.get("_provider_contract"):
            return True
        if lane == "A" and not (generation_decision is not None and generation_decision["resume"]):
            return False
        return cloud_policy.unit_reserved(ledger, route="patient_context", project_id=pid,
            message_id=mid, unit_key=unit_key, source_sig=unit_sig(), sent_only=True)

    def cloud_stop(stop, progress, history):
        """Classify one ``(stage, code)`` stop: the verdict, its bounded code and whether local
        work may replace paid requests. An unnamed stop holds, which is the earlier path.

        ``progress`` (cloud work for the current input) decides between waiting and the other
        verdicts; ``history`` (any paid request of the unit key) is what there is to replace."""
        stage, code = stop if stop is not None else ("pre_send", "unknown")
        code = code if code in cloud_send_admission.REFUSAL_CODES else "unknown"
        verdict = cloud_send_admission.classify_refusal(stage, code, progress=progress)
        # Before a send, only a unit key the cloud was asked for has anything to replace.
        return verdict, code, verdict == "local" and (history or stage != "pre_send")

    def cloud_off(progress, history, *, pinned=False):
        """Classify the stop of a unit the cloud is not attempted for: a source that already
        continues locally, or the reason the cloud is off.

        A wait is for the cloud to come back. Where the configuration does not ask the cloud
        for this patient, nothing would come back, whatever the cloud did for the patient
        before: such a unit keeps the earlier local path, and a local-only installation never
        waits."""
        verdict, code, replace = cloud_stop(("pre_send", "local_pinned" if pinned else off_reason),
                                            progress, history)
        # ``progress`` does not enter this rule. The route's provider is part of every unit's
        # input, so without one no cloud cursor is loaded and no request the cloud route made
        # belongs to the current input. A shadow comparison is the exception: it also runs
        # without a route provider, and its request carries the same input a later pass has
        # with the cloud off. That answer was only ever compared, so nothing would come back
        # for such a unit either, and it holds like the rest.
        if verdict == "defer" and not cloud_requested:
            verdict = "hold"
        return verdict, code, replace

    def cloud_wait():
        """Defer a new overview before it claims a generation slot when the cloud stopped for
        a transient reason, so that a wait takes no slot.

        Reads the same checks the send path makes, without sending or reserving. An input whose
        generation already began is left to the send path: it claims no new slot, and an answer
        it already paid for is taken up there without a send."""
        if mode not in ("on", "off"):
            return
        try:
            if context_quota.inspect(ledger.db, pid, generation_plan['input_key'],
                    now=patient_context_refresh_state.time.time(),
                    catchup=generation_plan['catchup'])['resume']:
                return
            # A generation that has not begun holds no cloud work for its input, and this
            # check replaces nothing.
            if mode == "off":
                verdict, code, _ = cloud_off(False, False)
            else:
                code = (cloud_policy.block_reason(ledger, cloud_settings, "patient_context", pid,
                                                  model=cloud_selection["model"])
                        or cloud_policy.setting_block_reason(cloud_settings)
                        or cloud_policy.budget_block_reason(ledger, cloud_settings,
                            max_tokens=cloud_policy.selection_max_tokens(cloud_selection), selection=cloud_selection))
                if code in cloud_policy.REFINED_CODES:
                    code = cloud_policy.refusal_code(cfg, code, model=cloud_selection["model"],
                                                     root=extract_llm.HOME, settings=cloud_settings,
                                                     route="patient_context", project_id=pid)
                verdict, code, _ = cloud_stop(("pre_send", code), False, False) if code else (None, None, False)
        except Exception:
            return   # the send path decides again, with the same table
        if verdict == "defer":
            cloud_refusal[0] = code
            raise patient_context_refresh_state.Deferred("patient_context_cloud_deferred:" + code)

    def infer(fn, *, unit_key, mid=None, saved=None):
        nonlocal provider, shadow_call, estimated_unit, generation_uncertain, generation_contacted
        # A checked local fallback keeps its own cursor; a cloud attempt starts from the original.
        pinned = bool(saved and saved.get("_source_meta", {}).get("local_fallback") is True)
        use_cloud = mode == "on" and not pinned
        local_saved = dict(saved) if saved and not saved.get("_provider_contract") else None
        # Cloud work the unit held before this attempt; its own new reservation must not count.
        progress = cloud_progress(unit_key, mid, saved)
        history = progress or cloud_history(unit_key, mid, saved)
        # Whether the local check may replace this unit's paid requests (a definitive stop).
        allow = False

        def stopped(unit):
            # A send made in this pass is cloud work for a stop before the next send; a
            # failed send is judged by what the unit held before it.
            stop = getattr(unit, 'refusal', None)
            sent = bool(unit.possibly_sent) and (stop is None or stop[0] != "after_send")
            verdict, code, replace = cloud_stop(stop, progress or sent, history or sent)
            if stop is not None:
                cloud_refusal[0] = code
            return verdict, code, replace
        if use_cloud:
            provider = cloud_for(unit_key, mid)
            try:
                reset = bool(saved and not saved.get("_provider_contract"))
                if reset:
                    saved.clear()
                value = fn(provider, reset)
                guard_provider(provider)
                cloud_route(provider)
                if getattr(provider, 'answered_reservations', None):
                    evidence = _completed_cloud_receipts(ledger, provider, answered_only=True)
                    meta['cloud_receipts'] = [{'artifact_id': aid, 'witness': proof['witness']}
                                              for aid, proof in sorted(evidence.items())]
                return value
            except cloud_client.CloudNotSent:
                generation_contacted = generation_contacted or provider.possibly_sent
                if getattr(provider, 'unresolved', provider.possibly_sent):
                    generation_uncertain = True
                verdict, code, allow = stopped(provider)
                provider = None
                if verdict == "defer":
                    raise patient_context_refresh_state.Deferred("patient_context_cloud_deferred:" + code)
            except cloud_inference.CloudObservationIncomplete as error:
                raise patient_context_refresh_state.Deferred('patient_context_readiness_unverified') from error
            except (cloud_inference.CloudFallback, ValueError) as error:
                changed = isinstance(error, cloud_inference.CloudFallback) and str(error) == "cloud_unit_changed"
                paid = provider.possibly_sent
                generation_contacted = generation_contacted or paid
                unresolved = getattr(provider, 'unresolved', paid)
                generation_uncertain = generation_uncertain or bool(unresolved)
                if changed:
                    provider.discard()
                    if paid:
                        cloud_route(provider)
                    provider = None
                    if paid:
                        raise ValueError('patient_context_cloud_failed') from error
                    raise patient_context_refresh_state.Deferred("patient_context_cloud_source_changed") from error
                verdict, code, allow = stopped(provider)
                if (str(error) == "cloud_send_blocked" and saved and saved.get("_provider_contract")
                        and verdict != "local"):
                    # A fresh unsent remainder does not release a persisted cloud cursor.
                    raise patient_context_refresh_state.Deferred("patient_context_cloud_budget") from error
                if verdict == "defer":
                    # Nothing is discarded: answers this unit already received stay recoverable.
                    provider = None
                    raise patient_context_refresh_state.Deferred("patient_context_cloud_deferred:" + code) from error
                if paid:
                    cloud_route(provider)
                provider = None
                if paid and (not local_admit() or unresolved and verdict != "local"):
                    # a paid attempt failed and local cannot take over now: record
                    # the retry instead of re-sending the same unit every tick.
                    # Partial cloud progress keeps its cloud pin (a local record
                    # would resume the cloud cursor locally); a fresh unit is
                    # recorded under the local route, where selection finds it.
                    if saved and saved.get("_provider_contract"):
                        saved_route(saved["_source_meta"])
                    elif not cloud_requested:
                        local_route()
                    if isinstance(error, ValueError):
                        raise
                    raise ValueError("patient_context_cloud_failed") from error
        elif pinned or mode == "off":
            # A source already continuing locally keeps replacing its cloud units; with the
            # cloud off, the reason decides between waiting, replacing and the strict check.
            verdict, code, allow = cloud_off(progress, history, pinned=pinned)
            if history or cloud_requested and verdict != "local":
                cloud_refusal[0] = code   # a unit the cloud was never asked for has no stop to report
            if verdict == "defer":
                raise patient_context_refresh_state.Deferred("patient_context_cloud_deferred:" + code)
        if local_saved:
            saved.update(local_saved)
            if saved.get("complete"):
                raise patient_context_refresh_state.Deferred("patient_context_cloud_pending")
        if not local_admit():
            raise patient_context_refresh_state.Deferred("patient_context_local_capacity")
        def guard_local(prompt):
            guard_local_input(unit_key, mid, prompt, allow=allow)
            local_route()
        reset = bool(saved and saved.get("_provider_contract"))
        value = fn(None, reset, local_guard=guard_local)
        local_route()
        estimated_unit = mode == "dry_run"
        if mode == "shadow":
            shadow_call = (fn, unit_key, mid, value, set())
        return value
    # Lanes B/C bind both together only once a source is chosen; a failure
    # before that writes no source record (and never another source's).
    saved = source = None
    try:
        if lane == "A":
            guard_summary_sources()
            facts, total, projected = patient_context_refresh_sources._all_facts(ledger.db, pid, snapshot, workday_id, projected=True, cfg=cfg)
            old, old_meta = patient_context_refresh_state._latest(ledger.db, patient_context_refresh_state.KIND, pid)
            changed = patient_context_refresh_state._facts_sig(facts)
            if generation_plan is None or generation_plan['input_key'] != _generation_input(snapshot, facts):
                raise patient_context_refresh_state.Deferred('patient_context_source_changed')
            summary_scans = patient_context_refresh_sources._current_scans(
                ledger.db, pid, snapshot["sources"], snapshot["route"], cfg=cfg)
            cloud_sources_ready = all(scan.get("_provider_contract") for scan in summary_scans.values())
            same_backend = (not facts or mode != "on" or old_meta.get("local_fallback") is True
                            or old_meta.get("provider") == snapshot["route"]["provider"]
                            or not cloud_sources_ready and not old_meta.get("provider"))
            same = (old.get("facts_sig") == changed and old_meta.get("source_sig") == snapshot["source_sig"]
                    and patient_context_refresh_state.route_accepts(snapshot["route"], old_meta)
                    and same_backend)
            if (same_backend and old_meta.get("quality_audit_key") and old_meta.get("source_sig") == snapshot["source_sig"]
                    and old_meta.get("scan_sig") == frozen_scan_sig and patient_context_refresh_state.route_accepts(snapshot["route"], old_meta)
                    and patient_context_refresh_sources._summary_valid(ledger.db, pid, old, {context_plan.source_key(s): s for s in snapshot["sources"]},
                                       allow_historical=True)):
                same = True
            if same:
                if old_meta.get('local_fallback') is True:
                    payload, _, _, _ = context_selection.summary_input(facts, snapshot['source_sig'])
                    # The kept overview is itself the local replacement of its cloud unit.
                    kept = cloud_stop(("pre_send", "local_pinned"), False, cloud_history('summary', None, None))
                    guard_local_input('summary', None, json.dumps(payload, ensure_ascii=False), allow=kept[2])
                saved_route(old_meta)
                for key in ("quality_repair_ref", "cloud_receipts"):
                    if key in old_meta:
                        meta[key] = old_meta[key]
            if facts and not same or quality_required:
                generation_plan['needed'] = True
                if facts and not same:
                    cloud_wait()
                begin_generation()
            if same and quality_required:
                generation_quality = True
                try:
                    quality_status, quality_result = quality_step(
                        ledger, pid, cfg, workday_id, old, deadline, generation_admit, base_meta=old_meta)
                    repaired = None
                    if quality_status == "candidate":
                        if quality_result.get("repair_ref") is not None:
                            meta["quality_repair_ref"] = quality_result["repair_ref"]
                        by_id = {fact["id"]: fact for fact in quality_result["facts"]}
                        chosen = [by_id[fid] for fid in quality_result["fact_ids"]]
                        chosen_ids = {fact["id"] for fact in chosen}
                        selected_facts = (chosen + [fact for fact in facts if fact["id"] not in chosen_ids])[:context_selection.MAX_FACTS]
                        repaired = {**old, "facts": selected_facts, "facts_sig": patient_context_refresh_state._facts_sig(selected_facts),
                            "summary": [{"text": context_selection.summary_text(fact), "fact_ids": [fact["id"]]}
                                        for fact in chosen],
                            "temporal_view": {key: quality_result["state"][key] for key in ("groups", "relations")},
                            "facts_total": len(quality_result["facts"]),
                            "truncated": len(quality_result["facts"]) > len(selected_facts)}
                        quality_status, quality_result = quality_step(
                            ledger, pid, cfg, workday_id, repaired, deadline, generation_admit, base_doc=old, base_meta=old_meta)
                except (patient_context_refresh_state.Deferred, ValueError, RuntimeError) as exc:
                    raise patient_context_refresh_state.Deferred(
                        "summary_quality_unavailable:" + (_quality_unavailable_detail(exc) or type(exc).__name__)) from None
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
                quality_policy, problems = semantic_policy.runtime_config(cfg)
                if problems or not patient_context_quality_receipts._quality_scope_allowed(ledger.db, pid, cfg, snapshot["sources"]):
                    raise patient_context_refresh_state.Deferred("summary_quality_promotion")
                meta["quality_audit_key"] = quality_identity["audit_key"]
                if quality_skipped:
                    meta["quality_audit_status"] = "SKIPPED"
                else:
                    meta.pop("quality_audit_status", None)
                meta["quality_schedule_key"] = patient_context_refresh_state._digest([cfg, quality_identity["assessment"]["code"]])
            if not facts:
                local_route()
            summary = (repaired["summary"] if same and quality_required and repaired is not None
                       else old["summary"] if same else [] if not facts else infer(
                lambda backend, reset, metrics=None, comparison=False, local_guard=None: patient_context_quality_controller._summary(cfg, facts, deadline,
                    generation_admit if backend else lambda: local_admit() and generation_admit(),
                    source_sig=snapshot["source_sig"], local_guard=local_guard, **({"provider": backend} if backend else {}),
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
                      "coverage": patient_context_refresh_sources._coverage(ledger.db, pid, snapshot["sources"], snapshot["route"], facts, cfg=cfg)}
        else:
            scans = patient_context_refresh_sources._current_scans(ledger.db, pid, snapshot["sources"], snapshot["route"], cfg=cfg)
            for candidate in snapshot["sources"]:
                if not context_plan.source_usable(candidate):
                    continue
                stored = scans.get(context_plan.source_key(candidate), {})
                cloud_pending = (lane == "B" and patient_context_refresh_readiness._needs_cloud_scan(
                    stored, mode == "on"))
                if not patient_context_refresh_readiness._ready({} if cloud_pending else stored, now):
                    continue
                if (lane == "B" and (not stored.get("complete") or cloud_pending)) or (
                        lane == "C" and stored.get("complete") and not stored.get("audited")):
                    meta["source_sig"] = context_plan.source_signature(candidate)
                    meta["original_source_sig"] = context_plan.original_source_signature(candidate)
                    meta["source_key"] = context_plan.source_key(candidate)
                    saved, source = stored, candidate
                    source = next(context_queries.patient_context_sources(ledger.db, pid, mid=context_plan.source_key(candidate), cfg=cfg), None)
                    if source is None:
                        return {"status": "source_changed", "done": 0}
                    selected_source[0] = source
                    selected_occurrence[0] = patient_context_refresh_sources._source_occurrence(ledger.db, pid, source)
                    if lane == 'C' and isinstance(saved.get('_source_meta'), dict):
                        prior_meta = saved['_source_meta']
                        saved_route(prior_meta)
                        for key in ('source_binding', 'source_occurrence', 'source_occurrence_version', 'cloud_receipts'):
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
                        scan_input[0] = patient_context_refresh_state._cloud_scan_binding(
                            meta["source_sig"], meta["contract"], None, selected_occurrence[0])
                        planned, planned_cursor, _, _ = context_plan.scan_plan(source, saved)
                        planned_span = [sum(map(len, planned[:planned_cursor])), sum(map(len, planned[:planned_cursor + 1]))]
                        route = snapshot["route"]
                        # The unit this pass would write: the route's fresh unit, or the local
                        # unit of a source that already continues locally under a cloud route
                        # (its replaced cloud units keep their own keys).
                        local_unit = not route["provider"] or saved.get("_source_meta", {}).get("local_fallback") is True
                        prior_unit = patient_context_refresh_state._unit_key(meta["source_sig"], planned_span, meta["contract"],
                            route["provider"], route["local_model"] if local_unit else None, selected_occurrence[0])
                        del planned
                        if ledger.db.execute("SELECT 1 FROM artifacts WHERE kind=? AND project_id=? AND "
                                + current_meta_pred("meta", target=patient_context_refresh_state.CURRENT_VERSION)
                                + " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.unit_key')=? "
                                "AND COALESCE(json_extract(meta,'$.invalidated'),0)=0 END LIMIT 1",
                                (patient_context_refresh_state.CHUNK, pid, prior_unit)).fetchone():
                            # A past equal input is not a new dispatch right. A
                            # lost checkpoint cannot infer a completed cursor.
                            raise ValueError("patient_context_original_unit_unverified")
                        result = infer(lambda backend, reset, metrics=None, comparison=False, local_guard=None: patient_context_quality_controller._scan(cfg, source,
                            {} if reset else {**saved, "facts": []} if comparison else saved, workday_id, deadline,
                            admit if backend else local_admit, local_guard=local_guard,
                            **({"provider": backend} if backend else {}),
                            **({"metrics": metrics} if metrics is not None else {})),
                            unit_key="scan:" + str(context_plan.source_key(source)), mid=source["message_id"] or None, saved=saved)
                        result.update(source_occurrence_version=1, source_occurrence=selected_occurrence[0])
                    else:
                        # The audit keeps the scan's own backend identity (copied above).
                        result = {**saved, "audited": True, "audited_chunks_sig": saved["_chunks_sig"]}
                        # Reconstruct all quotations, refusing stored polarity or
                        # subject rewrites; confidence scores are not evidence.
                        audited_ids = []
                        for part in patient_context_refresh_sources._source_chunks(ledger.db, pid, source, snapshot["route"], saved):
                            if len(context_plan.validated_source_facts(source, part.get("facts"))) != len(part.get("facts", [])):
                                raise ValueError("patient_context_audit_mismatch")
                            audited_ids.append(part["_artifact_id"])
                        result["_audited_chunk_ids"] = audited_ids
                        if cloud_requested or patient_context_quality_receipts._has_prior_quality_failure(ledger.db, pid, purpose='source'):
                            try:
                                quality_status, quality_result = quality_step(
                                    ledger, pid, cfg, workday_id, {}, deadline, admit, source=source)
                            except (patient_context_refresh_state.Deferred, ValueError, RuntimeError) as exc:
                                result = None
                                raise patient_context_refresh_state.Deferred(
                                    "source_quality_unavailable:" + (_quality_unavailable_detail(exc) or type(exc).__name__)) from None
                            if quality_status == "source_candidate":
                                quality_status, quality_result = quality_step(
                                    ledger, pid, cfg, workday_id, {}, deadline, admit, source=source,
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
                                staged = patient_context_refresh_sources._source_repair_chunks(
                                    ledger.db, pid, source, context_artifacts.quality_binding(snapshot["route"]), accepted)
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
        deferred_reason = str(exc) or "patient_context_waiting"
        if (generation_claim is not None and not generation_decision['resume'] and not generation_quality
                and not generation_uncertain and not generation_contacted and (deferred_reason in (
                    'patient_context_capacity_or_deadline', 'patient_context_admission_deferred', 'patient_context_turn_busy', 'patient_context_endpoint_unsent', 'patient_context_local_capacity')
                    or deferred_reason.startswith('patient_context_cloud_deferred:'))
                and (provider is None or not provider.possibly_sent)):
            return_generation('unsent', proven_unsent=True)
        discard_if_changed()
    except (ValueError, TypeError, KeyError, TimeoutError) as exc:
        failure = safe_error_code(exc)
    if result is None and failure is None and deferred_reason is None:
        return {"status": "deferred", "done": 0}
    if patient_context_refresh_state.time.monotonic() >= deadline or not admit():
        discard_if_changed()
        return {"status": "deferred", "done": 0}
    def quality_proofs(reader):
        source_pass_content = refreshed_quality = None
        if source_quality_identity is not None:
            if not patient_context_refresh_sources._source_quality_verified(
                    reader.db, pid, source, snapshot["route"], cfg, result):
                return None
            if not source_quality_skipped:
                source_pass_content = patient_context_quality_receipts._quality_pass_content(
                    reader.db, pid, source_quality_identity)
                if source_pass_content is None:
                    return None
        if quality_identity is not None:
            refreshed_quality = patient_context_refresh_sources._quality_capture(
                reader.db, pid, cfg, workday_id, result["summary"], quality_identity["assessment"],
                result["temporal_view"], source_keys=source_keys)
            if refreshed_quality["identity"] != quality_identity:
                return None
        return source_pass_content, refreshed_quality

    quality_current = None
    if quality_identity is not None or source_quality_identity is not None:
        with patient_context_refresh_state.capture_quality_detail() as detail:
            try:
                proofs, quality_current = patient_context_quality_receipts._quality_read(ledger, deadline, quality_proofs)
            except (ValueError, KeyError, TypeError, patient_context_refresh_state.Deferred):
                return _quality_source_changed(detail["code"] or "save_proof_unread")
        if proofs is None:
            return _quality_source_changed("save_proof_changed")
        source_pass_content, refreshed_quality = proofs
    version = ledger.db.execute("PRAGMA data_version").fetchone()[0]
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
        # A live gateway-restart intent ends the wait early too, so the
        # cron frees the session before its 60s cap.
        if waited >= 60 or patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.COMMIT_MARGIN >= deadline or not admit() or writer_lock.restart_intent_active():
            if (retainable and waited >= 60 and patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.COMMIT_MARGIN < deadline
                    and admit()):
                resume_cfg, resume_deadline, admit, complete = yield {
                    "status": "deferred", "done": 0, "reason": "run_lock", "lock_wait_s": waited}
                # A new tick grants time to save, never more time to keep this
                # old response. Expiry drops only the in-memory body; its paid
                # receipt remains and cannot authorize another POST.
                deadline = min(deadline, resume_deadline)
                if patient_context_refresh_state.time.monotonic() + patient_context_refresh_state.COMMIT_MARGIN >= deadline:
                    return {"status": "deferred", "done": 0, "reason": "patient_context_completed_expired"}
                import notify_spool
                if (not admit() or not extract_llm._writer_current(full=True)
                        or not extract_llm._same_but_operating_limits(resume_cfg, completed_cfg)
                        or notify_spool.restore_writer_hold(notify_spool.data_root(ledger)) is not None):
                    return {"status": "source_changed", "done": 0}
                try:
                    guard_provider(provider)
                    if _completed_cloud_receipts(ledger, provider) != completed_receipts:
                        raise ValueError("patient_context_cloud_receipt_changed")
                except cloud_inference.CloudObservationIncomplete:
                    return {"status": "deferred", "done": 0, "reason": "patient_context_readiness_unverified"}
                except cloud_inference.CloudFallback:
                    discard_if_changed()
                    return {"status": "source_changed", "done": 0}
                except ValueError:
                    return {"status": "source_changed", "done": 0}
                version = ledger.db.execute("PRAGMA data_version").fetchone()[0]
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
            if lane == 'A':
                try:
                    guard_summary_sources()
                except patient_context_refresh_state.Deferred:
                    ledger.db.rollback()
                    return {"status": "deferred", "done": 0, "reason": "patient_context_cloud_not_sent",
                            "guard_error": guard_error[0]}
            for check in local_checks:
                # Read-only here: nothing is recorded as replaced before every check below passed.
                try:
                    guard_local_input(*check[:3], allow=check[3])
                except patient_context_refresh_state.Deferred as refused:
                    ledger.db.rollback()
                    return {"status": "deferred", "done": 0, "reason": str(refused),
                            "guard_error": guard_error[0], "cloud_refusal": cloud_refusal[0]}
            if quality_current is not None:
                with patient_context_refresh_state.capture_quality_detail() as detail:
                    fenced = quality_current()
                if not fenced:
                    ledger.db.rollback()
                    return _quality_source_changed(detail["code"])
            if quality_identity is not None:
                live_cfg = extract_llm.load_config()
                live_policy, problems = semantic_policy.runtime_config(live_cfg)
                try:
                    repair_verified = patient_context_refresh_readiness._summary_repair_publication(
                        ledger.db, pid, cfg, result, meta, refreshed_quality,
                        quality_identity['assessment'], snapshot['route'])
                except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, RuntimeError):
                    repair_verified = False
                refusal = (_quality_save_refusal(ledger.db, pid, live_cfg, live_policy, problems, fresh["sources"],
                                                 quality_identity, quality_keys, quality_skipped,
                                                 {row["fact_ids"][0] for row in result["summary"]})
                           or (None if repair_verified else "save_repair_unverified"))
                if refusal is not None:
                    ledger.db.rollback()
                    return _quality_source_changed(refusal)
            if source_quality_identity is not None:
                live_cfg = extract_llm.load_config()
                live_policy, problems = semantic_policy.runtime_config(live_cfg)
                import semantic_jev   # the adoption check below reads the key again
                refusal = _quality_save_refusal(ledger.db, pid, live_cfg, live_policy, problems, fresh["sources"],
                                                source_quality_identity, source_quality_keys, source_quality_skipped,
                                                set(), expected=source_pass_content)
                if refusal is not None:
                    ledger.db.rollback()
                    return _quality_source_changed(refusal)
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
                if changed and provider is not None and answer_stale(actual):
                    provider.discard()
                return {"status": "source_changed" if changed else "deferred", "done": 0,
                        **({"reason": "patient_context_readiness_unverified"} if not changed else {})}
            moved = (generation_claim is not None and not context_quota.owns(
                         ledger.db, pid, generation_claim, now=patient_context_refresh_state.time.time())
                     or fresh["source_sig"] != snapshot["source_sig"] or scans_now != frozen_scan_sig
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
                if provider is not None and moved and answer_stale(fresh):
                    provider.discard()
                return {"status": "source_changed", "done": 0}
            if failure is None and deferred_reason is None and result is not None and local_checks:
                # The local result is about to be saved: record the paid requests it replaces in
                # this same transaction, then prove once more, under the same unit keys, that
                # nothing else holds its prompts and none of those requests is left unrecorded.
                # Every return below this point rolls these rows back with the result.
                try:
                    for check in local_checks:
                        if check[3]:
                            guard_local_input(*check[:3], allow=True, supersede=True)
                    for check in local_checks:
                        guard_local_input(*check[:3], allow=check[3], recorded=True)
                except patient_context_refresh_state.Deferred as refused:
                    ledger.db.rollback()
                    return {"status": "deferred", "done": 0, "reason": str(refused),
                            "guard_error": guard_error[0], "cloud_refusal": cloud_refusal[0]}
                if lane == "B" and replaced:
                    # The source's cloud scan cites receipts that are now ended: it can never
                    # verify again, and would shadow the local scan once the cloud route returns.
                    patient_context_refresh_state._retire_cloud_scan_tx(
                        ledger, pid, source["message_id"], meta["source_key"], meta["source_sig"])
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
            chosen = lane != "A" and source is not None
            prior = progress if lane == "A" else (saved or {})
            attempts = prior.get("attempts", 0) + 1 if failure else 0
            retry = {"failure": failure, "held": failure in ("patient_context_unsplittable_assertion", "patient_context_original_unit_unverified"),
                     "attempts": attempts, "checks": patient_context_refresh_state._checks(),
                     "failure_at": now if failure else None,
                     "next_try": (now + (86400 if attempts >= 5 else 300 * 2 ** attempts)) if failure else 0}
            progress = {**progress, "last_attempt": now, "last_attempt_unit": rotation.get("units", 0) + 1,
                        "workday_id": workday_id,
                        **({"source_sig": snapshot["source_sig"], "source_scope": source_keys} if lane == "A" else {}),
                        **(retry if lane == "A" and deferred_reason is None else {})}
            if lane == "A" and deferred_reason is not None:
                wait = cloud_backlog.deferral_delay(deferred_reason)
                progress["next_try"] = ((generation_wait.get('next_at') or now + wait) if generation_wait is not None
                    else now if deferred_reason == "summary_quality_pending" else now + wait)
                progress["quality_state"] = (deferred_reason[len("summary_quality_"):]
                    if deferred_reason.startswith("summary_quality_") else None)
            elif lane == "A":
                progress.pop("quality_state", None)
            if failure and chosen:
                patient_context_refresh_state._replace(ledger, patient_context_refresh_state.SCAN, pid, {**{k: v for k, v in saved.items() if not k.startswith("_")}, **retry}, meta, source["message_id"])
            if lane == "C" and chosen and deferred_reason and deferred_reason.startswith("source_quality_"):
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
                        or not patient_context_refresh_state.route_accepts(snapshot["route"], prior_meta)
                        or prior_doc.get("facts_sig") != result.get("facts_sig")
                        or prior_doc.get("summary") != result.get("summary"))
                    saved_scans = patient_context_refresh_sources._current_scans(ledger.db, pid, snapshot["sources"], snapshot["route"], cfg=cfg)
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
                    if generation_plan.get("rebuilt"):
                        progress.pop("rebuild_summary", None)   # the rebuilt sources have their overview
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
                    if lane == "B" and replaced:
                        # The overview of these rebuilt sources may skip the generation interval
                        # once; an earlier one-time skip no longer binds it.
                        progress["rebuild_summary"] = True
                        progress.pop("catchup_generation", None)
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
                    and source_quality_keys == patient_context_refresh_state._credential_tag(
                        semantic_jev.api_key(), cloud_policy.selected_key(extract_llm.HOME, extract_llm.load_config()))
                    and patient_context_quality_receipts._quality_scope_allowed(ledger.db, pid, extract_llm.load_config(), [final_source])
                    and context_queries.patient_context_scope(ledger.db, pid).get('fetch_reason') not in ('forbidden', 'permission_denied', 'access_revoked')
                    and admit() and extract_llm._writer_current(full=True) and patient_context_refresh_state.time.monotonic() < deadline
                    and ledger.db.total_changes == adoption_before + adoption_writes[0])
                if not valid_adoption:
                    ledger.db.rollback()
                    return {"status": "source_changed", "done": 0}
            if not failure and estimated_unit:
                cloud_policy.dryrun_tx(ledger, route="patient_context", project_id=pid,
                    unit_key=lane, input_chars=sum(source.get("body_length", 0) for source in snapshot["sources"]),
                    max_tokens=cloud_policy.selection_max_tokens(cloud_selection), model=cloud_selection["model"])
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
            if generation_claim is not None:
                outcome = ('complete' if not failure and deferred_reason is None else
                    'failed' if (failure in ('patient_context_length_stop', 'patient_context_invalid_response', 'patient_context_unsupported_summary')
                    or deferred_reason == 'summary_quality_failed') and not generation_uncertain else 'unknown')
                if not context_quota.finish_tx(ledger.db, pid, generation_claim,
                        now=patient_context_refresh_state.time.time(), outcome=outcome):
                    ledger.db.rollback()
                    return {'status': 'source_changed', 'done': 0}
            ledger.db.commit()
            generation_claim = None
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
            if lane == 'A':
                begin_generation(after_commit=True)
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
        if lane == 'A' and generation_claim is not None:
            return_generation('complete' if cloud_value is not None else 'unknown')
    return {"status": "failed" if failure else "deferred" if deferred_reason else "progressed",
            "done": int(not failure and deferred_reason is None), "lane": lane,
            # A repair waiting for a child keeps its quality state above; the pass reports the wait.
            **({"reason": failure or (CHILD_WAIT_REASON if repair_waits[0] else deferred_reason)}
               if failure or deferred_reason else {}),
            # The gate that stopped a quality deferral this pass reports (observation only).
            **({"quality_detail": patient_context_refresh_state.bounded_quality_detail(quality_detail[0])}
               if not failure and not repair_waits[0] and quality_detail[0] is not None and deferred_reason
               and deferred_reason.startswith(("summary_quality_", "source_quality_")) else {}),
            **({"guard_error": guard_error[0]} if guard_error[0] is not None else {}),
            **({"cloud_refusal": cloud_refusal[0]} if cloud_refusal[0] is not None else {}),
            "lock_wait_s": waited, **stats}
