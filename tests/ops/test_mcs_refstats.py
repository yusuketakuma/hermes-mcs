"""Approved reference-set statistics workflow tests.

capture -> human-approved ops.refstat_approve -> verify. Uses a real
Ledger + published snapshot in tmp_path; no network, no live data.
"""
import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import ledger
import mcs_adapter
import mcs_refstats
import mcs_requests as requests


def _db(tmp_path):
    return ledger.Ledger(str(tmp_path / "ledger.db"))


def _msg(mid=1, body="test body"):
    return mcs_adapter.Message(
        message_id=mid, project_id=1, parent_id=None, sender_id=1,
        sender_name="sender", sender_type="user", profession="看護師",
        organization="", posted_at="2026-09-19T00:00:00+09:00",
        body_html=body, body_state="full", is_unread=False,
        reply_count=0)


def _snapshot(tmp_path):
    return ledger.publish_snapshot(str(tmp_path / "ledger.db"),
                                   str(tmp_path / "snap"))


def _capture(tmp_path, name="base"):
    argv = ["capture", "--name", name, "--stat", "overview",
            "--snapshot", str(_snapshot(tmp_path)),
            "--data-dir", str(tmp_path)]
    assert mcs_refstats.main(argv) == 0


def _pending_hash(tmp_path, name="base"):
    return mcs_refstats.file_sha256(
        mcs_refstats._ref_path(tmp_path, name, "pending"))


def _approve_req(name, file_hash, actor="ops", reason="reviewed"):
    return {"version": 1, "cmd": "ops.refstat_approve",
            "command_id": str(uuid.uuid4()),
            "actor": actor, "human_confirmed": True, "project_id": 1,
            "name": name, "file_hash": file_hash, "reason": reason}


def _verify(tmp_path, name="base", snapshot=None):
    argv = ["verify", "--name", name,
            "--snapshot", str(snapshot or _snapshot(tmp_path)),
            "--data-dir", str(tmp_path)]
    return mcs_refstats.main(argv)


# ---------- capture ----------

def test_capture_writes_pending_with_hash(tmp_path, capsys):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    db.close()
    _capture(tmp_path)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["file_hash"]
    path = mcs_refstats._ref_path(tmp_path, "base", "pending")
    ref = json.load(open(path))
    assert ref["schema"] == "refstat_v1" and ref["name"] == "base"
    assert "overview" in ref["stats"]


def test_capture_rejects_bad_name(tmp_path, capsys):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    db.close()
    argv = ["capture", "--name", "../escape", "--stat", "overview",
            "--snapshot", str(_snapshot(tmp_path)),
            "--data-dir", str(tmp_path)]
    assert mcs_refstats.main(argv) == 1
    assert "bad_name" in capsys.readouterr().out


def test_capture_rejects_list_mode(tmp_path, capsys):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    db.close()
    argv = ["capture", "--name", "n", "--list",
            "--snapshot", str(_snapshot(tmp_path)),
            "--data-dir", str(tmp_path)]
    assert mcs_refstats.main(argv) == 1
    assert "list_not_capturable" in capsys.readouterr().out


def test_capture_pins_as_of(tmp_path):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    db.close()
    _capture(tmp_path)
    ref = json.load(open(mcs_refstats._ref_path(tmp_path, "base",
                                                "pending")))
    assert ref["query"]["as_of"]  # frozen window — republish ≠ drift


def test_pending_and_verify_reject_traversal(tmp_path, capsys):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    snap = _snapshot(tmp_path)
    db.close()
    for cmd in ("pending", "verify"):
        argv = [cmd, "--name", "../../ledger", "--data-dir", str(tmp_path)]
        if cmd == "verify":
            argv += ["--snapshot", str(snap)]
        assert mcs_refstats.main(argv) == 1
        assert "bad_name" in capsys.readouterr().out


def test_verify_rejects_malformed_approved(tmp_path, capsys):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    snap = _snapshot(tmp_path)
    db.close()
    approved_dir = tmp_path / "refstats" / "approved"
    approved_dir.mkdir(parents=True)
    for bad in (b"not json", b'{"stats": "x"}', b"[1,2]",
                b'{"schema":"refstat_v1","name":"other","stats":{}}'):
        (approved_dir / "bad.json").write_bytes(bad)
        argv = ["verify", "--name", "bad", "--snapshot", str(snap),
                "--data-dir", str(tmp_path)]
        assert mcs_refstats.main(argv) == 1
        assert "refstat_corrupt" in capsys.readouterr().out


