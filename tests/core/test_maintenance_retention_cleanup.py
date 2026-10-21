"""Preupdate backup retention/disk check, overdue
housekeeping, batched attachment prune, leftover cleanup, log targets
and lookup indexes. Temp files and synthetic rows only."""
import json
import os
import sqlite3
import time
from contextlib import closing
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
    att = tmp_path / "attachments"
    att.mkdir()
    for aid in (1, 2):
        path = att / str(aid)
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
        with closing(sqlite3.connect(tmp_path / "ledger.db")) as other, other:
            seen.append(other.execute(
                "SELECT state,local_path FROM attachments WHERE attachment_id=?",
                (int(os.path.basename(path)),)).fetchone())
        real_unlink(path, *a, **k)

    monkeypatch.setattr(maintenance.os, "unlink", unlink)
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 2
    assert seen == [("pruned", str(att / "1")), ("pruned", str(att / "2"))]
    assert not (att / "1").exists() and not (att / "2").exists()


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

    real = os.lstat

    def lstat(path, *args, **kwargs):
        checked.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(maintenance.os, "lstat", lstat)
    maintenance.rotate_log()
    names = {os.path.basename(p) for p in checked}
    assert {"update.log", "recovery.log", "cmd_int.log", "llamacpp.log"} <= names
    assert str(tmp_path / ".hermes" / "logs" / "llamacpp.log") in checked


def test_rotate_log_covers_standalone_llamacpp_log(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setattr(maintenance, "HOME", str(tmp_path / "mcs"))
    checked = []

    real = os.lstat

    def lstat(path, *args, **kwargs):
        checked.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(maintenance.os, "lstat", lstat)
    maintenance.rotate_log()
    assert str(tmp_path / "mcs" / "logs" / "llamacpp.log") in checked
    assert str(tmp_path / "user" / ".hermes" / "logs" / "llamacpp.log") in checked


def test_rotate_log_keeps_rotated_copy_private_under_loose_umask(monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "LOG_MAX", 3)
    fresh, stale = tmp_path / "fresh.log", tmp_path / "stale.log"
    for log in (fresh, stale):
        log.write_bytes(b"synthetic line\n")
        log.chmod(0o600)
    (tmp_path / "stale.log.1").write_bytes(b"old")
    (tmp_path / "stale.log.1").chmod(0o644)
    old = os.umask(0o022)
    try:
        maintenance.rotate_log([str(fresh), str(stale)])
    finally:
        os.umask(old)
    for log in (fresh, stale):
        rotated = tmp_path / (log.name + ".1")
        assert rotated.read_bytes() == b"synthetic line\n"
        assert rotated.stat().st_mode & 0o777 == 0o600
        assert log.read_bytes() == b""


def test_prune_leftovers_removes_stranded_backup_and_snapshot_copies(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "HOME", str(tmp_path))
    backups, snaps = tmp_path / "backups", tmp_path / "snapshots"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(snaps))
    backups.mkdir()
    snaps.mkdir()
    for f in (backups / "ledger-20260101.db.tmp", backups / "preupdate-x-1.db.tmp",
              backups / "ledger-20260101.db", backups / "preupdate-x-2.db",
              snaps / ".snapshot.abc.tmp", snaps / "ledger-snapshot.db"):
        f.write_bytes(b"x")
    assert maintenance.prune_leftovers() == 0               # in-flight copies stay
    monkeypatch.setattr(maintenance, "LEFTOVER_KEEP_S", -60)
    assert maintenance.prune_leftovers() == 3
    assert sorted(p.name for p in backups.iterdir()) == [
        "ledger-20260101.db", "preupdate-x-2.db"]
    assert [p.name for p in snaps.iterdir()] == ["ledger-snapshot.db"]


