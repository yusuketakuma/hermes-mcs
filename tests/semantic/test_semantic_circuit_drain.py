"""Jev outages skip verification while the durable circuit survives restarts."""
import semantic_drain
import json
import time

from ledger import Ledger
import semantic_jev as jev
import semantic_runtime as runtime
from semantic_testkit import _cfg, _FakeJev, _llm, _message, _seeded


def test_drain_circuit_preserves_jobs_and_resumes_after_cooldown(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    db = _seeded(tmp_path)
    failing = _FakeJev(error=jev.JevError("transport", retryable=True))
    try:
        for mid in (1, 3, 4):
            if mid != 1:
                db.save_messages([_message(mid)], project_id=1, semantic=True)
            completed = semantic_drain.run_due(
                db, _cfg(), {"errors": []}, time.monotonic() + 300,
                jev_client=failing, llm_fn=_llm, max_jobs=1)
            assert completed["done"] == 1
        assert runtime.circuit_open(db)
        assert db.db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='semantic' AND state!='done'").fetchone()[0] == 0
        db.close()
        db = Ledger(str(tmp_path / 'ledger.db'))
        db.save_messages([_message(5)], project_id=1, semantic=True)
        healthy = _FakeJev()
        held = semantic_drain.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 300,
                                jev_client=healthy, llm_fn=_llm, max_jobs=1)
        assert held['circuit_open'] and healthy.requests_made == 0
        assert held['done'] == 1
        audit = max(db.artifacts('semantic_audit', message_id=5), key=lambda row: row['artifact_id'])
        meta = json.loads(audit['meta'])
        assert meta['audit_status'] == 'SKIPPED' and meta['jev_skip_reason'] == 'circuit_open'
        clock[0] += 301
        db.save_messages([_message(6)], project_id=1, semantic=True)
        resumed = semantic_drain.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 300,
                                   jev_client=healthy, llm_fn=_llm, max_jobs=1)
        assert resumed['done'] == 1 and healthy.requests_made > 0
        assert not runtime.circuit_open(db)
    finally:
        db.close()
