"""Serialize patient selection and claim behind a per-database mutex."""
import os
import subprocess
import sys

import pytest

import patient_context_refresh as refresh
import patient_context_refresh_state as state
import writer_lock
from ledger import Ledger
from test_patient_context_refresh import document, runtime as runtime, seed, step


def lock_path(led):
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]
    return os.path.join(os.path.dirname(os.path.realpath(db_path)),
                        '.patient_context_selection.lock')


def test_selection_lock_unit_busy_release_and_no_unlink(runtime):
    led, _, _ = runtime
    seed(led, mid=1, pid=1)
    path = lock_path(led)
    fd, reason = refresh._selection_lock(led.db)
    assert reason is None and fd is not None
    try:
        assert os.path.exists(path)
        other, other_reason = refresh._selection_lock(led.db)
        assert other is None and other_reason == 'patient_context_selection_busy'
    finally:
        os.close(fd)
    again, reason = refresh._selection_lock(led.db)
    assert reason is None and again is not None
    os.close(again)
    assert os.path.exists(path)


def test_selection_lock_unavailable_without_database_path(tmp_path):
    import sqlite3
    db = sqlite3.connect(':memory:')
    try:
        fd, reason = refresh._selection_lock(db)
        assert fd is None and reason == 'patient_context_selection_unavailable'
    finally:
        db.close()


def test_selection_lock_alias_and_swap_keep_same_path(runtime, tmp_path):
    led, _, _ = runtime
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]
    alias = tmp_path / 'alias'
    alias.symlink_to(os.path.dirname(db_path), target_is_directory=True)
    alias_ledger = Ledger(str(alias / os.path.basename(db_path)))
    try:
        fd, reason = refresh._selection_lock(alias_ledger.db)
        assert reason is None
        os.close(fd)
        expected = os.path.join(os.path.dirname(os.path.realpath(db_path)),
                                '.patient_context_selection.lock')
        held = writer_lock.acquire_run_lock(expected)
        assert held is not None
        os.close(held)
        swapped = os.path.join(os.path.dirname(db_path), 'swapped.db')
        os.rename(db_path, swapped)
        try:
            fd, reason = refresh._selection_lock(led.db)
            assert reason is None and fd is not None
            os.close(fd)
        finally:
            os.rename(swapped, db_path)
    finally:
        alias_ledger.close()


def test_busy_pass_defers_without_consumption_then_proceeds(runtime):
    led, _, calls = runtime
    seed(led, mid=1, pid=1)
    before_progress = document(led, state.PROGRESS)
    before_scan = document(led, state.SCAN)
    path = lock_path(led)
    held = writer_lock.acquire_run_lock(path)
    assert held is not None
    try:
        result = step(runtime)
    finally:
        os.close(held)
    assert result['status'] == 'deferred' and result['done'] == 0
    assert result.get('reason') == 'patient_context_selection_busy'
    assert 'scan_interrupts' not in result and 'scanned' not in result
    assert document(led, state.PROGRESS) == before_progress
    assert document(led, state.SCAN) == before_scan
    assert not calls
    after = step(runtime)
    assert after['status'] == 'progressed' and after['done'] == 1, after


def test_all_waiting_stays_waiting_not_busy(runtime):
    led, _, _ = runtime
    seed(led, mid=1, pid=1, proof=False)
    result = step(runtime)
    assert result.get('reason') != 'patient_context_selection_busy'
    assert 'scanned' in result and 'waiting' in result


def test_selection_exceptions_release_lock(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, mid=1, pid=1)
    path = lock_path(led)

    def boom(*args, **kwargs):
        raise RuntimeError('synthetic_selection_failure')

    monkeypatch.setattr(refresh, '_select_patient', boom)
    with pytest.raises(RuntimeError):
        step(runtime)
    fd, reason = refresh._selection_lock(led.db)
    assert reason is None and fd is not None
    os.close(fd)
    assert os.path.exists(path)


def test_selection_control_exception_is_not_swallowed(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, mid=1, pid=1)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(refresh, '_select_patient', interrupt)
    with pytest.raises(KeyboardInterrupt):
        step(runtime)
    fd, reason = refresh._selection_lock(led.db)
    assert reason is None and fd is not None
    os.close(fd)


def test_lock_released_before_inference(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, mid=1, pid=1)
    path = lock_path(led)
    import local_llm
    original = local_llm.admitted_chat
    probes = []

    def chat(owner, text, **kwargs):
        fd = writer_lock.acquire_run_lock(path)
        probes.append(fd is not None)
        if fd is not None:
            os.close(fd)
        return original(owner, text, **kwargs)

    monkeypatch.setattr(local_llm, 'admitted_chat', chat)
    result = step(runtime)
    assert result['status'] == 'progressed', result
    assert probes and all(probes)


def test_run_lock_holder_does_not_deadlock_selection(runtime):
    led, _, _ = runtime
    seed(led, mid=1, pid=1)
    held = writer_lock.acquire_run_lock()
    assert held is not None
    try:
        result = step(runtime, seconds=30)
    finally:
        os.close(held)
    assert result['status'] in ('progressed', 'deferred', 'complete')
    assert result.get('reason') != 'patient_context_selection_busy'


def test_two_processes_exclude_selection_lock(runtime, tmp_path):
    led, _, _ = runtime
    seed(led, mid=1, pid=1)
    path = lock_path(led)
    probe = (
        'import fcntl, os, sys\n'
        'fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n'
        'fcntl.flock(fd, fcntl.LOCK_EX)\n'
        'os.write(1, b"held\\n")\n'
        'sys.stdin.readline()\n'
        'os.close(fd)\n'
        'os.write(1, b"released\\n")\n'
    )
    owner = subprocess.Popen([sys.executable, '-I', '-c', probe, path],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert owner.stdout.readline().strip() == 'held'
        busy = step(runtime)
        assert busy.get('reason') == 'patient_context_selection_busy'
        assert busy['done'] == 0
        owner.stdin.write('\n')
        owner.stdin.flush()
        assert owner.stdout.readline().strip() == 'released'
        owner.wait(timeout=10)
        assert owner.returncode == 0, owner.stderr.read()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)
    assert os.path.exists(path)
    after = step(runtime)
    assert after['status'] == 'progressed' and after['done'] == 1, after
