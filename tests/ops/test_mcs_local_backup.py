"""Local backup list/verify/held restore with a synthetic ledger; never live HOME."""
import json
import os
from pathlib import Path

import pytest

import ledger
import local_backup
import maintenance
import mcs_local_backup
import mcs_restore
import notify_spool
from mcs_util import file_sha256
from test_ledger_recovery_contract import _build_fixture


@pytest.fixture
def world(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir(mode=0o700)
    source, _ = _build_fixture(source_dir)
    db = ledger.Ledger(str(source))
    db.finish_run(db.begin_run(None), "ok")
    db.close()
    backups = source_dir / "backups"
    monkeypatch.setattr(local_backup, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(maintenance.time, "strftime", lambda _: "20261003")
    usage = maintenance.shutil.disk_usage(source_dir)
    monkeypatch.setattr(maintenance.shutil, "disk_usage",
                        lambda _: type(usage)(usage.total, usage.used, 16 * 1024**3))
    assert local_backup.daily_backup(str(source)) is None
    parent = tmp_path / "restore-parent"
    parent.mkdir(mode=0o700)
    return source, backups, parent


def _cli(capsys, *argv):
    code = mcs_local_backup.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_list_reads_names_and_markers_without_opening_databases(world, capsys, monkeypatch):
    _, backups, _ = world
    monkeypatch.setattr(mcs_local_backup.backup_evidence, "inventory_snapshot",
                        lambda *_a, **_k: pytest.fail("list opened a database"))
    (backups / "hand-made.db").write_bytes(b"synthetic")
    code, out = _cli(capsys, "list")
    assert code == 0
    assert [(r["name"], r["kind"], r["marker"]) for r in out["backups"]] == [
        ("ledger-20261003.db", "daily", "verified")]


def test_verify_reports_integrity_and_the_restore_hash(world, capsys):
    _, backups, _ = world
    code, out = _cli(capsys, "list", "--verify")
    [row] = out["backups"]
    assert code == 0 and row["check"] == "ok"
    assert row["sha256"] == file_sha256(backups / "ledger-20261003.db")
    assert row["schema"] == ledger.SCHEMA_VERSION and row["last_successful_run"] is not None


def test_verify_flags_a_damaged_generation_and_fails(world, capsys):
    _, backups, _ = world
    (backups / "ledger-20261002.db").write_bytes(b"not a database")
    code, out = _cli(capsys, "list", "--verify")
    checks = {r["name"]: r["check"] for r in out["backups"]}
    assert code == 1
    assert checks["ledger-20261003.db"] == "ok"
    assert checks["ledger-20261002.db"] != "ok"


def test_restore_places_a_held_copy_that_mcs_restore_can_plan(world, capsys):
    source, backups, parent = world
    live = file_sha256(source)
    target = parent / "data"
    code, report = _cli(capsys, "restore", "--backup", "ledger-20261003.db",
                        "--destination", str(target))
    assert code == 0 and report["restore_pending"] is True
    assert report["sha256"] == file_sha256(backups / "ledger-20261003.db")
    assert file_sha256(target / "ledger.db") == report["plain_sha256"]
    assert notify_spool.restore_awaiting_consent(str(target))
    assert file_sha256(source) == live
    assert sorted(os.listdir(parent)) == ["data"]       # no plaintext work dir left
    plan = mcs_restore.plan(str(target), source_sha256=report["sha256"])
    approval = mcs_restore.approve(
        str(target), source_sha256=report["sha256"], plan_sha256=plan["plan_sha256"],
        confirm_human=True, actor="synthetic-operator", reason="synthetic drill",
        custody_ref="synthetic-ref", delivery_policy="hold_all")
    resumed = mcs_restore.resume(
        str(target), receipt_sha256=approval["receipt_sha256"], confirm_human=True,
        actor="synthetic-operator", reason="synthetic drill")
    assert resumed["writers_resumed"] is True and resumed["delivery_policy"] == "hold_all"


@pytest.mark.parametrize("name", ["../ledger.db", "ledger.db", "hand-made.db", "ledger-2026.db"])
def test_restore_accepts_only_rotation_or_updater_names(world, capsys, name):
    _, _, parent = world
    code, out = _cli(capsys, "restore", "--backup", name, "--destination", str(parent / "data"))
    assert code == 1 and out == {"error": "backup_local_name_invalid"}
    assert not (parent / "data").exists()


def test_restore_never_reuses_an_existing_directory(world, capsys):
    _, _, parent = world
    (parent / "data").mkdir(mode=0o700)
    code, out = _cli(capsys, "restore", "--backup", "ledger-20261003.db",
                     "--destination", str(parent / "data"))
    assert code == 1 and out == {"error": "backup_restore_requires_new_local_directory"}
    assert os.listdir(parent / "data") == []


def test_restore_refuses_a_symlinked_backup(world, capsys):
    _, backups, parent = world
    os.symlink(backups / "ledger-20261003.db", backups / "ledger-20261004.db")
    code, out = _cli(capsys, "restore", "--backup", "ledger-20261004.db",
                     "--destination", str(parent / "data"))
    assert code == 1 and out == {"error": "backup_static_snapshot_required"}


def test_restore_refuses_a_damaged_backup_without_publishing(world, capsys):
    _, backups, parent = world
    (backups / "ledger-20261002.db").write_bytes(b"not a database")
    code, out = _cli(capsys, "restore", "--backup", "ledger-20261002.db",
                     "--destination", str(parent / "data"))
    assert code == 1 and "error" in out
    assert not (parent / "data").exists() and os.listdir(parent) == []


def test_restore_requires_a_private_parent(world, capsys):
    _, _, parent = world
    parent.chmod(0o755)
    code, out = _cli(capsys, "restore", "--backup", "ledger-20261003.db",
                     "--destination", str(parent / "data"))
    assert code == 1 and out == {"error": "backup_directory_not_private"}


def test_generations_lists_daily_before_preupdate(world):
    _, backups, _ = world
    for name in ("patient-context-reset-20261002-000000-ab.db", "ledger-20261001.db",
                 "preupdate-20261002-000000-x.db"):
        Path(backups / name).write_bytes(b"synthetic")
    assert [n for n, _, _ in local_backup.generations()] == [
        "ledger-20261003.db", "ledger-20261001.db", "preupdate-20261002-000000-x.db",
        "patient-context-reset-20261002-000000-ab.db"]


def test_restore_accepts_the_copy_taken_before_a_patient_context_reset(world, capsys):
    source, _, parent = world
    reset = Path(local_backup.reset_backup(str(source)))
    code, out = _cli(capsys, "list", "--verify")
    assert code == 0
    assert {r["name"]: (r["kind"], r["check"]) for r in out["backups"]}[reset.name] == (
        "patient_context_reset", "ok")
    target = parent / "data"
    code, report = _cli(capsys, "restore", "--backup", reset.name, "--destination", str(target))
    assert code == 0 and report["restore_pending"] is True
    assert report["sha256"] == file_sha256(reset)
    assert notify_spool.restore_awaiting_consent(str(target))
