"""Use synthetic enrollment files and stubs without services or credentials."""
import sys

import pytest

import sdc_cli
from adapters.sdc.storage import Store
import test_sdc_customer_cli as customer_cli

REAL_SERVICE = sdc_cli.service
REAL_OWNER_CONFIG = sdc_cli.lifecycle_io.read_owner_config
REAL_ATOMIC_WRITE = sdc_cli.atomic_write


@pytest.fixture
def customer_world(tmp_path, monkeypatch):
    world = customer_cli.world.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(__name__ + '.Store', sdc_cli.Store)
    return world


@pytest.mark.parametrize('answer,expected_jobs', [('2', 0), ('', 0), ('1', 1)])
def test_pending_consent_is_saved_before_job_install(customer_world, monkeypatch, answer, expected_jobs):
    home, _, _, _, jobs = customer_world
    root = home / '.mcs-security-customer'
    customer_cli.seed(root)
    original_transport, original_service = sdc_cli.Transport, sdc_cli.service
    class Pending(original_transport):
        def send(self, path, raw, headers=None):
            result = super().send(path, raw, headers)
            if path.endswith('/status'):
                result.update(state='claimed', pending_expires_at=4_000_000_000_000)
            return result
    def service(enabled, selected, **kwargs):
        with Store(selected) as store:
            assert store.read('enrollment-consent.json')['identity']['alias'] == 'Synthetic device'
            assert not store.read('state.json')['enabled']
        original_service(enabled, selected, **kwargs)
    monkeypatch.setattr(sdc_cli, 'Transport', Pending)
    monkeypatch.setattr(sdc_cli, 'service', service)
    monkeypatch.setattr('builtins.input', lambda _: answer)
    assert sdc_cli.main(['connect', '--customer', *customer_cli.FLAGS]) == 0
    assert len(jobs) == expected_jobs
    with Store(root) as store:
        assert bool(store.read('enrollment-consent.json', {})) == bool(expected_jobs)
        assert not store.read('state.json')['enabled']


def test_job_install_failure_preserves_receipt_and_registration(customer_world, monkeypatch):
    home, cfg, _, _, _ = customer_world
    root = home / '.mcs-security-customer'
    customer_cli.seed(root)
    original = sdc_cli.Transport
    class Pending(original):
        def send(self, path, raw, headers=None):
            result = super().send(path, raw, headers)
            if path.endswith('/status'):
                result.update(state='claimed', pending_expires_at=4_000_000_000_000)
            return result
    def fail(*args, **kwargs):
        raise OSError('SYNTHETIC_JOB_FAILURE')
    monkeypatch.setattr(sdc_cli, 'Transport', Pending)
    monkeypatch.setattr(sdc_cli, 'service', fail)
    before = cfg.copy()
    assert sdc_cli.main(['connect', '--customer', *customer_cli.FLAGS]) == 1
    assert cfg == before
    with Store(root) as store:
        assert store.read('enrollment-consent.json')
        assert store.read('state.json')['state'] == 'pending'
        assert store.read('credential')['key_id']


def test_pending_tick_uses_single_poll_and_never_facts_or_inventory(tmp_path, monkeypatch):
    root = tmp_path / 'private'
    with Store(root, create=True) as store:
        store.write('endpoint.json', {'origin': 'https://ingest.invalid', 'audience': 'synthetic', 'environment': 'synthetic'})
        store.write('state.json', {'state': 'pending', 'enabled': False})
    cfg = {'sdc': {'enabled': True, 'state_root': str(root)}}
    calls = []
    monkeypatch.setattr(sdc_cli.mcs_util, 'load_config', lambda: cfg)
    monkeypatch.setattr(sdc_cli.lifecycle_io, 'read_owner_config', lambda _: cfg)
    monkeypatch.setattr(sdc_cli.mcs_runtime, 'python_executable', lambda _: sys.executable)
    monkeypatch.setattr(sdc_cli, 'facts', lambda: pytest.fail('pending must not observe'))
    monkeypatch.setattr(sdc_cli, 'Transport', lambda _: object())
    class Connector:
        def __init__(self, *args, **kwargs):
            pass
        def poll_enrollment(self):
            calls.append('poll')
        def tick(self, *args):
            pytest.fail('pending heartbeat')
    monkeypatch.setattr(sdc_cli, 'Connector', Connector)
    monkeypatch.setattr(sdc_cli, 'InventorySender', lambda *a: pytest.fail('pending inventory'))
    assert sdc_cli.tick(root) == 0
    assert calls == ['poll']
    cfg['sdc']['enabled'] = False
    assert sdc_cli.tick(root) == 0
    assert calls == ['poll']


