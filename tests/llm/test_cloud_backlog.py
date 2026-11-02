"""Synthetic cross-process capacity and owned cloud-worker lifecycle checks."""
import os
import json
import sqlite3
from pathlib import Path
import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import cloud_backlog as backlog
import run_log_install


@pytest.fixture(autouse=True)
def installed_pool_log_helper(tmp_path, monkeypatch):
    monkeypatch.delenv("MCS_RUN_LOG_HELPER", raising=False)
    recovery = tmp_path / "synthetic-recovery"
    prepared = []
    def helper(root, recovery_dir=None, **kwargs):
        assert Path(root).resolve() == tmp_path
        if not prepared:
            recovery.mkdir(mode=0o700)
            run_log_install.prepare(tmp_path, Path(__file__).resolve().parents[2], recovery, promote=True)
            prepared.append(True)
        return run_log_install.helper_path(root, recovery, **kwargs)
    monkeypatch.setattr(backlog, "helper_path", helper)


def test_shared_slots_count_reduced_limits_and_release(tmp_path):
    with backlog.request_slot(tmp_path, 2) as first:
        assert first
        with backlog.request_slot(tmp_path, 2) as second:
            assert second
            with backlog.request_slot(tmp_path, 2) as full:
                assert not full
            with backlog.request_slot(tmp_path, 1) as reduced:
                assert not reduced
        with backlog.request_slot(tmp_path, 1) as still_full:
            assert not still_full
    with backlog.request_slot(tmp_path, 1) as released:
        assert released


@pytest.mark.parametrize("capacity", [0, 33, True])
def test_invalid_capacity_never_creates_slots(tmp_path, capacity):
    with pytest.raises(ValueError):
        with backlog.request_slot(tmp_path, capacity):
            pytest.fail("invalid capacity admitted")
    assert not list(tmp_path.iterdir())


def test_slot_symlink_never_changes_target(tmp_path):
    target = tmp_path / "synthetic-private"
    target.write_text("preserve")
    locks = tmp_path / "data" / "cloud_locks"
    locks.mkdir(parents=True)
    (locks / "admission.lock").symlink_to(target)
    with pytest.raises(OSError):
        with backlog.request_slot(tmp_path, 4):
            pytest.fail("symlink admitted")
    assert target.read_text() == "preserve"


def test_capacity_is_shared_with_a_separate_process_and_survives_exit(tmp_path):
    code = ("import cloud_backlog,sys; "
            "c=cloud_backlog.request_slot(sys.argv[1],1); "
            "print(int(c.__enter__()),flush=True); sys.stdin.read(1); c.__exit__(None,None,None)")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    with backlog.request_slot(tmp_path, 1):
        child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "0"
            child.communicate("x", timeout=10)
            assert child.returncode == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "1"
        child.kill()
        child.wait()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    with backlog.request_slot(tmp_path, 1) as recovered:
        assert recovered


