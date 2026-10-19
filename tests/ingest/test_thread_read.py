"""Per-thread read acknowledgement (owner report 2026-10-06): stored
replies stayed unread on MCS because a reply's unread state lives on
its thread. Synthetic stubs only — no real MCS."""
import mcs_contract
import json
import time
from types import SimpleNamespace

import pytest

import mcs_adapter
import run_check
from ingest_testkit import _ledger


@pytest.fixture
def db(tmp_path):
    store = _ledger(tmp_path)
    try:
        yield store
    finally:
        store.close()


def _store(db, mid, parent=None, pid=1, first_seen=None, body_state="full"):
    db.db.execute(
        "INSERT INTO messages(message_id,project_id,parent_id,posted_at,"
        "posted_at_ts,body_text,body_state,content_hash,first_seen) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (mid, pid, parent, "2026-10-06T09:00", 1, "本文", body_state,
         f"{mid:064x}", time.time() if first_seen is None else first_seen))
    db.db.commit()


class _Wire(mcs_adapter.MCSAdapter):
    def __init__(self, routes):
        super().__init__()
        self.routes, self.calls = routes, []

    def _get(self, path, params=None, extend_session=True):
        self.calls.append((path, dict(params or {})))
        return self.routes(path, params or {})

    def _request(self, *a, **k):
        pytest.fail("external request forbidden")


def test_thread_unread_reads_the_root_marker_without_keep_read_status():
    marker = {"messages": [{"id": 10, "oldest_unread_thread_message": {"id": 11}}]}
    wire = _Wire(lambda path, params: marker)
    assert wire.thread_unread(1, 10) is True
    path, params = wire.calls[0]
    assert path == "/messages" and params == {
        "message_ids": 10, "include_oldest_unread_thread_message_id": 1}
    assert _Wire(lambda p, q: {"messages": [{"id": 10}]}).thread_unread(1, 10) is False
    with pytest.raises(mcs_contract.SchemaError):
        _Wire(lambda p, q: {"messages": []}).thread_unread(1, 10)


def test_read_thread_walks_every_page_without_keep_read_status():
    pages = {1: ([11, 12], True), 2: ([13], False)}

    def routes(path, params):
        ids, more = pages[params["page"]]
        return {"messages": [{"id": i, "comment": "x",
                              "created_at": "2026-10-06T09:00:00+09:00"} for i in ids],
                "paginate": {"has_next": more}}
    wire = _Wire(routes)
    assert wire.read_thread(1, 10) == {11, 12, 13}
    assert all("keep_read_status" not in params for _, params in wire.calls)
    assert [p for p, _ in wire.calls] == ["/projects/1/messages/10/messages"] * 2


@pytest.mark.parametrize("paginate,kind", [
    ([], "schema_error"), ({"has_next": True}, "thread_incomplete"),
])
def test_read_thread_does_not_certify_invalid_or_incomplete_pages(paginate, kind):
    wire = _Wire(lambda p, q: {"messages": [], "paginate": paginate})
    with pytest.raises(mcs_contract.MCSError) as error:
        wire.read_thread(1, 10, max_pages=1)
    assert error.value.kind == kind


def _adapter(unread, server, seen=None):
    """unread: successive thread_unread answers."""
    answers = list(unread)
    calls = []
    return SimpleNamespace(
        calls=calls,
        thread_unread=lambda pid, parent: calls.append("check") or answers.pop(0),
        fetch_thread=lambda pid, parent: [SimpleNamespace(message_id=i) for i in server],
        read_thread=lambda pid, parent: calls.append("clear") or set(seen or server))


def _result():
    return {"errors": []}


