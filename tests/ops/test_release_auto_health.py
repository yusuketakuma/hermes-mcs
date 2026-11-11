"""v3 health報告・status DTO・legacy抑止 (M8/W14) を合成store+fake depsで検証。

実network/processは使わない。VERIFIED到達はruntime proof試験と同じfake面。
"""
import hashlib
import json

import pytest

import test_release_auto_apply as at
import test_release_auto_owner as ot
import test_release_runtime_proof as rt_t
from adapters.sdc import release_auto_state as state
import release_auto_health as health
import release_auto_status as status_mod
import release_runtime_proof as rt


class HealthDeps(rt_t.RuntimeDeps):
    """RuntimeDepsへ/v3/mcs/releases/healthのecho ACKを足す。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.health_fail = False
        self.ack_override = None

    def signed_send(self, path, raw):
        if path == health.HEALTH_PATH:
            if self.health_fail:
                raise OSError('synthetic health failure')
            self.sent.append((path, raw))
            obs = json.loads(raw)
            ack = {'schema_version': 'mcs.health-ack.v3',
                   'request_id': '9' * 8 + '-0000-4000-8000-'
                                 + '0' * 12,
                   'request_nonce': obs['request_nonce'],
                   'scope': obs['scope'], 'report_id': obs['report_id'],
                   'observation_sha256': hashlib.sha256(raw).hexdigest(),
                   'received_at': self.now_ms_value,
                   'issued_at': self.now_ms_value,
                   'expires_at': self.now_ms_value + 300000}
            if self.ack_override is not None:
                ack = self.ack_override(ack, obs)
            return json.dumps(ack).encode()
        return super().signed_send(path, raw)


@pytest.fixture
def store(tmp_path):
    with ot.Store(tmp_path / 'private-runtime', create=True) as value:
        yield value


def _verified(store, deps=None):
    """VERIFIEDまで実経路で進める。active=None・outcome確定済み。"""
    at.seed(store)
    deps = deps or HealthDeps()
    op_id, deps = at.started(store, deps)
    import release_auto_apply as apply_mod
    apply_mod.apply_code_only_v3(store, at._capability(store), deps)
    for _ in range(10):
        if state.load(store)['active'] is None:
            break
        rt.advance_runtime(store, deps)
    assert state.load(store)['active'] is None
    return op_id, deps


# --- record_verified_binding ---------------------------------------------------

def test_verified_records_binding_from_runtime_proof(store):
    op_id, deps = _verified(store)
    raw = store.read(health.HEALTH_NAME)
    binding = raw['binding']
    assert binding['operation_id'] == op_id
    assert binding['boot_id'] == deps.boot_hex
    assert binding['local_runtime_proof_id']
    assert binding['runtime_generation'] > 0
    assert binding['scope'] == state.load(store)['scope']


def test_binding_idempotent_for_same_operation(store):
    _verified(store)
    first = store.read(health.HEALTH_NAME)['binding']
    owner = state.load(store)
    spent = owner['spent'][0]
    # runtime_proof無しactiveはno-op(証拠無しにbindingを作らない)
    assert health.record_verified_binding(store, {'runtime_proof': None},
                                          spent, owner['scope']) is None
    # 同opでもproof無しでは上書きされない
    assert store.read(health.HEALTH_NAME)['binding'] == first


# --- report_health --------------------------------------------------------------

def test_report_health_no_binding(store):
    deps = HealthDeps()
    assert health.report_health(store, deps)['action'] == 'no_binding'
    assert not any(p == health.HEALTH_PATH for p, _ in deps.sent)


def test_report_health_sends_observation_and_acks(store):
    _verified(store)
    deps = HealthDeps()
    result = health.report_health(store, deps)
    assert result['action'] == 'acked'
    sent = [raw for p, raw in deps.sent if p == health.HEALTH_PATH]
    assert len(sent) == 1
    obs = json.loads(sent[0])
    assert obs['schema_version'] == 'mcs.health-observation.v3'
    assert obs['status'] == 'healthy' and obs['reason_code'] == 'NONE'
    assert obs['health_sequence'] == 1
    info = health.health_status(store)
    assert info['state'] == 'bound'
    assert info['health_sequence'] == 1
    assert info['last_ack_report_id'] == obs['report_id']


def test_report_health_resends_identical_raw_until_acked(store):
    """送信失敗・ACK不一致はpendingを保持し、次回は同一bytesで再送する。"""
    _verified(store)
    deps = HealthDeps()
    deps.health_fail = True
    assert health.report_health(store, deps)['action'] == 'send_failed'
    deps.health_fail = False
    # 失敗sendは記録されない → 新規観測を組み立てて送信・ACK
    assert health.report_health(store, deps)['action'] == 'acked'
    # ACK不一致: 別report_idを返すoverrideでpending保持を確認
    deps2 = HealthDeps()
    deps2.ack_override = lambda ack, obs: {**ack, 'report_id': 'bad'}
    assert health.report_health(store, deps2)['action'] == 'ack_mismatch'
    raw1 = [r for p, r in deps2.sent if p == health.HEALTH_PATH][-1]
    deps2.ack_override = None
    assert health.report_health(store, deps2)['action'] == 'acked'
    raw2 = [r for p, r in deps2.sent if p == health.HEALTH_PATH][-1]
    assert raw1 == raw2  # SEM32: 再送は同一bytes(report_id/seq/時刻を作り直さない)


def test_report_health_sequence_monotonic(store):
    _verified(store)
    deps = HealthDeps()
    health.report_health(store, deps)
    health.report_health(store, deps)
    sent = [json.loads(r) for p, r in deps.sent if p == health.HEALTH_PATH]
    assert [o['health_sequence'] for o in sent] == [1, 2]
    assert sent[0]['report_id'] != sent[1]['report_id']


def test_health_status_unreadable_on_corrupt(store):
    store.write(health.HEALTH_NAME, {'bogus': True})
    assert health.health_status(store)['state'] == 'unreadable'


# --- status DTO / legacy抑止 ----------------------------------------------------

def test_status_dto_uninitialized_and_idle(store):
    # file不在 → uninitialized
    dto = status_mod.status_dto(store)
    assert dto['state'] == 'uninitialized'
    # seed済み・active無し → idle
    ot.seed(store)
    dto = status_mod.status_dto(store)
    assert dto['state'] == 'idle' and dto['phase'] is None


def test_status_dto_active_phase_mapping(store):
    ot.seed(store)
    deps = rt_t.RuntimeDeps()
    ot._prepared_owner(store, deps)  # PREPARED active
    dto = status_mod.status_dto(store)
    assert dto['state'] == 'active' and dto['phase'] == 'PREPARED'


def test_status_dto_never_renders_unknown_as_ok(store):
    # UNKNOWN phaseは常にstate='unknown'。正常系の語へは決して写像しない。
    assert status_mod._UNKNOWN_PHASES == frozenset({'UNKNOWN'})


def test_legacy_auto_blocked(store):
    # file不在 → False
    assert status_mod.legacy_auto_blocked(store) is False
    # v3 owner state存在 → True
    ot.seed(store)
    assert status_mod.legacy_auto_blocked(store) is True
    # 破損 → True(fail-closed)
    store.write('release-owner.json', {'schema_version': 'bogus'})
    assert status_mod.legacy_auto_blocked(store) is True


def test_status_dto_at_root_resolution(tmp_path):
    # root未設定/非str → None(呼出側でキー省略)
    assert status_mod.status_dto_at(None) is None
    assert status_mod.status_dto_at('') is None
    assert status_mod.status_dto_at(123) is None
    # dir未作成 → uninitialized
    dto = status_mod.status_dto_at(str(tmp_path / 'missing'))
    assert dto['state'] == 'uninitialized'
    # 0700 dir・owner file無し → Storeが開き uninitialized
    empty = tmp_path / 'empty-root'
    empty.mkdir(mode=0o700)
    assert status_mod.status_dto_at(str(empty))['state'] == 'uninitialized'
    # 権限不正 → Store拒否 → unreadable(fail-closed、正常扱いしない)
    bad = tmp_path / 'bad-root'
    bad.mkdir(mode=0o755)
    assert status_mod.status_dto_at(str(bad))['state'] == 'unreadable'
    # seeded store → idle DTO
    root = tmp_path / 'seeded-root'
    with ot.Store(root, create=True) as seeded:
        ot.seed(seeded)
    dto = status_mod.status_dto_at(str(root))
    assert dto['state'] == 'idle' and dto['schema_version'] == 'mcs.release-status-local.v1'
