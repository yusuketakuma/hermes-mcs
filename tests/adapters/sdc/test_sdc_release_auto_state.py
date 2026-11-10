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
        state.retire_intent(store, operation_id=U,
                            terminal_record=terminal_record(U, H, state_name='DEFERRED'))
    # 別operationの記録では拒否
    state.record_terminal(store, terminal_record('92345678-1234-4234-8234-123456789abc', H,
                                                 state_name='DEFERRED'))
    with pytest.raises(state.OwnerStateError, match='INTENT_EVIDENCE'):
        state.retire_intent(store, operation_id=U,
                            terminal_record=terminal_record(U, H, state_name='DEFERRED'))
    # 対象operationの記録を耐久化
    state.record_terminal(store, terminal_record(U, H, state_name='DEFERRED'))
    # 改竄した引数(proof/attempt_key違い)ではfenceを保持したまま拒否
    forged = terminal_record(U, H, state_name='DEFERRED')
    forged['proof_sha256'] = '0' * 64
    with pytest.raises(state.OwnerStateError, match='INTENT_EVIDENCE'):
        state.retire_intent(store, operation_id=U, terminal_record=forged)
    with pytest.raises(state.OwnerStateError, match='INTENT_EVIDENCE'):
        state.retire_intent(store, operation_id=U,
                            terminal_record=terminal_record(U, 'c' * 64, state_name='DEFERRED'))
    # 完全一致の記録でのみ退役
    retired = state.retire_intent(store, operation_id=U,
                                  terminal_record=terminal_record(U, H, state_name='DEFERRED'))
    assert retired['phase'] == 'retired'
    assert retired['terminal_outcome_sha256'] == '8' * 64
    # 重複terminal記録と二重retireを拒否
    with pytest.raises(state.OwnerStateError, match='TERMINAL_DUPLICATE'):
        state.record_terminal(store, terminal_record(U, H, state_name='DEFERRED'))
    with pytest.raises(state.OwnerStateError, match='INTENT_PHASE'):
        state.retire_intent(store, operation_id=U,
                            terminal_record=terminal_record(U, H, state_name='DEFERRED'))


def test_verified_terminal_requires_finalize_path(store):
    """R22: VERIFIEDはfinalize_verifiedのatomic確定経路のみ。record_terminal/save()での
    単独追記は原子的確定を伴わないため拒否する。"""
    seed(store)
    with pytest.raises(state.OwnerStateError, match='INVALID'):
        state.record_terminal(store, terminal_record(U, H))
    base = state.load(store)
    with pytest.raises(state.OwnerStateError, match='VERIFIED_REQUIRES_FINALIZE'):
        state.save(store, dict(base, revision=2, execution_revision=2,
                               outcomes=[terminal_record(U, H)]))


def active_operation(**over):
    op = {'operation_id': 'a2345678-1234-4234-8234-123456789abc',
          'phase': 'APPLIED', 'target': release_ref(2),
          'attempt_key': 'b' * 64, 'prepare_nonce': None, 'prepare_deadline': None,
          'private_witness': None, 'grant_raw': None, 'start_receipt_id': None,
          'candidate_generation': None, 'startup_nonce': None,
          'activation_receipt_id': None, 'runtime_proof': None,
          'reserved_owner_bytes': 4096, 'error_code': None,
          'rollout': {'rollout_id': 'c2345678-1234-4234-8234-123456789abc',
                      'assignment_id': 'd2345678-1234-4234-8234-123456789abc',
                      'policy_sha256': 'e' * 64, 'cohort_sha256': 'f' * 64,
                      'allocation_epoch': 1, 'authorization_epoch': 1,
                      'stage': 'pilot', 'wave_index': 1}}
    op.update(over)
    return op


