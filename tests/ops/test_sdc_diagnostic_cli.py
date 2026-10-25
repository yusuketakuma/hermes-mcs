"""Synthetic diagnostic consent CLI with real private state and forbidden external effects."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import sdc_diagnostic_cli as cli
from adapters.sdc import diagnostic_state as local
from adapters.sdc.storage import Store
from adapters.sdc.support_contract import canonical
from test_sdc_diagnostic_sender import World as SenderWorld, managed_world  # noqa: F401

NOW = 1800000000000
HOUR = 3600000
DAY = 24 * HOUR
IDS = [f'00000000-0000-4000-8000-{n:012d}' for n in range(1, 8)]
CANARY = 'SYNTHETIC_PRIVATE_NEVER_PRINT'
CONSENT = ['--valid-for-hours', '24', '--confirm-human', '--reason', 'synthetic-consent']


def digest(value):
    return hashlib.sha256(canonical(value, max_bytes=16384).encode('ascii')).hexdigest()


def forbidden(*_args, **_kwargs):
    pytest.fail('unexpected credential, HTTP, provider, service or config effect')


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / CANARY
    with Store(root, create_new=True, keychain_factory=forbidden) as store:
        fingerprint = store.root_fingerprint()
    target = {'origin': 'https://support.invalid', 'audience': 'support:synthetic', 'environment': 'synthetic'}
    bundle = {'profile_id': 'support', **target, 'root_id': IDS[0], 'tenant': 'synthetic',
              'installation': 'synthetic-install', 'device_id': IDS[1], 'claim_id': IDS[2],
              'realm_epoch': 'realm-1', 'device_epoch': 1}
    binding = {'root_fingerprint': fingerprint, 'monitor_endpoint_hash': 'a' * 64,
               'support_endpoint_hash': digest(target), 'support_bundle_hash': digest(bundle),
               **{k: bundle[k] for k in ('root_id', 'tenant', 'installation', 'device_id', 'claim_id',
                                        'realm_epoch', 'device_epoch', 'audience', 'environment')},
               'key_id': IDS[3]}
    snapshot = {'bundle': bundle, 'bundle_hash': digest(bundle), 'root_fingerprint': fingerprint,
                'monitor_endpoint_hash': 'a' * 64, 'local_receipt_id': IDS[4]}
    result = SimpleNamespace(root=root, binding=binding, snapshot=snapshot, now=NOW,
                             opened=[], calls=[], tick=0, diagnostic=0)

    def open_store(path):
        assert path == str(root)
        value = Store(path, keychain_factory=forbidden)
        result.opened.append(value)
        return value

    original_tick, original_diagnostic = Store.lock, Store.diagnostic_state_lock

    @contextmanager
    def tick(store):
        result.calls.append('tick')
        with original_tick(store):
            result.tick += 1
            try:
                yield
            finally:
                result.tick -= 1

    @contextmanager
    def diagnostic(store):
        result.calls.append('diagnostic')
        with original_diagnostic(store):
            result.diagnostic += 1
            try:
                yield
            finally:
                result.diagnostic -= 1

    def current(store, *, now_ms):
        assert store.root == root and now_ms == result.now
        assert result.diagnostic == 0
        result.calls.append(('current', result.tick))
        return copy.deepcopy(result.binding)

    def profile(store, *, profile, now_ms):
        assert store.root == root and profile == 'support' and now_ms == result.now
        assert result.tick == result.diagnostic == 0
        result.calls.append('profile')
        return copy.deepcopy(result.snapshot)

    original_read = Store._read_file

    def metadata(store, name, *args, **kwargs):
        assert name not in ('credential', 'credential-store.json')
        return original_read(store, name, *args, **kwargs)

    monkeypatch.setattr(cli, 'Store', open_store)
    monkeypatch.setattr(Store, 'lock', tick)
    monkeypatch.setattr(Store, 'diagnostic_state_lock', diagnostic)
    monkeypatch.setattr(Store, '_read_file', metadata)
    monkeypatch.setattr(Store, '_keychain', forbidden)
    monkeypatch.setattr(cli.time, 'time', lambda: result.now / 1000)
    monkeypatch.setattr(cli.mcs_util, 'load_config', forbidden)
    monkeypatch.setattr(cli.diagnostic_sender, 'managed_binding', current)
    monkeypatch.setattr(cli.provision, 'profile_snapshot', profile)
    monkeypatch.setattr(cli.diagnostic_sender, 'run_managed_once', forbidden)
    monkeypatch.setattr(cli.diagnostic_sender, 'DiagnosticTransport', forbidden)
    monkeypatch.setattr(cli.diagnostic_sender.Connector, 'secret', forbidden)
    monkeypatch.setattr('builtins.input', lambda _: '1')
    # Explicit non-effects include code which is not otherwise imported by the CLI.
    import sdc_cli
    import sdc_diagnostic_events
    import sdc_support_signatures
    monkeypatch.setattr(sdc_cli, 'service', forbidden)
    monkeypatch.setattr(sdc_diagnostic_events, 'collect_events', forbidden)
    monkeypatch.setattr(sdc_support_signatures, '_verify_pss', forbidden)
    yield result
    assert all(store._closed for store in result.opened)
    assert result.tick == result.diagnostic == 0


def command(world, action='enable', *, consent=True):
    return [action, '--state-root', str(world.root), *(CONSENT if consent else [])]


def saved(world):
    with Store(world.root, keychain_factory=forbidden) as store:
        return store.read(local.NAME)


def seed(world):
    with Store(world.root, keychain_factory=forbidden) as store:
        local.enable(store, binding=world.binding, now_ms=NOW, expires_at=NOW + DAY,
                     confirm_human=True, reason='seed-consent')
    world.calls.clear()


def test_enable_discloses_target_without_effects_before_confirmation_then_saves_once(world, monkeypatch, capsys):
    def confirm(_):
        assert world.tick == world.diagnostic == 0
        assert not (world.root / local.NAME).exists()
        assert not (world.root / local.ANCHOR).exists()
        assert not (world.root / 'tick.lock').exists()
        assert not (world.root / 'diagnostic-state.lock').exists()
        world.now += 2000
        return '1'
    monkeypatch.setattr('builtins.input', confirm)
    assert cli.main(command(world)) == 0
    record = saved(world)
    assert record['binding'] == world.binding and record['enabled'] is True
    assert record['receipt']['accepted_at'] == NOW + 2000
    assert record['receipt']['expires_at'] == NOW + DAY  # Prompt never renews the displayed anchor.
    assert record['queue'] == [] and record['pending'] is None and record['consent_epoch'] is None
    assert world.calls == [('current', 0), 'profile', 'tick', ('current', 1), 'diagnostic']
    assert os.stat(world.root / local.NAME).st_mode & 0o777 == 0o600
    assert os.stat(world.root / local.ANCHOR).st_mode & 0o777 == 0o600
    out = capsys.readouterr()
    assert out.err == '' and CANARY not in out.out
    for text in ('support.invalid', 'support:synthetic', 'synthetic-install', IDS[1], world.binding['root_fingerprint'],
                 '10分', 'partial', '7日', '180日', '90日', 'Console', 'R4a', '中央の診断同意',
                 '未観測', 'Hermes', '患者情報', '秘密', '原文', 'ログ', 'URL', 'path', '既存の監視tick'):
        assert text in out.out
    assert all(component in out.out for component in
               ('runtime', 'scheduler', 'notification', 'connectivity', 'update', 'storage', 'config', 'connector'))


@pytest.mark.parametrize('answer', ['', '2', 'yes', '0', '11', EOFError, KeyboardInterrupt])
def test_decline_and_prompt_interrupt_leave_no_consent_or_lock(world, monkeypatch, answer):
    def prompt(_):
        if isinstance(answer, type):
            raise answer(CANARY)
        return answer
    monkeypatch.setattr('builtins.input', prompt)
    assert cli.main(command(world)) == 0
    assert list(world.root.iterdir()) == []
    assert world.calls == [('current', 0), 'profile']


@pytest.mark.parametrize('field,value', [
    ('support_bundle_hash', 'f' * 64), ('support_endpoint_hash', 'f' * 64),
    ('root_fingerprint', 'f' * 64), ('monitor_endpoint_hash', 'f' * 64),
    ('tenant', 'another'), ('installation', 'another'), ('device_id', IDS[5]),
    ('realm_epoch', 'realm-2'), ('device_epoch', 2), ('key_id', IDS[6]),
])
def test_change_during_prompt_cannot_authorize_displayed_target(world, monkeypatch, field, value, capsys):
    def prompt(_):
        world.binding[field] = value
        return '1'
    monkeypatch.setattr('builtins.input', prompt)
    assert cli.main(command(world)) == 1
    assert not (world.root / local.NAME).exists() and not (world.root / local.ANCHOR).exists()
    assert 'diagnostic' not in world.calls
    assert CANARY not in capsys.readouterr().err


@pytest.mark.parametrize('change', ['hash', 'origin', 'profile', 'root', 'monitor', 'missing_bundle_hash'])
def test_display_snapshot_must_join_managed_binding_before_prompt(world, monkeypatch, change):
    if change == 'hash':
        world.snapshot['bundle_hash'] = 'f' * 64
    elif change == 'origin':
        world.snapshot['bundle']['origin'] = 'https://changed.invalid'
    elif change == 'profile':
        world.snapshot['bundle']['profile_id'] = 'monitor'
    elif change == 'root':
        world.snapshot['root_fingerprint'] = 'f' * 64
    elif change == 'monitor':
        world.snapshot['monitor_endpoint_hash'] = 'f' * 64
    else:
        world.binding.pop('support_bundle_hash')
    monkeypatch.setattr('builtins.input', forbidden)
    assert cli.main(command(world)) == 1
    assert list(world.root.iterdir()) == []


@pytest.mark.parametrize('elapsed', [-1, DAY, DAY - 59999])
def test_expired_or_rolled_back_clock_does_not_extend_consent(world, monkeypatch, elapsed):
    def prompt(_):
        world.now = NOW + elapsed
        return '1'
    monkeypatch.setattr('builtins.input', prompt)
    assert cli.main(command(world)) == 1
    assert not (world.root / local.NAME).exists() and not (world.root / local.ANCHOR).exists()


@pytest.mark.parametrize('hours', ['1', '2160'])
def test_valid_duration_endpoints_are_fixed(world, hours):
    args = command(world, consent=False) + ['--valid-for-hours', hours, '--confirm-human', '--reason', 'synthetic']
    assert cli.main(args) == 0
    assert saved(world)['receipt']['expires_at'] == NOW + int(hours) * HOUR


@pytest.mark.parametrize('args', [
    ['enable'], ['renew'], ['enable', '--confirm-human', '--reason', 'synthetic'],
    ['enable', '--valid-for-hours', '24', '--reason', 'synthetic'],
    ['enable', '--valid-for-hours', '24', '--confirm-human'],
    *[['enable', '--valid-for-hours', value, '--confirm-human', '--reason', 'synthetic']
      for value in ('0', '-1', '2161', '1.0', CANARY)],
    *[['enable', '--valid-for-hours', '24', '--confirm-human', '--reason', value]
      for value in ('', '   ', 'x' * 129, '日本語', 'synthetic\nsecret', '\x1bprivate')],
    *[[action, *flag] for action in ('status', 'disable')
      for flag in (['--confirm-human'], ['--reason', CANARY], ['--valid-for-hours', '1'])],
    ['status', '--state-root', ''], ['status', '--state-root', '   '], [CANARY],
])
def test_argument_errors_echo_no_values_and_open_nothing(world, monkeypatch, args, capsys):
    monkeypatch.setattr(cli, 'Store', forbidden)
    monkeypatch.setattr('builtins.input', forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2 and world.calls == []
    out = capsys.readouterr()
    assert CANARY not in out.out + out.err and '\x1bprivate' not in out.err
    assert '引数が正しくありません' in out.err


@pytest.mark.parametrize('action', ['status', 'disable'])
def test_local_controls_bypass_config_profile_tick_credentials_and_http(world, monkeypatch, action, capsys):
    seed(world)
    before = saved(world)
    monkeypatch.setattr(cli.diagnostic_sender, 'managed_binding', forbidden)
    monkeypatch.setattr(cli.provision, 'profile_snapshot', forbidden)
    monkeypatch.setattr(Store, 'lock', forbidden)
    monkeypatch.setattr('builtins.input', forbidden)
    assert cli.main(command(world, action, consent=False)) == 0
    out = capsys.readouterr()
    summary = json.loads(out.out.splitlines()[0])
    assert set(summary) == {'enabled', 'local_receipt_id', 'consent_epoch', 'queued_events', 'pending_batch_id',
                            'attempts', 'result', 'dropped_events', 'unknown_batches', 'last_receipt',
                            'last_receipt_local_receipt_id', 'last_receipt_generation'}
    assert summary['local_receipt_id'] == before['receipt']['local_receipt_id']
    assert CANARY not in out.out + out.err
    if action == 'status':
        assert len(out.out.splitlines()) == 1 and saved(world) == before
    else:
        assert summary['enabled'] is False and saved(world)['enabled'] is False
        assert '開始済み処理の終了は未確認' in out.out and 'R1' in out.out


@pytest.mark.parametrize('action', ['status', 'disable'])
@pytest.mark.parametrize('missing', ['directory', 'both_files', 'config_root'])
def test_missing_local_state_is_unconfigured_without_repair(world, monkeypatch, action, missing, capsys):
    monkeypatch.setattr(cli.diagnostic_sender, 'managed_binding', forbidden)
    monkeypatch.setattr(cli.provision, 'profile_snapshot', forbidden)
    monkeypatch.setattr(Store, 'lock', forbidden)
    args = command(world, action, consent=False)
    if missing == 'directory':
        world.root.rmdir()
    elif missing == 'config_root':
        monkeypatch.setattr(cli.mcs_util, 'load_config', lambda: {})
        monkeypatch.setattr(cli, 'Store', forbidden)
        args = [action]
    assert cli.main(args) == 0
    assert not (world.root / local.NAME).exists() and not (world.root / local.ANCHOR).exists()
    if missing == 'directory':
        assert not world.root.exists()
    out = capsys.readouterr().out
    if action == 'status' and missing == 'both_files':
        assert json.loads(out)['local_receipt_id'] is None
    else:
        assert '未設定' in out


@pytest.mark.parametrize('action', ['status', 'disable', 'enable', 'renew'])
def test_anchor_without_state_never_repairs_or_succeeds(world, action, capsys):
    seed(world)
    anchor = (world.root / local.ANCHOR).read_bytes()
    (world.root / local.NAME).unlink()
    assert cli.main(command(world, action, consent=action in ('enable', 'renew'))) == 1
    assert not (world.root / local.NAME).exists()
    assert (world.root / local.ANCHOR).read_bytes() == anchor
    out = capsys.readouterr()
    assert '完了できませんでした' in out.err and CANARY not in out.out + out.err


def test_configured_root_only_and_explicit_root_has_precedence(world, monkeypatch):
    monkeypatch.setattr(cli.mcs_util, 'load_config', lambda: {'sdc': {'state_root': str(world.root)}})
    assert cli.main(['enable', *CONSENT]) == 0
    monkeypatch.setattr(cli.mcs_util, 'load_config', lambda: (_ for _ in ()).throw(ValueError(CANARY)))
    assert cli.main(command(world, 'status', consent=False)) == 0


@pytest.mark.parametrize('cfg', [None, [], {'sdc': False}, {'sdc': {'state_root': 1}},
                               {'sdc': {'state_root': ''}}, {'sdc': {'state_root': '   '}}])
def test_invalid_config_never_falls_back_to_default_root(world, monkeypatch, cfg, capsys):
    monkeypatch.setattr(cli.mcs_util, 'load_config', lambda: cfg)
    monkeypatch.setattr(cli, 'Store', forbidden)
    assert cli.main(['status']) == 1
    assert CANARY not in capsys.readouterr().err


@pytest.mark.parametrize('action', ['enable', 'renew'])
def test_unknown_target_or_missing_root_is_not_initialized(world, monkeypatch, action):
    monkeypatch.setattr(cli.diagnostic_sender, 'managed_binding', lambda *_a, **_k: (_ for _ in ()).throw(ValueError(CANARY)))
    monkeypatch.setattr('builtins.input', forbidden)
    assert cli.main(command(world, action)) == 1
    assert list(world.root.iterdir()) == []
    world.root.rmdir()
    assert cli.main(command(world, action)) == 1
    assert not world.root.exists()


@pytest.mark.parametrize('failure', ['corrupt', 'symlink'])
@pytest.mark.parametrize('action', ['status', 'disable'])
def test_private_state_corruption_is_fixed_failure_and_preserved(world, action, failure, capsys):
    seed(world)
    path = world.root / local.NAME
    if failure == 'corrupt':
        path.write_bytes(CANARY.encode())
    else:
        target = world.root / 'synthetic-target'
        target.write_bytes(CANARY.encode())
        path.unlink()
        path.symlink_to(target)
    assert cli.main(command(world, action, consent=False)) == 1
    assert path.read_bytes() == CANARY.encode()
    out = capsys.readouterr()
    assert CANARY not in out.out + out.err and '完了できませんでした' in out.err


def test_reopen_disable_and_explicit_renew_preserve_unknown_metadata(world, capsys):
    assert cli.main(command(world)) == 0
    with Store(world.root, keychain_factory=forbidden) as store:
        request = local.admit(store, current=world.binding, now_ms=NOW)
        token = local.record_response(store, request, {
            'schema_version': 'sdc.diagnostics.status.v1', 'request_id': IDS[5],
            'consent': {'state': 'active', 'consent_epoch': 1, 'expires_at': NOW + DAY}, 'batches': [],
        }, current=world.binding, now_ms=NOW)
        event = {'component': 'runtime', 'stage': 'run', 'code': 'UNKNOWN', 'severity': 'warning',
                 'bucket_start': NOW // 600000 * 600000, 'count': 1}
        local.collect(store, token, {'events': [event], 'dropped_events': 0, 'partial': True,
                                   'observed_at': NOW, 'source_marks': {
                                       'runtime': {'observed_at': NOW, 'signature': 'c' * 64}}},
                      current=world.binding, now_ms=NOW)
        pending = local.admit(store, current=world.binding, now_ms=NOW + 300000)
        assert pending['path'] == local.REPORT
    before = saved(world)
    world.now = NOW + 300001
    assert cli.main(command(world, 'disable', consent=False)) == 0
    disabled = saved(world)
    assert disabled['history'][0]['batch_id'] == json.loads(pending['raw'])['batch_id']
    assert disabled['history'][0]['result'] == 'unknown'
    assert cli.main(command(world, 'enable')) == 1  # enable is never an implicit re-consent.
    assert saved(world) == disabled
    assert cli.main(command(world, 'renew')) == 0
    renewed = saved(world)
    assert renewed['enabled'] is True and renewed['consent_epoch'] is None
    assert renewed['queue'] == [] and renewed['pending'] is None
    assert renewed['history'] == disabled['history']
    assert renewed['receipt']['local_receipt_id'] != before['receipt']['local_receipt_id']
    assert renewed['consent_history'][0]['receipt'] == before['receipt']
    assert renewed['generation'] != before['generation']
    assert renewed['receipt']['expires_at'] == world.now + DAY
    assert cli.main(command(world, 'status', consent=False)) == 0
    assert saved(world) == renewed
    assert CANARY not in capsys.readouterr().out


def test_renew_cancel_keeps_disabled_receipt_and_history(world, monkeypatch):
    seed(world)
    assert cli.main(command(world, 'disable', consent=False)) == 0
    before = (world.root / local.NAME).read_bytes()
    monkeypatch.setattr('builtins.input', lambda _: '')
    assert cli.main(command(world, 'renew')) == 0
    assert (world.root / local.NAME).read_bytes() == before


def test_expired_local_permission_requires_explicit_renew_and_keeps_prior_receipt(world):
    seed(world)
    before = saved(world)
    world.now = NOW + DAY
    assert cli.main(command(world, 'status', consent=False)) == 0
    expired = saved(world)
    assert expired['enabled'] is False and expired['result'] == 'expired'
    assert cli.main(command(world, 'enable')) == 1
    assert saved(world) == expired
    assert cli.main(command(world, 'renew')) == 0
    renewed = saved(world)
    assert renewed['enabled'] is True and renewed['consent_epoch'] is None
    assert renewed['consent_history'][0]['receipt'] == before['receipt']
    assert renewed['receipt']['local_receipt_id'] != before['receipt']['local_receipt_id']


@pytest.mark.parametrize('field,value', [('tenant', 'another'), ('device_id', IDS[6])])
def test_renew_cannot_transfer_saved_consent_to_another_displayed_scope(world, field, value):
    seed(world)
    before = saved(world)
    world.binding[field] = value
    world.snapshot['bundle'][field] = value
    changed_hash = digest(world.snapshot['bundle'])
    world.binding['support_bundle_hash'] = world.snapshot['bundle_hash'] = changed_hash
    assert cli.main(command(world, 'renew')) == 1
    assert saved(world) == before


def test_save_failure_does_not_print_success_or_repair_anchor(world, monkeypatch, capsys):
    original = Store.write
    def fail(store, name, value):
        if name == local.NAME:
            raise OSError(CANARY)
        return original(store, name, value)
    monkeypatch.setattr(Store, 'write', fail)
    assert cli.main(command(world)) == 1
    assert (world.root / local.ANCHOR).exists() and not (world.root / local.NAME).exists()
    out = capsys.readouterr()
    assert '同意を保存しました' not in out.out and CANARY not in out.out + out.err


def test_busy_tick_is_bounded_failure_without_diagnostic_mutation(world, monkeypatch):
    @contextmanager
    def busy(_):
        raise BlockingIOError(CANARY)
        yield
    monkeypatch.setattr(Store, 'lock', busy)
    assert cli.main(command(world)) == 1
    assert not (world.root / local.NAME).exists() and not (world.root / local.ANCHOR).exists()


def test_help_describes_status_retention_without_access(world, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'Store', forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(['--help'])
    assert error.value.code == 0
    assert '期限切れ本文を整理' in capsys.readouterr().out


@pytest.mark.parametrize('result', [0, 1, 23])
def test_mcs_dispatch_preserves_diagnostic_arguments_and_return_without_lifecycle_effects(world, monkeypatch, result):
    import mcs_cli
    monkeypatch.setenv('MCS_LIFECYCLE_PINNED', '1')
    monkeypatch.setattr(mcs_cli, 'load_config', forbidden)
    monkeypatch.setattr(mcs_cli.os, 'execv', forbidden)
    monkeypatch.setattr(mcs_cli.mcs_runtime, 'python_executable', forbidden)
    args = ['renew', '--state-root', str(world.root), *CONSENT]
    received = []
    def downstream(argv):
        received.append(list(argv))
        return result
    monkeypatch.setattr(cli, 'main', downstream)
    assert mcs_cli.main(['diagnostics', *args]) == result
    assert received == [args] and world.calls == [] and world.opened == []


def test_mcs_unknown_command_still_refuses_before_dispatch_or_lifecycle(world, monkeypatch):
    import mcs_cli
    monkeypatch.setenv('MCS_LIFECYCLE_PINNED', '1')
    monkeypatch.setattr(mcs_cli, 'load_config', forbidden)
    monkeypatch.setattr(mcs_cli.os, 'execv', forbidden)
    monkeypatch.setattr(cli, 'main', forbidden)
    with pytest.raises(SystemExit) as error:
        mcs_cli.main(['synthetic-unsupported-command'])
    assert error.value.code == 2 and world.calls == [] and world.opened == []


@pytest.fixture
def tick_world(tmp_path, monkeypatch):
    import sdc_cli
    import sdc_diagnostic_events
    from adapters.sdc import diagnostic_sender
    t = SimpleNamespace(root=tmp_path / 'private-tick', trusted=tmp_path / 'application',
                        mono=100.0, wall=NOW, timer=(30.0, 0.0), timers=[], handlers=[],
                        events=[], stores=[], samples=[], deadlines=[], parent_calls=[],
                        inventory_error=None, inventory_elapsed=0, diagnostic_enabled=True,
                        status_error=None, mode='standalone', configured_mode='standalone',
                        interpreter=sdc_cli.sys.executable, inventory_enabled=True, r1_enabled=True,
                        real_status=local.status, real_sender=diagnostic_sender.run_managed_once,
                        real_binding=diagnostic_sender.managed_binding)

    def parent(signum, frame):
        t.parent_calls.append((signum, frame))
        if getattr(t, 'parent_error', None) is not None:
            raise t.parent_error
    t.parent = t.handler = parent

    def get_handler(signum):
        assert signum == sdc_cli.signal.SIGALRM
        return t.handler

    def set_handler(signum, handler):
        assert signum == sdc_cli.signal.SIGALRM
        previous, t.handler = t.handler, handler
        t.handlers.append(handler)
        return previous

    def get_timer(kind):
        assert kind == sdc_cli.signal.ITIMER_REAL
        return t.timer

    def set_timer(kind, delay, interval=0):
        assert kind == sdc_cli.signal.ITIMER_REAL
        previous, t.timer = t.timer, (delay, interval)
        t.timers.append((delay, interval))
        return previous

    class FakeStore:
        def __init__(self, root, *, credential_deadline):
            assert root == t.root
            self.root, self.closed = root, False
            t.stores.append(self)
            t.events.append(('store', credential_deadline))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.closed = True

        @contextmanager
        def lock(self):
            yield

        def guard_credential(self):
            pass  # This fake models a settled credential; mutation holds use the real Store tests.

        def read(self, name, default=None):
            assert name in ('state.json', 'endpoint.json', 'inventory-policy.json')
            if name == 'state.json':
                return {'state': 'active', 'enabled': t.r1_enabled}
            if name == 'endpoint.json':
                return {'origin': 'https://monitor.invalid', 'environment': 'synthetic', 'audience': 'monitor:synthetic'}
            return {'enabled': True} if t.inventory_enabled else None

    class FakeConnector:
        def __init__(self, store, transport):
            assert store.root == t.root and transport == 'synthetic-r1-transport'

        def tick(self, observed):
            assert observed == {'mode': t.mode}
            t.events.append('r1')

    class FakeInventory:
        def __init__(self, store, transport):
            assert store.root == t.root and transport == 'synthetic-r1-transport'

        def tick(self, observed):
            assert observed == {'mode': t.mode}
            t.events.append('inventory')
            t.mono += t.inventory_elapsed
            if t.inventory_error is not None:
                if callable(t.inventory_error):
                    return t.inventory_error()
                raise t.inventory_error

    def status(store, *, now_ms):
        assert store.root == t.root and now_ms == t.wall
        t.events.append('diagnostic-status')
        if t.status_error is not None:
            raise t.status_error
        return {'enabled': t.diagnostic_enabled}

    def sample(**kwargs):
        t.samples.append(kwargs)
        return {'events': [], 'partial': True, 'dropped_events': 0, 'observed_at': t.wall, 'source_marks': {}}

    def run(store, *, provider, deadline):
        assert store.root == t.root
        t.events.append('diagnostic-send')
        t.deadlines.append(deadline)
        provider(previous_marks={}, since_ms=NOW, now_ms=t.wall)

    t.cli = sdc_cli
    monkeypatch.setattr(sdc_cli.signal, 'getsignal', get_handler)
    monkeypatch.setattr(sdc_cli.signal, 'signal', set_handler)
    monkeypatch.setattr(sdc_cli.signal, 'getitimer', get_timer)
    monkeypatch.setattr(sdc_cli.signal, 'setitimer', set_timer)
    monkeypatch.setattr(sdc_cli.signal, 'alarm', forbidden)
    monkeypatch.setattr(sdc_cli.time, 'monotonic', lambda: t.mono)
    monkeypatch.setattr(sdc_cli.time, 'time', lambda: t.wall / 1000)
    monkeypatch.setattr(sdc_cli.time, 'sleep', lambda delay: None if delay == 0 else forbidden())
    monkeypatch.setattr(sdc_cli.secrets, 'randbelow', lambda bound: 0)
    monkeypatch.setattr(sdc_cli, 'Store', FakeStore)
    monkeypatch.setattr(sdc_cli, 'Connector', FakeConnector)
    monkeypatch.setattr(sdc_cli, 'Transport', lambda target: 'synthetic-r1-transport')
    monkeypatch.setattr(sdc_cli, 'InventorySender', FakeInventory)
    monkeypatch.setattr(sdc_cli, 'facts', lambda: {'mode': t.mode})
    monkeypatch.setattr(sdc_cli, 'service', forbidden)
    monkeypatch.setattr(sdc_cli.mcs_util, 'load_config', lambda: {'runtime_mode': t.configured_mode})
    monkeypatch.setattr(sdc_cli.lifecycle_io, 'read_owner_config', lambda _: {
        'sdc': {'enabled': True, 'state_root': str(t.root)}})
    monkeypatch.setattr(sdc_cli.mcs_util, 'HOME', str(t.trusted))
    monkeypatch.setattr(sdc_cli.mcs_runtime, 'mode', lambda cfg: cfg['runtime_mode'])
    monkeypatch.setattr(sdc_cli.mcs_runtime, 'python_executable', lambda cfg: t.interpreter)
    monkeypatch.setattr(local, 'status', status)
    monkeypatch.setattr(diagnostic_sender, 'managed_binding', forbidden)
    monkeypatch.setattr(diagnostic_sender, 'run_managed_once', run)
    monkeypatch.setattr(sdc_diagnostic_events, 'collect_events', sample)
    monkeypatch.setattr(Store, '_keychain', forbidden)
    monkeypatch.setattr(diagnostic_sender.DiagnosticTransport, 'send', forbidden)
    yield t
    assert all(store.closed for store in t.stores)


def test_tick_preserves_parent_budget_order_provider_root_and_remaining_timer(tick_world):
    t = tick_world
    t.inventory_elapsed = 11
    assert t.cli.tick(t.root) == 0
    assert t.events == [('store', 130.0), 'r1', 'inventory', 'diagnostic-status', 'diagnostic-send']
    assert t.deadlines == [130.0]
    assert t.samples == [{'trusted_root': str(t.trusted), 'mode': 'standalone',
                         'previous_marks': {}, 'since_ms': NOW, 'now_ms': NOW}]
    assert t.timers == [(30.0, 0), (0, 0), (19.0, 0.0)]
    assert t.handler is t.parent and t.handlers[-1] is t.parent and t.parent_calls == []


def test_tick_without_parent_has_one_original_sixty_second_budget(tick_world):
    t = tick_world
    t.timer, t.handler = (0.0, 0.0), t.cli.signal.SIG_DFL
    assert t.cli.tick(t.root) == 0
    assert t.events[0] == ('store', 160.0) and t.deadlines == [160.0]
    assert t.timers == [(60.0, 0), (0, 0)] and t.handler == t.cli.signal.SIG_DFL


@pytest.mark.parametrize('handler', ['default', 'ignore'])
def test_active_parent_timer_requires_callable_owner_before_store(tick_world, handler, capsys):
    t = tick_world
    t.handler = t.cli.signal.SIG_DFL if handler == 'default' else t.cli.signal.SIG_IGN
    assert t.cli.tick(t.root) == 1
    assert t.events == [] and t.timers == [] and t.handlers == []
    assert capsys.readouterr().err == 'SDC_TICK_UNAVAILABLE\n'


def test_parent_deadline_propagates_original_baseexception_once_without_rearming(tick_world):
    class ParentDeadline(BaseException):
        pass
    t = tick_world
    error = ParentDeadline(CANARY)
    t.parent_error, t.inventory_elapsed = error, 30
    with pytest.raises(ParentDeadline) as caught:
        t.cli.tick(t.root)
    assert caught.value is error
    assert t.parent_calls == [(t.cli.signal.SIGALRM, None)]
    assert t.timers == [(30.0, 0), (0, 0)] and t.handler is t.parent
    assert t.deadlines == t.samples == [] and 'diagnostic-status' not in t.events


def test_inventory_interrupted_by_tick_alarm_does_not_start_diagnostic_work(tick_world, capsys):
    t = tick_world
    t.inventory_error = lambda: t.handler(t.cli.signal.SIGALRM, None)
    assert t.cli.tick(t.root) == 1
    assert t.events == [('store', 130.0), 'r1', 'inventory']
    assert t.deadlines == t.samples == [] and t.parent_calls == []
    assert capsys.readouterr().err == 'SDC_TICK_UNAVAILABLE\n'


@pytest.mark.parametrize('failure', [ValueError, OSError, TimeoutError])
def test_other_inventory_failure_records_fixed_error_and_allows_remaining_diagnostic(tick_world, capsys, failure):
    t = tick_world
    t.inventory_error = failure(CANARY)
    t.inventory_elapsed = 11
    assert t.cli.tick(t.root) == 0
    assert t.events == [('store', 130.0), 'r1', 'inventory', 'diagnostic-status', 'diagnostic-send']
    assert t.deadlines == [130.0] and len(t.samples) == 1
    assert capsys.readouterr().err == 'SDC_INVENTORY_UNAVAILABLE\n'


def test_original_tick_deadline_exhausted_after_inventory_starts_no_diagnostic(tick_world, capsys):
    t = tick_world
    t.timer, t.inventory_elapsed = (0.0, 0.0), 60
    assert t.cli.tick(t.root) == 1
    assert t.events == [('store', 160.0), 'r1', 'inventory']
    assert t.deadlines == t.samples == []
    assert capsys.readouterr().err == 'SDC_TICK_UNAVAILABLE\n'


@pytest.mark.parametrize('whole_tick', [False, True])
def test_diagnostic_timeout_preserves_success_only_with_remaining_tick_budget(tick_world, monkeypatch, capsys, whole_tick):
    from adapters.sdc import diagnostic_sender
    t = tick_world

    def fail(_store, *, provider, deadline):
        assert deadline == 130.0
        t.events.append('diagnostic-timeout')
        if whole_tick:
            t.handler(t.cli.signal.SIGALRM, None)
        raise TimeoutError(CANARY)

    monkeypatch.setattr(diagnostic_sender, 'run_managed_once', fail)
    assert t.cli.tick(t.root) == (1 if whole_tick else 0)
    assert t.events == [('store', 130.0), 'r1', 'inventory', 'diagnostic-status', 'diagnostic-timeout']
    assert t.samples == []
    assert capsys.readouterr().err == ('SDC_TICK_UNAVAILABLE\n' if whole_tick else 'SDC_DIAGNOSTIC_UNAVAILABLE\n')


def test_diagnostic_off_still_allows_r1_inventory_but_no_authority_sample_or_http(tick_world):
    t = tick_world
    t.diagnostic_enabled = False
    assert t.cli.tick(t.root) == 0
    assert t.events == [('store', 130.0), 'r1', 'inventory', 'diagnostic-status']
    assert t.deadlines == t.samples == []


def test_corrupt_diagnostic_state_does_not_fail_completed_r1(tick_world, capsys):
    t = tick_world
    t.status_error = ValueError(CANARY)
    assert t.cli.tick(t.root) == 0
    assert t.events == [('store', 130.0), 'r1', 'inventory', 'diagnostic-status']
    assert t.deadlines == t.samples == []
    assert capsys.readouterr().err == 'SDC_DIAGNOSTIC_UNAVAILABLE\n'


@pytest.mark.parametrize('mismatch', ['mode', 'interpreter'])
def test_diagnostic_runtime_mismatch_starts_no_authority_or_provider(tick_world, mismatch, capsys):
    t = tick_world
    t.inventory_enabled = False  # Exercise the diagnostic owner's own guard.
    if mismatch == 'mode':
        t.configured_mode = 'hermes'
    else:
        t.interpreter = '/synthetic-other-runtime/bin/python'
    assert t.cli.tick(t.root) == 0
    assert t.events == [('store', 130.0), 'r1', 'inventory', 'diagnostic-status']
    assert t.deadlines == t.samples == []
    assert capsys.readouterr().err == 'SDC_DIAGNOSTIC_RUNTIME_UNKNOWN\n'


@pytest.fixture
def not_yet_enabled_managed_world(tick_world, monkeypatch, request):
    # Reuse the existing three-profile/real-signer fixture, deferring only its
    # test convenience admission so the real CLI performs the first enable.
    # Install fake signal/clock boundaries before even its Store constructor.
    monkeypatch.setattr(cli.diagnostic_sender, 'managed_binding', tick_world.real_binding)
    monkeypatch.setattr(local, 'status', tick_world.real_status)
    monkeypatch.setattr(SenderWorld, 'enable', lambda self: None)
    return request.getfixturevalue('managed_world')


def test_actual_cli_enable_to_managed_tick_status_sample_next_tick_report(
        not_yet_enabled_managed_world, tick_world, monkeypatch, capsys):
    import sdc_diagnostic_events
    from adapters.sdc import diagnostic_sender
    w, t = not_yet_enabled_managed_world, tick_world
    t.root, t.wall = w.store.root, w.at
    opened, key_reads = [], []
    assert w.store.read(local.NAME) is None and w.store.read(local.ANCHOR) is None
    assert len({bundle['origin'] for bundle in w.bundles.values()}) == 3
    assert len({bundle['audience'] for bundle in w.bundles.values()}) == 3
    monkeypatch.setattr(local, 'status', t.real_status)
    monkeypatch.setattr(diagnostic_sender, 'managed_binding', t.real_binding)

    def owned(root, **kwargs):
        assert Path(root) == w.store.root
        if kwargs:
            assert kwargs == {'credential_deadline': 130.0}
        store = Store(root, keychain_factory=forbidden, **kwargs)
        opened.append(store)
        read = store.read
        def reading(name, default=None):
            if name == 'credential':
                key_reads.append(name)
            return read(name, default)
        monkeypatch.setattr(store, 'read', reading)
        return store

    def sample(*, trusted_root, mode, **kwargs):
        assert trusted_root == str(t.trusted) and mode == 'standalone'
        return w.sample(**kwargs)

    def sender(store, *, provider, deadline):
        assert deadline == 130.0
        t.deadlines.append(deadline)
        return t.real_sender(store, provider=provider, deadline=deadline,
                             clock=lambda: w.at / 1000, monotonic=lambda: t.mono, transport_factory=w.wire)

    monkeypatch.setattr(cli, 'Store', owned)
    monkeypatch.setattr(t.cli, 'Store', owned)
    monkeypatch.setattr(diagnostic_sender, 'run_managed_once', sender)
    monkeypatch.setattr(sdc_diagnostic_events, 'collect_events', sample)
    monkeypatch.setattr('builtins.input', lambda _: '1')
    assert cli.main(['enable', '--state-root', str(w.store.root), *CONSENT]) == 0
    assert not key_reads and not w.calls and not w.samples
    initial = w.store.read(local.NAME)
    assert initial['enabled'] is True and initial['consent_epoch'] is None
    assert w.store.read('support-state.json')['enabled'] is False
    assert t.cli.tick(w.store.root) == 0
    assert [path for path, _ in w.calls] == [local.STATUS]
    assert len(w.samples) == 1 and len(w.signed) == 1 and len(key_reads) >= 2
    assert w.store.read(local.NAME)['queue'] and w.store.read(local.NAME)['pending'] is None
    w.at += 300000
    t.wall, t.timer = w.at, (30.0, 0.0)
    assert t.cli.tick(w.store.root) == 0
    assert [path for path, _ in w.calls] == [local.STATUS, local.REPORT]
    assert len(w.samples) == 1 and len(w.signed) == 2 and t.deadlines == [130.0, 130.0]
    result = local.status(w.store, now_ms=w.at)
    assert result['result'] == 'accepted' and result['last_receipt']['batch_id'] == json.loads(w.calls[1][1])['batch_id']
    assert result['local_receipt_id'] == initial['receipt']['local_receipt_id']
    assert all(store._closed for store in opened)
    assert capsys.readouterr().err == ''
