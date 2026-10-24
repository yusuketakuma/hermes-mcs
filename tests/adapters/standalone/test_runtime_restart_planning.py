"""Synthetic passive owner observations; no service, HTTP, Keychain or execution."""
import asyncio
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import threading
from types import SimpleNamespace

import pytest
import sdc_support_signatures

from adapters.sdc import restart_client
from adapters.sdc import provision as managed_provision
from adapters.sdc import restart_planning as planning
from adapters.sdc import restart_state as consent
from adapters.sdc import storage
from adapters.sdc import support_client, support_state
from adapters.sdc.support_contract import canonical, hash_consent_binding
from adapters.sdc.transport import Transport
from mcs_standalone import __main__ as entry
from mcs_standalone import runtime

V = json.loads((Path(__file__).resolve().parents[3]
                / 'adapters/sdc/contracts/support/restart-wire-vectors.json').read_text())
NOW = V['now']
CANARY = 'SYNTHETIC_PRIVATE_ERROR_NOT_FOR_OUTPUT'
ERROR = '^standalone_restart_planning_unavailable$'


def write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def request(host, *, phase='sample'):
    return {'request_id': '00000000-0000-4000-8000-000000000099', 'planning_owner': 'a' * 32,
            'phase': phase, 'expected_generation': None if phase == 'sample' else host.generation,
            'expires_at': NOW + 300000,
            'expected': {k: copy.deepcopy(v) for k, v in V['expected'].items()
                         if k not in ('service_revision', 'params')}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    root.mkdir(mode=0o700)
    (root / 'data').mkdir(mode=0o700)
    cfg = {'runtime_mode': 'standalone'}
    write(root / 'config.json', cfg)
    monkeypatch.setattr(runtime.time, 'time', lambda: NOW / 1000)
    host = runtime.Runtime(root, cfg)
    fd = os.open(host.data / 'standalone.lock', os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    host._planning_host_fd = fd
    (host.data / 'update.lock').touch(mode=0o600)
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('real child forbidden'))
    yield SimpleNamespace(host=host, root=root, cfg=cfg, private=tmp_path / 'private', fd=fd)
    host._planning_host_fd = None
    os.close(fd)


def test_only_real_owner_facts_are_returned_without_reservation_or_cleanup(world, monkeypatch):
    host = world.host
    for name in ('_updating', '_probe_marker', 'reserve_restart'):
        monkeypatch.setattr(host, name, lambda *_: pytest.fail('passive observation cannot mutate'))
    monkeypatch.setattr(runtime.update_journal, 'load_state', lambda *_: pytest.fail('unbounded journal read'))
    before = {p.name: p.read_bytes() for p in host.data.iterdir()}
    value = host._restart_planning_preflight(request(host))
    assert value == {'schema_version': 'sdc.support.restart-preflight.v1', 'product': 'hermes-mcs',
                     **{k: V['expected'][k] for k in ('tenant', 'installation', 'device_id', 'environment', 'case_id')},
                     'service_id': 'mcs.standalone', 'runtime_generation': host.generation,
                     'started_at': NOW, 'observed_at': NOW, 'owner_alive': True,
                     'update_in_progress': False, 'command_intake_in_progress': False, 'restarting': False}
    assert host._restart_reservation is None and host._restart_execution_owner is None
    assert host.native_admission.snapshot()['open'] and host.delivery_admission.snapshot()['open']
    assert not host.restarting and host._support_restart is None
    assert {p.name: p.read_bytes() for p in host.data.iterdir()} == before


@pytest.mark.parametrize('phase', ['sample', 'refresh', 'confirm'])
def test_same_generation_sample_and_explicit_rechecks(world, phase):
    assert world.host._restart_planning_preflight(request(world.host, phase=phase))['runtime_generation'] == world.host.generation


@pytest.mark.parametrize('transport', ['slack', 'discord'])
def test_configured_connector_requires_its_actual_ready_owner(world, monkeypatch, transport):
    host = world.host
    host.cfg['notify'] = {'interactive': transport}
    monkeypatch.setattr(runtime.config, 'load', lambda _root: copy.deepcopy(host.cfg))
    identity = host.delivery_admission.begin_connector()
    host._delivery_identity = identity
    host.connector_started(NOW / 1000)
    with pytest.raises(RuntimeError, match=ERROR):
        host._restart_planning_preflight(request(host))
    host.delivery_admission.connector_ready(identity)
    assert host._restart_planning_preflight(request(host))['owner_alive'] is True
    host.delivery_admission.connector_closing(identity)
    with pytest.raises(RuntimeError, match=ERROR):
        host._restart_planning_preflight(request(host))


@pytest.mark.parametrize('change', ['unserved', 'thread', 'successor', 'host_lock_replaced', 'config'])
def test_owner_identity_and_current_configuration_are_required(world, change):
    host = world.host
    req = request(host, phase='confirm')
    if change == 'unserved':
        host._planning_host_fd = None
    elif change == 'thread':
        host._reservation_owner_thread = threading.get_ident() + 1
    elif change == 'successor':
        host.generation = '11111111111141118111111111111111'
    elif change == 'host_lock_replaced':
        (host.data / 'standalone.lock').unlink()
        (host.data / 'standalone.lock').touch(mode=0o600)
    else:
        write(host.root / 'config.json', {**world.cfg, 'synthetic_changed': True})
    with pytest.raises(RuntimeError, match=ERROR):
        host._restart_planning_preflight(req)


@pytest.mark.parametrize('busy', ['restarting', 'signal', 'reservation', 'uncertain', 'execution',
                                 'native_closed', 'delivery_closed', 'queued', 'running', 'delivery_active',
                                 'orphan', 'probe', 'adopted', 'connector_unknown', 'cmd', 'cmd_int', 'update', 'lineworks'])
def test_busy_or_unobserved_work_never_becomes_idle(world, busy):
    host = world.host
    if busy == 'restarting':
        host.restarting = True
    elif busy == 'signal':
        host._restart_signal_seen = True
    elif busy == 'reservation':
        host._restart_reservation = object()
    elif busy == 'uncertain':
        host._restart_reservation_uncertain = True
    elif busy == 'execution':
        host._restart_execution_owner = object()
    elif busy == 'native_closed':
        host.native_admission.close()
    elif busy == 'delivery_closed':
        host.delivery_admission.close()
    elif busy in ('queued', 'running'):
        setattr(host.native_admission, '_' + busy, 1)
    elif busy == 'delivery_active':
        host.delivery_admission._active_passes = 1
    elif busy == 'orphan':
        host.orphan_candidates['synthetic'] = 1234
    elif busy == 'probe':
        host.identity_probe = object()
    elif busy == 'adopted':
        host.children['synthetic'] = {'process': runtime._Adopted(1234, ['synthetic'])}
    elif busy == 'connector_unknown':
        host.connector['state'] = 'running'
    else:
        host.children[busy] = {'process': object()}
    with pytest.raises(RuntimeError, match=ERROR):
        host._restart_planning_preflight(request(host))
    assert host._support_restart is None


@pytest.mark.parametrize('problem', ['missing_lock', 'symlink_lock', 'fifo_lock', 'public_lock', 'held_lock',
                                    'marker', 'broken_journal', 'null_journal', 'large_journal', 'pending_journal',
                                    'applying_journal', 'empty_applying_journal', 'corrupt_journal',
                                    'symlink_journal', 'fifo_journal', 'public_journal',
                                    'broken_restart', 'null_restart', 'duplicate_restart', 'current_restart'])
def test_unknown_or_pending_control_files_remain_untouched(world, problem):
    host = world.host
    lock, journal = host.data / 'update.lock', host.data / 'update_state.json'
    extra_fd = None
    if problem in ('missing_lock', 'symlink_lock', 'fifo_lock'):
        lock.unlink()
        if problem == 'symlink_lock':
            lock.symlink_to(host.data / 'absent')
        elif problem == 'fifo_lock':
            os.mkfifo(lock, 0o600)
    elif problem == 'public_lock':
        lock.chmod(0o644)
    elif problem == 'held_lock':
        extra_fd = os.open(lock, os.O_RDONLY)
        fcntl.flock(extra_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif problem == 'marker':
        write(host.data / runtime.UPDATE_MARKER_NAME, {'synthetic': 'pending'})
    elif problem in ('symlink_journal', 'fifo_journal'):
        if problem == 'symlink_journal':
            journal.symlink_to(host.data / 'absent')
        else:
            os.mkfifo(journal, 0o600)
    elif problem.endswith('_journal'):
        payload = {'broken_journal': '{' + CANARY, 'null_journal': 'null',
                   'large_journal': ' ' * (1024 * 1024 + 1),
                   'pending_journal': '{"v":1,"stages":[{"stage":"lock","at":1}]}',
                   'applying_journal': '{"v":1,"applying":{"sha":"synthetic"}}',
                   'empty_applying_journal': '{"v":1,"applying":{}}',
                   'corrupt_journal': '{"v":1,"_corrupt":true}',
                   'public_journal': '{"v":1}'}[problem]
        journal.write_text(payload)
        journal.chmod(0o644 if problem == 'public_journal' else 0o600)
    else:
        path = host.data / runtime.service.RESTART_FILE
        if problem == 'current_restart':
            write(path, {'generation': host.generation, 'request_id': 'a' * 32, 'requested_at': NOW / 1000})
        else:
            path.write_text({'broken_restart': '{' + CANARY, 'null_restart': 'null',
                             'duplicate_restart': '{"generation":"old","generation":"old"}'}[problem])
            path.chmod(0o600)
    before = {p.name: (p.lstat().st_mode, p.lstat().st_ino, p.lstat().st_size) for p in host.data.iterdir()}
    try:
        with pytest.raises(RuntimeError, match=ERROR) as error:
            host._restart_planning_preflight(request(host))
        assert CANARY not in str(error.value)
        assert {p.name: (p.lstat().st_mode, p.lstat().st_ino, p.lstat().st_size)
                for p in host.data.iterdir()} == before
        assert host.native_admission.snapshot()['open'] and not host.restarting
    finally:
        if extra_fd is not None:
            os.close(extra_fd)


def test_old_valid_restart_marker_is_not_current_owner_stop_or_cleaned(world):
    path = world.host.data / runtime.service.RESTART_FILE
    write(path, {'generation': 'b' * 32, 'request_id': 'a' * 32, 'requested_at': NOW / 1000 - 1000})
    before = path.read_bytes()
    assert world.host._restart_planning_preflight(request(world.host))['owner_alive'] is True
    assert path.read_bytes() == before


@pytest.mark.parametrize('change', ['signal', 'configuration', 'generation', 'intake', 'expired'])
def test_change_during_passive_probe_rejects_response(world, monkeypatch, change):
    host = world.host
    probe = host._planning_controls_clear
    def changing_probe():
        probe()
        if change == 'signal':
            host._restart_signal(signal.SIGTERM, None)
        elif change == 'configuration':
            write(host.root / 'config.json', {**world.cfg, 'synthetic_changed': True})
        elif change == 'generation':
            host.generation = '11111111111141118111111111111111'
        elif change == 'intake':
            host.native_admission._queued = 1
        else:
            monkeypatch.setattr(runtime.time, 'time', lambda: NOW / 1000 + 300)
    monkeypatch.setattr(host, '_planning_controls_clear', changing_probe)
    with pytest.raises(RuntimeError, match=ERROR):
        host._restart_planning_preflight(request(host))


def test_update_lock_swap_is_unknown_and_probe_descriptor_is_closed(world, monkeypatch):
    host = world.host
    opened, opening, reading = [], runtime.os.open, runtime.safe_io.read_private_bytes
    def track(path, *args, **kwargs):
        fd = opening(path, *args, **kwargs)
        if Path(path).name == 'update.lock' and not args[0] & os.O_CREAT:
            opened.append(fd)
        return fd
    def swap(path, *args):
        if Path(path).name == 'update_state.json':
            (host.data / 'update.lock').unlink()
            (host.data / 'update.lock').touch(mode=0o600)
        return reading(path, *args)
    monkeypatch.setattr(runtime.os, 'open', track)
    monkeypatch.setattr(runtime.safe_io, 'read_private_bytes', swap)
    with pytest.raises(RuntimeError, match=ERROR):
        host._restart_planning_preflight(request(host))
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize('change', ['extra', 'expected_extra', 'expired', 'wrong_generation', 'refresh_without_generation'])
def test_invalid_or_expired_request_does_not_probe_controls(world, monkeypatch, change):
    host, req = world.host, request(world.host)
    if change == 'extra':
        req['authority'] = CANARY
    elif change == 'expected_extra':
        req['expected']['params'] = {'expected_runtime_generation': host.generation}
    elif change == 'expired':
        req['expires_at'] = NOW
    elif change == 'wrong_generation':
        req['expected_generation'] = 'b' * 32
    else:
        req['phase'] = 'refresh'
    monkeypatch.setattr(host, '_planning_controls_clear', lambda: pytest.fail('invalid request cannot probe'))
    with pytest.raises(RuntimeError, match=ERROR):
        host._restart_planning_preflight(req)


def configure_private(world):
    with storage.Store(world.private, create=True):
        pass
    world.cfg['sdc'] = {'state_root': str(world.private)}
    write(world.root / 'config.json', world.cfg)


def configure_managed_private(world, *, profiles=('monitor', 'support', 'restart')):
    """Seed exact synthetic profile evidence; support consumer is an explicit metadata stub."""
    vectors = json.loads((Path(__file__).resolve().parents[3]
                         / 'adapters/sdc/contracts/provision/provision-vectors.json').read_text())
    assert vectors['now'] == NOW
    snapshots = {}
    with storage.Store(world.private, create_new=True) as store:
        root_id = managed_provision.prepare_root(store, confirm_human=True, reason='fixture-root', now_ms=NOW)['root_id']
        for profile in profiles:
            bundle = copy.deepcopy(next(row['value'] for row in vectors['positive']
                                        if row['definition'] == 'Bundle' and row['value']['profile_id'] == profile))
            bundle.update(root_id=root_id, origin='https://' + profile + '.invalid', audience='fixture:' + profile)
            raw = canonical(bundle, max_bytes=16384).encode('ascii')
            expected = {k: bundle[k] for k in managed_provision.EXPECTED_FIELDS if k != 'bundle_hash'}
            expected['bundle_hash'] = hashlib.sha256(raw).hexdigest()
            plan = managed_provision.prepare_import(store, raw, expected=expected, now_ms=NOW)
            managed_provision.commit_import(store, plan, confirm_human=True, reason='fixture-import', now_ms=NOW)
            if profile == 'monitor':
                managed_provision.begin_registration(store, now_ms=NOW)
                store.write('state.json', {'state': 'active', 'enabled': True,
                    'device_id': '00000000-0000-4000-8000-000000000005',
                    'claim_id': '00000000-0000-4000-8000-000000000006',
                    'key_id': '00000000-0000-4000-8000-000000000007'})
            else:
                def initialize(selected=profile):
                    if selected == 'support':
                        store.write('support-state.json', {'root_fingerprint': store.root_fingerprint()})
                    else:
                        consent.initialize(store)
                        planning.initialize(store)
                managed_provision.initialize_profile(store, profile=profile, now_ms=NOW, initializer=initialize)
            snapshots[profile] = managed_provision.profile_snapshot(store, profile=profile, now_ms=NOW)
    world.cfg['sdc'] = {'state_root': str(world.private)}
    write(world.root / 'config.json', world.cfg)
    return snapshots


def forbid_metadata_authority(monkeypatch):
    syntax_calls = []
    def forbidden(*_a, **_k):
        pytest.fail('Runtime metadata cannot acquire keys, verify signatures, send or execute provision')
    original_read = storage.Store._read_file
    def metadata_read(store, name, *args, **kwargs):
        assert name not in ('credential', 'credential-store.json')
        return original_read(store, name, *args, **kwargs)
    monkeypatch.setattr(storage.Store, '_read_file', metadata_read)
    monkeypatch.setattr(storage.Store, '_keychain', forbidden)
    monkeypatch.setattr(storage, 'KeychainCredentialStore', forbidden)
    monkeypatch.setattr(Transport, 'send', forbidden)
    monkeypatch.setattr(support_client, '_supplier', forbidden)
    monkeypatch.setattr(restart_client, '_supplier', forbidden)
    # These two validators are also the C6 pure-Python DER/SPKI schema checks.
    # Permit the real parser; injected suppliers and actual verification stay forbidden.
    for name in ('validate_pins', 'validate_restart_pins'):
        original = getattr(sdc_support_signatures, name)
        def syntax(pins, *, purpose, now_ms, _original=original, _name=name):
            syntax_calls.append((_name, purpose))
            return _original(pins, purpose=purpose, now_ms=now_ms)
        monkeypatch.setattr(sdc_support_signatures, name, syntax)
    for name in ('verify_envelope', 'verify_restart_envelope', '_verify_envelope', '_verify_pss'):
        monkeypatch.setattr(sdc_support_signatures, name, forbidden)
    return syntax_calls


@pytest.mark.parametrize('provision', [None, False, True, {}])
def test_unprovisioned_hook_does_not_open_store_sample_or_read_keys(world, monkeypatch, provision):
    configure_private(world)
    monkeypatch.setattr(storage, 'Store', lambda *_a, **_k: pytest.fail('unprovisioned Store access'))
    monkeypatch.setattr(planning, 'respond_once', lambda *_a, **_k: pytest.fail('unprovisioned response'))
    monkeypatch.setattr(world.host, '_restart_planning_preflight', lambda *_a: pytest.fail('unprovisioned sample'))
    assert world.host._respond_restart_planning(provision) is False


@pytest.mark.parametrize('injected', [False, True, {}])
def test_invalid_injected_gate_does_not_fall_back_to_present_managed_profile(world, monkeypatch, injected):
    configure_managed_private(world)
    monkeypatch.setattr(storage, 'Store', lambda *_a, **_k: pytest.fail('invalid gate cannot open managed Store'))
    monkeypatch.setattr(planning, 'respond_once', lambda *_a, **_k: pytest.fail('invalid gate cannot sample'))
    assert world.host._respond_restart_planning(injected) is False


@pytest.mark.parametrize('kind', ['absent_root', 'missing', 'symlink', 'directory'])
def test_default_managed_gate_rejects_missing_or_nonregular_file_before_store(world, monkeypatch, kind):
    if kind != 'absent_root':
        configure_private(world)
        path = world.private / managed_provision.NAME
        if kind == 'directory':
            path.mkdir(mode=0o700)
        elif kind == 'symlink':
            target = world.private / 'synthetic-target'
            target.write_text('{}')
            path.symlink_to(target)
    else:
        world.cfg['sdc'] = {'state_root': str(world.private)}
        write(world.root / 'config.json', world.cfg)
    monkeypatch.setattr(storage, 'Store', lambda *_a, **_k: pytest.fail('missing/nonregular profile cannot open Store'))
    monkeypatch.setattr(planning, 'respond_once', lambda *_a, **_k: pytest.fail('missing/nonregular profile cannot sample'))
    assert world.host._respond_restart_planning(None) is False
    if kind == 'absent_root':
        assert not world.private.exists()


@pytest.mark.parametrize('kind', ['not-installed', 'missing', 'symlink', 'fifo', 'directory'])
def test_managed_without_regular_planning_file_repeats_without_store_or_authority(world, monkeypatch, kind):
    configure_managed_private(world, profiles=('monitor',) if kind == 'not-installed' else ('monitor', 'support', 'restart'))
    path = world.private / planning.NAME
    if kind != 'not-installed':
        path.unlink()
        if kind == 'symlink':
            target = world.private / 'synthetic-planning-target'
            write(target, {'synthetic': 'not-a-consumer'})
            path.symlink_to(target)
        elif kind == 'fifo':
            os.mkfifo(path, 0o600)
        elif kind == 'directory':
            path.mkdir(mode=0o700)
    syntax_calls = forbid_metadata_authority(monkeypatch)
    opens, samples, snapshots = [], [], []
    def opening(*_args, **_kwargs):
        opens.append(True)
        pytest.fail('missing/nonregular planning file cannot open Store')
    def sample(*_args, **_kwargs):
        samples.append(True)
        pytest.fail('missing/nonregular planning file cannot sample')
    def snapshot(*_args, **_kwargs):
        snapshots.append(True)
        pytest.fail('missing/nonregular planning file cannot parse profiles')
    monkeypatch.setattr(storage, 'Store', opening)
    monkeypatch.setattr(planning, 'respond_once', sample)
    monkeypatch.setattr(world.host, '_restart_planning_preflight', sample)
    monkeypatch.setattr(managed_provision, 'profile_snapshot', snapshot)
    for _ in range(20):
        assert world.host._respond_restart_planning(None) is False
    assert opens == samples == snapshots == syntax_calls == []
    assert world.host._restart_reservation is None and world.host._restart_execution_owner is None


@pytest.mark.parametrize('pending', [False, True])
def test_managed_none_hook_uses_one_current_metadata_store_and_no_authority(world, monkeypatch, pending):
    snapshots = configure_managed_private(world)
    syntax_calls = forbid_metadata_authority(monkeypatch)
    opened, sampled = [], []
    original_store = storage.Store
    def open_store(*args, **kwargs):
        store = original_store(*args, **kwargs)
        opened.append(store.fd)
        return store
    def respond(store, *, sample, now):
        os.fstat(store.fd)
        assert now() == NOW
        assert managed_provision.profile_snapshot(store, profile='restart', now_ms=NOW) == snapshots['restart']
        if pending:
            sampled.append(sample(request(world.host)))
        os.fstat(store.fd)
        return pending
    monkeypatch.setattr(storage, 'Store', open_store)
    monkeypatch.setattr(planning, 'respond_once', respond)
    assert world.host._respond_restart_planning(None) is pending
    assert len(opened) == 1 and len(sampled) == int(pending)
    assert {name for name, _ in syntax_calls} == {'validate_pins', 'validate_restart_pins'}
    assert world.host._restart_reservation is None and world.host._restart_execution_owner is None
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize('invalid', ['corrupt', 'corrupt_planning', 'expired', 'changed_scope', 'missing_consumer'])
def test_managed_none_hook_rejects_invalid_metadata_and_closes_store(world, monkeypatch, invalid):
    snapshots = configure_managed_private(world)
    if invalid == 'corrupt':
        write(world.private / managed_provision.NAME, {'synthetic': 'corrupt'})
    elif invalid == 'corrupt_planning':
        write(world.private / planning.NAME, {'synthetic': 'corrupt'})
    elif invalid == 'expired':
        monkeypatch.setattr(runtime.time, 'time', lambda: snapshots['restart']['bundle']['expires_at'] / 1000)
    elif invalid == 'missing_consumer':
        (world.private / 'restart-state.json').unlink()
    else:
        with storage.Store(world.private) as store:
            identity = store.read('state.json')
            identity['device_id'] = '11111111-1111-4111-8111-111111111111'
            store.write('state.json', identity)
    forbid_metadata_authority(monkeypatch)
    opened, original_store = [], storage.Store
    def open_store(*args, **kwargs):
        store = original_store(*args, **kwargs)
        opened.append(store.fd)
        return store
    monkeypatch.setattr(storage, 'Store', open_store)
    monkeypatch.setattr(planning, 'respond_once', lambda *_a, **_k: pytest.fail('invalid profile cannot sample'))
    assert world.host._respond_restart_planning(None) is False and len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize('interrupt', [ValueError, KeyboardInterrupt])
def test_managed_metadata_response_failure_still_closes_owned_store(world, monkeypatch, interrupt):
    configure_managed_private(world)
    forbid_metadata_authority(monkeypatch)
    descriptors = []
    error = interrupt(CANARY)
    def respond(store, **_kwargs):
        descriptors.append(store.fd)
        raise error
    monkeypatch.setattr(planning, 'respond_once', respond)
    if interrupt is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt) as caught:
            world.host._respond_restart_planning(None)
        assert caught.value is error
    else:
        assert world.host._respond_restart_planning(None) is False
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.parametrize('pending', [False, True])
def test_hook_borrows_one_store_until_response_finishes(world, monkeypatch, pending):
    configure_private(world)
    calls, fds = [], []
    def provision():
        pytest.fail('Runtime must not execute its provision marker')
    def respond(store, *, sample, now):
        assert now() == NOW
        os.fstat(store.fd)
        fds.append(store.fd)
        if pending:
            calls.append(sample(request(world.host)))
            os.fstat(store.fd)
        return pending
    monkeypatch.setattr(planning, 'respond_once', respond)
    assert world.host._respond_restart_planning(provision) is pending
    assert len(calls) == int(pending) and len(fds) == 1
    with pytest.raises(OSError):
        os.fstat(fds[0])


@pytest.mark.parametrize('customer', [False, True])
def test_real_pending_caller_reaches_runtime_once_and_successor_cannot_replace_answer(world, monkeypatch, customer):
    configure_private(world)
    pin_calls = []
    r4a_pins, restart_pins = [{'synthetic': 'r4a'}], [{'synthetic': 'restart'}]
    def check(pins, *, purpose, now_ms):
        assert now_ms == NOW
        assert pins == (r4a_pins if purpose == 'consent-preview' else restart_pins)
        pin_calls.append(purpose)
    monkeypatch.setattr(sdc_support_signatures, 'validate_pins', check)
    monkeypatch.setattr(sdc_support_signatures, 'validate_restart_pins', check)
    monkeypatch.setattr(storage, 'KeychainCredentialStore', lambda *_a, **_k: pytest.fail('no real Keychain'))
    identity = {k: V['expected'][k] for k in ('claim_id', 'device_id', 'key_id')}
    key = hashlib.sha256(b'fully synthetic planning credential').hexdigest()
    keychain_reads = []
    def read_key():
        keychain_reads.append(True)
        return json.dumps({'key_id': identity['key_id'], 'key': key}).encode('ascii')
    fake_keychain = SimpleNamespace(read=read_key, write=lambda _raw: None)

    def observe_without_authority():
        saved_reads, saved_pins = list(keychain_reads), list(pin_calls)
        read, read_file = storage.Store.read, storage.Store._read_file
        def forbidden(*_args, **_kwargs):
            pytest.fail('Runtime metadata must not acquire credentials, execute provision, crypto or HTTP')
        def metadata_read(store, name, *args, **kwargs):
            assert name not in ('credential', 'credential-store.json')
            return read(store, name, *args, **kwargs)
        def metadata_file(store, name, *args, **kwargs):
            assert name not in ('credential', 'credential-store.json')
            return read_file(store, name, *args, **kwargs)
        with monkeypatch.context() as guard:
            guard.setattr(storage.Store, 'read', metadata_read)
            guard.setattr(storage.Store, '_read_file', metadata_file)
            guard.setattr(storage.Store, '_keychain', forbidden)
            guard.setattr(support_client, '_supplier', forbidden)
            guard.setattr(restart_client, '_supplier', forbidden)
            for name in ('validate_pins', 'validate_restart_pins', 'verify_envelope', 'verify_restart_envelope', '_verify_pss'):
                guard.setattr(sdc_support_signatures, name, forbidden)
            guard.setattr(Transport, 'send', forbidden)
            result = world.host._respond_restart_planning(forbidden)
        assert keychain_reads == saved_reads and pin_calls == saved_pins
        return result

    with storage.Store(world.private, keychain_factory=lambda _account: fake_keychain) as store:
        monitor = {'origin': 'https://synthetic-monitor.invalid', 'audience': V['expected']['audience'],
                   'environment': V['expected']['environment']}
        target = {**monitor, 'origin': 'https://synthetic-support.invalid'}
        store.write('endpoint.json', monitor)
        if customer:
            store.enable_customer()
        store.write('state.json', {**identity, 'state': 'active', 'enabled': True})
        if not customer:
            store.write('credential', {'key_id': identity['key_id'], 'key': key})
        binding = store.enrollment_binding()
        provision_value = {'root_fingerprint': binding['root_fingerprint'], 'monitor_endpoint_hash': binding['endpoint_hash'],
                           'support_endpoint': target, 'pins': restart_pins}
        def provision():
            return copy.deepcopy(provision_value)
        store.write('support-bootstrap.json', {'schema_version': support_client.BOOTSTRAP,
                    **provision_value, 'pins': r4a_pins})
        parent_current = support_client._supplier(store, check, runtime.time.time, {})
        before = support_state.prepare(store, binding_supplier=parent_current, now_ms=NOW, initialize=True)
        preview = {'schema_version': 'sdc.support.consent-preview.v1', 'product': 'hermes-mcs',
                   **request(world.host)['expected'], 'preview_id': '00000000-0000-4000-8000-000000000097',
                   'purpose_code': 'HEALTH_CHECK', 'capability_revision': 'sdc-support-capabilities-v1',
                   'diagnostic_ids': ['runtime.status.v1'], 'operator_refs': ['a' * 64], 'approved_by': 'b' * 64,
                   'consent_revision': 'sdc-support-consent-v1', 'issued_at': NOW, 'not_before': NOW,
                   'expires_at': NOW + 300000, 'consent_expires_at': NOW + 1800000}
        preview['consent_binding_hash'] = hash_consent_binding(preview)
        accepted = support_state.confirm_consent(store, preview, expected_owner_generation=before['owner_generation'],
            expected_binding=before['binding'], binding_supplier=parent_current, now_ms=NOW, confirm_human=True)
        accept = support_state.begin_accept(store, expected_owner_generation=accepted['owner_generation'],
                                           binding_supplier=parent_current, now_ms=NOW)
        support_state.record_accept_ack(store, accept, {**json.loads(accept['raw']), 'case_id': preview['case_id'],
            'state': 'accepted', 'expires_at': preview['consent_expires_at'],
            'request_id': '00000000-0000-4000-8000-000000000098'})
        consent.initialize(store)
        planning.initialize(store)
        current = restart_client._supplier(store, provision, check, check, runtime.time.time, {})
        frame = planning.snapshot(store, binding_supplier=current, now_ms=NOW)
        planning.begin(store, frame, binding_supplier=current, now_ms=NOW, confirm_human=True)
        read = planning.read_attempt(store, binding_supplier=current, now_ms=NOW)
        read_body = json.loads(read['raw'])
        planning.record_registration(store, read, {'schema_version': 'sdc.support.restart-registration-response.v1',
            'case_id': read_body['case_id'], 'local_receipt_id': read_body['local_receipt_id'],
            'request_id': '00000000-0000-4000-8000-000000000096', 'registration': copy.deepcopy(V['registration'])},
            binding_supplier=current, now_ms=NOW)
        planning.request_sample(store, 'sample', binding_supplier=current, now_ms=NOW)
        assert observe_without_authority() is True
        answered = store.read(planning.NAME)
        generation = world.host.generation
        assert answered['plan']['generation'] == generation
        assert answered['plan']['samples']['sample']['preflight']['runtime_generation'] == generation
        calls_before = list(pin_calls)
        assert observe_without_authority() is False
        assert pin_calls == calls_before and store.read(planning.NAME) == answered
        planning.confirm_inspection(store, binding_supplier=current, now_ms=NOW, confirm_human=True)
        planning.request_sample(store, 'refresh', binding_supplier=current, now_ms=NOW)
        world.host.generation = '11111111111141118111111111111111'
        assert observe_without_authority() is False
        saved = store.read(planning.NAME)
        assert saved['plan']['generation'] == generation
        assert saved['plan']['samples']['sample'] == answered['plan']['samples']['sample']
        assert saved['plan']['samples']['refresh']['preflight'] is None
        assert key not in json.dumps(saved) and store.read('restart-execution.json') is None
        assert world.host._support_restart is None and world.host._restart_reservation is None
        assert bool(keychain_reads) is customer
        os.fstat(store.fd)


def fake_signals(monkeypatch):
    originals = {sig: object() for sig in (signal.SIGTERM, signal.SIGINT)}
    handlers = dict(originals)
    def register(sig, handler):
        previous, handlers[sig] = handlers[sig], handler
        return previous
    monkeypatch.setattr(runtime.signal, 'signal', register)
    monkeypatch.setattr(runtime.signal, 'getsignal', handlers.__getitem__)
    return originals, handlers


def test_serving_loop_progresses_through_metadata_failure_and_restores_ownership(world, monkeypatch):
    configure_private(world)
    # The real serve below must acquire its own singleton, not borrow this fixture's owner.
    fcntl.flock(world.fd, fcntl.LOCK_UN)
    world.host._planning_host_fd = None
    originals, handlers = fake_signals(monkeypatch)
    calls, owners, ticks, progress = [], [], [], []
    def tick(host, **kwargs):
        ticks.append(True)
        if len(ticks) == 3:
            host.restarting = True
        return host.restarting
    def respond(store, *, sample, **kwargs):
        calls.append(True)
        owners.append(sample.__self__)
        if len(calls) == 1:
            raise ValueError(CANARY)
        assert sample(request(sample.__self__))['runtime_generation'] == sample.__self__.generation
        return True
    monkeypatch.setattr(runtime.Runtime, 'tick', tick)
    monkeypatch.setattr(planning, 'respond_once', respond)
    def forbidden(*_args, **_kwargs):
        pytest.fail('metadata loop cannot perform authority acquisition')
    monkeypatch.setattr(storage.Store, '_keychain', forbidden)
    monkeypatch.setattr(support_client, '_supplier', forbidden)
    monkeypatch.setattr(restart_client, '_supplier', forbidden)
    monkeypatch.setattr(sdc_support_signatures, 'validate_pins', forbidden)
    monkeypatch.setattr(sdc_support_signatures, 'validate_restart_pins', forbidden)
    monkeypatch.setattr(Transport, 'send', forbidden)
    original_sleep = asyncio.sleep
    async def yield_once(*_args):
        progress.append(True)
        await original_sleep(0)
    monkeypatch.setattr(runtime.asyncio, 'sleep', yield_once)
    assert asyncio.run(runtime.serve(world.root, world.cfg, restart_provision=forbidden)) == 0
    assert len(ticks) == 3 and len(calls) == 2 and len(progress) >= 2
    assert handlers == originals and all(host._planning_host_fd is None for host in owners)
    assert all(host._support_restart is None for host in owners)
    fcntl.flock(world.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_existing_entry_passes_only_explicit_provision_injection(monkeypatch):
    captured = []
    def provider():
        return None
    async def serve(root, cfg, connector, *, restart_provision):
        captured.append((root, cfg, connector, restart_provision))
        return 0
    monkeypatch.setattr(runtime, 'serve', serve)
    assert asyncio.run(entry._serve('synthetic-root', {}, '', restart_provision=provider)) == 0
    assert captured == [('synthetic-root', {}, None, provider)]
