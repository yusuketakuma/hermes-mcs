"""Synthetic publication failures and restored approvals never authorize a second POST."""
import hashlib
import itertools
import json
import os
from pathlib import Path
import sqlite3

import pytest

import notify_spool
import stamp_write
from notify_testkit import NOW, _dispatch, _intent, _msg, _spec, _token_for
from test_stamp_write import (
    _apply, _command, _drain, _receipt, led, pinned_clock, scene,
)

__all__ = ["led", "pinned_clock", "scene"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


def _operation(scene, request):
    row = scene[0].db.execute(
        "SELECT * FROM artifacts WHERE kind=? AND json_extract(meta,'$.command_id')=?",
        (stamp_write.KIND, request["command_id"])).fetchone()
    assert row is not None
    return dict(row)


def _witness(scene, request):
    return scene[2] / "stamp_operations" / "1-100-77" / (request["command_id"] + ".json")


def test_terminal_result_publication_retries_from_committed_status_without_post(scene, monkeypatch):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    original = notify_spool.publish_file

    def fail_result(directory, name, raw):
        if Path(directory).name == "cmd_results":
            raise OSError("synthetic result publication failure")
        return original(directory, name, raw)

    monkeypatch.setattr(notify_spool, "publish_file", fail_result)
    counts = _drain(scene)
    assert counts["confirmed"] == 1 and counts["held"] == 1 and counts["publish_failed"] == 1
    assert _receipt(scene, request)["stamp_state"] == "confirmed"
    assert json.loads(_operation(scene, request)["content"])["result_published"] is False
    posts, gets = list(scene[3].posts), list(scene[3].gets)
    assert posts == [(100, "viewed", False)]
    # The retry slot is committed up front, so the row sits out the backoff.
    assert json.loads(_operation(scene, request)["content"])["publish_next_at"] == (
        NOW + stamp_write.PUBLISH_BACKOFF_BASE_S)
    idle = _drain(scene)
    assert idle["publish_failed"] == 0 and idle["publish_pending"] == 1
    monkeypatch.setattr(notify_spool, "publish_file", original)
    monkeypatch.setattr(stamp_write.time, "time", lambda: NOW + stamp_write.PUBLISH_BACKOFF_BASE_S)
    _drain(scene)
    result = Path(notify_spool.notify_dirs(scene[2])["cmd_results"]) / (request["command_id"] + ".json")
    assert json.loads(result.read_bytes())["stamp_state"] == "confirmed"
    assert json.loads(_operation(scene, request)["content"])["result_published"] is True
    assert scene[3].posts == posts and scene[3].gets == gets


def _stuck_publication(scene, request, wait=100, **fields):
    """Settle an applied operation without publishing its result (synthetic)."""
    row = _operation(scene, request)
    content = json.loads(row["content"])
    content.update(state="confirmed", updated_at=NOW - wait, result_published=False)
    content.pop("publish_attempts", None)
    content.pop("publish_next_at", None)
    content.update(fields)
    scene[0].db.execute("UPDATE artifacts SET content=?,created_at=? WHERE artifact_id=?",
                        (json.dumps(content), NOW - wait, row["artifact_id"]))
    scene[0].db.commit()


def _block_publications(monkeypatch, keep):
    original = notify_spool.publish_file

    def fail_matching(directory, name, raw):
        if Path(directory).name == "cmd_results" and name != keep:
            raise OSError("synthetic result publication failure")
        return original(directory, name, raw)

    monkeypatch.setattr(notify_spool, "publish_file", fail_matching)
    return original


def test_failed_publications_rotate_and_later_results_still_publish(scene, monkeypatch):
    # A queued witness in the same namespace holds new operations, so the stuck
    # rows settle before the healthy request is admitted.
    for n in range(5):
        req = _command(scene, serial=800 + n)
        assert _apply(scene, req)["stamp_state"] == "queued"
        _stuck_publication(scene, req)
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    scene[0].db.commit()
    _block_publications(monkeypatch, request["command_id"] + ".json")
    counts = _drain(scene)          # the bounded publish window holds four of them
    assert counts["confirmed"] == 1
    assert counts["publish_failed"] == 4 and counts["held"] == 4
    assert counts["publish_pending"] == 5 and counts["publish_oldest_wait_s"] == 100
    assert json.loads(_operation(scene, request)["content"])["result_published"] is True
    # Deferred rows still retry once their backoff matures, without any wire call.
    counts = _drain(scene)
    assert counts["publish_failed"] == 1 and counts["publish_pending"] == 5
    monkeypatch.setattr(stamp_write.time, "time",
                        lambda: NOW + stamp_write.PUBLISH_BACKOFF_BASE_S)
    counts = _drain(scene)
    assert counts["publish_failed"] == 4 and counts["publish_pending"] == 5
    counts = _drain(scene)
    assert counts["publish_failed"] == 1
    assert scene[3].posts == [(100, "viewed", False)] and scene[3].gets == [(1, 100, None), (1, 100, None)]


def test_result_settled_mid_drain_publishes_in_the_same_drain(scene, monkeypatch):
    # The wall clock moves while queued work executes; a result must publish in
    # its own drain instead of waiting for the next collection interval.
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    ticks = itertools.count()
    monkeypatch.setattr(stamp_write.time, "time", lambda: NOW + next(ticks))
    counts = _drain(scene)
    assert counts["confirmed"] == 1 and counts["publish_failed"] == 0
    assert counts["publish_pending"] == 0
    assert json.loads(_operation(scene, request)["content"])["result_published"] is True
    result = Path(notify_spool.notify_dirs(scene[2])["cmd_results"]) / (request["command_id"] + ".json")
    assert json.loads(result.read_bytes())["stamp_state"] == "confirmed"


def test_rerender_failure_after_publish_is_not_a_publish_failure(scene, monkeypatch):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    import notify_cards

    def fail(*args, **kwargs):
        raise OSError("synthetic render failure")

    monkeypatch.setattr(notify_cards, "rerender_message_cards", fail)
    counts = _drain(scene)
    assert counts["confirmed"] == 1 and counts["held"] == 1 and counts["publish_failed"] == 0
    assert json.loads(_operation(scene, request)["content"])["result_published"] is True
    result = Path(notify_spool.notify_dirs(scene[2])["cmd_results"]) / (request["command_id"] + ".json")
    assert json.loads(result.read_bytes())["stamp_state"] == "confirmed"
    sent = []
    original = notify_spool.publish_file

    def watch(directory, name, raw):
        sent.append(name)
        return original(directory, name, raw)

    monkeypatch.setattr(notify_spool, "publish_file", watch)
    monkeypatch.setattr(notify_cards, "rerender_message_cards", lambda *a, **k: None)
    counts = _drain(scene)
    assert counts["publish_failed"] == 0 and sent == []


@pytest.mark.parametrize("fields", [
    {},                                          # pre-backoff rows carry no retry fields
    {"publish_attempts": "two"},                  # corrupt count restarts at zero
    {"publish_attempts": True},
    {"publish_next_at": "soon"},
    {"publish_next_at": -5},
    {"publish_next_at": NOW + 10 * stamp_write.PUBLISH_BACKOFF_CAP_S},
    {"publish_next_at": 1e300},
])
def test_corrupt_or_missing_publish_retry_fields_are_normalized(scene, monkeypatch, fields):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    _stuck_publication(scene, request, **fields)
    scene[0].db.commit()
    counts = _drain(scene)
    assert counts["publish_failed"] == 0 and counts["publish_pending"] == 0, counts
    assert counts["publish_retry_repaired"] == (0 if fields == {} else 1), counts
    stored = json.loads(scene[0].db.execute("SELECT content FROM artifacts WHERE kind=?",
                                            (stamp_write.KIND,)).fetchone()[0])
    assert stored["result_published"] is True
    assert stored["publish_attempts"] == 1
    assert stored["publish_next_at"] == NOW + stamp_write.PUBLISH_BACKOFF_BASE_S
    assert scene[3].posts == [] and scene[3].gets == [] and scene[3].profiles == 0


def test_started_witness_visible_after_publication_error_allows_get_only(scene, monkeypatch):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    original = stamp_write.publish_new_bytes

    def publish_then_fail(path, raw):
        original(path, raw)
        if Path(path) == _witness(scene, request):
            raise OSError("synthetic error after durable publication")

    monkeypatch.setattr(stamp_write, "publish_new_bytes", publish_then_fail)
    assert _drain(scene)["held"] == 1
    assert json.loads(_witness(scene, request).read_bytes())["state"] == "started"
    assert scene[3].posts == []
    gets = len(scene[3].gets)
    monkeypatch.setattr(stamp_write, "publish_new_bytes", original)
    assert _drain(scene)["unknown"] == 1
    assert len(scene[3].gets) > gets
    assert _receipt(scene, request)["stamp_state"] == "unknown"
    assert scene[3].posts == []


def test_deadline_expires_after_preflight_before_attempt_witness(scene, monkeypatch):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    clock = [0.0]
    monkeypatch.setattr(stamp_write.time, "monotonic", lambda: clock[0])
    original = scene[3].fetch_message_metadata

    def finish_preflight(*args, **kwargs):
        post = original(*args, **kwargs)
        clock[0] = 99.0
        return post

    monkeypatch.setattr(scene[3], "fetch_message_metadata", finish_preflight)
    counts = stamp_write.drain(scene[0], scene[3], scene[1], scene[2], 100.0)
    assert counts["held"] == 1
    assert scene[3].profiles == 1 and scene[3].gets == [(1, 100, None)]
    assert scene[3].posts == [] and not _witness(scene, request).exists()
    assert _receipt(scene, request)["stamp_state"] == "queued"


def test_expired_unstarted_intent_is_rejected_even_when_scope_capacity_is_full(scene, monkeypatch):
    from notify_testkit import _uuid
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    namespace = json.loads(_operation(scene, request)["content"])["namespace"]
    other_id = _uuid(999)
    path = _witness(scene, request).with_name(other_id + ".json")
    path.write_text(json.dumps({"command_id": other_id, "payload_hash": "a" * 64,
                               "namespace": namespace, "scope": [1, 100, 77],
                               "state": "confirmed"}))
    path.chmod(0o600)
    monkeypatch.setattr(stamp_write, "MAX_SCOPE_OPERATIONS", 1)
    monkeypatch.setattr(stamp_write.time, "time", lambda: NOW + stamp_write.INTENT_MAX_AGE_S + 1)
    assert _drain(scene)["rejected"] == 1
    assert _receipt(scene, request)["stamp_error"] == "stamp_intent_expired"
    assert scene[3].posts == [] and scene[3].gets == [] and scene[3].profiles == 0


@pytest.mark.parametrize("phase,error_type", [
    ("import", ImportError), ("enabled", RuntimeError), ("drain", sqlite3.OperationalError),
])
def test_stamp_failure_does_not_skip_remaining_collection_jobs(scene, monkeypatch, phase, error_type):
    import builtins
    from types import SimpleNamespace
    import collection_stages
    import job_ops
    import notify_cmds
    import run_check
    import self_identity
    stages = []
    for name in ("drain_commands", "seed_discovery", "seed_trickle"):
        monkeypatch.setattr(job_ops, name, lambda *a, **k: None)
    monkeypatch.setattr(notify_cmds, "drain_int_commands", lambda *a, **k: None)
    monkeypatch.setattr(collection_stages, "with_relogin",
                        lambda adapter, ledger, result, name, *a, **k: stages.append(name))
    monkeypatch.setattr(self_identity, "latest_self_profile", lambda db: {"sender_id": 77})
    monkeypatch.setattr(run_check, "HOME", str(scene[2].parent))

    def failed(*a, **k):
        raise error_type("synthetic-private-stamp-detail")

    if phase == "import":
        original = builtins.__import__

        def guarded(name, *a, **k):
            return failed() if name == "stamp_write" else original(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", guarded)
    else:
        monkeypatch.setattr(stamp_write, phase, failed)
    result = {"errors": []}
    run_check._run_jobs(scene[3], scene[0], SimpleNamespace(
        jobs_only=False, download_files=True, mark_read=True), scene[1], result,
        stamp_write.time.monotonic() + 60, False, None)
    assert result["errors"] == [f"stamps: {error_type.__name__}"]
    assert stages == ["discovery", "reply_jobs", "history_jobs", "thread_read", "attachments",
                      "trickle_jobs", "reconcile"]


@pytest.mark.parametrize("kind", ["malformed", "symlink", "fifo"])
def test_unsafe_witness_never_reaches_account_or_wire(scene, kind):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    path = _witness(scene, request)
    if kind == "malformed":
        path.write_bytes(b"{")
        path.chmod(0o600)
    elif kind == "symlink":
        target = scene[2] / "synthetic-private-witness.json"
        target.write_text("{}")
        target.chmod(0o600)
        path.symlink_to(target)
    else:
        os.mkfifo(path, 0o600)
    for _ in range(2):
        assert _drain(scene)["held"] == 1
        assert _receipt(scene, request)["stamp_state"] == "queued"
    assert scene[3].posts == [] and scene[3].gets == [] and scene[3].profiles == 0


def test_never_started_restored_approval_cannot_cross_newer_terminal_aba(scene):
    old = _command(scene)
    assert _apply(scene, old)["stamp_state"] == "queued"
    saved = _operation(scene, old)
    saved_receipt = _receipt(scene, old)
    ledger = scene[0]
    # A prior DB image can omit this queued row while disk history advances.
    with ledger.db:
        ledger.db.execute("DELETE FROM artifacts WHERE artifact_id=?", (saved["artifact_id"],))
        ledger.db.execute("DELETE FROM command_receipts WHERE command_id=?", (old["command_id"],))
    newer = _command(scene, serial=801, kind="accepted")
    assert _apply(scene, newer)["stamp_state"] == "queued"
    assert _drain(scene)["confirmed"] == 1
    cancel = _command(scene, serial=802, cancel=True)
    assert _apply(scene, cancel)["stamp_state"] == "queued"
    assert _drain(scene)["confirmed"] == 1
    assert scene[3].own is None
    assert not _witness(scene, old).exists()
    posts = list(scene[3].posts)
    assert posts == [(100, "accepted", False), (100, None, True)]
    with ledger.db:
        columns = list(saved)
        ledger.db.execute("INSERT INTO artifacts(" + ",".join(columns) + ") VALUES("
                          + ",".join("?" for _ in columns) + ")", [saved[k] for k in columns])
        ledger.db.execute("INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                          (old["command_id"], saved_receipt["payload_hash"], 1, None,
                           saved_receipt["outcome"], json.dumps(saved_receipt), NOW))
    assert _drain(scene)["rejected"] == 1
    assert _receipt(scene, old)["stamp_error"] == "stamp_target_changed"
    assert scene[3].posts == posts and scene[3].own is None
    assert not _witness(scene, old).exists()


@pytest.mark.parametrize("field", ["reaction_type", "expected_type"])
@pytest.mark.parametrize("bad", [{}, []], ids=["dict", "list"])
def test_container_values_reject_without_exception_or_side_effect(scene, field, bad):
    request = _command(scene)
    if field == "reaction_type":
        request["reaction_type"] = bad
    else:
        request["expected_own"] = {**request["expected_own"], "type": bad}
    assert _apply(scene, request)["outcome"] == "rejected"
    assert scene[0].db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?",
                               (stamp_write.KIND,)).fetchone()[0] == 0
    _drain(scene)
    assert scene[3].posts == [] and scene[3].gets == [] and scene[3].profiles == 0


