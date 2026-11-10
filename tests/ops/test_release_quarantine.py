"""v3 quarantine deps (M6/W10実deps面) を合成tmpdir+fake spawn/procで検証する。

実processは立てない。spawnerはargv/envを記録しwitness fileを人工的に書く。
"""
import json
import os
import uuid

import pytest

from mcs.ops import release_quarantine as rq
from mcs.ops.release_auto_owner import OwnerError

BOOT = 'b' * 64
RECEIPT = '11111111-2222-3333-4444-555555555555'
OP = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
NONCE = 'n' * 64
GEN = 7


def _write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(doc, f)


def _witness(sid, deps, **over):
    doc = {'schema_version': rq.WITNESS_SCHEMA, 'receipt_id': RECEIPT,
           'service_id': sid, 'operation_id': OP, 'startup_nonce': NONCE,
           'runtime_generation': GEN, 'boot_id': BOOT,
           'loaded_source_sha256': deps.loaded_source_expect(None),
           'process_instance_id': str(uuid.uuid4()), 'pid': 4000 + len(sid),
           'birth_witness': 'd' * 64, 'state': 'running',
           'observed_at': 1000}
    doc.update(over)
    return doc


class FakeSpawner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, env):
        self.calls.append((list(argv), dict(env)))
        return type('P', (), {'pid': 4242})()


@pytest.fixture
def deps(tmp_path):
    state_dir = tmp_path / 'state'
    gen_root = tmp_path / 'gen'
    gen_root.mkdir()
    (gen_root / 'app.py').write_text('x = 1\n')
    (gen_root / 'sub').mkdir()
    (gen_root / 'sub' / 'mod.py').write_text('y = 2\n')
    services = tmp_path / 'services.json'
    _write(str(services), {
        'schema_version': rq.SERVICES_SCHEMA,
        'services': {'svc-a': {'kind': 'resident', 'entrypoint': 'a'},
                     'svc-b': {'kind': 'timer', 'entrypoint': 'b'}}})
    spawner = FakeSpawner()
    births = {}

    def proc_info(pid):
        return births.get(pid, {'alive': False, 'birth': None})

    d = rq.QuarantineDeps(
        state_dir=str(state_dir), services_path=str(services),
        generation_root=str(gen_root), bootstrap='/boot/entry.py',
        python='/usr/bin/python3', spawner=spawner, proc_info=proc_info,
        now_ms=lambda: 2000, pycache_prefix='/tmp/pyc-gen7', boot_id=BOOT)
    d._births = births
    d._state_dir = state_dir
    return d


def _active():
    return {'operation_id': OP, 'startup_nonce': NONCE,
            'candidate_generation': GEN, 'activation_receipt_id': RECEIPT}


def _plant(deps, sid, **over):
    doc = _witness(sid, deps, **over)
    path = deps._witness_path(RECEIPT, sid)
    _write(path, doc)
    deps._births[doc['pid']] = {'alive': True, 'birth': doc['birth_witness']}
    hb = deps._heartbeat_path(RECEIPT, sid)
    os.makedirs(os.path.dirname(hb), exist_ok=True)
    with open(hb, 'w') as f:
        f.write(json.dumps({'heartbeat_at': 1500}) + '\n')
        f.write(json.dumps({'heartbeat_at': 1900}) + '\n')
    return doc


# --- argv / tree -------------------------------------------------------------

def test_quarantine_argv_isolated_form():
    argv = rq.quarantine_argv('/py', '/boot.py', '/tmp/pc')
    assert argv == ['/py', '-I', '-S', '-X', 'pycache_prefix=/tmp/pc', '/boot.py']


def test_quarantine_argv_rejects_empty():
    with pytest.raises(OwnerError):
        rq.quarantine_argv('', '/boot.py', '/tmp/pc')


