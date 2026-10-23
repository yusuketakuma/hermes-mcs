"""Bound identity probes without blocking the connector or abandoning owned processes."""
import asyncio
import fcntl
import io
import json
import os
from types import SimpleNamespace

import pytest

from mcs_standalone import runtime


class Probe:
    def __init__(self, pid):
        self.pid, self.returncode = pid, None
        source, sink = os.pipe()
        self.input_fd = source
        self.stdin = os.fdopen(sink, 'wb', buffering=0)
        source, sink = os.pipe()
        self.stdout = os.fdopen(source, 'rb', buffering=0)
        self.output_fd = sink
        os.set_blocking(self.input_fd, False)
        self.request = None

    def poll(self):
        return self.returncode

    def read_request(self):
        if self.request is not None:
            return self.request
        try:
            raw = os.read(self.input_fd, runtime.IDENTITY_BYTES + 1)
        except BlockingIOError:
            return None
        if raw:
            self.request = json.loads(raw)
        return self.request

    def complete(self, identity=None, *, sent=False):
        request = self.read_request()
        assert request is not None
        os.write(self.output_fd, json.dumps({
            'generation': request['generation'], 'pid': request['pid'],
            'identity': identity, 'sent': sent}).encode())
        self.returncode = 0

    def close(self):
        for stream in (self.stdin, self.stdout):
            if not stream.closed:
                stream.close()
        os.close(self.input_fd)
        os.close(self.output_fd)


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / 'data').mkdir()
    host = runtime.Runtime(tmp_path, {'runtime_mode': 'standalone'})
    monkeypatch.setattr(runtime, 'log', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(host, '_updating', lambda now: False)
    probes, killed = [], []

    def spawn(argv, **kwargs):
        assert argv[1:3] == ['-I', '-c']
        assert kwargs['start_new_session'] is True
        assert kwargs['env'] == {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
        probe = Probe(9000 + len(probes))
        probes.append(probe)
        return probe

    monkeypatch.setattr(runtime.subprocess, 'Popen', spawn)
    def kill_group(pid, sig):
        if sig == 0:
            probe = next(p for p in probes if p.pid == pid)
            if probe.returncode is not None:
                raise ProcessLookupError
            return
        killed.append((pid, sig))

    monkeypatch.setattr(runtime.os, 'killpg', kill_group)
    try:
        yield host, probes, killed
    finally:
        for probe in probes:
            probe.close()


def test_eight_timed_out_candidates_leave_connector_loop_responsive_and_fair(world, monkeypatch):
    host, probes, killed = world
    clock = [0.0]
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock[0])
    jobs = sorted(host.argv)[:8]
    host.orphan_candidates = {job: 4000 + index for index, job in enumerate(jobs)}
    progress, windows = [], []
    stopped = [False]

    async def connector():
        while not stopped[0]:
            progress.append(1)
            await asyncio.sleep(0)

    async def drive():
        for index in range(8):
            host.tick(100 + clock[0], draining=True)
            assert len(probes) == index + 1
            host.tick(100 + clock[0], draining=True)  # Feed input without waiting for ps.
            assert probes[-1].read_request()['pid'] == host.orphan_candidates[jobs[index]]
            clock[0] += 6
            host.tick(100 + clock[0], draining=True)
            assert len(probes) == index + 1  # Killed but unreaped: never start another child.
            assert killed[-1] == (probes[-1].pid, runtime.signal.SIGKILL)
            before = len(progress)
            await asyncio.sleep(0)
            windows.append(len(progress) > before)
            probes[-1].returncode = -runtime.signal.SIGKILL
        host._poll_identity(100 + clock[0])
        stopped[0] = True

    async def run():
        await asyncio.gather(connector(), drive())

    asyncio.run(run())
    assert windows == [True] * 8
    assert host.orphan_candidates.keys() == set(jobs)
    assert host.identity_probe is None
    assert all(probe.stdin.closed and probe.stdout.closed for probe in probes)


def test_cancelled_serve_keeps_host_lock_until_probe_is_reaped(world, monkeypatch):
    host, probes, _killed = world
    host.orphan_candidates = {'extract-0': 4001}
    host.schedules = []
    host.restarting = True
    monkeypatch.setattr(runtime, 'Runtime', lambda *_args, **_kwargs: host)
    monkeypatch.setattr(runtime.config, 'load', lambda root: host.cfg)
    real_sleep = asyncio.sleep

    async def fast_sleep(*_args):
        await real_sleep(0)

    monkeypatch.setattr(runtime.asyncio, 'sleep', fast_sleep)

    async def run():
        task = asyncio.create_task(runtime.serve(host.root, host.cfg))
        for _ in range(30):
            await real_sleep(0)
            if probes:
                break
        assert len(probes) == 1
        task.cancel()
        for _ in range(30):
            await real_sleep(0)
            if probes[0].read_request() is not None:
                break
        assert not task.done()
        with (host.data / 'standalone.lock').open('rb') as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        probes[0].complete(False)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host.identity_probe is None
        assert probes[0].stdin.closed and probes[0].stdout.closed
        with (host.data / 'standalone.lock').open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    asyncio.run(run())


@pytest.mark.parametrize('identity', [True, False, None])
def test_only_fresh_worker_identity_can_authorize_a_signal(monkeypatch, capsys, identity):
    request = {'pid': 4001, 'argv': ['synthetic-worker'], 'operation': int(runtime.signal.SIGTERM),
               'generation': 'a' * 32, 'owner_pid': 3999}
    calls = []
    monkeypatch.setattr(runtime, '_is_orphan', lambda pid, argv, owner_pid:
                        calls.append(('identity', pid, argv, owner_pid)) or identity)
    monkeypatch.setattr(runtime.os, 'kill', lambda pid, sig: calls.append(('signal', pid, sig)))
    monkeypatch.setattr(runtime.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())))
    runtime._identity_worker()
    result = json.loads(capsys.readouterr().out)
    assert result['identity'] is identity
    assert result['sent'] is (identity is True)
    assert calls[0] == ('identity', 4001, ['synthetic-worker'], 3999)
    assert calls[1:] == ([('signal', 4001, runtime.signal.SIGTERM)] if identity is True else [])


