"""Synthetic local reservation ownership; no remote authority or real service is exercised."""
import asyncio
import copy
import fcntl
import json
import os
import threading
from types import SimpleNamespace

import pytest

from mcs_standalone import runtime


@pytest.fixture
def world(tmp_path, monkeypatch):
    cfg = {'runtime_mode': 'standalone'}
    (tmp_path / 'data').mkdir(mode=0o700)
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    host = runtime.Runtime(tmp_path, cfg)
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('no real child allowed'))
    return host


def update_lock(host):
    fd = os.open(host.data / 'update.lock', os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def test_reservation_owns_existing_update_lock_without_run_lock_or_restart(world):
    host = world
    run_fd = os.open(host.data / 'run.lock', os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(run_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        handle = host.reserve_restart()
        assert not host.restarting
        assert host.native_admission.snapshot() == {'open': False, 'queued': 0, 'running': 0}
        with pytest.raises(BlockingIOError):
            update_lock(host)
        assert host.release_restart(handle) is True
        fd = update_lock(host)
        os.close(fd)
    finally:
        os.close(run_fd)


def test_existing_updater_is_busy_and_original_gate_reopens(world):
    host = world
    fd = update_lock(host)
    try:
        with pytest.raises(RuntimeError, match='^standalone_restart_update_busy$'):
            host.reserve_restart()
        assert host.native_admission.snapshot()['open']
        assert host._restart_reservation is None
    finally:
        os.close(fd)


def test_status_distinguishes_reservation_without_claiming_restart(world):
    host = world
    host._publish_status(1000, False)
    path = host.data / runtime.service.STATUS_FILE
    assert json.loads(path.read_text())['restart_reservation'] is None
    handle = host.reserve_restart()
    try:
        host._publish_status(1001, False)
        active = json.loads(path.read_text())
        assert active['restart_reservation'] == 'active'
        assert active['restarting'] is False and active['update_in_progress'] is False
    finally:
        host.release_restart(handle)
    host._publish_status(1002, False)
    assert json.loads(path.read_text())['restart_reservation'] is None


@pytest.mark.parametrize('job', ['cmd', 'cmd_int', 'update', 'lineworks'])
def test_spawned_protected_work_is_busy_even_without_marker(world, job):
    host = world
    child = SimpleNamespace(poll=lambda: None)
    host.children[job] = {'kind': 'core', 'stopping_at': None, 'process': child}
    with pytest.raises(RuntimeError, match='^standalone_restart_reservation_busy$'):
        host.reserve_restart()
    assert host.native_admission.snapshot()['open']
    assert host.children[job]['process'] is child
    assert not (host.data / 'update.lock').exists()


@pytest.mark.parametrize('unknown', ['orphan', 'probe', 'connector', 'adopted'])
def test_unobserved_owner_or_identity_is_not_ready(world, unknown):
    host = world
    if unknown == 'orphan':
        host.orphan_candidates['mcs_check'] = 1234
    elif unknown == 'probe':
        host.identity_probe = object()
    elif unknown == 'connector':
        host.connector['state'] = 'running'
    else:
        host.children['mcs_check'] = {'process': runtime._Adopted(1234, ['synthetic'])}
    with pytest.raises(RuntimeError, match='^(standalone_restart_reservation_busy|native_owner_unknown)$'):
        host.reserve_restart()
    assert host.native_admission.snapshot()['open']


@pytest.mark.parametrize('transport', ['slack', 'discord', 'lineworks'])
def test_configured_chat_owner_is_explicitly_unsupported(world, monkeypatch, transport):
    host = world
    host.cfg['notify'] = {'interactive': transport}
    monkeypatch.setattr(runtime.config, 'load', lambda _root: copy.deepcopy(host.cfg))
    with pytest.raises(RuntimeError, match='^native_owner_unknown$'):
        host.reserve_restart()
    assert host.native_admission.snapshot()['open']
    assert not (host.data / 'update.lock').exists()


@pytest.mark.parametrize('stage', ['queued', 'running'])
def test_actual_native_counter_busy_not_cancelled_waiter(world, stage):
    host = world
    ticket = host.native_admission._reserve()
    # No real worker is required to assert that queued work alone prevents READY.
    if stage == 'running':
        with host.native_admission._lock:
            host.native_admission._queued -= 1
            host.native_admission._running += 1
            ticket.stage = 'running'
    try:
        with pytest.raises(RuntimeError, match='^standalone_restart_reservation_busy$'):
            host.reserve_restart()
        assert host.native_admission.snapshot()[stage] == 1
    finally:
        if stage == 'queued':
            ticket.cancel_queued()
        else:
            with host.native_admission._lock:
                host.native_admission._running -= 1
                ticket.stage = 'done'


@pytest.mark.parametrize('payload', ['{broken', '{"v":1,"stages":[{"stage":"lock","at":1}]}', '{"v":1,"applying":{"sha":"synthetic"}}'])
def test_pending_or_corrupt_journal_is_read_only_and_busy(world, payload):
    host = world
    path = host.data / 'update_state.json'
    path.write_text(payload)
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='^standalone_restart_update_pending$'):
        host.reserve_restart()
    assert path.read_bytes() == before
    assert host.native_admission.snapshot()['open']
    os.close(update_lock(host))


def test_marker_is_never_cleaned_or_its_owner_probed(world, monkeypatch):
    host = world
    path = host.data / runtime.UPDATE_MARKER_NAME
    path.write_text('synthetic pending marker')
    monkeypatch.setattr(host, '_probe_marker', lambda *_a: pytest.fail('reservation cannot repair marker'))
    with pytest.raises(RuntimeError, match='^standalone_restart_update_pending$'):
        host.reserve_restart()
    assert path.read_text() == 'synthetic pending marker'
    assert host.native_admission.snapshot()['open']


@pytest.mark.parametrize('kind', ['fifo', 'symlink', 'foreign_owner'])
def test_nb_lock_rejects_unsafe_path_and_holds_gate_on_unknown(world, monkeypatch, kind):
    host = world
    path = host.data / 'update.lock'
    if kind == 'fifo':
        os.mkfifo(path, 0o600)
    elif kind == 'symlink':
        target = host.data / 'untouched'
        target.write_text('original')
        path.symlink_to(target)
    else:
        path.touch(mode=0o600)
        fstat = runtime.os.fstat
        inode = path.stat().st_ino
        def foreign(fd):
            info = fstat(fd)
            if info.st_ino != inode:
                return info
            return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1)
        monkeypatch.setattr(runtime.os, 'fstat', foreign)
    with pytest.raises(RuntimeError, match='^standalone_restart_update_lock_unknown$'):
        host.reserve_restart()
    assert not host.native_admission.snapshot()['open']
    assert host._restart_reservation_uncertain and host._restart_reservation is None
    if kind == 'symlink':
        assert target.read_text() == 'original'


def test_tick_preserves_schedule_inbox_and_tracks_existing_child_without_self_probe(world, monkeypatch):
    host = world
    (host.data / 'cmd_int').mkdir()
    (host.data / 'cmd_int' / 'synthetic.json').write_text('{}')
    host.last_minute = 100
    host.due = {'mcs_check': 6000}
    host.fired = {'mcs_check': 5940}
    host.drained['cmd_int'] = set()
    child = SimpleNamespace(pid=1001, poll=lambda: None,
                            terminate=lambda: pytest.fail('reservation is not drain'),
                            kill=lambda: pytest.fail('reservation is not drain'))
    host.children['mcs_check'] = {'process': child, 'kind': 'core', 'stopping_at': None}
    handle = host.reserve_restart()
    monkeypatch.setattr(host, '_updating', lambda *_a: pytest.fail('cannot reacquire own update.lock'))
    try:
        assert not host.tick(6000)
        host._schedule(6060)
        host.start('extract-0', 'background', 6000)
        assert host.fired == {'mcs_check': 5940} and not host.inflight
        assert host.drained['cmd_int'] == set()
        assert (host.data / 'cmd_int' / 'synthetic.json').exists()
        assert list(host.children) == ['mcs_check']
        child.poll = lambda: 0
        assert not host.tick(6001)
        assert not host.children and host.exits['mcs_check']['last_exit'] == 0
        assert not host.restarting
    finally:
        host.release_restart(handle)


def test_old_and_foreign_objects_cannot_release_new_owner(world):
    host = world
    first = host.reserve_restart()
    with pytest.raises(RuntimeError, match='^standalone_restart_reservation_busy$'):
        host.reserve_restart()
    assert host.release_restart(first)
    second = host.reserve_restart()
    for wrong in (first, object(), None):
        with pytest.raises(RuntimeError, match='^standalone_restart_reservation_invalid$'):
            host.release_restart(wrong)
        assert not host.native_admission.snapshot()['open']
        with pytest.raises(BlockingIOError):
            update_lock(host)
    foreign = runtime.Runtime(host.root, host.cfg)
    with pytest.raises(RuntimeError, match='^standalone_restart_reservation_invalid$'):
        foreign.release_restart(second)
    assert host.release_restart(second)


@pytest.mark.parametrize('change', ['stop', 'generation', 'config', 'gate_identity', 'restart_file'])
def test_retirement_never_reopens_changed_or_stopping_owner(world, change):
    host = world
    handle = host.reserve_restart()
    if change == 'stop':
        host.restarting = True
    elif change == 'generation':
        host.generation = 'changed'
    elif change == 'config':
        (host.root / 'config.json').write_text('{"runtime_mode":"standalone","notify":{}}')
    elif change == 'gate_identity':
        previous = host.native_admission
        host.native_admission = runtime.NativeAdmission()
        host.native_admission.close()
    else:
        (host.data / runtime.service.RESTART_FILE).write_text(json.dumps({
            'generation': host.generation, 'request_id': 'a' * 32, 'requested_at': runtime.time.time()}))
        (host.data / runtime.service.RESTART_FILE).chmod(0o600)
    assert host.release_restart(handle) is False
    assert not host.native_admission.snapshot()['open']
    if change == 'gate_identity':
        assert not previous.snapshot()['open']
    os.close(update_lock(host))


def test_already_closed_gate_is_not_owned_or_reopened(world):
    host = world
    host.native_admission.close()
    with pytest.raises(RuntimeError, match='^standalone_restart_admission_closed$'):
        host.reserve_restart()
    assert not host.native_admission.snapshot()['open']


def test_unknown_fd_retirement_is_not_retried_or_reopened(world, monkeypatch):
    host = world
    handle = host.reserve_restart()
    fd = host._restart_reservation['fd']
    close = runtime.os.close
    calls = []
    def failed(value):
        if value == fd:
            calls.append(value)
            raise OSError('synthetic secret close failure')
        close(value)
    with monkeypatch.context() as patch:
        patch.setattr(runtime.os, 'close', failed)
        with pytest.raises(RuntimeError, match='^standalone_restart_retirement_unknown$'):
            host.release_restart(handle)
        with pytest.raises(RuntimeError, match='^standalone_restart_reservation_invalid$'):
            host.release_restart(handle)
    assert calls == [fd] and host._restart_reservation_uncertain
    assert not host.native_admission.snapshot()['open']
    host._publish_status(1000, False)
    snapshot = json.loads((host.data / runtime.service.STATUS_FILE).read_text())
    assert snapshot['restart_reservation'] == 'uncertain'
    assert snapshot['restarting'] is False and snapshot['update_in_progress'] is False
    assert 'synthetic secret close failure' not in json.dumps(snapshot)
    close(fd)  # Synthetic test alone owns the still-open descriptor.


def test_serve_does_not_recreate_connector_and_releases_reservation_on_stop(world, monkeypatch):
    host = world
    handle = host.reserve_restart()
    monkeypatch.setattr(runtime, 'Runtime', lambda *_a: host)
    monkeypatch.setattr(runtime.config, 'load', lambda _root: host.cfg)
    ticks = []
    original_tick = host.tick
    def tick(*args, **kwargs):
        ticks.append(True)
        if len(ticks) == 2:
            host.restarting = True
        return original_tick(6000, **kwargs)
    monkeypatch.setattr(host, 'tick', tick)
    async def connector(*_a, **_k):
        pytest.fail('no new connector during reservation')
    async def scenario():
        sleep = asyncio.sleep
        async def fast(*_a):
            await sleep(0)
        monkeypatch.setattr(runtime.asyncio, 'sleep', fast)
        assert await runtime.serve(host.root, host.cfg, connector) == 0
    asyncio.run(scenario())
    assert host._restart_reservation is None and not host.native_admission.snapshot()['open']
    with pytest.raises(RuntimeError, match='^standalone_restart_reservation_invalid$'):
        host.release_restart(handle)
    os.close(update_lock(host))


def test_reservation_requires_original_synchronous_owner_thread(world):
    host = world
    errors = []
    def attempt():
        try:
            host.reserve_restart()
        except RuntimeError as exc:
            errors.append(str(exc))
    worker = threading.Thread(target=attempt)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == ['standalone_restart_owner_required']
    assert host.native_admission.snapshot()['open']
    assert not (host.data / 'update.lock').exists()


def test_marker_lstat_failure_is_not_absence_and_preserves_original(world, monkeypatch):
    host = world
    path = host.data / runtime.UPDATE_MARKER_NAME
    path.write_bytes(b'synthetic original marker')
    before = path.read_bytes()
    lstat = runtime.os.lstat
    def refuse_marker(value, *args, **kwargs):
        if os.fspath(value) == os.fspath(path):
            raise PermissionError('synthetic secret marker failure')
        return lstat(value, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(runtime.os, 'lstat', refuse_marker)
        with pytest.raises(RuntimeError, match='^standalone_restart_update_marker_unknown$') as caught:
            host.reserve_restart()
        assert 'synthetic secret' not in str(caught.value)
        assert host._restart_reservation is None
        assert host.native_admission.snapshot()['open']
        os.close(update_lock(host))
    assert path.read_bytes() == before