def test_stored_unread_thread_is_cleared_and_confirmed_once(db):
    _store(db, 10)
    _store(db, 11, parent=10)
    adapter = _adapter([True, False], server=[11])
    result = _result()
    run_check.stage_thread_read(adapter, db, result, time.monotonic() + 120)
    assert adapter.calls == ["check", "clear", "check"]
    assert result["threads_marked_read"] == [10]
    row = db.db.execute("SELECT status,last_reply_id FROM thread_read_marks").fetchone()
    assert tuple(row) == ("confirmed", 11)
    # confirmed: the next tick does not touch the thread again
    again = _adapter([], server=[11])
    run_check.stage_thread_read(again, db, _result(), time.monotonic() + 120)
    assert again.calls == []
    # a newer stored reply makes it a candidate again
    _store(db, 12, parent=10)
    assert [tuple(r) for r in db.thread_read_candidates(0, 10)] == [(1, 10, 12)]


@pytest.mark.parametrize("body_state", ["snippet", "unknown"])
def test_incomplete_stored_reply_is_fetched_without_clearing(db, body_state):
    _store(db, 11, parent=10, body_state=body_state)
    adapter = _adapter([True], server=[11])
    run_check.stage_thread_read(adapter, db, _result(), time.monotonic() + 120)
    assert adapter.calls == ["check"]
    assert db.stored_reply_ids(10) == set()
    jobs = db.db.execute("SELECT kind,message_id,parent_id FROM fetch_jobs").fetchall()
    assert [tuple(j) for j in jobs] == [("reply", 11, 10)]


def test_unstored_reply_blocks_the_clear_and_is_fetched_first(db):
    _store(db, 10)
    _store(db, 11, parent=10)
    adapter = _adapter([True], server=[11, 12])
    run_check.stage_thread_read(adapter, db, _result(), time.monotonic() + 120)
    assert adapter.calls == ["check"]                 # never cleared
    jobs = db.db.execute("SELECT kind,message_id,parent_id FROM fetch_jobs").fetchall()
    assert [tuple(j) for j in jobs] == [("reply", 12, 10)]
    assert db.db.execute("SELECT count(*) FROM thread_read_marks").fetchone()[0] == 0


def test_foreign_patient_reply_is_reported_never_requeued_and_never_cleared(db):
    _store(db, 10)
    _store(db, 11, parent=10, pid=2, first_seen=time.time() - 30 * 86400)
    _store(db, 12, parent=10)
    for _tick in range(2):
        adapter = _adapter([True], server=[11, 12])
        result = _result()
        run_check.stage_thread_read(adapter, db, result, time.monotonic() + 120)
        # a refetch cannot move message 11 to patient 1: nothing is queued,
        # and patient 1 has not stored it, so the thread is not cleared
        assert "thread_read: reply_stored_for_other_patient" in result["errors"]
        assert adapter.calls == ["check"]
        assert result.get("threads_marked_read", []) == []
        assert db.db.execute("SELECT COUNT(*) FROM fetch_jobs").fetchone()[0] == 0
        assert db.db.execute("SELECT COUNT(*) FROM thread_read_marks").fetchone()[0] == 0
    # once the reply is stored for this patient the normal clear resumes
    with db.db:
        db.db.execute("UPDATE messages SET project_id=1 WHERE message_id=11")
    adapter = _adapter([True, False], server=[11, 12])
    result = _result()
    run_check.stage_thread_read(adapter, db, result, time.monotonic() + 120)
    assert result["errors"] == []
    assert adapter.calls == ["check", "clear", "check"]
    assert result["threads_marked_read"] == [10]


@pytest.mark.parametrize("body_state,stored_parent", [
    ("snippet", 10), ("unknown", 10), ("full", 99)])
def test_foreign_reply_is_detected_by_id_whatever_its_body_or_parent(
        db, body_state, stored_parent):
    # an unsettled body without a failed job, or the same id under another
    # parent, is still owned by patient 2: a refetch for patient 1 is refused
    _store(db, 10)
    _store(db, 11, parent=stored_parent, pid=2, body_state=body_state,
           first_seen=time.time() - 30 * 86400)
    _store(db, 12, parent=10)
    adapter = _adapter([True], server=[11, 12])
    result = _result()
    run_check.stage_thread_read(adapter, db, result, time.monotonic() + 120)
    assert "thread_read: reply_stored_for_other_patient" in result["errors"]
    assert db.db.execute("SELECT COUNT(*) FROM fetch_jobs").fetchone()[0] == 0
    assert adapter.calls == ["check"]
    assert db.db.execute("SELECT COUNT(*) FROM thread_read_marks").fetchone()[0] == 0


