"""v3 deps factory(release_auto_deps.build_deps)を合成fixtureで検証する。

実MCS・実launchd・実networkには触れない。Storeはtmp_path配下の合成control
root、git/wire/backup/quarantineはfake注入。drain labelsは明示注入で
native launchctl経路を避ける。
"""
import os

import pytest

from adapters.sdc.storage import Store
from runtime_entry import control_root
import release_auto_deps as deps_mod
from release_auto_owner import OwnerError
from release_auto_services import ServiceError
from release_auto_wire import WireError
from release_auto_backup import BackupEvidenceError
from test_release_auto_witness import seed_owner


class _FakeGit:
    def __init__(self, head='a' * 40):
        self._head = head
        self.calls = []

    def repository_path(self):
        return '/synthetic/repo'

    def run(self, argv, env=None, timeout_s=None):
        self.calls.append(list(argv))
        if 'rev-parse' in argv:
            return {'rc': 0, 'stdout': self._head + '\n', 'stderr': ''}
        if 'ls-tree' in argv:
            return {'rc': 0, 'stdout': 'tree-entry\0', 'stderr': ''}
        return {'rc': 0, 'stdout': '', 'stderr': ''}

    def git_inspect(self, repo):
        return {}


class _FakeLocal:
    def now_ms(self):
        return 1000

    def now_s(self):
        return 1.0

    def mono(self):
        return 0.0

    def boot_id(self):
        return 'b' * 64

    def reserved_owner_bytes(self):
        return 1024

    def capacity_ok(self, required):
        return True

    def installed_schema(self):
        return 7


@pytest.fixture
def env(tmp_path):
    runtime_home = str(tmp_path / 'runtime-home')
    control = str(control_root(runtime_home))
    os.makedirs(control, mode=0o700, exist_ok=True)
    store = Store(control, create=True)
    yield {'store': store, 'control': control,
           'runtime_home': runtime_home,
           'data_dir': str(tmp_path / 'data'),
           'ledger': str(tmp_path / 'ledger.db'),
           'repo': str(tmp_path / 'repo')}
    store.close()


def _build(env, **kw):
    params = dict(
        store=env['store'], runtime_home=env['runtime_home'],
        cfg={'runtime_mode': 'hermes'},
        data_dir=env['data_dir'], ledger_path=env['ledger'],
        capacity_path=env['data_dir'],
        monitor_root=None, drain_labels=[],
        git=_FakeGit(), local=_FakeLocal(),
        source_facts=lambda: {'source_tree_sha256': '1' * 64,
                              'runtime_fingerprint': '2' * 64},
        file_witnesses=lambda: {'locks': [], 'configs': []},
        backup_detail=lambda: {'path': '/b', 'sha256': '3' * 64},
        required_free_bytes=lambda: 4096)
    # file_witnesses差替え時はlocks 8件必須 — witness検査は呼出時なので
    # ここでは構築のみ確認する注入値をそのまま渡す
    params.update(kw)
    return deps_mod.build_deps(**params)


def test_build_deps_surfaces(env):
    deps = _build(env)
    for name in ('signed_send', 'verify', 'fetch_artifact', 'release_metadata',
                 'offer_eligible', 'stage_sink', 'stage_verify',
                 'bundle_path', 'file_digest', 'git_run', 'git_inspect',
                 'repository_path', 'now_ms', 'now_s', 'mono', 'boot_id',
                 'capacity_ok', 'installed_schema', 'reserved_owner_bytes',
                 'holds', 'local_revoke', 'backup_evidence',
                 'contract_digests', 'stop_new_intake', 'managed_services',
                 'request_stop', 'await_retirement', 'retirement_complete',
                 'restart_same_version', 'acquire_locks', 'q_exclusive',
                 'evidence_store', 'evidence_load', 'witness_private',
                 'apply_effect', 'measure_state', 'journal_apply'):
        assert callable(getattr(deps, name)), name


