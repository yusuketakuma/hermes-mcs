"""Resident extraction waits for the writer without crossing update fences."""
import json
import hashlib
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import extract_llm
import writer_lock


def test_resident_waits_for_real_lock_then_opens_ledger(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "run.lock")
    real_acquire = writer_lock.acquire_run_lock
    owner = real_acquire(path)
    assert owner is not None
    ledger = object()
    waiting = threading.Event()
    opened = []

    def acquire():
        result = real_acquire(path)
        if result is None:
            waiting.set()
        return result

    monkeypatch.setattr(writer_lock, "acquire_run_lock", acquire)
    monkeypatch.setattr(extract_llm, "_writer_current", lambda **_: True)
    monkeypatch.setattr(extract_llm, "Ledger", lambda _: opened.append(True) or ledger)
    result = []
    worker = threading.Thread(target=lambda: result.append(
        extract_llm._open_resident_ledger(time.monotonic() + 5)))
    try:
        worker.start()
        assert waiting.wait(2)
        assert worker.is_alive() and not opened
    finally:
        os.close(owner)
        worker.join(5)
    assert not worker.is_alive() and result == [ledger] and opened == [True]
    events = [json.loads(line)["event"] for line in capsys.readouterr().err.splitlines()]
    assert events == ["extractor_waiting_for_writer", "extractor_ledger_opened"]


@pytest.mark.parametrize("failure", ["hold", "generation", "deadline"])
def test_wait_does_not_open_after_fence_or_deadline(monkeypatch, failure, capsys):
    held, generation = [False], [False]
    now = [0.0]
    attempts = []
    initial = extract_llm._WRITER_STATE
    assert initial[0] is False
    monkeypatch.setattr(writer_lock, "writer_blocked", lambda: held[0])
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (
        False, initial[1] + int(generation[0])))
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda: attempts.append(True) or None)
    monkeypatch.setattr(extract_llm, "Ledger", lambda _: pytest.fail("ledger opened"))

    def advance(seconds):
        now[0] += seconds
        held[0] = failure == "hold"
        generation[0] = failure == "generation"

    monkeypatch.setattr(extract_llm, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=advance))
    assert extract_llm._open_resident_ledger(1.0) is None
    assert attempts and now[0] <= 1.0
    events = [json.loads(line)["event"] for line in capsys.readouterr().err.splitlines()]
    assert events == ["extractor_waiting_for_writer"]


def test_nonresident_open_remains_nonblocking(monkeypatch):
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda: None)
    monkeypatch.setattr(extract_llm, "Ledger", lambda _: pytest.fail("ledger opened"))
    assert extract_llm._open_current_ledger() is None


def test_lock_obtained_at_deadline_is_released_without_opening(tmp_path, monkeypatch):
    now = [0.0]
    lock = str(tmp_path / "deadline.lock")
    real_acquire = writer_lock.acquire_run_lock

    def acquire():
        fd = real_acquire(lock)
        now[0] = 1.0
        return fd

    monkeypatch.setattr(writer_lock, "acquire_run_lock", acquire)
    monkeypatch.setattr(extract_llm, "_writer_current", lambda **_: True)
    monkeypatch.setattr(extract_llm, "Ledger", lambda _: pytest.fail("opened after deadline"))
    monkeypatch.setattr(extract_llm, "time", SimpleNamespace(monotonic=lambda: now[0]))
    assert extract_llm._open_current_ledger(deadline=1.0) is None
    fd = real_acquire(lock)
    assert fd is not None
    os.close(fd)


def test_lock_is_released_when_ledger_constructor_fails(tmp_path, monkeypatch):
    lock = str(tmp_path / "constructor.lock")
    real_acquire = writer_lock.acquire_run_lock
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda: real_acquire(lock))
    monkeypatch.setattr(extract_llm, "_writer_current", lambda **_: True)

    def fail(_):
        raise RuntimeError("synthetic constructor failure")

    monkeypatch.setattr(extract_llm, "Ledger", fail)
    with pytest.raises(RuntimeError, match="synthetic constructor failure"):
        extract_llm._open_current_ledger(deadline=time.monotonic() + 1)
    fd = real_acquire(lock)
    assert fd is not None
    os.close(fd)


def test_generation_change_during_lock_acquire_blocks_ledger(tmp_path, monkeypatch):
    lock = str(tmp_path / "generation.lock")
    real_acquire = writer_lock.acquire_run_lock
    initial, changed = extract_llm._WRITER_STATE, [False]
    assert initial[0] is False
    monkeypatch.setattr(writer_lock, "writer_blocked", lambda: False)
    monkeypatch.setattr(writer_lock, "updater_state", lambda: (
        False, initial[1] + int(changed[0])))

    def acquire():
        fd = real_acquire(lock)
        changed[0] = True
        return fd

    monkeypatch.setattr(writer_lock, "acquire_run_lock", acquire)
    monkeypatch.setattr(extract_llm, "Ledger", lambda _: pytest.fail("opened stale ledger"))
    assert extract_llm._open_resident_ledger(time.monotonic() + 2) is None
    fd = real_acquire(lock)
    assert fd is not None
    os.close(fd)


def test_transient_fence_loss_after_lock_is_not_retried(tmp_path, monkeypatch):
    lock = str(tmp_path / "transient-fence.lock")
    real_acquire = writer_lock.acquire_run_lock
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda: real_acquire(lock))
    observed = []

    def current(**_):
        observed.append(True)
        return len(observed) != 2

    monkeypatch.setattr(extract_llm, "_writer_current", current)
    monkeypatch.setattr(extract_llm, "Ledger", lambda _: pytest.fail("opened after fence loss"))
    assert extract_llm._open_resident_ledger(time.monotonic() + 2) is None
    assert len(observed) == 2
    fd = real_acquire(lock)
    assert fd is not None
    os.close(fd)