def test_message_owners_reads_every_stored_id_across_chunks(db):
    for mid in range(1000, 1501):
        _store(db, mid, parent=10, pid=1 + mid % 2, body_state="unknown")
    owners = db.message_owners([*range(1000, 1501), 99999])
    assert len(owners) == 501 and 99999 not in owners
    assert owners[1000] == 1 and owners[1001] == 2 and owners[1500] == 1


def test_foreign_patient_reply_does_not_stand_in_for_an_unstored_one(db):
    _store(db, 10)
    _store(db, 11, parent=10, pid=2, first_seen=time.time() - 30 * 86400)
    _store(db, 12, parent=10)
    adapter = _adapter([True], server=[11, 12, 13])
    result = _result()
    run_check.stage_thread_read(adapter, db, result, time.monotonic() + 120)
    # only 13 (stored nowhere) is fetched; the foreign 11 is reported, not queued
    assert [tuple(row) for row in db.db.execute(
        "SELECT kind,project_id,message_id FROM fetch_jobs")] == [("reply", 1, 13)]
    assert "thread_read: reply_stored_for_other_patient" in result["errors"]
    assert adapter.calls == ["check"]
    assert db.db.execute("SELECT COUNT(*) FROM thread_read_marks").fetchone()[0] == 0


def test_scoped_stored_reply_ids_keep_settled_body_and_failed_job_contract(db):
    for mid, state, pid in [(11, "full", 1), (12, "deleted", 1),
                            (13, "snippet", 1), (14, "unknown", 1),
                            (15, "snippet", 1), (21, "full", 2),
                            (22, "unknown", 2)]:
        _store(db, mid, parent=10, pid=pid, body_state=state)
    for mid, pid in [(13, 1), (14, 1), (22, 2)]:
        db.job_add("reply", pid, mid, parent_id=10)
    with db.db:
        db.db.execute("UPDATE fetch_jobs SET state='failed'")
    assert db.stored_reply_ids(10) == {11, 12, 13, 14, 21, 22}
    assert db.stored_reply_ids(10, project_id=1) == {11, 12, 13, 14}
    assert db.stored_reply_ids(10, project_id=2) == {21, 22}
    assert db.stored_reply_ids(10, project_id=3) == set()


def test_already_read_thread_is_confirmed_without_a_clearing_read(db):
    _store(db, 11, parent=10)
    adapter = _adapter([False], server=[11])
    run_check.stage_thread_read(adapter, db, _result(), time.monotonic() + 120)
    assert adapter.calls == ["check"]
    assert db.db.execute("SELECT status FROM thread_read_marks").fetchone()[0] == "confirmed"


def test_unconfirmed_clear_stays_unknown_and_a_racing_reply_is_fetched(db):
    _store(db, 11, parent=10)
    adapter = _adapter([True, True], server=[11], seen=[11, 13])
    run_check.stage_thread_read(adapter, db, _result(), time.monotonic() + 120)
    assert db.db.execute("SELECT status FROM thread_read_marks").fetchone()[0] == "unknown"
    jobs = [tuple(j) for j in db.db.execute(
        "SELECT kind,message_id,parent_id FROM fetch_jobs")]
    assert jobs == [("reply", 13, 10)]
    # unknown is retried on the next tick
    assert [tuple(r) for r in db.thread_read_candidates(0, 10)] == [(1, 10, 11)]


def test_failed_clear_keeps_durable_unknown_intent(db):
    _store(db, 11, parent=10)
    adapter = _adapter([True], server=[11])

    def fail(pid, parent):
        assert db.db.execute("SELECT status FROM thread_read_marks").fetchone()[0] == "unknown"
        raise mcs_contract.MCSError("network_error", retryable=True)

    adapter.read_thread = fail
    result = _result()
    run_check.stage_thread_read(adapter, db, result, time.monotonic() + 120)
    assert result["threads_marked_read"] == []
    assert result["errors"]
    assert db.db.execute("SELECT status FROM thread_read_marks").fetchone()[0] == "unknown"