def test_unknown_and_low_memory_do_not_spawn(monkeypatch):
    monkeypatch.setattr(backlog.sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", lambda self: "MemTotal: 16000000 kB\nMemAvailable: 1000000 kB\n")
    assert backlog.memory_workers(32) == 0
    monkeypatch.setattr(Path, "read_text", lambda self: "MemTotal: 16000000 kB\nMemAvailable: 8000000 kB\n")
    assert backlog.memory_workers(4) == 4
    assert 4 < backlog.memory_workers(32) <= 32
    monkeypatch.setattr(Path, "read_text", lambda self: "malformed")
    assert backlog.memory_workers(32) == 0


def test_pool_owns_one_group_and_only_stops_its_children(tmp_path, monkeypatch):
    import extract_llm
    (tmp_path / "data").mkdir()
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "max_concurrency": 4}}
    monkeypatch.setattr(extract_llm.cloud_policy, "api_key", lambda *a: "synthetic-key")
    monkeypatch.setattr(backlog, "memory_workers", lambda limit: min(2, limit))
    children = []

    class Child:
        def __init__(self, command, **kw):
            helper = backlog.helper_path(tmp_path, require_installed=True)
            assert command[:7] == [sys.executable, "-I", helper, "exec", "--data-dir", str(tmp_path / "data"), "--"]
            assert command[7:] == [sys.executable, str(Path(backlog.__file__).resolve()), "--parent", str(os.getpid())]
            assert kw["stdin"] == subprocess.DEVNULL and kw["close_fds"]
            assert kw["stdout"] == kw["stderr"] == subprocess.DEVNULL
            self.stopped = False
            children.append(self)

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout=None):
            assert self.stopped
            return 0

    monkeypatch.setattr(backlog.subprocess, "Popen", Child)
    first, second = backlog.Pool(tmp_path), backlog.Pool(tmp_path)
    try:
        first.sync(cfg)
        assert len(children) == 2
        second.sync(cfg)
        assert not second.children
        first.last_sync -= 11
        first.sync(cfg)
        assert len(children) == 4
        first.close()
        assert all(p.stopped for p in children)
        second.last_sync -= 11
        second.sync(cfg)
        assert len(second.children) == 2
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize('budget', [True, False, None, 'synthetic'])
@pytest.mark.parametrize('semantic_busy', [False, 'once', 'twice'])
@pytest.mark.parametrize('counters,expected,done,selection_reason', [
    ({'selected': 64, 'deferred': 3, 'context_blocked': 1, 'skipped': 0,
      'lock_lost': 2, 'slow_deferred': 4},
     {'selected': 64, 'deferred': 3, 'context_blocked': 1, 'skipped': 0,
      'lock_lost': 2, 'slow_deferred': 4}, 1, None),
    ({}, {}, 0, None),
    ({'selected': True, 'deferred': -1, 'context_blocked': 'synthetic-private-extra',
      'skipped': 1.5, 'lock_lost': None, 'slow_deferred': {'private': 'synthetic-private-extra'},
      'cloud_selection_incomplete': 'synthetic-private-extra', 'cloud_selection_reason': 'deadline'}, {}, 0, None),
    ({'selected': None, 'cloud_selection_incomplete': True, 'cloud_selection_reason': 'deadline'},
     {}, 0, 'cloud_selection_deadline'),
    ({'selected': None, 'cloud_selection_incomplete': True, 'cloud_selection_reason': 'synthetic-private-extra'},
     {}, 0, 'cloud_selection_incomplete'),
])
def test_worker_alternates_cloud_only_units_without_local_calls(
        tmp_path, monkeypatch, capsys, budget, counters, expected, done, semantic_busy, selection_reason):
    import extract_llm
    import ledger
    import mcs_util
    import semantic_drain
    import writer_lock
    parent = 991
    retries = {'once': 1, 'twice': 2}.get(semantic_busy, 0)
    # one extra getppid: the post-acquire guard re-verifies parent/generation
    # before run_due closes the lock-window
    parents = iter([parent] * (5 + retries) + [1])
    monkeypatch.setattr(backlog.os, "getppid", lambda: next(parents))
    monkeypatch.setattr(extract_llm, "_context_memory_available", lambda: True)
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(ledger, "Ledger", lambda *a: SimpleNamespace(close=lambda: None))
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
    monkeypatch.setattr(mcs_util, "load_config", lambda: cfg)
    monkeypatch.setattr(extract_llm.cloud_policy, "global_block_reason", lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm.cloud_policy, "budget_block_reason", lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm, "legacy_admissions", lambda *a: None)
    monkeypatch.setattr(semantic_drain, "runtime_config", lambda *a: ({"job_budget_seconds": 1200}, []))
    waits = []
    monkeypatch.setattr(backlog.time, "sleep", waits.append)
    acquisitions = []
    fail_at = {'once': (2,), 'twice': (2, 3)}.get(semantic_busy, ())
    def acquire():
        acquisitions.append(True)
        if len(acquisitions) in fail_at:
            return None
        return os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(writer_lock, "acquire_run_lock", acquire)
    seen = []

    def extract(*a, **kw):
        assert kw["cloud_only"] and kw["background"] and kw["prioritize_patients"]
        seen.append("extract")
        return {"done": done, "budget_exhausted": True, **counters,
                "pids": [1], "private_extra": "synthetic-private-extra"}

    def semantic(*a, **kw):
        assert kw["cloud_only"] and kw["lane"] == "backlog"
        seen.append("semantic")
        return {"done": done, "budget_exhausted": budget, **counters,
                "pids": [1], "private_extra": "synthetic-private-extra"}

    monkeypatch.setattr(extract_llm, "run_pending", extract)
    monkeypatch.setattr(semantic_drain, "run_due", semantic)
    monkeypatch.setattr(backlog, "_parent", None)
    backlog.worker(parent)
    # a busy run lock never bounces the turn: the worker keeps short
    # wait/reacquire cycles until the unexecuted semantic job goes first
    assert seen == ["extract", "semantic"]
    assert len(acquisitions) == 2 + retries
    idle = [2] if done == 0 else []
    assert waits == idle + [1] * retries + idle
    passes = [json.loads(line) for line in capsys.readouterr().out.splitlines()
              if json.loads(line).get('event') == 'cloud_backlog_pass']
    assert passes[0].get('reason') == selection_reason
    budget_held = selection_reason is None and budget is True
    assert passes[1].get('reason') == (selection_reason or ('jev_backlog_budget_reserved' if budget_held else None))
    assert passes[1].get('jev_effect') == ('skipped_if_eligible' if budget_held else None)
    assert [item.get('cloud_selection_incomplete') for item in passes] == (
        [True, True] if selection_reason is not None else [None, None])
    keys = {'selected', 'deferred', 'context_blocked', 'skipped', 'lock_lost', 'slow_deferred'}
    assert [item['done'] for item in passes] == [done, done]
    assert [{key: item[key] for key in keys if key in item} for item in passes] == [expected, expected]
    assert not any('pids' in item or 'private_extra' in item for item in passes)
    assert 'synthetic-private-extra' not in json.dumps(passes)


