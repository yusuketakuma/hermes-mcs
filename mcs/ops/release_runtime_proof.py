"""v3 runtime proof / activation (M6/W10): APPLIED→CANDIDATE_READY→ACTIVATING→VERIFIED。

quarantine起動・native PID/birth・loaded source・heartbeatの観測は全てdeps注入。
HEAD/PID自己申告だけでは証明にしない(SEM22/23)。証拠に不確実性があれば
APPLIED_UNVERIFIED/UNKNOWNを保持しコードを戻さない。

deps契約(全て冪等必須・副作用はlock外):
- quarantine_launch(active, receipt_id): receipt_idで冪等起動/再装着。
  成功時のみreturn、失敗は例外。起動だけでなく観測probeを可能にする責務。
- probe_runtime(active) -> {'boot_id','loaded_source_sha256','services':{sid:obs}}
- service_desires() -> {service_id: 'resident'|'timer'|'disabled'}
- loaded_source_expect(active) -> 起動source検証済みの期待loaded sha256
- heartbeat_observations(active) -> [{'service_id','heartbeat_at'}]
- release_update_fence(generation): 自分の更新fenceを冪等解放
- measure_source_manifest() -> 認証済みtarget観測のsource manifest sha256
"""
import hashlib
import json

from mcs.ops.release_auto_owner import OwnerError, _det_uuid, _nonce, _mutate
from adapters.sdc import release_auto_contract as contract
from adapters.sdc import release_auto_state as auto_state

LOCK_SET = ('G', 'U')  # DEV40: 別更新排除のためG/Uのみ保持、Q/R/C/A/M/Oは解放
HEARTBEAT_MIN_INTERVAL_MS = 5000
DOC_BYTES = 256 * 1024

_RESIDENT_REQUIRED = ('process_instance_id', 'pid', 'birth_witness',
                      'loaded_source_sha256')


def _service_proof(raw, service_id, desired_kind, proof_id, now_ms):
    """deps観測をServiceProofへ写し、desired kindごとの必須証拠を検査する。"""
    if not isinstance(raw, dict):
        raise OwnerError('SERVICE_PROOF_MISSING')
    state_name = raw.get('state')
    proof = {'service_id': service_id, 'desired_kind': desired_kind,
             'proof_id': proof_id,
             'process_instance_id': raw.get('process_instance_id'),
             'pid': raw.get('pid'),
             'birth_witness': raw.get('birth_witness'),
             'loaded_source_sha256': raw.get('loaded_source_sha256'),
             'state': state_name, 'observed_at': now_ms,
             'previous_heartbeat_at': raw.get('previous_heartbeat_at')}
    if desired_kind == 'resident':
        if (state_name != 'running'
                or any(proof[k] is None for k in _RESIDENT_REQUIRED)
                or proof['previous_heartbeat_at'] is None):
            raise OwnerError('SERVICE_PROOF_INSUFFICIENT')
    elif desired_kind == 'timer':
        if state_name != 'armed':
            raise OwnerError('SERVICE_PROOF_INSUFFICIENT')
    elif desired_kind == 'disabled':
        if state_name != 'stopped':
            raise OwnerError('SERVICE_PROOF_INSUFFICIENT')
    else:
        raise OwnerError('SERVICE_PROOF_INSUFFICIENT')
    return contract.parse_typed(json.dumps(proof).encode(), 'ServiceProof')


def _runtime_proof(deps, owner, active, services, probe):
    public = active['private_witness']['public']
    boot_id = probe.get('boot_id')
    loaded = probe.get('loaded_source_sha256')
    if not (isinstance(boot_id, str) and len(boot_id) == 64
            and isinstance(loaded, str) and len(loaded) == 64):
        raise OwnerError('RUNTIME_PROBE_INSUFFICIENT')
    # loaded identityは起動source検証済みのdeps値のみ受理(ディスクHEAD自己申告禁止)
    if loaded != deps.loaded_source_expect(active):
        raise OwnerError('LOADED_SOURCE_MISMATCH')
    proof_id = _det_uuid('runtime-proof|' + active['operation_id'])
    proof = {'schema_version': 'mcs.runtime-proof.v3', 'proof_id': proof_id,
             'operation_id': active['operation_id'],
             'release': active['target'],
             'profile_sha256': owner['profile']['profile_sha256'],
             'control_closure_sha256': public['control_closure_sha256'],
             'runtime_generation': active['candidate_generation'],
             'boot_id': boot_id, 'startup_nonce': active['startup_nonce'],
             'services': services,
             'activation_receipt_id': active['activation_receipt_id'],
             'observed_at': deps.now_ms()}
    return contract.parse_document(json.dumps(proof).encode(), 'RuntimeProof',
                                   max_bytes=DOC_BYTES)