def test_source_change_during_wait_is_detected_when_max_mtime_is_unchanged(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    newest, changed = root / "newest.py", root / "changed.py"
    newest.write_text("newest")
    changed.write_text("before")
    future = time.time() + 3600
    os.utime(newest, (future, future))
    old_max = max(p.stat().st_mtime_ns for p in root.glob("*.py"))
    now, attempts = [0.0], []
    real_acquire = writer_lock.acquire_run_lock
    lock = str(tmp_path / "source.lock")

    def acquire():
        attempts.append(True)
        return None if len(attempts) == 1 else real_acquire(lock)

    def change(seconds):
        now[0] += seconds
        changed.write_text("changed synthetic source")

    monkeypatch.setattr(extract_llm, "_RESIDENT_SOURCE_ROOT", str(root))
    monkeypatch.setattr(extract_llm, "_writer_current", lambda **_: True)
    monkeypatch.setattr(writer_lock, "acquire_run_lock", acquire)
    monkeypatch.setattr(extract_llm, "Ledger", lambda _: pytest.fail("opened after source change"))
    monkeypatch.setattr(extract_llm, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=change))
    assert extract_llm._open_resident_ledger(2.0) is None
    assert len(attempts) == 2 and max(p.stat().st_mtime_ns for p in root.glob("*.py")) == old_max
    fd = real_acquire(lock)
    assert fd is not None
    os.close(fd)


def test_unreadable_source_state_fails_before_ledger(monkeypatch):
    def unavailable():
        raise OSError("synthetic source unavailable")

    monkeypatch.setattr(extract_llm, "_resident_source_state", unavailable)
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda: pytest.fail("unexpected lock"))
    monkeypatch.setattr(extract_llm, "Ledger", lambda _: pytest.fail("unexpected ledger"))
    assert extract_llm._open_resident_ledger(time.monotonic() + 2) is None


@pytest.mark.parametrize("damage", ["changed_before_snapshot", "unreadable", "none"])
def test_loaded_source_digest_is_checked_under_lock(tmp_path, monkeypatch, damage):
    root = tmp_path / "source"
    root.mkdir()
    path = root / "synthetic.py"
    path.write_bytes(b"synthetic source before")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if damage == "changed_before_snapshot":
        path.write_bytes(b"synthetic source after")
    elif damage == "unreadable":
        path.unlink()
        (root / "other.py").write_text("synthetic")
    monkeypatch.setattr(extract_llm, "_RESIDENT_SOURCE_ROOT", str(root))
    monkeypatch.setattr(extract_llm, "_LOADED_SOURCE_PATHS", {"module": str(path)})
    monkeypatch.setattr(extract_llm, "_LOADED_SOURCE_DIGESTS", {"module": digest})
    monkeypatch.setattr(extract_llm, "_writer_current", lambda **_: True)
    lock = str(tmp_path / "digest.lock")
    real_acquire = writer_lock.acquire_run_lock
    monkeypatch.setattr(writer_lock, "acquire_run_lock", lambda: real_acquire(lock))
    ledger, opened = object(), []

    def construct(_):
        assert real_acquire(lock) is None
        opened.append(True)
        return ledger

    monkeypatch.setattr(extract_llm, "Ledger", construct)
    result = extract_llm._open_resident_ledger(time.monotonic() + 2)
    assert result is (ledger if damage == "none" else None)
    assert opened == ([True] if damage == "none" else [])
    fd = real_acquire(lock)
    assert fd is not None
    os.close(fd)


def test_resident_stop_after_bounds_startup_wait(tmp_path, monkeypatch, capsys):
    now = [0.0]
    monkeypatch.setattr(extract_llm.sys, "argv", ["extract_llm", "--all", "--lane", "3", "--stop-after", "2"])
    monkeypatch.setattr(extract_llm, "HOME", str(tmp_path))
    monkeypatch.setattr(extract_llm, "_writer_current", lambda **_: True)
    monkeypatch.setattr(extract_llm, "_open_current_ledger", lambda **_: None)
    monkeypatch.setattr(extract_llm, "time", SimpleNamespace(
        time=lambda: 1000.0,
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)))
    assert extract_llm.main() == 1 and now[0] == 2.0
    assert json.loads(capsys.readouterr().out)["error"] == "writer_unavailable"


def test_waiter_continues_after_lock_owner_process_exits(tmp_path, monkeypatch):
    lock = str(tmp_path / "owner.lock")
    code = ("import fcntl,sys,time; f=open(sys.argv[1],'w'); "
            "fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(30)")
    owner = subprocess.Popen([sys.executable, "-c", code, lock], stdout=subprocess.PIPE, text=True)
    worker = None
    try:
        assert owner.stdout.readline().strip() == "locked"
        real_acquire = writer_lock.acquire_run_lock
        waiting = threading.Event()
        def acquire():
            fd = real_acquire(lock)
            if fd is None:
                waiting.set()
            return fd
        monkeypatch.setattr(writer_lock, "acquire_run_lock", acquire)
        monkeypatch.setattr(extract_llm, "_writer_current", lambda **_: True)
        ledger, result = object(), []
        monkeypatch.setattr(extract_llm, "Ledger", lambda _: ledger)
        worker = threading.Thread(target=lambda: result.append(
            extract_llm._open_resident_ledger(time.monotonic() + 5)))
        worker.start()
        assert waiting.wait(2) and worker.is_alive()
        owner.terminate()
        owner.wait(timeout=2)
        worker.join(5)
        assert not worker.is_alive() and result == [ledger]
    finally:
        if owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=2)
        if owner.stdout:
            owner.stdout.close()
        if worker is not None:
            worker.join(5)
