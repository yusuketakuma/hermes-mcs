"""mcs.release-owner.v3 のload/CAS/intent/outbox規約を合成Storeで検証する。"""
import hashlib
import json
from pathlib import Path

import pytest

from adapters.sdc import release_auto_state as state
from adapters.sdc.storage import Store

H = 'a' * 64
U = '12345678-1234-4234-8234-123456789abc'
COMMIT = 'b' * 40
EXAMPLES = Path(__file__).resolve().parents[3] / 'adapters/sdc/contracts/mcs-auto/v3/examples'


def _example(name):
    return json.loads((EXAMPLES / name).read_text())['data']


def release_ref(seq=1):
    return {'release_id': H, 'code_seq': seq, 'source_commit': COMMIT,
            'artifact_sha256': 'c' * 64, 'artifact_size': 1024}


def owner_value(store, **over):
    value = {
        'schema_version': 'mcs.release-owner.v3',
        'scope': {'product': 'hermes-mcs', 'tenant': 'tenant-a', 'installation': 'inst-a',
                  'device_id': U, 'realm_epoch': 'realm-1', 'environment': 'synthetic', 'stream_id': 'st'},
        'root_fingerprint': store.root_fingerprint(),
        'repository_fingerprint': 'd' * 64, 'activation_sha256': 'e' * 64,
        'control_closure_sha256': 'f' * 64,
        'revision': 1, 'execution_revision': 1,
        'trust_json': '{}', 'legacy_evidence': [], 'operation_evidence': [],
        'lock_fingerprints': {k: '0' * 64 for k in 'GUQR CAMO'.replace(' ', '')},
        'installed_source_manifest_sha256': '1' * 64, 'verified_source_manifest_sha256': '2' * 64,
        'profile': {'profile_id': 'auto-mini', 'profile_sha256': '3' * 64, 'runtime_mode': 'standalone',
                    'os': 'macos', 'cpu': 'arm64', 'python': [3, 14], 'control_closure_sha256': '4' * 64},
        'consent': None,
        'highest_seen': release_ref(1), 'installed': release_ref(1),
        'highest_installed': release_ref(1), 'verified_installed': release_ref(1),
        'runtime_generation': 1, 'bindings': [{'code_seq': 1, 'release_id': H}],
        'active': None, 'spent': [], 'outcomes': [], 'outbox': [], 'prepare_budget': [],
    }
    value.update(over)
    return value


def outcome_entry(report_id=None, operation_id=None):
    """positive fixtureのOutcomeをraw bytes化し、sha256を正しく計算したOutboxEntryを返す。"""
    data = _example('outcome-verified.json')
    if report_id is not None:
        data['report_id'] = report_id
    if operation_id is not None:
        data['operation_id'] = operation_id
    raw = json.dumps(data, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    return {'report_id': data['report_id'], 'operation_id': data['operation_id'],
            'raw': raw, 'sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
            'acknowledged': False}


def terminal_record(operation_id, attempt_key, outcome_sha256=None, state_name='VERIFIED'):
    return {'schema_version': 'mcs.terminal-record.v3', 'operation_id': operation_id,
            'state': state_name, 'attempt_key': attempt_key,
            'outcome_sha256': outcome_sha256 or '8' * 64,
            'proof_sha256': '9' * 64, 'terminal_at': 2000}


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / 'private-auto', create=True) as value:
        yield value


def seed(store, **over):
    store.write(state.OWNER_NAME, owner_value(store, **over))


def test_missing_owner_is_not_initialized(store):
    with pytest.raises(state.OwnerStateError, match='UNKNOWN_BASELINE'):
        state.load(store)


def test_load_rejects_v1_schema_and_root_mismatch(store):
    seed(store, schema_version='mcs.release-owner.v1')
    with pytest.raises(state.OwnerStateError):
        state.load(store)
    seed(store, root_fingerprint='9' * 64)
    with pytest.raises(state.OwnerStateError, match='ROOT_MISMATCH'):
        state.load(store)


