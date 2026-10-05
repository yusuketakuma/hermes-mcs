"""Per-thread read acknowledgement (owner report 2026-10-06): stored
replies stayed unread on MCS because a reply's unread state lives on
its thread. Synthetic stubs only — no real MCS."""
import time
from types import SimpleNamespace

import pytest

import mcs_adapter
import run_check
from ingest_testkit import _ledger


def _store(db, mid, parent=None, pid=1, first_seen=None):
    db.db.execute(
        "INSERT INTO messages(message_id,project_id,parent_id,posted_at,"
        "posted_at_ts,body_text,body_state,content_hash,first_seen) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (mid, pid, parent, "2026-10-06T09:00", 1, "本文", "full",
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
    with pytest.raises(mcs_adapter.SchemaError):
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


def test_stored_unread_thread_is_cleared_and_confirmed_once(tmp_path):
    db = _ledger(tmp_path)
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


def test_unstored_reply_blocks_the_clear_and_is_fetched_first(tmp_path):
    db = _ledger(tmp_path)
    _store(db, 10)
    _store(db, 11, parent=10)
    adapter = _adapter([True], server=[11, 12])
    run_check.stage_thread_read(adapter, db, _result(), time.monotonic() + 120)
    assert adapter.calls == ["check"]                 # never cleared
    jobs = db.db.execute("SELECT kind,message_id,parent_id FROM fetch_jobs").fetchall()
    assert [tuple(j) for j in jobs] == [("reply", 12, 10)]
    assert db.db.execute("SELECT count(*) FROM thread_read_marks").fetchone()[0] == 0


def test_already_read_thread_is_confirmed_without_a_clearing_read(tmp_path):
    db = _ledger(tmp_path)
    _store(db, 11, parent=10)
    adapter = _adapter([False], server=[11])
    run_check.stage_thread_read(adapter, db, _result(), time.monotonic() + 120)
    assert adapter.calls == ["check"]
    assert db.db.execute("SELECT status FROM thread_read_marks").fetchone()[0] == "confirmed"


def test_unconfirmed_clear_stays_unknown_and_a_racing_reply_is_fetched(tmp_path):
    db = _ledger(tmp_path)
    _store(db, 11, parent=10)
    adapter = _adapter([True, True], server=[11], seen=[11, 13])
    run_check.stage_thread_read(adapter, db, _result(), time.monotonic() + 120)
    assert db.db.execute("SELECT status FROM thread_read_marks").fetchone()[0] == "unknown"
    jobs = [tuple(j) for j in db.db.execute(
        "SELECT kind,message_id,parent_id FROM fetch_jobs")]
    assert jobs == [("reply", 13, 10)]
    # unknown is retried on the next tick
    assert [tuple(r) for r in db.thread_read_candidates(0, 10)] == [(1, 10, 11)]


def test_old_replies_and_mcs_errors_stay_bounded(tmp_path):
    db = _ledger(tmp_path)
    _store(db, 11, parent=10, first_seen=time.time() - 30 * 86400)
    assert db.thread_read_candidates(time.time() - run_check.THREAD_READ_WINDOW_S, 10) == []
    _store(db, 21, parent=20)

    def boom(pid, parent):
        raise mcs_adapter.MCSError("http_error", "x", status=500)
    adapter = SimpleNamespace(thread_unread=boom)
    result = _result()
    run_check.stage_thread_read(adapter, db, result, time.monotonic() + 120)
    assert result["errors"] and result["errors"][0].startswith("thread_read:")
