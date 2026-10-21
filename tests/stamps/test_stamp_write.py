"""Synthetic human stamp commands preserve single-attempt and restore boundaries."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import time

import pytest

from mcs_payload import normalize_message
from mcs_requests import apply_command
from notify_testkit import (
    CFG, CLICKER, NOW, _attach, _card, _dispatch, _intent, _seed_thread, _spec,
    _token_for, _uuid, led, pinned_clock,
)
import stamp_write

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


class StampAPI:
    """An in-memory remote account; no network or adapter fallback exists."""

    def __init__(self):
        self.account = 77
        self.own = None
        self.posts = []
        self.gets = []
        self.profiles = 0
        self.failure = None
        self.changed_body = False

    def message(self, mid):
        raw = {"id": mid, "comment": "synthetic changed" if self.changed_body else f"synthetic post {mid}",
               "created_at": "2026-09-24T08:00:00+09:00", "user": {"id": 8},
               "reactions": ([] if self.own is None else
                             [{"type": self.own, "count": 1, "self_reacted": True}])}
        return normalize_message(raw, 1, parent_id=None if mid == 100 else 100)

    roster = ()                    # own-station staff served by station_staffs()

    def self_profile(self):
        self.profiles += 1
        return {"sender_id": self.account, "stations": [{"id": 5, "name": "合成薬局"}]}

    def station_staffs(self, stations, *, strict_self=False):
        assert stations == [{"id": 5, "name": "合成薬局"}] and strict_self is True
        if isinstance(self.roster, Exception):
            raise self.roster
        return list(self.roster)

    def fetch_message_metadata(self, pid, mid, *, parent_id):
        self.gets.append((pid, mid, parent_id))
        return self.message(mid)

    def write_reaction(self, mid, reaction_type=None, *, cancel=False):
        self.posts.append((mid, reaction_type, cancel))
        if self.failure:
            raise self.failure
        self.own = None if cancel else reaction_type


@pytest.fixture
def scene(led, tmp_path):
    cfg = deepcopy(CFG)
    cfg["stamps"] = {"enabled": True, "account_user_id": 77, "operator": CLICKER}
    api = StampAPI()
    _seed_thread(led)
    for mid in (100, 101):
        message = api.message(mid)
        # notify_testkit deliberately uses synthetic placeholder hashes. Bind
        # these rows to the actual normalized bytes returned by this fake API.
        led.db.execute("UPDATE messages SET content_hash=? WHERE message_id=?",
                       (hashlib.sha256(message.body_html.encode()).hexdigest(), mid))
        led.save_metadata_shadow(message, publish=True)
    led.db.commit()
    _attach(led, 101, state="discovered", local_path=None)
    assert _dispatch(led, _intent(led), cfg)["dispatched"]
    token = _token_for(_spec(led), "stamp")
    return led, cfg, tmp_path / "data", api, token


def _form(scene):
    ledger, cfg, _, _, token = scene
    result = stamp_write.form(ledger.db, _card(ledger), cfg, CLICKER, NOW, token)
    assert "error" not in result
    return result


def _command(scene, *, serial=700, kind="viewed", cancel=False, mid=100):
    form = _form(scene)
    post = next(p for p in form["posts"] if p["message_id"] == mid)
    request = {"version": 1, "cmd": "ops.mcs_reaction", "command_id": _uuid(serial),
               "actor": CLICKER, "human_confirmed": True, "reason": "Synthetic human approval",
               "project_id": 1, "card_id": form["card_id"], "token": form["token"],
               "message_id": mid, "source_hash": post["source_hash"],
               "form_hash": form["form_hash"], "mcs_user_id": 77,
               "expected_own": {k: post["own"][k] for k in ("type", "observed_at")},
               "op": "cancel" if cancel else "add"}
    if not cancel:
        request["reaction_type"] = kind
    return request


def _apply(scene, request):
    ledger, cfg, root, _, _ = scene
    return apply_command(ledger, request, cfg=cfg, data_dir=root)


def _drain(scene):
    ledger, cfg, root, api, _ = scene
    return stamp_write.drain(ledger, api, cfg, root, time.monotonic() + 60)


def _receipt(scene, request):
    row = scene[0].db.execute("SELECT receipt_json FROM command_receipts WHERE command_id=?",
                              (request["command_id"],)).fetchone()
    assert row is not None
    return json.loads(row[0])


def _capture(scene, own):
    ledger, _, _, api, _ = scene
    api.own = own
    ledger.save_metadata_shadow(api.message(100), publish=True)


@pytest.mark.parametrize("before,desired,cancel,expected", [
    (None, "viewed", False, (100, "viewed", False)),
    ("viewed", "accepted", False, (100, "accepted", False)),
    ("accepted", None, True, (100, None, True)),
])
def test_add_change_and_cancel_are_explicit_single_post(scene, before, desired, cancel, expected):
    _capture(scene, before)
    request = _command(scene, kind=desired, cancel=cancel)
    assert _apply(scene, request)["stamp_state"] == "queued"
    assert scene[3].posts == []  # admission commits before any wire effect
    assert _drain(scene)["confirmed"] == 1
    assert scene[3].posts == [expected]
    assert scene[3].own == desired
    assert _receipt(scene, request)["stamp_state"] == "confirmed"
    _drain(scene)
    assert scene[3].posts == [expected]


def test_fresh_matching_stamp_is_noop_and_receipt_replay_does_not_requeue(scene):
    request = _command(scene)
    queued = _apply(scene, request)
    assert _apply(scene, request) == queued
    # The account changed remotely after the human opened the form.
    scene[3].own = "viewed"
    assert _drain(scene)["confirmed"] == 1
    assert scene[3].gets == [(1, 100, None)]
    assert scene[3].posts == []
    replay = _apply(scene, request)
    assert replay["stamp_state"] == "confirmed"
    _drain(scene)
    assert scene[3].gets == [(1, 100, None)] and scene[3].posts == []
    conflict = {**request, "reason": "Different approved payload"}
    assert _apply(scene, conflict)["error"] == "command_id_conflict"


def test_unexpected_adapter_value_error_is_private_and_retryable(scene, monkeypatch):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    api = scene[3]
    original = api.self_profile
    canary = "synthetic-private-adapter-detail"

    def broken_profile():
        raise ValueError(canary)

    monkeypatch.setattr(api, "self_profile", broken_profile)
    assert _drain(scene)["held"] == 1
    assert api.posts == []
    assert _receipt(scene, request)["stamp_state"] == "queued"
    assert _receipt(scene, request)["stamp_error"] == "stamp_check_held"
    records = scene[0].db.execute(
        "SELECT content FROM artifacts WHERE kind=?", (stamp_write.KIND,)).fetchall()
    assert all(canary not in row[0] for row in records)
    assert canary not in json.dumps(_receipt(scene, request))
    assert all(canary not in p.read_text() for p in scene[2].rglob("*.json"))
    monkeypatch.setattr(api, "self_profile", original)
    assert _drain(scene)["confirmed"] == 1
    assert api.posts == [(100, "viewed", False)]


def _staff(sid, is_self):
    return {"staff_id": sid, "name": "合成 職員", "professions": [], "station": "合成薬局", "is_self": is_self}


def test_a_profile_without_id_uses_the_single_self_in_the_fresh_roster(scene):
    api = scene[3]
    api.account, api.roster = None, [_staff(12, False), _staff(77, True)]
    _apply(scene, _command(scene))
    assert _drain(scene)["confirmed"] == 1
    assert api.posts == [(100, "viewed", False)]


@pytest.mark.parametrize("roster", [
    [],                                                  # roster without the logged-in user
    [_staff(12, False)],
    [_staff(77, True), _staff(78, True)],                # ambiguous self
    [_staff(78, True)],                                  # another account
    [_staff("x", True)],                                 # invalid id
])
def test_an_unknown_or_other_account_never_posts(scene, roster):
    api = scene[3]
    api.account, api.roster = None, roster
    request = _command(scene)
    _apply(scene, request)
    _drain(scene)
    assert api.posts == []
    assert _receipt(scene, request)["stamp_error"] == "stamp_account_mismatch"


def test_a_failed_roster_read_holds_without_posting(scene):
    from mcs_contract import MCSError
    api = scene[3]
    api.account, api.roster = None, MCSError("station_staffs_unavailable", "synthetic")
    request = _command(scene)
    _apply(scene, request)
    _drain(scene)
    assert api.posts == [] and _receipt(scene, request)["stamp_state"] != "confirmed"


def test_a_profile_id_is_used_as_is_without_reading_the_roster(scene):
    api = scene[3]
    api.account, api.roster = 78, RuntimeError("roster must not be read")
    request = _command(scene)
    _apply(scene, request)
    _drain(scene)
    assert api.posts == [] and _receipt(scene, request)["stamp_error"] == "stamp_account_mismatch"


def test_empty_observation_is_selectable_but_unfetched_is_unknown(scene):
    form = _form(scene)
    assert form["posts"][0]["own"] == {"type": None, "state": "observed", "observed_at": NOW}
    scene[0].db.execute("DELETE FROM message_metadata WHERE message_id=100")
    scene[0].db.commit()
    unknown = next(p for p in _form(scene)["posts"] if p["message_id"] == 100)
    assert unknown["own"] == {"type": None, "state": "unknown", "observed_at": None}
    result = _apply(scene, _command(scene))
    assert result["outcome"] == "rejected"
    _drain(scene)
    assert scene[3].posts == [] and scene[3].gets == []


def test_form_without_selectable_posts_is_rejected(scene):
    ledger, cfg, _, api, token = scene
    ledger.db.execute("UPDATE messages SET body_state='snippet' WHERE project_id=1")
    result = stamp_write.form(ledger.db, _card(ledger), cfg, CLICKER, NOW, token)
    assert result == {"error": "stamp_target_invalid"}
    assert api.posts == [] and api.gets == [] and api.profiles == 0


@pytest.mark.parametrize("error", ["mentions_invalid", "is_bookmarked_invalid", "is_pinned_invalid"])
def test_non_reaction_metadata_error_does_not_invalidate_fresh_stamp_observation(scene, monkeypatch, error):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    api = scene[3]
    original = api.message

    def message(mid):
        post = original(mid)
        post.metadata_errors = [error]
        return post

    monkeypatch.setattr(api, "message", message)
    assert _drain(scene)["confirmed"] == 1
    assert api.posts == [(100, "viewed", False)]
    assert _receipt(scene, request)["stamp_state"] == "confirmed"


@pytest.mark.parametrize("change", ["source", "archived", "actor", "account"])
def test_changed_authorization_or_target_before_admission_never_writes(scene, change):
    request = _command(scene)
    ledger, cfg, _, api, _ = scene
    if change == "source":
        ledger.db.execute("UPDATE messages SET content_hash=? WHERE message_id=100", ("a" * 64,))
    elif change == "archived":
        ledger.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    elif change == "actor":
        request["actor"] = "discord:other-operator"
    else:
        cfg["stamps"]["account_user_id"] = 88
    ledger.db.commit()
    assert _apply(scene, request)["outcome"] == "rejected"
    assert _drain(scene) == {"confirmed": 0, "unknown": 0, "rejected": 0, "held": 0}
    assert api.posts == [] and api.gets == [] and api.profiles == 0


@pytest.mark.parametrize("change", ["source", "archived", "actor", "account", "fresh_account", "fresh_source"])
def test_queued_intent_requires_current_target_and_authenticated_account(scene, change):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    ledger, cfg, _, api, _ = scene
    if change == "source":
        ledger.db.execute("UPDATE messages SET content_hash=? WHERE message_id=100", ("b" * 64,))
    elif change == "archived":
        ledger.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    elif change == "actor":
        cfg["stamps"]["operator"] = "discord:replacement"
    elif change == "account":
        cfg["stamps"]["account_user_id"] = 88
    elif change == "fresh_account":
        api.account = 88
    else:
        api.changed_body = True
    ledger.db.commit()
    assert _drain(scene)["rejected"] == 1
    assert _receipt(scene, request)["stamp_state"] == "rejected"
    assert api.posts == []


def test_unknown_attempt_is_get_only_forever_and_blocks_other_command(scene):
    request = _command(scene)
    other = _command(scene, serial=701, kind="accepted")
    assert _apply(scene, request)["stamp_state"] == "queued"
    assert _apply(scene, other)["outcome"] == "rejected"
    scene[3].failure = TimeoutError("synthetic ambiguous response")
    _drain(scene)
    assert _receipt(scene, request)["stamp_state"] == "unknown"
    assert scene[3].posts == [(100, "viewed", False)]
    scene[3].failure = None
    _drain(scene)
    assert _receipt(scene, request)["stamp_state"] == "unknown"
    assert scene[3].posts == [(100, "viewed", False)]
    # A later GET can establish completion without granting a second POST.
    scene[3].own = "viewed"
    _drain(scene)
    assert _receipt(scene, request)["stamp_state"] == "confirmed"
    assert scene[3].posts == [(100, "viewed", False)]


def test_restored_queued_db_with_attempt_witness_reconciles_only_by_get(scene):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    ledger = scene[0]
    original = ledger.db.execute("SELECT artifact_id,content FROM artifacts WHERE kind=?",
                                (stamp_write.KIND,)).fetchone()
    assert original is not None
    scene[3].failure = TimeoutError("synthetic ambiguous response")
    assert _drain(scene)["unknown"] == 1
    assert scene[3].posts == [(100, "viewed", False)]
    # Restore the queued operation row as a DB rollback would, retaining disk witness.
    ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (original["content"], original["artifact_id"]))
    ledger.db.commit()
    scene[3].failure = None
    scene[3].own = None
    gets = len(scene[3].gets)
    _drain(scene)
    assert len(scene[3].gets) > gets
    assert _receipt(scene, request)["stamp_state"] == "unknown"
    assert scene[3].posts == [(100, "viewed", False)]


def test_restored_queued_db_keeps_proven_terminal_history_after_later_remote_change(scene):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    ledger = scene[0]
    original = ledger.db.execute("SELECT artifact_id,content FROM artifacts WHERE kind=?",
                                (stamp_write.KIND,)).fetchone()
    assert _drain(scene)["confirmed"] == 1
    ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                      (original["content"], original["artifact_id"]))
    ledger.db.commit()
    scene[3].own = None  # a later MCS action does not erase the original success
    assert _drain(scene)["confirmed"] == 1
    assert _receipt(scene, request)["stamp_state"] == "confirmed"
    assert scene[3].posts == [(100, "viewed", False)]


def test_unknown_can_be_reconciled_after_original_action_token_is_removed(scene):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    scene[3].failure = TimeoutError("synthetic ambiguous response")
    assert _drain(scene)["unknown"] == 1
    scene[0].db.execute("DELETE FROM notification_action_tokens WHERE token=?", (request["token"],))
    scene[0].db.commit()
    scene[3].failure = None
    scene[3].own = "viewed"
    assert _drain(scene)["confirmed"] == 1
    assert scene[3].posts == [(100, "viewed", False)]


def test_witness_capacity_refuses_a_new_command_without_losing_old_reconciliation(scene, monkeypatch):
    monkeypatch.setattr(stamp_write, "MAX_SCOPE_OPERATIONS", 1)
    first = _command(scene)
    assert _apply(scene, first)["stamp_state"] == "queued"
    assert _drain(scene)["confirmed"] == 1
    second = _command(scene, serial=701, kind="accepted")
    result = _apply(scene, second)
    assert result["outcome"] == "rejected" and result["error"] == "stamp_witness_capacity"
    assert scene[3].posts == [(100, "viewed", False)]


def test_existing_directory_retries_parent_fsync_after_failed_creation(tmp_path, monkeypatch):
    calls = []
    original = stamp_write.fsync_dir

    def fsync(path):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("synthetic failed parent fsync")
        original(path)

    monkeypatch.setattr(stamp_write, "fsync_dir", fsync)
    path = tmp_path / "private-stamp-scope"
    with pytest.raises(OSError):
        stamp_write._private_dir(path)
    assert path.is_dir()
    assert stamp_write._private_dir(path) == path
    assert calls == [tmp_path, tmp_path]


def test_unpublished_safe_io_staging_after_crash_never_counts_as_an_attempt(scene):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    scope = scene[2] / "stamp_operations" / "1-100-77"
    leftovers = [scope / ".alias-synthetic.tmp", scope / ".atomic.synthetic.tmp"]
    for path in leftovers:
        path.write_bytes(b"partial synthetic unpublished JSON")
        path.chmod(0o600)
    assert _drain(scene)["confirmed"] == 1
    assert scene[3].posts == [(100, "viewed", False)]
    assert all(path.read_bytes() == b"partial synthetic unpublished JSON" for path in leftovers)


def test_copied_db_without_original_namespace_holds_without_api_calls(scene, tmp_path):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    ledger, cfg, _, api, _ = scene
    counts = stamp_write.drain(ledger, api, cfg, tmp_path / "different-install", time.monotonic() + 60)
    assert counts["held"] == 1
    assert _receipt(scene, request)["stamp_state"] == "queued"
    assert api.posts == [] and api.gets == [] and api.profiles == 0


@pytest.mark.parametrize("damage", ["missing_receipt", "changed_payload", "changed_artifact_scope"])
def test_executor_requires_the_exact_committed_human_receipt(scene, damage):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    db = scene[0].db
    if damage == "missing_receipt":
        db.execute("DELETE FROM command_receipts WHERE command_id=?", (request["command_id"],))
    elif damage == "changed_payload":
        db.execute("UPDATE artifacts SET content=json_set(content,'$.request.reaction_type','good') WHERE kind=?",
                   (stamp_write.KIND,))
    else:
        db.execute("UPDATE artifacts SET message_id=101 WHERE kind=?", (stamp_write.KIND,))
    db.commit()
    assert _drain(scene)["held"] >= 1
    assert scene[3].posts == [] and scene[3].gets == [] and scene[3].profiles == 0


@pytest.mark.parametrize("patch", [
    {"reaction_type": "all"}, {"reaction_type": "future_kind"},
    {"reaction_type": True}, {"human_confirmed": False}, {"reason": ""},
    {"reason": "x" * 201}, {"expected_own": {"type": None, "observed_at": True}},
    {"op": "cancel", "reaction_type": "viewed"},
])
def test_invalid_reaction_or_human_fields_reject_before_any_operation(scene, patch):
    request = {**_command(scene), **patch}
    assert _apply(scene, request)["outcome"] == "rejected"
    assert scene[0].db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?", (stamp_write.KIND,)).fetchone()[0] == 0
    _drain(scene)
    assert scene[3].posts == [] and scene[3].gets == [] and scene[3].profiles == 0


def test_fresh_conflicting_stamp_rejects_stale_human_choice(scene):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    scene[3].own = "completed"
    assert _drain(scene)["rejected"] == 1
    assert _receipt(scene, request)["stamp_state"] == "rejected"
    assert scene[3].own == "completed" and scene[3].posts == []


def test_cancel_already_absent_is_noop_after_fresh_account_read(scene):
    _capture(scene, "accepted")
    request = _command(scene, cancel=True)
    assert _apply(scene, request)["stamp_state"] == "queued"
    scene[3].own = None
    assert _drain(scene)["confirmed"] == 1
    assert scene[3].profiles == 1 and scene[3].gets == [(1, 100, None)]
    assert scene[3].posts == []


def test_reply_selection_writes_the_containing_post_not_a_file(scene):
    post = next(p for p in _form(scene)["posts"] if p["message_id"] == 101)
    assert "添付あり" in post["label"]
    request = _command(scene, mid=101)
    assert _apply(scene, request)["stamp_state"] == "queued"
    assert _drain(scene)["confirmed"] == 1
    assert scene[3].gets == [(1, 101, 100), (1, 101, 100)]
    assert scene[3].posts == [(101, "viewed", False)]


def test_expired_execution_window_leaves_queued_intent_without_api_calls(scene):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    ledger, cfg, root, api, _ = scene
    assert stamp_write.drain(ledger, api, cfg, root, time.monotonic()) == {
        "confirmed": 0, "unknown": 0, "rejected": 0, "held": 0}
    assert _receipt(scene, request)["stamp_state"] == "queued"
    assert api.posts == [] and api.gets == [] and api.profiles == 0


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_definite_refusal_read_back_unchanged_ends_and_unlocks_the_post(scene, status):
    from mcs_contract import MCSError
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    scene[3].failure = MCSError("http_error", status=status)
    assert _drain(scene)["rejected"] == 1
    receipt = _receipt(scene, request)
    assert (receipt["stamp_state"], receipt["stamp_error"]) == ("rejected", "stamp_server_rejected")
    assert scene[3].posts == [(100, "viewed", False)]
    scene[3].failure = None
    _drain(scene)
    assert scene[3].posts == [(100, "viewed", False)]       # terminal: never re-sent
    assert _apply(scene, _command(scene, serial=701, kind="accepted"))["stamp_state"] == "queued"


@pytest.mark.parametrize("failure", ["throttled", "server", "session", "redirect"])
def test_ambiguous_or_session_failures_stay_unknown(scene, failure):
    from mcs_contract import MCSError, SessionExpired
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    scene[3].failure = {"throttled": MCSError("http_error", status=429),
                        "server": MCSError("http_error", status=500, retryable=True),
                        "session": SessionExpired(status=401),
                        "redirect": MCSError("http_error", status=302)}[failure]
    _drain(scene)
    assert _receipt(scene, request)["stamp_state"] == "unknown"


def test_refusal_with_a_changed_read_back_stays_unknown(scene, monkeypatch):
    from mcs_contract import MCSError
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    api = scene[3]

    def refuse_but_change(mid, reaction_type=None, *, cancel=False):
        api.posts.append((mid, reaction_type, cancel))
        api.own = "accepted"                     # someone else's change is not our refusal
        raise MCSError("http_error", status=409)
    monkeypatch.setattr(api, "write_reaction", refuse_but_change)
    _drain(scene)
    assert _receipt(scene, request)["stamp_state"] == "unknown"


def test_pending_restore_holds_a_queued_intent_instead_of_ending_it(scene):
    import notify_spool
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    notify_spool.mark_restored(str(scene[2]))
    _drain(scene)
    assert _receipt(scene, request)["stamp_state"] == "queued"
    assert scene[3].posts == []
    notify_spool.clear_restore_pending(str(scene[2]))
    assert _drain(scene)["confirmed"] == 1


def test_unsaveable_rows_rotate_behind_valid_queued_work(scene):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    ledger = scene[0]
    for n in range(4):
        ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) "
                          "VALUES(?,1,100,?,'{}',?)", (stamp_write.KIND, json.dumps(
                              {"state": "queued", "request": {"command_id": f"bogus-{n}"}}), NOW - 100))
    ledger.db.commit()
    _drain(scene)                                  # the four stale rows fill the first window
    assert _drain(scene)["confirmed"] == 1         # rotated behind, the approved one runs next


def test_intent_ages_from_the_human_confirm_time(scene):
    stale = dict(_command(scene), confirmed_at=NOW - stamp_write.INTENT_MAX_AGE_S - 1)
    result = _apply(scene, stale)
    assert result["outcome"] == "rejected" and result["error"] == "stamp_intent_expired"
    fresh = dict(_command(scene, serial=701), confirmed_at=NOW - 100)
    assert _apply(scene, fresh)["stamp_state"] == "queued"
    row = scene[0].db.execute("SELECT content FROM artifacts WHERE kind=? ORDER BY artifact_id DESC",
                              (stamp_write.KIND,)).fetchone()
    assert json.loads(row[0])["queued_at"] == NOW - 100
    bad = dict(_command(scene, serial=702), confirmed_at="yesterday")
    assert _apply(scene, bad)["outcome"] == "rejected"
    null = dict(_command(scene, serial=703), confirmed_at=None)
    result = _apply(scene, null)
    assert result["outcome"] == "rejected" and result["error"] == "stamp_target_invalid"
    _drain(scene)
    assert len(scene[3].posts) == 1  # only the fresh confirm reached the wire


def test_reobserving_another_post_keeps_the_confirm_valid(scene):
    request = _command(scene, mid=100)
    before = _form(scene)["form_hash"]
    ledger, _, _, api, _ = scene
    api.own = "viewed"
    ledger.save_metadata_shadow(api.message(101), publish=True)   # only the other post changed
    api.own = None
    assert _form(scene)["form_hash"] == before
    assert _apply(scene, request)["stamp_state"] == "queued"


def test_post_labels_keep_the_layout_and_add_only_the_sender(scene):
    ledger = scene[0]
    ledger.db.execute("UPDATE messages SET sender_name=? WHERE message_id=101", ("合成  太郎",))
    ledger.db.commit()
    posts = {p["message_id"]: p["label"] for p in _form(scene)["posts"]}
    assert posts[101].endswith("返信 合成 太郎（添付あり）")
    # Layout as before: date, root/reply, then the sender; never a number
    # (LINE WORKS prefixes its own "1." to each choice).
    assert all(label[:2].isdigit() and label[2] == "-" for label in posts.values())


# ---------- one press on the card's MCS-style stamp row ----------

def _press_buttons(scene):
    from notify_testkit import _spec as spec_of
    return [b for row in spec_of(scene[0])["parts"]["action_rows"] for b in row
            if b["id"] == "stamp_press"]


def _press(scene, label_prefix, *, actor=CLICKER, serial=None, clicked_at=NOW, cfg=None, button=None):
    import notify_cmds
    from notify_testkit import ORIGIN
    ledger, scene_cfg, root, _, _ = scene
    cfg = cfg or scene_cfg
    button = button or next(b for b in _press_buttons(scene) if b["label"].startswith(label_prefix))
    req = {"version": 1, "op": "notification",
           "command_id": f"{button['token']}:{(serial or 1):016x}", "actor": actor,
           "token": button["token"], "origin": ORIGIN}
    if clicked_at is not None:
        req["clicked_at"] = clicked_at
    assert notify_cmds.validate_int(req) is None
    return notify_cmds.dispatch(ledger, req, cfg, str(root), now=NOW)


def test_card_carries_mcs_stamp_row_on_the_latest_post(scene):
    buttons = _press_buttons(scene)
    assert [b["label"] for b in buttons] == ["👀見ました", "🙆承知", "🙏感謝", "👍いいね", "✅完了"]
    params = [json.loads(scene[0].db.execute(
        "SELECT params FROM notification_action_tokens WHERE token=?", (b["token"],)).fetchone()[0])
        for b in buttons]
    assert {p["message_id"] for p in params} == {101}
    assert [p["kind"] for p in params] == ["viewed", "accepted", "thanked", "good", "completed"]


def test_one_press_queues_the_receipted_command_and_sends_after_readback(scene):
    result = _press(scene, "🙆")
    assert (result["outcome"], result["action"], result["stamp_op"]) == ("applied", "stamp_press", "add")
    row = scene[0].db.execute("SELECT receipt_json FROM command_receipts WHERE command_id=?",
                              (result["stamp_command_id"],)).fetchone()
    receipt = json.loads(row[0])
    assert (receipt["stamp_state"], receipt["scheduled"]) == ("queued", True)
    assert scene[3].posts == []                     # nothing on the wire inside the click
    # a retried or double-tapped click replays, never queues a second POST
    again = _press(scene, "🙆")
    assert again["stamp_command_id"] == result["stamp_command_id"]
    assert scene[0].db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?",
                               (stamp_write.KIND,)).fetchone()[0] == 1
    assert _drain(scene)["confirmed"] == 1
    assert scene[3].posts == [(101, "accepted", False)]
    # the card is re-rendered from the MCS readback, own stamp highlighted
    own = next(b for b in _press_buttons(scene) if b["label"].startswith("🙆"))
    assert own["label"] == "🙆承知✓" and own["style"] == "primary"


def test_pressing_the_own_stamp_takes_it_back_like_mcs(scene):
    ledger, _, _, api, _ = scene
    api.own = "accepted"
    ledger.save_metadata_shadow(api.message(101), publish=True)
    ledger.db.commit()
    import notify_cards
    notify_cards.issue_render(ledger.db, 1, scene[1], NOW + 1, [], force=True)
    ledger.db.commit()
    own = next(b for b in _press_buttons(scene) if b["label"].startswith("🙆"))
    assert own["label"] == "🙆承知✓" and own["style"] == "primary"
    result = _press(scene, "🙆")
    assert result["stamp_op"] == "cancel"
    assert _drain(scene)["confirmed"] == 1
    assert api.posts == [(101, None, True)]


def _republish_own(scene, own):
    ledger, _, _, api, _ = scene
    api.own = own
    ledger.save_metadata_shadow(api.message(101), publish=True)
    ledger.db.commit()


def _rerender(scene):
    import notify_cards
    notify_cards.issue_render(scene[0].db, 1, scene[1], NOW + 1, [], force=True)
    scene[0].db.commit()


@pytest.mark.parametrize("shown,now_own,label", [
    (None, "accepted", "🙆"),       # rendered unpressed: an add must not become a cancel
    ("accepted", None, "🙆"),       # rendered pressed: a cancel must not become an add
])
def test_a_press_after_the_own_stamp_changed_is_refused_not_flipped(scene, shown, now_own, label):
    _republish_own(scene, shown)
    _rerender(scene)
    button = next(b for b in _press_buttons(scene) if b["label"].startswith(label))
    _republish_own(scene, now_own)                 # MCS changed after the render
    result = _press(scene, label, button=button)
    assert result["outcome"] == "rejected" and result["error"] == "stamp_target_changed"
    assert scene[0].db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?",
                               (stamp_write.KIND,)).fetchone()[0] == 0
    _drain(scene)
    assert scene[3].posts == []


def test_a_press_ages_from_the_host_click_time(scene):
    assert _press(scene, "👀", clicked_at=None)["error"] == "stamp_intent_expired"
    late = _press(scene, "👀", serial=2, clicked_at=NOW - stamp_write.INTENT_MAX_AGE_S - 1)
    assert late["outcome"] == "rejected" and late["error"] == "stamp_intent_expired"
    _drain(scene)
    assert scene[3].posts == []
    ok = _press(scene, "👀", serial=3, clicked_at=NOW - 30)
    row = scene[0].db.execute("SELECT content FROM artifacts WHERE kind=?", (stamp_write.KIND,)).fetchone()
    assert ok["stamp_op"] == "add" and json.loads(row[0])["queued_at"] == NOW - 30


def test_a_replayed_press_is_reauthorized_first(scene):
    button = next(b for b in _press_buttons(scene) if b["label"].startswith("👀"))
    assert _press(scene, "👀", button=button)["outcome"] == "applied"
    off = {**scene[1], "stamps": {**scene[1]["stamps"], "enabled": False}}
    again = _press(scene, "👀", button=button, cfg=off)
    assert again["outcome"] == "rejected" and again["error"] == "stamp_not_allowed"


def test_only_the_operator_can_press(scene):
    result = _press(scene, "👀", actor="discord:2002")
    assert result["outcome"] == "rejected" and result["error"] == "stamp_not_allowed"
    assert scene[3].posts == []


def test_stamps_are_on_by_default_once_the_identity_is_configured(scene):
    cfg = {**scene[1], "stamps": {"account_user_id": 77, "operator": CLICKER}}
    assert stamp_write.enabled(cfg, CLICKER)
    assert not stamp_write.enabled({**cfg, "stamps": {**cfg["stamps"], "enabled": False}}, CLICKER)
    assert not stamp_write.enabled({**cfg, "stamps": {"operator": CLICKER}}, CLICKER)
    assert not stamp_write.enabled({**cfg, "stamps": {}}, CLICKER)


def test_changed_own_stamp_refreshes_for_explicit_post_reselection(scene):
    old = next(b for b in _press_buttons(scene) if b["label"].startswith("🙆"))
    _republish_own(scene, "accepted")
    result = _press(scene, "🙆", button=old)
    assert result["error"] == "stamp_target_changed" and result["stamp_reselect"] is True
    assert scene[3].posts == []
    assert scene[0].db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?", (stamp_write.KIND,)).fetchone()[0] == 0
    # The old press keeps its meaning; only a new explicit form selection can proceed.
    assert _press(scene, "🙆", button=old, serial=2)["error"] == "stamp_target_changed"
    token = _token_for(_spec(scene[0]), "stamp")
    fresh = (*scene[:4], token)
    command = _command(fresh, kind="viewed", mid=101)
    assert _apply(fresh, command)["outcome"] == "applied"
    assert _drain(fresh)["confirmed"] == 1
    assert scene[3].posts == [(101, "viewed", False)]


@pytest.mark.parametrize("alter", ["actor", "project", "expired"])
def test_reselection_never_refreshes_unrelated_or_unauthorized_cards(scene, monkeypatch, alter):
    import notify_cards
    req = _command(scene)
    if alter == "actor":
        req["actor"] = "discord:untrusted"
    elif alter == "project":
        req["project_id"] = 2
    else:
        scene[0].db.execute("UPDATE notification_action_tokens SET expires_at=? WHERE token=?", (NOW - 1, req["token"]))
        scene[0].db.commit()
    monkeypatch.setattr(notify_cards, "rerender_message_cards", lambda *a, **k: pytest.fail("must not refresh"))
    denied = {"outcome": "rejected", "error": "stamp_target_changed"}
    assert stamp_write.refresh_rejected(scene[0], req, denied, scene[1], NOW) == denied
    assert scene[3].posts == []


def test_reselection_render_failure_preserves_the_original_refusal(scene, monkeypatch):
    import notify_cards
    def fail(*args, **kwargs):
        raise OSError("synthetic private detail")
    monkeypatch.setattr(notify_cards, "rerender_message_cards", fail)
    req = _command(scene)
    denied = {"outcome": "rejected", "error": "stamp_target_changed"}
    result = stamp_write.refresh_rejected(scene[0], req, denied, scene[1], NOW)
    assert result == {**denied, "rerender_error": "OSError"}
    assert scene[3].posts == []


def test_stale_source_refuses_without_retargeting_the_press(scene):
    button = next(b for b in _press_buttons(scene) if b["label"].startswith("👀"))
    scene[0].db.execute("UPDATE messages SET content_hash=? WHERE message_id=101", ("f" * 64,))
    scene[0].db.commit()
    result = _press(scene, "👀", button=button)
    assert result["outcome"] == "rejected" and result["error"] == "stale_source"
    assert result["stamp_reselect"] is True
    assert scene[3].posts == []
    assert scene[0].db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?", (stamp_write.KIND,)).fetchone()[0] == 0


@pytest.mark.parametrize("code", ["stamp_result_unknown", "stamp_observation_required", "stamp_restore_hold"])
def test_unresolved_stamp_state_never_requests_a_reselection_refresh(scene, monkeypatch, code):
    import notify_cards
    monkeypatch.setattr(notify_cards, "rerender_message_cards", lambda *a, **k: pytest.fail("must not refresh"))
    result = {"outcome": "rejected", "error": code}
    assert stamp_write.refresh_rejected(scene[0], _command(scene), result, scene[1], NOW) == result