def test_measure_tree_deterministic_and_ignores_pyc(tmp_path):
    root = tmp_path / 't'
    root.mkdir()
    (root / 'a.py').write_text('a')
    (root / 'b.pyc').write_text('junk')
    h1 = rq.measure_tree(str(root))
    assert h1 == rq.measure_tree(str(root))
    (root / 'a.py').write_text('changed')
    assert rq.measure_tree(str(root)) != h1


# --- services / fence ---------------------------------------------------------

def test_service_desires_reads_manifest(deps):
    assert deps.service_desires() == {'svc-a': 'resident', 'svc-b': 'timer'}


def test_service_desires_fail_closed(deps, tmp_path):
    bad = tmp_path / 'bad.json'
    _write(str(bad), {'schema_version': 'wrong', 'services': {}})
    deps.services_path = str(bad)
    with pytest.raises(OwnerError) as e:
        deps.service_desires()
    assert e.value.code == 'SERVICE_SET_UNKNOWN'


def test_release_update_fence_idempotent(deps, tmp_path):
    fence = tmp_path / 'update.fence'
    fence.write_text('x')
    deps.fence_path = str(fence)
    deps.release_update_fence(GEN)
    assert not fence.exists()
    deps.release_update_fence(GEN)  # 未存在でも成功(冪等)


# --- launch -------------------------------------------------------------------

def test_quarantine_launch_spawns_each_service_with_binding_env(deps):
    deps.quarantine_launch(_active(), RECEIPT)
    assert len(deps.spawner.calls) == 2
    argv, env = deps.spawner.calls[0]
    assert argv[1:3] == ['-I', '-S']
    assert 'pycache_prefix=/tmp/pyc-gen7' in argv
    assert env['MCS_Q_RECEIPT_ID'] == RECEIPT
    assert env['MCS_Q_OPERATION_ID'] == OP
    assert env['MCS_Q_STARTUP_NONCE'] == NONCE
    assert env['MCS_Q_GENERATION'] == str(GEN)
    assert env['MCS_Q_SERVICE_ID'] in ('svc-a', 'svc-b')


def test_quarantine_launch_idempotent_skips_witnessed(deps):
    _plant(deps, 'svc-a')
    deps.quarantine_launch(_active(), RECEIPT)
    spawned = {env['MCS_Q_SERVICE_ID'] for _, env in deps.spawner.calls}
    assert spawned == {'svc-b'}


# --- probe --------------------------------------------------------------------

def test_probe_runtime_happy_path(deps):
    _plant(deps, 'svc-a')
    _plant(deps, 'svc-b')
    obs = deps.probe_runtime(_active())
    assert obs['boot_id'] == BOOT
    assert obs['loaded_source_sha256'] == deps.loaded_source_expect(None)
    assert set(obs['services']) == {'svc-a', 'svc-b'}
    svc = obs['services']['svc-a']
    assert svc['pid'] > 0 and svc['birth_witness'] == 'd' * 64
    assert svc['previous_heartbeat_at'] == 1900


def test_probe_missing_witness_fail_closed(deps):
    _plant(deps, 'svc-a')
    with pytest.raises(OwnerError) as e:
        deps.probe_runtime(_active())
    assert e.value.code == 'SERVICE_PROOF_MISSING'


def test_probe_nonce_mismatch_rejected(deps):
    _plant(deps, 'svc-a')
    _plant(deps, 'svc-b', startup_nonce='x' * 64)
    with pytest.raises(OwnerError) as e:
        deps.probe_runtime(_active())
    assert e.value.code == 'SERVICE_PROOF_MISMATCH'


def test_probe_generation_mismatch_rejected(deps):
    _plant(deps, 'svc-a')
    _plant(deps, 'svc-b', runtime_generation=99)
    with pytest.raises(OwnerError) as e:
        deps.probe_runtime(_active())
    assert e.value.code == 'SERVICE_PROOF_MISMATCH'


