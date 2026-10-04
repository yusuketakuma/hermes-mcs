"""Synthetic Ledger query integration for structural root/reply pairs."""
import pytest

from ledger import Ledger
from test_interaction_latency import NOW, pair, post
from mcs_queries import thread_reply_pairs


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
