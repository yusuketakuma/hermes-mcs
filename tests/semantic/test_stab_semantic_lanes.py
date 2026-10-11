"""Semantic-lane stability (H3, M6, T8): endpoint outages feed the
shared breaker, the realtime lane serves the newest seed (not the newest
defer) and leaves long-output jobs to the backlog workers, free long-
output defers are capped per generation, and an unsaved long-output mark
is logged and counted instead of silently dropped.

Synthetic fixtures, temp DB and stubbed model/Jev clients only."""
import json
import time

import pytest

import mcs_util
import semantic
import semantic_drain
import semantic_runtime as runtime
from semantic_testkit import _FakeJev, _cfg, _ledger, _patient, _seeded


@pytest.fixture(autouse=True)
def _isolated_marks(monkeypatch, tmp_path):
    monkeypatch.setattr(semantic, "_FMT_PATH", str(tmp_path / "format.json"))
    monkeypatch.setattr(semantic, "_LONG_PATH", str(tmp_path / "long.json"))
    monkeypatch.setattr(semantic, "_LONG_UNSAVED", {})
    monkeypatch.setattr(semantic, "_LONG_MARK_FAILURES", {"busy": 0, "error": 0})


def _run(db, llm_fn, lane=None, **kw):
    return semantic.run_due(db, _cfg("shadow"), {"errors": []},
                            time.monotonic() + 300, jev_client=_FakeJev(),
                            llm_fn=llm_fn, lane=lane, **kw)


# ---------- H3: endpoint outage feeds the shared breaker ----------

def test_refused_semantic_call_feeds_breaker_once(tmp_path):
    db = _seeded(tmp_path)
    try:
        def refused(*_a, **_k):
            raise runtime.LLMNotSent("llm_unreachable")
        out = _run(db, refused)
        assert out["deferred"] == 1 and out["llm_down"] is True
        state = json.loads(mcs_util.circuit_state_path(db).read_text())
        assert state["failures"] == 1
        assert mcs_util.llm_down_since(db) is not None
        row = db.db.execute("SELECT attempts FROM fetch_jobs "
                            "WHERE kind='semantic'").fetchone()
        assert row["attempts"] == 0
    finally:
        db.close()


def test_admission_hold_is_not_an_outage(tmp_path):
    db = _seeded(tmp_path)
    try:
        def held(*_a, **_k):
            raise runtime.LLMNotSent("llm_admission:class_full")
        out = _run(db, held)
        assert "llm_down" not in out
        assert not mcs_util.circuit_state_path(db).exists()
        assert mcs_util.llm_down_since(db) is None
    finally:
        db.close()


def test_answered_semantic_call_clears_outage(tmp_path):
    db = _seeded(tmp_path)
    try:
        mcs_util.circuit_failure(db)
        _run(db, lambda *_a, **_k: "{}")
        assert not mcs_util.circuit_state_path(db).exists()
        assert mcs_util.llm_down_since(db) is None
        assert mcs_util.llm_last_success(db) is not None
    finally:
        db.close()


# ---------- M6: realtime order and long-output handling ----------

def _job(db, mid, *, created, updated, next_try, generation=None):
    payload = {"targets": [mid], "eligible": True,
               "generation": generation or f"g{mid}"}
    with db.db:
        return db.db.execute(
            "INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,"
            "payload,state,attempts,next_try,created_at,updated_at)"
            " VALUES('semantic',1,?,NULL,?,'pending',0,?,?,?)",
            (mid, json.dumps(payload, sort_keys=True), next_try, created,
             updated)).lastrowid


@pytest.fixture
def jobs_db(tmp_path):
    db = _ledger(tmp_path)
    _patient(db)
    yield db
    db.close()


def _serve(monkeypatch, outcome):
    served = []

    def process(ledger, scfg, job, jev_client, llm_fn, deadline, **kw):
        served.append(job["message_id"])
        status, not_sent = outcome(job)
        semantic_drain._PASS_REASON["not_sent"] = not_sent
        return status
    monkeypatch.setattr(semantic, "_process_job", process)
    return served


def test_realtime_serves_newest_seed_not_newest_defer(jobs_db, monkeypatch):
    now = time.time()
    # seeded (next_try == updated_at) 1000 s ago
    _job(jobs_db, 10, created=now - 1000, updated=now - 1000,
         next_try=now - 1000)
    # created long ago; just took a free defer (next_try > updated_at)
    _job(jobs_db, 20, created=now - 5000, updated=now - 100,
         next_try=now - 40)
    served = _serve(monkeypatch, lambda job: ("done", None))
    _run(jobs_db, None, lane="realtime", max_jobs=1)
    assert served == [10]