def test_probe_dead_process_rejected(deps):
    _plant(deps, 'svc-a')
    _plant(deps, 'svc-b')
    deps._births.clear()  # 全process死亡
    with pytest.raises(OwnerError) as e:
        deps.probe_runtime(_active())
    assert e.value.code == 'PROCESS_EVIDENCE_MISMATCH'


def test_probe_birth_mismatch_rejected(deps):
    _plant(deps, 'svc-a')
    _plant(deps, 'svc-b')
    for pid in list(deps._births):
        deps._births[pid]['birth'] = 'e' * 64  # PID再利用/別processを模倣
    with pytest.raises(OwnerError) as e:
        deps.probe_runtime(_active())
    assert e.value.code == 'PROCESS_EVIDENCE_MISMATCH'


def test_probe_loaded_source_mismatch_rejected(deps):
    _plant(deps, 'svc-a', loaded_source_sha256='0' * 64)
    _plant(deps, 'svc-b')
    with pytest.raises(OwnerError) as e:
        deps.probe_runtime(_active())
    assert e.value.code == 'SERVICE_PROOF_MISMATCH'


def test_probe_wrong_receipt_dir_rejected(deps):
    other = '99999999-8888-7777-6666-555555555555'
    doc = _witness('svc-a', deps, receipt_id=other)
    _write(deps._witness_path(other, 'svc-a'), doc)
    deps._births[doc['pid']] = {'alive': True, 'birth': doc['birth_witness']}
    # active receipt配下にはsvc-aが無い→missing
    with pytest.raises(OwnerError) as e:
        deps.probe_runtime(_active())
    assert e.value.code == 'SERVICE_PROOF_MISSING'


# --- heartbeats / manifest -----------------------------------------------------

def test_heartbeat_observations_scoped_to_receipt(deps):
    _plant(deps, 'svc-a')
    _plant(deps, 'svc-b')
    obs = deps.heartbeat_observations(_active())
    assert sorted(o['heartbeat_at'] for o in obs) == [1500, 1500, 1900, 1900]
    assert {o['service_id'] for o in obs} == {'svc-a', 'svc-b'}
    assert deps.heartbeat_observations(
        {'activation_receipt_id': None}) == []


def test_measure_source_manifest_equals_tree(deps):
    assert deps.measure_source_manifest() == deps.loaded_source_expect(None)
    assert deps.measure_source_manifest() == rq.measure_tree(deps.generation_root)


# --- 結合: owner状態機械×実deps面 ---------------------------------------------

class ChildWritingSpawner:
    """子processのtrusted bootstrapを模倣: envのbinding値でwitness+heartbeatを書く。"""

    def __init__(self, state_dir, gen_root, now_ms):
        self.state_dir = state_dir
        self.loaded = rq.measure_tree(str(gen_root))
        self.now_ms = now_ms
        self.births = {}
        self._next_pid = 5000

    def _witness_path(self, receipt, sid):
        return os.path.join(str(self.state_dir), 'quarantine', receipt,
                            f'{sid}.json')

    def _heartbeat_path(self, receipt, sid):
        return os.path.join(str(self.state_dir), 'quarantine', receipt,
                            f'heartbeat-{sid}.jsonl')

    def __call__(self, argv, env):
        sid = env['MCS_Q_SERVICE_ID']
        receipt = env['MCS_Q_RECEIPT_ID']
        pid = self._next_pid
        self._next_pid += 1
        birth = 'b' * 63 + format(pid % 10, 'x')
        self.births[pid] = {'alive': True, 'birth': birth}
        witness = {'schema_version': rq.WITNESS_SCHEMA,
                   'receipt_id': receipt, 'service_id': sid,
                   'operation_id': env['MCS_Q_OPERATION_ID'],
                   'startup_nonce': env['MCS_Q_STARTUP_NONCE'],
                   'runtime_generation': int(env['MCS_Q_GENERATION']),
                   'boot_id': env['MCS_Q_BOOT_ID'],
                   'loaded_source_sha256': self.loaded,
                   'process_instance_id': str(uuid.uuid4()),
                   'pid': pid, 'birth_witness': birth,
                   'state': 'running' if env['MCS_Q_SERVICE_KIND'] == 'resident'
                            else ('armed' if env['MCS_Q_SERVICE_KIND'] == 'timer'
                                  else 'stopped'),
                   'observed_at': 1000}
        _write(self._witness_path(receipt, sid), witness)
        hb = self._heartbeat_path(receipt, sid)
        os.makedirs(os.path.dirname(hb), exist_ok=True)
        now = self.now_ms()
        with open(hb, 'w') as f:
            for at in (now - 6000, now - 1000):
                f.write(json.dumps({'heartbeat_at': at}) + '\n')
        return type('P', (), {'pid': pid})()


