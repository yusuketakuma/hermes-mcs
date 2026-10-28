"""Semantic-lane stability (H3, M6, T8): endpoint outages feed the
shared breaker, the realtime lane serves the newest seed (not the newest
defer) and leaves long-output jobs to the backlog workers, free long-
output defers are capped per generation, and an unsaved long-output mark
is logged and counted instead of silently dropped.

Synthetic fixtures, temp DB and stubbed model/Jev clients only."""
import llm_availability
import semantic_transport
import json
import time

import pytest

import semantic_drain
from inference_version import CURRENT_VERSION, VERSION_KEY
import semantic_runtime as runtime
from semantic_testkit import _FakeJev, _cfg, _ledger, _message, _patient, _seeded


@pytest.fixture(autouse=True)
def _isolated_marks(monkeypatch, tmp_path):
    monkeypatch.setattr(semantic_transport, '_FMT_PATH', str(tmp_path / "format.json"))
    monkeypatch.setattr(semantic_transport, '_LONG_PATH', str(tmp_path / "long.json"))
    monkeypatch.setattr(semantic_transport, '_LONG_UNSAVED', {})
    monkeypatch.setattr(semantic_transport, '_LONG_MARK_FAILURES', {"busy": 0, "error": 0})


def _run(db, llm_fn, lane=None, **kw):
    return semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
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
        state = json.loads(llm_availability.circuit_state_path(db.db).read_text())
        assert state["failures"] == 1
        assert llm_availability.llm_down_since(db.db) is not None
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
        assert not llm_availability.circuit_state_path(db.db).exists()
        assert llm_availability.llm_down_since(db.db) is None
    finally:
        db.close()


def test_answered_semantic_call_clears_outage(tmp_path):
    db = _seeded(tmp_path)
    try:
        llm_availability.circuit_failure(db.db)
        _run(db, lambda *_a, **_k: "{}")
        assert not llm_availability.circuit_state_path(db.db).exists()
        assert llm_availability.llm_down_since(db.db) is None
        assert llm_availability.llm_last_success(db.db) is not None
    finally:
        db.close()


# ---------- M6: realtime order and long-output handling ----------

def _job(db, mid, *, created, updated, next_try, generation=None):
    payload = {VERSION_KEY: CURRENT_VERSION, "targets": [mid], "eligible": True,
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
        kw["diagnostics"]["not_sent"] = not_sent
        return status
    monkeypatch.setattr(semantic_drain, '_process_job', process)
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
    jobs_db.save_messages([_message(40)], project_id=1)
    long_id = _job(jobs_db, 40, created=now - 50, updated=now - 50,
                   next_try=now - 50)
    _serve(monkeypatch, lambda job: ("deferred", "long_output_needs_budget"))
    _run(jobs_db, None, lane="backlog", max_jobs=1)
    assert str(long_id) in semantic_drain._long_defers(jobs_db)
    with jobs_db.db:                     # re-seed: new input generation
        jobs_db.db.execute(
            "UPDATE fetch_jobs SET payload=?, next_try=0 WHERE job_id=?",
            (json.dumps({VERSION_KEY: CURRENT_VERSION, "targets": [40], "eligible": True,
                         "generation": "g40b"}), long_id))
    _serve(monkeypatch, lambda job: ("done", None))
    _run(jobs_db, None, lane="realtime", max_jobs=1)
    assert str(long_id) not in semantic_drain._long_defers(jobs_db)


def test_wrong_root_targets_do_not_discard_old_generation(jobs_db):
    now = time.time()
    jobs_db.save_messages([_message(40), _message(41)], project_id=1)
    job_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    original = {'g': 'g40', 'n': 7}
    with jobs_db.db:
        jobs_db.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?',
                           (json.dumps({VERSION_KEY: CURRENT_VERSION, 'generation': 'g40b',
                                        'targets': [41]}), job_id))
        semantic_drain._sched_write(jobs_db, {'long_defers': {str(job_id): original}}, commit=False)
    with semantic_drain._sched_update(jobs_db) as sched:
        semantic_drain._merge_long_defers(jobs_db, sched, {})
    assert semantic_drain._long_defers(jobs_db)[str(job_id)] == original
    token = runtime.JobToken.from_row(jobs_db.db.execute(
        'SELECT * FROM fetch_jobs WHERE job_id=?', (job_id,)).fetchone())
    with pytest.raises(ValueError, match='semantic_long_defer_generation_unknown'):
        semantic_drain._reserve_long_defer(jobs_db, token)
    assert semantic_drain._long_defers(jobs_db)[str(job_id)] == original