@pytest.mark.parametrize("changed", ["parent", "generation"])
def test_semantic_recheck_after_lock_wait_skips_stale_work(tmp_path, monkeypatch, changed):
    """A parent exit or update-generation change landing in the run-lock
    wait closes the window before run_due: no semantic pass runs, the held
    lock fd is released, and the worker exits without another extract."""
    import extract_llm
    import ledger
    import mcs_util
    import semantic_drain
    import writer_lock
    parent = 991
    # getppid: startup wait, owner check, one extract + three semantic loop
    # checks around the two refused acquisitions, then the post-acquire
    # re-check — the point where the change lands.
    parents = iter([parent] * 6 + ([1] if changed == "parent" else [parent]) + [parent])
    monkeypatch.setattr(backlog.os, "getppid", lambda: next(parents))
    monkeypatch.setattr(extract_llm, "_context_memory_available", lambda: True)
    generations = iter([(False, 1)] * 7 + [(False, 2)] * 4)
    monkeypatch.setattr(writer_lock, "updater_state", lambda: next(generations))
    closed_ledger = []
    monkeypatch.setattr(ledger, "Ledger",
                        lambda *a: SimpleNamespace(close=lambda: closed_ledger.append(True)))
    monkeypatch.setattr(mcs_util, "load_config",
                        lambda: {"cloud_llm": {"mode": "on", "project_ids": [1]}})
    monkeypatch.setattr(extract_llm.cloud_policy, "global_block_reason", lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm.cloud_policy, "budget_block_reason", lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm, "legacy_admissions", lambda *a: None)
    monkeypatch.setattr(semantic_drain, "runtime_config", lambda *a: ({"job_budget_seconds": 1200}, []))
    waits = []
    monkeypatch.setattr(backlog.time, "sleep", waits.append)
    acquisitions, fds = [], []

    def acquire():
        acquisitions.append(True)
        if len(acquisitions) in (2, 3):
            return None
        fd = os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600)
        fds.append(fd)
        return fd
    monkeypatch.setattr(writer_lock, "acquire_run_lock", acquire)
    seen = []
    monkeypatch.setattr(extract_llm, "run_pending",
                        lambda *a, **kw: (seen.append("extract"), {"done": 1})[1])
    monkeypatch.setattr(semantic_drain, "run_due",
                        lambda *a, **kw: (seen.append("semantic"), {"done": 1})[1])
    monkeypatch.setattr(backlog, "_parent", None)
    backlog.worker(parent)
    assert seen == ["extract"]
    assert len(acquisitions) == 4
    assert waits == [1, 1]
    assert closed_ledger == [True]
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_worker_never_opens_database_without_startup_lock(monkeypatch):
    import ledger
    import writer_lock
    parents = iter([991, 1])
    monkeypatch.setattr(backlog.os, "getppid", lambda: next(parents))
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda: None)
    monkeypatch.setattr(backlog.time, "sleep", lambda _: None)
    monkeypatch.setattr(ledger, "Ledger", lambda *a: pytest.fail("database opened without lock"))
    monkeypatch.setattr(backlog, "_parent", None)
    backlog.worker(991)


