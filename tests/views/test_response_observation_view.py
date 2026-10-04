"""Synthetic published-snapshot contracts for the explicitly-self-addressed list."""
import json

import pytest

from ledger import Ledger, publish_snapshot
from mcs_adapter import _norm_message
from mcs_signals import record_station_staff
from mcs_view import View
from response_observation_view import get_response_observation_list

NOW = 1_800_000_000.25
SELF = 11
AGE = 300


@pytest.fixture
def store(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr("ledger.time.time", lambda: clock[0])
    ledger = Ledger(str(tmp_path / "synthetic.db"))
    ledger.ensure_patient(1)
    with ledger.db:
        record_station_staff(ledger.db, [{"staff_id": SELF, "is_self": True,
                                        "name": "ACTOR-NAME-CANARY"}])
        ledger.db.execute("UPDATE patients SET fetch_state='complete'")
    yield ledger, clock
    ledger.close()


def _post(ledger, mid=1, *, pid=1, sender=22, parent=None, at=NOW - 100):
    ledger.ensure_patient(pid)
    message = _norm_message({
        "id": mid, "comment": "BODY-FREE-TEXT-CANARY", "user": {"id": sender},
        "thread_message_id": parent,
    }, pid)
    message.parent_id = parent
    ledger.save_messages([message], project_id=pid, notify=False)
    with ledger.db:
        ledger.db.execute("UPDATE messages SET posted_at_ts=?,sender_name='SENDER-NAME-CANARY' "
                          "WHERE message_id=?", (at, mid))
        ledger.db.execute("UPDATE patients SET fetch_state='complete' WHERE project_id=?", (pid,))


def _meta(ledger, mid=1, *, mentions=True, reactions=(), at=NOW, error=None, raw=None):
    content = {}
    if mentions is not None:
        value = ([{"type": "user", "id": SELF}] if mentions is True else
                 [] if mentions is False else mentions)
        content["mentions"] = {"value": value, "observed_at": at}
    if reactions is not None:
        content["reactions"] = {"value": list(reactions), "observed_at": at}
    with ledger.db:
        ledger.db.execute("INSERT OR REPLACE INTO message_metadata "
                          "(message_id,source,content,checked_at,last_error) VALUES(?,?,?,?,?)",
                          (mid, "capture", json.dumps(content) if raw is None else raw, NOW, error))


def _view(store, tmp_path):
    ledger, clock = store
    clock[0] = NOW + 1
    path = publish_snapshot(ledger.db.execute("PRAGMA database_list").fetchone()["file"],
                            str(tmp_path / "snapshots"))
    assert path is not None
    return View(path)


def _read(view, **kwargs):
    return get_response_observation_list(view.db, enabled=True, max_age_s=AGE, **kwargs)


def _own(kind="completed", count=1):
    return [{"type": kind, "count": count, "self_reacted": True}]


def test_publication_off_does_not_execute_any_sql():
    class NoRead:
        def execute(self, *args):
            raise AssertionError("disabled publication inspected data")

    result = get_response_observation_list(
        NoRead(), enabled=False, projects=["invalid"], limit=0, cursor="invalid")
    assert result["state"] == "disabled" and result["items"] == []
    assert result["counts"]["scanned"] == 0
    assert result["nonresponse"] is None and result["clinical_completion"] is None


def test_live_ledger_and_missing_freshness_policy_are_not_empty_response_proof(store, tmp_path):
    ledger, _ = store
    _post(ledger)
    _meta(ledger)
    live = get_response_observation_list(ledger.db, enabled=True, max_age_s=AGE)
    assert live["state"] == "unavailable" and live["reason"] == "published_snapshot_required"
    view = _view(store, tmp_path)
    try:
        result = get_response_observation_list(view.db, enabled=True)
        assert result["state"] == "unknown" and result["reason"] == "freshness_policy_unknown"
        assert result["items"] == [] and result["counts"]["scanned"] == 0
    finally:
        view.close()


def test_primary_api_and_cli_keep_publication_and_freshness_explicit(store, tmp_path, capsys):
    from mcs_view import main

    ledger, _ = store
    _post(ledger)
    _meta(ledger)
    view = _view(store, tmp_path)
    try:
        disabled = view.read("response_observations")
        assert disabled["state"] == "disabled" and disabled["items"] == []
        unknown = view.read("response_observations", publication=True)
        assert unknown["state"] == "unknown" and unknown["reason"] == "freshness_policy_unknown"
        result = view.read("response_observations", publication=True, max_age_s=AGE)
        assert result["items"][0]["state"] == "not_observed"
        assert result["items"][0]["clinical_completion"] is None
        snapshot = view.db.execute("PRAGMA database_list").fetchone()[2]
    finally:
        view.close()
    assert main(["--snapshot", snapshot, "response_observations",
                 "--publication", "--max-age-s", str(AGE)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"][0]["state"] == "not_observed"
    assert payload["items"][0]["nonresponse"] is None
    assert all(canary not in json.dumps(payload) for canary in (
        "BODY-FREE-TEXT-CANARY", "ACTOR-NAME-CANARY", "SENDER-NAME-CANARY"))


@pytest.mark.parametrize(("mention", "expected"), [
    (True, "self"), (False, "other"), (None, "unknown"),
    ([{"type": "user", "id": 22}], "other"),
    ([{"type": "station", "id": SELF}], "other"),
    ([{"type": "project", "id": SELF}], "other"),
    ([{"type": "user"}], "unknown"),
    ([{"type": "future_actor", "id": SELF}], "unknown"),
])
def test_target_true_false_unknown_reuses_exact_self_identification(store, tmp_path, mention, expected):
    ledger, _ = store
    _post(ledger)
    _meta(ledger, mentions=mention)
    view = _view(store, tmp_path)
    try:
        result = _read(view)
        assert result["counts"][expected + "_target"] == 1
        assert result["counts"]["self_target"] == int(expected == "self")
        assert len(result["items"]) == int(expected == "self")
        assert result["state"] == ("unknown" if expected == "unknown" else "complete")
        if result["items"]:
            item = result["items"][0]
            assert item["state"] == "not_observed"
            assert item["reply"] == {"state": "not_observed", "posted_at": None}
            assert item["self_reaction"]["state"] == "not_observed"
            assert item["clinical_completion"] is None and item["nonresponse"] is None
    finally:
        view.close()


def test_unknown_self_id_is_not_a_self_target_or_response_absence(store, tmp_path):
    ledger, _ = store
    _post(ledger)
    _meta(ledger)
    with ledger.db:
        ledger.db.execute("DELETE FROM artifacts WHERE kind='station_staff_v1'")
    view = _view(store, tmp_path)
    try:
        result = _read(view)
        assert result["state"] == "unknown" and result["reason"] == "self_identity_unknown"
        assert result["items"] == [] and result["counts"]["self_target"] == 0
        assert result["nonresponse"] is None
    finally:
        view.close()


@pytest.mark.parametrize(("reactions", "reply", "selected", "state"), [
    ([], False, True, "not_observed"),
    (_own(), False, True, "not_observed"),
    (None, True, True, "unknown"),
    (_own(), True, False, None),
    ([], True, True, "not_observed"),
    (_own("unknown-private-name-canary"), True, True, "unknown"),
    (_own(count=0), True, True, "unknown"),
])
def test_reply_and_ui_action_stay_distinct_and_never_complete_clinical_work(
        store, tmp_path, reactions, reply, selected, state):
    ledger, _ = store
    _post(ledger)
    _meta(ledger, reactions=reactions)
    if reply:
        _post(ledger, 2, sender=SELF, parent=1, at=NOW - .125)
    view = _view(store, tmp_path)
    try:
        result = _read(view)
        assert bool(result["items"]) is selected
        if selected:
            item = result["items"][0]
            assert item["state"] == state
            assert item["reply"]["posted_at"] == (NOW - .125 if reply else None)
            assert item["self_reaction"]["basis"] == "ui_operation_only"
            assert item["unread"] is None and item["nonresponse"] is None
        else:
            assert result["counts"]["observed_pairs"] == 1
            assert result["clinical_completion"] is None
        encoded = json.dumps(result)
        assert not any(c in encoded for c in (
            "BODY-FREE-TEXT-CANARY", "SENDER-NAME-CANARY",
            "ACTOR-NAME-CANARY", "unknown-private-name-canary"))
    finally:
        view.close()


@pytest.mark.parametrize(("sender", "reply_at", "known_reply"), [
    (SELF, NOW + 30, False),
    (None, NOW - .125, False),
    (SELF, None, False),
    (SELF, NOW + 30, True),
])
def test_future_or_unattributed_reply_cannot_hide_unknown(
        store, tmp_path, sender, reply_at, known_reply):
    ledger, _ = store
    _post(ledger)
    _meta(ledger, reactions=_own())
    _post(ledger, 2, sender=sender, parent=1, at=reply_at)
    if known_reply:
        _post(ledger, 3, sender=SELF, parent=1, at=NOW - .125)
    view = _view(store, tmp_path)
    try:
        item = _read(view)["items"][0]
        assert item["state"] == "unknown" and "reply_time_context" in item["unknown"]
        assert item["reply"]["state"] == ("observed" if known_reply else "unknown")
        assert item["self_reaction"]["state"] == "observed"
    finally:
        view.close()


@pytest.mark.parametrize("case", [
    "old", "future", "partial", "malformed", "hash_changed", "hash_invalid",
    "updated_body", "body_incomplete",
])
def test_untrusted_target_observation_is_unknown_not_self_target(store, tmp_path, case):
    ledger, _ = store
    _post(ledger)
    _meta(ledger)
    with ledger.db:
        if case == "old":
            ledger.db.execute("UPDATE message_metadata SET content=json_set(content,"
                              "'$.mentions.observed_at',?),checked_at=?", (NOW - AGE - 1, NOW))
        elif case == "future":
            ledger.db.execute("UPDATE message_metadata SET content=json_set(content,"
                              "'$.mentions.observed_at',?)", (NOW + 60,))
        elif case == "partial":
            ledger.db.execute("UPDATE message_metadata SET last_error='reactions_invalid'")
        elif case == "malformed":
            ledger.db.execute("UPDATE message_metadata SET content='broken'")
        elif case == "hash_changed":
            ledger.db.execute("UPDATE messages SET content_hash=?,updated_seen=?",
                              ("0" * 64, NOW + .125))
        elif case == "hash_invalid":
            ledger.db.execute("UPDATE messages SET content_hash=NULL")
        elif case == "updated_body":
            ledger.db.execute("UPDATE messages SET updated_seen=?", (NOW + .125,))
        else:
            ledger.db.execute("UPDATE messages SET body_state='snippet'")
    view = _view(store, tmp_path)
    try:
        result = _read(view)
        assert result["state"] == "unknown" and result["items"] == []
        assert result["counts"]["unknown_target"] == 1 and result["counts"]["self_target"] == 0
        assert result["nonresponse"] is None and result["unread"] is None
    finally:
        view.close()


def test_cancelled_stamp_does_not_restore_old_action_or_hide_partial_ui(store, tmp_path):
    ledger, _ = store
    _post(ledger)
    _meta(ledger, reactions=_own())
    _meta(ledger, reactions=[])
    _post(ledger, 2, sender=SELF, parent=1, at=NOW - .125)
    with ledger.db:
        # Mention is known, but the reaction field timestamp is malformed.
        ledger.db.execute("UPDATE message_metadata SET content=json_set(content,"
                          "'$.reactions.observed_at','bad')")
    view = _view(store, tmp_path)
    try:
        item = _read(view)["items"][0]
        assert item["state"] == "unknown" and item["reply"]["state"] == "observed"
        assert item["self_reaction"]["state"] == "unknown"
        assert item["self_reaction"]["types"] == []
    finally:
        view.close()


def test_incomplete_history_does_not_turn_missing_reply_into_negative_proof(store, tmp_path):
    ledger, _ = store
    _post(ledger)
    _meta(ledger, reactions=_own())
    with ledger.db:
        ledger.db.execute("UPDATE patients SET fetch_state='incomplete'")
    view = _view(store, tmp_path)
    try:
        item = _read(view)["items"][0]
        assert item["reply"]["state"] == "unknown" and item["nonresponse"] is None
    finally:
        view.close()


def test_scoped_bounded_pages_hide_deleted_archived_other_targets_and_private_values(store, tmp_path):
    ledger, _ = store
    for mid, pid in ((1, 1), (2, 1), (3, 2), (4, 3), (5, 1), (6, 1)):
        _post(ledger, mid, pid=pid)
        _meta(ledger, mid, mentions=mid != 6)
    with ledger.db:
        ledger.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=5")
        ledger.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=3")
    view = _view(store, tmp_path)
    try:
        first = _read(view, projects=[1], limit=1)
        assert first["items"] == [] and first["counts"]["other_target"] == 1
        assert first["counts"]["scanned"] == 1 and first["next_cursor"] is not None
        second = _read(view, projects=[1], limit=1, cursor=first["next_cursor"])
        third = _read(view, projects=[1], limit=1, cursor=second["next_cursor"])
        assert [second["items"][0]["message_id"], third["items"][0]["message_id"]] == [2, 1]
        assert third["next_cursor"] is None
        assert _read(view, projects=[])["counts"]["scanned"] == 0
        all_rows = _read(view)
        assert [r["message_id"] for r in all_rows["items"]] == [3, 2, 1]
        with pytest.raises(ValueError, match="cursor_scope_or_generation_changed"):
            _read(view, projects=[2], cursor=first["next_cursor"])
    finally:
        view.close()


def test_cursor_generation_freshness_binding_and_no_mutations(store, tmp_path):
    ledger, clock = store
    for mid in (1, 2):
        _post(ledger, mid)
        _meta(ledger, mid)
    original = [tuple(r) for r in ledger.db.execute("SELECT * FROM message_metadata")]
    view = _view(store, tmp_path)
    try:
        before = view.db.total_changes
        result = _read(view, limit=1)
        token = result["next_cursor"]
        assert token is not None
        assert _read(view, limit=1) == result
        with pytest.raises(ValueError, match="cursor_scope_or_generation_changed"):
            get_response_observation_list(
                view.db, enabled=True, max_age_s=AGE + 1, cursor=token)
        assert view.db.total_changes == before
        assert [tuple(r) for r in ledger.db.execute("SELECT * FROM message_metadata")] == original
    finally:
        view.close()
    clock[0] += 1
    snapshot = publish_snapshot(ledger.db.execute("PRAGMA database_list").fetchone()["file"],
                                str(tmp_path / "next-snapshot"))
    next_view = View(snapshot)
    try:
        with pytest.raises(ValueError, match="cursor_scope_or_generation_changed"):
            _read(next_view, cursor=token)
    finally:
        next_view.close()


@pytest.mark.parametrize("cursor", ["", "bad", "e30=", "W10=", "a" * 2049])
def test_invalid_cursor_is_rejected_without_writes(store, tmp_path, cursor):
    view = _view(store, tmp_path)
    try:
        with pytest.raises(ValueError, match="cursor_scope_or_generation_changed"):
            _read(view, cursor=cursor)
        assert view.db.total_changes == 0
    finally:
        view.close()


def test_same_clock_body_revision_does_not_rebind_retained_mentions_to_new_hash(store, tmp_path):
    ledger, _ = store
    _post(ledger)
    _meta(ledger)
    changed = _norm_message({"id": 1, "comment": "NEW-SYNTHETIC-BODY",
                             "user": {"id": 22}}, 1)
    # Exact same clock; old metadata is retained, while the body hash changes.
    ledger.save_messages([changed], project_id=1, notify=False)
    assert ledger.db.execute("SELECT max(seq) FROM message_revisions "
                             "WHERE message_id=1").fetchone()[0] == 2
    view = _view(store, tmp_path)
    try:
        result = _read(view)
        assert result["state"] == "unknown" and result["counts"]["self_target"] == 0
        assert result["unknown_reasons"]["source_binding_unknown"] == 1
    finally:
        view.close()


def test_explicit_freshness_window_uses_original_observation_time(store, tmp_path):
    ledger, _ = store
    _post(ledger)
    _meta(ledger, at=NOW - AGE - 10)
    with ledger.db:
        ledger.db.execute("UPDATE messages SET updated_seen=?", (NOW - AGE - 10,))
    view = _view(store, tmp_path)
    try:
        result = _read(view)
        assert result["unknown_reasons"]["observation_stale"] == 1
        assert result["counts"]["self_target"] == 0
    finally:
        view.close()


@pytest.mark.parametrize("unread", [None, 0, 1])
def test_original_unread_flag_is_not_inferred_from_completed_ui_stamp(store, tmp_path, unread):
    ledger, _ = store
    _post(ledger)
    _meta(ledger, reactions=_own())
    with ledger.db:
        ledger.db.execute("UPDATE messages SET is_unread=?", (unread,))
    view = _view(store, tmp_path)
    try:
        item = _read(view)["items"][0]
        assert item["unread"] is None and item["clinical_completion"] is None
        assert item["reply"]["state"] == "not_observed"
    finally:
        view.close()