def _collect_service_proofs(store, deps, owner, active):
    """APPLIED: startup_nonce耐久化→quarantine launch→service proof収集。"""
    if active['startup_nonce'] is None:
        nonce = _nonce()

        def set_nonce(new):
            a = new['active']
            if (a is None or a['operation_id'] != active['operation_id']
                    or a['phase'] != 'APPLIED' or a['startup_nonce'] is not None):
                raise OwnerError('PHASE_MISMATCH')
            a['startup_nonce'] = nonce

        _mutate(store, set_nonce)
        active = auto_state.load(store)['active']
    # receipt_idはoperation決定論的: launch成功→receipt永続化前のcrashで
    # retryしてもdepsが同一receipt_idで冪等起動/再装着できる(二重起動防止)。
    receipt_id = _det_uuid('activation-receipt|' + active['operation_id'])
    deps.quarantine_launch(active, receipt_id)

    def set_receipt(new):
        a = new['active']
        if (a is None or a['operation_id'] != active['operation_id']
                or a['phase'] != 'APPLIED'):
            raise OwnerError('PHASE_MISMATCH')
        a['activation_receipt_id'] = receipt_id

    _mutate(store, set_receipt)
    active = auto_state.load(store)['active']
    desires = deps.service_desires()
    if not isinstance(desires, dict) or not desires:
        raise OwnerError('SERVICE_SET_UNKNOWN')
    probe = deps.probe_runtime(active)
    if not isinstance(probe, dict):
        raise OwnerError('RUNTIME_PROBE_MISSING')
    raw_services = probe.get('services')
    if not isinstance(raw_services, dict):
        raise OwnerError('RUNTIME_PROBE_INSUFFICIENT')
    services = []
    for service_id in sorted(desires):
        services.append(_service_proof(raw_services.get(service_id),
                                       service_id, desires[service_id],
                                       _det_uuid('service-proof|' + service_id
                                                 + '|' + active['operation_id']),
                                       deps.now_ms()))
    proof = _runtime_proof(deps, owner, active, services, probe)

    def ready(new):
        a = new['active']
        if (a is None or a['operation_id'] != active['operation_id']
                or a['phase'] != 'APPLIED'):
            raise OwnerError('PHASE_MISMATCH')
        a['phase'] = 'CANDIDATE_READY'
        a['runtime_proof'] = proof

    _mutate(store, ready)
    return {'phase': 'CANDIDATE_READY'}


def _begin_activation(store, deps, owner, active):
    """CANDIDATE_READY: proof再検証→ACTIVATINGへ。同operation/generationでintent耐久化。"""
    proof = active.get('runtime_proof')
    if proof is None:
        raise OwnerError('RUNTIME_PROOF_MISSING')
    proof = contract.parse_document(json.dumps(proof).encode(), 'RuntimeProof',
                                    max_bytes=DOC_BYTES)
    if not (proof['operation_id'] == active['operation_id']
            and proof['release'] == active['target']
            and proof['runtime_generation'] == active['candidate_generation']
            and proof['startup_nonce'] == active['startup_nonce']
            and proof['activation_receipt_id'] == active['activation_receipt_id']):
        raise OwnerError('RUNTIME_PROOF_MISMATCH')
    # proof内のservice証拠を現在観測と再照合(起動後の差替えを検出)
    desires = deps.service_desires()
    probe = deps.probe_runtime(active)
    raw_services = probe.get('services') if isinstance(probe, dict) else None
    if not isinstance(raw_services, dict):
        raise OwnerError('RUNTIME_PROBE_INSUFFICIENT')
    for svc in proof['services']:
        if svc['service_id'] not in desires:
            raise OwnerError('SERVICE_SET_MISMATCH')
        _service_proof(raw_services.get(svc['service_id']), svc['service_id'],
                       desires[svc['service_id']], svc['proof_id'],
                       deps.now_ms())

    def activating(new):
        a = new['active']
        if (a is None or a['operation_id'] != active['operation_id']
                or a['phase'] != 'CANDIDATE_READY'):
            raise OwnerError('PHASE_MISMATCH')
        a['phase'] = 'ACTIVATING'

    # ACTIVATINGを先に耐久化してからfence解放: 逆順だと解放済み・未遷移の
    # 隙間に別更新が割込み得る。解放失敗時は次tickの_complete_verifiedが
    # 冪等に再試行する(deps.release_update_fenceは冪等必須)。
    _mutate(store, activating)
    deps.release_update_fence(active['candidate_generation'])
    return {'phase': 'ACTIVATING'}


def _heartbeat_ok(deps, active, proof):
    """fresh heartbeat 2回(間隔>=5秒)をresident serviceごとに要求する。"""
    observations = deps.heartbeat_observations(active)
    if not isinstance(observations, list):
        raise OwnerError('HEARTBEAT_MISSING')
    by_service = {}
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        sid = obs.get('service_id')
        at = obs.get('heartbeat_at')
        if isinstance(sid, str) and isinstance(at, int):
            by_service.setdefault(sid, []).append(at)
    for svc in proof['services']:
        if svc['desired_kind'] != 'resident':
            continue
        times = sorted(by_service.get(svc['service_id'], []))
        if len(times) < 2 or times[-1] - times[-2] < HEARTBEAT_MIN_INTERVAL_MS:
            raise OwnerError('HEARTBEAT_INSUFFICIENT')


