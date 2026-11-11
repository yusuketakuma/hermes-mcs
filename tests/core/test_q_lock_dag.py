"""Q(runtime-entry.lock)のDAG契約: SH保持者0のEX証明・Q→C順序・FD継承identity。"""
import os
from pathlib import Path

import pytest

import runtime_entry
from adapters.sdc.storage import Store

U = '12345678-1234-4234-8234-123456789abc'
H = 'a' * 64


def owner_value(store):
    ref = {'release_id': H, 'code_seq': 1, 'source_commit': 'b' * 40,
           'artifact_sha256': 'c' * 64, 'artifact_size': 1024}
    return {
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
        'highest_seen': ref, 'installed': ref, 'highest_installed': ref, 'verified_installed': ref,
        'runtime_generation': 7, 'bindings': [{'code_seq': 1, 'release_id': H}],
        'active': None, 'spent': [], 'outcomes': [], 'outbox': [], 'prepare_budget': [],
    }


@pytest.fixture
def runtime_home(tmp_path):
    return tmp_path / 'mcs-home'


def seed_v3(runtime_home):
    root = runtime_entry.control_root(runtime_home)
    root.mkdir(mode=0o700, parents=True)
    os.chmod(root.parent, 0o700)
    with Store(root) as store:
        store.install_runtime_entry_lock()
        store.write('release-owner.json', owner_value(store))
    return root


@pytest.fixture(autouse=True)
def _reset_current():
    yield
    runtime_entry._CURRENT = None


def test_ex_held_while_shared_holders_alive(runtime_home):
    """Q EX取得成功=管理下業務processのSH保持者が全て退役した証明(AT055/068)。"""
    seed_v3(runtime_home)
    business = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        with Store(runtime_entry.control_root(runtime_home)) as store:
            with pytest.raises(BlockingIOError):
                with store.runtime_entry_lock(shared=False):
                    pass
        business.close()
        with Store(runtime_entry.control_root(runtime_home)) as store:
            with store.runtime_entry_lock(shared=False):
                pass
    finally:
        business.close()


def test_q_shared_does_not_block_config_lock(tmp_path, runtime_home):
    """Q SH保持中もC(config.json.edit.lock)は取得できる — Q/Cは独立lockで
    Q→C順序を構成する。Q保持がconfig書込みを塞がないことを確認する。"""
    import config_edit
    seed_v3(runtime_home)
    entry = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        assert entry.managed
        base = tmp_path / 'config.json'
        with config_edit._edit_lock(str(base)):
            pass
        # C保持中に別process相当のQ SHも共存する(順序は常にQ→C)
        second = runtime_entry.acquire(runtime_home, wait_s=0)
        second.close()
    finally:
        entry.close()


def test_config_lock_source_has_no_release_lock_edge(tmp_path):
    """C→Q逆順edgeの静的検査: config編集経路はruntime_entry/Qを取得しない。"""
    root = Path(__file__).resolve().parents[2]
    for rel in ('mcs/ops/setup/config_edit.py', 'mcs/core/provider_settings.py'):
        text = (root / rel).read_text()
        for marker in ('runtime_entry', 'runtime-entry.lock', 'release_owner_lock',
                       'release_entry_lock', 'runtime_entry_lock'):
            assert marker not in text, f'{rel}: unexpected {marker} (C→Q edge)'


def test_adopted_child_shares_lock_lifetime(runtime_home):
    """adopt()した子のfdは同一open file descriptionを共有し、親close後も
    親が保持していた間はEXを塞ぎ続ける。逆に子closeは親の保持を解かない。"""
    seed_v3(runtime_home)
    parent = runtime_entry.acquire(runtime_home, wait_s=0)
    try:
        fd = os.dup(parent.fd)
        dev, ino = parent.lock_fingerprint
        env = {runtime_entry.CHILD_FDS_ENV: f'{fd}:{dev}:{ino}'}
        child = runtime_entry.adopt(runtime_home, env=env)
        try:
            parent.close()
            with Store(runtime_entry.control_root(runtime_home)) as store:
                with pytest.raises(BlockingIOError):
                    with store.runtime_entry_lock(shared=False):
                        pass
        finally:
            child.close()  # adopted close releases the inherited fd reference
        with Store(runtime_entry.control_root(runtime_home)) as store:
            with store.runtime_entry_lock(shared=False):
                pass
    finally:
        parent.close()


