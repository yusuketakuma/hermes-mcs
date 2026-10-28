"""Synthetic reproduction of a tick loaded before an update."""
import json
import sqlite3
import sys
from types import SimpleNamespace

import collection_stages
import notify_spool
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
    monkeypatch.setattr(writer_lock, 'code_stamp', lambda: 2)
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
    run_check._main()
    with sqlite3.connect(data / 'ledger.db') as db:
        raw, attempts = db.execute("SELECT payload,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
    assert (raw, attempts) == (original, 4)
    assert not seen