def _stub_job_manager(monkeypatch, home, cfg, *, reconcile):
    from types import SimpleNamespace
    writes, files = [], []
    monkeypatch.setattr(sdc_cli.mcs_util, 'CONF_PATH', str(home / 'owner.json'))
    monkeypatch.setattr(sdc_cli.sys, 'platform', 'darwin')
    monkeypatch.setattr(sdc_cli.lifecycle_io, 'read_owner_config', lambda _: cfg)
    monkeypatch.setattr(sdc_cli, 'atomic_write', lambda *args, **kwargs: writes.append('config'))
    monkeypatch.setattr(sdc_cli.service_management, 'default_paths', lambda **kwargs: SimpleNamespace(agents_dir=home / 'agents'))
    monkeypatch.setattr(sdc_cli.service_management, 'service_substitutions', lambda *args, **kwargs: {})
    monkeypatch.setattr(sdc_cli.service_management, 'desired_agents', lambda *args, **kwargs: {sdc_cli.service_contract.SDC_LABEL: 'synthetic-plist'})
    monkeypatch.setattr(sdc_cli.service_management, 'write_service_file', lambda *args: files.append(args))
    monkeypatch.setattr(sdc_cli.service_management, 'agent_reconcile', reconcile)
    return writes, files


def test_explicit_reconnect_repairs_failed_job_without_rewriting_config(customer_world, monkeypatch):
    home, cfg, _, _, _ = customer_world
    root = home / '.mcs-security-customer'
    customer_cli.seed(root)
    original_transport = sdc_cli.Transport
    # The fixture replaces service; use the one source function shared with production.
    monkeypatch.setattr(sdc_cli, 'service', REAL_SERVICE)
    class Pending(original_transport):
        def send(self, path, raw, headers=None):
            if path.endswith('/status'):
                with Store(root) as store:
                    self.key = store.read('credential')['key']
                    self.claim_id = store.read('state.json')['claim_id']
            result = super().send(path, raw, headers)
            if path.endswith('/status'):
                result.update(state='claimed', pending_expires_at=4_000_000_000_000)
            return result
    monkeypatch.setattr(sdc_cli, 'Transport', Pending)
    attempts = []
    def reconcile(*args, **kwargs):
        attempts.append(args)
        return len(attempts) > 1
    writes, files = _stub_job_manager(monkeypatch, home, cfg, reconcile=reconcile)
    assert sdc_cli.main(['connect', '--customer', *customer_cli.FLAGS]) == 1
    with Store(root) as store:
        receipt = store.read('enrollment-consent.json')
        identity = store.read('state.json')['claim_id'], store.read('credential')['key']
    import copy
    saved = copy.deepcopy(cfg)
    assert sdc_cli.main(['connect', '--customer', *customer_cli.FLAGS]) == 0
    assert cfg == saved and writes == ['config'] and len(files) == 2
    with Store(root) as store:
        assert store.read('enrollment-consent.json') == receipt
        assert identity == (store.read('state.json')['claim_id'], store.read('credential')['key'])