@pytest.mark.parametrize('payload', [
    '{',
    json.dumps({VERSION_KEY: '2.0', 'targets': [40], 'generation': 'future'}),
    '{"targets":[40],"x":' + '[' * 130 + '0' + ']' * 130 + '}',
    '{"inference_engine_version":"1.0","inference_engine_version":"2.0"}',
    '{"targets":null}',
    '{"padding":"' + 'x' * (1024 * 1024) + '"}',
])
def test_unreadable_long_defer_survives_other_job_and_restart(jobs_db, monkeypatch, payload):
    now = time.time()
    held_id = _job(jobs_db, 40, created=now - 50, updated=now - 50, next_try=now - 50)
    _job(jobs_db, 41, created=now - 40, updated=now - 40, next_try=now - 40)
    original = {'g': 'g40', 'n': 7}
    with jobs_db.db:
        jobs_db.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?', (payload, held_id))
        semantic_drain._sched_write(jobs_db, {'long_defers': {str(held_id): original}}, commit=False)
    served = _serve(monkeypatch, lambda job: ('done', None))
    _run(jobs_db, None, lane='backlog', max_jobs=1)
    assert served == [41]
    assert semantic_drain._long_defers(jobs_db)[str(held_id)] == original
    path = jobs_db.db.execute('PRAGMA database_list').fetchone()[2]
    jobs_db.db.close()
    # Reopening the same synthetic database exercises the durable scheduler row.
    import sqlite3
    jobs_db.db = sqlite3.connect(path)
    jobs_db.db.row_factory = sqlite3.Row
    assert semantic_drain._long_defers(jobs_db)[str(held_id)] == original


def test_uncertain_long_defer_is_not_replaced_or_evicted(jobs_db, monkeypatch):
    now = time.time()
    held_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    other_id = _job(jobs_db, 41, created=now, updated=now, next_try=now)
    original = {'g': 'g40', 'n': 7}
    with jobs_db.db:
        jobs_db.db.execute("UPDATE fetch_jobs SET payload='{',state='failed' WHERE job_id=?", (held_id,))
        semantic_drain._sched_write(jobs_db, {'long_defers': {str(held_id): original}}, commit=False)
    monkeypatch.setattr(semantic_drain, '_LONG_DEFERS_MAX', 1)
    with pytest.raises(ValueError, match='semantic_long_defers_capacity'):
        with semantic_drain._sched_update(jobs_db) as sched:
            semantic_drain._merge_long_defers(jobs_db, sched, {
                str(held_id): {'g': 'other', 'n': 1},
                str(other_id): {'g': 'g41', 'n': 1},
            })
    assert semantic_drain._long_defers(jobs_db) == {str(held_id): original}


def test_full_long_defer_map_holds_new_job_before_call(jobs_db, monkeypatch):
    now = time.time()
    held_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    pending_id = _job(jobs_db, 41, created=now, updated=now, next_try=now)
    original = {'g': 'g40', 'n': 7}
    with jobs_db.db:
        jobs_db.db.execute("UPDATE fetch_jobs SET payload='{' WHERE job_id=?", (held_id,))
        semantic_drain._sched_write(jobs_db, {'long_defers': {str(held_id): original}}, commit=False)
    monkeypatch.setattr(semantic_drain, '_LONG_DEFERS_MAX', 1)
    served = _serve(monkeypatch, lambda job: ('done', None))
    result = {'errors': []}
    out = semantic_drain.run_due(jobs_db, _cfg('shadow'), result,
                                 time.monotonic() + 300, jev_client=_FakeJev(),
                                 llm_fn=None, lane='backlog', max_jobs=1)
    assert served == []
    assert out['long_defer_reservation_failed'] is True
    assert 'semantic: long_defer_reservation_failed' in result['errors']
    assert semantic_drain._long_defers(jobs_db) == {str(held_id): original}
    assert jobs_db.db.execute('SELECT state,attempts FROM fetch_jobs WHERE job_id=?',
                              (pending_id,)).fetchone()[:] == ('pending', 0)


