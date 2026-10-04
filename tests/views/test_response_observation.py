"""Synthetic snapshot/render regression corpus for independent response observations."""
import json

import pytest

from ledger import Ledger, publish_snapshot
from mcs_adapter import _norm_message
from mcs_signals import record_station_staff
from mcs_view import View
from message_metadata import get_message_metadata, self_reaction_text
from notify_render import card_reaction_lines

NOW = 1_800_000_000.25
SELF = 11


def _reaction(kind="completed", mine=True):
    return [{"type": kind, "count": 1, "self_reacted": mine}]


def _post(store, mid=1, sender=SELF, parent=None, at=NOW - 100, **metadata):
    message = _norm_message({
        "id": mid, "comment": "fully synthetic request",
        "user": {"id": sender}, "thread_message_id": parent, **metadata,
    }, 1)
    # The synthetic fixture sets the known parent and exact source timestamp.
    message.parent_id = parent
    store.save_messages([message], project_id=1, notify=False)
    with store.db:
        store.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?", (at, mid))
    return message


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("ledger.time.time", lambda: NOW)
    ledger = Ledger(str(tmp_path / "synthetic.db"))
    ledger.ensure_patient(1)
    with ledger.db:
        record_station_staff(ledger.db, [{"staff_id": SELF, "is_self": True}])
    yield ledger
    ledger.close()


def _evidence(store, tmp_path, mid=1):
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    snapshot = publish_snapshot(path, str(tmp_path / "snapshot"))
    assert snapshot is not None
    view = View(snapshot)
    try:
        return view.read("evidence", project=1, message_id=mid)["message"]
    finally:
        view.close()


@pytest.mark.parametrize(("fields", "state", "types"), [
    ({}, "not_fetched", []),
    ({"reactions": []}, "observed", []),
    ({"reactions": _reaction()}, "observed", ["completed"]),
    ({"reactions": _reaction("accepted")}, "observed", ["accepted"]),
    ({"reactions": _reaction(mine=False)}, "observed", []),
    ({"reactions": _reaction("synthetic-private-name")}, "observed", ["unknown"]),
    ({"reactions": ["broken"]}, "invalid", []),
])
def test_snapshot_and_render_keep_operation_evidence_separate(store, tmp_path, fields, state, types):
    # Given: capture, including missing/invalid/unknown and other people's stamps.
    _post(store, sender=22, **fields)
    # When: the actual snapshot evidence API and card renderer consume it.
    evidence = _evidence(store, tmp_path)
    metadata = evidence["message_metadata"]
    observation = metadata["response_observation"]
    rendered = card_reaction_lines([(1, metadata)])
    # Then: operation evidence cannot complete a clinical request.
    assert observation["self_reaction"]["state"] == state
    assert observation["self_reaction"]["types"] == types
    assert observation["self_reaction"]["basis"] == "ui_operation_only"
    assert observation["reply"] == {"state": "not_observed", "posted_at": None}
    assert all(observation[k] is None for k in ("clinical_completion", "nonresponse", "unread"))
    assert rendered == [self_reaction_text(metadata)]
    assert "synthetic-private-name" not in json.dumps(observation)


@pytest.mark.parametrize("unread", [None, False, True])
def test_missing_self_id_and_unread_never_prove_nonresponse(store, tmp_path, unread):
    # Given: no unique self ID, regardless of the adapter's unread flag.
    with store.db:
        store.db.execute("DELETE FROM artifacts WHERE kind='station_staff_v1'")
    fields = {} if unread is None else {"is_unread": unread}
    _post(store, sender=22, reactions=_reaction(), **fields)
    _post(store, mid=2, parent=1, at=NOW - 10)
    # When: querying the published API.
    result = _evidence(store, tmp_path)["message_metadata"]["response_observation"]
    # Then: the account's UI stamp is evidence, but reply identity/read status are unknown.
    assert result["reply"]["state"] == "unknown"
    assert result["self_reaction"]["types"] == ["completed"]
    assert result["nonresponse"] is None and result["unread"] is None


@pytest.mark.parametrize(("parent", "at", "deleted", "expected"), [
    (1, NOW - 10.125, False, NOW - 10.125),
    (1, NOW - 100, False, None),  # simultaneous, not a later reply
    (1, NOW - 101, False, None),
    (1, NOW + 1, False, None),  # beyond snapshot as_of
    (3, NOW - 10, False, None),  # another thread
    (1, NOW - 10, True, None),
])
def test_reply_and_stamp_coexist_without_deadline_or_completion_inference(
        store, tmp_path, parent, at, deleted, expected):
    # Given: a request with a completed stamp and a reply candidate.
    _post(store, sender=22, reactions=_reaction())
    _post(store, mid=3, sender=22)
    _post(store, mid=2, parent=parent, at=at)
    with store.db:
        if deleted:
            store.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=2")
        store.db.execute("INSERT INTO requests(project_id,source_message_id,source_hash,"
                         "title,due_date,status,revision,created_at,updated_at) "
                         "VALUES(1,1,'synthetic','synthetic','2000-01-01','open',1,?,?)",
                         (NOW, NOW))
    # When: observing through the real snapshot API, even long past the due date.
    observation = _evidence(store, tmp_path)["message_metadata"]["response_observation"]
    # Then: exact reply time and stamp evidence are independent of request status.
    assert observation["reply"] == {
        "state": "observed" if expected is not None else "not_observed", "posted_at": expected}
    assert observation["self_reaction"]["types"] == ["completed"]
    assert observation["clinical_completion"] is None and observation["nonresponse"] is None
    assert store.db.execute("SELECT status FROM requests").fetchone()[0] == "open"