@pytest.mark.parametrize('change', ['disabled', 'other_root'])
def test_reconcile_only_rejects_changed_owner_before_any_service_operation(tmp_path, monkeypatch, change):
    root = tmp_path / 'private'
    cfg = {'sdc': {'enabled': change != 'disabled', 'state_root': str(root if change == 'disabled' else tmp_path / 'other')}}
    writes, files = _stub_job_manager(monkeypatch, tmp_path, cfg, reconcile=lambda *a, **k: pytest.fail('reconcile disallowed'))
    with pytest.raises(ValueError, match='SDC_LOCAL_START_NOT_PERMITTED'):
        sdc_cli.service(True, root, reconcile_only=True)
    assert writes == files == []


def test_stop_during_reconcile_never_reenables_config_or_pending_tick(tmp_path, monkeypatch):
    root = tmp_path / 'private'
    with Store(root, create=True) as store:
        store.write('endpoint.json', customer_cli.CONFIG)
        store.write('state.json', {'state': 'pending', 'enabled': False})
    cfg = {'sdc': {'enabled': True, 'state_root': str(root)}}
    def reconcile(*args, **kwargs):
        cfg['sdc']['enabled'] = False
        return True
    writes, files = _stub_job_manager(monkeypatch, tmp_path, cfg, reconcile=reconcile)
    sdc_cli.service(True, root, reconcile_only=True)
    monkeypatch.setattr(sdc_cli.mcs_util, 'load_config', lambda: cfg)
    monkeypatch.setattr(sdc_cli, 'Transport', lambda _: pytest.fail('disabled network construction'))
    assert sdc_cli.tick(root) == 0
    assert cfg['sdc']['enabled'] is False and writes == [] and len(files) == 1


@pytest.mark.parametrize('change', ['disabled', 'other_root'])
def test_saved_receipt_cli_never_reinstalls_or_contacts_changed_owner(customer_world, monkeypatch, change):
    home, cfg, _, calls, jobs = customer_world
    root = home / '.mcs-security-customer'
    customer_cli.seed(root)
    original = sdc_cli.Transport
    class Pending(original):
        def send(self, path, raw, headers=None):
            result = super().send(path, raw, headers)
            if path.endswith('/status'):
                result.update(state='claimed', pending_expires_at=4_000_000_000_000)
            return result
    monkeypatch.setattr(sdc_cli, 'Transport', Pending)
    monkeypatch.setattr(sdc_cli.lifecycle_io, 'read_owner_config', lambda _: cfg)
    assert sdc_cli.main(['connect', '--customer', *customer_cli.FLAGS]) == 0
    calls.clear()
    jobs.clear()
    if change == 'disabled':
        cfg['sdc']['enabled'] = False
    else:
        cfg['sdc']['state_root'] = str(home / 'other')
    assert sdc_cli.main(['connect', '--customer', *customer_cli.FLAGS]) == 1
    assert calls == jobs == []


