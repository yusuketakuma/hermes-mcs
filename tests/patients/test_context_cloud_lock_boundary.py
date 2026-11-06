"""Synthetic cloud waits leave the real local writer lock and SQLite available."""
import os
import subprocess
import sys

import extract_llm as worker
import patient_context_refresh_state
import writer_lock
from anthropic_context_testkit import cloud, drain, runtime, source_repair

__all__ = ['cloud', 'runtime', 'source_repair']


def test_cloud_source_repair_wait_allows_an_independent_local_writer(
        runtime, source_repair, tmp_path, monkeypatch):
    ledger, _, _ = runtime
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

    # Replace the shared fixture's /dev/null descriptor with the real lock.
    monkeypatch.setattr(patient_context_refresh_state, 'acquire_run_lock', acquire)
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

    client_factory = worker.anthropic_client.AnthropicClient
    network_calls = []

    def client(*args, **kwargs):
        instance = client_factory(*args, **kwargs)
        post = instance._post_fn
        assert callable(post)

        def waiting_post(body, timeout):
            assert not ledger.db.in_transaction
            # The response cannot return until another process commits locally.
            progress = local_writer()
            assert progress.returncode == 0, progress.stderr
            assert progress.stdout.strip() == 'local-progress'
            network_calls.append(1)
            return post(body, timeout)

        instance._post_fn = waiting_post
        return instance

    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient', client)
    drain(runtime)
    assert len(network_calls) == len(source_repair['sent']) == 2
    assert acquisitions and all(acquisitions)
    assert ledger.db.execute('SELECT count(*) FROM synthetic_local_progress').fetchone()[0] == 2
    assert not ledger.db.in_transaction