# ---------- approve (ops path) ----------

def test_approve_moves_file_and_writes_artifact(tmp_path):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    _capture(tmp_path)
    fh = _pending_hash(tmp_path)
    receipt = requests.apply_command(db, _approve_req("base", fh))
    assert receipt["outcome"] == "applied"
    approved = mcs_refstats._ref_path(tmp_path, "base", "approved")
    assert Path(approved).is_file()
    assert not Path(mcs_refstats._ref_path(
        tmp_path, "base", "pending")).exists()
    arts = db.artifacts("refstat_approval_v1")
    assert len(arts) == 1
    content = json.loads(arts[0]["content"])
    assert content["file_hash"] == fh and content["name"] == "base"
    db.close()


def test_approve_rejects_hash_mismatch(tmp_path):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    _capture(tmp_path)
    receipt = requests.apply_command(
        db, _approve_req("base", "0" * 64))
    assert receipt["outcome"] == "rejected"
    assert receipt["error"] == "refstat_hash_mismatch"
    # file stays pending, no artifact
    assert Path(mcs_refstats._ref_path(
        tmp_path, "base", "pending")).is_file()
    assert not db.artifacts("refstat_approval_v1")
    db.close()


@pytest.mark.parametrize("failure", ["hash", "artifact", "receipt", "commit"])
def test_failed_reapproval_preserves_previous_baseline(tmp_path, failure):
    db = _db(tmp_path)
    try:
        db.save_messages([_msg()])
        _capture(tmp_path)
        requests.apply_command(db, _approve_req("base", _pending_hash(tmp_path)))
        approved = Path(mcs_refstats._ref_path(tmp_path, "base", "approved"))
        previous = approved.read_bytes()
        db.save_messages([_msg(2, "new synthetic record")])
        _capture(tmp_path)
        pending = Path(mcs_refstats._ref_path(tmp_path, "base", "pending"))
        proposed = pending.read_bytes()
        req = _approve_req("base", "0" * 64 if failure == "hash"
                           else _pending_hash(tmp_path))

        def authorize(action, name, *unused):
            table = {"artifact": "artifacts", "receipt": "command_receipts"}.get(failure)
            if action == sqlite3.SQLITE_INSERT and name == table:
                return sqlite3.SQLITE_DENY
            if (failure == "commit" and action == sqlite3.SQLITE_TRANSACTION
                    and name == "COMMIT"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.db.set_authorizer(authorize)
        try:
            if failure == "hash":
                assert requests.apply_command(db, req)["error"] == "refstat_hash_mismatch"
            else:
                with pytest.raises(sqlite3.DatabaseError):
                    requests.apply_command(db, req)
        finally:
            db.db.set_authorizer(None)
        assert approved.read_bytes() == previous
        assert pending.read_bytes() == proposed
        assert len(db.artifacts("refstat_approval_v1")) == 1
        row = db.db.execute(
            "SELECT outcome FROM command_receipts WHERE command_id=?",
            (req["command_id"],)).fetchone()
        if failure == "hash":
            assert row["outcome"] == "rejected"
        else:
            assert row is None
    finally:
        db.close()


def test_approve_rejects_missing_pending(tmp_path):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    receipt = requests.apply_command(
        db, _approve_req("ghost", "0" * 64))
    assert receipt["outcome"] == "rejected"
    assert receipt["error"] == "refstat_not_pending"
    db.close()


def test_approve_envelope_validation(tmp_path):
    db = _db(tmp_path)
    bad = _approve_req("../x", "0" * 64)
    assert requests.validate(bad) == "bad_refstat_name"
    bad2 = _approve_req("ok", "not-a-hash")
    assert requests.validate(bad2) == "bad_file_hash"
    bad3 = _approve_req("ok", "0" * 64)
    bad3.pop("reason")
    assert requests.validate(bad3) == "bad_reason"
    db.close()


def test_approve_replay_is_idempotent(tmp_path):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    _capture(tmp_path)
    req = _approve_req("base", _pending_hash(tmp_path))
    first = requests.apply_command(db, req)
    second = requests.apply_command(db, req)
    assert first["outcome"] == second["outcome"] == "applied"
    assert first == second   # replay returns the stored receipt verbatim
    # conflicting payload under the same command_id is rejected
    other = _approve_req("base", "1" * 64)
    other["command_id"] = req["command_id"]
    third = requests.apply_command(db, other)
    assert third["outcome"] == "rejected"
    assert third["error"] == "command_id_conflict"
    assert len(db.artifacts("refstat_approval_v1")) == 1
    db.close()


# ---------- verify ----------

def test_verify_match_same_snapshot(tmp_path):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    _capture(tmp_path)
    requests.apply_command(
        db, _approve_req("base", _pending_hash(tmp_path)))
    snap = _snapshot(tmp_path)          # republish includes artifact
    db.close()
    assert _verify(tmp_path, snapshot=snap) == 0


def test_verify_match_republished_same_data_preset(tmp_path):
    """as_of-pinned query: republishing identical data on a NEW snapshot
    must still verify 'match' even for as_of-windowed stats."""
    db = _db(tmp_path)
    db.save_messages([_msg()])
    argv = ["capture", "--name", "ops", "--preset", "operational",
            "--snapshot", str(_snapshot(tmp_path)),
            "--data-dir", str(tmp_path)]
    assert mcs_refstats.main(argv) == 0
    requests.apply_command(
        db, _approve_req("ops", _pending_hash(tmp_path, "ops")))
    snap = _snapshot(tmp_path)   # new generated_at, same data
    db.close()
    assert _verify(tmp_path, name="ops", snapshot=snap) == 0


def test_verify_reports_drift_on_new_data(tmp_path):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    _capture(tmp_path)
    requests.apply_command(
        db, _approve_req("base", _pending_hash(tmp_path)))
    db.save_messages([_msg(2, "another post")])   # dataset changes
    snap = _snapshot(tmp_path)
    db.close()
    assert _verify(tmp_path, snapshot=snap) == 2  # drift -> nonzero


def test_verify_flags_file_without_artifact(tmp_path):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    _capture(tmp_path)
    requests.apply_command(
        db, _approve_req("base", _pending_hash(tmp_path)))
    # tamper: rewrite the approved file after approval
    approved = mcs_refstats._ref_path(tmp_path, "base", "approved")
    data = json.load(open(approved))
    data["stats"]["overview"]["posts_in_scope"] = 999
    with open(approved, "w") as f:
        json.dump(data, f)
    snap = _snapshot(tmp_path)
    db.close()
    assert _verify(tmp_path, snapshot=snap) == 2  # diffs + hash change


def test_verify_unapproved_name_fails(tmp_path, capsys):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    db.close()
    assert _verify(tmp_path, name="never") == 1
    assert "refstat_not_approved" in capsys.readouterr().out


def test_verify_unverified_on_pre_artifact_snapshot(tmp_path, capsys):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    stale_snap = _snapshot(tmp_path)   # published BEFORE approval
    _capture(tmp_path)
    requests.apply_command(
        db, _approve_req("base", _pending_hash(tmp_path)))
    db.close()
    # the approved file exists, but this snapshot predates the artifact
    assert _verify(tmp_path, snapshot=stale_snap) == 2
    assert '"unverified"' in capsys.readouterr().out


def test_verify_superseded_after_reapproval(tmp_path, capsys):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    _capture(tmp_path)
    requests.apply_command(
        db, _approve_req("base", _pending_hash(tmp_path)))
    approved = mcs_refstats._ref_path(tmp_path, "base", "approved")
    v1_bytes = open(approved, "rb").read()   # stash the v1 baseline
    # new data + re-capture (v2 pending overwrites), approve again
    db.save_messages([_msg(2, "another post")])
    _capture(tmp_path)
    requests.apply_command(
        db, _approve_req("base", _pending_hash(tmp_path)))
    snap = _snapshot(tmp_path)
    db.close()
    assert _verify(tmp_path, snapshot=snap) == 0   # latest baseline ok
    # roll the approved file back to the earlier approved bytes — an
    # artifact exists for them, but a NEWER approval supersedes it
    with open(approved, "wb") as f:
        f.write(v1_bytes)
    assert _verify(tmp_path, snapshot=snap) == 2
    assert '"superseded"' in capsys.readouterr().out


# ---------- diff ----------

def test_diff_reports_path_and_values():
    diffs = mcs_refstats._diff({"a": {"b": 1}}, {"a": {"b": 2, "c": 3}})
    assert any("a.b" in d and "1" in d and "2" in d for d in diffs)
    assert any("a.c" in d and "<missing>" in d for d in diffs)
    assert not mcs_refstats._diff({"x": 1}, {"x": 1.0000000001})
    assert mcs_refstats._diff({"x": True}, {"x": 1})  # bool ≠ int