def test_prune_leftovers_removes_stranded_staging_sidecars(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "HOME", str(tmp_path))
    backups, snaps = tmp_path / "backups", tmp_path / "snapshots"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(snaps))
    backups.mkdir()
    snaps.mkdir()
    stranded = [backups / "ledger-20260101-ab.db.tmp-journal",
                backups / "preupdate-x-1.db.tmp-wal",
                backups / "preupdate-x-1.db.tmp-shm",
                snaps / ".snapshot.abc.tmp-journal",
                snaps / ".snapshot.abc.tmp-wal",
                snaps / ".snapshot.abc.tmp-shm"]
    kept = [backups / "ledger-20260101.db", snaps / "ledger-snapshot.db"]
    for f in stranded + kept:
        f.write_bytes(b"x")
    assert maintenance.prune_leftovers() == 0               # in-flight copies stay
    monkeypatch.setattr(maintenance, "LEFTOVER_KEEP_S", -60)
    assert maintenance.prune_leftovers() == len(stranded)
    assert sorted(p for p in [*backups.iterdir(), *snaps.iterdir()]) == sorted(kept)


def test_rotate_log_refuses_symlinked_copy_and_still_rotates_the_rest(monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "LOG_MAX", 3)
    linked, plain = tmp_path / "linked.log", tmp_path / "plain.log"
    for log in (linked, plain):
        log.write_bytes(b"synthetic line\n")
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"keep")
    (tmp_path / "linked.log.1").symlink_to(target)
    with pytest.raises(maintenance.MaintenanceError, match="log_rotate_symlink_refused"):
        maintenance.rotate_log([str(linked), str(plain)])
    assert target.read_bytes() == b"keep"                   # never written through
    assert linked.read_bytes() == b"synthetic line\n"      # not truncated unrotated
    assert (tmp_path / "plain.log.1").read_bytes() == b"synthetic line\n"
    assert plain.read_bytes() == b""


def test_daily_backup_staging_copy_is_private_from_creation(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    writer = ledger.Ledger(str(source))
    writer.ensure_patient(1)
    writer.close()
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(maintenance, "disk_floor_mb", lambda: 0)
    real_valid = maintenance.valid_mcs_db
    staged = []

    def spy(path, **kwargs):
        if path.endswith(".tmp"):
            staged.append(os.stat(path).st_mode & 0o777)
        return real_valid(path, **kwargs)

    monkeypatch.setattr(maintenance, "valid_mcs_db", spy)
    old = os.umask(0o022)
    try:
        maintenance.daily_backup(str(source))
    finally:
        os.umask(old)
    assert staged == [0o600]
    assert list(backups.glob("*.tmp")) == []
    assert len(list(backups.glob("ledger-*.db"))) == 1


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


# Regression: preupdate backup survives prune before it is recorded, and a
# failed backup never strands a partial *.db.tmp copy.
def _source(tmp_path):
    src = tmp_path / "ledger.db"
    db = ledger.Ledger(str(src))
    db.ensure_patient(1)
    db.close()
    return str(src)


def test_unrecorded_fresh_preupdate_backup_survives_prune(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    state = tmp_path / "update_state.json"
    state.write_text(json.dumps({"applied": [], "applying": None}))
    fresh = maintenance.preupdate_backup(_source(tmp_path))
    stale = backups / "preupdate-old.db"
    stale.write_bytes(b"x")
    old = time.time() - maintenance.PREUPDATE_GRACE_S - 60
    os.utime(stale, (old, old))
    assert maintenance.prune_preupdate_backups(str(state)) == 1
    assert os.path.exists(fresh)
    assert not stale.exists()


@pytest.mark.parametrize("fn", ["preupdate_backup", "daily_backup"])
def test_failed_backup_removes_tmp(tmp_path, monkeypatch, fn):
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    src = _source(tmp_path)
    real_connect = sqlite3.connect

    class Boom:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, *a):
            return self.conn.execute(*a)

        def backup(self, dst, **_):
            raise sqlite3.OperationalError("database or disk is full")

        def close(self):
            self.conn.close()

    def connect(path, *a, **kw):
        conn = real_connect(path, *a, **kw)
        return Boom(conn) if kw.get("uri") else conn

    monkeypatch.setattr(maintenance.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError):
        getattr(maintenance, fn)(src)
    assert [p for p in os.listdir(backups) if p.endswith(".tmp")] == []
