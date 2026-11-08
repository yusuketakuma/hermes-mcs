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
