"""Local cross-list state, publication gates, immutable complete sets and snapshots."""
import json
import sqlite3

import pytest

from cross_lists import ARTIFACT_KIND, sync_cross_list
from cross_lists_view import get_cross_list
from ledger import LedgerReader, publish_snapshot
import test_cross_lists as wire
from test_cross_lists import MID, PID, message, page

replay = wire.replay
store = wire.store


def capture(store, replay, rows=None, *, now=100, dataset="mentioned", unread_only=False):
    client, _, _ = replay([page([message(is_unread=True)] if rows is None else rows)])
    return sync_cross_list(store, client, dataset, enabled=True, now=now,
                           unread_only=unread_only)


@pytest.mark.parametrize("rows,state", [([], "empty"), ([message()], "complete")])
def test_publication_opt_in_empty_unknown_and_nonempty(store, replay, rows, state):
    assert get_cross_list(store.db, "mentioned")["state"] == "disabled"
    assert get_cross_list(store.db, "mentioned", enabled=True)["state"] == "unknown"
    capture(store, replay, rows)
    assert get_cross_list(store.db, "mentioned", now=100)["rows"] == []
    view = get_cross_list(store.db, "mentioned", enabled=True, now=100)
    assert view["state"] == state and view["current_known"]
    assert view["unread_preservation"] == view["session_preservation"] == "unverified"


def test_failure_keeps_last_complete_historical_then_complete_empty_replaces(store, replay):
    capture(store, replay)
    client, pending, _ = replay([page([message(MID + 1)], has_next=True), 401])
    sync_cross_list(store, client, "mentioned", enabled=True, now=110)
    assert not pending
    view = get_cross_list(store.db, "mentioned", enabled=True, now=120)
    assert view["state"] == "failed" and not view["current_known"] and view["historical"]
    assert view["http_status"] == 401 and view["last_complete_at"] == 100
    assert [r["message_id"] for r in view["rows"]] == [MID]
    capture(store, replay, [], now=130)
    view = get_cross_list(store.db, "mentioned", enabled=True, now=130)
    assert view["state"] == "empty" and view["current_known"] and view["rows"] == []
    assert len(store.artifacts(ARTIFACT_KIND)) == 3


@pytest.mark.parametrize("now,state", [(200, "complete"), (201, "stale"), (99, "stale")])
def test_expiry_and_clock_rollback(store, replay, now, state):
    capture(store, replay)
    view = get_cross_list(store.db, "mentioned", enabled=True, now=now, max_age_s=100)
    assert view["state"] == state and view["historical"] == (state == "stale")


def test_datasets_and_filters_are_separate_sets(store, replay):
    capture(store, replay)
    capture(store, replay, [], dataset="bookmarked")
    capture(store, replay, [], unread_only=True)
    assert get_cross_list(store.db, "mentioned", enabled=True, now=100)["state"] == "complete"
    assert get_cross_list(store.db, "mentioned", enabled=True,
                          unread_only=True, now=100)["state"] == "empty"
    assert get_cross_list(store.db, "bookmarked", enabled=True, now=100)["state"] == "empty"


@pytest.mark.parametrize("field,value", [
    ("rows", [{"project_id": PID}]), ("complete", "yes"),
    ("attempted_at", float("nan")), ("reason", "unsupported"),
    ("attempted_at", 10 ** 400),
    ("rows", None), ("timestamp", True),
])
def test_invalid_artifact_is_unknown_not_empty(store, replay, field, value):
    capture(store, replay)
    payload = json.loads(store.artifacts(ARTIFACT_KIND)[0]["content"])
    payload[field] = value
    store.artifact_add(ARTIFACT_KIND, json.dumps(payload))
    view = get_cross_list(store.db, "mentioned", enabled=True, now=100)
    assert view["state"] == "unknown" and view["reason"] == "artifact_invalid"
    assert view["rows"] == [] and not view["current_known"]


@pytest.mark.parametrize("option", ["now", "max_age_s"])
def test_oversized_numeric_options_are_rejected_before_sql(option):
    with pytest.raises(ValueError, match="cross list view options"):
        get_cross_list(None, "mentioned", enabled=True, **{option: 10 ** 400})


def test_metadata_capture_can_be_read_without_source_body_or_false_unread(store, replay):
    capture(store, replay, [message(comment=None, mentions=[], is_bookmarked=False)])
    view = get_cross_list(store.db, "mentioned", enabled=True, now=100)
    assert view["rows"][0]["is_unread"] is None
    assert view["rows"][0]["metadata"] == {"mentions": [], "is_bookmarked": False}
    assert view["rows"][0]["body_state"] == "unknown"
    assert "body_html" not in view["rows"][0]


def test_snapshot_read_and_scope_recheck(store, replay, tmp_path):
    capture(store, replay)
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    snapshot = publish_snapshot(path, str(tmp_path / "snapshot"))
    reader = LedgerReader(snapshot)
    try:
        view = get_cross_list(reader.db, "mentioned", enabled=True, now=100)
        assert view["state"] == "complete"
        with pytest.raises(sqlite3.OperationalError):
            reader.db.execute("DELETE FROM artifacts")
    finally:
        reader.close()
    from mcs_adapter import _norm_message
    store.save_messages([_norm_message(message(), PID + 1)], project_id=PID + 1)
    view = get_cross_list(store.db, "mentioned", enabled=True, now=100)
    assert view["state"] == "unknown" and view["rows"] == []
