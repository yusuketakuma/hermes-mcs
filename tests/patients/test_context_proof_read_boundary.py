"""Sealed source repair reads stay independent of the refresh worker and its I/O."""
import builtins
import json
from pathlib import Path
import subprocess
import sys

import pytest

import patient_context_artifacts as artifacts
from test_anthropic_context_refresh import cloud, context_refresh, drain, runtime, source_repair

__all__ = ['cloud', 'runtime', 'source_repair']


def test_core_reader_root_import_does_not_load_refresh_worker():
    root = Path(__file__).resolve().parents[2]
    code = '''import sys, socket, subprocess
sys.path.insert(0, sys.argv[1])
import _mcs_path
class NoWorker:
    def find_spec(self, fullname, *args):
        if fullname == 'patient_context_refresh':
            raise AssertionError('reader imported refresh worker')
sys.meta_path.insert(0, NoWorker())
def forbidden(*args, **kwargs):
    raise AssertionError('reader import performed external I/O')
socket.socket.connect = forbidden
socket.create_connection = forbidden
subprocess.Popen = forbidden
import inference_dependency_loss
import patient_context_artifacts
assert 'patient_context_refresh' not in sys.modules
assert patient_context_artifacts.ORIGINAL_KIND == 'patient_context_scan_chunk_v1'
'''
    result = subprocess.run([sys.executable, '-I', '-c', code, str(root / 'mcs')],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('borrowed_transaction', [False, True])
def test_saved_source_repair_replay_never_imports_worker_or_changes_rows(
        runtime, source_repair, monkeypatch, borrowed_transaction):
    ledger, _, _ = runtime
    drain(runtime)
    headers = ledger.artifacts(context_refresh.SOURCE_REPAIR, project_id=1)
    assert len(headers) == 1
    rows = [dict(row) for row in ledger.artifacts(context_refresh.SOURCE_REPAIR_CANDIDATE, project_id=1)]
    assert rows
    before = [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    network_calls = list(source_repair['sent'])
    if borrowed_transaction:
        ledger.db.execute('BEGIN')
    importer = builtins.__import__
    def reader_import(name, *args, **kwargs):
        if name == 'patient_context_refresh':
            pytest.fail('saved proof reader must not import the refresh worker')
        return importer(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', reader_import)
    assert context_refresh._verify_source_repair_rows is artifacts.verify_source_repair_rows
    selected = artifacts.selected_rows(ledger.db, 1, 1,
        source_binding=json.loads(headers[0]['meta'])['source_binding'])
    assert {row['artifact_id'] for row in selected} == {row['artifact_id'] for row in rows}
    assert ledger.db.in_transaction is borrowed_transaction
    assert [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert source_repair['sent'] == network_calls
    if borrowed_transaction:
        ledger.db.rollback()