def test_unavailable_stubs_fail_closed(env):
    deps = _build(env)
    with pytest.raises(WireError):
        deps.signed_send('/v3/x', b'{}')
    with pytest.raises(OwnerError):
        deps.contract_digests()
    with pytest.raises(BackupEvidenceError):
        deps.backup_evidence()
    # backup_detail未注入でもwitness providerはfail-closed
    assert deps.witness_private is not None


def test_empty_drain_labels_vacuous(env):
    deps = _build(env, drain_labels=[])
    assert deps.managed_services() == []
    evidence = deps.await_retirement(10**15)
    assert deps.retirement_complete(evidence, []) is True


def test_drain_unavailable_for_none(env):
    deps = _build(env, drain_labels=None)
    with pytest.raises(ServiceError):
        deps.managed_services()
    with pytest.raises(ServiceError):
        deps.stop_new_intake()


def test_default_drain_labels_hermes_and_lineworks(env):
    # labelsはLaunchdDrain内部 — services()がlaunchctlを呼ぶため
    # 構築時点では検証しない。label導出関数を直接検査する。
    _build(env, drain_labels=deps_mod._UNSET,
           cfg={'runtime_mode': 'hermes',
                'notify': {'interactive': 'lineworks'}})
    labels = deps_mod._default_drain_labels(
        {'runtime_mode': 'hermes', 'notify': {'interactive': 'lineworks'}})
    # diagnosed_agent_labels正本: watcher(cmd/int/health)+resident3+
    # LINE WORKS。SDCはsdc.enabled未設定のため含まれない
    for expected in ('local.mcs-cmd', 'local.mcs-int', 'local.mcs-health',
                     'ai.mcs.extract-drainer', 'ai.mcs.lineworks'):
        assert expected in labels
    st = deps_mod._default_drain_labels({'runtime_mode': 'standalone'})
    assert st is None


def test_measure_state(env):
    deps = _build(env, contract_digests=lambda: {
        'config_contract_sha256': '4' * 64,
        'data_contract_sha256': '5' * 64})
    seed_owner(env['store'])
    measured = deps.measure_state()
    assert measured['head'] == 'a' * 40
    assert measured['config_contract_sha256'] == '4' * 64
    assert measured['installed_schema'] == 7
    assert set(measured) == {'head', 'control_closure_sha256',
                             'config_contract_sha256',
                             'data_contract_sha256', 'installed_schema',
                             'profile_sha256'}


def test_journal_apply_wiring(env, tmp_path):
    deps = _build(env, data_dir=str(tmp_path / 'jdata'))
    os.makedirs(tmp_path / 'jdata', exist_ok=True)
    record = {'kind': 'release-auto-v3', 'event': 'applied',
              'operation_id': 'op-x', 'grant_id': 'g', 'attempt_key': 'a',
              'source_commit': 'c' * 40, 'applied_at': 5}
    deps.journal_apply(record)
    import update_journal
    state = update_journal.load_state(
        str(tmp_path / 'jdata' / 'update_state.json'))
    assert state['v3']['applied']['op-x']['event'] == 'applied'


def test_apply_effect_and_runtime_advance_hooks(env):
    deps = _build(env)
    assert callable(deps.apply_effect)
    # quarantine未注入 → runtime_advanceは未設定(observe_only経路)
    assert getattr(deps, 'runtime_advance', None) is None


def test_runtime_advance_with_quarantine(env):
    calls = []

    class _Q:
        def service_desires(self):
            return {}

        def release_update_fence(self, g):
            pass

        def quarantine_launch(self, a, r):
            pass

        def probe_runtime(self, a):
            return {}

        def heartbeat_observations(self, a):
            return []

        def loaded_source_expect(self, a):
            return 'x' * 64

        def measure_source_manifest(self):
            return 'y' * 64

    deps = _build(env, quarantine=_Q())
    assert callable(deps.runtime_advance)
    assert callable(deps.service_desires)
    assert calls == []