def finalize_fixture(store, doc_over=None, preflight_gen=None):
    """outcome-verified fixtureをactive/spent/terminalへ整合させてseedする。"""
    doc = _example('outcome-verified.json')
    if doc_over:
        doc.update(doc_over)
    raw = json.dumps(doc, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    outcome_sha = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    term = terminal_record(doc['operation_id'], doc['attempt_key'], outcome_sha256=outcome_sha)
    preflight = _example('prepared-public.json')
    if preflight_gen is not None:
        preflight['runtime_generation'] = preflight_gen
    spent = spent_record(operation_id=doc['operation_id'],
                         attempt_key=doc['attempt_key'], grant_id=doc['grant_id'])
    spent['public_preflight'] = preflight
    seed(store, scope=doc['scope'],
         active=active_operation(operation_id=doc['operation_id'],
                                 attempt_key=doc['attempt_key'],
                                 target=doc['target'], rollout=doc['rollout'],
                                 candidate_generation=doc['runtime_generation']),
         spent=[spent], verified_installed=release_ref(1))
    return doc, raw, term


def test_finalize_verified_atomic_and_replay(store):
    """R20: VERIFIED終端をoutcome/verified_installed/source baseline/active終結/outboxでatomic確定。"""
    doc, raw, term = finalize_fixture(store)
    manifest = '7' * 64
    result = state.finalize_verified(store, term, outcome_raw=raw,
                                     source_manifest_sha256=manifest,
                                     expected_execution_revision=1)
    value = result
    assert value['outcomes'] == [term]
    assert value['verified_installed'] == doc['target']
    assert value['installed'] == doc['target']
    assert value['highest_installed'] == doc['target']
    assert value['runtime_generation'] == doc['runtime_generation']
    assert {'code_seq': doc['target']['code_seq'],
            'release_id': doc['target']['release_id']} in value['bindings']
    assert value['installed_source_manifest_sha256'] == manifest
    assert value['verified_source_manifest_sha256'] == manifest
    assert value['active'] is None
    entry = value['outbox'][0]
    assert entry['raw'] == raw and entry['sha256'] == term['outcome_sha256']
    assert entry['acknowledged'] is False
    assert value['revision'] == 2 and value['execution_revision'] == 2
    # exact再送はdurable記録が整合する場合のみ現stateを返す
    again = state.finalize_verified(store, term, outcome_raw=raw,
                                    source_manifest_sha256=manifest,
                                    expected_execution_revision=99)
    assert again['revision'] == 2
    # manifest引数は再送時のreceipt条件ではない(現在baselineは進み得る)ため、
    # 異なる値でもdurable証拠が整合する限り無変更で現stateを返す
    replay_diff_manifest = state.finalize_verified(store, term, outcome_raw=raw,
                                                   source_manifest_sha256='8' * 64,
                                                   expected_execution_revision=99)
    assert replay_diff_manifest['revision'] == 2
    # 同一opの別terminalはconflict
    forged = dict(term, proof_sha256='0' * 64)
    with pytest.raises(state.OwnerStateError, match='TERMINAL_CONFLICT'):
        state.finalize_verified(store, forged, outcome_raw=raw,
                                source_manifest_sha256=manifest,
                                expected_execution_revision=2)


def test_finalize_verified_rejects_invalid(store):
    doc, raw, term = finalize_fixture(store)
    manifest = '7' * 64
    # stale execution_revision
    with pytest.raises(state.OwnerStateError, match='STALE_EXECUTION'):
        state.finalize_verified(store, term, outcome_raw=raw,
                                source_manifest_sha256=manifest,
                                expected_execution_revision=2)
    # activeのattempt_keyがterminal/outcomeと不一致
    base = state.load(store)
    store.write(state.OWNER_NAME, dict(base, active=dict(base['active'], attempt_key='c' * 64)))
    with pytest.raises(state.OwnerStateError, match='ACTIVE_MISMATCH'):
        state.finalize_verified(store, term, outcome_raw=raw,
                                source_manifest_sha256=manifest,
                                expected_execution_revision=1)
    # spent欠落(activeは正常値へ戻してからraw writeで欠落stateを作る)
    store.write(state.OWNER_NAME, dict(base, spent=[]))
    with pytest.raises(state.OwnerStateError, match='SPENT_MISSING'):
        state.finalize_verified(store, term, outcome_raw=raw,
                                source_manifest_sha256=manifest,
                                expected_execution_revision=1)


def test_finalize_verified_outcome_binding(store):
    doc, raw, term = finalize_fixture(store)
    # outcome hash不一致
    bad_term = terminal_record(doc['operation_id'], doc['attempt_key'],
                               outcome_sha256='0' * 64)
    with pytest.raises(state.OwnerStateError, match='OUTCOME_HASH'):
        state.finalize_verified(store, bad_term, outcome_raw=raw,
                                source_manifest_sha256='7' * 64,
                                expected_execution_revision=1)
    # outcome targetがactive.targetと不一致(installed/verifiedも整合させて契約は通す)
    other = release_ref(3)
    tampered = dict(doc, target=other, installed=other, verified_installed=other)
    tampered_raw = json.dumps(tampered, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    tampered_sha = hashlib.sha256(tampered_raw.encode()).hexdigest()
    with pytest.raises(state.OwnerStateError, match='OUTCOME_MISMATCH'):
        state.finalize_verified(store, terminal_record(doc['operation_id'],
                                                       doc['attempt_key'],
                                                       outcome_sha256=tampered_sha),
                                outcome_raw=tampered_raw,
                                source_manifest_sha256='7' * 64,
                                expected_execution_revision=1)
    # 非VERIFIED terminalは拒否
    with pytest.raises(state.OwnerStateError, match='INVALID'):
        state.finalize_verified(store, terminal_record(doc['operation_id'],
                                                       doc['attempt_key'],
                                                       outcome_sha256=term['outcome_sha256'],
                                                       state_name='APPLIED_UNVERIFIED'),
                                outcome_raw=raw, source_manifest_sha256='7' * 64,
                                expected_execution_revision=1)


def runtime_proof(operation_id, target, generation, **over):
    """RuntimeProof v3 fixture(services最小1件)。"""
    proof = {'schema_version': 'mcs.runtime-proof.v3',
             'proof_id': '92345678-1234-4234-8234-123456789abc',
             'operation_id': operation_id, 'release': target,
             'profile_sha256': 'a' * 64, 'control_closure_sha256': 'b' * 64,
             'runtime_generation': generation, 'boot_id': 'c' * 64,
             'startup_nonce': 'd' * 64,
             'services': [{'service_id': 'svc-a', 'desired_kind': 'resident',
                           'proof_id': 'a2345678-1234-4234-8234-123456789abc',
                           'process_instance_id': None, 'pid': None,
                           'birth_witness': None, 'loaded_source_sha256': None,
                           'state': 'running', 'observed_at': 100,
                           'previous_heartbeat_at': None}],
             'activation_receipt_id': 'b2345678-1234-4234-8234-123456789abc',
             'observed_at': 100}
    proof.update(over)
    return proof


def test_finalize_verified_generation_binding(store):
    """r29: outcome.runtime_generationはactive.candidate_generation(認証済み
    target世代)への一致を要求し、未記録・旧世代・proof不整合を拒否する。"""
    doc, raw, term = finalize_fixture(store)
    manifest = '7' * 64
    # candidate_generation未記録は相関先が無いため拒否
    base = state.load(store)
    store.write(state.OWNER_NAME,
                dict(base, active=dict(base['active'], candidate_generation=None)))
    with pytest.raises(state.OwnerStateError, match='OUTCOME_MISMATCH'):
        state.finalize_verified(store, term, outcome_raw=raw,
                                source_manifest_sha256=manifest,
                                expected_execution_revision=1)
    # 旧世代(preflight世代)のoutcomeはcandidate世代と一致せず拒否
    doc2, raw2, term2 = finalize_fixture(store)
    old = dict(doc2, runtime_generation=1)
    old_raw = json.dumps(old, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    old_sha = hashlib.sha256(old_raw.encode()).hexdigest()
    with pytest.raises(state.OwnerStateError, match='OUTCOME_MISMATCH'):
        state.finalize_verified(store, terminal_record(doc2['operation_id'],
                                                       doc2['attempt_key'],
                                                       outcome_sha256=old_sha),
                                outcome_raw=old_raw, source_manifest_sha256=manifest,
                                expected_execution_revision=1)
    # r38: preflight/currentより大きいがcandidateと異なる世代も拒否する
    # (等値束縛の反証。旧>preflight条件では通ってしまうケース)
    doc2b = finalize_fixture(store)[0]
    future = dict(doc2b, runtime_generation=doc2b['runtime_generation'] + 1)
    future_raw = json.dumps(future, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    future_sha = hashlib.sha256(future_raw.encode()).hexdigest()
    base = state.load(store)
    store.write(state.OWNER_NAME, dict(base, active=dict(
        base['active'], candidate_generation=doc2b['runtime_generation'])))
    with pytest.raises(state.OwnerStateError, match='OUTCOME_MISMATCH'):
        state.finalize_verified(store, terminal_record(doc2b['operation_id'],
                                                       doc2b['attempt_key'],
                                                       outcome_sha256=future_sha),
                                outcome_raw=future_raw, source_manifest_sha256=manifest,
                                expected_execution_revision=1)
    # runtime_proofがある場合はoperation/release/世代の一致を追加要求
    doc3, raw3, term3 = finalize_fixture(store)
    base = state.load(store)
    good = runtime_proof(doc3['operation_id'], doc3['target'], doc3['runtime_generation'])
    store.write(state.OWNER_NAME,
                dict(base, active=dict(base['active'], runtime_proof=good)))
    result = state.finalize_verified(store, term3, outcome_raw=raw3,
                                     source_manifest_sha256=manifest,
                                     expected_execution_revision=1)
    assert result['runtime_generation'] == doc3['runtime_generation']
    # proof世代不一致は拒否
    doc4, raw4, term4 = finalize_fixture(store)
    base = state.load(store)
    bad = runtime_proof(doc4['operation_id'], doc4['target'],
                        doc4['runtime_generation'] + 1)
    store.write(state.OWNER_NAME,
                dict(base, active=dict(base['active'], runtime_proof=bad)))
    with pytest.raises(state.OwnerStateError, match='RUNTIME_PROOF_MISMATCH'):
        state.finalize_verified(store, term4, outcome_raw=raw4,
                                source_manifest_sha256=manifest,
                                expected_execution_revision=1)


def test_spent_grant_id_unique(store):
    """SEM29/r30: grant_idはspent全体で一意。同grant再利用を両入口で拒否。"""
    grant = '62345678-1234-4234-8234-123456789abc'
    seed(store, spent=[spent_record(operation_id=U, attempt_key='a' * 64,
                                    grant_id=grant)])
    dup = spent_record(operation_id='72345678-1234-4234-8234-123456789abc',
                       attempt_key='b' * 64, grant_id=grant)
    with pytest.raises(state.OwnerStateError, match='GRANT_REUSE'):
        state.record_spent(store, dup)
    base = state.load(store)
    with pytest.raises(state.OwnerStateError, match='SPENT_REGRESSION'):
        state.save(store, dict(base, revision=2, execution_revision=2,
                               spent=base['spent'] + [dup]))


def test_finalize_verified_ack_loss_allows_next_update(store):
    """R20/R25: outbox未ACKのまま異なるtargetへの次更新(A→B→C)を完遂できる。"""
    doc_b, raw_b, term_b = finalize_fixture(store)
    state.finalize_verified(store, term_b, outcome_raw=raw_b,
                            source_manifest_sha256='7' * 64,
                            expected_execution_revision=1)
    # CはBと異なるrelease_id/code_seq/commit/artifactを持つ真の後続target
    target_c = {'release_id': 'e' * 64, 'code_seq': 3, 'source_commit': 'd' * 40,
                'artifact_sha256': 'f' * 64, 'artifact_size': 8192}
    doc_c = dict(doc_b)
    doc_c.update({
        'operation_id': '62345678-1234-4234-8234-123456789abc',
        'report_id': '72345678-1234-4234-8234-123456789abc',
        'attempt_key': hashlib.sha256(b'c-attempt').hexdigest(),
        'grant_id': '82345678-1234-4234-8234-123456789abc',
        'target': target_c, 'installed': target_c, 'verified_installed': target_c,
        'runtime_generation': doc_b['runtime_generation'] + 1,
    })
    raw_c = json.dumps(doc_c, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    sha_c = hashlib.sha256(raw_c.encode()).hexdigest()
    base = state.load(store)
    preflight_c = _example('prepared-public.json')
    preflight_c['runtime_generation'] = doc_b['runtime_generation']
    state.save(store, dict(base, revision=base['revision'] + 1,
                           execution_revision=base['execution_revision'] + 1,
                           active=active_operation(operation_id=doc_c['operation_id'],
                                                   attempt_key=doc_c['attempt_key'],
                                                   target=target_c, rollout=doc_c['rollout'],
                                                   candidate_generation=doc_c['runtime_generation']),
                           spent=base['spent'] + [dict(
                               spent_record(operation_id=doc_c['operation_id'],
                                            attempt_key=doc_c['attempt_key'],
                                            grant_id=doc_c['grant_id']),
                               public_preflight=preflight_c)]))
    state.finalize_verified(store, terminal_record(doc_c['operation_id'],
                                                   doc_c['attempt_key'],
                                                   outcome_sha256=sha_c),
                            outcome_raw=raw_c, source_manifest_sha256='9' * 64,
                            expected_execution_revision=3)
    value = state.load(store)
    # BをbaseにCが採用され、installed/verified/baselineが全てCを指す
    assert value['verified_installed'] == target_c
    assert value['installed'] == target_c
    assert value['highest_installed'] == target_c
    assert value['verified_source_manifest_sha256'] == '9' * 64
    assert value['runtime_generation'] == doc_c['runtime_generation']
    assert len(value['outcomes']) == 2 and len(value['outbox']) == 2
    assert value['outbox'][0]['acknowledged'] is False  # BのACK喪失を保持
    # Bの遅延ACK後もCのbaselineは戻らない
    state.ack_outbox(store, report_id=doc_b['report_id'],
                     outcome_sha256=term_b['outcome_sha256'])
    value = state.load(store)
    assert value['verified_installed'] == target_c
    assert value['outbox'][0]['acknowledged'] is True
    # C採用後のB exact finalize再送は過去opの耐久証拠で照合し、
    # 現在Cをreceipt条件にせず無変更で現stateを返す(r29/r37)
    before = state.load(store)
    replay_b = state.finalize_verified(store, term_b, outcome_raw=raw_b,
                                       source_manifest_sha256='7' * 64,
                                       expected_execution_revision=99)
    assert replay_b == before  # Cのstate/revision/基線/outboxが一切変わらない


def test_spent_outcome_highwater_exact_append_only(store):
    """R11残件: 同key別bytesのspent改変・outcomes削除/改変/同op別terminal・
    high-water差替えを拒否。"""
    seed(store, spent=[spent_record()],
         outcomes=[terminal_record(U, H, state_name='DEFERRED')])
    base = state.load(store)

    def attempt(**over):
        state.save(store, dict(base, revision=base['revision'] + 1,
                               execution_revision=base['execution_revision'] + 1, **over))

    # 同operation/attemptのspentを別bytesへ改変
    forged = dict(base['spent'][0], grant_sha256='0' * 64)
    with pytest.raises(state.OwnerStateError, match='SPENT_REGRESSION'):
        attempt(spent=[forged])
    # outcomes削除・改変・同op別terminal追加
    with pytest.raises(state.OwnerStateError, match='OUTCOME_REGRESSION'):
        attempt(outcomes=[])
    with pytest.raises(state.OwnerStateError, match='OUTCOME_REGRESSION'):
        attempt(outcomes=[dict(base['outcomes'][0], proof_sha256='0' * 64)])
    with pytest.raises(state.OwnerStateError, match='OUTCOME_REGRESSION'):
        attempt(outcomes=base['outcomes'] + [terminal_record(
            U, 'c' * 64, state_name='CANCELLED_NO_EFFECT')])
    # spent同op別attempt・同attempt別opの追加
    with pytest.raises(state.OwnerStateError, match='SPENT_REGRESSION'):
        attempt(spent=base['spent'] + [spent_record(
            operation_id=U, attempt_key='d' * 64,
            grant_id='72345678-1234-4234-8234-123456789abc')])
    with pytest.raises(state.OwnerStateError, match='SPENT_REGRESSION'):
        attempt(spent=base['spent'] + [spent_record(
            operation_id='a2345678-1234-4234-8234-123456789abc', attempt_key='5' * 64,
            grant_id='82345678-1234-4234-8234-123456789abc')])
    # bindings同seq別release_id・重複pair拒否、新seq追加は許可
    with pytest.raises(state.OwnerStateError, match='BINDINGS_REGRESSION'):
        attempt(bindings=[{'code_seq': 1, 'release_id': '0' * 64}])
    with pytest.raises(state.OwnerStateError, match='BINDINGS_REGRESSION'):
        attempt(bindings=base['bindings'] + [dict(base['bindings'][0])])
    # high-water同seq+idのartifact差替え
    mutated = release_ref(1)
    mutated['artifact_sha256'] = '0' * 64
    with pytest.raises(state.OwnerStateError, match='HIGH_WATER_MUTATION'):
        attempt(highest_seen=mutated)
    # bindings新seq追加は許可(最後に実施しstore状態を進める)
    state.save(store, dict(base, revision=base['revision'] + 1,
                           execution_revision=base['execution_revision'] + 1,
                           bindings=base['bindings'] + [{'code_seq': 2, 'release_id': '1' * 64}]))


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
