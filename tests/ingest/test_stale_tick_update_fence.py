"""Synthetic reproduction of a tick loaded before an update."""
import json
import os
import sqlite3
import sys
from types import SimpleNamespace

import collection_stages
import notify_spool
import pytest
import run_check
import writer_lock
from semantic_testkit import _ledger, _message, _patient


def test_tick_with_changed_code_stamp_does_not_run_fetch(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    store = _ledger(data)
    try:
        _patient(store)
        store.save_messages([_message(1)], project_id=1)
        store.semantic_seed(1, [1], {'source': 'history_import'})
        original = json.dumps({'inference_engine_version': '2.0', 'targets': [1]})
        with store.db:
            store.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE kind='semantic'", (original,))
    finally:
        store.close()

    for name, value in {'HOME': tmp_path, 'DB': data / 'ledger.db',
                        'LOCKFILE': data / 'run.lock'}.items():
        monkeypatch.setattr(run_check, name, str(value))
    monkeypatch.setattr(collection_stages, 'ATTACH_DIR', str(data / 'attachments'))
    monkeypatch.setattr(sys, 'argv', ['run_check', '--no-notify'])
    monkeypatch.setattr(run_check, '_CODE_STAMP', 1)
    monkeypatch.setattr(writer_lock, 'code_stamp_checked', lambda: 2)
    monkeypatch.setattr(run_check, 'MCSAdapter', lambda **kw: SimpleNamespace(set_deadline=lambda deadline: None))
    monkeypatch.setattr(notify_spool, 'restore_awaiting_consent', lambda *a: None)
    monkeypatch.setattr(run_check, '_config', lambda: {})
    monkeypatch.setattr(run_check, '_semantic_enabled', lambda *a: True)
    seen = []

    def fetch(adapter, ledger, *args):
        seen.append('fetch')
        ledger.semantic_seed(1, [1], {'source': 'unread', 'event_id': 42})

    monkeypatch.setattr(run_check, '_stage_fetch', fetch)
    for name in ('_run_jobs', '_deliver', '_housekeeping', '_write_health'):
        monkeypatch.setattr(run_check, name, lambda *a, **kw: None)
    monkeypatch.setattr(collection_stages, 'with_relogin', lambda *a, **kw: None)
    monkeypatch.setattr(run_check, '_finish_run', lambda *a: 'ok')
    assert run_check._main() == 3
    with sqlite3.connect(data / 'ledger.db') as db:
        raw, attempts = db.execute("SELECT payload,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
    assert (raw, attempts) == (original, 4)
    assert not seen


@pytest.mark.parametrize('mode', ['tick', 'commands'])
@pytest.mark.parametrize('cause', ['stamp', 'marker', 'marker_error', 'stamp_error', 'initial_unknown'])
def test_entry_refuses_before_db_or_commands_and_releases_lock(tmp_path, monkeypatch, mode, cause):
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setattr(run_check, 'HOME', str(tmp_path))
    monkeypatch.setattr(run_check, 'DB', str(data / 'ledger.db'))
    monkeypatch.setattr(run_check, 'LOCKFILE', str(data / 'run.lock'))
    monkeypatch.setattr(collection_stages, 'ATTACH_DIR', str(data / 'attachments'))
    monkeypatch.setattr(sys, 'argv', ['run_check'] + (['--commands-only'] if mode == 'commands' else []))
    monkeypatch.setattr(run_check, '_CODE_STAMP', None if cause == 'initial_unknown' else 1)
    if cause == 'marker':
        (data / 'update_in_progress.marker').write_text('synthetic')
    if cause == 'marker_error':
        original_lstat = os.lstat
        def lstat(path, *args, **kwargs):
            if str(path) == str(data / 'update_in_progress.marker'):
                raise PermissionError('synthetic')
            return original_lstat(path, *args, **kwargs)
        monkeypatch.setattr(run_check.os, 'lstat', lstat)
    def stamp():
        if cause == 'stamp_error':
            raise OSError('synthetic')
        return 2 if cause == 'stamp' else 1
    monkeypatch.setattr(writer_lock, 'code_stamp_checked', stamp)
    monkeypatch.setattr(run_check, 'Ledger', lambda *a, **k: pytest.fail('opened DB'))
    monkeypatch.setattr(run_check, 'MCSAdapter', lambda *a, **k: pytest.fail('opened adapter'))
    monkeypatch.setattr(run_check, '_commands_only', lambda *a: pytest.fail('ran commands'))
    assert run_check._main() == 3
    assert not (data / 'ledger.db').exists()
    assert not (data / 'attachments').exists()
    fd = writer_lock.acquire_run_lock(str(data / 'run.lock'))
    assert fd is not None
    os.close(fd)


def test_waited_lock_is_rechecked(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setattr(run_check, 'HOME', str(tmp_path))
    monkeypatch.setattr(run_check, 'LOCKFILE', str(data / 'run.lock'))
    monkeypatch.setattr(sys, 'argv', ['run_check'])
    monkeypatch.setattr(run_check, '_CODE_STAMP', 1)
    monkeypatch.setattr(writer_lock, 'code_stamp_checked', lambda: 2)
    monkeypatch.setattr(run_check, 'acquire_run_lock', lambda path: None)
    monkeypatch.setattr(writer_lock, 'wait_run_lock', lambda path, **kw: writer_lock.acquire_run_lock(path))
    monkeypatch.setattr(run_check, 'Ledger', lambda *a, **k: pytest.fail('opened DB'))
    assert run_check._main() == 3
    fd = writer_lock.acquire_run_lock(str(data / 'run.lock'))
    assert fd is not None
    os.close(fd)


def test_entry_exception_releases_lock(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setattr(run_check, 'HOME', str(tmp_path))
    monkeypatch.setattr(run_check, 'LOCKFILE', str(data / 'run.lock'))
    monkeypatch.setattr(sys, 'argv', ['run_check'])
    def error():
        raise RuntimeError('synthetic')
    monkeypatch.setattr(run_check, '_entry_code_current', error)
    with pytest.raises(RuntimeError, match='synthetic'):
        run_check._main()
    fd = writer_lock.acquire_run_lock(str(data / 'run.lock'))
    assert fd is not None
    os.close(fd)


def test_attachment_setup_exception_releases_lock(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setattr(run_check, 'HOME', str(tmp_path))
    monkeypatch.setattr(run_check, 'LOCKFILE', str(data / 'run.lock'))
    monkeypatch.setattr(collection_stages, 'ATTACH_DIR', str(data / 'attachments'))
    monkeypatch.setattr(sys, 'argv', ['run_check'])
    monkeypatch.setattr(run_check, '_CODE_STAMP', 1)
    monkeypatch.setattr(writer_lock, 'code_stamp_checked', lambda: 1)
    def chmod(path, mode):
        raise PermissionError('synthetic')
    monkeypatch.setattr(run_check.os, 'chmod', chmod)
    with pytest.raises(PermissionError):
        run_check._main()
    fd = writer_lock.acquire_run_lock(str(data / 'run.lock'))
    assert fd is not None
    os.close(fd)


def test_unchanged_commands_entry_is_allowed(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setattr(run_check, 'HOME', str(tmp_path))
    monkeypatch.setattr(run_check, 'LOCKFILE', str(data / 'run.lock'))
    monkeypatch.setattr(collection_stages, 'ATTACH_DIR', str(data / 'attachments'))
    monkeypatch.setattr(sys, 'argv', ['run_check', '--commands-only'])
    monkeypatch.setattr(run_check, '_CODE_STAMP', 1)
    monkeypatch.setattr(writer_lock, 'code_stamp_checked', lambda: 1)
    seen = []
    def commands(fd, deadline):
        seen.append(True)
        os.close(fd)
        return 0
    monkeypatch.setattr(run_check, '_commands_only', commands)
    assert run_check._main() == 0
    assert seen == [True]


def test_checked_stamp_fails_on_unreadable_source(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    root.mkdir()
    path = root / 'synthetic.py'
    path.write_text('pass\n')
    monkeypatch.setattr(writer_lock, '_MCS_ROOT', str(root))
    assert writer_lock.code_stamp_checked() == path.stat().st_mtime_ns
    original_stat = os.stat
    def stat(filename, *args, **kwargs):
        if str(filename) == str(path):
            raise PermissionError('synthetic')
        return original_stat(filename, *args, **kwargs)
    monkeypatch.setattr(writer_lock.os, 'stat', stat)
    with pytest.raises(PermissionError):
        writer_lock.code_stamp_checked()


def test_checked_stamp_fails_when_source_tree_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(writer_lock, '_MCS_ROOT', str(tmp_path / 'missing'))
    with pytest.raises(OSError):
        writer_lock.code_stamp_checked()
