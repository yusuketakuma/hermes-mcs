"""Synthetic recovery contracts for RT-034, RT-035, and RT-036.

The fixture is deliberately small and private to this test module. It uses
the real SQLite writer, verified backup, snapshot publisher, read-only reader,
and JSON CLI paths without contacting MCS, Discord, Keychain, or a live HOME.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import ledger
import maintenance
import mcs_view
from mcs_adapter import Attachment, Message


# This is a predeclared synthetic smoke limit, not a production performance
# target. RT-038 requires the varying inputs and tolerance to be fixed before
# measuring; a real-device performance claim needs a separate approved run.
SYNTHETIC_WALL_LIMIT_SECONDS = 5.0


def _build_fixture(root: Path) -> tuple[Path, dict]:
    """Create one source DB and a relative attachment manifest."""
    source = root / "source.db"
    db = ledger.Ledger(str(source))
    db.ensure_patient(1)
    attachment_bytes = b"synthetic attachment for recovery"
    attachment_hash = hashlib.sha256(attachment_bytes).hexdigest()
    attachment = Attachment("file-1", "document.txt", "https://invalid.test/file")
    reply = Message(
        message_id=2,
        project_id=1,
        parent_id=1,
        sender_id=2,
        sender_name="reply sender",
        sender_type="user",
        profession="",
        organization="",
        posted_at="2026-09-19T00:01:00+09:00",
        body_html="<p>recovery reply</p>",
        body_state="full",
        is_unread=False,
        reply_count=0,
    )
    root_message = Message(
        message_id=1,
        project_id=1,
        parent_id=None,
        sender_id=1,
        sender_name="root sender",
        sender_type="user",
        profession="",
        organization="",
        posted_at="2026-09-19T00:00:00+09:00",
        body_html="<p>recovery root</p>",
        body_state="full",
        is_unread=False,
        reply_count=1,
        replies=[reply],
        attachments=[attachment],
    )
    db.save_messages([root_message])
    with db.db:
        db.db.execute(
            "UPDATE patients SET patient_name=?,project_type=?,url=? "
            "WHERE project_id=1",
            (
                "synthetic patient",
                "medical",
                "https://www.medical-care.net/projects/medical/1",
            ),
        )
        db.db.execute(
            "UPDATE attachments SET local_path=?,bytes=?,sha256=?,state='downloaded' "
            "WHERE message_id=? AND file_id=?",
            (
                "attachments/document.txt",
                len(attachment_bytes),
                attachment_hash,
                1,
                "file-1",
            ),
        )
    db.close()

    attachments = root / "attachments"
    attachments.mkdir()
    (attachments / "document.txt").write_bytes(attachment_bytes)
    manifest = {
        "files": [
            {
                "message_id": 1,
                "file_id": "file-1",
                "path": "attachments/document.txt",
                "bytes": len(attachment_bytes),
                "sha256": attachment_hash,
            }
        ]
    }
    manifest_path = root / "attachments-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return source, {"attachments": attachments, "manifest": manifest_path, **manifest}


class _CountingSource:
    """Delegate a read-only sqlite connection while counting backup calls."""

    def __init__(self, connection, calls: list[str]):
        self._connection = connection
        self._calls = calls

    def backup(self, destination, *args, **kwargs):
        self._calls.append("backup")
        return self._connection.backup(destination, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _InterruptedSource(_CountingSource):
    def backup(self, destination, *args, **kwargs):
        self._calls.append("interrupted")
        raise sqlite3.DatabaseError("synthetic_backup_interrupt")


def test_interrupted_backup_retains_last_verified_generation(tmp_path, monkeypatch):
    """RT-034: an interrupted candidate cannot replace the valid generation."""
    source, _ = _build_fixture(tmp_path)
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backup_dir))
    stamps = iter(("20260920", "20260921", "20260922"))
    monkeypatch.setattr(maintenance.time, "strftime", lambda _fmt: next(stamps))

    maintenance.daily_backup(str(source))
    previous = backup_dir / "ledger-20260920.db"
    previous_bytes = previous.read_bytes()
    assert maintenance.valid_mcs_db(str(previous))

    real_connect = maintenance.sqlite3.connect

    def interrupted_connect(database, *args, **kwargs):
        connection = real_connect(database, *args, **kwargs)
        if str(database).endswith("source.db?mode=ro"):
            return _InterruptedSource(connection, [])
        return connection

    monkeypatch.setattr(maintenance.sqlite3, "connect", interrupted_connect)
    with pytest.raises(sqlite3.DatabaseError, match="synthetic_backup_interrupt"):
        maintenance.daily_backup(str(source))

    # The failed run was for a new generation. The previously published file
    # remains byte-for-byte unchanged and still passes the static validator.
    assert previous.read_bytes() == previous_bytes
    assert maintenance.valid_mcs_db(str(previous))
    assert not (backup_dir / "ledger-20260921.db").exists()
    assert not (backup_dir / "ledger-20260921.db.tmp").exists()

    # The failed run removed its own candidate; a subsequent run can publish the
    # next generation once the source path is healthy again.
    monkeypatch.setattr(maintenance.sqlite3, "connect", real_connect)
    maintenance.daily_backup(str(source))
    assert maintenance.valid_mcs_db(str(backup_dir / "ledger-20260922.db"))



def test_backup_skipped_when_disk_cannot_hold_two_copies(tmp_path, monkeypatch):
    source, _ = _build_fixture(tmp_path)
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("MCS_DISK_GUARD_MB", "512")
    usage = shutil.disk_usage(tmp_path)
    need = source.stat().st_size * 2 + 512 * 1024 * 1024
    monkeypatch.setattr(maintenance.shutil, "disk_usage",
                        lambda _p: usage._replace(free=need - 1))
    assert maintenance.daily_backup(str(source)) == "skipped_disk_low"
    assert list(backup_dir.iterdir()) == []
    monkeypatch.setattr(maintenance.shutil, "disk_usage",
                        lambda _p: usage._replace(free=need + 1))
    assert maintenance.daily_backup(str(source)) is None
    assert len(list(backup_dir.glob("ledger-*.db"))) == 1


def _counting_validator(monkeypatch):
    calls: list[str] = []
    real = maintenance.valid_mcs_db

    def valid(path):
        calls.append(os.path.basename(path))
        return real(path)
    monkeypatch.setattr(maintenance, "valid_mcs_db", valid)
    return calls


def test_same_day_backup_skips_quick_check_while_unchanged(tmp_path,
                                                           monkeypatch):
    source, _ = _build_fixture(tmp_path)
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(maintenance.time, "strftime", lambda _f: "20260920")
    maintenance.daily_backup(str(source))
    dest = backup_dir / "ledger-20260920.db"
    assert (backup_dir / "ledger-20260920.db.ok").exists()
    calls = _counting_validator(monkeypatch)
    maintenance.daily_backup(str(source))
    assert calls == []
    # a changed (here: corrupted) file no longer matches the marker, is
    # re-validated and replaced by a fresh verified backup (B24)
    dest.write_bytes(b"not a database" * 100)
    maintenance.daily_backup(str(source))
    assert calls[0] == "ledger-20260920.db"
    assert maintenance.valid_mcs_db(str(dest))
    calls.clear()
    maintenance.daily_backup(str(source))
    assert calls == []


def test_backup_without_marker_is_still_validated(tmp_path, monkeypatch):
    source, _ = _build_fixture(tmp_path)
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(maintenance.time, "strftime", lambda _f: "20260920")
    maintenance.daily_backup(str(source))
    (backup_dir / "ledger-20260920.db.ok").unlink()
    calls = _counting_validator(monkeypatch)
    maintenance.daily_backup(str(source))
    assert calls == ["ledger-20260920.db"]


def test_backup_rotation_removes_markers(tmp_path, monkeypatch):
    source, _ = _build_fixture(tmp_path)
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backup_dir))
    stamps = iter(f"202609{d:02d}" for d in range(1, 20))
    monkeypatch.setattr(maintenance.time, "strftime",
                        lambda _f: next(stamps))
    for _ in range(maintenance.BACKUP_KEEP + 2):
        maintenance.daily_backup(str(source))
    dbs = sorted(p.name for p in backup_dir.glob("ledger-*.db"))
    oks = sorted(p.name[:-3] for p in backup_dir.glob("ledger-*.db.ok"))
    assert len(dbs) == maintenance.BACKUP_KEEP
    assert oks == dbs


def test_independent_restore_preserves_relations_fts_snapshot_and_attachment_hash(
    tmp_path, monkeypatch
):
    """RT-035/038: restore one fixed fixture and compare logical evidence."""
    source_root = tmp_path / "source"
    source_root.mkdir()
    source, fixture = _build_fixture(source_root)
    backup_dir = source_root / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(maintenance.time, "strftime", lambda _fmt: "20260920")

    backup_calls: list[str] = []
    real_connect = maintenance.sqlite3.connect

    def counting_connect(database, *args, **kwargs):
        connection = real_connect(database, *args, **kwargs)
        if str(database).endswith("source.db?mode=ro"):
            return _CountingSource(connection, backup_calls)
        return connection

    monkeypatch.setattr(maintenance.sqlite3, "connect", counting_connect)
    started = time.monotonic()
    maintenance.daily_backup(str(source))
    backup = backup_dir / "ledger-20260920.db"

    restore_root = tmp_path / "restore"
    restore_root.mkdir()
    restored_db = restore_root / "ledger.db"
    shutil.copy2(backup, restored_db)
    shutil.copytree(fixture["attachments"], restore_root / "attachments")
    shutil.copy2(fixture["manifest"], restore_root / "attachments-manifest.json")

    reader = ledger.LedgerReader(str(restored_db))
    try:
        counts = {
            "patients": reader.db.execute("SELECT count(*) FROM patients").fetchone()[0],
            "messages": reader.db.execute("SELECT count(*) FROM messages").fetchone()[0],
            "attachments": reader.db.execute("SELECT count(*) FROM attachments").fetchone()[0],
        }
        assert counts == {"patients": 1, "messages": 2, "attachments": 1}
        relation = reader.db.execute(
            "SELECT parent_id FROM messages WHERE message_id=2"
        ).fetchone()[0]
        assert relation == 1
        assert {row["message_id"] for row in reader.search("recovery")} == {1, 2}
        assert reader.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"

        manifest = json.loads(
            (restore_root / "attachments-manifest.json").read_text(encoding="utf-8")
        )
        entry = manifest["files"][0]
        attachment = reader.db.execute(
            "SELECT file_id,bytes,sha256,state FROM attachments "
            "WHERE message_id=? AND file_id=?",
            (entry["message_id"], entry["file_id"]),
        ).fetchone()
        restored_file = restore_root / entry["path"]
        assert dict(attachment) == {
            "file_id": entry["file_id"],
            "bytes": entry["bytes"],
            "sha256": entry["sha256"],
            "state": "downloaded",
        }
        assert hashlib.sha256(restored_file.read_bytes()).hexdigest() == entry["sha256"]
        assert restored_file.stat().st_size == entry["bytes"]
    finally:
        reader.close()

    monkeypatch.setattr(maintenance.sqlite3, "connect", real_connect)
    snapshot_dir = restore_root / "snapshots"
    snapshot = ledger.publish_snapshot(str(restored_db), str(snapshot_dir))
    assert snapshot is not None
    view = mcs_view.View(snapshot)
    try:
        assert [row["message_id"] for row in view.read("timeline", project=1)["items"]] == [1]
        assert view.read("thread", project=1, message_id=1)["items"][0]["message_id"] == 2
        assert view.read("attachments", project=1, message_id=1)["items"][0]["name"] == "document.txt"
    finally:
        view.close()

    assert backup_calls == ["backup"]
    assert time.monotonic() - started < SYNTHETIC_WALL_LIMIT_SECONDS


def test_explicit_snapshot_cli_survives_foreign_cwd_and_home(tmp_path):
    """RT-036: the old JSON facade honors an explicit snapshot path."""
    source_root = tmp_path / "source"
    source_root.mkdir()
    source, _ = _build_fixture(source_root)
    snapshot_dir = tmp_path / "snapshots"
    snapshot = ledger.publish_snapshot(str(source), str(snapshot_dir))
    assert snapshot is not None

    cwd = tmp_path / "foreign-cwd"
    home = tmp_path / "foreign-home"
    cwd.mkdir()
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "mcs") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "mcs" / "views" / "mcs_view.py"),
            "--snapshot",
            str(snapshot),
            "status",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["project_id"] is None
    assert payload["items"][0]["project_id"] == 1
    assert not (home / ".mcs").exists()
