"""Local backups of the home-care records database: daily, pre-update, list/verify and plain restore — synthetic files only."""
import json
import os
import sqlite3

import pytest

import backup_names
import homecare_catalog
import homecare_store
import local_backup
import mcs_local_backup

MODULES = homecare_catalog.registry(["pharmacy"]).modules


@pytest.fixture
def world(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    backups = data / "backups"
    homecare = data / "homecare.db"
    monkeypatch.setattr(local_backup, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(local_backup, "HOMECARE_DB", str(homecare))
    usage = local_backup.shutil.disk_usage(tmp_path)
    monkeypatch.setattr(local_backup.shutil, "disk_usage",
                        lambda _: type(usage)(usage.total, usage.used, 16 * 1024**3))
    parent = tmp_path / "restore-parent"
    parent.mkdir(mode=0o700)
    return homecare, backups, parent


def _records(path, note):
    """A WAL home-care database whose last write is still only in the -wal file."""
    db = homecare_store.connect(path, MODULES)
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("CREATE TABLE IF NOT EXISTS synthetic_note(text TEXT)")
    db.execute("INSERT INTO synthetic_note VALUES (?)", (note,))
    return db


def _notes(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [row[0] for row in db.execute("SELECT text FROM synthetic_note")]
    finally:
        db.close()


def _stamp(monkeypatch, value):
    monkeypatch.setattr(local_backup.time, "strftime", lambda fmt: value if "-" not in fmt else value + "-000000")


def test_absent_records_database_is_neither_created_nor_backed_up(world):
    homecare, backups, _ = world
    assert local_backup.daily_homecare_backup() is None
    assert local_backup.preupdate_homecare_backup() is None
    assert not homecare.exists() and not backups.exists()


def test_daily_copy_includes_wal_writes_and_rotates_only_its_own_files(world, monkeypatch):
    homecare, backups, _ = world
    live = _records(homecare, "合成メモ")
    try:
        assert (homecare.parent / "homecare.db-wal").stat().st_size > 0
        for day in ("20261101", "20261102", "20261103", "20261104"):
            _stamp(monkeypatch, day)
            assert local_backup.daily_homecare_backup() is None
        backups.joinpath("ledger-20261101.db").write_bytes(b"synthetic ledger")
        _stamp(monkeypatch, "20261105")
        assert local_backup.daily_homecare_backup() is None
    finally:
        live.close()
    names = sorted(os.listdir(backups))
    assert [n for n in names if n.startswith("homecare-") and n.endswith(".db")] == [
        "homecare-20261103.db", "homecare-20261104.db", "homecare-20261105.db"]
    assert "ledger-20261101.db" in names and "homecare-20261105.db.ok" in names
    assert _notes(backups / "homecare-20261105.db") == ["合成メモ"]
    assert oct(os.stat(backups / "homecare-20261105.db").st_mode & 0o777) == "0o600"


def test_a_file_that_is_not_home_care_records_is_refused(world):
    homecare, backups, _ = world
    other = sqlite3.connect(homecare)
    other.execute("CREATE TABLE unrelated(x)")
    other.commit()
    other.close()
    with pytest.raises(local_backup.MaintenanceError, match="backup_verify_failed"):
        local_backup.daily_homecare_backup()
    assert not [n for n in os.listdir(backups) if n.startswith("homecare-")]


def test_preupdate_copies_keep_the_newest_three(world, monkeypatch):
    homecare, backups, _ = world
    _records(homecare, "合成").close()
    paths = []
    for second in range(4):
        monkeypatch.setattr(local_backup.time, "strftime", lambda _f, s=second: f"20261101-00000{s}")
        paths.append(local_backup.preupdate_homecare_backup())
    assert len([n for n in os.listdir(backups) if n.endswith(".db")]) == 4   # rotation is separate
    local_backup.rotate_homecare_preupdate()
    kept = sorted(n for n in os.listdir(backups) if n.endswith(".db"))
    assert kept == sorted(os.path.basename(p) for p in paths[1:])
    assert {backup_names.backup_class(n) for n in kept} == {"homecare_preupdate"}


def test_a_hand_named_preupdate_copy_is_never_rotated(world, monkeypatch):
    homecare, backups, _ = world
    _records(homecare, "合成").close()
    backups.mkdir(mode=0o700)
    hand = backups / "homecare-preupdate-1.2.2.db"
    hand.write_bytes(b"synthetic hand copy")
    for second in range(4):
        monkeypatch.setattr(local_backup.time, "strftime", lambda _f, s=second: f"20261101-00000{s}")
        local_backup.preupdate_homecare_backup()
    local_backup.rotate_homecare_preupdate()
    assert hand.exists() and backup_names.backup_class(hand.name) == "manual"


def test_a_failed_attempt_discards_only_its_own_copy(world, monkeypatch):
    homecare, backups, _ = world
    _records(homecare, "合成").close()
    monkeypatch.setattr(local_backup.time, "strftime", lambda _f: "20261101-000000")
    kept = local_backup.preupdate_homecare_backup()
    monkeypatch.setattr(local_backup.time, "strftime", lambda _f: "20261102-000000")
    failed = local_backup.preupdate_homecare_backup()
    local_backup.discard_homecare_preupdate(failed)
    local_backup.discard_homecare_preupdate(str(homecare))      # never a non-copy
    assert os.path.exists(kept) and not os.path.exists(failed) and homecare.exists()


def test_preupdate_room_counts_the_home_care_copy(world, tmp_path):
    homecare, _backups, _ = world
    ledger = tmp_path / "ledger.db"
    ledger.write_bytes(b"x" * 1024 * 1024)
    without = local_backup.preupdate_need_mb(str(ledger))
    homecare.write_bytes(b"y" * 3 * 1024 * 1024)
    assert local_backup.preupdate_need_mb(str(ledger)) == pytest.approx(without + 6)


def test_home_care_copies_are_not_ledger_restore_points():
    assert backup_names.backup_class("homecare-20261101.db") == "homecare_daily"
    assert backup_names.backup_class("homecare-20261101.db.ok") == "homecare_daily_ok"
    assert backup_names.backup_class("homecare-preupdate-20261101-000000-x.db") == "homecare_preupdate"
    assert not backup_names.restorable("homecare-20261101.db")
    assert not backup_names.restorable("homecare-preupdate-20261101-000000-x.db")
    assert backup_names.restorable("ledger-20261101.db") and backup_names.restorable("hand-made.db")


def _cli(capsys, *argv):
    code = mcs_local_backup.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_verify_and_restore_place_a_plain_copy_the_records_code_opens(world, capsys, monkeypatch):
    homecare, backups, parent = world
    _stamp(monkeypatch, "20261101")
    _records(homecare, "合成メモ").close()
    local_backup.daily_homecare_backup()
    code, out = _cli(capsys, "list", "--verify")
    [row] = out["backups"]
    assert code == 0 and (row["kind"], row["check"]) == ("homecare_daily", "ok")
    assert row["migrations"]["core"] >= 1 and row["migrations"]["pharmacy"] >= 1
    target = parent / "restored"
    code, out = _cli(capsys, "restore", "--backup", "homecare-20261101.db",
                     "--destination", str(target))
    assert code == 0 and out["sha256"] == row["sha256"]
    assert sorted(os.listdir(target)) == ["homecare.db"]
    assert out["database"] == str(target / "homecare.db")
    homecare_store.connect(target / "homecare.db", MODULES).close()
    assert _notes(target / "homecare.db") == ["合成メモ"]


def test_restore_refuses_a_copy_without_the_foundation_migration(world, capsys):
    _homecare, backups, parent = world
    backups.mkdir(mode=0o700)
    fake = sqlite3.connect(backups / "homecare-20261101.db")
    fake.execute("CREATE TABLE schema_migrations(module, number, name, sha256, applied_at)")
    fake.commit()
    fake.close()
    code, out = _cli(capsys, "restore", "--backup", "homecare-20261101.db",
                     "--destination", str(parent / "restored"))
    assert code == 1 and out == {"error": "backup_homecare_schema_invalid"}
    assert not (parent / "restored").exists()
