"""v3 device health reporting (M8/W14): VERIFIED bindingの観測報告。

RuntimeProofはfinalizeでactive終結と共にowner正本から消えるため、
_complete_verifiedがfinalize前に`record_verified_binding`でbinding snapshot
(operation_id/grant_id/rollout/target/runtime_generation/boot_id/
local_runtime_proof_id/scope)を`release-health.json`へ耐久化する。以後の
報告はその固定bindingからHealthObservationを組み立て、
/v3/mcs/releases/healthへ送る。pending observationはACK確認まで同一raw
bytesを再送する(SEM32: report_id/sequence/observed_atを作り直さない。
AT127のsame-hash冪等ACKと整合)。

観測のみ。service再起動・rollback・中央stage書換えは行わない。bindingが
無い状態では報告を組み立てない(unknownをhealthyにしない)。状態RMWは
release_owner_lock下、外部送信はlock外で行う。
"""
import hashlib
import json
import uuid

from adapters.sdc import release_auto_contract as contract

HEALTH_NAME = 'release-health.json'
HEALTH_PATH = '/v3/mcs/releases/health'
STATE_SCHEMA = 'mcs.release-health-local.v1'


def _blank():
    return {'schema_version': STATE_SCHEMA, 'health_sequence': 0,
            'binding': None, 'pending': None, 'last_ack': None}


def _load(store):
    raw = store.read(HEALTH_NAME)
    if raw is None:
        return _blank()
    if (not isinstance(raw, dict)
            or raw.get('schema_version') != STATE_SCHEMA
            or not isinstance(raw.get('health_sequence'), int)
            or raw['health_sequence'] < 0):
        raise ValueError('RELEASE_HEALTH_INVALID')
    return raw


def _save(store, value):
    store.write(HEALTH_NAME, value)


def record_verified_binding(store, active, spent, scope):
    """VERIFIED確定前にruntime proof由来のbinding snapshotを耐久化する。

    同一operationの再呼出しは冪等(最初の記録を保持)。別operationは
    現在runtimeの証拠として新しいbindingで置き換える。runtime_proofが
    無いactiveでは何もしない。
    """
    proof = active.get('runtime_proof')
    if proof is None:
        return None
    operation_id = active['operation_id']
    binding = {'operation_id': operation_id,
               'grant_id': spent['grant_id'],
               'rollout': active['rollout'],
               'target': active['target'],
               'runtime_generation': active['candidate_generation'],
               'boot_id': proof['boot_id'],
               'local_runtime_proof_id': proof['proof_id'],
               'scope': scope,
               'observed_at': proof['observed_at']}
    with store.release_owner_lock():
        state = _load(store)
        existing = state.get('binding')
        if isinstance(existing, dict) \
                and existing.get('operation_id') == operation_id:
            return existing
        state['binding'] = binding
        _save(store, state)
    return binding


def _build_observation(state, now_ms):
    """bindingから次のHealthObservationを組み立てる。schema検証済みdictを返す。"""
    binding = state['binding']
    if binding is None:
        return None
    sequence = state['health_sequence'] + 1
    report_id = str(uuid.uuid4())
    obs = {'schema_version': 'mcs.health-observation.v3',
           'report_id': report_id,
           'request_nonce': hashlib.sha256(
               ('health-nonce|' + report_id).encode()).hexdigest(),
           'health_sequence': sequence,
           'scope': binding['scope'],
           'rollout': binding['rollout'],
           'operation_id': binding['operation_id'],
           'grant_id': binding['grant_id'],
           'target': binding['target'],
           'runtime_generation': binding['runtime_generation'],
           'boot_id': binding['boot_id'],
           'local_runtime_proof_id': binding['local_runtime_proof_id'],
           'status': 'healthy', 'reason_code': 'NONE',
           'observed_at': now_ms}
    obs = contract.parse_typed(json.dumps(obs).encode(), 'HealthObservation')
    # HealthAckのobservation_sha256は中央がcanonicalAuto hashで返す — 送信bytesを
    # canonical形に揃え、pending sha256と中央hashを一致させる。
    raw = contract.canonical_typed(obs, 'HealthObservation')
    return obs, raw, sequence


def _ack_matches(pending, ack):
    return (isinstance(ack, dict)
            and ack.get('schema_version') == 'mcs.health-ack.v3'
            and ack.get('report_id') == pending['report_id']
            and ack.get('observation_sha256') == pending['sha256']
            and ack.get('request_nonce') == pending['request_nonce']
            and ack.get('scope') == pending['scope'])


def report_health(store, deps):
    """1回分のhealth報告。pendingがあれば同一rawを再送、なければ新規観測を送る。

    durable-before-effect: pending rawは送信前にrelease-health.jsonへ保存。
    ACK一致時だけpendingを消す。送信失敗はpendingを残し次回同一bytesで再送。
    戻り値は固定action語のみ。
    """
    with store.release_owner_lock():
        state = _load(store)
        pending = state.get('pending')
        if pending is None:
            built = _build_observation(state, deps.now_ms())
            if built is None:
                return {'action': 'no_binding'}
            obs, raw, sequence = built
            pending = {'report_id': obs['report_id'],
                       'request_nonce': obs['request_nonce'],
                       'scope': obs['scope'],
                       'sha256': hashlib.sha256(raw).hexdigest(),
                       'raw': raw.decode('utf-8')}
            state['pending'] = pending
            state['health_sequence'] = sequence
            _save(store, state)
        raw = pending['raw'].encode('utf-8')
    # 外部送信はlock外。ACKはreport_id/observation_sha256/request_nonce/scope一致のみ。
    try:
        response = deps.signed_send(HEALTH_PATH, raw)
        ack = deps.verify(HEALTH_PATH, response)
    except Exception:
        return {'action': 'send_failed', 'pending': True}
    with store.release_owner_lock():
        state = _load(store)
        current = state.get('pending')
        if current is None or current.get('sha256') != pending['sha256']:
            return {'action': 'superseded'}
        if not _ack_matches(current, ack):
            return {'action': 'ack_mismatch', 'pending': True}
        state['pending'] = None
        state['last_ack'] = {'report_id': current['report_id'],
                             'acked_at': deps.now_ms()}
        _save(store, state)
    return {'action': 'acked'}


def health_status(store):
    """read-onlyのhealth状態報告。W14/status表示が使う固定DTO。"""
    try:
        state = _load(store)
    except (OSError, ValueError):
        return {'state': 'unreadable'}
    binding = state.get('binding')
    pending = state.get('pending')
    return {'state': 'pending' if pending is not None
            else ('bound' if binding is not None else 'unbound'),
            'health_sequence': state['health_sequence'],
            'operation_id': binding['operation_id'] if binding else None,
            'pending_report_id': pending['report_id'] if pending else None,
            'last_ack_report_id': (state.get('last_ack') or {}).get('report_id')}
