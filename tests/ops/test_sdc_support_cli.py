"""Synthetic support CLI routing, fixed disclosure and bounded cancellation."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import mcs_cli
import sdc_support_cli as cli
from adapters.sdc import provision
from adapters.sdc.storage import Store
from adapters.sdc.support_contract import DIAGNOSTIC_IDS, PURPOSE_CODES, canonical

CASE = '33333333-3333-4333-8333-333333333333'
CANARY = 'SYNTHETIC_PRIVATE_DO_NOT_ECHO'
ROOT = Path(__file__).resolve().parents[2]


def display(purpose='HEALTH_CHECK'):
    return {'case_id': CASE, 'purpose_code': purpose, 'diagnostic_ids': list(DIAGNOSTIC_IDS),
            'operator_refs': ['a' * 64], 'tenant': 'tenant-a', 'installation': 'installation-a',
            'device_id': CASE, 'environment': 'synthetic', 'expires_at': 1792663500000,
            'consent_expires_at': 1792665000000}


def forbid_credential_reads(monkeypatch):
    original = Store._read_file
    def metadata_read(store, name, *args, **kwargs):
        assert name not in ('credential', 'credential-store.json')
        return original(store, name, *args, **kwargs)
    monkeypatch.setattr(Store, '_read_file', metadata_read)


def allow_pin_syntax_and_forbid_verification(monkeypatch):
    calls = []
    for name in ('validate_pins', 'validate_restart_pins'):
        original = getattr(cli.sdc_support_signatures, name)
        def syntax(pins, *, purpose, now_ms, _original=original, _name=name):
            calls.append((_name, purpose))
            return _original(pins, purpose=purpose, now_ms=now_ms)
        monkeypatch.setattr(cli.sdc_support_signatures, name, syntax)
    def forbidden(*_a, **_k):
        pytest.fail('metadata schema checks cannot invoke signature verification')
    monkeypatch.setattr(cli.sdc_support_signatures.subprocess, 'Popen', forbidden)
    for name in ('verify_envelope', 'verify_restart_envelope', '_verify_envelope', '_verify_pss'):
        monkeypatch.setattr(cli.sdc_support_signatures, name, forbidden)
    return calls


@pytest.mark.parametrize('purpose,label', list(zip(PURPOSE_CODES, ['通知障害の確認', '処理停止の確認', '設定不整合の確認', '更新失敗の確認', '稼働状況の確認'])))
def test_all_contract_purposes_diagnostics_are_disclosed_in_japanese(monkeypatch, capsys, purpose, label):
    monkeypatch.setattr('builtins.input', lambda _: '2')
    assert cli._confirm(display(purpose), {'origin': 'https://support.invalid'}) is False
    out = capsys.readouterr().out
    assert label in out
    assert all(x in out for x in ('実行環境の状態', '通知の健全性', '接続状況', 'Console', 'scope', '担当ref', 'mcs support stop', 'UTC'))
    assert not any(name in out for name in DIAGNOSTIC_IDS)
    assert '秘密' not in out


@pytest.mark.parametrize('answer', ['', '2', 'yes', 'true', '0'])
def test_default_no(monkeypatch, answer):
    monkeypatch.setattr('builtins.input', lambda _: answer)
    assert cli._confirm(display(), {'origin': 'https://support.invalid'}) is False


@pytest.mark.parametrize('exception', [EOFError, KeyboardInterrupt])
def test_input_cancel_is_normal_no(monkeypatch, exception):
    monkeypatch.setattr('builtins.input', lambda _: (_ for _ in ()).throw(exception()))
    assert cli._confirm(display(), {'origin': 'https://support.invalid'}) is False


@pytest.fixture
def fake_store(monkeypatch):
    class Fake:
        def __init__(self, root):
            self.root = root
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self, _):
            return None
    monkeypatch.setattr(cli, 'Store', Fake)
    monkeypatch.setattr(cli.mcs_util, 'load_config', lambda: {'sdc': {'state_root': '/synthetic/approved'}})


def test_connect_injects_explicit_crypto_transport_and_human_confirmation(fake_store, monkeypatch, capsys):
    calls = []
    def connect(store, case, **kwargs):
        calls.append((store.root, case, kwargs))
        kwargs['budget']()
        return {'state': 'cancelled'}
    monkeypatch.setattr(cli.support_client, 'connect', connect)
    assert cli.main(['connect', CASE]) == 0
    root, case, options = calls[0]
    assert root == '/synthetic/approved' and case == CASE
    assert options['verifier'] is cli.sdc_support_signatures.verify_envelope
    assert options['check_pins'] is cli.sdc_support_signatures.validate_pins
    assert options['transport_factory'] is cli.SupportTransport and options['confirm'] is cli._confirm
    assert '中止' in capsys.readouterr().out


@pytest.mark.parametrize('ended,text', [(True, '前景処理の終了確認済み'), (False, '終了は未確認')])
def test_stop_distinguishes_foreground_lifetime(fake_store, monkeypatch, capsys, ended, text):
    monkeypatch.setattr(cli.support_client, 'stop', lambda *_a, **_kw: {'foreground_ended': ended})
    assert cli.main(['stop']) == 0
    out = capsys.readouterr().out
    assert text in out and '通常の状態監視は継続' in out
    assert '通信が完全停止' not in out


@pytest.mark.parametrize('error', [BlockingIOError, OSError, ValueError])
def test_stop_failure_is_not_success_and_exception_is_secret_free(fake_store, monkeypatch, capsys, error):
    monkeypatch.setattr(cli.support_client, 'stop', lambda *_a, **_kw: (_ for _ in ()).throw(error(CANARY)))
    assert cli.main(['stop']) == 1
    out = capsys.readouterr()
    assert '停止未完了' in out.err and CANARY not in out.err + out.out


def test_status_is_explicitly_local(fake_store, monkeypatch, capsys):
    monkeypatch.setattr(cli.support_client, 'status', lambda *_a, **_kw: {'enabled': False, 'accept': 'unknown'})
    assert cli.main(['status']) == 0
    out = capsys.readouterr().out
    assert '中央の現在の認可' in out and 'unknown' in out


@pytest.mark.parametrize('command', ['plan-restart', 'consent-restart'])
def test_restart_entry_without_trusted_provision_reads_only_local_metadata(command, monkeypatch, tmp_path, capsys):
    root = tmp_path / 'legacy'
    with Store(root, create_new=True):
        pass
    fds = []
    def open_store(selected):
        assert selected == str(root)
        store = Store(selected)
        fds.append(store.fd)
        return store
    def forbidden(*_a, **_k):
        pytest.fail('unprovisioned authority, credential, crypto, collection or HTTP')
    monkeypatch.setattr(cli, 'Store', open_store)
    monkeypatch.setattr(cli.mcs_util, 'load_config', lambda: {'sdc': {'state_root': str(root)}})
    forbid_credential_reads(monkeypatch)
    monkeypatch.setattr(Store, '_keychain', forbidden)
    for name in ('plan', 'consent'):
        monkeypatch.setattr(cli.restart_client, name, forbidden)
    syntax_calls = allow_pin_syntax_and_forbid_verification(monkeypatch)
    assert cli.main([command, CASE]) == 1
    assert syntax_calls == []  # No profile exists to parse.
    assert '初期provision' in capsys.readouterr().err
    assert len(fds) == 1
    with pytest.raises(OSError):
        os.fstat(fds[0])
    assert sorted(p.name for p in root.iterdir()) == []


@pytest.fixture
def managed_profiles(tmp_path, monkeypatch):
    vectors = json.loads((ROOT / 'adapters/sdc/contracts/provision/provision-vectors.json').read_text())
    now = vectors['now']
    root = tmp_path / 'managed'
    targets = {'monitor': ('https://monitor.invalid', 'fixture:monitor'),
               'support': ('https://support.invalid', 'fixture:support'),
               'restart': ('https://restart.invalid', 'fixture:restart')}
    snapshots = {}
    with Store(root, create_new=True) as store:
        root_id = provision.prepare_root(store, confirm_human=True, reason='fixture-root', now_ms=now)['root_id']
        for profile in ('monitor', 'support', 'restart'):
            bundle = copy.deepcopy(next(row['value'] for row in vectors['positive']
                                        if row['definition'] == 'Bundle' and row['value']['profile_id'] == profile))
            bundle.update(root_id=root_id, origin=targets[profile][0], audience=targets[profile][1])
            raw = canonical(bundle, max_bytes=16384).encode('ascii')
            expected = {k: bundle[k] for k in provision.EXPECTED_FIELDS if k != 'bundle_hash'}
            expected['bundle_hash'] = hashlib.sha256(raw).hexdigest()
            plan = provision.prepare_import(store, raw, expected=expected, now_ms=now)
            provision.commit_import(store, plan, confirm_human=True, reason='fixture-import', now_ms=now)
            if profile == 'monitor':
                provision.begin_registration(store, now_ms=now)
                store.write('state.json', {'state': 'active', 'enabled': True,
                    'device_id': '00000000-0000-4000-8000-000000000005',
                    'claim_id': '00000000-0000-4000-8000-000000000006',
                    'key_id': '00000000-0000-4000-8000-000000000007'})
            else:
                def initialize(selected=profile):
                    # Closed metadata test double; CLI routing does not authorize
                    # consent or instantiate a foreground support operation here.
                    names = ('support-state.json',) if selected == 'support' else ('restart-state.json', 'restart-planning.json')
                    for name in names:
                        store.write(name, {'root_fingerprint': store.root_fingerprint()})
                provision.initialize_profile(store, profile=profile, now_ms=now, initializer=initialize)
            snapshots[profile] = provision.profile_snapshot(store, profile=profile, now_ms=now)
    monkeypatch.setattr(cli.time, 'time', lambda: now / 1000)
    monkeypatch.setattr(cli.mcs_util, 'load_config', lambda: {'sdc': {'state_root': str(root)}})
    return SimpleNamespace(root=root, now=now, snapshots=snapshots, targets=targets)


@pytest.mark.parametrize('command', ['plan-restart', 'consent-restart'])
def test_managed_default_restart_provider_is_current_five_field_snapshot_in_same_store(managed_profiles, monkeypatch, command):
    world = managed_profiles
    observed = []
    def forbidden(*_a, **_k):
        pytest.fail('metadata discovery must not use native credential, signature or HTTP')
    forbid_credential_reads(monkeypatch)
    monkeypatch.setattr(Store, '_keychain', forbidden)
    monkeypatch.setattr(cli.RestartTransport, 'send', forbidden)
    syntax_calls = allow_pin_syntax_and_forbid_verification(monkeypatch)
    def operation(store, identifier, **options):
        assert identifier == CASE and store.root == world.root
        os.fstat(store.fd)
        current = options['restart_provision']()
        assert set(current) == {'bundle', 'root_fingerprint', 'monitor_endpoint_hash', 'bundle_hash', 'local_receipt_id'}
        assert current == world.snapshots['restart']
        assert current['bundle']['origin'] == world.targets['restart'][0]
        assert current['bundle']['audience'] == world.targets['restart'][1]
        assert current['bundle']['pins'] != world.snapshots['support']['bundle']['pins']
        observed.append((store.fd, options['restart_provision']))
        return {'state': 'planned' if command == 'plan-restart' else 'accepted'}
    monkeypatch.setattr(cli.restart_client, 'plan' if command == 'plan-restart' else 'consent', operation)
    assert cli.main([command, CASE]) == 0 and len(observed) == 1
    assert {name for name, _ in syntax_calls} == {'validate_pins', 'validate_restart_pins'}
    with pytest.raises(OSError):
        os.fstat(observed[0][0])
    with pytest.raises(ValueError):
        observed[0][1]()


@pytest.mark.parametrize('invalid', ['missing', 'corrupt', 'expired', 'changed_scope', 'missing_consumer'])
def test_managed_restart_metadata_failure_never_enters_client_or_authority(managed_profiles, monkeypatch, capsys, invalid):
    world = managed_profiles
    if invalid == 'missing':
        (world.root / provision.NAME).unlink()
    elif invalid == 'corrupt':
        (world.root / provision.NAME).write_text('{invalid synthetic record')
    elif invalid == 'expired':
        monkeypatch.setattr(cli.time, 'time', lambda: world.snapshots['restart']['bundle']['expires_at'] / 1000)
    elif invalid == 'missing_consumer':
        (world.root / 'restart-state.json').unlink()
    else:
        with Store(world.root) as store:
            identity = store.read('state.json')
            identity['device_id'] = CASE
            store.write('state.json', identity)
    def forbidden(*_a, **_k):
        pytest.fail('invalid managed metadata cannot reach authority or a restart client')
    forbid_credential_reads(monkeypatch)
    monkeypatch.setattr(Store, '_keychain', forbidden)
    monkeypatch.setattr(cli.restart_client, 'plan', forbidden)
    monkeypatch.setattr(cli.RestartTransport, 'send', forbidden)
    allow_pin_syntax_and_forbid_verification(monkeypatch)
    assert cli.main(['plan-restart', CASE]) == 1
    output = capsys.readouterr()
    assert '承認済み支援設定' in output.err and 'invalid synthetic record' not in output.err + output.out


@pytest.mark.parametrize('command', ['plan-restart', 'consent-restart'])
@pytest.mark.parametrize('injected', [False, True, {}])
def test_explicit_invalid_provider_never_falls_back_to_managed_profile(managed_profiles, monkeypatch, command, injected):
    before = (managed_profiles.root / provision.NAME).read_bytes()
    def forbidden(*_a, **_k):
        pytest.fail('explicit invalid injection must not acquire managed authority')
    forbid_credential_reads(monkeypatch)
    monkeypatch.setattr(provision, 'profile_snapshot', forbidden)
    monkeypatch.setattr(Store, '_keychain', forbidden)
    monkeypatch.setattr(cli.RestartTransport, 'send', forbidden)
    syntax_calls = allow_pin_syntax_and_forbid_verification(monkeypatch)
    assert cli.main([command, CASE], restart_provision=injected) == 1
    assert syntax_calls == []
    assert (managed_profiles.root / provision.NAME).read_bytes() == before


@pytest.mark.parametrize('command', ['plan-restart', 'consent-restart'])
def test_restart_routes_use_separate_pins_human_prompt_and_shortenable_budget(fake_store, monkeypatch, command):
    calls = []
    legacy = {'root_fingerprint': 'a' * 64, 'monitor_endpoint_hash': 'b' * 64,
              'support_endpoint': {'origin': 'https://legacy-restart.invalid', 'audience': 'legacy:restart',
                                   'environment': 'synthetic'}, 'pins': [{'synthetic': 'legacy-restart'}]}
    def provision():
        return copy.deepcopy(legacy)
    def operation(store, identifier, **kwargs):
        kwargs['budget']()
        assert callable(kwargs['budget'].expires_at)
        assert kwargs['restart_provision']() == legacy
        assert set(kwargs['restart_provision']()) == {'root_fingerprint', 'monitor_endpoint_hash', 'support_endpoint', 'pins'}
        calls.append((store.root, identifier, kwargs))
        return {'state': 'planned' if command == 'plan-restart' else 'accepted'}
    monkeypatch.setattr(cli.restart_client, 'plan' if command == 'plan-restart' else 'consent', operation)
    assert cli.main([command, CASE], restart_provision=provision) == 0
    root, identifier, options = calls[0]
    assert root == '/synthetic/approved' and identifier == CASE
    assert options['restart_provision'] is provision and options['check_pins'] is cli.sdc_support_signatures.validate_pins
    assert options['check_restart_pins'] is cli.sdc_support_signatures.validate_restart_pins
    assert options['confirm'] is cli._confirm_restart and options['transport_factory'] is cli.RestartTransport
    if command == 'consent-restart':
        assert options['verifier'] is cli.sdc_support_signatures.verify_restart_envelope


def test_stop_disables_parent_before_children_and_reports_partial_failure(fake_store, monkeypatch, capsys):
    events = []
    monkeypatch.setattr(cli.support_client, 'stop', lambda *_a, **_k: events.append('parent') or {'foreground_ended': True})
    def child(_):
        events.append('children')
        raise ValueError(CANARY)
    monkeypatch.setattr(cli.restart_client, 'stop_children', child)
    assert cli.main(['stop']) == 1 and events == ['parent', 'children']
    output = capsys.readouterr()
    assert '停止未完了' in output.err and CANARY not in output.err + output.out


@pytest.mark.parametrize('answer', ['', '2', 'yes', 'true'])
def test_restart_read_confirmation_is_separate_and_defaults_to_cancel(monkeypatch, capsys, answer):
    monkeypatch.setattr('builtins.input', lambda _: answer)
    parent = {'expected': display(), 'expires_at': 1792665000000}
    assert not cli._confirm_restart('read', parent, {'origin': 'https://restart.invalid'})
    out = capsys.readouterr().out
    assert all(text in out for text in ('登録metadata', '最大4回', '1日60回', 'inspection', '対象scope', 'mcs support stop'))


@pytest.mark.parametrize('command', ['plan-restart', 'consent-restart'])
@pytest.mark.parametrize('extra', [['--expected', CANARY], ['--generation', CANARY], ['--yes'], ['--initialize'], ['--url', CANARY]])
def test_restart_cli_never_accepts_expected_generation_url_or_implicit_consent(command, extra, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main([command, CASE, *extra])
    assert caught.value.code == 2 and CANARY not in capsys.readouterr().err


@pytest.mark.parametrize('args', [['connect'], ['status', CASE], ['stop', CASE], ['connect', CASE, '--url', 'https://unapproved.invalid'],
                                  ['connect', CASE, '--pin', CANARY], ['connect', CASE, '--yes'], ['connect', CASE, '--initialize']])
def test_no_url_pin_auto_consent_or_initialization_options(args):
    with pytest.raises(SystemExit) as result:
        cli.main(args)
    assert result.value.code == 2


def test_pinned_cli_forwards_support_without_changing_arguments(monkeypatch):
    monkeypatch.setenv('MCS_LIFECYCLE_PINNED', '1')
    calls = []
    monkeypatch.setattr(cli, 'main', lambda args: calls.append(args) or 0)
    assert mcs_cli.main(['support', 'connect', CASE]) == 0
    assert calls == [['connect', CASE]]


def test_real_shim_routes_support_with_literal_args(tmp_path):
    home = tmp_path / 'synthetic-home'
    executable = home / '.mcs/venv/bin/python3'
    executable.parent.mkdir(parents=True)
    executable.write_text(f'#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    executable.chmod(0o700)
    result = subprocess.run(['/bin/sh', str(ROOT / 'scripts/mcs'), 'support', 'status'],
                            env=dict(os.environ, HOME=str(home), MCS_ROOT=str(home / '.mcs')), capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert json.loads(result.stdout) == [str(ROOT / 'mcs/ops/mcs_cli.py'), 'support', 'status']


def fake_alarm(monkeypatch, *, parent_delay=0, interval=0, handler=None):
    state = SimpleNamespace(now=100.0, handler=handler or (lambda *_: None), timer=(parent_delay, interval), history=[])
    monkeypatch.setattr(cli.time, 'monotonic', lambda: state.now)
    monkeypatch.setattr(cli.signal, 'getsignal', lambda _: state.handler)
    monkeypatch.setattr(cli.signal, 'getitimer', lambda _: state.timer)
    def install(_, handler):
        previous, state.handler = state.handler, handler
        return previous
    def timer(_, delay, interval=0):
        state.timer = (delay, interval)
        state.history.append((delay, interval))
    monkeypatch.setattr(cli.signal, 'signal', install)
    monkeypatch.setattr(cli.signal, 'setitimer', timer)
    return state


def test_deadline_never_extends_earlier_parent_alarm(monkeypatch):
    state = fake_alarm(monkeypatch, parent_delay=2)
    with cli._deadline() as budget:
        assert state.timer[0] <= 2
        state.now += 1
        budget()
    assert state.timer == (1, 0)


def test_whole_session_budget_expires_prompt_without_renewing(monkeypatch):
    state = fake_alarm(monkeypatch)
    with pytest.raises(cli._Expired):
        with cli._deadline() as budget:
            state.now += cli.FOREGROUND_SECONDS
            budget()
    assert state.timer == (0, 0)


def test_expired_parent_is_dispatched_and_not_rearmed(monkeypatch):
    calls = []
    state = fake_alarm(monkeypatch, parent_delay=2, handler=lambda *_: calls.append('parent'))
    with pytest.raises(cli._Expired):
        with cli._deadline() as budget:
            state.now += 2
            budget()
    assert calls == ['parent'] and state.timer == (0, 0)


@pytest.mark.parametrize('kind', ['flag', 'command', 'extra'])
def test_parser_never_echoes_untrusted_arguments(capsys, kind):
    args = ['connect', CASE, '--pin', CANARY] if kind == 'flag' else [CANARY] if kind == 'command' else ['status', CASE, CANARY]
    with pytest.raises(SystemExit) as result:
        cli.main(args)
    captured = capsys.readouterr()
    assert result.value.code == 2
    assert CANARY not in captured.err + captured.out
    assert '引数が正しくありません' in captured.err


@pytest.mark.parametrize('parent_exception', [False, True])
def test_expiry_during_first_cleanup_cancel_still_restores_parent_handler_and_timer(monkeypatch, parent_exception):
    calls = []
    def parent(*_):
        calls.append('parent')
        if parent_exception:
            raise RuntimeError('synthetic parent interruption')
    state = fake_alarm(monkeypatch, parent_delay=2, interval=3, handler=parent)
    normal_timer = cli.signal.setitimer
    delivered = False
    def timer(which, delay, interval=0):
        nonlocal delivered
        normal_timer(which, delay, interval)
        if delay == 0 and not delivered:
            delivered = True
            state.now = 102.5
            state.handler(cli.signal.SIGALRM, None)
    monkeypatch.setattr(cli.signal, 'setitimer', timer)
    with pytest.raises(RuntimeError if parent_exception else cli._Expired):
        with cli._deadline():
            pass
    assert state.handler is parent and state.timer == (2.5, 3)
    assert calls == ['parent']


def test_own_expiry_during_cleanup_restores_unexpired_parent_alarm(monkeypatch):
    def parent(*_):
        pytest.fail('parent is not due')
    state = fake_alarm(monkeypatch, parent_delay=400, handler=parent)
    normal_timer = cli.signal.setitimer
    delivered = False
    def timer(which, delay, interval=0):
        nonlocal delivered
        normal_timer(which, delay, interval)
        if delay == 0 and not delivered:
            delivered = True
            state.now = 400.5
            state.handler(cli.signal.SIGALRM, None)
    monkeypatch.setattr(cli.signal, 'setitimer', timer)
    with pytest.raises(cli._Expired):
        with cli._deadline():
            pass
    assert state.handler is parent and state.timer == (99.5, 0)


def test_run_uses_explicit_provider_under_whole_foreground_budget(fake_store, monkeypatch, capsys):
    def provider(*_a, **_kw):
        return None
    calls = []
    def run(store, **kwargs):
        kwargs['budget']()
        calls.append((store.root, kwargs))
        return {'state': 'empty'}
    monkeypatch.setattr(cli.support_client, 'run_once', run)
    assert cli.main(['run'], provider=provider) == 0
    assert calls[0][1]['provider'] is provider and calls[0][0] == '/synthetic/approved'
    assert '観測されません' in capsys.readouterr().out


@pytest.mark.parametrize('mode,expected', [('standalone', 'standalone'), ('hermes', 'hermes'), (None, None), ('invalid', None), (True, None)])
def test_default_run_provider_uses_product_root_and_only_explicit_mode(fake_store, monkeypatch, mode, expected):
    import sdc_support_diagnostics
    calls = []
    monkeypatch.setattr(cli.mcs_util, 'load_config', lambda: {'runtime_mode': mode, 'sdc': {'state_root': '/synthetic/monitor-private'}})
    monkeypatch.setattr(cli.mcs_util, 'HOME', Path('/synthetic/product'))
    monkeypatch.setattr(sdc_support_diagnostics, 'collect_diagnostic', lambda diagnostic_id, **kwargs: calls.append((diagnostic_id, kwargs)))
    def run(_store, **kwargs):
        kwargs['provider']('runtime.status.v1', generated_at_ms=1792663200000)
        return {'state': 'empty'}
    monkeypatch.setattr(cli.support_client, 'run_once', run)
    assert cli.main(['run', '--state-root', '/synthetic/other-monitor-private']) == 0
    assert calls == [('runtime.status.v1', {'generated_at_ms': 1792663200000, 'root': Path('/synthetic/product'), 'mode': expected})]


def test_run_argv_contains_no_remote_params_or_automatic_schedule(capsys):
    with pytest.raises(SystemExit):
        cli.main(['run', '--shell', CANARY])
    output = capsys.readouterr()
    assert CANARY not in output.err + output.out


def test_run_pregrant_terminal_message_does_not_claim_diagnosis(fake_store, monkeypatch, capsys):
    monkeypatch.setattr(cli.support_client, 'run_once', lambda *_a, **_kw: {'state': 'pregrant_terminal', 'terminal_state': 'expired'})
    assert cli.main(['run'], provider=lambda *_a, **_kw: None) == 0
    output = capsys.readouterr().out
    assert '未開始' in output and '診断は実行していません' in output


@pytest.mark.parametrize('interval', [None, '30', '60'])
def test_watch_explicit_entry_injects_fixed_provider_and_shorten_only_alarm(fake_store, monkeypatch, capsys, interval):
    calls = []
    def provider(*_a, **_kw):
        return None
    def watch(_store, **kwargs):
        kwargs['budget']()
        calls.append(kwargs)
        return {'state': 'watch_finished', 'reason': 'deadline', 'passes': 1}
    monkeypatch.setattr(cli.support_client, 'watch', watch)
    args = ['watch'] + (['--interval', interval] if interval else [])
    assert cli.main(args, provider=provider) == 0
    assert calls[0]['provider'] is provider and calls[0]['interval'] == int(interval or 60)
    assert callable(calls[0]['limit_deadline'])
    output = capsys.readouterr().out
    assert '300秒' in output and '完了時間の保証ではありません' in output


@pytest.mark.parametrize('args', [['run', '--interval', '30'], ['connect', CASE, '--interval', '60'], ['watch', '--interval', '1'], ['watch', '--interval', CANARY]])
def test_interval_is_watch_only_and_closed_without_input_echo(args, capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(args)
    assert result.value.code == 2 and CANARY not in capsys.readouterr().err


def test_deadline_explicit_shorten_never_extends_parent_or_resets_budget(monkeypatch):
    state = fake_alarm(monkeypatch, parent_delay=20)
    monkeypatch.setattr(cli.time, 'time', lambda: 1000)
    with cli._deadline() as budget:
        budget.shorten(1005000)
        assert state.timer[0] == 5
        budget.shorten(999999999)
        assert state.timer[0] == 5
        state.now += 2
        budget.shorten(999999999)
        assert state.timer[0] == 3
    assert state.timer[0] == 18


@pytest.mark.parametrize('command', ['stop', 'status', 'connect'])
def test_explicit_root_nonprovider_commands_do_not_read_broken_config(fake_store, monkeypatch, command):
    def broken():
        raise ValueError(CANARY)
    monkeypatch.setattr(cli.mcs_util, 'load_config', broken)
    monkeypatch.setattr(cli.support_client, 'stop', lambda *_a, **_kw: {'foreground_ended': True})
    monkeypatch.setattr(cli.support_client, 'status', lambda *_a, **_kw: {'enabled': False})
    monkeypatch.setattr(cli.support_client, 'connect', lambda *_a, **_kw: {'state': 'cancelled'})
    args = [command] + ([CASE] if command == 'connect' else []) + ['--state-root', '/synthetic/approved']
    assert cli.main(args) == 0


@pytest.mark.parametrize('config', [{'sdc': None}, {'sdc': []}, {}, None])
def test_unspecified_root_bad_config_fails_fixed_without_repair(fake_store, monkeypatch, capsys, config):
    monkeypatch.setattr(cli.mcs_util, 'load_config', lambda: config)
    assert cli.main(['stop']) == 1
    output = capsys.readouterr()
    assert '停止未完了' in output.err and CANARY not in output.err


def test_unspecified_root_config_exception_is_not_echoed(fake_store, monkeypatch, capsys):
    monkeypatch.setattr(cli.mcs_util, 'load_config', lambda: (_ for _ in ()).throw(ValueError(CANARY)))
    assert cli.main(['status']) == 1
    output = capsys.readouterr()
    assert CANARY not in output.err + output.out


@pytest.mark.parametrize('command', ['run', 'watch'])
def test_explicit_root_injected_provider_does_not_need_config(fake_store, monkeypatch, command):
    monkeypatch.setattr(cli.mcs_util, 'load_config', lambda: (_ for _ in ()).throw(ValueError(CANARY)))
    monkeypatch.setattr(cli.support_client, 'run_once', lambda *_a, **_kw: {'state': 'empty'})
    monkeypatch.setattr(cli.support_client, 'watch', lambda *_a, **_kw: {'state': 'watch_finished', 'reason': 'deadline'})
    assert cli.main([command, '--state-root', '/synthetic/approved'], provider=lambda *_a, **_kw: None) == 0


@pytest.mark.parametrize('command', ['run', 'watch'])
def test_default_provider_keeps_product_root_with_explicit_monitor_root_and_null_sdc(fake_store, monkeypatch, command):
    import sdc_support_diagnostics
    calls = []
    monkeypatch.setattr(cli.mcs_util, 'load_config', lambda: {'runtime_mode': 'standalone', 'sdc': None})
    monkeypatch.setattr(cli.mcs_util, 'HOME', Path('/synthetic/product'))
    monkeypatch.setattr(sdc_support_diagnostics, 'collect_diagnostic', lambda diagnostic_id, **kwargs: calls.append((diagnostic_id, kwargs)))
    def run(_store, **kwargs):
        kwargs['provider']('runtime.status.v1', generated_at_ms=1792663200000)
        return {'state': 'empty'}
    monkeypatch.setattr(cli.support_client, 'run_once', run)
    monkeypatch.setattr(cli.support_client, 'watch', run)
    assert cli.main([command, '--state-root', '/synthetic/monitor-private']) == 0
    assert calls == [('runtime.status.v1', {'generated_at_ms': 1792663200000, 'root': Path('/synthetic/product'), 'mode': 'standalone'})]