def _verified_outcome(store, deps, owner, active, spent):
    seq = sum(1 for e in owner['outbox']
              if e['operation_id'] == active['operation_id']) + 1
    report_id = _det_uuid(f'outcome|{active["operation_id"]}|{seq}')
    outcome = {'schema_version': 'mcs.outcome.v3', 'report_id': report_id,
               'request_nonce': hashlib.sha256(
                   ('outcome-nonce|' + report_id).encode()).hexdigest(),
               'report_sequence': seq, 'scope': owner['scope'],
               'operation_id': active['operation_id'],
               'grant_id': spent['grant_id'],
               'attempt_key': active['attempt_key'],
               'target': active['target'],
               'installed': active['target'],
               'verified_installed': active['target'],
               'state': 'verified', 'readiness': 'ready',
               'runtime_generation': active['candidate_generation'],
               'started_at': spent['started_at'],
               'observed_at': deps.now_ms(),
               'verified_at': deps.now_ms(),
               'local_proof_id': active['runtime_proof']['proof_id'],
               'error_code': 'NONE', 'rollout': active['rollout']}
    outcome = contract.parse_typed(json.dumps(outcome).encode(), 'Outcome')
    raw = json.dumps(outcome, ensure_ascii=True, separators=(',', ':')).encode()
    return outcome, raw


def _complete_verified(store, deps, owner, active):
    """ACTIVATING: fence再確認→heartbeat観測→service/source再照合→VERIFIED原子確定。"""
    # _begin_activationのfence解放がcrashで未完了の場合に備え冪等に再試行
    deps.release_update_fence(active['candidate_generation'])
    proof = active.get('runtime_proof')
    if proof is None:
        raise OwnerError('RUNTIME_PROOF_MISSING')
    _heartbeat_ok(deps, active, proof)
    # 全necessary serviceとsourceを再照合(起動後の改変を検出)
    desires = deps.service_desires()
    probe = deps.probe_runtime(active)
    raw_services = probe.get('services') if isinstance(probe, dict) else None
    if not isinstance(raw_services, dict):
        raise OwnerError('RUNTIME_PROBE_INSUFFICIENT')
    for svc in proof['services']:
        if svc['service_id'] not in desires:
            raise OwnerError('SERVICE_SET_MISMATCH')
        _service_proof(raw_services.get(svc['service_id']), svc['service_id'],
                       desires[svc['service_id']], svc['proof_id'],
                       deps.now_ms())
    if (probe.get('loaded_source_sha256')
            != deps.loaded_source_expect(active)):
        raise OwnerError('LOADED_SOURCE_MISMATCH')
    spent = next((s for s in owner['spent']
                  if s['operation_id'] == active['operation_id']), None)
    if spent is None:
        raise OwnerError('SPENT_MISSING')
    outcome, raw = _verified_outcome(store, deps, owner, active, spent)
    outcome_sha = hashlib.sha256(raw).hexdigest()
    # VERIFIEDのproof_sha256はruntime proofへの決定論的束縛(03_CONTRACTS file表)
    proof_sha = contract.hash_v3('runtime-proof', active['runtime_proof'])
    terminal = {'schema_version': 'mcs.terminal-record.v3',
                'operation_id': active['operation_id'],
                'attempt_key': active['attempt_key'], 'state': 'VERIFIED',
                'outcome_sha256': outcome_sha, 'proof_sha256': proof_sha,
                'terminal_at': deps.now_ms()}
    terminal = contract.parse_document(json.dumps(terminal).encode(),
                                       'TerminalRecord', max_bytes=DOC_BYTES)
    manifest_sha = deps.measure_source_manifest()
    if not (isinstance(manifest_sha, str) and len(manifest_sha) == 64):
        raise OwnerError('MANIFEST_UNVERIFIED')
    owner = auto_state.load(store)
    auto_state.finalize_verified(
        store, terminal, outcome_raw=raw.decode('utf-8'),
        source_manifest_sha256=manifest_sha,
        expected_execution_revision=owner['execution_revision'])
    auto_state.retire_intent(store, operation_id=active['operation_id'],
                             terminal_record=terminal)
    return {'phase': 'VERIFIED'}


def advance_runtime(store, deps):
    """APPLIED以降の観測的前進。CANDIDATE_READY/ACTIVATING/VERIFIEDまで。

    STARTED以前・UNKNOWNは触らない(観測的復旧の領域)。
    """
    owner = auto_state.load(store)
    active = owner['active']
    if active is None or active['phase'] not in (
            'APPLIED', 'CANDIDATE_READY', 'ACTIVATING'):
        return {'action': 'observe_only'}
    if deps.local_revoke() or deps.holds():
        return {'action': 'hold', 'phase': active['phase']}
    with deps.acquire_locks(LOCK_SET):
        owner = auto_state.load(store)
        active = owner['active']
        phase = active['phase']
        if phase == 'APPLIED':
            return _collect_service_proofs(store, deps, owner, active)
        if phase == 'CANDIDATE_READY':
            return _begin_activation(store, deps, owner, active)
        return _complete_verified(store, deps, owner, active)