def test_excess_output_is_discarded_and_no_replacement_precedes_reap(world):
    host, probes, killed = world
    host.orphan_candidates = {'mcs_check': 4001}
    host._start_identity()
    host._poll_identity(100)
    host.identity_probe['raw'].extend(b'x' * (runtime.IDENTITY_BYTES + 1))
    host._poll_identity(100)
    assert killed == [(probes[0].pid, runtime.signal.SIGKILL)]
    host._start_identity()
    assert len(probes) == 1
    probes[0].returncode = -runtime.signal.SIGKILL
    host._poll_identity(101)
    assert host.identity_probe is None
    assert host.orphan_candidates == {'mcs_check': 4001}
    assert host.children == {}


def test_reaped_probe_with_lingering_group_remains_fenced(world, monkeypatch):
    host, probes, _killed = world
    host.orphan_candidates = {'mcs_check': 4001}
    host._start_identity()
    host._poll_identity(100)
    probes[0].complete(False)
    group_exists = [True]

    def group(pid, sig):
        assert sig == 0  # Never signal a reaped leader's potentially reused group.
        if not group_exists[0]:
            raise ProcessLookupError

    monkeypatch.setattr(runtime.os, 'killpg', group)
    host._poll_identity(101)
    host._start_identity()
    assert host.identity_probe is not None and len(probes) == 1
    assert host.orphan_candidates == {'mcs_check': 4001}
    group_exists[0] = False
    host._poll_identity(102)
    assert host.identity_probe is None and host.orphan_candidates == {}


@pytest.mark.parametrize('change', ['generation', 'pid', 'argv'])
def test_stale_probe_identity_never_releases_a_different_candidate(world, change):
    host, _probes, _killed = world
    host.orphan_candidates = {'mcs_check': 4001}
    probe = {'job': 'mcs_check', 'pid': 4001, 'argv': list(host.argv['mcs_check']),
             'generation': host.generation, 'operation': None}
    if change == 'generation':
        probe['generation'] = '0' * 32
    elif change == 'pid':
        probe['pid'] = 4002
    else:
        probe['argv'] = ['synthetic-other']
    host._identity_result(probe, {'pid': probe['pid'], 'generation': probe['generation'],
                                  'identity': False, 'sent': False}, 101)
    assert host.orphan_candidates == {'mcs_check': 4001}
    assert host.children == {}


def test_adopted_core_pending_stop_logs_and_saves_its_slot_only_once(world, monkeypatch):
    host, _probes, _killed = world
    child = runtime._Adopted(4001, host.argv['mcs_check'])
    host.children['mcs_check'] = {'process': child, 'kind': 'core', 'stopping_at': None,
                                  'pending': None, 'adopted': True, 'unfired': 60}
    host.fired, host.inflight = {'mcs_check': 120}, {'mcs_check': 60}
    host.restarting, host.restart_since = True, 100
    events, saves = [], []
    monkeypatch.setattr(runtime, 'log', lambda event, **fields: events.append(event))
    monkeypatch.setattr(runtime.os, 'kill', lambda pid, sig: None)
    monkeypatch.setattr(host, '_start_identity', lambda: None)
    monkeypatch.setattr(host, '_save_fired', lambda: saves.append(dict(host.fired)))
    for step in range(5):
        assert host.tick(101 + runtime.DRAIN_CAP_S + step, draining=True) is False
    assert events.count('drain_cap_reached') == 1
    assert saves == [{'mcs_check': 60}]
    assert host.fired == host.inflight == {'mcs_check': 60}
    assert child.operation == runtime.signal.SIGTERM
    assert host.children['mcs_check']['stopping_at'] is None