class IntegratedDeps:
    """owner/applyのFakeDepsへQuarantineDepsのruntime面を合成する。"""

    def __init__(self, qdeps, base):
        self._q = qdeps
        self._base = base
        self.runtime_advance = self._advance

    def _advance(self, store):
        from mcs.ops import release_runtime_proof as rt
        return rt.advance_runtime(store, self)

    def __getattr__(self, name):
        return getattr(self._q if name in _Q_NAMES else self._base, name)


_Q_NAMES = {'service_desires', 'quarantine_launch', 'probe_runtime',
            'heartbeat_observations', 'loaded_source_expect',
            'measure_source_manifest', 'release_update_fence'}


def test_end_to_end_applied_to_verified_with_real_deps(tmp_path):
    """APPLIED→VERIFIEDを実witness/heartbeatファイルで完走する結合試験。"""
    import test_release_auto_apply as at
    import test_release_auto_owner as ot
    from adapters.sdc import release_auto_state as state
    from mcs.ops import release_auto_apply as apply_mod
    from mcs.ops import release_runtime_proof as rt

    store_dir = tmp_path / 'owner'
    with ot.Store(store_dir, create=True) as store:
        at.seed(store)
        # owner/apply面はFakeDeps、runtime面は実QuarantineDeps
        gen_root = tmp_path / 'gen'
        gen_root.mkdir()
        (gen_root / 'app.py').write_text('x = 1\n')
        services = tmp_path / 'services.json'
        _write(str(services), {
            'schema_version': rq.SERVICES_SCHEMA,
            'services': {'svc-a': {'kind': 'resident', 'entrypoint': 'a'},
                         'svc-b': {'kind': 'timer', 'entrypoint': 'b'},
                         'svc-c': {'kind': 'disabled', 'entrypoint': 'c'}}})
        qstate = tmp_path / 'qstate'
        spawner = ChildWritingSpawner(qstate, gen_root,
                                      now_ms=lambda: ot.NOW_MS)
        births = spawner.births
        qdeps = rq.QuarantineDeps(
            state_dir=str(qstate), services_path=str(services),
            generation_root=str(gen_root), bootstrap='/boot/entry.py',
            python='/usr/bin/python3', spawner=spawner,
            proc_info=lambda pid: births.get(pid),
            now_ms=lambda: ot.NOW_MS, pycache_prefix='/tmp/pc',
            boot_id=BOOT)
        base = at.ApplyDeps()
        deps = IntegratedDeps(qdeps, base)
        op_id, _ = at.started(store, deps)
        apply_mod.apply_code_only_v3(store, at._capability(store), deps)
        assert state.load(store)['active']['phase'] == 'APPLIED'
        for _ in range(3):
            rt.advance_runtime(store, deps)
        owner = state.load(store)
        assert owner['active'] is None
        assert owner['outcomes'][-1]['state'] == 'VERIFIED'
        # witness/heartbeatはreceipt dir配下に観測証拠として残る
        receipts = list((qstate / 'quarantine').iterdir())
        assert len(receipts) == 1
        assert (receipts[0] / 'svc-a.json').exists()
        assert (receipts[0] / 'heartbeat-svc-a.jsonl').exists()
        assert owner['verified_installed'] == ot.TARGET