def test_reservation_survives_lost_run_lock(jobs_db, monkeypatch):
    import writer_lock

    now = time.time()
    job_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)

    def interrupted(*args, **kwargs):
        raise writer_lock.RunLockLost(False)
    monkeypatch.setattr(semantic_drain, '_process_job', interrupted)
    _run(jobs_db, None, lane='backlog', max_jobs=1)
    assert semantic_drain._long_defers(jobs_db)[str(job_id)] == {'g': 'g40', 'n': 1}
    path = jobs_db.db.execute('PRAGMA database_list').fetchone()[2]
    jobs_db.db.close()
    import sqlite3
    jobs_db.db = sqlite3.connect(path)
    jobs_db.db.row_factory = sqlite3.Row
    assert semantic_drain._long_defers(jobs_db)[str(job_id)] == {'g': 'g40', 'n': 1}


def test_no_long_defer_refunds_reservation(jobs_db, monkeypatch):
    now = time.time()
    job_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    original = {'g': 'g40', 'n': 3}
    with jobs_db.db:
        semantic_drain._sched_write(jobs_db, {'long_defers': {str(job_id): original}}, commit=False)
    served = _serve(monkeypatch, lambda job: ('deferred', 'not_a_long_output_defer'))
    _run(jobs_db, None, lane='backlog', max_jobs=1)
    assert served == [40]
    assert semantic_drain._long_defers(jobs_db)[str(job_id)] == original


def test_full_map_allows_existing_generation_but_holds_new(jobs_db, monkeypatch):
    now = time.time()
    held_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    existing_id = _job(jobs_db, 41, created=now, updated=now, next_try=now)
    _job(jobs_db, 42, created=now, updated=now, next_try=now)
    with jobs_db.db:
        jobs_db.db.execute("UPDATE fetch_jobs SET payload='{' WHERE job_id=?", (held_id,))
        semantic_drain._sched_write(jobs_db, {'long_defers': {
            str(held_id): {'g': 'g40', 'n': 3},
            str(existing_id): {'g': 'g41', 'n': 2},
        }}, commit=False)
    monkeypatch.setattr(semantic_drain, '_LONG_DEFERS_MAX', 2)
    served = _serve(monkeypatch, lambda job: ('deferred', 'long_output_needs_budget'))
    result = {'errors': []}
    semantic_drain.run_due(jobs_db, _cfg('shadow'), result, time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=None, lane='backlog', max_jobs=2)
    assert served == [41]
    assert semantic_drain._long_defers(jobs_db) == {
        str(held_id): {'g': 'g40', 'n': 3}, str(existing_id): {'g': 'g41', 'n': 3}}
    assert 'semantic: long_defer_reservation_failed' in result['errors']


def test_separate_connections_respect_reserved_capacity(jobs_db, monkeypatch):
    from ledger import Ledger

    now = time.time()
    first_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    second_id = _job(jobs_db, 41, created=now, updated=now, next_try=now)
    tokens = [runtime.JobToken.from_row(jobs_db.db.execute(
        'SELECT * FROM fetch_jobs WHERE job_id=?', (job_id,)).fetchone())
        for job_id in (first_id, second_id)]
    monkeypatch.setattr(semantic_drain, '_LONG_DEFERS_MAX', 1)
    path = jobs_db.db.execute('PRAGMA database_list').fetchone()[2]
    other = Ledger(path)
    try:
        semantic_drain._reserve_long_defer(jobs_db, tokens[0])
        with pytest.raises(ValueError, match='semantic_long_defers_capacity'):
            semantic_drain._reserve_long_defer(other, tokens[1])
        assert semantic_drain._long_defers(other) == {str(first_id): {'g': 'g40', 'n': 1}}
    finally:
        other.close()


def test_reservation_lock_failure_keeps_existing_scheduler(jobs_db):
    import sqlite3
    from ledger import Ledger

    now = time.time()
    job_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    token = runtime.JobToken.from_row(jobs_db.db.execute(
        'SELECT * FROM fetch_jobs WHERE job_id=?', (job_id,)).fetchone())
    path = jobs_db.db.execute('PRAGMA database_list').fetchone()[2]
    other = Ledger(path)
    try:
        other.db.execute('PRAGMA busy_timeout=0')
        jobs_db.db.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError):
            semantic_drain._reserve_long_defer(other, token)
        jobs_db.db.rollback()
        assert semantic_drain._long_defers(other) == {}
        semantic_drain._reserve_long_defer(other, token)
        assert semantic_drain._long_defers(jobs_db)[str(job_id)] == {'g': 'g40', 'n': 1}
    finally:
        if jobs_db.db.in_transaction:
            jobs_db.db.rollback()
        other.close()


