"""Synthetic Ledger query integration for structural root/reply pairs."""
import pytest

from ledger import Ledger
from test_interaction_latency import NOW, pair, post
from mcs_queries import coverage_gaps, incomplete_reply_roots, thread_reply_pairs


@pytest.fixture
def store(tmp_path):
    lg = Ledger(str(tmp_path / "synthetic-pairs.db"))
    yield lg
    lg.close()


def test_root_half_open_scope_and_reply_observation_horizon(store):
    # Given: root at cohort start, reply after cohort end, another root at end.
    pair(store, 10, delay=120)
    post(store, 20, ts=NOW + 60)
    post(store, 30, pid=2)
    # When
    rows = thread_reply_pairs(store.db, since=NOW, until=NOW + 60,
                              as_of=NOW + 200, project_id=1)
    # Then
    assert len(rows) == 1
    assert rows[0]["root_id"] == 10 and rows[0]["reply_id"] == 11
    assert rows[0]["reply_ts"] - rows[0]["root_ts"] == 120


def test_missing_time_does_not_pick_a_later_timed_reply(store):
    # Given
    pair(store, 10, replies=2)
    post(store, 12, parent=10, ts=None)
    # When
    rows = thread_reply_pairs(store.db, as_of=NOW + 1000)
    # Then
    assert rows[0]["reply_id"] == 12
    assert rows[0]["reply_ts"] is None
    assert rows[0]["stored_full_replies"] == 2


def test_foreign_project_deleted_partial_and_future_replies_are_not_selected(store):
    # Given
    post(store, 10, replies=3)
    post(store, 11, parent=10, state="deleted")
    post(store, 12, parent=10, state="snippet")
    post(store, 13, pid=2, parent=10)
    post(store, 14, parent=10, ts=NOW + 2000)
    # When
    rows = thread_reply_pairs(store.db, as_of=NOW + 1000, project_id=1)
    # Then
    assert rows[0]["reply_id"] is None
    assert rows[0]["stored_full_replies"] == 1


def test_reply_tie_break_and_unplaced_roots_are_deterministic(store):
    # Given
    pair(store, 10, replies=2)
    post(store, 12, parent=10, ts=NOW + 60)
    post(store, 20, ts=None)
    post(store, 30, ts=NOW - 1)
    # When
    rows = thread_reply_pairs(store.db, since=NOW, until=NOW + 1, as_of=NOW + 1000)
    # Then
    assert [(r["root_id"], r["reply_id"]) for r in rows] == [(10, 11), (20, None)]


@pytest.mark.parametrize("room_count", [0, 1, 20])
def test_coverage_gaps_preserves_counts_with_constant_query_count(store, room_count):
    # Given: full/deleted replies complete one root; snippets do not count.
    with store.db:
        for pid in range(1, room_count + 1):
            store.db.execute(
                "INSERT INTO patients(project_id,is_archived) VALUES(?,?)",
                (pid, int(pid == 20)))
            store.db.execute(
                "INSERT INTO messages(message_id,project_id,body_state,reply_count) "
                "VALUES(?,?,'full',2)", (pid * 10, pid))
        if room_count:
            store.db.execute(
                "INSERT INTO messages(message_id,project_id,parent_id,body_state) "
                "VALUES(11,1,10,'full'),(12,1,10,'deleted'),(13,1,10,'snippet')")
    expected = sum(incomplete_reply_roots(store.db, pid)
                   for pid in range(1, min(room_count, 19) + 1))
    queries = []
    store.db.set_trace_callback(queries.append)
    # When
    try:
        result = coverage_gaps(store.db)
    finally:
        store.db.set_trace_callback(None)
    # Then
    assert result == {"incomplete_rooms": [], "jobs": {},
                      "partial_bodies": int(bool(room_count)), "reply_gaps": expected}
    assert expected == max(0, min(room_count, 19) - 1)
    assert sum(query.lstrip().upper().startswith("SELECT") for query in queries) == 4
