"""Normal cloud scan and selection waits leave the shared local writer available."""
import json
import os
import subprocess
import sys

import pytest

import anthropic_client
import patient_context_refresh_state as state
import writer_lock
from test_context_cloud_update_identity import cloud, finish, real_cloud, runtime

__all__ = ['cloud', 'real_cloud', 'runtime']


@pytest.mark.parametrize('phase', ['source_scan', 'summary'])
def test_normal_cloud_wait_allows_an_independent_local_writer(
        runtime, real_cloud, tmp_path, monkeypatch, phase):
    ledger, _, local_calls = runtime
    cfg, posts = real_cloud
    lock_path = tmp_path / 'run.lock'
    db_path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
    ledger.db.execute('CREATE TABLE synthetic_local_progress (completed INTEGER)')
    ledger.db.commit()
    monkeypatch.setattr(writer_lock, 'RUN_LOCK', str(lock_path))
    acquisitions = []

    def acquire():
        fd = writer_lock.acquire_run_lock()
        acquisitions.append(fd is not None)
        return fd

    # The common runtime fixture supplies /dev/null; exercise the actual lock.
    monkeypatch.setattr(state, 'acquire_run_lock', acquire)
    probe = '''
import fcntl
import sqlite3
import sys

with open(sys.argv[1], 'a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(75)
    with sqlite3.connect(sys.argv[2], timeout=0) as db:
        db.execute('INSERT INTO synthetic_local_progress VALUES (1)')
    print('local-progress')
'''

    def local_writer():
        return subprocess.run([sys.executable, '-I', '-c', probe, str(lock_path), db_path],
                              capture_output=True, text=True, timeout=10, check=False)

    held = writer_lock.acquire_run_lock()
    assert held is not None
    try:
        assert local_writer().returncode == 75
    finally:
        os.close(held)
    assert ledger.db.execute('SELECT count(*) FROM synthetic_local_progress').fetchone()[0] == 0
    request = anthropic_client.AnthropicClient.request
    waiting_calls = []

    def waiting_request(client, method, body, timeout, *, deadline):
        payload = json.loads(body['messages'][0]['content'])
        actual_phase = 'summary' if 'reports' in payload else 'source_scan'
        if actual_phase == phase:
            assert not ledger.db.in_transaction
            # Keep HTTP pending until an independent process commits locally.
            progress = local_writer()
            assert progress.returncode == 0, progress.stderr
            assert progress.stdout.strip() == 'local-progress'
            waiting_calls.append(actual_phase)
        return request(client, method, body, timeout, deadline=deadline)

    monkeypatch.setattr(anthropic_client.AnthropicClient, 'request', waiting_request)
    finish(runtime, cfg)
    assert waiting_calls == [phase]
    assert acquisitions and all(acquisitions)
    assert ledger.db.execute('SELECT count(*) FROM synthetic_local_progress').fetchone()[0] == 1
    assert len(posts) == len(ledger.artifacts('cloud_usage_reserve')) == len(ledger.artifacts('cloud_usage_result')) == 2
    assert not ledger.artifacts(state.SOURCE_REPAIR) and not local_calls
    assert not ledger.db.in_transaction
