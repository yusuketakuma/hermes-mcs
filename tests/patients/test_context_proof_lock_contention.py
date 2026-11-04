"""Isolate the selected patient's proof-read writer-lock side effect."""
import json
import sqlite3
import time

import patient_context_refresh as refresh
from test_patient_context_refresh import runtime as runtime, seed, step


def test_only_final_proof_read_blocks_unrelated_local_save(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, mid=1, pid=1)
    seed(led, mid=2, pid=2, proof=False)
    path = led.db.execute('PRAGMA database_list').fetchone()[2]
    second = sqlite3.connect(path, timeout=0)
    probes, proof_durations = [], []

    def save(phase):
        started = time.perf_counter()
        try:
            second.execute('INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) '
                           "VALUES('extract_llm',2,2,?, 'synthetic-local','{}',0)",
                           (json.dumps({'summary': 'synthetic unrelated local result'}),))
            second.commit()
            status = 'committed'
        except sqlite3.OperationalError as exc:
            second.rollback()
            assert exc.sqlite_errorcode == sqlite3.SQLITE_BUSY
            status = 'busy'
        probes.append({'phase': phase, 'status': status,
                       'elapsed_ms': round((time.perf_counter() - started) * 1000, 3)})
        return status

    original = refresh._inference_ready

    def ready(reader, pid, cfg, **kwargs):
        if not led.db.in_transaction or reader.db is led.db:
            return original(reader, pid, cfg, **kwargs)
        assert pid == 1
        started = time.perf_counter()
        assert save('final_proof') == 'busy'
        # Hold only the proof-read boundary, without changing any clinical proof.
        time.sleep(0.04)
        result = original(reader, pid, cfg, **kwargs)
        proof_durations.append(round((time.perf_counter() - started) * 1000, 3))
        return result

    monkeypatch.setattr(refresh, '_inference_ready', ready)
    try:
        version = led.db.execute('PRAGMA data_version').fetchone()[0]
        assert save('before') == 'committed'
        assert led.db.execute('PRAGMA data_version').fetchone()[0] != version
        assert step(runtime)['status'] == 'progressed'
        assert not led.db.in_transaction
        assert save('after') == 'committed'
        assert [probe['status'] for probe in probes] == ['committed', 'busy', 'committed']
        assert len(proof_durations) == 1
        print(json.dumps({'probes': probes, 'synthetic_proof_hold_ms': proof_durations,
                          'other_patient_data_version_changed': True}, sort_keys=True))
    finally:
        second.close()
