"""Resume bounded whole-corpus inference reprocessing from scheduler witnesses."""
from __future__ import annotations
import mcs_util
import semantic_store
import copy
import math
import sqlite3
import time
from inference_version import (CURRENT_VERSION, VERSION_KEY, is_current)
import semantic_runtime as runtime
from semantic_policy import (JOB_KIND, runtime_config)
import semantic_notice

from semantic_scheduler_state import (
    _reservation_sched,
    _sched_state,
    _sched_update,
)
from semantic_store import _REPROCESS_JOB_MAX_BYTES as _REPROCESS_JOB_MAX_BYTES

def _reprocess_patient_state(ledger, row, cfg, scfg, witness=None):
    """Inspect patient proofs without replacing a rollup whose freshness is unproved."""
    import patient_context_refresh
    from mcs_queries import paused
    pid = row['project_id']
    if (scfg['project_ids'] is not None and pid not in scfg['project_ids']
            or row['fetch_reason'] in ('forbidden', 'permission_denied', 'access_revoked')):
        return 'excluded'
    import semantic_intake_hold
    if semantic_intake_hold.has_unstored(ledger.db, pid):
        return 'held'
    if paused(ledger.db, pid):
        return 'paused'
    proof = patient_context_refresh.inference_coverage(ledger.db, pid, cfg)
    if not is_current(proof):
        return 'pending'
    if not proof['complete']:
        dependency = proof.get('dependencies', {}).get('coverage', {})
        if dependency.get('terminal', 0):
            return 'terminal_gap'
        if dependency.get('repairable', 0):
            return 'repair_pending'
        counts = proof['sources']
        if counts['failed']:
            return 'failed'
        if counts['held'] or proof['status'] == 'off':
            return 'held'
        if counts['incomplete'] or proof['status'] in ('unacquired', 'dependency_gap'):
            return 'incomplete'
        return 'pending'
    import rollup
    proof = rollup.completion_coverage(ledger.db, pid, cfg)
    if not is_current(proof) or proof.get('current') is not True:
        return 'rollup_unverified'
    if witness is not None:
        witness.update(proof)
    return 'current'


_REPROCESS_PROOF_CONTRACT = 1


def _reprocess_proof_generation(ledger, cfg, scfg):
    """Bind completion to interpretation and scope, never credentials or spending caps."""
    import patient_context_refresh
    import rollup
    import cloud_inference
    cloud = cfg.get('cloud_llm', {})
    cloud = cloud if isinstance(cloud, dict) else {'mode': 'invalid'}
    contract_cfg = dict(cfg)
    contract_cloud = {key: value for key, value in cloud.items()
                      if key not in runtime.CLOUD_OPERATING_LIMITS}
    if contract_cloud:
        contract_cfg['cloud_llm'] = contract_cloud
    else:
        contract_cfg.pop('cloud_llm', None)
    return mcs_util.payload_hash({
        'contract': _REPROCESS_PROOF_CONTRACT, 'target_classification': 'empty-body-v1',
        'execution': runtime.execution_generation(scfg),
        'semantic': {key: scfg.get(key) for key in ('mode', 'summary_mode', 'loop_mode',
                     'extract_qc', 'project_ids', 'candidate_fingerprint')},
        'context': patient_context_refresh._contract(cfg),
        'context_options': patient_context_refresh.options(cfg),
        'cloud': {key: cloud.get(key) for key in ('mode', 'routes', 'project_ids', 'exclude_project_ids', 'model')},
        'cloud_contract': cloud_inference.cloud_provider_contract(contract_cfg),
        'cloud_repair_contract': cloud_inference.cloud_provider_contract(
            contract_cfg, route='semantic_backlog', purpose='quality_repair'),
        'rollup': rollup.config_generation(cfg),
        'dictionary': rollup.generation_signature(ledger.db),
        'references': tuple(ledger.db.execute('SELECT COUNT(*),MAX(artifact_id) FROM artifacts WHERE kind=?',
                                             (rollup.REF_KIND,)).fetchone()),
        'patients': tuple(ledger.db.execute('SELECT COUNT(*),MAX(project_id) FROM patients').fetchone())})