def test_six_unknown_posts_all_receive_later_reconciliation_windows(scene, monkeypatch):
    ledger, cfg, root, api, _ = scene
    mids = list(range(100, 106))
    for mid in mids[2:]:
        _msg(ledger, mid, parent=100)
        message = api.message(mid)
        ledger.db.execute("UPDATE messages SET content_hash=? WHERE message_id=?",
                           (hashlib.sha256(message.body_html.encode()).hexdigest(), mid))
        ledger.save_metadata_shadow(message, publish=True)
    ledger.db.commit()
    assert _dispatch(ledger, _intent(ledger, payload={"message_ids": mids}), cfg)["dispatched"]
    current = (ledger, cfg, root, api, _token_for(_spec(ledger), "stamp"))
    requests = []
    for mid in mids:
        # Each approval binds the then-current whole form, including pending posts.
        request = _command(current, serial=900 + mid, mid=mid)
        assert _apply(current, request)["stamp_state"] == "queued"
        requests.append(request)
    api.failure = TimeoutError("synthetic ambiguous remote effect")
    now = [NOW]
    monkeypatch.setattr(stamp_write.time, "time", lambda: now[0])
    for _ in range(4):
        _drain(current)
        now[0] += 1
    assert len(api.posts) == len(mids)
    assert {mid for mid, _, _ in api.posts} == set(mids)
    for request in requests:
        assert _receipt(current, request)["stamp_state"] == "unknown"
    assert all(sum(got_mid == mid for _, got_mid, _ in api.gets) >= 2 for mid in mids)
