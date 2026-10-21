"""Bounded queue refill after lock skips, using synthetic jobs only."""
from contextlib import contextmanager
import time

import pytest

import llm_availability
import semantic_drain as drain
import semantic_runtime as runtime
from extract_testkit import _ledger, _message
from semantic_testkit import BODY, _cfg, _FakeJev, _llm, _patient


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    _patient(db, 1)
    _patient(db, 2)
    monkeypatch.setattr(llm_availability, "disk_free_mb", lambda *a: 100_000.0)
    yield db
    db.close()


def _seed(store, mid, *, pid=1, eligible=True):
    store.save_messages([_message(mid=mid, project_id=pid, body=BODY, unread=eligible)],
                        project_id=pid, semantic=True,
                        notify={"source": "unread"} if eligible else None)
    return store.job_pending(drain.JOB_KIND, pid, mid)["job_id"]


def _locks(monkeypatch, allowed):
    inspected = []

    @contextmanager
    def lock(store, job_id):
        inspected.append(job_id)
        yield job_id in allowed

    monkeypatch.setattr(drain, "_job_lock", lock)
    return inspected


def _processor(monkeypatch):
    processed = []

    def process(store, cfg, job, *args, **kwargs):
        processed.append(job["job_id"])
        assert runtime.transition(store, runtime.JobToken.from_row(job), "done")
        return "done"

    monkeypatch.setattr(drain, "_process_job", process)
    return processed


def _run(store, lane=None, max_jobs=1):
    errors = {"errors": []}
    out = drain.run_due(store, _cfg("shadow", budget=100000), errors,
                        time.monotonic() + 300, jev_client=_FakeJev(),
                        llm_fn=_llm, lane=lane, max_jobs=max_jobs)
    assert not errors["errors"]
    return out


@pytest.mark.parametrize("lane", [None, "backlog", "realtime"])
def test_refill_reaches_runnable_active_job_beyond_initial_window(store, monkeypatch, lane):
    ids = [_seed(store, mid) for mid in range(100, 112)]
    ready = ids[0] if lane == "realtime" else ids[-1]
    inspected = _locks(monkeypatch, {ready})
    processed = _processor(monkeypatch)
    before = {r["job_id"]: dict(r) for r in store.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind IN (?,?)",
        (drain.JOB_KIND, drain.QC_JOB_KIND))}
    out = _run(store, lane)
    assert out["done"] == 1 and processed == [ready]
    assert len(inspected) == len(set(inspected)) == len(ids)
    assert out["lock_skipped"][drain.JOB_KIND] == len(ids) - 1
    after = {r["job_id"]: dict(r) for r in store.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind IN (?,?) AND job_id<>?",
        (drain.JOB_KIND, drain.QC_JOB_KIND, ready))}
    assert after == {job_id: row for job_id, row in before.items() if job_id != ready}
    sched = drain._sched_state(store)
    assert sched["arrival_selected"] == 1 and sched["backfill_selected"] == 0
    assert store.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind=?",
                            (drain.SCHED_KIND,)).fetchone()[0] == 1


def test_refill_exhausts_active_candidates_before_archived_work(store, monkeypatch):
    active = [_seed(store, mid) for mid in range(100, 103)]
    archived = _seed(store, 200, pid=2)
    with store.db:
        store.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    inspected = _locks(monkeypatch, {archived})
    processed = _processor(monkeypatch)
    assert _run(store)["done"] == 1
    assert inspected == active + [archived]
    assert processed == [archived]


def test_refill_scan_is_finite_and_does_not_complete_locked_jobs(store, monkeypatch):
    ids = [_seed(store, mid) for mid in range(100, 140)]
    inspected = _locks(monkeypatch, set())
    processed = _processor(monkeypatch)
    before = [dict(r) for r in store.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind IN (?,?) ORDER BY job_id",
        (drain.JOB_KIND, drain.QC_JOB_KIND))]
    out = _run(store)
    assert out["done"] == 0 and processed == []
    assert inspected == ids[:16]
    assert [dict(r) for r in store.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind IN (?,?) ORDER BY job_id",
        (drain.JOB_KIND, drain.QC_JOB_KIND))] == before
    sched = drain._sched_state(store)
    assert sched["arrival_selected"] == sched["backfill_selected"] == 0
    assert "turn" not in sched
    assert store.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind=?",
                            (drain.SCHED_KIND,)).fetchone()[0] == 1


def test_refill_respects_deadline_before_inspecting_next_job(store, monkeypatch):
    _seed(store, 100)
    selected = {"qc": 0, "arrival": 0, "backfill": 0}
    out = {"lanes": {"qc": 0, "arrival": 0, "backfill": 0}}
    batch = drain._select_due(store, None, (drain.JOB_KIND,), False, 1)
    assert list(drain._refill_due(
        store, None, (drain.JOB_KIND,), False, 1, {}, batch,
        selected, out, time.monotonic() - 1, {})) == []
    assert sum(out["lanes"].values()) == 0


def test_refill_persists_only_the_served_cohort_turn_across_reopen(store, monkeypatch):
    blocked = _seed(store, 100)
    arrival = _seed(store, 101)
    backfill = _seed(store, 102, eligible=False)
    inspected = _locks(monkeypatch, {arrival, backfill})
    processed = _processor(monkeypatch)
    assert _run(store)["done"] == 1
    assert inspected == [blocked, arrival]
    assert drain._sched_state(store)["turn"] == "arrival"
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    store.close()
    from ledger import Ledger
    reopened = Ledger(path)
    try:
        assert _run(reopened)["done"] == 1
        assert processed == [arrival, backfill]
        assert drain._sched_state(reopened)["turn"] == "backfill"
        assert reopened.db.execute(
            "SELECT state,attempts FROM fetch_jobs WHERE job_id=?", (blocked,)).fetchone()[:] == ("pending", 0)
    finally:
        reopened.close()
