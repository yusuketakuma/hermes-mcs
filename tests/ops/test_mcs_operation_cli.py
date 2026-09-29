"""The existing snapshot CLI exposes bounded operations, never a second writer DB."""
import io
import json
import uuid

import pytest

import job_ops
import ledger
import mcs_requests
import mcs_view
from ops_testkit import _source


def test_operations_view_and_human_confirmed_cli_use_existing_inbox(tmp_path, monkeypatch, capsys):
    db = _source(tmp_path)
    inbox = tmp_path / 'inbox'
    inbox.mkdir()
    payload = {'targets': [1], 'generation': 'synthetic', 'private_metadata': 'not-a-view-field'}
    db.job_add('semantic', 1, 1, payload=payload)
    snapshot = ledger.publish_snapshot(str(tmp_path / 'source.db'), str(tmp_path / 'snapshots'))
    args = ['--snapshot', str(snapshot), '--cmd-dir', str(inbox)]
    try:
        assert mcs_view.main(args + ['operations', '--project', '1']) == 0
        raw = capsys.readouterr().out
        result = json.loads(raw)
        job = next(item for item in result['items'] if item['kind'] == 'semantic')
        assert job['payload_hash'] == mcs_requests.payload_hash(payload)
        assert 'private_metadata' not in raw and 'not-a-view-field' not in raw
        assert not result['semantic_paused'] and not list(inbox.iterdir())
        with pytest.raises(SystemExit):
            mcs_view.main(args + ['control', 'pause', '--project', '1'])
        assert not list(inbox.iterdir())
        capsys.readouterr()
        command = {'command_id': str(uuid.uuid4()), 'actor': 'synthetic-human', 'feature': 'semantic'}
        monkeypatch.setattr(mcs_view.sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(command).encode())))
        assert mcs_view.main(args + ['control', 'pause', '--project', '1', '--confirm-human']) == 0
        assert json.loads(capsys.readouterr().out)['outcome'] == 'queued'
        job_ops.drain_commands(db, {'errors': []}, str(inbox))
        ledger.publish_snapshot(str(tmp_path / 'source.db'), str(tmp_path / 'snapshots'))
        assert mcs_view.main(args + ['operations', '--project', '1']) == 0
        assert json.loads(capsys.readouterr().out)['semantic_paused']
    finally:
        db.close()
