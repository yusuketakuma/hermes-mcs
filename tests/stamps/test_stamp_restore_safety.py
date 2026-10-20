"""Synthetic publication failures and restored approvals never authorize a second POST."""
import hashlib
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
    assert counts["confirmed"] == 1 and counts["held"] == 1
    assert _receipt(scene, request)["stamp_state"] == "confirmed"
    assert json.loads(_operation(scene, request)["content"])["result_published"] is False
    posts, gets = list(scene[3].posts), list(scene[3].gets)
    assert posts == [(100, "viewed", False)]
    monkeypatch.setattr(notify_spool, "publish_file", original)
    _drain(scene)
    result = Path(notify_spool.notify_dirs(scene[2])["cmd_results"]) / (request["command_id"] + ".json")
    assert json.loads(result.read_bytes())["stamp_state"] == "confirmed"
    assert json.loads(_operation(scene, request)["content"])["result_published"] is True
    assert scene[3].posts == posts and scene[3].gets == gets


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