def test_changed_payload_is_not_refunded(jobs_db):
    now = time.time()
    job_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    token = runtime.JobToken.from_row(jobs_db.db.execute(
        'SELECT * FROM fetch_jobs WHERE job_id=?', (job_id,)).fetchone())
    reservation = semantic_drain._reserve_long_defer(jobs_db, token)
    with jobs_db.db:
        jobs_db.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?',
                           (json.dumps({VERSION_KEY: '2.0'}), job_id))
    semantic_drain._refund_long_defer(jobs_db, token, reservation)
    assert semantic_drain._long_defers(jobs_db)[str(job_id)] == {'g': 'g40', 'n': 1}


@pytest.mark.parametrize('raw', ['{', '[]', '{"long_defers":null}',
                                 '{"long_defers":{"1":{"g":"x","n":-1}}}'])
def test_invalid_scheduler_holds_work_and_keeps_original(jobs_db, monkeypatch, raw):
    now = time.time()
    _job(jobs_db, 40, created=now, updated=now, next_try=now)
    with jobs_db.db:
        semantic_drain._sched_write(jobs_db, {}, commit=False)
        jobs_db.db.execute('UPDATE fetch_jobs SET payload=? WHERE kind=?',
                           (raw, semantic_drain.SCHED_KIND))
    served = _serve(monkeypatch, lambda job: ('done', None))
    result = {'errors': []}
    semantic_drain.run_due(jobs_db, _cfg('shadow'), result, time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=None, lane='backlog', max_jobs=1)
    assert served == []
    assert 'semantic: long_defer_reservation_failed' in result['errors']
    assert jobs_db.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                              (semantic_drain.SCHED_KIND,)).fetchone()[0] == raw


def test_bounded_long_defer_scan_rotates(jobs_db, monkeypatch):
    now = time.time()
    first_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    second_id = _job(jobs_db, 41, created=now, updated=now, next_try=now)
    with jobs_db.db:
        jobs_db.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?',
                           (json.dumps({VERSION_KEY: CURRENT_VERSION, 'generation': 'g41b'}), second_id))
        semantic_drain._sched_write(jobs_db, {'long_defers': {
            str(first_id): {'g': 'g40', 'n': 2},
            str(second_id): {'g': 'g41', 'n': 3},
        }}, commit=False)
    monkeypatch.setattr(semantic_drain, '_LONG_DEFERS_SCAN_BYTES', 1)
    with semantic_drain._sched_update(jobs_db) as sched:
        semantic_drain._merge_long_defers(jobs_db, sched, {})
    assert str(second_id) in semantic_drain._long_defers(jobs_db)
    # Give the next pass enough room for the second row; the cursor starts there.
    monkeypatch.setattr(semantic_drain, '_LONG_DEFERS_SCAN_BYTES', 100)
    with semantic_drain._sched_update(jobs_db) as sched:
        semantic_drain._merge_long_defers(jobs_db, sched, {})
    assert str(second_id) not in semantic_drain._long_defers(jobs_db)


def test_scheduler_blob_is_read_only_after_size_check(jobs_db):
    from types import SimpleNamespace

    class TrackedBlob:
        def __init__(self, blob, job_id, reads):
            self.blob, self.job_id, self.reads = blob, job_id, reads

        def __enter__(self):
            self.blob.__enter__()
            return self

        def __exit__(self, *args):
            return self.blob.__exit__(*args)

        def __len__(self):
            return len(self.blob)

        def read(self, size):
            self.reads.append((self.job_id, size))
            return self.blob.read(size)

    class TrackedConnection:
        def __init__(self, db):
            self.db, self.reads = db, []

        def __getattr__(self, name):
            return getattr(self.db, name)

        def blobopen(self, table, column, job_id, **kwargs):
            return TrackedBlob(self.db.blobopen(table, column, job_id, **kwargs), job_id, self.reads)

    tracked = TrackedConnection(jobs_db.db)
    ledger = SimpleNamespace(db=tracked)
    limit = semantic_drain._REPROCESS_JOB_MAX_BYTES
    raw = '{"long_defers":{}}'
    with jobs_db.db:
        semantic_drain._sched_write(jobs_db, {}, commit=False)
        jobs_db.db.execute('UPDATE fetch_jobs SET payload=? WHERE kind=?',
                           (raw + ' ' * (limit - len(raw)), semantic_drain.SCHED_KIND))
    assert semantic_drain._reservation_sched(ledger)[1] == {}
    assert [size for _, size in tracked.reads] == [limit]
    tracked.reads.clear()
    with jobs_db.db:
        jobs_db.db.execute('UPDATE fetch_jobs SET payload=payload||? WHERE kind=?',
                           (' ', semantic_drain.SCHED_KIND))
    with pytest.raises(ValueError, match='semantic_scheduler_invalid'):
        semantic_drain._reservation_sched(ledger)
    assert tracked.reads == []


