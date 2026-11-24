"""Semantic job results no feature reads are not produced (owner decision 2026-11-24).

With semantic on but summary and loop off, no semantic job is queued,
re-queued or run, pending ones stay untouched, and patient context does
not wait on them; enabling a reader brings the old behaviour back.

Synthetic only: temp ledger, Jev/LLM stubs, no services."""
import time

import pytest

import run_check
import semantic
import semantic_drain
import semantic_due_selection
import semantic_policy
from semantic_testkit import _FakeJev, _cfg, _ledger, _message, _patient

UNREAD = {"summary_mode": "off", "loop_mode": "off"}


def _scfg(cfg):
    scfg, errors = semantic_policy.semantic_config(cfg)
    assert not errors
    return scfg


def test_one_predicate_names_the_readers():
    assert not semantic_policy.primary_verdicts_consumed(_scfg(_cfg("assist", **UNREAD)))
    assert not semantic_policy.primary_verdicts_consumed(_scfg(_cfg("off")))
    for reader in ("summary_mode", "loop_mode"):
        assert semantic_policy.primary_verdicts_consumed(
            _scfg(_cfg("assist", **{**UNREAD, reader: "shadow"})))


def test_tick_seeds_only_while_a_reader_is_enabled():
    assert not run_check._semantic_seeding(_cfg("assist", **UNREAD))
    assert not run_check._semantic_seeding({"semantic": "typo"})
    assert run_check._semantic_seeding(_cfg("assist", **{**UNREAD, "loop_mode": "shadow"}))


def _jobs(db):
    return [tuple(r) for r in db.db.execute(
        "SELECT job_id,state,attempts,next_try,payload FROM fetch_jobs WHERE kind=? ORDER BY job_id",
        (semantic_drain.JOB_KIND,))]


@pytest.fixture
def threads(tmp_path, monkeypatch):
    """Four synthetic patients, one pending semantic job each."""
    monkeypatch.setattr(semantic_due_selection, "_BACKLOG_TURN", {"since_qc": 0})
    db = _ledger(tmp_path)
    for pid in range(1, 5):
        p = _patient(db, pid)
        p.messages = [_message(pid * 10, pid=pid)]
        db.save_patient(p, notify={"source": "unread"}, semantic=True)
    try:
        yield db
    finally:
        db.close()


def test_explicit_replay_seed_needs_a_reader(tmp_path, threads):
    import json
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_cfg("assist", **UNREAD)))
    threads.db.execute("DELETE FROM fetch_jobs WHERE kind=?", (semantic_drain.JOB_KIND,))
    threads.db.commit()
    assert semantic.seed(threads, 10, cfg_path=str(path)) is None
    assert _jobs(threads) == []
    path.write_text(json.dumps(_cfg("assist", **{**UNREAD, "loop_mode": "shadow"})))
    assert semantic.seed(threads, 10, cfg_path=str(path)) == 10
    assert len(_jobs(threads)) == 1


@pytest.mark.parametrize("lane", [None, "realtime", "backlog"])
@pytest.mark.parametrize("qc", ["off", "annotate"])
def test_pending_jobs_stay_queued_unrun_without_a_reader(threads, monkeypatch, lane, qc):
    before = _jobs(threads)
    assert len(before) == 4
    calls = []
    monkeypatch.setattr(semantic_drain, "_process_job", lambda *a, **k: calls.append(a[2]["job_id"]))
    monkeypatch.setattr(semantic_drain, "reprocess_step", lambda *a, **k: {"status": "stub"})
    jev = _FakeJev()
    out = semantic_drain.run_due(threads, _cfg("assist", extract_qc=qc, **UNREAD), {"errors": []},
                                 time.monotonic() + 300, jev_client=jev, lane=lane, max_jobs=4)
    assert calls == [] and jev.requests_made == 0
    assert out["done"] == 0 and out["unread_semantic_pending"] == 4
    # recorded and reversible: the rows are exactly as they were
    assert _jobs(threads) == before


def test_pending_jobs_run_again_once_a_reader_is_enabled(threads, monkeypatch):
    calls = []
    monkeypatch.setattr(semantic_drain, "_process_job", lambda *a, **k: calls.append(a[2]["job_id"]))
    monkeypatch.setattr(semantic_drain, "reprocess_step", lambda *a, **k: {"status": "stub"})
    semantic_drain.run_due(threads, _cfg("assist", **{**UNREAD, "loop_mode": "shadow"}), {"errors": []},
                           time.monotonic() + 300, jev_client=_FakeJev(), lane="backlog", max_jobs=4)
    assert calls


def _walk(db, cfg):
    for _ in range(20):
        status = semantic_drain.reprocess_step(db, cfg)["status"]
        if status in ("waiting", "complete"):
            return
    pytest.fail("synthetic inference reprocess did not settle")


def test_reprocess_pass_requeues_only_for_a_reader(tmp_path):
    db = _ledger(tmp_path)
    try:
        for pid in (1, 2):
            p = _patient(db, pid)
            p.messages = [_message(pid * 10, pid=pid)]
            db.save_patient(p)   # stored without a semantic job
        _walk(db, _cfg("assist", **UNREAD))
        assert _jobs(db) == []
        # enabling a reader changes the proof generation: the next round seeds
        _walk(db, _cfg("assist", **{**UNREAD, "loop_mode": "shadow"}))
        jobs = _jobs(db)
        assert len(jobs) == 2 and {j[1] for j in jobs} == {"pending"}
    finally:
        db.close()