def test_low_memory_worker_exits_and_closes_its_ledger(tmp_path, monkeypatch):
    import extract_llm
    import ledger
    import writer_lock
    closed = []
    monkeypatch.setattr(backlog, "_parent", None)
    monkeypatch.setattr(backlog.os, "getppid", lambda: 991)
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda: os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600))
    monkeypatch.setattr(ledger, "Ledger", lambda *a: SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr(extract_llm, "_context_memory_available", lambda: False)
    monkeypatch.setattr(extract_llm, "run_pending", lambda *a, **kw: pytest.fail("low-memory work started"))
    backlog.worker(991)
    assert closed == [True]


@pytest.fixture
def carry_worker(tmp_path, monkeypatch):
    """Run a worker with synthetic state and no network or real ledger."""
    import extract_llm
    import ledger
    import mcs_util
    import semantic_drain
    import writer_lock

    state = SimpleNamespace(alive=True, generation=(False, 1), memory=True,
                            block=None, events=[], closed=False, carry=None)
    monkeypatch.setattr(backlog, "_parent", None)
    monkeypatch.setattr(backlog.os, "getppid", lambda: 991 if state.alive else 1)
    monkeypatch.setattr(writer_lock, "updater_state", lambda: state.generation)
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda: os.open(
        tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600))
    fake_ledger = SimpleNamespace(close=lambda: setattr(state, "closed", True))
    monkeypatch.setattr(ledger, "Ledger", lambda *a: fake_ledger)
    monkeypatch.setattr(extract_llm, "_context_memory_available", lambda: state.memory)
    monkeypatch.setattr(extract_llm, "_writer_current", lambda **kw: True)
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
    monkeypatch.setattr(mcs_util, "load_config", lambda: (
        {"cloud_llm": {"mode": "off"}} if state.block == "mode" else cfg))
    monkeypatch.setattr(extract_llm.cloud_policy, "global_block_reason",
                        lambda *a, **kw: state.block if state.block == "stop" else None)
    monkeypatch.setattr(extract_llm.cloud_policy, "budget_block_reason",
                        lambda *a, **kw: state.block if state.block == "budget" else None)
    monkeypatch.setattr(extract_llm, "legacy_admissions", lambda *a: None)
    monkeypatch.setattr(extract_llm, "_write_lock", lambda *a, **kw: nullcontext(True))
    monkeypatch.setattr(semantic_drain, "runtime_config",
                        lambda *a: ({"job_budget_seconds": 1200}, []))
    monkeypatch.setattr(semantic_drain, "run_due",
                        lambda *a, **kw: state.events.append("semantic") or {"done": 1})
    monkeypatch.setattr(backlog.time, "sleep", lambda _: None)
    return state, fake_ledger