def test_long_output_jobs_leave_realtime_and_cap_free_defers(
        jobs_db, monkeypatch):
    now = time.time()
    long_id = _job(jobs_db, 30, created=now - 50, updated=now - 50,
                   next_try=now - 50)
    _job(jobs_db, 31, created=now - 900, updated=now - 900,
         next_try=now - 900)

    def outcome(job):
        if job["message_id"] == 30:
            return "deferred", "long_output_needs_budget"
        return "done", None
    served = _serve(monkeypatch, outcome)
    _run(jobs_db, None, lane="realtime", max_jobs=1)
    assert served == [30]
    sched = semantic_drain._long_defers(jobs_db)
    assert sched[str(long_id)] == {"g": "g30", "n": 1}
    with jobs_db.db:
        jobs_db.db.execute("UPDATE fetch_jobs SET next_try=0")
    _run(jobs_db, None, lane="realtime", max_jobs=1)
    assert served == [30, 31]            # the long job is not retried here

    monkeypatch.setattr(semantic_drain, "LONG_FREE_DEFER_CAP", 2)
    for _ in range(2):
        with jobs_db.db:
            jobs_db.db.execute("UPDATE fetch_jobs SET next_try=0")
        out = _run(jobs_db, None, lane="backlog", max_jobs=1)
    assert out.get("long_output_capped") == 1
    row = jobs_db.db.execute("SELECT attempts,state FROM fetch_jobs "
                             "WHERE job_id=?", (long_id,)).fetchone()
    assert row["attempts"] == 1          # the capped defer was charged
    assert semantic_drain._long_defers(jobs_db)[str(long_id)]["n"] == 3


def test_long_defer_entry_dropped_on_new_generation(jobs_db, monkeypatch):
    now = time.time()
    long_id = _job(jobs_db, 40, created=now - 50, updated=now - 50,
                   next_try=now - 50)
    _serve(monkeypatch, lambda job: ("deferred", "long_output_needs_budget"))
    _run(jobs_db, None, lane="backlog", max_jobs=1)
    assert str(long_id) in semantic_drain._long_defers(jobs_db)
    with jobs_db.db:                     # re-seed: new input generation
        jobs_db.db.execute(
            "UPDATE fetch_jobs SET payload=?, next_try=0 WHERE job_id=?",
            (json.dumps({"targets": [40], "eligible": True,
                         "generation": "g40b"}), long_id))
    _serve(monkeypatch, lambda job: ("done", None))
    _run(jobs_db, None, lane="realtime", max_jobs=1)
    assert str(long_id) not in semantic_drain._long_defers(jobs_db)


def test_legacy_payload_long_job_leaves_realtime(jobs_db, monkeypatch):
    now = time.time()
    long_id = _job(jobs_db, 50, created=now - 50, updated=now - 50,
                   next_try=now - 50)
    _job(jobs_db, 51, created=now - 900, updated=now - 900,
         next_try=now - 900)
    with jobs_db.db:                     # a payload from before "generation"
        jobs_db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                           (json.dumps({"targets": [50], "eligible": True}),
                            long_id))

    def outcome(job):
        if job["message_id"] == 50:
            return "deferred", "long_output_needs_budget"
        return "done", None
    served = _serve(monkeypatch, outcome)
    _run(jobs_db, None, lane="realtime", max_jobs=1)
    assert served == [50]
    assert str(long_id) in semantic_drain._long_defers(jobs_db)
    with jobs_db.db:
        jobs_db.db.execute("UPDATE fetch_jobs SET next_try=0")
    _run(jobs_db, None, lane="realtime", max_jobs=1)
    assert served == [50, 51]            # the legacy long job stays out


# ---------- T8: unsaved long-output mark ----------

def test_unsaved_long_mark_is_logged_counted_and_kept(monkeypatch, capsys,
                                                      tmp_path):
    monkeypatch.setattr(semantic, "_LONG_PATH",
                        str(tmp_path / "missing" / "\0bad" / "long.json"))
    semantic._long_mark("k1", 1)
    err = capsys.readouterr().err
    assert '"event": "semantic_long_mark_unsaved"' in err
    assert "k1" not in err                       # never the key/prompt
    assert semantic.long_mark_failures()["error"] == 1
    assert semantic._long_marks()["k1"] == 1     # suppression still holds


def test_long_mark_lock_contention_counts_busy(monkeypatch, tmp_path):
    import fcntl
    path = tmp_path / "long.json"
    monkeypatch.setattr(semantic, "_LONG_PATH", str(path))
    with open(str(path) + ".lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        semantic._long_mark("k2", 2)
    assert semantic.long_mark_failures()["busy"] == 1
    semantic._long_mark("k3", 1)                 # next save persists both
    saved = json.loads(path.read_text())
    assert saved == {"k2": 2, "k3": 1} and semantic._LONG_UNSAVED == {}
