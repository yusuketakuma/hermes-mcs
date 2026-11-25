"""Daily backup compression at publish: .gz stored form, rotation, materialize, prune independence."""
import gzip
import os
import sqlite3
import time
from pathlib import Path

import pytest

import ledger
import local_backup
import maintenance
from backup_names import backup_class, restorable
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
    return source, backups


def test_daily_publish_stores_compressed_with_marker_and_no_plaintext(world):
    source, backups = world
    assert local_backup.daily_backup(str(source)) is None
    names = sorted(p.name for p in backups.iterdir())
    assert "ledger-20261003.db.gz" in names
    assert "ledger-20261003.db.gz.ok" in names
    assert "ledger-20261003.db" not in names
    assert not (backups / "ledger-20261003.db.gz.tmp").exists()


def test_compressed_copy_is_a_complete_valid_database(world):
    source, backups = world
    local_backup.daily_backup(str(source))
    with gzip.open(backups / "ledger-20261003.db.gz", "rb") as f:
        raw = f.read()
    probe = backups / "probe.db"
    probe.write_bytes(raw)
    try:
        con = sqlite3.connect(str(probe))
        try:
            assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            con.close()
    finally:
        probe.unlink()


def test_materialize_backup_roundtrip_and_passthrough(world):
    source, backups = world
    local_backup.daily_backup(str(source))
    gz = str(backups / "ledger-20261003.db.gz")
    with local_backup.materialize_backup(gz) as real:
        assert real != gz and os.path.isfile(real)
        con = sqlite3.connect(Path(real).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            con.close()
        staged = real
    assert not os.path.exists(staged)  # staging copy removed on exit
    with local_backup.materialize_backup(str(source)) as real:
        assert real == str(source)  # uncompressed passes through untouched


def test_backup_class_and_restorable_are_gz_aware():
    assert backup_class("ledger-20261003.db.gz") == "daily"
    assert backup_class("ledger-20261003.db.gz.ok") == "daily_ok"
    assert backup_class("preupdate-x.db.gz") == "preupdate"
    assert restorable("ledger-20261003.db.gz") is True
    assert restorable("ledger-20261003.db") is True
    assert restorable("ledger-20261003.db.gz.ok") is False
    assert backup_class("ledger-20261003.db.gz.tmp") == "staging"


def test_rotation_counts_compressed_generations(world, monkeypatch):
    source, backups = world
    days = ["20261003", "20261004", "20261005", "20261006"]
    for day in days:
        monkeypatch.setattr(maintenance.time, "strftime", lambda _, d=day: d)
        local_backup.daily_backup(str(source))
    stored = sorted(p.name for p in backups.iterdir() if p.name.endswith(".gz"))
    assert len(stored) == local_backup.BACKUP_KEEP
    assert f"ledger-{days[0]}.db.gz" not in stored  # oldest rotated out


def test_same_day_verified_compressed_copy_is_not_rewritten(world):
    source, backups = world
    assert local_backup.daily_backup(str(source)) is None
    gz = backups / "ledger-20261003.db.gz"
    sig = (gz.stat().st_size, gz.stat().st_mtime_ns)
    assert local_backup.daily_backup(str(source)) is None
    assert (gz.stat().st_size, gz.stat().st_mtime_ns) == sig


def test_uncompressed_same_day_copy_converges_to_compressed(world):
    source, backups = world
    local_backup.daily_backup(str(source))
    # simulate a pre-compression-era valid same-day copy: decompress back
    gz = backups / "ledger-20261003.db.gz"
    plain = backups / "ledger-20261003.db"
    with gzip.open(gz, "rb") as f:
        plain.write_bytes(f.read())
    local_backup._write_ok_marker(str(plain))
    for victim in (gz, backups / "ledger-20261003.db.gz.ok"):
        victim.unlink()
    assert local_backup.daily_backup(str(source)) is None
    assert not plain.exists()
    assert gz.is_file() and (backups / "ledger-20261003.db.gz.ok").is_file()


def test_prune_removes_stranded_compressed_preupdate_and_its_marker(world, monkeypatch):
    source, backups = world
    monkeypatch.setattr(local_backup, "PREUPDATE_GRACE_S", 0)
    backups.mkdir(mode=0o700)
    state = backups / "update_state.json"
    state.write_text('{"applied": []}')
    stranded = backups / "preupdate-20261003-120000-abcd.db.gz"
    stranded.write_bytes(b"synthetic")
    (backups / "preupdate-20261003-120000-abcd.db.gz.ok").write_text("{}")
    assert local_backup.prune_preupdate_backups(str(state)) == 1
    assert not stranded.exists()
    assert not (backups / "preupdate-20261003-120000-abcd.db.gz.ok").exists()


def test_prune_keeps_everything_when_state_is_unreadable(world, monkeypatch):
    source, backups = world
    monkeypatch.setattr(local_backup, "PREUPDATE_GRACE_S", 0)
    backups.mkdir(mode=0o700)
    stranded = backups / "preupdate-20261003-120000-abcd.db.gz"
    stranded.write_bytes(b"synthetic")
    assert local_backup.prune_preupdate_backups(str(backups / "missing.json")) == 0
    assert stranded.is_file()


def test_deadline_stop_during_compression_keeps_valid_uncompressed_copy(world, monkeypatch):
    source, backups = world
    def explode(*_a, **_k):
        raise local_backup.MaintenanceError("backup_deadline_exceeded")
    monkeypatch.setattr(local_backup, "_compress_gz", explode)
    assert local_backup.daily_backup(str(source)) is None
    plain = backups / "ledger-20261003.db"
    assert plain.is_file()  # the verified copy stands; compression retries a later tick
    assert (backups / "ledger-20261003.db.ok").is_file()
