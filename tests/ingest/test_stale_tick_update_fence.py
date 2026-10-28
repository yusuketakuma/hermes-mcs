"""Synthetic reproduction of a tick loaded before an update."""
import json
import os
import sqlite3
import sys
from types import SimpleNamespace
import uuid

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


@pytest.mark.parametrize('commands_only', [False, True])
@pytest.mark.parametrize('stale', [False, True])
@pytest.mark.parametrize('hold_changes', [False, True])
def test_update_hold_processes_only_approval_with_current_code(tmp_path, monkeypatch, capsys, commands_only, stale, hold_changes):
    import job_ops
    import mcs_requests
    import restore_evidence
    import update_launch

    data = tmp_path / 'data'
    data.mkdir()
    cmd = data / 'cmd'
    cmd.mkdir()
    for name, value in {'HOME': tmp_path, 'DB': data / 'ledger.db', 'LOCKFILE': data / 'run.lock'}.items():
        monkeypatch.setattr(run_check, name, str(value))
    monkeypatch.setattr(collection_stages, 'ATTACH_DIR', str(data / 'attachments'))
    monkeypatch.setattr(sys, 'argv', ['run_check'] + (['--commands-only'] if commands_only else []))
    monkeypatch.setattr(run_check, '_CODE_STAMP', 1)
    monkeypatch.setattr(writer_lock, 'code_stamp_checked', lambda: 2 if stale else 1)
    monkeypatch.setattr(run_check, 'MCSAdapter', lambda *a, **k: pytest.fail('adapter during hold'))
    monkeypatch.setattr(run_check, '_commands_only', lambda *a: pytest.fail('ordinary command processing during hold'))
    (data / 'update_in_progress.marker').write_text('synthetic')
    notify_spool.mark_restored(str(data), backup_path=str(data / 'b.db'), phase='awaiting_consent')
    if hold_changes:
        read_hold = notify_spool.restore_awaiting_consent
        observations = []
        def changed_hold(root):
            observations.append(True)
            return read_hold(root) if len(observations) == 1 else None
        monkeypatch.setattr(notify_spool, 'restore_awaiting_consent', changed_hold)
    rid = 'a' * 64
    for req in (
        {'cmd': 'ops.pause', 'project_id': 1, 'feature': 'semantic'},
        {'cmd': 'ops.restore_approve', 'project_id': None, 'report_id': rid,
         'backup_sha256': 'b' * 64, 'backup_schema': 7, 'reason': 'synthetic'},
    ):
        req.update(version=1, command_id=str(uuid.uuid4()), actor='synthetic', human_confirmed=True)
        mcs_requests.enqueue(req, str(cmd))
    drain = job_ops.drain_commands
    monkeypatch.setattr(job_ops, 'drain_commands', lambda db, result, **kw: drain(db, result, str(cmd), **kw))
    spawned = []
    monkeypatch.setattr(update_launch, 'spawn_detached', lambda **kwargs: spawned.append(True))
    monkeypatch.setattr(restore_evidence, 'create_loss_report', lambda **kwargs: {
        'report_id': rid, 'backup_sha256': 'b' * 64, 'backup_schema': 7})
    assert run_check._main() == (3 if stale else 0)
    result = json.loads(capsys.readouterr().out)
    assert not (data / 'attachments').exists()
    if stale:
        assert not (data / 'ledger.db').exists()
        assert len(list(cmd.glob('*.json'))) == 2
        assert not spawned
    else:
        assert result['restore_consent_hold'] is True and result['commands'] == 1
        assert len(list(cmd.glob('*.json'))) == 1
        assert spawned
        with sqlite3.connect(data / 'ledger.db') as db:
            rows = db.execute('SELECT receipt_json FROM command_receipts').fetchall()
        assert len(rows) == 1 and json.loads(rows[0][0])['cmd'] == 'ops.restore_approve'
    fd = writer_lock.acquire_run_lock(str(data / 'run.lock'))
    assert fd is not None
    os.close(fd)


@pytest.mark.parametrize('phase', ['restored', 'unreadable'])
def test_update_marker_without_confirmed_consent_hold_runs_no_commands(tmp_path, monkeypatch, phase):
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setattr(run_check, 'HOME', str(tmp_path))
    monkeypatch.setattr(run_check, 'LOCKFILE', str(data / 'run.lock'))
    monkeypatch.setattr(sys, 'argv', ['run_check', '--commands-only'])
    monkeypatch.setattr(run_check, '_CODE_STAMP', 1)
    monkeypatch.setattr(writer_lock, 'code_stamp_checked', lambda: 1)
    monkeypatch.setattr(run_check, '_commands_only', lambda *a: pytest.fail('commands while update is pending'))
    monkeypatch.setattr(run_check, '_consent_only', lambda *a: pytest.fail('restore already applied'))
    (data / 'update_in_progress.marker').write_text('synthetic')
    if phase == 'restored':
        notify_spool.mark_restored(str(data), phase='restored')
    else:
        (data / 'restore_pending.json').write_text('{')
    assert run_check._main() == 3
    fd = writer_lock.acquire_run_lock(str(data / 'run.lock'))
    assert fd is not None
    os.close(fd)