@pytest.mark.parametrize('later', ['unchanged', 'same_value_stop', 'other_root'])
def test_first_config_write_failure_requires_unchanged_owner_and_explicit_retry(customer_world, monkeypatch, later):
    import json
    home, cfg, _, calls, _ = customer_world
    root = home / '.mcs-security-customer'
    customer_cli.seed(root)
    owner = home / 'owner.json'
    initial = {**cfg, 'sdc': {'enabled': False, 'state_root': str(root)}}
    owner.write_text(json.dumps(initial))
    owner.chmod(0o600)
    monkeypatch.setattr(sdc_cli.mcs_util, 'CONF_PATH', str(owner))
    monkeypatch.setattr(sdc_cli, 'service', REAL_SERVICE)
    _, files = _stub_job_manager(monkeypatch, home, cfg, reconcile=lambda *a, **k: True)
    monkeypatch.setattr(sdc_cli.lifecycle_io, 'read_owner_config', REAL_OWNER_CONFIG)
    attempts = []
    def atomic(path, writer, **kwargs):
        attempts.append(path)
        if len(attempts) == 1:
            raise OSError('SYNTHETIC_CONFIG_WRITE_FAILURE')
        return REAL_ATOMIC_WRITE(path, writer, **kwargs)
    monkeypatch.setattr(sdc_cli, 'atomic_write', atomic)
    original_transport = sdc_cli.Transport
    class Pending(original_transport):
        def send(self, path, raw, headers=None):
            if path.endswith('/status'):
                with Store(root) as store:
                    self.key = store.read('credential')['key']
                    self.claim_id = store.read('state.json')['claim_id']
            result = super().send(path, raw, headers)
            if path.endswith('/status'):
                result.update(state='claimed', pending_expires_at=4_000_000_000_000)
            return result
    monkeypatch.setattr(sdc_cli, 'Transport', Pending)
    assert sdc_cli.main(['connect', '--customer', *customer_cli.FLAGS, '--state-root', str(root)]) == 1
    with Store(root) as store:
        receipt = store.read('enrollment-consent.json')
        claim, key = store.read('state.json')['claim_id'], store.read('credential')['key']
    assert REAL_OWNER_CONFIG(str(owner)) == initial
    calls.clear()
    if later != 'unchanged':
        changed = {**initial, 'sdc': dict(initial['sdc'])}
        if later == 'other_root':
            changed['sdc']['state_root'] = str(home / 'other')
        REAL_ATOMIC_WRITE(owner, lambda stream: json.dump(changed, stream), mode=0o600)
    expected = 0 if later == 'unchanged' else 1
    assert sdc_cli.main(['connect', '--customer', *customer_cli.FLAGS, '--state-root', str(root)]) == expected
    if later == 'unchanged':
        assert REAL_OWNER_CONFIG(str(owner))['sdc'] == {'enabled': True, 'state_root': str(root)}
        assert len(attempts) == 2 and len(files) == 1
    else:
        assert len(attempts) == 1 and files == [] and calls == []
    with Store(root) as store:
        assert store.read('enrollment-consent.json') == receipt
        assert (store.read('state.json')['claim_id'], store.read('credential')['key']) == (claim, key)


@pytest.fixture
def owner_world(tmp_path, monkeypatch):
    import json
    root, owner = tmp_path / 'private', tmp_path / 'owner.json'
    with Store(root, create=True) as store:
        store.write('endpoint.json', customer_cli.CONFIG)
        store.write('state.json', {'state': 'pending', 'enabled': False})
    cfg = {'sdc': {'enabled': False, 'state_root': str(root)}}
    owner.write_text(json.dumps(cfg))
    owner.chmod(0o600)
    monkeypatch.setattr(sdc_cli.mcs_util, 'CONF_PATH', str(owner))
    _, files = _stub_job_manager(monkeypatch, tmp_path, cfg, reconcile=lambda *a, **k: True)
    monkeypatch.setattr(sdc_cli.lifecycle_io, 'read_owner_config', REAL_OWNER_CONFIG)
    monkeypatch.setattr(sdc_cli, 'atomic_write', REAL_ATOMIC_WRITE)
    monkeypatch.setattr(sdc_cli.service_management, 'retire_agents', lambda *a, **k: 0)
    monkeypatch.setattr(sdc_cli.mcs_util, 'load_config', lambda: REAL_OWNER_CONFIG(str(owner)))
    return root, owner, files


def test_sdc_stop_wins_before_initial_install_compare(owner_world):
    root, owner, files = owner_world
    with sdc_cli._sdc_owner_lock():
        expected = sdc_cli._owner_snapshot()
    # Even a same-value stop publishes a new config generation under the same lock.
    sdc_cli.service(False, root)
    with pytest.raises(ValueError, match='SDC_OWNER_CHANGED'):
        sdc_cli.service(True, root, expected_owner=expected)
    assert REAL_OWNER_CONFIG(str(owner))['sdc']['enabled'] is False
    assert files == []


