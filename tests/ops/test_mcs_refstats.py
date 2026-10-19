"""Approved reference-set statistics workflow tests.

capture -> human-approved ops.refstat_approve -> verify. Uses a real
Ledger + published snapshot in tmp_path; no network, no live data.
"""
import operation_contract
import mcs_contract
import json
import stat
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

import ledger
import mcs_refstats
import mcs_requests as requests
from common_testkit import make_ledger
import mcs_requests


_db = make_ledger


def _msg(mid=1, body="test body"):
    return mcs_contract.Message(
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
        operation_contract.reference_path(tmp_path, name, "pending"))


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
    path = operation_contract.reference_path(tmp_path, "base", "pending")
    ref = json.loads(Path(path).read_text())
    assert ref["schema"] == "refstat_v1" and ref["name"] == "base"
    assert "overview" in ref["stats"]


def test_capture_staging_is_private_and_does_not_follow_symlinks(tmp_path):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    db.close()
    pending = tmp_path / "refstats" / "pending"
    pending.mkdir(parents=True)
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep")
    (pending / "base.json.tmp").symlink_to(unrelated)
    _capture(tmp_path)
    assert unrelated.read_text() == "keep"
    assert (pending / "base.json.tmp").is_symlink()
    assert not (pending / "base.json").is_symlink()
    assert stat.S_IMODE((pending / "base.json").stat().st_mode) == 0o600