def _reprocess_proof_time_valid(progress, now):
    started, until = progress.get('proof_started_at'), progress.get('proof_valid_until')
    checked = progress.get('proof_checked_at', started)
    return (type(started) in (int, float) and math.isfinite(started) and 0 <= started <= now
            and type(checked) in (int, float) and math.isfinite(checked) and started <= checked <= now
            and (until is None or type(until) in (int, float) and math.isfinite(until) and now < until < 1e12))


def _reprocess_bookmark_sealed(ledger, old_progress, epochs, version, changes, cfg, scfg, generation):
    """Recheck a cached observation under the same writer fence without rewriting it."""
    import semantic_intake_hold
    ledger.db.execute('BEGIN IMMEDIATE')
    with ledger.db:
        current, _ = _reservation_sched(ledger)
        return (current.get('inference_reprocess') == old_progress
                and all(current.get(field, 0) == epochs[field]
                        for field in ('source_epoch', 'analysis_epoch', 'job_state_epoch'))
                and semantic_intake_hold.hold_epoch(ledger.db) == epochs['intake_hold_epoch']
                and semantic_intake_hold.unstored_epoch(ledger.db) == epochs['intake_unstored_epoch']
                and ledger.db.execute('PRAGMA data_version').fetchone()[0] == version
                and ledger.db.total_changes == changes
                and _reprocess_proof_generation(ledger, cfg, scfg) == generation
                and _reprocess_proof_time_valid(old_progress, time.time()))