def test_revision_cas_and_execution_revision_classification(store):
    seed(store)
    ack_only = owner_value(store, revision=2)
    state.save(store, ack_only)
    assert state.load(store)['revision'] == 2
    # revision後退・execution据置き違反・安全項目のexecution未前進を拒否
    with pytest.raises(state.OwnerStateError, match='REVISION_REGRESSION'):
        state.save(store, owner_value(store, revision=1))
    unsafe = owner_value(store, revision=3, runtime_generation=2)
    with pytest.raises(state.OwnerStateError, match='EXECUTION_REVISION_REQUIRED'):
        state.save(store, unsafe)
    with pytest.raises(state.OwnerStateError, match='EXECUTION_REVISION_FROZEN'):
        state.save(store, owner_value(store, revision=3, execution_revision=2))
    ok = owner_value(store, revision=3, execution_revision=2, runtime_generation=2)
    state.save(store, ok)
    assert state.load(store)['runtime_generation'] == 2


def test_immutable_identity_fields_rejected(store):
    """R11: scope/repository/lock/manifest等のidentity項目はrevisionに関係なく拒否。"""
    seed(store)
    scope = owner_value(store)['scope']
    scope['tenant'] = 'tenant-b'
    with pytest.raises(state.OwnerStateError, match='IMMUTABLE_FIELD'):
        state.save(store, owner_value(store, revision=2, execution_revision=2, scope=scope))
    with pytest.raises(state.OwnerStateError, match='IMMUTABLE_FIELD'):
        state.save(store, owner_value(store, revision=2, execution_revision=2,
                                      repository_fingerprint='0' * 64))
    with pytest.raises(state.OwnerStateError, match='IMMUTABLE_FIELD'):
        state.save(store, owner_value(store, revision=2, execution_revision=2,
                                      verified_source_manifest_sha256='0' * 64))


def spent_record(**over):
    record = {'operation_id': U, 'grant_id': '42345678-1234-4234-8234-123456789abc',
              'attempt_key': '5' * 64, 'start_receipt_id': '52345678-1234-4234-8234-123456789abc',
              'started_at': 1000, 'grant_raw': '{"schema_version":"mcs.start-grant.v3"}',
              'grant_sha256': '6' * 64,
              'public_preflight': _example('prepared-public.json'),
              'manifest_evidence_sha256': '8' * 64, 'consent_evidence_sha256': '9' * 64}
    record.update(over)
    return record


def test_history_regression_rejected(store):
    """R11: spent/bindings/high-water/outboxの削除・後退を拒否。"""
    seed(store, spent=[spent_record()],
         highest_seen=release_ref(9), installed=release_ref(9),
         highest_installed=release_ref(9), verified_installed=release_ref(9))
    entry = outcome_entry()
    state.append_outbox(store, entry)
    base = state.load(store)

    def attempt(**over):
        state.save(store, dict(base, revision=base['revision'] + 1,
                               execution_revision=base['execution_revision'] + 1, **over))

    # spent削除を安全項目変更(execution前進)付きでも拒否
    with pytest.raises(state.OwnerStateError, match='SPENT_REGRESSION'):
        attempt(spent=[])
    # bindings削除を拒否
    with pytest.raises(state.OwnerStateError, match='BINDINGS_REGRESSION'):
        attempt(bindings=[])
    # high-water後退を拒否
    with pytest.raises(state.OwnerStateError, match='HIGH_WATER_REGRESSION'):
        attempt(highest_seen=release_ref(1))
    # outbox削除を拒否
    with pytest.raises(state.OwnerStateError, match='OUTBOX_REGRESSION'):
        attempt(outbox=[])