def test_adopt_rejects_foreign_control_root(runtime_home, tmp_path):
    """別control rootのQ inodeを持つfdはadoptを通さない(borrowed FD identity)。"""
    seed_v3(runtime_home)
    other_home = tmp_path / 'other-home'
    other_root = runtime_entry.control_root(other_home)
    other_root.mkdir(mode=0o700, parents=True)
    os.chmod(other_root.parent, 0o700)
    with Store(other_root) as store:
        store.install_runtime_entry_lock()
        store.write('release-owner.json', owner_value(store))
    with Store(other_root) as store:
        with store.runtime_entry_lock(shared=True) as foreign_fd:
            dup = os.dup(foreign_fd)
            dev = os.fstat(dup).st_dev
            ino = os.fstat(dup).st_ino
            try:
                env = {runtime_entry.CHILD_FDS_ENV: f'{dup}:{dev}:{ino}'}
                with pytest.raises(runtime_entry.RuntimeEntryError, match='RUNTIME_HOLD'):
                    runtime_entry.adopt(runtime_home, env=env)
            finally:
                os.close(dup)


def test_start_intent_consistency_gate(runtime_home):
    """owner.activeとstart-intentの不整合は起動を拒否する(§3.2)。"""
    seed_v3(runtime_home)
    store = Store(runtime_entry.control_root(runtime_home))
    try:
        op = '12345678-1234-4234-8234-123456789abd'
        other_op = '99999999-9999-4999-8999-999999999999'
        nonce = 'e' * 64
        rollout = {'rollout_id': '12345678-1234-4234-8234-123456789abf',
                   'assignment_id': '12345678-1234-4234-8234-123456789ac0',
                   'policy_sha256': '3' * 64, 'cohort_sha256': '4' * 64,
                   'allocation_epoch': 1, 'authorization_epoch': 1,
                   'stage': 'self', 'wave_index': 1}

        def active(phase, operation_id=op):
            return {'operation_id': operation_id, 'phase': phase,
                    'target': {'release_id': H, 'code_seq': 1, 'source_commit': 'b' * 40,
                               'artifact_sha256': 'c' * 64, 'artifact_size': 1024},
                    'attempt_key': '1' * 64, 'prepare_nonce': nonce,
                    'prepare_deadline': None, 'private_witness': None, 'grant_raw': None,
                    'start_receipt_id': None, 'candidate_generation': None,
                    'startup_nonce': None, 'activation_receipt_id': None,
                    'runtime_proof': None, 'reserved_owner_bytes': 1,
                    'error_code': None, 'rollout': rollout}

        def intent(phase='open', **over):
            value = {'schema_version': 'mcs.start-intent.v3', 'operation_id': op,
                     'grant_id': '12345678-1234-4234-8234-123456789abe',
                     'grant_sha256': '5' * 64, 'attempt_key': '1' * 64,
                     'prepare_nonce': nonce, 'root_fingerprint': store.root_fingerprint(),
                     'source_before': 'a' * 40, 'source_target': 'b' * 40,
                     'created_at': 1000, 'phase': phase,
                     'terminal_outcome_sha256': None}
            value.update(over)
            return value

        owner = owner_value(store)

        # intent必須phaseのactiveがintentなし → 拒否(単独marker消失を許可とみなさない)
        owner['active'] = active('STARTED')
        store.write('release-owner.json', owner)
        with pytest.raises(runtime_entry.RuntimeEntryError, match='FENCE_INCONSISTENT'):
            runtime_entry.acquire(runtime_home, wait_s=0)

        # open intentがlive activeと不一致 → 拒否
        store.write('release-start-intent.json', intent())
        owner['active'] = active('STARTED', operation_id=other_op)
        store.write('release-owner.json', owner)
        with pytest.raises(runtime_entry.RuntimeEntryError, match='FENCE_INCONSISTENT'):
            runtime_entry.acquire(runtime_home, wait_s=0)

        # open intent + active=None(orphaned fence) → 拒否
        owner['active'] = None
        store.write('release-owner.json', owner)
        with pytest.raises(runtime_entry.RuntimeEntryError, match='FENCE_INCONSISTENT'):
            runtime_entry.acquire(runtime_home, wait_s=0)

        # 正しいopen intent + 一致active → 許可(Q SHは保持される)
        owner['active'] = active('STARTED')
        store.write('release-owner.json', owner)
        entry = runtime_entry.acquire(runtime_home, wait_s=0)
        entry.close()

        # retired intentは終端証拠hash必須。active=Noneなら許可
        store.write('release-start-intent.json',
                    intent('retired', terminal_outcome_sha256='2' * 64))
        owner['active'] = None
        store.write('release-owner.json', owner)
        entry = runtime_entry.acquire(runtime_home, wait_s=0)
        entry.close()

        # retired intent + 終端hashなし → 拒否
        store.write('release-start-intent.json', intent('retired'))
        with pytest.raises(runtime_entry.RuntimeEntryError, match='FENCE_INCONSISTENT'):
            runtime_entry.acquire(runtime_home, wait_s=0)

        # PREPARED(active)はintent不要 → intent削除しても許可
        os.unlink('release-start-intent.json', dir_fd=store.fd)
        owner['active'] = active('PREPARED')
        store.write('release-owner.json', owner)
        entry = runtime_entry.acquire(runtime_home, wait_s=0)
        entry.close()
    finally:
        store.close()