def reprocess_step(ledger, cfg, *, limit=32, deadline=None):
    """Advance a finite keyset pass; input races never erase its accepted cursor."""
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError('inference_reprocess_limit')
    if ledger.db.in_transaction:
        raise ValueError('inference_reprocess_transaction_active')
    cfg = copy.deepcopy(cfg)
    scfg, errors = runtime_config(cfg)
    if errors or scfg['mode'] == 'off':
        return {'status': 'off', 'complete': False}
    generation = runtime.execution_generation(scfg)
    scope_generation = mcs_util.payload_hash(scfg['project_ids'])
    proof_generation = _reprocess_proof_generation(ledger, cfg, scfg)
    now = time.time()
    version = ledger.db.execute('PRAGMA data_version').fetchone()[0]
    changes = ledger.db.total_changes
    original = _sched_state(ledger)
    old_progress = original.get('inference_reprocess')
    fields = ('source_epoch', 'analysis_epoch', 'job_state_epoch')
    epochs = {field: original.get(field, 0) for field in fields}
    import semantic_intake_hold
    try:
        epochs['intake_hold_epoch'] = semantic_intake_hold.hold_epoch(ledger.db)
        epochs['intake_unstored_epoch'] = semantic_intake_hold.unstored_epoch(ledger.db)
    except sqlite3.Error:
        return {'status': 'intake_hold_unverified', 'complete': False}
    if any(original.get(field + '_invalid') or type(value) is not int
           or not 0 <= value < 2**63 for field, value in epochs.items()):
        return {'status': 'invalid_source_epoch', 'complete': False}
    progress = old_progress
    live_upper = ledger.db.execute('SELECT COALESCE(MAX(message_id),0) FROM messages').fetchone()[0]
    if (not isinstance(progress, dict) or not is_current(progress)
            or progress.get('execution_generation') != generation
            or progress.get('scope_generation') != scope_generation):
        progress = {VERSION_KEY: CURRENT_VERSION, 'execution_generation': generation,
                    'scope_generation': scope_generation, **epochs,
                    'upper': live_upper,
                    'cursor': 0, 'seed_cursor': 0, 'phase': 'seed', 'counts': {}}
    counters = {'current', 'current_skipped', 'pending', 'repair_required', 'held', 'failed', 'permission',
                'scope', 'paused', 'deleted', 'body_unavailable', 'empty_body'}
    if (progress.get('phase') not in ('seed', 'verify', 'verify_patients', 'waiting', 'complete')
            or any(type(progress.get(key)) is not int or not 0 <= progress[key] < 2**63
                   for key in ('cursor', 'upper'))
            or progress['cursor'] > progress['upper']
            or not isinstance(progress.get('counts'), dict)
            or any(key not in counters or type(value) is not int or not 0 <= value < 2**63
                   for key, value in progress['counts'].items())):
        return {'status': 'invalid_progress', 'complete': False}
    progress = {**progress, 'counts': dict(progress['counts'])}
    progress.setdefault('seed_cursor', progress['cursor'] if progress['phase'] == 'seed' else progress['upper'])
    if type(progress['seed_cursor']) is not int or not 0 <= progress['seed_cursor'] < 2**63:
        return {'status': 'invalid_progress', 'complete': False}
    changed = any(progress.get(field, 0) != value for field, value in epochs.items())
    proof_changed = progress.get('proof_generation') != proof_generation
    proof_expired = not _reprocess_proof_time_valid(progress, now)
    progress.update(future_upper=live_upper, tail_pending=live_upper > progress['seed_cursor'])
    progress.update(epochs)
    if changed:
        progress.update(dirty=True, complete=False)
    # Finished/waiting rounds are bookmarks, not an instruction to seed the
    # corpus again. Only a real source, adopted proof or job-state change wakes
    # a new bounded round. Operational settings grant no fresh attempt budget.
    if progress['phase'] in ('waiting', 'complete'):
        patient_counts = progress.get('patient_counts')
        proof_contract = progress.get('proof_contract')
        if (not isinstance(patient_counts, dict) or any(
                not isinstance(key, str) or type(value) is not int or not 0 <= value < 2**63
                for key, value in patient_counts.items())
                or proof_contract is not None and (type(proof_contract) is not int
                    or proof_contract != _REPROCESS_PROOF_CONTRACT)):
            return {'status': 'invalid_progress', 'complete': False}
        if not changed and not progress['tail_pending'] and not proof_changed and not proof_expired \
                and progress.get('proof_contract') == _REPROCESS_PROOF_CONTRACT \
                and ((progress['phase'] == 'waiting' and progress.get('complete') is False
                      and progress.get('proof_status') in ('incomplete', 'rollup_freshness_unverified'))
                     or (progress['phase'] == 'complete' and progress.get('complete') is True
                         and progress.get('proof_status') == 'current' and patient_counts.get('current', 0) > 0
                         and progress.get('proof_valid_until') is not None)):
            if not _reprocess_bookmark_sealed(ledger, old_progress, epochs, version, changes,
                                             cfg, scfg, proof_generation):
                return {'status': 'source_changed', 'complete': False, 'processed': 0}
            return {'status': progress['phase'], 'processed': 0, **progress}
        if not changed and not progress['tail_pending'] and progress.get('proof_contract') != _REPROCESS_PROOF_CONTRACT:
            counts = progress.get('patient_counts')
            if counts is not None and (not isinstance(counts, dict) or any(
                    not isinstance(key, str) or type(value) is not int or not 0 <= value < 2**63
                    for key, value in counts.items())):
                return {'status': 'invalid_progress', 'complete': False}
            if isinstance(counts, dict) and counts.get('current', 0):
                current, unverified = counts['current'], counts.get('rollup_unverified', 0)
                if current + unverified >= 2**63:
                    return {'status': 'invalid_progress', 'complete': False}
                counts = dict(counts)
                counts['rollup_unverified'] = current + unverified
                counts.pop('current')
                progress['patient_counts'] = counts
            reasons = progress.get('waiting_reasons')
            if reasons is not None and not isinstance(reasons, dict):
                return {'status': 'invalid_progress', 'complete': False}
            if isinstance(counts, dict) and counts.get('rollup_unverified', 0):
                reasons = dict(reasons or {})
                patients = reasons.get('patients')
                if patients is not None and (not isinstance(patients, dict) or any(
                        not isinstance(key, str) or type(value) is not int or not 0 <= value < 2**63
                        for key, value in patients.items())):
                    return {'status': 'invalid_progress', 'complete': False}
                patients = dict(patients or {})
                patients['rollup_unverified'] = counts['rollup_unverified']
                reasons['patients'] = patients
                progress['waiting_reasons'] = reasons
            stages = progress.get('remaining_stages')
            stages = list(stages) if isinstance(stages, list) else []
            if 'rollup_freshness_witness' not in stages:
                stages.append('rollup_freshness_witness')
            progress.update(phase='waiting', complete=False,
                            proof_status='rollup_freshness_unverified',
                            proof_reason='患者集約の現行性を確認できません',
                            remaining_stages=stages)
            # Keep old observations as unverified; the next call evaluates
            # this same corpus without resetting a job's attempt generation.
            progress['proof_contract'] = _REPROCESS_PROOF_CONTRACT
            progress['proof_generation'] = None
            if progress != old_progress:
                with _sched_update(ledger) as sched:
                    if (sched.get('inference_reprocess') != old_progress
                            or any(sched.get(field, 0) != epochs[field] for field in fields)
                            or semantic_intake_hold.hold_epoch(ledger.db) != epochs['intake_hold_epoch']
                            or semantic_intake_hold.unstored_epoch(ledger.db) != epochs['intake_unstored_epoch']
                            or ledger.db.execute('PRAGMA data_version').fetchone()[0] != version
                            or ledger.db.total_changes != changes):
                        return {'status': 'source_changed', 'complete': False, 'processed': 0}
                    sched['inference_reprocess'] = progress
            return {'status': progress['phase'], 'processed': 0, **progress}
        upper = ledger.db.execute('SELECT COALESCE(MAX(message_id),0) FROM messages').fetchone()[0]
        tail = upper > progress['seed_cursor']
        progress.update(phase='seed' if tail else 'verify',
                        cursor=progress['seed_cursor'] if tail else 0, upper=upper,
                        counts={}, dirty=False, verify_epochs=dict(epochs), complete=False,
                        proof_generation=proof_generation)
    if progress['phase'] in ('verify', 'verify_patients') and progress.get('proof_generation') != proof_generation:
        # Finish the bounded round, but never accept mixed-generation proofs.
        progress.update(dirty=True, complete=False)
    if progress['phase'] == 'verify_patients' and proof_expired:
        # Configuration/time changes invalidate accepted patient proofs, not
        # source enumeration or same-input retry rights.
        progress.update(patient_cursor=0, patient_counts={},
                        proof_contract=_REPROCESS_PROOF_CONTRACT, proof_started_at=now,
                        proof_checked_at=now, proof_valid_until=None, complete=False)
    if progress['phase'] == 'verify_patients':
        states = {'current', 'pending', 'held', 'paused', 'failed', 'incomplete', 'excluded',
                  'terminal_gap', 'repair_pending', 'rollup_unverified'}
        if (any(type(progress.get(key)) is not int or progress[key] < 0
                for key in ('patient_cursor', 'patient_upper'))
                or not isinstance(progress.get('patient_counts'), dict)
                or any(key not in states or type(value) is not int or value < 0
                       for key, value in progress['patient_counts'].items())):
            return {'status': 'invalid_progress', 'complete': False}
        rows = ledger.db.execute('SELECT project_id,fetch_reason FROM patients '
                                 'WHERE project_id>? AND project_id<=? ORDER BY project_id LIMIT ?',
                                 (progress['patient_cursor'], progress['patient_upper'], limit)).fetchall()
        counts = dict(progress['patient_counts'])
        processed = 0
        for row in rows:
            if deadline is not None and time.monotonic() >= deadline:
                break
            witness = {}
            state = _reprocess_patient_state(ledger, row, cfg, scfg, witness)
            if state == 'current':
                until = witness.get('valid_until')
                checked = witness.get('checked_at')
                if (type(until) not in (int, float) or not math.isfinite(until) or not now < until < 1e12
                        or type(checked) not in (int, float) or not math.isfinite(checked) or not now <= checked < until):
                    state = 'rollup_unverified'
                else:
                    prior = progress.get('proof_valid_until')
                    progress['proof_valid_until'] = until if prior is None else min(prior, until)
                    progress['proof_checked_at'] = max(progress.get('proof_checked_at', now), checked)
            counts[state] = counts.get(state, 0) + 1
            progress['patient_cursor'] = row['project_id']
            processed += 1
        with _sched_update(ledger) as sched:
            # Patient proofs are read-only. No local or external writer may
            # change their inputs before this batch's cursor is adopted.
            if (sched.get('inference_reprocess') != old_progress
                    or any(sched.get(field, 0) != epochs[field] for field in fields)
                    or ledger.db.execute('PRAGMA data_version').fetchone()[0] != version
                    or ledger.db.total_changes != changes
                    or _reprocess_proof_generation(ledger, cfg, scfg) != proof_generation):
                return {'status': 'source_changed', 'complete': False, 'processed': 0}
            actual = {field: sched.get(field, 0) for field in fields}
            actual['intake_hold_epoch'] = semantic_intake_hold.hold_epoch(ledger.db)
            actual['intake_unstored_epoch'] = semantic_intake_hold.unstored_epoch(ledger.db)
            if any(sched.get(field + '_invalid') or type(value) is not int or not 0 <= value < 2**63
                   for field, value in actual.items()):
                return {'status': 'invalid_source_epoch', 'complete': False, 'processed': 0}
            progress['dirty'] = progress.get('dirty', False) or actual != epochs
            progress.update(actual)
            progress['patient_counts'] = counts
            if processed == len(rows) and (len(rows) < limit or progress['patient_cursor'] >= progress['patient_upper']):
                messages = progress.get('coverage', {}).get('counts', {})
                blocked_messages = {key: value for key, value in messages.items()
                    if value and key in ('pending', 'repair_required', 'held', 'failed', 'paused',
                                         'body_unavailable', 'empty_body')}
                blocked_patients = {key: value for key, value in counts.items() if value and key not in ('current', 'excluded')}
                upper = ledger.db.execute('SELECT COALESCE(MAX(message_id),0) FROM messages').fetchone()[0]
                tail = upper > progress['seed_cursor']
                progress.update(future_upper=upper, tail_pending=tail)
                dirty = progress.get('dirty', False) or progress.get('verify_epochs', actual) != actual
                proof_time_valid = _reprocess_proof_time_valid(progress, time.time())
                progress['complete'] = bool((counts.get('current', 0) + counts.get('current_skipped', 0))
                    and not blocked_messages and not blocked_patients
                    and not tail and not dirty and not actual['intake_unstored_epoch'] and proof_time_valid
                    and progress.get('proof_valid_until') is not None
                    and progress.get('proof_generation') == proof_generation)
                progress['waiting_reasons'] = {'messages': blocked_messages, 'patients': blocked_patients}
                if actual['intake_unstored_epoch']:
                    progress['waiting_reasons']['intake_unstored'] = 'present'
                progress['remaining_stages'] = (['source_or_patient_proof']
                    if blocked_messages or blocked_patients or tail or dirty else [])
                unverified = bool(counts.get('rollup_unverified') or not counts or not proof_time_valid)
                progress['proof_status'] = ('current' if progress['complete'] else
                    'rollup_freshness_unverified' if unverified else 'incomplete')
                if unverified:
                    progress['proof_reason'] = '患者集約の現行性を確認できません'
                    progress['remaining_stages'].append('rollup_freshness_witness')
                else:
                    progress.pop('proof_reason', None)
                progress['verified_at'] = time.time()
                if tail or dirty:
                    progress.update(phase='seed' if tail else 'verify',
                        cursor=progress['seed_cursor'] if tail else 0, upper=upper,
                        counts={}, dirty=False, verify_epochs=dict(actual),
                        proof_generation=proof_generation)
                else:
                    progress['phase'] = 'complete' if progress['complete'] else 'waiting'
            sched['inference_reprocess'] = progress
        return {'status': progress['phase'], 'processed': processed, **progress}
    rows = ledger.db.execute(
        'SELECT m.message_id,m.project_id,m.parent_id,m.body_state,m.body_text,'
        'p.project_id AS patient_id,p.fetch_reason FROM messages m '
        'LEFT JOIN patients p ON p.project_id=m.project_id '
        'WHERE m.message_id>? AND m.message_id<=? ORDER BY m.message_id LIMIT ?',
        (progress['cursor'], progress['upper'], limit)).fetchall()
    planned, sources = [], {}
    for row in rows:
        if deadline is not None and time.monotonic() >= deadline:
            break
        extra = {'deadline': deadline} if scfg['summary_mode'] == 'off' and scfg['loop_mode'] != 'off' else {}
        state = semantic_store.reprocess_target_state(ledger, row, scfg, cfg=cfg, **extra)
        replay = None
        if state in ('current', 'current_skipped', 'pending', 'repair_required', 'held', 'failed'):
            root = row['parent_id'] or row['message_id']
            key = (row['project_id'], root)
            old = ledger.db.execute('SELECT substr(payload,1,?) AS payload,state FROM fetch_jobs WHERE kind=? '
                                    'AND project_id=? AND message_id=?',
                                    (_REPROCESS_JOB_MAX_BYTES + 1, JOB_KIND, *key)).fetchone()
            pl = semantic_store._reprocess_job_payload(ledger, old, *key) if old is not None else {}
            import semantic_intake_hold
            semantic_intake_hold.validate_indexes(ledger.db)
            if semantic_intake_hold.has_held_root(ledger.db, *key):
                planned.append((row, 'held', None))
                continue
            if old is not None and pl is None:
                planned.append((row, 'held', None))
                continue
            if state in ('current', 'current_skipped'):
                planned.append((row, state, None))
                continue
            if key not in sources:
                sources[key] = ledger._semantic_source_generation(*key)
            if (not is_current(pl)
                    or pl.get('execution_contract') == 1 and pl.get('execution_generation') != generation
                    or pl.get('source_generation') != sources[key]
                    or not isinstance(pl.get('targets'), list) or row['message_id'] not in pl['targets']):
                arrival = (semantic_notice.origin_event_id(ledger.db, row['project_id'], [row['message_id']])
                           if old is not None and old['state'] == 'pending' and pl.get('eligible')
                           and not pl.get('notification_free') else None)
                replay = (sources[key], arrival)
        planned.append((row, state, replay))
    with _sched_update(ledger) as sched:
        if (sched.get('inference_reprocess') != old_progress
                or any(sched.get(field, 0) != epochs[field] for field in fields)
                or semantic_intake_hold.hold_epoch(ledger.db) != epochs['intake_hold_epoch']
                or semantic_intake_hold.unstored_epoch(ledger.db) != epochs['intake_unstored_epoch']
                or ledger.db.execute('PRAGMA data_version').fetchone()[0] != version
                or ledger.db.total_changes != changes):
            return {'status': 'source_changed', 'complete': False, 'processed': 0}
        for row, state, replay in planned:
            if replay is not None:
                ledger.semantic_seed_tx(row['project_id'], [row['message_id']],
                    {'source': 'inference_reprocess', 'notification_free': True,
                     'inference_generation': generation, 'arrival_event_id': replay[1]},
                    _source_generation=replay[0])
            progress['counts'][state] = progress['counts'].get(state, 0) + 1
            progress['cursor'] = row['message_id']
            if progress['phase'] == 'seed':
                progress['seed_cursor'] = row['message_id']
        if len(planned) == len(rows) and (len(rows) < limit or progress['cursor'] >= progress['upper']):
            if progress['phase'] == 'seed':
                progress['seed_cursor'] = max(progress['seed_cursor'], progress['upper'])
                progress.update(phase='verify', cursor=0, counts={}, dirty=False, verify_epochs=dict(epochs),
                                proof_generation=proof_generation)
            else:
                progress['coverage'] = {'counts': progress['counts'], 'observed_at': time.time(),
                                       'upper': progress['upper'], **epochs}
                progress.update(phase='verify_patients', counts={}, patient_cursor=0, patient_counts={},
                    proof_contract=_REPROCESS_PROOF_CONTRACT,
                    proof_started_at=time.time(), proof_checked_at=time.time(), proof_valid_until=None,
                    patient_upper=ledger.db.execute('SELECT COALESCE(MAX(project_id),0) FROM patients').fetchone()[0])
        progress['complete'] = False
        progress['tail_pending'] = progress['future_upper'] > progress['seed_cursor']
        progress['remaining_stages'] = ['patient_context', 'rollup', 'karte']
        sched['inference_reprocess'] = progress
    return {'status': progress['phase'], 'processed': len(planned), **progress}


_REALTIME_ORDER = ("CASE WHEN next_try<=updated_at THEN updated_at "
                   "ELSE created_at END DESC, job_id DESC")


_REALTIME_KEY = "CASE WHEN next_try<=updated_at THEN updated_at ELSE created_at END"


LONG_FREE_DEFER_CAP = 24


_LONG_DEFERS_MAX = 500


_LONG_DEFERS_SCAN_BYTES = 8 * 1024 * 1024