@pytest.mark.parametrize('diagnostics,expected', [
    ({'failed': 0, 'held_prior': 2, 'carried': 3, 'disk_low': False,
      'llm_calls': {'calls': 0, 'private': 'synthetic-private-detail'}},
     {'failed': 0, 'held_prior': 2, 'carried': 3, 'disk_low': False, 'llm_calls': {'calls': 0}}),
    ({'failed': 4, 'held_prior': 0, 'carried': 0, 'disk_low': True, 'llm_calls': {'calls': 5}},
     {'failed': 4, 'held_prior': 0, 'carried': 0, 'disk_low': True, 'llm_calls': {'calls': 5}}),
    ({}, {}),
    ({'failed': True, 'held_prior': -1, 'carried': 1.5, 'disk_low': 1, 'llm_calls': {'calls': True}}, {}),
    ({'failed': 'synthetic-private-detail', 'held_prior': None,
      'carried': {'private': 'synthetic-private-detail'}, 'disk_low': 'synthetic-private-detail',
      'llm_calls': {'calls': -1}}, {}),
    ({'disk_low': None, 'llm_calls': ['synthetic-private-detail']}, {}),
    ({'llm_calls': {'calls': 1.5}}, {}),
    ({'llm_calls': {'calls': 'synthetic-private-detail'}}, {}),
    ({'llm_calls': {'private': 'synthetic-private-detail'}}, {}),
])
def test_worker_logs_only_safe_existing_diagnostics(carry_worker, monkeypatch, capsys, diagnostics, expected):
    import extract_llm
    state, _ = carry_worker

    def extract(*args, **kwargs):
        state.alive = False
        return {'done': 1, **diagnostics, 'private': 'synthetic-private-detail',
                'pids': ['synthetic-private-detail']}

    monkeypatch.setattr(extract_llm, 'run_pending', extract)
    backlog.worker(991)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    passes = [item for item in records if item['event'] == 'cloud_backlog_pass']
    assert len(passes) == 1 and state.closed
    keys = {'failed', 'held_prior', 'carried', 'disk_low', 'llm_calls'}
    assert {key: passes[0][key] for key in keys if key in passes[0]} == expected
    assert 'private' not in passes[0] and 'pids' not in passes[0]
    assert 'synthetic-private-detail' not in json.dumps(records)


@pytest.mark.parametrize('block', [None, 'mode', 'stop', 'budget'])
def test_worker_waits_and_reloads_unreadable_config(carry_worker, monkeypatch, capsys, block):
    import extract_llm
    import mcs_util
    state, _ = carry_worker
    state.block = block
    original = mcs_util.load_config
    reads, waits = [], []

    def load():
        reads.append(True)
        if len(reads) == 1:
            raise mcs_util.ProviderSettingsUnreadable('synthetic-private-detail')
        return original()

    def extract(*args, **kwargs):
        state.events.append('extract')
        state.alive = False
        return {'done': 1}

    def sleep(seconds):
        waits.append(seconds)
        if len(reads) == 2:
            state.alive = False

    monkeypatch.setattr(mcs_util, 'load_config', load)
    monkeypatch.setattr(extract_llm, 'run_pending', extract)
    monkeypatch.setattr(backlog.time, 'sleep', sleep)
    backlog.worker(991)
    assert len(reads) == 2
    assert waits == ([10] if block is None else [10, 10])
    assert state.events == (['extract'] if block is None else [])
    assert state.closed
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {'event': 'cloud_backlog_deferred', 'reason': 'provider_settings_unreadable'} in records
    assert 'synthetic-private-detail' not in json.dumps(records)


def test_worker_keeps_unreadable_config_closed_without_work(carry_worker, monkeypatch, capsys):
    import extract_llm
    import mcs_util
    state, _ = carry_worker
    reads, waits = [], []

    def load():
        reads.append(True)
        raise mcs_util.ProviderSettingsUnreadable('synthetic-private-detail')

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            state.alive = False

    monkeypatch.setattr(mcs_util, 'load_config', load)
    monkeypatch.setattr(extract_llm, 'run_pending', lambda *a, **kw: pytest.fail('unreadable config admitted'))
    monkeypatch.setattr(backlog.time, 'sleep', sleep)
    backlog.worker(991)
    assert len(reads) == 2 and waits == [10, 10]
    assert state.events == [] and state.closed
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    holds = [item for item in records if item['event'] == 'cloud_backlog_deferred']
    assert holds == [{'event': 'cloud_backlog_deferred', 'reason': 'provider_settings_unreadable'}]
    assert 'synthetic-private-detail' not in json.dumps(records)