def test_capture_failed_publication_keeps_previous_baseline(tmp_path, monkeypatch):
    db = _db(tmp_path)
    db.save_messages([_msg()])
    db.close()
    _capture(tmp_path)
    pending = tmp_path / "refstats" / "pending"
    previous = (pending / "base.json").read_bytes()
    snap = _snapshot(tmp_path)

    def fail_replace(*args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(mcs_refstats.os, "replace", fail_replace)
    assert mcs_refstats.main([
        "capture", "--name", "base", "--stat", "overview",
        "--snapshot", str(snap), "--data-dir", str(tmp_path)]) == 1
    assert (pending / "base.json").read_bytes() == previous
    assert not list(pending.glob("*.tmp"))


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
    ref = json.loads(Path(operation_contract.reference_path(tmp_path, "base",
                                                "pending")).read_text())
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
    approved = operation_contract.reference_path(tmp_path, "base", "approved")
    assert Path(approved).is_file()
    assert not Path(operation_contract.reference_path(
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
    assert Path(operation_contract.reference_path(
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
        approved = Path(operation_contract.reference_path(tmp_path, "base", "approved"))
        previous = approved.read_bytes()
        db.save_messages([_msg(2, "new synthetic record")])
        _capture(tmp_path)
        pending = Path(operation_contract.reference_path(tmp_path, "base", "pending"))
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
            # Keep the fault cleanup portable to the minimum Python 3.10.
            db.db.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
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
    approved = operation_contract.reference_path(tmp_path, "base", "approved")
    data = json.loads(Path(approved).read_text())
    data["stats"]["overview"]["posts_in_scope"] = 999
    with open(approved, "w") as f:
        json.dump(data, f)
    snap = _snapshot(tmp_path)
    db.close()
    assert _verify(tmp_path, snapshot=snap) == 2  # diffs + hash change


def test_verify_cannot_borrow_approval_receipt_for_replaced_bytes(tmp_path, capsys):
    db = _db(tmp_path)
    try:
        db.save_messages([_msg()])
        _capture(tmp_path)
        req = _approve_req("base", _pending_hash(tmp_path))
        requests.apply_command(db, req)
        approved = Path(operation_contract.reference_path(tmp_path, "base", "approved"))
        approved.write_bytes(approved.read_bytes() + b"\n")
        borrowed = {"name": "base", "file_hash": mcs_refstats.file_sha256(approved),
                    "command_id": req["command_id"], "actor": "unapproved"}
        db.artifact_add("refstat_approval_v1", json.dumps(borrowed))
        snap = _snapshot(tmp_path)
    finally:
        db.close()
    assert _verify(tmp_path, snapshot=snap) == 2
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "unverified"


def test_corrupt_approval_row_does_not_hide_valid_approval(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_messages([_msg()])
        _capture(tmp_path)
        requests.apply_command(db, _approve_req("base", _pending_hash(tmp_path)))
        db.artifact_add("refstat_approval_v1", "[]")
        snap = _snapshot(tmp_path)
    finally:
        db.close()
    assert _verify(tmp_path, snapshot=snap) == 0


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
    approved = operation_contract.reference_path(tmp_path, "base", "approved")
    v1_bytes = Path(approved).read_bytes()   # stash the v1 baseline
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


def test_default_paths_follow_mcs_root(tmp_path):
    """Without --snapshot/--data-dir, capture uses MCS_ROOT, not ~/.mcs."""
    import os
    import subprocess
    import sys
    root = tmp_path / "root"
    db = _db(tmp_path)
    db.save_messages([_msg()], project_id=1)
    ledger.publish_snapshot(str(tmp_path / "ledger.db"),
                            str(root / "data" / "snapshots"))
    proc = subprocess.run(
        [sys.executable, mcs_refstats.__file__, "capture", "--name", "rooted",
         "--stat", "overview"],
        env={**os.environ, "MCS_ROOT": str(root)},
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (root / "data" / "refstats" / "pending" / "rooted.json").is_file()


# Capture at a fractional generated_at must replay identically on verify.
GEN = 1790000000.6


def test_capture_pins_integer_as_of_so_verify_passes(tmp_path, monkeypatch, capsys):
    # post exactly on the 7d lower bound at int(GEN): excluded at the
    # float as_of, included at the replayed whole-second as_of
    ts = int(GEN) - 7 * 86400
    iso = datetime.fromtimestamp(ts, timezone(timedelta(hours=9))).isoformat()
    db = _db(tmp_path)
    db.save_messages([mcs_contract.Message(
        message_id=1, project_id=1, parent_id=None, sender_id=1,
        sender_name="s", sender_type="user", profession="看護師",
        organization="", posted_at=iso, body_html="x", body_state="full",
        is_unread=False, reply_count=0)])
    monkeypatch.setattr(ledger.time, "time", lambda: GEN)
    snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snap"))
    monkeypatch.undo()
    assert mcs_refstats.main(["capture", "--name", "b", "--preset", "operational",
                              "--snapshot", str(snap), "--data-dir", str(tmp_path)]) == 0
    r = requests.apply_command(db, _approve_req("b", _pending_hash(tmp_path, "b")))
    assert r["outcome"] == "applied", r
    # republish so the snapshot carries the approval artifact; the
    # pinned as_of freezes the window, so same data must still match
    monkeypatch.setattr(ledger.time, "time", lambda: GEN + 60)
    snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snap"))
    monkeypatch.undo()
    capsys.readouterr()
    rc = mcs_refstats.main(["verify", "--name", "b", "--snapshot", str(snap),
                            "--data-dir", str(tmp_path)])
    out = capsys.readouterr().out
    db.close()
    assert rc == 0, out


# Reference approval requires an unambiguous receipt for the exact command.
@pytest.mark.parametrize("corruption", ["duplicate", "column_hash", "json_hash", "both_hash"])
def test_ambiguous_or_conflicting_receipt_cannot_verify_reference(tmp_path, capsys, corruption):
    store = _db(tmp_path)
    try:
        _capture(tmp_path)
        command = _approve_req("base", _pending_hash(tmp_path))
        receipt = mcs_requests.apply_command(store, command)
        assert receipt["outcome"] == "applied"
        artifact_id = receipt["refstat_artifact_id"]
        content = json.loads(store.db.execute(
            "SELECT content FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()[0])
        raw = json.dumps(receipt)
        if corruption == "duplicate":
            raw = '{"outcome":"rejected",' + raw[1:]
        if corruption in ("json_hash", "both_hash"):
            receipt["payload_hash"] = "f" * 64
            raw = json.dumps(receipt)
        with store.db:
            store.db.execute("UPDATE command_receipts SET receipt_json=? WHERE command_id=?",
                             (raw, command["command_id"]))
            if corruption in ("column_hash", "both_hash"):
                store.db.execute("UPDATE command_receipts SET payload_hash=? WHERE command_id=?",
                                 ("f" * 64, command["command_id"]))
        assert not mcs_refstats._receipt_applied(store.db, content, artifact_id)
    finally:
        store.close()
    capsys.readouterr()


def test_maximum_unicode_actor_reason_still_verifies_reference(tmp_path, capsys):
    store = _db(tmp_path)
    try:
        _capture(tmp_path)
        command = _approve_req("base", _pending_hash(tmp_path), actor="😀" * 120, reason="😀" * 2000)
        receipt = mcs_requests.apply_command(store, command)
        assert receipt["outcome"] == "applied"
        artifact_id = receipt["refstat_artifact_id"]
        content = json.loads(store.db.execute(
            "SELECT content FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()[0])
        assert mcs_refstats._receipt_applied(store.db, content, artifact_id)
    finally:
        store.close()
    capsys.readouterr()


def _audit_ref(tmp_path, db, name="audit"):
    argv = ["capture", "--name", name, "--preset", "extraction_audit",
            "--snapshot", str(_snapshot(tmp_path)), "--data-dir", str(tmp_path)]
    assert mcs_refstats.main(argv) == 0
    requests.apply_command(db, _approve_req(name, _pending_hash(tmp_path, name)))
    return Path(operation_contract.reference_path(tmp_path, name, "approved"))


def _last(capsys):
    return json.loads(capsys.readouterr().out.splitlines()[-1])


def test_audit_capture_binds_the_definition_and_the_same_definition_matches(tmp_path, capsys):
    import extraction_audit
    db = _db(tmp_path)
    try:
        db.save_messages([_msg()])
        approved = _audit_ref(tmp_path, db)
        snap = _snapshot(tmp_path)
    finally:
        db.close()
    assert json.loads(approved.read_text())["audit_definition"] == extraction_audit.definition_fingerprint()
    assert _verify(tmp_path, name="audit", snapshot=snap) == 0
    assert _last(capsys)["status"] == "match"


def test_a_changed_audit_definition_never_matches_equal_numbers(tmp_path, capsys, monkeypatch):
    import extraction_audit
    db = _db(tmp_path)
    try:
        db.save_messages([_msg()])
        _audit_ref(tmp_path, db)
        snap = _snapshot(tmp_path)
    finally:
        db.close()
    reference = extraction_audit.definition_fingerprint()
    monkeypatch.setattr(extraction_audit, "definition_fingerprint", lambda: "f" * 64)
    assert _verify(tmp_path, name="audit", snapshot=snap) == 2
    report = _last(capsys)
    assert report["status"] == "definition_changed" and report["diffs"] == []
    assert report["reference_definition"] == reference and report["current_definition"] == "f" * 64


def test_an_old_audit_reference_without_a_definition_is_not_compared(tmp_path, capsys):
    db = _db(tmp_path)
    try:
        db.save_messages([_msg()])
        approved = _audit_ref(tmp_path, db)
        data = json.loads(approved.read_text())
        data.pop("audit_definition")
        # an approved audit refstat from before the binding existed
        pending = Path(operation_contract.reference_path(tmp_path, "audit", "pending"))
        pending.write_text(json.dumps(data))
        requests.apply_command(db, _approve_req("audit", mcs_refstats.file_sha256(pending)))
        snap = _snapshot(tmp_path)
    finally:
        db.close()
    assert _verify(tmp_path, name="audit", snapshot=snap) == 2
    report = _last(capsys)
    assert report["status"] == "definition_missing" and report["diffs"] == []


def test_non_audit_presets_keep_their_old_shape_and_comparison(tmp_path, capsys):
    db = _db(tmp_path)
    try:
        db.save_messages([_msg()])
        _capture(tmp_path)
        assert "audit_definition" not in json.loads(
            Path(operation_contract.reference_path(tmp_path, "base", "pending")).read_text())
        requests.apply_command(db, _approve_req("base", _pending_hash(tmp_path)))
        snap = _snapshot(tmp_path)
    finally:
        db.close()
    assert _verify(tmp_path, snapshot=snap) == 0
    assert _last(capsys)["status"] == "match"
