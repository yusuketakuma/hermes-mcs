"""Synthetic consent, collision, malformed-evidence and finite-budget regressions."""
import json
from pathlib import Path
import sqlite3
import sys
import uuid

import pytest

import import_drug_master as drug_master
import mcs_backup as backup
import mcs_repair as repair
import mcs_requests as requests
import mcs_restore as restore
import mcs_refstats as refstats
import test_mcs_backup as backup_tests
import test_mcs_repair as repair_tests
from test_backup_restore_consent import approved, resumed, restored as restored
from test_mcs_backup import KEY, _create
from test_mcs_repair import NONCE
from test_mcs_refstats import _approve_req, _capture, _db, _msg, _pending_hash, _snapshot
from test_mcs_request_loops import _candidate_setup, _create as loop_create

backup_world = backup_tests.world
repair_world = repair_tests.world


@pytest.mark.parametrize("damage", ["ledger.db", "restore.json", restore.MARKER,
                                    "move", "missing_marker"])
def test_repeat_resume_revalidates_the_active_consent(restored, damage):
    receipt = approved(restored)
    resumed(restored, receipt)
    if damage == "move":
        moved = restored.with_name("moved")
        restored.rename(moved)
        restored = moved
    elif damage == "missing_marker":
        (restored / restore.MARKER).unlink()
    else:
        path = restored / damage
        replacement = restored / "replacement"
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(path)
    assert restore.writers_resumed(str(restored)) is False
    before = {p.name: p.read_bytes() for p in restored.iterdir() if p.is_file()}
    with pytest.raises((restore.RestoreConsentError, OSError)):
        resumed(restored, receipt)
    assert {p.name: p.read_bytes() for p in restored.iterdir() if p.is_file()} == before
    assert not (restored / restore.BLOCKED).exists()


def test_repeat_resume_preserves_normal_writes_after_adoption(restored):
    receipt = approved(restored)
    first = resumed(restored, receipt)
    with sqlite3.connect(restored / "ledger.db") as db:
        db.execute("UPDATE patients SET last_seen=last_seen+1")
    assert restore.writers_resumed(str(restored)) is True
    assert resumed(restored, receipt) == first
    assert not (restored / restore.BLOCKED).exists()


def test_drill_publication_never_replaces_a_concurrent_ledger(backup_world, monkeypatch):
    receipt = _create(backup_world)
    target = Path(backup_world[3].scratch_dir) / "drill-race"
    mark = backup.notify_cards.mark_restored
    crossed = []

    def marker_then_collision(*args, **kwargs):
        result = mark(*args, **kwargs)
        (target / "ledger.db").write_bytes(b"synthetic concurrent ledger")
        (target / "ledger.db").chmod(0o600)
        crossed.append(True)
        return result

    monkeypatch.setattr(backup.notify_cards, "mark_restored", marker_then_collision)
    with pytest.raises(FileExistsError):
        backup.verify(receipt["bundle"], receipt["sha256"], backup_world[3],
                      lambda: KEY, drill_destination=str(target))
    assert crossed == [True]
    assert (target / "ledger.db").read_bytes() == b"synthetic concurrent ledger"
    assert backup.notify_cards.restore_awaiting_consent(str(target)) is not None
    assert not list(target.glob(".restore-*"))


@pytest.mark.parametrize("field", ["last_verify_at", "source_last_successful_run"])
def test_oversized_integer_history_is_a_stable_refusal(backup_world, tmp_path, field):
    records = tmp_path / "trusted-records"
    records.mkdir(mode=0o700)
    state = records / "backup_state.json"
    state.write_text(json.dumps({"v": 1, "policy_id": backup_world[3].policy_id,
                                 field: 10**1000}))
    state.chmod(0o600)
    before = state.read_bytes()
    with pytest.raises(backup.BackupError, match="backup_record_invalid"):
        backup.status(str(records), backup_world[3])
    assert state.read_bytes() == before


def test_oversized_integer_deadline_is_refused_before_policy_access(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "load_policy", lambda *a, **kw: pytest.fail("invalid budget read policy"))
    report = backup.plan(tmp_path / "never-read.json", max_seconds=10**1000)
    assert report["blockers"] == ["backup_inspection_budget_invalid"]
    assert report["exit_code"] == 1 and report["facts"] == {}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("field", ["content", "meta"])
def test_unparseable_loop_candidate_is_rejected_with_a_receipt(tmp_path, field):
    db, row, bundle, policy, source_hash = _candidate_setup(tmp_path)
    try:
        depth = sys.getrecursionlimit() + 100
        nested = "[" * depth + "0" + "]" * depth
        with db.db:
            db.db.execute(f"UPDATE artifacts SET {field}=? WHERE artifact_id=?",
                          (nested, row["artifact_id"]))
        result = requests.apply_command(db, loop_create(db, row, bundle, policy, source_hash))
        assert result["outcome"] == "rejected"
        assert result["error"] == ("loop_candidate_malformed" if field == "content"
                                     else "loop_candidate_meta_malformed")
        assert db.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        assert db.db.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 1
        assert not db.artifacts("request_loop_link", project_id=1)
    finally:
        db.close()


@pytest.mark.parametrize("damage", ["artifact", "receipt"])
def test_deeply_corrupt_approval_does_not_hide_an_earlier_valid_baseline(
        tmp_path, capsys, damage):
    db = _db(tmp_path)
    try:
        db.save_messages([_msg()])
        _capture(tmp_path)
        digest = _pending_hash(tmp_path)
        assert requests.apply_command(db, _approve_req("base", digest))["outcome"] == "applied"
        depth = sys.getrecursionlimit() + 100
        nested = "[" * depth + "0" + "]" * depth
        if damage == "artifact":
            db.artifact_add(refstats.APPROVAL_KIND, nested)
        else:
            command_id = str(uuid.uuid4())
            with db.db:
                db.db.execute(
                    "INSERT INTO command_receipts(command_id,payload_hash,outcome,receipt_json,processed_at) "
                    "VALUES(?,?,'applied',?,1)", (command_id, "b" * 64, nested))
            db.artifact_add(refstats.APPROVAL_KIND, json.dumps({
                "name": "base", "file_hash": digest, "command_id": command_id}))
        snapshot = _snapshot(tmp_path)
    finally:
        db.close()
    capsys.readouterr()
    assert refstats.main(["verify", "--name", "base", "--snapshot", str(snapshot),
                          "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "match"


def test_malformed_rule_metadata_is_unknown_in_repair_observations(repair_world):
    path = repair_world["snapshot"]
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO artifacts(kind,project_id,message_id,meta,created_at) "
                   "VALUES('extract_v1',1,101,?,200)", ("{synthetic broken JSON",))
    original = path.read_bytes()
    report = repair.plan(path, nonce=NONCE)
    assert report["audit"]["ok"] is True
    assert report["counts"]["stale_v1_messages"] is None
    assert report["gapless_verified"] is False
    assert path.read_bytes() == original


def test_deeply_malformed_medicine_pin_is_refused_without_a_traceback(tmp_path, capsys):
    depth = sys.getrecursionlimit() + 100
    pin = tmp_path / "pin.json"
    pin.write_text("[" * depth + "0" + "]" * depth)
    before = pin.read_bytes()
    assert drug_master.main(["--source", str(tmp_path / "never-read.csv"),
                             "--pin", str(pin), "--dry-run"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "refused" and report["written"] is False
    assert pin.read_bytes() == before
    assert list(tmp_path.iterdir()) == [pin]
