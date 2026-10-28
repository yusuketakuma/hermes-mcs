"""Route customer commands through temporary configuration and fake Keychain only."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import sdc_cli
import service_contract
from adapters.sdc.protocol import CUSTOMER_CONSENT, strict_json
from adapters.sdc.storage import Store
from test_sdc_customer import REASON, Owner

CONFIG = {'origin': 'https://ingest.invalid', 'audience': 'sdc:synthetic', 'environment': 'synthetic'}
DEVICE = '11111111-1111-4111-8111-111111111111'
KEY_ID = '22222222-2222-4222-8222-222222222222'
NEXT_ID = '33333333-3333-4333-8333-333333333333'
CLAIM = '55555555-5555-4555-8555-555555555555'
KEY = 'a1' * 32
FLAGS = ['--confirm-human', '--reason', REASON]


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('ascii')


class Native:
    def __init__(self):
        self.items, self.calls, self.status = {}, [], 0

    def copy(self, account):
        self.calls.append(('copy', account))
        if self.status:
            return self.status, b''
        return (0, self.items[account]) if account in self.items else (-25300, b'')

    def add(self, account, raw):
        self.calls.append(('add', account))
        if account in self.items:
            return -25299
        self.items[account] = raw
        return 0

    def update(self, account, raw):
        self.calls.append(('update', account))
        self.items[account] = raw
        return 0

    def delete(self, account):
        self.calls.append(('delete', account))
        del self.items[account]
        return 0


@pytest.fixture
def world(tmp_path, monkeypatch):
    cfg = {'sdc': {}, 'runtime_mode': 'standalone'}
    monkeypatch.setattr(sdc_cli.mcs_util, 'CONF_PATH', str(tmp_path / 'config.json'))
    native, calls, jobs = Native(), [], []
    monkeypatch.setattr(Path, 'home', classmethod(lambda _: tmp_path))
    class SyntheticStore(Store):
        def __init__(self, root, **kwargs):
            kwargs.setdefault('keychain_factory', lambda account: Owner(account, native))
            super().__init__(root, **kwargs)
    factory = SyntheticStore
    monkeypatch.setattr(sdc_cli, 'Store', factory)
    monkeypatch.setattr(__name__ + '.Store', factory)
    monkeypatch.setattr(sdc_cli.mcs_util, 'load_config', lambda: cfg)
    monkeypatch.setattr(sdc_cli.getpass, 'getpass', lambda _: 'SYNTHETIC_CUSTOMER_TOKEN')
    monkeypatch.setattr('builtins.input', lambda _: '1')

    class Transport:
        def __init__(self, _):
            self.key, self.rotation = None, 'missing'

        def send(self, path, raw, headers=None):
            calls.append((path, strict_json(raw), headers))
            body = strict_json(raw)
            if path.endswith('/claim'):
                assert body['credential_storage'] == 'keychain'
                self.key = body['key']
                self.claim_id = body['claim_id']
                return {'claim_id': body['claim_id'], 'device_id': DEVICE, 'key_id': KEY_ID, 'audience': CONFIG['audience']}
            if path == '/v2/enrollments/status':
                return {'claim_id': self.claim_id, 'device_id': DEVICE, 'key_id': KEY_ID, 'audience': CONFIG['audience'], 'state': 'admin_approved', 'tenant_name': 'Synthetic customer', 'environment': CONFIG['environment'], 'alias': 'Synthetic device', 'data_scope': 'version-process-status-only', 'fingerprint': hashlib.sha256(bytes.fromhex(self.key)).hexdigest()[:16], 'consent_revision': CUSTOMER_CONSENT}
            if path.endswith('/confirm'):
                return {'claim_id': self.claim_id, 'device_id': DEVICE, 'key_id': KEY_ID, 'audience': CONFIG['audience'], 'state': 'active', 'tenant_name': 'Synthetic customer', 'environment': CONFIG['environment'], 'alias': 'Synthetic device', 'data_scope': 'version-process-status-only', 'fingerprint': hashlib.sha256(bytes.fromhex(self.key)).hexdigest()[:16], 'consent_revision': CUSTOMER_CONSENT}
            if path == '/v2/device/suspend':
                assert jobs and jobs[-1][0] is False
                return {'code': 'suspended'}
            if path.endswith('/prepare'):
                self.rotation = 'prepared'
            if path.endswith('/commit'):
                self.rotation = 'active'
            return {'state': self.rotation, 'key_id': None if self.rotation == 'missing' else NEXT_ID}

    def service(enabled, root, **kwargs):
        jobs.append((enabled, Path(root)))
        cfg['sdc'] = {**cfg['sdc'], 'enabled': enabled, 'state_root': str(root)}

    monkeypatch.setattr(sdc_cli, 'Transport', Transport)
    monkeypatch.setattr(sdc_cli, 'service', service)
    return tmp_path, cfg, native, calls, jobs


def seed(root, *, customer=False, active=False, native=None):
    with Store(root, create=True) as store:
        store.write('endpoint.json', CONFIG)
        if customer:
            store.enable_customer()
        if active and customer:
            # Seed the fake native item directly; fixtures never skip the mutation boundary.
            native.items[store.read('credential-store.json')['account']] = canonical({'key': KEY, 'key_id': KEY_ID})
        elif active:
            store.write('credential', {'key': KEY, 'key_id': KEY_ID})
        if active:
            store.write('state.json', {'state': 'active', 'enabled': True, 'sequence': 0, 'device_id': DEVICE, 'key_id': KEY_ID, **({'credential_storage': 'keychain', 'claim_id': CLAIM} if customer else {})})


def test_customer_connect_uses_a_separate_default_root_and_preserves_self(world, capsys):
    home, cfg, native, calls, jobs = world
    self_root, customer_root = home / '.mcs-security', home / '.mcs-security-customer'
    seed(self_root, active=True)
    seed(customer_root)
    before = {p.name: p.read_bytes() for p in self_root.iterdir()}
    cfg['sdc'] = {'enabled': True, 'state_root': str(self_root)}
    assert sdc_cli.main(['connect', '--customer', *FLAGS]) == 0
    assert jobs == [(True, customer_root)]
    assert cfg['sdc']['state_root'] == str(customer_root)
    assert {p.name: p.read_bytes() for p in self_root.iterdir()} == before
    assert calls[0][1]['credential_storage'] == 'keychain'
    assert native.items and not (customer_root / 'credential').exists()
    text = capsys.readouterr()
    assert '現在の監視先への heartbeat 送信は停止します' in text.out
    assert 'SYNTHETIC_CUSTOMER_TOKEN' not in text.out + text.err
    assert str(customer_root) not in text.out + text.err


def test_pending_customer_keeps_the_customer_option_in_the_next_step(world, monkeypatch, capsys):
    home, cfg, _, _, jobs = world
    root = home / '.mcs-security-customer'
    seed(root)
    original = sdc_cli.Transport

    class Pending(original):
        def send(self, path, raw, headers=None):
            result = super().send(path, raw, headers)
            if path == '/v2/enrollments/status':
                result['state'] = 'claimed'
            return result

    monkeypatch.setattr(sdc_cli, 'Transport', Pending)
    assert sdc_cli.main(['connect', '--customer', *FLAGS]) == 0
    assert jobs == [] and cfg['sdc'] == {}
    text = capsys.readouterr()
    assert 'mcs connect --customer' in text.out
    assert str(root) not in text.out + text.err


@pytest.mark.parametrize('command', ['connection', 'disconnect', 'rotate-key'])
def test_existing_commands_follow_current_configured_customer_root(world, command):
    home, cfg, native, calls, jobs = world
    root = home / 'selected-customer'
    seed(root, customer=True, active=True, native=native)
    cfg['sdc']['state_root'] = str(root)
    assert sdc_cli.main([command, *([] if command == 'connection' else FLAGS)]) == 0
    assert not (home / '.mcs-security').exists()
    if command == 'connection':
        assert calls == [] and jobs == []
    elif command == 'disconnect':
        assert jobs == [(False, root)]
        assert native.items == {}
        with Store(root) as store:
            assert store.read('state.json')['enabled'] is False
            assert store.customer
    else:
        with Store(root) as store:
            assert store.read('credential')['key_id'] == NEXT_ID
        assert native.items and not (root / 'credential').exists()


def test_explicit_state_root_overrides_config_without_implicit_migration(world):
    home, cfg, native, calls, jobs = world
    selected, explicit = home / 'customer', home / 'explicit-self'
    seed(selected, customer=True, active=True, native=native)
    seed(explicit)
    cfg['sdc']['state_root'] = str(selected)
    assert sdc_cli.main(['connection', '--state-root', str(explicit)]) == 0
    assert not (explicit / 'credential-store.json').exists()
    assert calls == [] and jobs == []


def test_customer_flag_refuses_self_root_without_changing_credentials(world, capsys):
    home, _, _, calls, jobs = world
    root = home / 'self'
    seed(root, active=True)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    assert sdc_cli.main(['connect', '--customer', '--state-root', str(root)]) == 1
    assert calls == [] and jobs == []
    assert not (root / 'credential-store.json').exists()
    assert all((root / name).read_bytes() == raw for name, raw in before.items())
    text = capsys.readouterr()
    assert KEY not in text.out + text.err and str(root) not in text.out + text.err


def test_locked_customer_reconnect_makes_no_request_or_keychain_write(world, capsys):
    home, cfg, native, calls, jobs = world
    root = home / 'customer'
    seed(root, customer=True, active=True, native=native)
    cfg['sdc']['state_root'] = str(root)
    before = (root / 'state.json').read_bytes()
    writes = [x for x in native.calls if x[0] in ('add', 'update')]
    native.status = -25308
    assert sdc_cli.main(['connect']) == 1
    assert calls == [] and jobs == []
    assert [x for x in native.calls if x[0] in ('add', 'update')] == writes
    assert (root / 'state.json').read_bytes() == before
    assert not (root / 'credential').exists()
    text = capsys.readouterr()
    assert KEY not in text.out + text.err


@pytest.mark.parametrize('error, message', [
    (sdc_cli.config_edit.ConfigConflict('config_busy'), '中央の登録状態'),
    (sdc_cli.config_edit.ConfigConflict('SDC_OWNER_CHANGED'), '再試行だけでは復旧できません'),
    (sdc_cli.SDCConfigWriteUnknown('SDC_CONFIG_WRITE_UNKNOWN'), '保存結果と監視ジョブの状態'),
])
def test_disconnect_config_conflict_reports_stop_unconfirmed(world, monkeypatch, capsys, error, message):
    home, cfg, native, calls, jobs = world
    root = home / 'customer'
    seed(root, customer=True, active=True, native=native)
    cfg['sdc'] = {'enabled': True, 'state_root': str(root)}
    before = (root / 'state.json').read_bytes()
    def conflict(*args, **kwargs):
        raise error
    monkeypatch.setattr(sdc_cli, 'service', conflict)
    assert sdc_cli.main(['disconnect']) == 1
    assert (root / 'state.json').read_bytes() == before
    assert calls == [] and jobs == []
    text = capsys.readouterr().err
    assert '停止していない可能性' in text and message in text


@pytest.mark.parametrize('command', ['connection', 'disconnect', 'rotate-key'])
def test_customer_flag_is_only_an_explicit_new_enrollment_option(world, command):
    _, _, native, calls, jobs = world
    with pytest.raises(SystemExit) as caught:
        sdc_cli.main([command, '--customer'])
    assert caught.value.code == 2
    assert native.calls == [] and calls == [] and jobs == []


def test_real_service_owner_persists_customer_root_and_keeps_other_sdc_settings(tmp_path, monkeypatch):
    cfg = {'sdc': {'retained_setting': 'synthetic'}}
    saved, reconciled = [], []
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(sdc_cli.mcs_util, 'CONF_PATH', str(path))
    monkeypatch.setattr(sdc_cli.sys, 'platform', 'darwin')
    monkeypatch.setattr(sdc_cli.lifecycle_io, 'read_owner_config', lambda _: cfg)
    def save(path, writer, **kwargs):
        stream = io.StringIO()
        writer(stream)
        saved.append(json.loads(stream.getvalue()))
    monkeypatch.setattr(sdc_cli, 'atomic_write', save)
    monkeypatch.setattr(sdc_cli.service_management, 'default_paths', lambda **_: SimpleNamespace(agents_dir=tmp_path / 'agents'))
    monkeypatch.setattr(sdc_cli.service_management, 'service_substitutions', lambda config, **_: {'SDC_ROOT': config['sdc']['state_root']})
    monkeypatch.setattr(sdc_cli.service_management, 'desired_agents', lambda values, *a, **kw: {service_contract.SDC_LABEL: values['SDC_ROOT']})
    monkeypatch.setattr(sdc_cli.service_management, 'write_service_file', lambda dst, body: reconciled.append((dst, body)))
    monkeypatch.setattr(sdc_cli.service_management, 'agent_reconcile', lambda *a, **kw: True)
    root = tmp_path / 'customer'
    sdc_cli.service(True, root, note=lambda _: None)
    assert cfg['sdc'] == {'retained_setting': 'synthetic'}
    assert saved[0]['sdc'] == {'retained_setting': 'synthetic', 'enabled': True, 'state_root': str(root)}
    assert reconciled[0][1] == str(root)


@pytest.mark.parametrize('raw', [b'[]', b'["SYNTHETIC_PRIVATE"]', b'1', b'true', b'null'])
@pytest.mark.parametrize('name', ['state.json', 'credential'])
@pytest.mark.parametrize('command', ['connect', 'disconnect', 'rotate-key'])
def test_self_malformed_state_or_credential_prevents_every_service_and_network_action(world, capsys, raw, name, command):
    home, cfg, native, calls, jobs = world
    root = home / 'self-corrupted'
    seed(root, active=True)
    cfg['sdc']['state_root'] = str(root)
    (root / name).write_bytes(raw)
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    assert sdc_cli.main([command]) == 1
    assert calls == [] and jobs == [] and native.calls == []
    assert all((root / filename).read_bytes() == content for filename, content in before.items())
    text = capsys.readouterr()
    assert text.out == ''
    assert '接続操作を完了できませんでした' in text.err
    assert 'SYNTHETIC_PRIVATE' not in text.err and str(root) not in text.err and KEY not in text.err


@pytest.mark.parametrize('raw', [b'[]', b'["SYNTHETIC_PRIVATE"]', b'1', b'true', b'null'])
@pytest.mark.parametrize('customer', [False, True])
def test_connection_rejects_nonobject_state_without_reading_credentials_or_changing_service(world, capsys, raw, customer):
    home, cfg, native, calls, jobs = world
    root = home / 'corrupted-state'
    seed(root, customer=customer, active=True, native=native)
    cfg['sdc']['state_root'] = str(root)
    native.calls.clear()
    (root / 'state.json').write_bytes(raw)
    assert sdc_cli.main(['connection']) == 1
    assert (root / 'state.json').read_bytes() == raw
    assert calls == [] and jobs == [] and native.calls == []
    assert 'SYNTHETIC_PRIVATE' not in capsys.readouterr().err


@pytest.mark.parametrize('raw', [b'[]', b'["SYNTHETIC_PRIVATE"]', b'1', b'true', b'null'])
def test_connection_remains_a_state_only_read_when_self_credential_is_malformed(world, capsys, raw):
    home, cfg, native, calls, jobs = world
    root = home / 'self-credential-corrupted'
    seed(root, active=True)
    cfg['sdc']['state_root'] = str(root)
    (root / 'credential').write_bytes(raw)
    before = (root / 'state.json').read_bytes()
    assert sdc_cli.main(['connection']) == 0
    assert (root / 'credential').read_bytes() == raw
    assert (root / 'state.json').read_bytes() == before
    assert calls == [] and jobs == [] and native.calls == []
    assert 'SYNTHETIC_PRIVATE' not in capsys.readouterr().out


def test_locked_keychain_still_allows_local_disconnect_without_plaintext_fallback(world, monkeypatch, capsys):
    home, cfg, native, calls, jobs = world
    root = home / 'locked-customer'
    seed(root, customer=True, active=True, native=native)
    cfg['sdc']['state_root'] = str(root)
    native.status = -25308
    original_items = dict(native.items)
    original_copy = native.copy
    def locked_copy(account):
        assert jobs == [(False, root)]
        assert strict_json((root / 'state.json').read_bytes())['enabled'] is False
        return original_copy(account)
    monkeypatch.setattr(native, 'copy', locked_copy)
    monkeypatch.setattr('builtins.input', lambda _: '2')
    assert sdc_cli.main(['disconnect']) == 0
    assert jobs == [(False, root)] and calls == []
    with Store(root) as store:
        state = store.read('state.json')
    assert state['enabled'] is False and state['disconnected'] is True
    assert state['central_suspended'] is False
    assert native.items == original_items
    assert not (root / 'credential').exists()
    text = capsys.readouterr()
    assert '未確認' in text.out
    assert KEY not in text.out + text.err


@pytest.mark.parametrize('command', ['connect', 'connection', 'disconnect', 'rotate-key'])
def test_copied_customer_marker_is_rejected_before_service_network_or_keychain(world, capsys, command):
    home, cfg, native, calls, jobs = world
    source, copied = home / 'source-customer', home / 'copied-customer'
    seed(source, customer=True, active=True, native=native)
    seed(copied)
    marker = (source / 'credential-store.json').read_bytes()
    copied_marker = copied / 'credential-store.json'
    copied_marker.write_bytes(marker)
    copied_marker.chmod(0o600)
    items = dict(native.items)
    native.calls.clear()
    cfg['sdc']['state_root'] = str(copied)
    before = {path.name: path.read_bytes() for path in copied.iterdir()}
    assert sdc_cli.main([command]) == 1
    assert calls == [] and jobs == [] and native.calls == []
    assert native.items == items
    assert (source / 'credential-store.json').read_bytes() == marker
    assert all((copied / filename).read_bytes() == content for filename, content in before.items())
    text = capsys.readouterr()
    assert '接続操作を完了できませんでした' in text.err
    assert KEY not in text.out + text.err and str(copied) not in text.out + text.err
