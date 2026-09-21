"""Transient Jev outages hold the existing jobs across process restarts."""
import time

from ledger import Ledger
import semantic
import semantic_jev as jev
import semantic_runtime as runtime
from test_mcs_semantic import _cfg, _FakeJev, _llm, _seeded


def test_drain_circuit_preserves_jobs_and_resumes_after_cooldown(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    db = _seeded(tmp_path)
    failing = _FakeJev(error=jev.JevError("transport", retryable=True))
    try:
        for _ in range(3):
            with db.db:
                db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            semantic.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 300,
                             jev_client=failing, llm_fn=_llm)
        assert runtime.circuit_open(db)
        attempts = db.db.execute("SELECT attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()[0]
        db.close()
        db = Ledger(str(tmp_path / 'ledger.db'))
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        healthy = _FakeJev()
        held = semantic.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 300,
                                jev_client=healthy, llm_fn=_llm)
        assert held['circuit_open'] and healthy.requests_made == 0
        row = db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert row['state'] == 'pending' and row['attempts'] == attempts
        clock[0] += 301
        resumed = semantic.run_due(db, _cfg(), {"errors": []}, time.monotonic() + 300,
                                   jev_client=healthy, llm_fn=_llm)
        assert resumed['done'] == 1 and healthy.requests_made > 0
        assert not runtime.circuit_open(db)
    finally:
        db.close()
