"""Synthetic first-provision caller checks without Keychain, services or HTTP."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import sdc_cli
import sdc_provision_cli as cli
from adapters.sdc import provision
from adapters.sdc.storage import Store


NOW = 1792663200000
EXPECTED = {
    'console_origin': 'https://console.invalid', 'bundle_hash': 'a' * 64,
    'tenant': 'synthetic', 'installation': 'synthetic-install',
    'environment': 'synthetic', 'enrollment_id': '00000000-0000-4000-8000-000000000003',
    'profile_id': 'monitor', 'origin': 'https://ingest.invalid', 'audience': 'sdc:synthetic',
}
CONFIRM = ['--confirm-human', '--reason', 'synthetic-initial-provision']
PROFILE_TARGETS = {'monitor': ('https://ingest.invalid', 'sdc:synthetic'),
                   'support': ('https://support.invalid', 'support:synthetic'),
                   'restart': ('https://restart.invalid', 'restart:synthetic')}


def answers(monkeypatch, values):
    remaining = iter(values)
    monkeypatch.setattr('builtins.input', lambda _: next(remaining))


@pytest.fixture
def boundary(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        pytest.fail('unexpected registration, service, credential or HTTP effect')

    class FakeStore:
        def __init__(self, root, **kwargs):
            calls.append(('open', root, kwargs))
            self.root = root

        def __enter__(self):
            return self

        def __exit__(self, *_):
            calls.append(('close',))

        def lock(self):
            pytest.fail('CLI must not nest the core state lock')

    monkeypatch.setattr(cli, 'Store', FakeStore)
    monkeypatch.setattr(sdc_cli, 'Store', forbidden)
    monkeypatch.setattr(sdc_cli, 'Connector', forbidden)
    monkeypatch.setattr(sdc_cli, 'Transport', forbidden)
    monkeypatch.setattr(sdc_cli, 'service', forbidden)
    monkeypatch.setattr(sdc_cli.mcs_util, 'load_config', forbidden)
    monkeypatch.setattr(cli, 'ProvisionTransport', forbidden)
    monkeypatch.setattr(cli.time, 'time', lambda: NOW / 1000)
    monkeypatch.setattr(provision, 'prepare_root', forbidden)
    monkeypatch.setattr(provision, 'prepare_import', forbidden)
    monkeypatch.setattr(provision, 'commit_import', forbidden)
    monkeypatch.setattr(provision, 'ack_once', forbidden)
    monkeypatch.setattr(provision, 'status', forbidden)
    monkeypatch.setattr(cli, '_initialize_profile', forbidden)
    return calls


def test_root_dispatch_precedes_existing_store_customer_and_service(boundary, monkeypatch, tmp_path):
    def prepare(store, **kwargs):
        assert kwargs == {'confirm_human': True, 'reason': CONFIRM[-1], 'now_ms': NOW}
        boundary.append(('prepare',))
        return {'root_id': '00000000-0000-4000-8000-000000000002'}

    monkeypatch.setattr(provision, 'prepare_root', prepare)
    answers(monkeypatch, ['1'])
    root = tmp_path / 'fresh'
    assert sdc_cli.main(['connect', '--state-root', str(root), '--prepare-provision-root', *CONFIRM]) == 0
    assert boundary == [('open', root, {'create_new': True}), ('prepare',), ('close',)]


@pytest.mark.parametrize('answer', ['', '2', 'unexpected'])
def test_root_cancel_is_zero_store_and_effects(boundary, monkeypatch, tmp_path, answer):
    answers(monkeypatch, [answer])
    assert sdc_cli.main(['connect', '--state-root', str(tmp_path / 'fresh'), '--prepare-provision-root', *CONFIRM]) == 1
    assert boundary == []


@pytest.mark.parametrize('flags', [
    ['connect', '--prepare-provision-root', *CONFIRM],
    ['connection', '--state-root', '/synthetic/private', '--prepare-provision-root', *CONFIRM],
    ['connect', '--state-root', '/synthetic/private', '--prepare-provision-root'],
    ['connect', '--state-root', '/synthetic/private', '--prepare-provision-root', '--confirm-human'],
    ['connect', '--state-root', '/synthetic/private', '--prepare-provision-root', '--reason', 'x'],
    ['connect', '--state-root', '/synthetic/private', '--prepare-provision-root', '--customer', *CONFIRM],
    ['connect', '--state-root', '/synthetic/private', '--prepare-provision-root', '--import-provision', 'x', *CONFIRM],
    ['connect', '--state-root', '/synthetic/private', '--provision-ack', 'monitor', *CONFIRM],
    ['connection', '--state-root', '/synthetic/private', '--provision-ack', 'support'],
    ['connection', '--state-root', '/synthetic/private', '--provision-status', 'restart', '--customer'],
    ['connection', '--state-root', '/synthetic/private', '--provision-status', 'monitor', *CONFIRM],
    # Without a provision operation the flags admit only connect / rotate-key / disconnect credential changes.
    ['connection', '--state-root', '/synthetic/private', *CONFIRM],
    ['connect', '--state-root', '/synthetic/private', '--reason', 'synthetic reason without confirmation'],
    ['rotate-key', '--state-root', '/synthetic/private', '--confirm-human', '--reason', '日本語'],
])
def test_invalid_flag_combinations_have_no_effects(boundary, monkeypatch, flags):
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('invalid argument prompt'))
    with pytest.raises(SystemExit) as error:
        sdc_cli.main(flags)
    assert error.value.code == 2 and boundary == []


@pytest.mark.parametrize('reason', ['', ' ', 'x' * 129, ' ' * 128 + 'x', 'line\nbreak', '日本語'])
def test_invalid_local_reason_is_rejected_before_store(boundary, reason):
    with pytest.raises(SystemExit):
        sdc_cli.main(['connect', '--state-root', '/synthetic/private', '--prepare-provision-root', '--confirm-human', '--reason', reason])
    assert boundary == []


@pytest.mark.parametrize('profile', ['monitor', 'support', 'restart'])
def test_import_passes_exact_bytes_and_independent_console_expected(boundary, monkeypatch, tmp_path, capsys, profile):
    file = tmp_path / 'public.json'
    exact = b'{"synthetic":"file-is-not-the-independent-expected"}'
    file.write_bytes(exact)
    expected = {**EXPECTED, 'profile_id': profile, 'origin': PROFILE_TARGETS[profile][0],
                'audience': PROFILE_TARGETS[profile][1]}
    plan = {'bundle': {'profile_id': profile}}
    borrowed = []

    def prepare(store, raw, **kwargs):
        assert raw == exact
        assert kwargs == {'expected': expected, 'now_ms': NOW}
        borrowed.append(store)
        boundary.append(('validated',))
        return plan

    def commit(store, candidate, **kwargs):
        assert candidate is plan
        assert store is borrowed[0]
        assert kwargs == {'confirm_human': True, 'reason': CONFIRM[-1], 'now_ms': NOW}
        boundary.append(('durable',))
        return {'local_receipt_id': '00000000-0000-4000-8000-000000000004'}

    monkeypatch.setattr(provision, 'prepare_import', prepare)
    monkeypatch.setattr(provision, 'commit_import', commit)
    def local_status(store, **kwargs):
        assert store is borrowed[0] and profile != 'monitor'
        assert kwargs == {'profile': profile, 'now_ms': NOW}
        boundary.append(('profile-fresh', profile))
        return {'local_receipt_id': None}
    monkeypatch.setattr(provision, 'status', local_status)
    def initialize(store, selected):
        assert store is borrowed[0] and selected == profile and profile != 'monitor'
        boundary.append(('initialize', selected))
    monkeypatch.setattr(cli, '_initialize_profile', initialize)
    answers(monkeypatch, ['1', *expected.values(), '1', *(['2'] if profile == 'monitor' else [])])
    root = tmp_path / 'private'
    assert sdc_cli.main(['connect', '--state-root', str(root), '--import-provision', str(file), *CONFIRM]) == 0
    assert boundary == [('open', root, {}), ('validated',),
                        *([('profile-fresh', profile)] if profile != 'monitor' else []), ('durable',),
                        *([('initialize', profile)] if profile != 'monitor' else []), ('close',)]
    out = capsys.readouterr().out
    if profile != 'monitor':
        assert ('mcs support connect' if profile == 'support' else 'mcs support plan-restart') in out
        assert 'このまま同じ領域で状態監視の登録' not in out
        assert '--state-root <同じ私有領域> --provision-status ' + profile in out
        assert '--provision-ack ' + profile + ' --confirm-human --reason ' in out


@pytest.mark.parametrize('profile', ['support', 'restart'])
def test_customer_registration_flag_cannot_be_ignored_by_profile_import(boundary, monkeypatch, tmp_path, profile):
    file = tmp_path / 'public.json'
    file.write_bytes(b'{"synthetic":"profile-is-not-monitor-registration"}')
    expected = {**EXPECTED, 'profile_id': profile, 'origin': PROFILE_TARGETS[profile][0],
                'audience': PROFILE_TARGETS[profile][1]}
    monkeypatch.setattr(provision, 'prepare_import', lambda *_a, **_kw: {'bundle': {'profile_id': profile}})
    answers(monkeypatch, ['1', *expected.values()])
    root = tmp_path / 'private'
    assert sdc_cli.main(['connect', '--state-root', str(root), '--import-provision', str(file), '--customer', *CONFIRM]) == 1
    assert boundary == [('open', root, {}), ('close',)]


@pytest.mark.parametrize('profile', ['support', 'restart'])
def test_existing_profile_reapproval_checks_disabled_state_without_initialization(boundary, monkeypatch, tmp_path, profile):
    from adapters.sdc import restart_client, support_client

    file = tmp_path / 'public.json'
    file.write_bytes(b'{"synthetic":"reapproval"}')
    expected = {**EXPECTED, 'profile_id': profile, 'origin': PROFILE_TARGETS[profile][0],
                'audience': PROFILE_TARGETS[profile][1]}
    receipt = {'local_receipt_id': '00000000-0000-4000-8000-000000000004'}
    borrowed = []
    def prepare(store, raw, **kwargs):
        assert raw == file.read_bytes() and kwargs == {'expected': expected, 'now_ms': NOW}
        borrowed.append(store)
        return {'bundle': {'profile_id': profile}}
    def previous(store, **kwargs):
        assert store is borrowed[0] and kwargs == {'profile': profile, 'now_ms': NOW}
        boundary.append(('existing', profile))
        return receipt
    def commit(store, plan, **kwargs):
        assert store is borrowed[0] and plan['bundle']['profile_id'] == profile
        assert kwargs == {'confirm_human': True, 'reason': CONFIRM[-1], 'now_ms': NOW}
        boundary.append(('same-receipt',))
        return receipt
    def snapshot(store, **kwargs):
        assert store is borrowed[0] and kwargs == {'profile': profile, 'now_ms': NOW}
        boundary.append(('snapshot',))
    def domain_status(store, **kwargs):
        assert store is borrowed[0] and kwargs == {'now_ms': NOW}
        boundary.append(('disabled-state',))
        return {'enabled': False, 'disabled': True}
    monkeypatch.setattr(provision, 'prepare_import', prepare)
    monkeypatch.setattr(provision, 'status', previous)
    monkeypatch.setattr(provision, 'commit_import', commit)
    monkeypatch.setattr(provision, 'profile_snapshot', snapshot)
    monkeypatch.setattr(support_client if profile == 'support' else restart_client, 'status', domain_status)
    answers(monkeypatch, ['1', *expected.values(), '1'])
    root = tmp_path / 'private'
    assert sdc_cli.main(['connect', '--state-root', str(root), '--import-provision', str(file), *CONFIRM]) == 0
    assert boundary == [('open', root, {}), ('existing', profile), ('same-receipt',),
                        ('snapshot',), ('disabled-state',), ('close',)]


@pytest.mark.parametrize('phase', ['independent', 'field', 'commit'])
def test_import_cancellation_cannot_save_or_register(boundary, monkeypatch, tmp_path, phase):
    file = tmp_path / 'public.json'
    file.write_bytes(b'{}')
    monkeypatch.setattr(provision, 'prepare_import', lambda *a, **kw: {'bundle': {'profile_id': 'monitor'}})
    inputs = {'independent': ['2'], 'field': ['1', ''], 'commit': ['1', *EXPECTED.values(), '2']}[phase]
    answers(monkeypatch, inputs)
    assert sdc_cli.main(['connect', '--state-root', str(tmp_path / 'private'), '--import-provision', str(file), *CONFIRM]) == 1
    assert (len(boundary) == 2) if phase == 'commit' else boundary == []


@pytest.mark.parametrize('failure', ['prepare', 'commit'])
def test_core_failure_is_redacted_and_never_enables_monitoring(boundary, monkeypatch, tmp_path, capsys, failure):
    file = tmp_path / 'public.json'
    file.write_bytes(b'{"synthetic":"PRIVATE_CANARY"}')
    monkeypatch.setattr(provision, 'prepare_import', lambda *a, **kw: {'bundle': {'profile_id': 'monitor'}})
    def fail(*args, **kwargs):
        raise ValueError('PRIVATE_CANARY')
    monkeypatch.setattr(provision, 'prepare_import' if failure == 'prepare' else 'commit_import', fail)
    answers(monkeypatch, ['1', *EXPECTED.values(), '1'])
    assert sdc_cli.main(['connect', '--state-root', str(tmp_path / 'private'), '--import-provision', str(file), *CONFIRM]) == 1
    result = capsys.readouterr()
    assert 'PRIVATE_CANARY' not in result.out + result.err
    assert '配布を保存しました' not in result.out


@pytest.mark.parametrize('action', ['ack', 'status'])
@pytest.mark.parametrize('profile', ['monitor', 'support', 'restart'])
def test_one_device_provision_action_with_fixed_transport_and_local_consent(boundary, monkeypatch, tmp_path, action, profile):
    def send(store, **kwargs):
        assert kwargs == {'profile': profile, 'transport_factory': cli.ProvisionTransport,
                          'clock': cli.time.time, 'action': action, 'confirm_human': action == 'ack',
                          'reason': CONFIRM[-1] if action == 'ack' else None}
        boundary.append(('one-action', action))
        return {'state': 'unknown'}
    monkeypatch.setattr(provision, 'ack_once', send)
    answers(monkeypatch, ['1'] if action == 'ack' else [])
    root = tmp_path / 'private'
    flags = CONFIRM if action == 'ack' else []
    assert sdc_cli.main(['connection', '--state-root', str(root), '--provision-' + action, profile, *flags]) == 1
    assert boundary == [('open', root, {}), ('one-action', action), ('close',)]


@pytest.mark.parametrize('action', ['ack', 'status'])
@pytest.mark.parametrize('profile', ['monitor', 'support', 'restart'])
def test_provision_timeout_guides_same_profile_status_once_without_retry_or_secret(boundary, monkeypatch, tmp_path, capsys, action, profile):
    def timed_out(store, **kwargs):
        assert store.root == root and kwargs == {
            'profile': profile, 'transport_factory': cli.ProvisionTransport, 'clock': cli.time.time,
            'action': action, 'confirm_human': action == 'ack', 'reason': CONFIRM[-1] if action == 'ack' else None}
        boundary.append(('one-timeout', action, profile))
        raise TimeoutError('PRIVATE_TIMEOUT_CANARY')
    monkeypatch.setattr(provision, 'ack_once', timed_out)
    answers(monkeypatch, ['1'] if action == 'ack' else [])
    root = tmp_path / 'private-timeout'
    flags = CONFIRM if action == 'ack' else []
    assert sdc_cli.main(['connection', '--state-root', str(root), '--provision-' + action, profile, *flags]) == 1
    assert boundary == [('open', root, {}), ('one-timeout', action, profile), ('close',)]
    output = capsys.readouterr()
    hint = 'mcs connection --state-root <同じ私有領域> --provision-status ' + profile
    assert output.err.count(hint) == 1 and '結果は確定していません' in output.err
    assert '--provision-ack' not in output.err and str(root) not in output.err
    assert 'PRIVATE_TIMEOUT_CANARY' not in output.out + output.err


@pytest.mark.parametrize('stage', ['root', 'import-prepare', 'import-commit'])
def test_preparation_timeout_does_not_claim_an_ack_was_sent(boundary, monkeypatch, tmp_path, capsys, stage):
    def timed_out(*_args, **_kwargs):
        boundary.append(('timeout', stage))
        raise TimeoutError('PRIVATE_PREPARE_TIMEOUT_CANARY')
    root = tmp_path / 'private-timeout'
    if stage == 'root':
        monkeypatch.setattr(provision, 'prepare_root', timed_out)
        args = ['connect', '--state-root', str(root), '--prepare-provision-root', *CONFIRM]
        answers(monkeypatch, ['1'])
    else:
        file = tmp_path / 'public.json'
        file.write_bytes(b'{"synthetic":"PRIVATE_PREPARE_TIMEOUT_CANARY"}')
        monkeypatch.setattr(provision, 'prepare_import', lambda *_args, **_kwargs: {'bundle': {'profile_id': 'monitor'}})
        monkeypatch.setattr(provision, 'prepare_import' if stage == 'import-prepare' else 'commit_import', timed_out)
        args = ['connect', '--state-root', str(root), '--import-provision', str(file), *CONFIRM]
        answers(monkeypatch, ['1', *EXPECTED.values(), '1'])
    assert sdc_cli.main(args) == 1
    assert boundary == [('open', root, {'create_new': True} if stage == 'root' else {}), ('timeout', stage), ('close',)]
    output = capsys.readouterr()
    assert '時間内に完了しませんでした' in output.err
    assert '--provision-status' not in output.err and '--provision-ack' not in output.err and '送信' not in output.err
    assert 'PRIVATE_PREPARE_TIMEOUT_CANARY' not in output.out + output.err


@pytest.mark.parametrize('interrupt', [EOFError, KeyboardInterrupt])
def test_interrupted_confirmation_is_not_success(boundary, monkeypatch, tmp_path, interrupt):
    def stop(_):
        raise interrupt
    monkeypatch.setattr('builtins.input', stop)
    assert sdc_cli.main(['connect', '--state-root', str(tmp_path / 'private'), '--prepare-provision-root', *CONFIRM]) == 1
    assert boundary == []


@pytest.mark.parametrize('kind', ['empty', 'oversize', 'directory', 'fifo', 'link', 'parent-link'])
def test_public_bundle_read_is_bounded_regular_and_no_follow(tmp_path, kind):
    target = tmp_path / 'bundle.json'
    target.write_bytes(b'{}')
    if kind == 'empty':
        target.write_bytes(b'')
    elif kind == 'oversize':
        target.write_bytes(b'x' * 16385)
    elif kind == 'directory':
        target = tmp_path
    elif kind == 'fifo':
        target = tmp_path / 'pipe'
        cli.os.mkfifo(target)
    elif kind == 'link':
        link = tmp_path / 'link'
        link.symlink_to(target)
        target = link
    elif kind == 'parent-link':
        link = tmp_path / 'parent'
        link.symlink_to(tmp_path, target_is_directory=True)
        target = link / target.name
    with pytest.raises((OSError, ValueError)):
        cli._read_bundle(target)


def test_public_bundle_read_accepts_exact_size_limit(tmp_path):
    target = tmp_path / 'public.json'
    target.write_bytes(b'x' * 16384)
    assert cli._read_bundle(target) == b'x' * 16384


@pytest.mark.parametrize('fail_begin', [False, True])
def test_registration_guard_precedes_customer_and_connector_without_lock_reentry(monkeypatch, tmp_path, fail_begin):
    calls = []
    locked = False

    class FakeStore:
        root = tmp_path / 'private'
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        @contextmanager
        def lock(self):
            nonlocal locked
            assert not locked
            locked = True
            try:
                yield
            finally:
                locked = False
        def enable_customer(self):
            calls.append('customer')
        def read(self, name, default=None):
            return default if name == 'state.json' else {}

    def begin(store, **kwargs):
        assert not locked and kwargs['now_ms'] > 0
        calls.append('guard')
        if fail_begin:
            raise ValueError('SDC_PROVISION_HOLD')

    def connector(*args, **kwargs):
        calls.append('connector')
        raise ValueError('stop before any actual registration')

    monkeypatch.setattr(provision, 'begin_registration', begin)
    monkeypatch.setattr(sdc_cli, 'Store', FakeStore)
    monkeypatch.setattr(sdc_cli, 'Connector', connector)
    monkeypatch.setattr(sdc_cli, 'Transport', lambda _: SimpleNamespace())
    monkeypatch.setattr(sdc_cli.mcs_util, 'load_config', lambda: {})
    # The one-time code is asked before the lock (RAM only), after the registration guard.
    monkeypatch.setattr(sdc_cli.getpass, 'getpass', lambda _: calls.append('code') or 'SYNTHETIC_ONE_TIME_TOKEN')
    assert sdc_cli.main(['connect', '--state-root', str(FakeStore.root), '--customer']) == 1
    assert calls == (['guard'] if fail_begin else ['guard', 'code', 'customer', 'connector'])


def test_existing_empty_root_is_not_claimed_by_prepare(monkeypatch, tmp_path):
    root = tmp_path / 'existing'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(cli, 'Store', Store)
    answers(monkeypatch, ['1'])
    assert sdc_cli.main(['connect', '--state-root', str(root), '--prepare-provision-root', *CONFIRM]) == 1
    assert list(root.iterdir()) == []


@pytest.fixture
def actual_world(tmp_path, monkeypatch):
    import test_sdc_customer_cli as customer
    from adapters.sdc.support_contract import canonical

    home, cfg, native, network, jobs = customer.world.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, 'Store', sdc_cli.Store)
    monkeypatch.setattr(__name__ + '.Store', sdc_cli.Store)
    monkeypatch.setattr(cli.time, 'time', lambda: NOW / 1000)
    root = home / 'provisioned'
    answers(monkeypatch, ['1'])
    assert sdc_cli.main(['connect', '--state-root', str(root), '--prepare-provision-root', *CONFIRM]) == 0
    assert native.calls == network == jobs == []
    with Store(root) as store:
        root_id = provision.status(store, now_ms=NOW)['root_id']
    vectors = json.loads((Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/provision/provision-vectors.json').read_text())
    bundle = copy.deepcopy(next(item['value'] for item in vectors['positive']
                                if item['definition'] == 'Bundle' and item['value']['profile_id'] == 'monitor'))
    bundle['root_id'] = root_id
    raw = canonical(bundle, max_bytes=16384).encode('ascii')
    # The synthetic issuer and independently entered Console target are fixed by
    # this oracle. The product caller never creates expected from downloaded raw.
    expected = {**EXPECTED, 'tenant': 'self', 'installation': 'self', 'bundle_hash': hashlib.sha256(raw).hexdigest()}
    file = home / 'public-bundle.json'
    file.write_bytes(raw)
    return SimpleNamespace(root=root, file=file, bundle=bundle, raw=raw, expected=expected,
                           cfg=cfg, native=native, network=network, jobs=jobs)


def test_actual_cli_import_same_store_r1_then_device_ack_and_status(actual_world, monkeypatch):
    from adapters.sdc.protocol import strict_json

    world = actual_world
    answers(monkeypatch, ['1', *world.expected.values(), '1', '1', '1'])
    assert sdc_cli.main(['connect', '--state-root', str(world.root), '--import-provision', str(world.file), '--customer', *CONFIRM]) == 0
    assert [item[0] for item in world.network] == ['/v2/enrollments/claim', '/v2/enrollments/status', '/v2/enrollments/confirm']
    assert world.jobs == [(True, world.root)] and world.native.items
    with Store(world.root) as store:
        local = provision.status(store, now_ms=NOW)
        assert local['registration_started'] is True and local['acknowledged'] is False
        identity = {name: store.read('state.json')[name] for name in ('device_id', 'claim_id', 'key_id')}
        assert store.read('state.json')['state'] == 'active'
        record = store._read_file(provision.NAME)['profiles']['monitor']
        saved_ack = record['ack_request']['raw'].encode('ascii')
        assert record['raw'].encode('ascii') == world.raw
        ack_body = strict_json(saved_ack)
        receipt = {key: value for key, value in ack_body.items() if key != 'schema_version'}
        receipt.update(identity, generation=1, accepted_at=NOW)

    calls = []
    class DeviceTransport:
        def __init__(self, config):
            assert config == {key: world.bundle[key] for key in ('origin', 'audience', 'environment')}
        def send(self, path, raw, headers):
            assert headers and raw
            calls.append((path, raw))
            if path == provision.ACK:
                assert raw == saved_ack
                return {'schema_version': 'sdc.provision.ack-response.v1', 'receipt': receipt,
                        'request_id': '00000000-0000-4000-8000-00000000001e'}
            assert path == provision.STATUS
            value = {key: world.bundle[key] for key in ('provision_id', 'root_id', 'generation', 'realm_epoch', 'policy_revision', 'expires_at')}
            value.update(bundle_hash=world.expected['bundle_hash'], current_generation=1,
                         current_provision_id=world.bundle['provision_id'], state='current', receipt=receipt)
            return {'schema_version': 'sdc.provision.status-response.v1', 'status': value,
                    'request_id': '00000000-0000-4000-8000-00000000001e'}

    monkeypatch.setattr(cli, 'ProvisionTransport', DeviceTransport)
    answers(monkeypatch, ['1'])
    assert sdc_cli.main(['connection', '--state-root', str(world.root), '--provision-ack', 'monitor', *CONFIRM]) == 0
    assert sdc_cli.main(['connection', '--state-root', str(world.root), '--provision-status', 'monitor']) == 0
    assert [path for path, _ in calls] == [provision.ACK, provision.STATUS]
    with Store(world.root) as store:
        summary = provision.status(store, now_ms=NOW)
        assert summary['acknowledged'] is True and summary['ack_attempts'] == 1
        assert summary['local_receipt_id'] == local['local_receipt_id']


def test_import_hold_reopen_needs_reapproval_and_keeps_same_receipt(actual_world, monkeypatch):
    world = actual_world
    flags = ['connect', '--state-root', str(world.root), '--import-provision', str(world.file), '--customer', *CONFIRM]
    answers(monkeypatch, ['1', *world.expected.values(), '1', '2'])
    assert sdc_cli.main(flags) == 0
    assert world.network == world.jobs == world.native.calls == []
    with Store(world.root) as store:
        before = provision.status(store, now_ms=NOW)
        assert before['registration_started'] is False
    # The already saved receipt is not a transferable first-I/O capability.
    assert sdc_cli.main(['connect', '--state-root', str(world.root), '--customer']) == 1
    assert world.network == world.jobs == world.native.calls == []
    answers(monkeypatch, ['1', *world.expected.values(), '1', '1', '1'])
    assert sdc_cli.main(flags) == 0
    with Store(world.root) as store:
        assert provision.status(store, now_ms=NOW)['local_receipt_id'] == before['local_receipt_id']
    assert len(world.network) == 3


@pytest.mark.parametrize('profile', ['support', 'restart'])
def test_managed_profile_import_initializes_same_store_once_without_r1_or_service(actual_world, monkeypatch, capsys, profile):
    from adapters.sdc.support_contract import canonical

    world = actual_world
    answers(monkeypatch, ['1', *world.expected.values(), '1', '1', '1'])
    assert sdc_cli.main(['connect', '--state-root', str(world.root), '--import-provision', str(world.file), '--customer', *CONFIRM]) == 0
    native_before, network_before, jobs_before = list(world.native.calls), list(world.network), list(world.jobs)
    vectors = json.loads((Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/provision/provision-vectors.json').read_text())
    initializers, descriptors = [], []
    def initialize(store, selected):
        os.fstat(store.fd)
        descriptors.append(store.fd)
        def initial():
            initializers.append(selected)
            # Explicit consumer stub: this oracle owns CLI/core lifetime only;
            # no support consent, crypto, credential or request is created here.
            names = ('support-state.json',) if selected == 'support' else ('restart-state.json', 'restart-planning.json')
            for name in names:
                store.write(name, {'root_fingerprint': store.root_fingerprint()})
        return provision.initialize_profile(store, profile=selected, now_ms=NOW, initializer=initial)
    monkeypatch.setattr(cli, '_initialize_profile', initialize)
    capsys.readouterr()
    for selected in (['support'] if profile == 'support' else ['support', 'restart']):
        with Store(world.root) as store:
            identity = store.read('state.json')
        bundle = copy.deepcopy(next(item['value'] for item in vectors['positive']
                                    if item['definition'] == 'Bundle' and item['value']['profile_id'] == selected))
        bundle.update(root_id=world.bundle['root_id'], device_id=identity['device_id'], claim_id=identity['claim_id'],
                      origin=PROFILE_TARGETS[selected][0], audience=PROFILE_TARGETS[selected][1])
        raw = canonical(bundle, max_bytes=16384).encode('ascii')
        # Independent synthetic Console target: distinct origins/audiences are
        # explicit fixture constants, not adopted by the caller from the raw.
        expected = {**world.expected, 'profile_id': selected, 'origin': PROFILE_TARGETS[selected][0],
                    'audience': PROFILE_TARGETS[selected][1], 'bundle_hash': hashlib.sha256(raw).hexdigest()}
        file = world.file.with_name(selected + '.json')
        file.write_bytes(raw)
        answers(monkeypatch, ['1', *expected.values(), '1'])
        assert sdc_cli.main(['connect', '--state-root', str(world.root), '--import-provision', str(file), *CONFIRM]) == 0
        with Store(world.root) as store:
            snapshot = provision.profile_snapshot(store, profile=selected, now_ms=NOW)
            assert snapshot['bundle'] == bundle and snapshot['bundle_hash'] == expected['bundle_hash']
            assert store._read_file(provision.NAME)['profiles'][selected]['raw'].encode('ascii') == raw
        with pytest.raises(OSError):
            os.fstat(descriptors[-1])
    assert initializers == (['support'] if profile == 'support' else ['support', 'restart'])
    assert world.native.calls == native_before and world.network == network_before and world.jobs == jobs_before
    out = capsys.readouterr().out
    assert ('mcs support connect' if profile == 'support' else 'mcs support plan-restart') in out
    assert 'このまま同じ領域で状態監視の登録' not in out


def test_profile_initialization_failure_cannot_recover_permission_by_cli_reimport(actual_world, monkeypatch, capsys):
    from adapters.sdc.support_contract import canonical

    world = actual_world
    answers(monkeypatch, ['1', *world.expected.values(), '1', '1', '1'])
    assert sdc_cli.main(['connect', '--state-root', str(world.root), '--import-provision', str(world.file), '--customer', *CONFIRM]) == 0
    baseline = (list(world.native.calls), list(world.network), list(world.jobs))
    with Store(world.root) as store:
        identity = store.read('state.json')
    vectors = json.loads((Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/provision/provision-vectors.json').read_text())
    bundle = copy.deepcopy(next(item['value'] for item in vectors['positive']
                                if item['definition'] == 'Bundle' and item['value']['profile_id'] == 'support'))
    bundle.update(root_id=world.bundle['root_id'], device_id=identity['device_id'], claim_id=identity['claim_id'],
                  audience=PROFILE_TARGETS['support'][1])
    raw = canonical(bundle, max_bytes=16384).encode('ascii')
    expected = {**world.expected, 'profile_id': 'support', 'origin': PROFILE_TARGETS['support'][0],
                'audience': PROFILE_TARGETS['support'][1], 'bundle_hash': hashlib.sha256(raw).hexdigest()}
    file = world.file.with_name('support-partial.json')
    file.write_bytes(raw)
    initializers, descriptors = [], []
    def initialize(store, selected):
        assert selected == 'support'
        descriptors.append(store.fd)
        def partial():
            initializers.append(selected)
            store.write('support-state.json', {'root_fingerprint': store.root_fingerprint(), 'partial': True})
            raise ValueError('SYNTHETIC_PRIVATE_INITIALIZER_FAILURE')
        return provision.initialize_profile(store, profile=selected, now_ms=NOW, initializer=partial)
    monkeypatch.setattr(cli, '_initialize_profile', initialize)
    capsys.readouterr()
    for _ in range(2):
        answers(monkeypatch, ['1', *expected.values(), '1'])
        assert sdc_cli.main(['connect', '--state-root', str(world.root), '--import-provision', str(file), *CONFIRM]) == 1
        with pytest.raises(OSError):
            os.fstat(descriptors[-1])
    assert initializers == ['support']
    assert (world.native.calls, world.network, world.jobs) == baseline
    with Store(world.root) as store:
        assert store.read('support-state.json')['partial'] is True
        assert store._read_file(provision.NAME)['profiles']['support']['raw'].encode('ascii') == raw
    captured = capsys.readouterr()
    assert 'SYNTHETIC_PRIVATE_INITIALIZER_FAILURE' not in captured.out + captured.err


@pytest.fixture
def paused_managed_world(actual_world, monkeypatch):
    import hmac

    from adapters.sdc.connector import Connector
    from adapters.sdc.protocol import strict_json
    from adapters.sdc.transport import TransportError

    world = actual_world
    answers(monkeypatch, ['1', *world.expected.values(), '1', '1', '1'])
    assert sdc_cli.main(['connect', '--state-root', str(world.root), '--import-provision', str(world.file), '--customer', *CONFIRM]) == 0
    assert [path for path, *_ in world.network] == ['/v2/enrollments/claim', '/v2/enrollments/status', '/v2/enrollments/confirm']
    with Store(world.root) as store:
        identity = store.read('state.json')
        credential = store.read('credential')
        config = store.read('endpoint.json')

    calls = []
    class Peer:
        def __init__(self, selected):
            assert selected == config

        def send(self, path, raw, headers):
            assert path in ('/v2/heartbeats', '/v2/keys/status'), 'resume must not issue a new claim or key'
            assert headers['X-SDC-Device'] == identity['device_id']
            assert headers['X-SDC-Key-ID'] == identity['key_id']
            source = '\n'.join(('SDC-HMAC-v1', config['audience'], 'POST', path,
                                identity['device_id'], identity['key_id'], headers['X-SDC-Time'],
                                headers['X-SDC-Nonce'], hashlib.sha256(raw).hexdigest())).encode('ascii')
            assert headers['X-SDC-Signature'] == hmac.new(bytes.fromhex(credential['key']), source, hashlib.sha256).hexdigest()
            calls.append((path, raw))
            if path == '/v2/heartbeats':
                raise TransportError(401, 'UNAUTHENTICATED')
            assert set(strict_json(raw)) == {'rotation_id'}
            return {'state': 'missing', 'key_id': None}

    monkeypatch.setattr(sdc_cli, 'Transport', Peer)
    with Store(world.root) as store:
        with store.lock():
            result = Connector(store, Peer(config), clock=lambda: NOW / 1000).tick({
                'mode': 'standalone', 'mcs_process': 'running', 'python_version': '3.13.0',
            })
        assert result == 'SDC_REQUEST_DENIED'
        paused = store.read('state.json')
        assert paused['state'] == 'active' and paused['enabled'] is False
        assert paused['sequence'] == 1 and paused['sending'].encode() == calls[0][1]
        assert store.read('credential') == credential
    world.paused, world.peer_calls = paused, calls
    world.credential_items = copy.deepcopy(world.native.items)
    world.native_offset, world.job_offset = len(world.native.calls), len(world.jobs)
    world.provision_bytes = (world.root / provision.NAME).read_bytes()
    return world


@pytest.mark.parametrize('choice', ['1', '2'])
def test_managed_401_pause_resumes_only_after_human_choice_with_same_identity(paused_managed_world, monkeypatch, choice):
    world = paused_managed_world
    prompts = []
    def choose(prompt):
        prompts.append(prompt)
        return choice
    monkeypatch.setattr('builtins.input', choose)
    assert sdc_cli.main(['connect', '--state-root', str(world.root)]) == 0
    assert len(prompts) == 1 and '同じ端末で送信を再開' in prompts[0]
    assert world.native.items == world.credential_items
    assert all(call[0] == 'copy' for call in world.native.calls[world.native_offset:])
    assert (world.root / provision.NAME).read_bytes() == world.provision_bytes
    with Store(world.root) as store:
        after = store.read('state.json')
    if choice == '1':
        assert after == {**world.paused, 'enabled': True, 'next_due': 0, 'last_result': 'SDC_EXPLICIT_RESUME'}
        assert [path for path, _ in world.peer_calls] == ['/v2/heartbeats', '/v2/keys/status']
        assert world.jobs[world.job_offset:] == [(True, world.root)]
    else:
        assert after == world.paused
        assert [path for path, _ in world.peer_calls] == ['/v2/heartbeats']
        assert world.jobs[world.job_offset:] == []
    assert after['claim_id'] == world.paused['claim_id']
    assert after['key_id'] == world.paused['key_id']
    assert after['sequence'] == world.paused['sequence']
    assert after['sending'] == world.paused['sending']
    assert len(world.network) == 3  # Only the original claim/status/confirm.


@pytest.mark.parametrize('field,value', [
    ('disconnected', True), ('central_suspended', True), ('enrollment_hold', True),
    ('rotation_id', '33333333-3333-4333-8333-333333333333'),
    ('claim_id', 'invalid-claim'), ('key_id', 'invalid-key'),
    ('state', 'suspended'), ('state', 'revoked'),
])
def test_managed_explicit_stop_or_invalid_identity_cannot_enter_resume(paused_managed_world, monkeypatch, field, value):
    world = paused_managed_world
    stopped = {**world.paused, field: value}
    with Store(world.root) as store:
        with store.lock():
            store.write('state.json', stopped)
    native_before = list(world.native.calls)
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('explicit stop or invalid identity cannot offer resume'))
    assert sdc_cli.main(['connect', '--state-root', str(world.root)]) == 1
    assert world.native.calls == native_before
    assert world.native.items == world.credential_items
    assert world.jobs[world.job_offset:] == []
    assert [path for path, _ in world.peer_calls] == ['/v2/heartbeats']
    assert len(world.network) == 3
    assert (world.root / provision.NAME).read_bytes() == world.provision_bytes
    with Store(world.root) as store:
        assert store.read('state.json') == stopped