def test_oversized_changed_job_is_not_fetched_or_refunded(jobs_db):
    from types import SimpleNamespace

    now = time.time()
    job_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    token = runtime.JobToken.from_row(jobs_db.db.execute(
        'SELECT * FROM fetch_jobs WHERE job_id=?', (job_id,)).fetchone())
    reservation = semantic_drain._reserve_long_defer(jobs_db, token)
    with jobs_db.db:
        jobs_db.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?',
                           ('x' * (semantic_drain._REPROCESS_JOB_MAX_BYTES + 1), job_id))

    class NoReadBlob:
        def __init__(self, blob):
            self.blob = blob

        def __enter__(self):
            self.blob.__enter__()
            return self

        def __exit__(self, *args):
            return self.blob.__exit__(*args)

        def __len__(self):
            return len(self.blob)

        def read(self, size):
            pytest.fail('oversized job payload was read')

    class TrackedConnection:
        def __init__(self, db):
            self.db, self.opened = db, []

        def __getattr__(self, name):
            return getattr(self.db, name)

        def __enter__(self):
            self.db.__enter__()
            return self

        def __exit__(self, *args):
            return self.db.__exit__(*args)

        def blobopen(self, table, column, row_id, **kwargs):
            blob = self.db.blobopen(table, column, row_id, **kwargs)
            self.opened.append((row_id, len(blob)))
            return NoReadBlob(blob) if row_id == job_id else blob

    tracked = TrackedConnection(jobs_db.db)
    ledger = SimpleNamespace(db=tracked)
    assert semantic_drain._reserve_long_defer(ledger, token) is None
    semantic_drain._refund_long_defer(ledger, token, reservation)
    assert (job_id, semantic_drain._REPROCESS_JOB_MAX_BYTES + 1) in tracked.opened
    assert semantic_drain._long_defers(jobs_db)[str(job_id)] == reservation[1]


def test_blob_read_error_rolls_back_reservation(jobs_db):
    import sqlite3
    from types import SimpleNamespace

    now = time.time()
    job_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    token = runtime.JobToken.from_row(jobs_db.db.execute(
        'SELECT * FROM fetch_jobs WHERE job_id=?', (job_id,)).fetchone())
    with jobs_db.db:
        semantic_drain._sched_write(jobs_db, {'long_defers': {str(job_id): {'g': 'g40', 'n': 3}}},
                                    commit=False)
    before = jobs_db.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                (semantic_drain.SCHED_KIND,)).fetchone()[0]

    class BrokenConnection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def __enter__(self):
            self.db.__enter__()
            return self

        def __exit__(self, *args):
            return self.db.__exit__(*args)

        def blobopen(self, table, column, row_id, **kwargs):
            if row_id == job_id:
                raise sqlite3.DatabaseError('synthetic read failure')
            return self.db.blobopen(table, column, row_id, **kwargs)

    with pytest.raises(sqlite3.DatabaseError):
        semantic_drain._reserve_long_defer(SimpleNamespace(db=BrokenConnection(jobs_db.db)), token)
    assert not jobs_db.db.in_transaction
    assert jobs_db.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                              (semantic_drain.SCHED_KIND,)).fetchone()[0] == before


def test_oversized_serialized_scheduler_does_not_commit(jobs_db):
    now = time.time()
    job_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    token = runtime.JobToken.from_row(jobs_db.db.execute(
        'SELECT * FROM fetch_jobs WHERE job_id=?', (job_id,)).fetchone())
    raw = json.dumps({'long_defers': {}, 'padding': '合' * 200_000}, ensure_ascii=False)
    assert len(raw.encode('utf-8')) < semantic_drain._REPROCESS_JOB_MAX_BYTES
    with jobs_db.db:
        semantic_drain._sched_write(jobs_db, {}, commit=False)
        jobs_db.db.execute('UPDATE fetch_jobs SET payload=? WHERE kind=?',
                           (raw, semantic_drain.SCHED_KIND))
    with pytest.raises(ValueError, match='semantic_scheduler_invalid'):
        semantic_drain._reserve_long_defer(jobs_db, token)
    assert jobs_db.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                              (semantic_drain.SCHED_KIND,)).fetchone()[0] == raw
    assert not jobs_db.db.in_transaction


