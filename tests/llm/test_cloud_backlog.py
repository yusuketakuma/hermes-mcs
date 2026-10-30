"""Synthetic cross-process capacity and owned cloud-worker lifecycle checks."""
import os
import json
from pathlib import Path
import subprocess
import sys
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
@pytest.mark.parametrize('semantic_busy', [False, True])
@pytest.mark.parametrize('counters,expected,done', [
    ({'selected': 64, 'deferred': 3, 'context_blocked': 1, 'skipped': 0,
      'lock_lost': 2, 'slow_deferred': 4},
     {'selected': 64, 'deferred': 3, 'context_blocked': 1, 'skipped': 0,
      'lock_lost': 2, 'slow_deferred': 4}, 1),
    ({}, {}, 0),
    ({'selected': True, 'deferred': -1, 'context_blocked': 'synthetic-private-extra',
      'skipped': 1.5, 'lock_lost': None, 'slow_deferred': {'private': 'synthetic-private-extra'}}, {}, 0),
])
def test_worker_alternates_cloud_only_units_without_local_calls(
        tmp_path, monkeypatch, capsys, budget, counters, expected, done, semantic_busy):
    import extract_llm
    import ledger
    import mcs_util
    import semantic_drain
    import writer_lock
    parent = 991
    parents = iter([parent] * (5 if semantic_busy else 4) + [1])
    monkeypatch.setattr(backlog.os, "getppid", lambda: next(parents))
    monkeypatch.setattr(extract_llm, "_context_memory_available", lambda: True)
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(ledger, "Ledger", lambda *a: SimpleNamespace(close=lambda: None))
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
    monkeypatch.setattr(mcs_util, "load_config", lambda: cfg)
    monkeypatch.setattr(extract_llm.cloud_policy, "global_block_reason", lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm.cloud_policy, "budget_block_reason", lambda *a: None)
    monkeypatch.setattr(extract_llm, "legacy_admissions", lambda *a: None)
    monkeypatch.setattr(backlog.time, "sleep", lambda _: None)
    monkeypatch.setattr(semantic_drain, "runtime_config", lambda *a: ({"job_budget_seconds": 1200}, []))
    acquisitions = []
    def acquire():
        acquisitions.append(True)
        if semantic_busy and len(acquisitions) == 2:
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
    assert seen == ["extract", "extract" if semantic_busy else "semantic"]
    passes = [json.loads(line) for line in capsys.readouterr().out.splitlines()
              if json.loads(line).get('event') == 'cloud_backlog_pass']
    assert 'reason' not in passes[0]
    assert passes[1].get('reason') == ('jev_backlog_budget_reserved' if budget is True and not semantic_busy else None)
    assert passes[1].get('jev_effect') == ('skipped_if_eligible' if budget is True and not semantic_busy else None)
    keys = {'selected', 'deferred', 'context_blocked', 'skipped', 'lock_lost', 'slow_deferred'}
    assert [item['done'] for item in passes] == [done, done]
    assert [{key: item[key] for key in keys if key in item} for item in passes] == [expected, expected]
    assert not any('pids' in item or 'private_extra' in item for item in passes)
    assert 'synthetic-private-extra' not in json.dumps(passes)


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