def test_outbox_append_and_ack_revision_only(store):
    seed(store)
    entry = outcome_entry()
    state.append_outbox(store, entry)
    value = state.load(store)
    assert value['revision'] == 2 and value['execution_revision'] == 2
    state.ack_outbox(store, report_id=entry['report_id'], outcome_sha256=entry['sha256'])
    value = state.load(store)
    assert value['outbox'][0]['acknowledged'] is True
    assert value['revision'] == 3 and value['execution_revision'] == 2
    # 既ACK entryへの別hash ACKも拒否(R14)
    with pytest.raises(state.OwnerStateError, match='OUTBOX_CONFLICT'):
        state.ack_outbox(store, report_id=entry['report_id'], outcome_sha256='5' * 64)
    # 別outcomeの追記と未知report拒否
    other = outcome_entry(report_id='62345678-1234-4234-8234-123456789abc',
                          operation_id='72345678-1234-4234-8234-123456789abc')
    state.append_outbox(store, other)
    with pytest.raises(state.OwnerStateError, match='OUTBOX_NOT_FOUND'):
        state.ack_outbox(store, report_id='82345678-1234-4234-8234-123456789abc',
                         outcome_sha256=entry['sha256'])


def test_append_outbox_rejects_bad_raw_and_hash(store):
    """R14: rawがOutcome契約でない・ID不一致・hash不一致を拒否。"""
    seed(store)
    entry = outcome_entry()
    bad = dict(entry, raw='{"schema_version":"mcs.outcome.v3"}')
    with pytest.raises(state.OwnerStateError):
        state.append_outbox(store, bad)
    mismatched = dict(entry, operation_id='32345678-1234-4234-8234-123456789abc')
    with pytest.raises(state.OwnerStateError, match='OUTBOX_MISMATCH'):
        state.append_outbox(store, mismatched)
    wrong_hash = dict(entry, sha256='0' * 64)
    with pytest.raises(state.OwnerStateError, match='OUTBOX_HASH'):
        state.append_outbox(store, wrong_hash)
    state.append_outbox(store, entry)
    with pytest.raises(state.OwnerStateError, match='OUTBOX_DUPLICATE'):
        state.append_outbox(store, entry)


def test_spent_dedup_and_intent_lifecycle(store):
    seed(store)
    record = spent_record()
    state.record_spent(store, record)
    with pytest.raises(state.OwnerStateError, match='SPENT_DUPLICATE'):
        state.record_spent(store, record)
    intent = {'schema_version': 'mcs.start-intent.v3', 'operation_id': U,
              'grant_id': '42345678-1234-4234-8234-123456789abc', 'grant_sha256': H,
              'attempt_key': H, 'prepare_nonce': '6' * 64, 'root_fingerprint': store.root_fingerprint(),
              'source_before': COMMIT, 'source_target': '7' * 40, 'created_at': 1000,
              'phase': 'open', 'terminal_outcome_sha256': None}
    state.open_intent(store, intent)
    assert state.load_intent(store)['phase'] == 'open'
    with pytest.raises(state.OwnerStateError, match='INTENT_EXISTS'):
        state.open_intent(store, intent)


def test_retire_intent_requires_durable_terminal_evidence(store):
    """R12: owner.outcomesへ耐久化された一致TerminalRecordなしではretireしない。"""
    seed(store)
    intent = {'schema_version': 'mcs.start-intent.v3', 'operation_id': U,
              'grant_id': '42345678-1234-4234-8234-123456789abc', 'grant_sha256': H,
              'attempt_key': H, 'prepare_nonce': '6' * 64, 'root_fingerprint': store.root_fingerprint(),
              'source_before': COMMIT, 'source_target': '7' * 40, 'created_at': 1000,
              'phase': 'open', 'terminal_outcome_sha256': None}
    state.open_intent(store, intent)
    # 証拠未保存ではfence保持
    with pytest.raises(state.OwnerStateError, match='INTENT_EVIDENCE'):
        state.retire_intent(store, operation_id=U, terminal_record=terminal_record(U, H))
    # 別operationの記録では拒否
    state.record_terminal(store, terminal_record('92345678-1234-4234-8234-123456789abc', H))
    with pytest.raises(state.OwnerStateError, match='INTENT_EVIDENCE'):
        state.retire_intent(store, operation_id=U, terminal_record=terminal_record(U, H))
    # 対象operationの記録を耐久化
    state.record_terminal(store, terminal_record(U, H))
    # 改竄した引数(proof/attempt_key違い)ではfenceを保持したまま拒否
    forged = terminal_record(U, H)
    forged['proof_sha256'] = '0' * 64
    with pytest.raises(state.OwnerStateError, match='INTENT_EVIDENCE'):
        state.retire_intent(store, operation_id=U, terminal_record=forged)
    with pytest.raises(state.OwnerStateError, match='INTENT_EVIDENCE'):
        state.retire_intent(store, operation_id=U, terminal_record=terminal_record(U, 'c' * 64))
    # 完全一致の記録でのみ退役
    retired = state.retire_intent(store, operation_id=U, terminal_record=terminal_record(U, H))
    assert retired['phase'] == 'retired'
    assert retired['terminal_outcome_sha256'] == '8' * 64
    # 重複terminal記録と二重retireを拒否
    with pytest.raises(state.OwnerStateError, match='TERMINAL_DUPLICATE'):
        state.record_terminal(store, terminal_record(U, H))
    with pytest.raises(state.OwnerStateError, match='INTENT_PHASE'):
        state.retire_intent(store, operation_id=U, terminal_record=terminal_record(U, H))