def test_removed_reaction_and_failed_refresh_do_not_resurrect_completed_stamp(store, tmp_path, monkeypatch):
    # Given: an old completed reaction, then an explicitly observed empty capture.
    _post(store, sender=22, reactions=_reaction())
    monkeypatch.setattr("ledger.time.time", lambda: NOW + 10.5)
    message = _post(store, sender=22, reactions=[])
    store.save_metadata_shadow(message, error="deadline_exceeded", publish=True)
    # When: rendering the published capture and separate failed shadow.
    evidence = _evidence(store, tmp_path)
    stamp = evidence["message_metadata"]["response_observation"]["self_reaction"]
    # Then: cancellation remains current; failed shadow never republishes old values.
    assert stamp["types"] == [] and stamp["state"] == "observed"
    assert stamp["observed_at"] == NOW + 10.5
    assert evidence["metadata_shadow"]["state"] == "failed"
    assert evidence["metadata_shadow"]["last_error"] == "deadline_exceeded"
    assert evidence["metadata_shadow"]["displayed"] is False


def test_old_and_failed_capture_keep_exact_observation_age(store, tmp_path):
    # Given: an old observation whose later check failed, with an independently stored reply.
    _post(store, sender=22, reactions=_reaction())
    _post(store, mid=2, parent=1, at=NOW - 10.125)
    with store.db:
        store.db.execute("UPDATE message_metadata SET content=json_set(content,"
                         "'$.reactions.observed_at',?),checked_at=?,last_error='network_error' "
                         "WHERE message_id=1", (NOW - 30 * 86400 - .5, NOW - 1))
    # When: querying the real snapshot API.
    result = _evidence(store, tmp_path)["message_metadata"]["response_observation"]
    # Then: no guessed expiry threshold erases timestamps or promotes history to success.
    assert result["self_reaction"]["state"] == "failed"
    assert result["self_reaction"]["observed_at"] == NOW - 30 * 86400 - .5
    assert result["self_reaction"]["age_s"] == 30 * 86400 + .5
    assert result["self_reaction"]["current_state"] == "unknown"
    assert result["reply"]["posted_at"] == NOW - 10.125
    assert result["clinical_completion"] is None and result["nonresponse"] is None


@pytest.mark.parametrize("state", ["complete", "stale", "failed", "cancelled"])
def test_actor_history_and_cancelled_set_through_snapshot_api(store, tmp_path, state):
    # Given: a complete historical actor set, with expiry/failure or a complete empty replacement.
    _post(store, reactions=_reaction())
    complete_at = NOW - (86400 if state == "stale" else 10)
    store.save_reaction_actors(1, [{"actor_id": 22, "reaction_type": "completed",
                                  "profession": "医師"}], True, now=complete_at)
    if state == "cancelled":
        store.save_reaction_actors(1, [], True, now=NOW)
        store.save_reaction_actors(1, [], False, error="deadline_exceeded", now=NOW)
    elif state == "failed":
        store.save_reaction_actors(1, [], False, error="http_error", now=NOW)
    else:
        # Set capture observation before the complete walk, so expiry alone controls staleness.
        with store.db:
            store.db.execute("UPDATE message_metadata SET content=json_set(content,"
                             "'$.reactions.observed_at',?)", (complete_at,))
    # When: reading actor presentation through the snapshot API.
    result = _evidence(store, tmp_path)["reaction_actors"]
    # Then: failed/current-empty never restores the earlier complete set.
    assert result["state"] == ("failed" if state == "cancelled" else state)
    assert result["counts"] == ({} if state == "cancelled" else {"医師": {"completed": 1}})
    assert result["complete_at"] == (NOW if state == "cancelled" else complete_at)
    assert "actor_id" not in json.dumps(result) and "22" not in json.dumps(result)


def test_shadow_publication_remains_independent_and_off_by_default(store, tmp_path):
    # Given: capture without reactions and a successful unpublished shadow.
    message = _post(store, sender=22)
    message.metadata = {"reactions": _reaction()}
    store.save_metadata_shadow(message)
    # When: querying capture through the snapshot API.
    result = _evidence(store, tmp_path)
    # Then: no fresh shadow evidence is implicitly published.
    assert get_message_metadata(store.db, 1)["reactions"] is None
    assert result["message_metadata"]["response_observation"]["self_reaction"]["types"] == []
    assert result["metadata_shadow"]["displayed"] is False


def test_thread_with_unfetched_replies_keeps_reply_unknown(store, tmp_path):
    # Given: our post whose thread reports 2 replies, only one (not ours) stored.
    _post(store, sender=22)
    _post(store, mid=2, sender=33, parent=1, at=NOW - 10)
    with store.db:
        store.db.execute("UPDATE messages SET reply_count=2 WHERE message_id=1")
    # Then: an unseen reply could be ours, so no "not observed" claim.
    result = _evidence(store, tmp_path)["message_metadata"]["response_observation"]
    assert result["reply"]["state"] == "unknown"
    with store.db:
        store.db.execute("UPDATE messages SET reply_count=1 WHERE message_id=1")
    result = _evidence(store, tmp_path)["message_metadata"]["response_observation"]
    assert result["reply"]["state"] == "not_observed"