def test_sdc_enable_wins_then_stop_is_serialized_and_next_tick_is_quiet(owner_world, monkeypatch):
    root, owner, _ = owner_world
    with sdc_cli._sdc_owner_lock():
        expected = sdc_cli._owner_snapshot()
    original = sdc_cli.atomic_write
    blocked = []
    def write(path, writer, **kwargs):
        # A competing SDC stop cannot write between the comparison and replace.
        with pytest.raises(BlockingIOError):
            sdc_cli.service(False, root)
        blocked.append(True)
        return original(path, writer, **kwargs)
    monkeypatch.setattr(sdc_cli, 'atomic_write', write)
    sdc_cli.service(True, root, expected_owner=expected)
    monkeypatch.setattr(sdc_cli, 'atomic_write', original)
    sdc_cli.service(False, root)
    assert blocked == [True] and REAL_OWNER_CONFIG(str(owner))['sdc']['enabled'] is False
    monkeypatch.setattr(sdc_cli, 'Transport', lambda _: pytest.fail('stopped pending tick'))
    assert sdc_cli.tick(root) == 0


@pytest.mark.parametrize('bad', ['symlink', 'public_mode', 'hardlink', 'nonempty'])
def test_owner_lock_refuses_nonprivate_or_aliased_files(owner_world, bad):
    root, owner, files = owner_world
    lock = owner.with_name(owner.name + '.sdc-owner.lock')
    if bad == 'symlink':
        lock.symlink_to(owner)
    else:
        lock.write_text('synthetic' if bad == 'nonempty' else '')
        lock.chmod(0o644 if bad == 'public_mode' else 0o600)
        if bad == 'hardlink':
            lock.with_name('alias').hardlink_to(lock)
    with pytest.raises((OSError, ValueError)):
        sdc_cli.service(True, root)
    assert REAL_OWNER_CONFIG(str(owner))['sdc']['enabled'] is False and files == []


@pytest.mark.parametrize('failure', ['owner_snapshot', 'intent_save', 'receipt_save'])
def test_prepared_install_cuts_leave_no_authorizing_receipt_and_can_retry(customer_world, monkeypatch, failure):
    home, cfg, _, calls, jobs = customer_world
    root = home / '.mcs-security-customer'
    customer_cli.seed(root)
    original_transport, original_write, original_snapshot = sdc_cli.Transport, Store.write, sdc_cli._owner_snapshot
    class Pending(original_transport):
        def send(self, path, raw, headers=None):
            if path.endswith('/status'):
                with Store(root) as store:
                    self.key = store.read('credential')['key']
                    self.claim_id = store.read('state.json')['claim_id']
            result = super().send(path, raw, headers)
            if path.endswith('/status'):
                result.update(state='claimed', pending_expires_at=4_000_000_000_000)
            return result
    monkeypatch.setattr(sdc_cli, 'Transport', Pending)
    fired = []
    def snapshot():
        if failure == 'owner_snapshot' and not fired:
            fired.append(True)
            raise OSError('SYNTHETIC_OWNER_READ_FAILURE')
        return original_snapshot()
    def write(store, name, value, **kwargs):
        cut = (failure == 'intent_save' and name == 'state.json' and 'enrollment_install' in value
               or failure == 'receipt_save' and name == 'enrollment-consent.json')
        if cut and not fired:
            fired.append(True)
            raise OSError('SYNTHETIC_RECORD_WRITE_FAILURE')
        return original_write(store, name, value, **kwargs)
    monkeypatch.setattr(sdc_cli, '_owner_snapshot', snapshot)
    monkeypatch.setattr(Store, 'write', write)
    assert sdc_cli.main(['connect', '--customer', *customer_cli.FLAGS]) == 1
    assert fired == [True] and jobs == [] and cfg['sdc'] == {}
    assert all(not path.endswith('/confirm') for path, *_ in calls)
    with Store(root) as store:
        assert store.read('enrollment-consent.json', {}) == {}
        identity = store.read('state.json')['claim_id'], store.read('credential')['key']
    assert sdc_cli.main(['connect', '--customer', *customer_cli.FLAGS]) == 0
    assert jobs == [(True, root)]
    with Store(root) as store:
        assert store.read('enrollment-consent.json')['accepted'] is True
        assert identity == (store.read('state.json')['claim_id'], store.read('credential')['key'])
