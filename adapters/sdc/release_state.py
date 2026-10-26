"""署名済みreleaseのTrust・基線・全sequence bindingを私有領域へ保存する。"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import threading

from . import release_contract as contract
from . import release_apply_contract as apply_contract
from .storage import MAX_FILE

NAME = 'release-owner.json'
ACTIVATION = 'release-owner-activation.json'
SCHEMA = 'mcs.release-owner.v1'
ACTIVATION_SCHEMA = 'mcs.release-owner-activation.v1'
OWNER_ID = 'mcs.updater'
OWNER_REVISION = 1


class ReleaseStateError(ValueError):
    """制御状態の拒否理由だけを示し、保存値や私有pathを出さない。"""
    def __init__(self, code='INVALID'):
        super().__init__('RELEASE_OWNER_' + code)


def _check(ok, code='INVALID'):
    if not ok:
        raise ReleaseStateError(code)


def _fields(value, names):
    _check(type(value) is dict and set(value) == set(names))


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False))


def _spent(value):
    _fields(value, ('operation', 'grant', 'admission', 'grant_envelope_sha256', 'witness_state',
                    'preconditions', 'start_receipt_id', 'started_at'))
    at = value['started_at']
    _check(contract.integer(at) and contract.uuid(value['start_receipt_id'])
           and contract.hex64(value['grant_envelope_sha256']))
    operation = apply_contract.validate_apply_typed_payload(apply_contract.PURPOSES[1], value['operation'], now_ms=at)
    grant = apply_contract.validate_apply_grant(value['grant'], now_ms=at)
    admission = apply_contract.validate_apply_admission(value['admission'], now_ms=at)
    _check(all(operation[key] == grant[key] for key in (*apply_contract.COMMON, *apply_contract.BOUND)))
    _check(all(grant[key] == admission[key] for key in ('operation_id', 'payload_hash', 'receipt_id',
                                                       'admission_id', 'owner_generation')))
    _check(admission['admitted_at'] <= grant['issued_at'] <= at
           and grant['expires_at'] <= operation['expires_at']
           and operation['window']['starts_at'] <= at < operation['window']['ends_at'])
    witness = contract.validate_release_state(value['witness_state'])
    preconditions = apply_contract.validate_apply_preconditions(value['preconditions'])
    _check(witness['unresolved'] is None
           and grant['state_sha256'] == apply_contract.hash_apply_state(witness)
           and grant['preflight_sha256'] == contract.hash_release_preconditions(preconditions))
    _check(all(witness[key] == grant[key] for key in (
        *contract.EXPECTED_FIELDS, 'tenant', 'installation', 'device_id', 'realm_epoch',
        'trust_digest', 'root_version', 'metadata_epoch')))
    return {**value, 'operation': operation, 'grant': grant, 'admission': admission,
            'witness_state': witness, 'preconditions': preconditions}


def _validate(value):
    try:
        _fields(value, ('schema_version', 'root_fingerprint', 'lock_fingerprint', 'entry_lock_fingerprint', 'activation_sha256', 'repo_fingerprint', 'owner_id',
                        'owner_revision', 'owner_evidence_sha256', 'revision', 'trust', 'state',
                        'baseline', 'bindings', 'spent', 'outbox'))
        _check(value['schema_version'] == SCHEMA and value['owner_id'] == OWNER_ID
               and type(value['owner_revision']) is int and value['owner_revision'] == OWNER_REVISION
               and contract.positive(value['revision']))
        _check(all(contract.hex64(value[key]) for key in ('root_fingerprint', 'lock_fingerprint', 'entry_lock_fingerprint', 'activation_sha256', 'repo_fingerprint', 'owner_evidence_sha256')))
        trust = contract.validate_release_trust(value['trust'])
        state = contract.validate_release_state(value['state'])
        _check(all(state[key] == trust[key] for key in contract.EXPECTED_FIELDS)
               and state['trust_digest'] == contract.hash_release_trust(trust)
               and state['root_version'] == trust['root_version']
               and state['metadata_epoch'] == trust['metadata_epoch'])
        baseline = value['baseline']
        _fields(baseline, ('release_id', 'code_seq', 'commit', 'lock_sha256', 'schema',
                           'profile_id', 'profile_sha256', 'runtime_fingerprint'))
        _check(contract.hex64(baseline['release_id']) and contract.positive(baseline['code_seq'])
               and contract.match(baseline['commit'], r'[0-9a-f]{40}')
               and contract.integer(baseline['schema']) and contract.slug(baseline['profile_id'])
               and all(contract.hex64(baseline[key]) for key in ('lock_sha256', 'profile_sha256', 'runtime_fingerprint')))
        # No saved Git HEAD or version string can fill a missing signed installation baseline.
        _check(state['installed'] is not None and state['highest_installed'] is not None
               and state['highest_seen'] is not None, 'UNKNOWN_BASELINE')
        _check(state['installed'] == {key: baseline[key] for key in ('release_id', 'code_seq')})
        bindings = value['bindings']
        _check(type(bindings) is list and bindings)
        seen = {}
        previous = 0
        for binding in bindings:
            _fields(binding, ('stream_id', 'code_seq', 'release_id'))
            _check(binding['stream_id'] == state['stream_id'] and contract.positive(binding['code_seq'])
                   and binding['code_seq'] > previous and contract.hex64(binding['release_id']))
            previous = binding['code_seq']
            seen[binding['code_seq']] = binding['release_id']
        for point in contract.STATE_POINTS:
            _check(seen.get(state[point]['code_seq']) == state[point]['release_id'], 'BINDING_MISSING')
        _check(state['highest_seen']['code_seq'] == previous)
        spent = value['spent']
        _check(type(spent) is list)
        operation_ids, grant_ids, fingerprints = set(), set(), set()
        previous = None
        for record in spent:
            record = _spent(record)
            op, grant = record['operation'], record['grant']
            _check(op['operation_id'] not in operation_ids and grant['grant_id'] not in grant_ids
                   and record['grant_envelope_sha256'] not in fingerprints)
            _check(seen.get(op['target_code_seq']) == op['release_id'], 'BINDING_MISSING')
            if previous is not None:
                _check(record['started_at'] >= previous)
            previous = record['started_at']
            operation_ids.add(op['operation_id'])
            grant_ids.add(grant['grant_id'])
            fingerprints.add(record['grant_envelope_sha256'])
        if spent:
            last = spent[-1]['operation']
            _check(state['unresolved'] == {'operation_id': last['operation_id'],
                                          'plan_sha256': last['plan_hash'], 'phase': 'unknown'})
        else:
            _check(state['unresolved'] is None)
        _outbox(value['outbox'], spent)
        _check(len(json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')) <= MAX_FILE,
               'CAPACITY_EXCEEDED')
        return _copy(value)
    except (TypeError, KeyError, ValueError, OverflowError, RecursionError) as error:
        if isinstance(error, ReleaseStateError):
            raise
        raise ReleaseStateError() from None


def _read(store):
    value = store.read(NAME)
    _check(value is not None, 'UNKNOWN_BASELINE')
    value = _validate(value)
    _check(value['root_fingerprint'] == store.root_fingerprint(), 'ROOT_MISMATCH')
    _check(value['lock_fingerprint'] == _lock_fingerprint(store), 'LOCK_MISMATCH')
    _check(value['entry_lock_fingerprint'] == _lock_fingerprint(store, 'release-entry.lock'), 'ENTRY_LOCK_MISMATCH')
    _activation_matches(store, value)
    return value


def _lock_fingerprint(store, name='release-owner.lock'):
    try:
        fd = store._open(name)
    except OSError:
        raise ReleaseStateError('ENTRY_LOCK_MISMATCH' if name == 'release-entry.lock' else 'LOCK_MISMATCH') from None
    try:
        info = os.fstat(fd)
        if name == 'release-entry.lock' and store._release_entry_fd is not None:
            held = os.fstat(store._release_entry_fd)
            _check((held.st_dev, held.st_ino, held.st_uid) == (info.st_dev, info.st_ino, info.st_uid),
                   'ENTRY_LOCK_MISMATCH')
        return store._fingerprint({'uid': info.st_uid, 'device': info.st_dev, 'inode': info.st_ino})
    finally:
        os.close(fd)


def _activation_matches(store, value):
    marker = store.read(ACTIVATION)
    _check(marker is not None, 'UNKNOWN_BASELINE')
    _fields(marker, ('schema_version', 'root_fingerprint', 'lock_fingerprint', 'entry_lock_fingerprint', 'repo_fingerprint',
                     'owner_id', 'owner_revision', 'owner_evidence_sha256',
                     'initial_baseline_sha256', 'initial_trust_sha256'))
    _check(marker['schema_version'] == ACTIVATION_SCHEMA
           and type(marker['owner_revision']) is int
           and all(marker[key] == value[key] for key in (
               'root_fingerprint', 'lock_fingerprint', 'entry_lock_fingerprint', 'repo_fingerprint', 'owner_id',
               'owner_revision', 'owner_evidence_sha256'))
           and contract.hex64(marker['initial_baseline_sha256']) and contract.hex64(marker['initial_trust_sha256'])
           and value['activation_sha256'] == store._fingerprint(marker), 'ACTIVATION_MISMATCH')


def _write(store, value):
    _check(not getattr(store, '_release_owner_uncertain', False), 'UNCERTAIN')
    value = _validate(value)
    try:
        _check(value['root_fingerprint'] == store.root_fingerprint(), 'ROOT_MISMATCH')
        _check(value['lock_fingerprint'] == _lock_fingerprint(store), 'LOCK_MISMATCH')
        _check(value['entry_lock_fingerprint'] == _lock_fingerprint(store, 'release-entry.lock'), 'ENTRY_LOCK_MISMATCH')
        _activation_matches(store, value)
        store.write(NAME, value)
        _check(value['root_fingerprint'] == store.root_fingerprint(), 'ROOT_MISMATCH')
        _check(value['lock_fingerprint'] == _lock_fingerprint(store), 'LOCK_MISMATCH')
        _check(value['entry_lock_fingerprint'] == _lock_fingerprint(store, 'release-entry.lock'), 'ENTRY_LOCK_MISMATCH')
        _activation_matches(store, value)
    except BaseException:
        store._release_owner_uncertain = True
        raise


def _bootstrap_verified(store, *, trust, state, baseline, bindings, repo_fingerprint,
                        owner_evidence_sha256):
    """Private handoff from independent baseline verification; never infer freshness from absence.

    No production CLI calls this component yet. The caller must have verified the
    external history floor, root/signatures and real commit/lock/schema/runtime;
    synthetic tests provide an explicit complete baseline. Missing state is not
    permission to call this after a restore or a lost prior installation.
    """
    _check(store._release_entry_fd is not None and store._release_entry_shared is False
           and store._release_entry_thread == threading.get_ident(), 'ENTRY_LOCK_REQUIRED')
    with store.release_owner_lock():
        _check(not getattr(store, '_release_owner_uncertain', False), 'UNCERTAIN')
        _check(store.read(NAME) is None and store.read(ACTIVATION) is None, 'ALREADY_INITIALIZED')
        marker = {'schema_version': ACTIVATION_SCHEMA, 'root_fingerprint': store.root_fingerprint(),
                  'lock_fingerprint': _lock_fingerprint(store), 'repo_fingerprint': repo_fingerprint,
                  'entry_lock_fingerprint': _lock_fingerprint(store, 'release-entry.lock'),
                  'owner_id': OWNER_ID, 'owner_revision': OWNER_REVISION,
                  'owner_evidence_sha256': owner_evidence_sha256,
                  'initial_baseline_sha256': store._fingerprint(baseline),
                  'initial_trust_sha256': contract.hash_release_trust(trust)}
        value = {'schema_version': SCHEMA, 'root_fingerprint': store.root_fingerprint(),
                 'lock_fingerprint': _lock_fingerprint(store),
                 'entry_lock_fingerprint': _lock_fingerprint(store, 'release-entry.lock'),
                 'activation_sha256': store._fingerprint(marker),
                 'repo_fingerprint': repo_fingerprint, 'owner_id': OWNER_ID,
                 'owner_revision': OWNER_REVISION, 'owner_evidence_sha256': owner_evidence_sha256,
                 'revision': 1, 'trust': trust, 'state': state, 'baseline': baseline,
                 'bindings': bindings, 'spent': [], 'outbox': []}
        value = _validate(value)
        try:
            # Publish the immutable fence first: a crash must not reopen the legacy bypass.
            store.write(ACTIVATION, marker)
        except BaseException:
            store._release_owner_uncertain = True
            raise
        _write(store, value)


def legacy_blocked(store):
    """失われたowner本体や壊れたmarkerを、未導入へ読み替えない。"""
    return store.read(NAME) is not None or store.read(ACTIVATION) is not None


def status(store):
    """基線の正本を読むだけで、初期化・復旧・grant再生をしない。"""
    _check(store.read(NAME) is not None, 'UNKNOWN_BASELINE')
    with store.release_owner_lock():
        value = _read(store)
        return {**value, 'uncertain': bool(getattr(store, '_release_owner_uncertain', False))}


@contextmanager
def _verified_snapshot(store, verifier):
    # Crypto may own a child; do not hold the short state lock while it waits.
    with store.release_owner_lock():
        _check(not getattr(store, '_release_owner_uncertain', False), 'UNCERTAIN')
        snapshot = _read(store)
    verified = verifier(snapshot)
    with store.release_owner_lock():
        _check(not getattr(store, '_release_owner_uncertain', False), 'UNCERTAIN')
        value = _read(store)
        _check(value == snapshot, 'STATE_CHANGED')
        yield value, verified


def observe(store, envelope, *, now_ms):
    """署名済みAttestationのbinding/Hを、後続preflightの成否と独立して保存する。"""
    return _observe_attestation(store, envelope, now_ms=now_ms)[1]


def _observe_attestation(store, envelope, *, now_ms):
    """Return the exact authenticated payload to its native caller without re-verifying crypto."""
    from sdc_support_signatures import verify_release_attestation
    def verify(value):
        expected = {key: value['state'][key] for key in contract.EXPECTED_FIELDS}
        return verify_release_attestation(envelope, trust=value['trust'], expected=expected, now_ms=now_ms)
    with _verified_snapshot(store, verify) as (value, attestation):
        state = value['state']
        seq = attestation['core']['code_seq']
        existing = next((binding for binding in value['bindings'] if binding['code_seq'] == seq), None)
        observed = contract.observe_release(state, attestation, trust=value['trust'], existing_binding=existing, now_ms=now_ms)
        if observed['binding_new'] or observed['highest_seen_raised']:
            value['state'] = observed['state']
            if observed['binding_new']:
                value['bindings'].append(observed['binding'])
                value['bindings'].sort(key=lambda binding: binding['code_seq'])
            value['revision'] += 1
            _write(store, value)
        return attestation, observed


def update_trust(store, proofs, *, now_ms):
    """旧・新thresholdを検証したchainだけを採用し、realm/floor/bindingを保持する。"""
    from sdc_support_signatures import verify_release_root_chain
    def verify(value):
        expected = {key: value['state'][key] for key in contract.EXPECTED_FIELDS}
        return verify_release_root_chain(proofs, trusted=value['trust'], expected=expected, now_ms=now_ms)
    with _verified_snapshot(store, verify) as (value, trust):
        if trust != value['trust']:
            value['trust'] = trust
            value['state'].update(trust_digest=contract.hash_release_trust(trust),
                                  root_version=trust['root_version'], metadata_epoch=trust['metadata_epoch'])
            value['revision'] += 1
            _write(store, value)
        return trust


def _consume_and_start(store, *, operation, grant, admission, grant_envelope_sha256,
                       witness_state, preconditions, start_receipt_id, started_at):
    """Owner内部だけの耐久境界。生存capabilityやcrypto/両lock検証の代わりではない。

    The caller consumes its RAM continuation before entering and must not enter
    the effect core unless this publish returns normally and all fresh binding /
    deadline checks still pass. Saved records never create a continuation.
    """
    record = _spent({'operation': operation, 'grant': grant, 'admission': admission,
                     'grant_envelope_sha256': grant_envelope_sha256, 'witness_state': witness_state,
                     'preconditions': preconditions, 'start_receipt_id': start_receipt_id, 'started_at': started_at})
    with store.release_owner_lock():
        _check(not getattr(store, '_release_owner_uncertain', False), 'UNCERTAIN')
        value = _read(store)
        _check(value['state'] == record['witness_state'] and value['state']['unresolved'] is None, 'STATE_CHANGED')
        _check(all(record['preconditions'][field] == value['baseline'][baseline_field]
                   for field, baseline_field in (('current_commit', 'commit'),
                                                  ('installed_lock_sha256', 'lock_sha256'),
                                                  ('installed_schema', 'schema'))), 'BASELINE_MISMATCH')
        _check(record['operation']['target_code_seq'] > value['state']['installed']['code_seq'], 'ALREADY_INSTALLED')
        _check(all(record['operation']['operation_id'] != old['operation']['operation_id']
                   and record['grant']['grant_id'] != old['grant']['grant_id']
                   and record['grant_envelope_sha256'] != old['grant_envelope_sha256'] for old in value['spent']), 'SPENT')
        value['spent'].append(record)
        value['state']['unresolved'] = {'operation_id': record['operation']['operation_id'],
                                        'plan_sha256': record['operation']['plan_hash'], 'phase': 'unknown'}
        value['revision'] += 1
        _write(store, value)
        return _copy(record)


def _outbox(entries, spent):
    _check(type(entries) is list)
    owners = {row['operation']['operation_id']: row for row in spent}
    ids, latest, facts = set(), {}, {}
    for sequence, entry in enumerate(entries, 1):
        _fields(entry, ('raw', 'sha256', 'acknowledged'))
        _check(type(entry['raw']) is str and type(entry['acknowledged']) is bool
               and contract.hex64(entry['sha256']))
        raw = entry['raw']
        _check(hashlib.sha256(raw.encode('ascii')).hexdigest() == entry['sha256'])
        report = json.loads(raw)
        _check(type(report) is dict)
        _check(raw == apply_contract.canonical(report, max_bytes=16384))
        owner = owners.get(report.get('operation_id'))
        _check(owner is not None, 'SPENT_MISSING')
        report = apply_contract.validate_apply_outcome(report, operation=owner['operation'],
            grant=owner['grant'], admission=owner['admission'], now_ms=report['observed_at'])
        _check(report['sequence'] == sequence and report['report_id'] not in ids
               and report['observed_at'] >= owner['started_at'] and report['state'] != 'succeeded')
        ids.add(report['report_id'])
        operation_id = report['operation_id']
        previous = latest.get(operation_id)
        if previous is not None:
            _check(report['observed_at'] >= previous['observed_at']
                   and report['state'] in apply_contract.NEXT[previous['state']], 'REPORT_REGRESSION')
        result = report['result']
        if result is not None:
            _check(result['start_receipt_id'] == owner['start_receipt_id']
                   and result['started_at'] == owner['started_at']
                   and result['verified_at'] is None and result['readiness'] == 'unknown')
            saved_facts = facts.setdefault(operation_id, {})
            for key in apply_contract.FACTS:
                old, new = saved_facts.get(key), result[key]
                _check(old is None or old == new, 'FACT_REGRESSION')
                if new is not None:
                    saved_facts[key] = new
        else:
            _check(not facts.get(operation_id), 'FACT_REGRESSION')
        latest[operation_id] = report
    return entries


def _queue_report(store, report, *, now_ms):
    """固定した報告bytesだけを保存する。実行capabilityやJ解除を発行しない。"""
    report = apply_contract.validate_apply_control('report', report, now_ms=now_ms)
    raw = apply_contract.canonical(report, max_bytes=16384)
    entry = {'raw': raw, 'sha256': hashlib.sha256(raw.encode('ascii')).hexdigest(), 'acknowledged': False}
    with store.release_owner_lock():
        _check(not getattr(store, '_release_owner_uncertain', False), 'UNCERTAIN')
        value = _read(store)
        for old in value['outbox']:
            prior = json.loads(old['raw'])
            if prior['report_id'] == report['report_id']:
                _check(old['raw'] == raw, 'REPORT_CONFLICT')
                return _copy(old)
        if report['result'] is not None and report['result']['installed_release_id'] is not None:
            _check(value['state']['installed'] == {
                'release_id': report['result']['installed_release_id'],
                'code_seq': report['result']['installed_code_seq']}, 'INSTALLATION_NOT_OBSERVED')
        value['outbox'].append(entry)
        value['revision'] += 1
        _write(store, value)
        return _copy(entry)


def pending_reports(store):
    """再送は保存済みbytesの読取りのみで、grant・基線・未解決Jを変更しない。"""
    return [entry for entry in status(store)['outbox'] if not entry['acknowledged']]


def _acknowledge_report(store, *, report_id, outcome_sha256):
    """Called only after transport validates the central ACK; no execution state is adopted."""
    _check(contract.uuid(report_id) and contract.hex64(outcome_sha256), 'REPORT_ACK_INVALID')
    with store.release_owner_lock():
        _check(not getattr(store, '_release_owner_uncertain', False), 'UNCERTAIN')
        value = _read(store)
        for entry in value['outbox']:
            if json.loads(entry['raw'])['report_id'] != report_id:
                continue
            _check(entry['sha256'] == outcome_sha256, 'REPORT_ACK_CONFLICT')
            if not entry['acknowledged']:
                entry['acknowledged'] = True
                value['revision'] += 1
                _write(store, value)
            return _copy(entry)
        raise ReleaseStateError('REPORT_ACK_UNKNOWN')


def _observe_installed(store, envelope, *, observed, now_ms):
    """Owner samples actual commit/lock/schema/runtime under both locks after all effects retire.

    The private caller supplies observations from current trusted code, never a
    report/HTTP body. This component only records matching authenticated facts;
    it creates no live continuation, readiness proof or permission to clear J.
    """
    from sdc_support_signatures import verify_release_attestation
    _fields(observed, ('commit', 'lock_sha256', 'schema', 'profile_id', 'profile_sha256', 'runtime_fingerprint'))
    def verify(value):
        _check(value['spent'] and value['state']['unresolved'] is not None, 'SPENT_MISSING')
        expected = {key: value['state'][key] for key in contract.EXPECTED_FIELDS}
        return verify_release_attestation(envelope, trust=value['trust'], expected=expected, now_ms=now_ms)
    with _verified_snapshot(store, verify) as (value, attestation):
        core = attestation['core']
        operation = value['spent'][-1]['operation']
        _check(attestation['release_id'] == operation['release_id']
               and core['code_seq'] == operation['target_code_seq']
               and contract.hash_release_manifest(attestation) == operation['manifest_digest'], 'RELEASE_MISMATCH')
        _check(observed['commit'] == core['commit'] and observed['lock_sha256'] == core['lock_sha256']
               and type(observed['schema']) is int and observed['schema'] == core['schema']['target']
               and all(observed[key] == value['baseline'][key] for key in (
                   'profile_id', 'profile_sha256', 'runtime_fingerprint')), 'INSTALLATION_NOT_OBSERVED')
        point = {'release_id': attestation['release_id'], 'code_seq': core['code_seq']}
        _check(point['code_seq'] >= value['state']['installed']['code_seq'], 'INSTALLATION_REGRESSION')
        _check(any(binding['code_seq'] == point['code_seq'] and binding['release_id'] == point['release_id']
                   for binding in value['bindings']), 'BINDING_MISSING')
        if value['state']['installed'] == point:
            _check(all(value['baseline'][key] == observed[key] for key in ('commit', 'lock_sha256', 'schema')),
                   'BASELINE_MISMATCH')
        else:
            value['state']['installed'] = point
            if point['code_seq'] > value['state']['highest_installed']['code_seq']:
                value['state']['highest_installed'] = point
            value['baseline'].update(point, **observed)
            value['revision'] += 1
            _write(store, value)
        return _copy(point)