def test_unverified_blob_encoding_fails_closed():
    import sqlite3

    db = sqlite3.connect(':memory:')
    try:
        db.execute("PRAGMA encoding='UTF-16'")
        db.execute('CREATE TABLE fetch_jobs(job_id INTEGER PRIMARY KEY,payload TEXT)')
        db.execute('INSERT INTO fetch_jobs(payload) VALUES(?)', ('{}',))
        assert semantic_drain._bounded_job_payload(db, 1, 1024) is None
    finally:
        db.close()


def test_invalid_utf8_and_nontext_payload_fail_closed(jobs_db):
    now = time.time()
    job_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    with jobs_db.db:
        jobs_db.db.execute("UPDATE fetch_jobs SET payload=CAST(x'80' AS TEXT) WHERE job_id=?", (job_id,))
    assert semantic_drain._bounded_job_payload(jobs_db.db, job_id, 1024) is None
    with jobs_db.db:
        jobs_db.db.execute("UPDATE fetch_jobs SET payload=x'7b7d' WHERE job_id=?", (job_id,))
    assert semantic_drain._bounded_job_payload(jobs_db.db, job_id, 1024) is None


def test_multibyte_job_at_byte_limit_can_reserve(jobs_db):
    now = time.time()
    job_id = _job(jobs_db, 40, created=now, updated=now, next_try=now)
    base = json.dumps({VERSION_KEY: CURRENT_VERSION, 'generation': 'g40',
                       'padding': '合' * 100}, ensure_ascii=False)
    limit = semantic_drain._REPROCESS_JOB_MAX_BYTES
    raw = base + ' ' * (limit - len(base.encode('utf-8')))
    assert len(raw.encode('utf-8')) == limit
    with jobs_db.db:
        jobs_db.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?', (raw, job_id))
    token = runtime.JobToken.from_row(jobs_db.db.execute(
        'SELECT * FROM fetch_jobs WHERE job_id=?', (job_id,)).fetchone())
    reservation = semantic_drain._reserve_long_defer(jobs_db, token)
    assert reservation[1] == {'g': 'g40', 'n': 1}
    with jobs_db.db:
        jobs_db.db.execute('UPDATE fetch_jobs SET payload=payload||? WHERE job_id=?', (' ', job_id))
    semantic_drain._refund_long_defer(jobs_db, token, reservation)
    assert semantic_drain._long_defers(jobs_db)[str(job_id)] == reservation[1]


def test_legacy_payload_long_job_leaves_realtime(jobs_db, monkeypatch):
    now = time.time()
    long_id = _job(jobs_db, 50, created=now - 50, updated=now - 50,
                   next_try=now - 50)
    _job(jobs_db, 51, created=now - 900, updated=now - 900,
         next_try=now - 900)
    with jobs_db.db:                     # a payload from before "generation"
        jobs_db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?",
                           (json.dumps({VERSION_KEY: CURRENT_VERSION, "targets": [50], "eligible": True}),
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
    monkeypatch.setattr(semantic_transport, '_LONG_PATH',
                        str(tmp_path / "missing" / "\0bad" / "long.json"))
    semantic_transport._long_mark("k1", 1)
    err = capsys.readouterr().err
    assert '"event": "semantic_long_mark_unsaved"' in err
    assert "k1" not in err                       # never the key/prompt
    assert semantic_transport.long_mark_failures()["error"] == 1
    assert semantic_transport._long_marks()["k1"] == 1     # suppression still holds


def test_long_mark_lock_contention_counts_busy(monkeypatch, tmp_path):
    import fcntl
    path = tmp_path / "long.json"
    monkeypatch.setattr(semantic_transport, '_LONG_PATH', str(path))
    with open(str(path) + ".lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        semantic_transport._long_mark("k2", 2)
    assert semantic_transport.long_mark_failures()["busy"] == 1
    semantic_transport._long_mark("k3", 1)                 # next save persists both
    saved = json.loads(path.read_text())
    assert saved == {"k2": 2, "k3": 1} and semantic_transport._LONG_UNSAVED == {}
