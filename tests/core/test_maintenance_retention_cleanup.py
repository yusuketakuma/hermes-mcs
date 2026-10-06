"""Preupdate backup retention/disk check, overdue
housekeeping, batched attachment prune, leftover cleanup, log targets
and lookup indexes. Temp files and synthetic rows only."""
import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

import ledger
import maintenance
from ingest_testkit import _ledger, _message


def _old(path, age=None):
    t = time.time() - (age or maintenance.PREUPDATE_GRACE_S + 60)
    os.utime(path, (t, t))


def test_prune_keeps_only_actionable_preupdate_backups(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    names = ["old-bump", "new-bump", "plain", "applying", "consent", "orphan"]
    paths = {n: backups / f"preupdate-{n}.db" for n in names}
    for p in paths.values():
        p.write_bytes(b"synthetic")
        _old(p)
    state = {
        "applied": [
            {"backup_path": str(paths["old-bump"]), "schema_bump": True},
            {"backup_path": str(paths["new-bump"]), "schema_bump": True},
            {"backup_path": str(paths["plain"]), "schema_bump": False},
        ],
        "applying": {"backup_path": str(paths["applying"])},
        "restore_consent": {"backup_path": str(paths["consent"])},
    }
    state_path = tmp_path / "update_state.json"
    state_path.write_text(json.dumps(state))
    assert maintenance.prune_preupdate_backups(str(state_path)) == 3
    assert sorted(p.name for p in backups.iterdir()) == [
        "preupdate-applying.db", "preupdate-consent.db", "preupdate-new-bump.db"]


def test_preupdate_backup_refuses_when_disk_is_low(tmp_path, monkeypatch):
    src = tmp_path / "ledger.db"
    ledger.Ledger(str(src)).close()
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(maintenance.shutil, "disk_usage",
                        lambda p: SimpleNamespace(free=0))
    with pytest.raises(maintenance.MaintenanceError, match="backup_disk_low"):
        maintenance.preupdate_backup(str(src))
    assert os.listdir(tmp_path / "backups") == []


def test_backup_overdue_only_with_a_stale_newest_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(tmp_path))
    assert maintenance.backup_overdue() is False          # fresh install
    old = tmp_path / "ledger-20260101.db"
    old.write_bytes(b"x")
    _old(old, maintenance.BACKUP_OVERDUE_S + 60)
    assert maintenance.backup_overdue() is True
    (tmp_path / "ledger-20260102.db").write_bytes(b"x")
    assert maintenance.backup_overdue() is False


def test_prune_attachments_commits_before_unlinking(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(1), _message(2)])
    old = time.time() - maintenance.ATTACHMENT_KEEP_S - 60
    for aid in (1, 2):
        path = tmp_path / str(aid)
        path.write_bytes(b"synthetic")
        db.db.execute(
            "INSERT INTO attachments(attachment_id,message_id,name,local_path,"
            "state,downloaded_at) VALUES(?,?,'f.pdf',?,'downloaded',?)",
            (aid, aid, str(path), old))
    db.db.commit()
    db.close()
    monkeypatch.setattr(maintenance, "PRUNE_BATCH", 1)
    real_unlink = os.unlink
    seen = []

    def unlink(path, *a, **k):
        with sqlite3.connect(tmp_path / "ledger.db") as other:
            seen.append(other.execute(
                "SELECT state,local_path FROM attachments WHERE attachment_id=?",
                (int(os.path.basename(path)),)).fetchone())
        real_unlink(path, *a, **k)

    monkeypatch.setattr(maintenance.os, "unlink", unlink)
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 2
    assert seen == [("pruned", None), ("pruned", None)]
    assert not (tmp_path / "1").exists() and not (tmp_path / "2").exists()


def test_prune_leftovers_removes_only_day_old_temp_and_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "HOME", str(tmp_path))
    att = tmp_path / "data" / "attachments"
    cmd = tmp_path / "data" / "cmd"
    att.mkdir(parents=True)
    cmd.mkdir()
    files = [att / ".download-a.part", cmd / "x.json.invalid",
             att / "7", cmd / "y.json"]
    for f in files:
        f.write_bytes(b"x")
    assert maintenance.prune_leftovers() == 0               # all fresh
    monkeypatch.setattr(maintenance, "LEFTOVER_KEEP_S", -60)
    assert maintenance.prune_leftovers() == 2
    assert sorted(p.name for p in att.iterdir()) == ["7"]
    assert sorted(p.name for p in cmd.iterdir()) == ["y.json"]


def test_rotate_log_covers_update_recovery_cmd_int_and_llamacpp(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    checked = []

    def getsize(path):
        checked.append(path)
        return 0

    monkeypatch.setattr(maintenance.os.path, "getsize", getsize)
    maintenance.rotate_log()
    names = {os.path.basename(p) for p in checked}
    assert {"update.log", "recovery.log", "cmd_int.log", "llamacpp.log"} <= names
    assert str(tmp_path / ".hermes" / "logs" / "llamacpp.log") in checked


def test_alert_and_failed_job_lookups_use_indexes(tmp_path):
    db = ledger.Ledger(str(tmp_path / "ledger.db"))
    plan = " ".join(r[3] for r in db.db.execute(
        "EXPLAIN QUERY PLAN SELECT MAX(created_at) FROM notify_outbox WHERE kind=?",
        ("session_expired",)))
    assert "idx_outbox_kind_created" in plan
    plan = " ".join(r[3] for r in db.db.execute(
        "EXPLAIN QUERY PLAN SELECT kind,COUNT(*) FROM fetch_jobs "
        "WHERE state='failed' AND updated_at>=? GROUP BY kind", (0,)))
    assert "idx_fetch_jobs_failed_recent" in plan
    db.close()
