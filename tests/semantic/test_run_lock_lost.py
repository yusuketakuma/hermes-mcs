"""An updater winning the run lock during semantic transport stops the
drain cleanly: no attempt is charged, no later job starts, no DB write."""
import json
import os
import time

import pytest

import mcs_util
import semantic
from semantic_testkit import _cfg, _FakeJev, _llm, _seeded


@pytest.fixture
def lock_env(tmp_path, monkeypatch):
    # stable stamp: concurrent edits of the real tree must not leak in
    monkeypatch.setattr(mcs_util, "code_stamp", lambda: 1)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    (tmp_path / "data").mkdir(exist_ok=True)
    path = str(tmp_path / "run.lock")
    fd = mcs_util.acquire_run_lock(path)
    yield tmp_path, path, fd
    os.close(fd)


def _jobs(db):
    return [tuple(r) for r in db.db.execute(
        "SELECT job_id,state,attempts,next_try,updated_at FROM fetch_jobs "
        "WHERE kind='semantic' ORDER BY job_id")]


def _two_jobs(tmp_path):
    db = _seeded(tmp_path)
    row = db.db.execute(
        "SELECT payload FROM fetch_jobs WHERE kind='semantic'").fetchone()
    db.job_add("semantic", 1, 3, payload=json.loads(row[0]))
    db.db.commit()
    return db


def test_updater_in_window_stops_drain_without_charging(lock_env):
    tmp_path, path, fd = lock_env
    db = _two_jobs(tmp_path)
    before = _jobs(db)
    assert len(before) == 2
    calls = []

    def model(prompt):
        calls.append(prompt)
        # the updater takes the lock, applies, and leaves its marker up
        (tmp_path / "data" / mcs_util.UPDATE_MARKER_NAME).write_text("x")
        return _llm(prompt)

    try:
        result = {"errors": []}
        out = semantic.run_due(db, _cfg(), result, time.monotonic() + 480,
                               llm_fn=model, jev_client=_FakeJev(),
                               max_jobs=4, run_lock_fd=fd)
        assert out["run_lock_lost"] == "held"
        assert "semantic: run_lock_lost" in result["errors"]
        assert len(calls) == 1                 # later jobs never started
        assert _jobs(db) == before             # no attempt, no defer
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE "
                             "kind='semantic_drain_run'").fetchone()[0] == 0
        assert mcs_util.acquire_run_lock(path) is None   # held for finally
    finally:
        db.close()


def test_reacquire_timeout_stops_drain_without_writes(lock_env, monkeypatch):
    tmp_path, path, fd = lock_env
    monkeypatch.setattr(mcs_util, "RUN_LOCK_REACQUIRE_S", 0.05)
    monkeypatch.setattr(mcs_util, "RUN_LOCK_POLL_S", 0.01)
    db = _two_jobs(tmp_path)
    before = _jobs(db)
    other = []

    def model(prompt):
        other.append(mcs_util.acquire_run_lock(path))  # updater keeps it
        return _llm(prompt)

    try:
        result = {"errors": []}
        out = semantic.run_due(db, _cfg(), result, time.monotonic() + 480,
                               llm_fn=model, jev_client=_FakeJev(),
                               max_jobs=4, run_lock_fd=fd)
        assert out["run_lock_lost"] == "timeout"
        assert len(other) == 1 and other[0] is not None
        assert _jobs(db) == before
    finally:
        for o in other:
            if o is not None:
                os.close(o)
        db.close()


def test_normal_transport_is_unchanged(lock_env):
    tmp_path, path, fd = lock_env
    db = _seeded(tmp_path)
    try:
        result = {"errors": []}
        out = semantic.run_due(db, _cfg(), result, time.monotonic() + 480,
                               llm_fn=_llm, jev_client=_FakeJev(),
                               run_lock_fd=fd)
        assert out["done"] == 1 and not result["errors"]
        assert "run_lock_lost" not in out
        assert mcs_util.acquire_run_lock(path) is None
    finally:
        db.close()


class _Db:
    in_transaction = False


def test_unlocked_transport_bounded_wait_and_code_stamp(lock_env, monkeypatch):
    tmp_path, path, fd = lock_env
    ledger = type("L", (), {"db": _Db()})()
    monkeypatch.setattr(mcs_util, "RUN_LOCK_POLL_S", 0.01)
    holder = []
    started = time.monotonic()
    with pytest.raises(mcs_util.RunLockLost) as e:
        with mcs_util.unlocked_transport(ledger, fd, wait_s=0.05):
            holder.append(mcs_util.acquire_run_lock(path))
    assert e.value.held is False and time.monotonic() - started < 5
    os.close(holder[0])
    # lock released by the holder: fd is unlocked (no stray hold)
    probe = mcs_util.acquire_run_lock(path)
    assert probe is not None
    os.close(probe)
    # a code stamp that moves during the window is refused, lock retaken
    stamps = iter([1, 2])
    monkeypatch.setattr(mcs_util, "code_stamp", lambda: next(stamps))
    with pytest.raises(mcs_util.RunLockLost) as e:
        with mcs_util.unlocked_transport(ledger, fd):
            pass
    assert e.value.held is True
    assert mcs_util.acquire_run_lock(path) is None