def migration_intent(store, **over):
    value = {
        'schema_version': 'mcs.release-owner-migration.v3',
        'migration_id': '82345678-1234-4234-8234-123456789abc',
        'root_fingerprint': store.root_fingerprint(),
        'repository_fingerprint': 'd' * 64,
        'source_schema': 'mcs.release-owner.v1',
        'target_schema': 'mcs.release-owner.v3',
        'bridge_sha256': '9' * 64, 'control_closure_sha256': 'f' * 64,
        'recovery_generation_sha256': 'ab' * 32,
        'external_floor_sha256': 'ac' * 32,
        'files': [{'logical_id': 'release-owner.json', 'before_sha256': 'b' * 64,
                   'size': 4096, 'disposition': 'archive_exact'}],
        'human_receipt_id': '92345678-1234-4234-8234-123456789abc',
        'created_at': 1000, 'phase': 'prepared',
    }
    value.update(over)
    return value


def test_migration_intent_monotonic_phases(store):
    from adapters.sdc import release_auto_migration as migration
    assert migration.load(store) is None
    migration.prepare(store, migration_intent(store))
    # 別idの競合prepareも既存intentを上書きしない(R13)
    with pytest.raises(migration.MigrationError, match='INTENT_EXISTS'):
        migration.prepare(store, migration_intent(store,
                                                migration_id='a2345678-1234-4234-8234-123456789abc'))
    with pytest.raises(migration.MigrationError, match='PHASE_REGRESSION'):
        migration.advance(store, migration_id='82345678-1234-4234-8234-123456789abc', phase='prepared')
    with pytest.raises(migration.MigrationError, match='INTENT_MISMATCH'):
        migration.advance(store, migration_id='a2345678-1234-4234-8234-123456789abc', phase='old_archived')
    migration.advance(store, migration_id='82345678-1234-4234-8234-123456789abc', phase='old_archived')
    with pytest.raises(migration.MigrationError, match='PHASE_REGRESSION'):
        migration.advance(store, migration_id='82345678-1234-4234-8234-123456789abc', phase='verified')
    migration.advance(store, migration_id='82345678-1234-4234-8234-123456789abc', phase='owner_replaced')
    migration.advance(store, migration_id='82345678-1234-4234-8234-123456789abc', phase='verified')
    assert migration.load(store)['phase'] == 'verified'
    migration.require_v3_ready(store)


def test_migration_pending_blocks_v3(store):
    from adapters.sdc import release_auto_migration as migration
    migration.prepare(store, migration_intent(store))
    with pytest.raises(migration.MigrationError, match='MIGRATION_PENDING'):
        migration.require_v3_ready(store)
    with pytest.raises(migration.MigrationError, match='ROOT_MISMATCH'):
        migration.prepare(store, migration_intent(store, root_fingerprint='9' * 64))