def _partial_read_failure(failure):
    error = (mcs_contract.SessionExpired("synthetic expiry", status=401)
             if failure == "session" else
             mcs_contract.MCSError("network_error", "synthetic failure", retryable=True))

    def routes(path, params):
        page = params["page"]
        if page > 1 and failure in ("network", "session"):
            raise error
        ids = [11, 13] if page == 1 else [11]
        messages = [{"id": mid, "comment": "synthetic reply"} for mid in ids]
        if page > 1 and failure == "schema":
            # A partially valid but malformed page must not contribute IDs.
            messages = [{"id": 14, "comment": "synthetic reply"}, {"id": "invalid"}]
        return {"messages": messages, "paginate": {"has_next": True}}

    return _Wire(routes), error


@pytest.mark.parametrize("failure", ["network", "session", "schema", "page_cap"])
def test_partial_thread_read_preserves_only_validated_page_ids(failure):
    wire, original = _partial_read_failure(failure)
    with pytest.raises(mcs_contract.MCSError) as caught:
        wire.read_thread(1, 10, max_pages=2)
    error = caught.value
    assert error.observed_reply_ids == frozenset({11, 13})
    if failure in ("network", "session"):
        assert error is original
        expected = (("synthetic expiry", 401, False) if failure == "session"
                    else ("synthetic failure", None, True))
        assert (error.detail, error.status, error.retryable) == expected
    else:
        assert error.kind == ("schema_error" if failure == "schema" else "thread_incomplete")
    assert all("keep_read_status" not in params for _, params in wire.calls)


@pytest.mark.parametrize("failure", ["network", "session", "schema", "page_cap"])
def test_failed_thread_read_keeps_racing_reply_job_and_unread_evidence(db, failure):
    _store(db, 11, parent=10)
    wire, original = _partial_read_failure(failure)
    wire.thread_unread = lambda pid, parent: True
    wire.fetch_thread = lambda pid, parent: [SimpleNamespace(message_id=11)]
    result = _result()
    if failure == "session":
        with pytest.raises(mcs_contract.SessionExpired) as caught:
            run_check.stage_thread_read(wire, db, result, time.monotonic() + 120)
        assert caught.value is original
    else:
        run_check.stage_thread_read(wire, db, result, time.monotonic() + 120)
        assert result["errors"]
    assert result["threads_marked_read"] == []
    assert db.db.execute("SELECT status FROM thread_read_marks").fetchone()[0] == "unknown"
    jobs = [dict(row) for row in db.db.execute(
        "SELECT kind,message_id,parent_id,payload FROM fetch_jobs")]
    assert len(jobs) == 1
    assert (jobs[0]["kind"], jobs[0]["message_id"], jobs[0]["parent_id"]) == ("reply", 13, 10)
    assert json.loads(jobs[0]["payload"]) == {"unread": True}
    # Once the server reports read, the following tick must keep the job.
    run_check.stage_thread_read(_adapter([False], server=[11]), db, _result(),
                                time.monotonic() + 120)
    assert db.job_pending("reply", 1, 13) is not None
    assert db.db.execute("SELECT count(*) FROM fetch_jobs").fetchone()[0] == 1


def test_old_replies_and_mcs_errors_stay_bounded(db):
    _store(db, 11, parent=10, first_seen=time.time() - 30 * 86400)
    assert db.thread_read_candidates(time.time() - run_check.THREAD_READ_WINDOW_S, 10) == []
    _store(db, 21, parent=20)

    def boom(pid, parent):
        raise mcs_contract.MCSError("http_error", "x", status=500)
    adapter = SimpleNamespace(thread_unread=boom)
    result = _result()
    run_check.stage_thread_read(adapter, db, result, time.monotonic() + 120)
    assert result["errors"] and result["errors"][0].startswith("thread_read:")