@pytest.mark.parametrize('gate,code', [
    ('mode', 'cloud_mode_off'),
    ('profile', 'cloud_profile_unavailable'),
    ('global-paused', 'paused'),
    ('global-circuit', 'circuit_open'),
    ('global-unknown', 'cloud_blocked'),
    ('budget-daily_cap', 'daily_cap'),
    ('budget-unknown', 'cloud_blocked'),
])
def test_worker_reports_each_wait_reason_once_per_change(carry_worker, monkeypatch, capsys, gate, code):
    import extract_llm
    state, _ = carry_worker
    if gate == 'mode':
        state.block = 'mode'
    elif gate == 'profile':
        monkeypatch.setattr(extract_llm.cloud_policy, 'resolve_profile',
                            lambda *a, **kw: (None, ['synthetic-private-detail']))
    elif gate == 'global-paused':
        monkeypatch.setattr(extract_llm.cloud_policy, 'global_block_reason', lambda *a, **kw: 'paused')
    elif gate == 'global-circuit':
        monkeypatch.setattr(extract_llm.cloud_policy, 'global_block_reason', lambda *a, **kw: 'circuit_open')
    elif gate == 'global-unknown':
        monkeypatch.setattr(extract_llm.cloud_policy, 'global_block_reason',
                            lambda *a, **kw: 'synthetic-private-detail')
    elif gate == 'budget-daily_cap':
        monkeypatch.setattr(extract_llm.cloud_policy, 'budget_block_reason', lambda *a, **kw: 'daily_cap')
    else:
        monkeypatch.setattr(extract_llm.cloud_policy, 'budget_block_reason',
                            lambda *a, **kw: 'synthetic-private-detail')
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            state.alive = False

    monkeypatch.setattr(backlog.time, 'sleep', sleep)
    monkeypatch.setattr(extract_llm, 'run_pending', lambda *a, **kw: pytest.fail('blocked work ran'))
    backlog.worker(991)
    assert waits == [10, 10] and state.events == [] and state.closed
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    holds = [item for item in records if item['event'] == 'cloud_backlog_deferred']
    assert holds == [{'event': 'cloud_backlog_deferred', 'reason': code}]
    assert 'synthetic-private-detail' not in json.dumps(records)


@pytest.mark.parametrize("block", ["stop", "budget", "mode"])
def test_worker_settles_paid_carry_before_no_new_send_gate(carry_worker, monkeypatch, block):
    import extract_llm
    state, fake_ledger = carry_worker
    item = object()

    def extract(ledger, **kw):
        state.events.append("extract")
        state.block = block
        state.received = (ledger, list(kw["carry"]))
        return {"done": 1, "carry": [item]}

    def settle(ledger, carry, **kw):
        state.events.append("settle")
        assert ledger is fake_ledger and carry == [item]
        assert kw["final"] is False and kw["fence"] == (False, 1)
        assert kw["preserve_on_fence_loss"] is True and kw["deadline"] > 0
        carry.clear()
        return {"done": 1, "held_prior": 0, "deferred": 0}, 1, set()

    def sleep(_):
        if state.block:
            state.events.append("blocked")
            state.alive = False

    monkeypatch.setattr(extract_llm, "run_pending", extract)
    monkeypatch.setattr(extract_llm, "_settle_carry", settle)
    monkeypatch.setattr(backlog.time, "sleep", sleep)
    backlog.worker(991)
    assert state.events == ["extract", "settle", "blocked"]
    assert state.received == (fake_ledger, [])
    assert state.closed


def test_worker_keeps_paid_carry_when_extract_return_omits_carry(carry_worker, monkeypatch):
    import extract_llm
    state, fake_ledger = carry_worker
    item = object()
    seen = []

    def extract(ledger, **kw):
        state.block = "stop"
        seen.append(kw["carry"])
        state.carry = kw["carry"]
        state.carry.append(item)
        return {"done": 1}

    def settle(ledger, carry, **kw):
        assert ledger is fake_ledger and carry is state.carry and carry == [item]
        state.events.append("settle")
        carry.clear()
        return {"done": 0, "held_prior": 0, "deferred": 0}, 0, set()

    def sleep(_):
        if state.block:
            state.alive = False

    monkeypatch.setattr(extract_llm, "run_pending", extract)
    monkeypatch.setattr(extract_llm, "_settle_carry", settle)
    monkeypatch.setattr(backlog.time, "sleep", sleep)
    backlog.worker(991)
    assert len(seen) == 1 and seen[0] is state.carry
    assert state.events == ["settle"]
    assert state.carry == [] and state.closed


