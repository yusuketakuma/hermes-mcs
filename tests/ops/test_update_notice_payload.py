"""Updater outcomes survive the live-ledger bridge without opening a candidate ledger."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import notify_flush
import update_lifecycle_context as context


@pytest.mark.parametrize('launcher', [False, True])
@pytest.mark.parametrize('outcome', [None, 'applied', 'rollback_failed'])
def test_notice_payload_survives_live_ledger_boundary(monkeypatch, tmp_path, launcher, outcome):
    calls, closed = [], []

    class Ledger:
        def __init__(self, path):
            assert path == str(tmp_path / 'synthetic.db')
            self.db = SimpleNamespace(close=lambda: closed.append(True))

        def outbox_add(self, kind, patient, payload):
            calls.append((kind, patient, payload))

    real_repo = str(Path(context.__file__).parents[2])
    monkeypatch.setattr(context, 'REPO', str(tmp_path) if launcher else real_repo)
    monkeypatch.setattr(context, 'LEDGER', str(tmp_path / 'synthetic.db'))
    monkeypatch.setattr(context, 'ledger', SimpleNamespace(Ledger=Ledger))

    def child(argv, **kwargs):
        assert argv[:2] == [sys.executable, '-c']
        assert argv[3] == str(tmp_path / 'mcs')
        forwarded = json.loads(argv[5])
        if outcome:
            assert forwarded['notice_code'] == outcome
        else:
            assert 'notice_code' not in forwarded
        monkeypatch.setattr(sys, 'argv', ['-c', *argv[3:]])
        monkeypatch.setattr(sys, 'path', list(sys.path))
        monkeypatch.setitem(sys.modules, '_mcs_path', SimpleNamespace())
        monkeypatch.setitem(sys.modules, 'ledger', SimpleNamespace(Ledger=Ledger))
        exec(compile(argv[2], '<synthetic-notice-child>', 'exec'), {})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(context, '_run_with_lifetime', child)
    options = {'notice_code': outcome, 'tag': 'v1.1.2'} if outcome else {}
    assert context._enqueue_notice('synthetic private detail', **options)
    assert closed == [True] and len(calls) == 1
    kind, patient, payload = calls[0]
    assert kind == 'update_notice' and patient is None
    assert payload == {'text': 'synthetic private detail', **options}
    text = notify_flush._system_event_text(kind, payload)
    assert 'synthetic private detail' not in text
    assert text.endswith('MCS-UPD-' + (outcome or 'unknown').upper().replace('_', '-'))


def test_notice_after_a_tree_move_never_migrates_with_the_loaded_ledger(monkeypatch, tmp_path):
    """Regression: recovery reset the live tree to the old version, then its
    already-loaded newer Ledger opened the old DB and migrated it."""
    import sqlite3
    database = tmp_path / 'synthetic.db'
    con = sqlite3.connect(database)
    con.execute('PRAGMA user_version=1')
    con.close()

    def loaded_ledger(_path):
        pytest.fail('the loaded Ledger must not open a DB of another schema')

    calls = []
    monkeypatch.setattr(context, 'REPO', str(Path(context.__file__).parents[2]))
    monkeypatch.setattr(context, 'LEDGER', str(database))
    monkeypatch.setattr(context, 'ledger', SimpleNamespace(Ledger=loaded_ledger, SCHEMA_VERSION=2))
    monkeypatch.setattr(context, '_run_with_lifetime',
                        lambda argv, **_kwargs: calls.append(argv) or SimpleNamespace(returncode=0))
    assert context._enqueue_notice('synthetic', notice_code='rolled_back')
    assert len(calls) == 1 and calls[0][3] == str(Path(context.REPO) / 'mcs')
    con = sqlite3.connect(database)
    assert con.execute('PRAGMA user_version').fetchone()[0] == 1
    con.close()
