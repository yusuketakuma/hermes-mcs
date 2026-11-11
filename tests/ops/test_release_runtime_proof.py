"""v3 runtime proof / activation (M6/W10) を合成Store+fake depsで検証する。

実process/network/gitには触れない。quarantine起動・PID/birth・loaded
source・heartbeatは全てscripted fake。証拠不確実時はphaseを進めずholdする。
"""
import uuid

import pytest

import test_release_auto_apply as at
import test_release_auto_owner as ot
from adapters.sdc import release_auto_contract as contract
from adapters.sdc import release_auto_state as state
import release_auto_apply as apply_mod
import release_auto_owner as owner_mod
import release_runtime_proof as rt

NOW_MS = ot.NOW_MS
BOOT = 'b' * 64
LOADED = 'e' * 64
MANIFEST = 'f' * 64


def _resident_obs(deps):
    return {'state': 'running',
            'process_instance_id': str(uuid.uuid4()),
            'pid': 4321, 'birth_witness': 'c' * 64,
            'loaded_source_sha256': deps.loaded,
            'previous_heartbeat_at': NOW_MS - 1000}


class RuntimeDeps(at.ApplyDeps):
    """apply depsへruntime proof観測面を足したfake。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.desires = {'svc-a': 'resident', 'svc-b': 'timer',
                        'svc-c': 'disabled'}
        self.boot_hex = BOOT  # FakeDeps.boot_id()メソッドと衝突しない名前
        self.loaded = LOADED
        self.manifest = MANIFEST
        self.heartbeats = [
            {'service_id': 'svc-a', 'heartbeat_at': NOW_MS - 6000},
            {'service_id': 'svc-a', 'heartbeat_at': NOW_MS - 1000}]
        self.fence_calls = []
        self.launches = []
        self.runtime_advance = self._advance

    def _advance(self, store):
        return rt.advance_runtime(store, self)

    def service_desires(self):
        return dict(self.desires)

    def quarantine_launch(self, active, receipt_id):
        self.launches.append((active['operation_id'], receipt_id))

    def release_update_fence(self, generation):
        self.fence_calls.append(generation)

    def probe_runtime(self, active=None):
        services = {}
        for sid, kind in self.desires.items():
            if kind == 'resident':
                services[sid] = _resident_obs(self)
            elif kind == 'timer':
                services[sid] = {'state': 'armed'}
            else:
                services[sid] = {'state': 'stopped'}
        return {'boot_id': self.boot_hex, 'loaded_source_sha256': self.loaded,
                'services': services}

    def loaded_source_expect(self, active):
        return self.loaded

    def heartbeat_observations(self, active=None):
        return list(self.heartbeats)

    def measure_source_manifest(self):
        return self.manifest


@pytest.fixture
def store(tmp_path):
    with ot.Store(tmp_path / 'private-runtime', create=True) as value:
        yield value


def _applied(store, deps=None):
    """APPLIEDまで進めて(operation_id, deps)を返す。"""
    at.seed(store)
    deps = deps or RuntimeDeps()
    op_id, deps = at.started(store, deps)
    apply_mod.apply_code_only_v3(store, at._capability(store), deps)
    return op_id, deps


def _advance_to(store, deps, phase):
    """目的phaseまでadvance_runtimeを繰り返す(上限10回)。"""
    for _ in range(10):
        cur = state.load(store)['active']
        if cur is None or cur['phase'] == phase or cur['phase'] == 'VERIFIED_DONE':
            break
        rt.advance_runtime(store, deps)
    return state.load(store)


# --- APPLIED → CANDIDATE_READY ----------------------------------------------

def test_applied_collects_proofs_and_reaches_candidate_ready(store):
    op_id, deps = _applied(store)
    owner = rt.advance_runtime(store, deps)
    assert owner['phase'] == 'CANDIDATE_READY'
    active = state.load(store)['active']
    assert active['phase'] == 'CANDIDATE_READY'
    assert active['startup_nonce'] is not None
    assert active['activation_receipt_id'] is not None
    proof = active['runtime_proof']
    assert proof['operation_id'] == op_id
    assert proof['runtime_generation'] == active['candidate_generation']
    assert proof['startup_nonce'] == active['startup_nonce']
    assert proof['activation_receipt_id'] == active['activation_receipt_id']
    assert len(proof['services']) == 3
    assert deps.launches == [(op_id, active['activation_receipt_id'])]


def test_resident_proof_requires_process_evidence(store):
    op_id, deps = _applied(store)
    deps.probe_runtime = lambda active=None: {
        'boot_id': BOOT, 'loaded_source_sha256': LOADED,
        'services': {'svc-a': {'state': 'running', 'pid': 4321},
                     'svc-b': {'state': 'armed'},
                     'svc-c': {'state': 'stopped'}}}
    with pytest.raises(owner_mod.OwnerError) as e:
        rt.advance_runtime(store, deps)
    assert e.value.code == 'SERVICE_PROOF_INSUFFICIENT'
    assert state.load(store)['active']['phase'] == 'APPLIED'


def test_loaded_source_mismatch_holds(store):
    _, deps = _applied(store)
    deps.loaded = '9' * 64  # expect側もprobe側も同じ値→一致してしまうのでprobeだけ変える
    orig = deps.probe_runtime

    def bad_probe(active=None):
        p = orig(active)
        p['loaded_source_sha256'] = '8' * 64
        return p
    deps.probe_runtime = bad_probe
    with pytest.raises(owner_mod.OwnerError) as e:
        rt.advance_runtime(store, deps)
    assert e.value.code == 'LOADED_SOURCE_MISMATCH'
    assert state.load(store)['active']['phase'] == 'APPLIED'


def test_unknown_service_set_rejected(store):
    _, deps = _applied(store)
    deps.desires = {}
    with pytest.raises(owner_mod.OwnerError) as e:
        rt.advance_runtime(store, deps)
    assert e.value.code == 'SERVICE_SET_UNKNOWN'
    assert state.load(store)['active']['phase'] == 'APPLIED'


# --- CANDIDATE_READY → ACTIVATING -------------------------------------------

def test_candidate_ready_begins_activation_and_releases_fence(store):
    _, deps = _applied(store)
    rt.advance_runtime(store, deps)  # → CANDIDATE_READY
    candidate = state.load(store)['active']['candidate_generation']
    owner = rt.advance_runtime(store, deps)
    assert owner['phase'] == 'ACTIVATING'
    assert state.load(store)['active']['phase'] == 'ACTIVATING'
    # 自分の更新fenceだけを同generationで解放する
    assert deps.fence_calls == [candidate]


def test_service_set_drift_rejected_at_activation(store):
    _, deps = _applied(store)
    rt.advance_runtime(store, deps)  # CANDIDATE_READY
    # proof確定後にdesired service集合が縮小→照合で検出して進めない
    deps.desires = {'svc-a': 'resident'}
    with pytest.raises(owner_mod.OwnerError) as e:
        rt.advance_runtime(store, deps)
    assert e.value.code == 'SERVICE_SET_MISMATCH'
    assert state.load(store)['active']['phase'] == 'CANDIDATE_READY'


# --- ACTIVATING → VERIFIED ---------------------------------------------------

def test_fence_release_crash_retried_by_complete_verified(store):
    """ACTIVATING永続化後・fence解放のcrash: _complete_verifiedが冪等に再試行。"""
    _, deps = _applied(store)
    rt.advance_runtime(store, deps)  # → CANDIDATE_READY
    real = deps.release_update_fence
    calls = []

    def flaky(generation):
        calls.append(generation)
        if len(calls) == 1:
            raise RuntimeError('synthetic fence crash')
        return real(generation)

    deps.release_update_fence = flaky
    with pytest.raises(RuntimeError):
        rt.advance_runtime(store, deps)  # ACTIVATING永続化後にcrash
    # phaseはACTIVATINGで保持(未検証を成功化しない)
    assert state.load(store)['active']['phase'] == 'ACTIVATING'
    # 次tick: _complete_verified冒頭でfenceを冪等再試行→VERIFIEDへ進む
    rt.advance_runtime(store, deps)
    owner = state.load(store)
    assert owner['active'] is None
    assert owner['outcomes'][-1]['state'] == 'VERIFIED'
    # fenceは同generationで2回呼ばれた(失敗1回+冪等再試行1回)
    candidate = calls[0]
    assert calls == [candidate, candidate]


def test_quarantine_launch_crash_retries_with_same_receipt(store):
    """launch成功後・receipt永続化前のcrash: 同一receipt_idで冪等再試行。"""
    _, deps = _applied(store)
    real = deps.quarantine_launch
    calls = []

    def flaky(active, receipt_id):
        calls.append(receipt_id)
        real(active, receipt_id)
        if len(calls) == 1:
            raise RuntimeError('post-launch crash')

    deps.quarantine_launch = flaky
    with pytest.raises(RuntimeError):
        rt.advance_runtime(store, deps)
    # receipt未永続化・APPLIED保持(起動証拠を推測で作らない)
    active = state.load(store)['active']
    assert active['phase'] == 'APPLIED'
    assert active.get('activation_receipt_id') is None
    # 再試行はoperation決定論的な同一receipt_idでlaunchする
    owner = rt.advance_runtime(store, deps)
    assert owner['phase'] == 'CANDIDATE_READY'
    assert calls[0] == calls[1]
    assert deps.launches[0] == deps.launches[1]


def test_activating_verifies_atomically(store):
    op_id, deps = _applied(store)
    owner = _advance_to(store, deps, 'ACTIVATING')
    assert owner['active']['phase'] == 'ACTIVATING'
    candidate = owner['active']['candidate_generation']
    rt.advance_runtime(store, deps)
    owner = state.load(store)
    assert owner['active'] is None
    term = owner['outcomes'][-1]
    assert term['state'] == 'VERIFIED'
    assert term['operation_id'] == op_id
    assert owner['verified_installed'] == owner['installed'] == ot.TARGET
    assert owner['runtime_generation'] == candidate
    # bindingがtargetへ更新されている
    assert any(b['release_id'] == ot.TARGET['release_id']
               for b in owner['bindings'])
    # intentはretired
    intent = state.load_intent(store)
    assert intent is not None and intent['phase'] == 'retired'
    # VERIFIED outbox entryがある
    entry = next((e for e in owner['outbox']
                  if e['operation_id'] == op_id), None)
    assert entry is not None and entry['acknowledged'] is False
    # SEM27: OutcomeAckのoutcome_sha256は中央canonicalAuto hash — 送信rawは
    # canonical形(再canonicalizeで不変)でなければACK照合が一致しない。
    assert entry['raw'] == contract.canonical_typed(
        contract.parse_typed(entry['raw'], 'Outcome'),
        'Outcome').decode('utf-8')


def test_heartbeat_insufficient_keeps_activating(store):
    _, deps = _applied(store)
    _advance_to(store, deps, 'ACTIVATING')
    deps.heartbeats = [{'service_id': 'svc-a', 'heartbeat_at': NOW_MS}]
    with pytest.raises(owner_mod.OwnerError) as e:
        rt.advance_runtime(store, deps)
    assert e.value.code == 'HEARTBEAT_INSUFFICIENT'
    assert state.load(store)['active']['phase'] == 'ACTIVATING'


def test_heartbeat_gap_too_small_rejected(store):
    _, deps = _applied(store)
    _advance_to(store, deps, 'ACTIVATING')
    deps.heartbeats = [
        {'service_id': 'svc-a', 'heartbeat_at': NOW_MS - 4000},
        {'service_id': 'svc-a', 'heartbeat_at': NOW_MS}]
    with pytest.raises(owner_mod.OwnerError) as e:
        rt.advance_runtime(store, deps)
    assert e.value.code == 'HEARTBEAT_INSUFFICIENT'


def test_service_drift_at_verification_rejected(store):
    _, deps = _applied(store)
    _advance_to(store, deps, 'ACTIVATING')
    orig = deps.probe_runtime

    def drifted(active=None):
        p = orig(active)
        p['services']['svc-a'] = {'state': 'stopped'}
        return p
    deps.probe_runtime = drifted
    with pytest.raises(owner_mod.OwnerError) as e:
        rt.advance_runtime(store, deps)
    assert e.value.code == 'SERVICE_PROOF_INSUFFICIENT'
    assert state.load(store)['active']['phase'] == 'ACTIVATING'


def test_manifest_unverified_holds(store):
    _, deps = _applied(store)
    _advance_to(store, deps, 'ACTIVATING')
    deps.manifest = None
    with pytest.raises(owner_mod.OwnerError) as e:
        rt.advance_runtime(store, deps)
    assert e.value.code == 'MANIFEST_UNVERIFIED'
    assert state.load(store)['active']['phase'] == 'ACTIVATING'


# --- tick/resume integration -------------------------------------------------

def test_tick_drives_runtime_phases_one_step_per_tick(store):
    deps = RuntimeDeps()
    _applied(store, deps)
    owner_mod.tick(store, deps)
    assert state.load(store)['active']['phase'] == 'CANDIDATE_READY'
    owner_mod.tick(store, deps)
    assert state.load(store)['active']['phase'] == 'ACTIVATING'
    owner_mod.tick(store, deps)
    owner = state.load(store)
    assert owner['active'] is None
    assert owner['outcomes'][-1]['state'] == 'VERIFIED'


def test_resume_holds_when_advance_hook_missing(store):
    at.seed(store)
    deps = RuntimeDeps()
    op_id, deps = at.started(store, deps)
    apply_mod.apply_code_only_v3(store, at._capability(store), deps)
    del deps.runtime_advance
    info = owner_mod.resume(store, deps)
    assert info['action'] == 'observe_only'
    assert state.load(store)['active']['phase'] == 'APPLIED'


def test_advance_error_is_hold_not_crash(store):
    at.seed(store)
    deps = RuntimeDeps()
    op_id, deps = at.started(store, deps)
    apply_mod.apply_code_only_v3(store, at._capability(store), deps)
    deps.desires = {}  # SERVICE_SET_UNKNOWNを起こす
    info = owner_mod.resume(store, deps)
    assert info['action'] == 'hold'
    assert info['code'] == 'SERVICE_SET_UNKNOWN'
    assert state.load(store)['active']['phase'] == 'APPLIED'