def test_worker_does_not_settle_old_carry_after_update_fence(carry_worker, monkeypatch):
    import extract_llm
    state, _ = carry_worker
    item = object()

    def extract(ledger, **kw):
        state.generation = (False, 2)
        state.carry = kw["carry"]
        state.carry.append(item)
        return {"done": 0, "updated": True}

    monkeypatch.setattr(extract_llm, "run_pending", extract)
    monkeypatch.setattr(extract_llm, "_settle_carry", lambda *a, **kw: state.events.append("settle"))
    backlog.worker(991)
    assert state.events == [] and state.carry == [item] and state.closed


def test_worker_attempts_paid_carry_before_low_memory_exit(carry_worker, monkeypatch):
    import extract_llm
    state, fake_ledger = carry_worker
    item = object()

    def extract(ledger, **kw):
        state.memory = False
        state.carry = [item]
        return {"done": 1, "carry": state.carry}

    def settle(ledger, carry, **kw):
        assert ledger is fake_ledger and carry == [item]
        assert kw["final"] is False and kw["fence"] == (False, 1)
        assert kw["preserve_on_fence_loss"] is True and kw["deadline"] > 0
        state.events.append("settle")
        carry.clear()
        return {"done": 1, "held_prior": 0, "deferred": 0}, 1, set()

    monkeypatch.setattr(extract_llm, "run_pending", extract)
    monkeypatch.setattr(extract_llm, "_settle_carry", settle)
    backlog.worker(991)
    assert state.events == ["settle"] and state.carry == [] and state.closed


def test_worker_retries_retained_paid_carry_before_starting_more_work(carry_worker, monkeypatch):
    import extract_llm
    state, _ = carry_worker
    item = object()

    def extract(ledger, **kw):
        state.events.append("extract")
        state.carry = [item]
        state.block = "stop"
        return {"done": 1, "carry": state.carry}

    def settle(ledger, carry, **kw):
        assert carry is state.carry and carry == [item]
        state.events.append("settle")
        if state.events.count("settle") == 3:
            carry.clear()
        return {"done": 0, "held_prior": 0, "deferred": 0}, 0, set()

    def sleep(seconds):
        if seconds == 10:
            state.alive = False

    monkeypatch.setattr(extract_llm, "run_pending", extract)
    monkeypatch.setattr(extract_llm, "_settle_carry", settle)
    monkeypatch.setattr(backlog.time, "sleep", sleep)
    backlog.worker(991)
    assert state.events == ["extract", "settle", "settle", "settle"]
    assert state.carry == [] and state.closed


def test_worker_retries_same_paid_carry_after_temporary_sqlite_lock(carry_worker, monkeypatch):
    import extract_llm
    state, fake_ledger = carry_worker
    item = object()

    def extract(ledger, **kw):
        state.events.append("extract")
        state.carry = [item]
        state.block = "stop"
        return {"done": 1, "carry": state.carry}

    def settle(ledger, carry, **kw):
        assert ledger is fake_ledger and carry is state.carry and carry == [item]
        assert kw["final"] is False and kw["fence"] == (False, 1)
        assert kw["preserve_on_fence_loss"] is True and kw["deadline"] > 0
        state.events.append("settle")
        if state.events.count("settle") == 1:
            raise sqlite3.OperationalError("database is locked")
        carry.clear()
        return {"done": 1, "held_prior": 0, "deferred": 0}, 1, set()

    def sleep(_):
        if state.carry == []:
            state.alive = False

    monkeypatch.setattr(extract_llm, "run_pending", extract)
    monkeypatch.setattr(extract_llm, "_settle_carry", settle)
    monkeypatch.setattr(backlog.time, "sleep", sleep)
    backlog.worker(991)
    assert state.events == ["extract", "settle", "settle"]
    assert state.carry == [] and state.closed
