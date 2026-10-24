"""DB capacity observations in health: codes and byte counts only, no DB open beyond the borrowed handle."""
import json
import os
import sqlite3
import time

import pytest

import health_report
import maintenance


@pytest.fixture
def backups(tmp_path, monkeypatch):
    path = tmp_path / "backups"
    path.mkdir()
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(path))
    return path


def test_backup_bytes_are_classified_by_their_owner(backups, tmp_path):
    (backups / "ledger-20260101.db").write_bytes(b"d" * 10)
    (backups / "ledger-20260101.db.ok").write_bytes(b"k" * 2)
    (backups / "preupdate-1.0.28-20260101.db").write_bytes(b"p" * 5)
    (backups / "ledger-20260102.db.tmp-wal").write_bytes(b"s" * 3)
    (backups / "hand-copy.db").write_bytes(b"m" * 7)
    (backups / "subdir").mkdir()
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"x" * 100)
    (backups / "linked.db").symlink_to(outside)
    assert maintenance.backup_bytes_by_class() == {
        "daily": 10, "daily_ok": 2, "preupdate": 5, "staging": 3, "manual": 7}
    assert maintenance.manual_backup_bytes() == 7
    assert sorted(os.listdir(backups)) == sorted([      # observation never deletes
        "ledger-20260101.db", "ledger-20260101.db.ok", "preupdate-1.0.28-20260101.db",
        "ledger-20260102.db.tmp-wal", "hand-copy.db", "subdir", "linked.db"])


def test_missing_directory_is_zero_and_an_unbounded_scan_is_unknown(backups, monkeypatch):
    for n in range(3):
        (backups / f"copy-{n}.db").write_bytes(b"x")
    monkeypatch.setattr(maintenance, "LOCAL_STATUS_SCAN_MAX", 2)
    assert maintenance.backup_bytes_by_class() is None
    assert maintenance.manual_backup_bytes() is None
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups / "absent"))
    assert maintenance.backup_bytes_by_class() == dict.fromkeys(maintenance.BACKUP_CLASSES, 0)


def test_ledger_capacity_reads_page_totals_from_the_borrowed_handle(tmp_path):
    db = sqlite3.connect(tmp_path / "synthetic.db")
    db.execute("CREATE TABLE t (v TEXT)")
    db.executemany("INSERT INTO t VALUES (?)", [("x" * 2000,)] * 200)
    db.commit()
    db.execute("DELETE FROM t")
    db.commit()
    pages, free, size = (db.execute(f"PRAGMA {n}").fetchone()[0]
                         for n in ("page_count", "freelist_count", "page_size"))
    assert free > 0
    assert health_report.ledger_capacity(db) == {
        "ledger_bytes": pages * size, "ledger_free_bytes": free * size}
    db.close()
    assert health_report.ledger_capacity(db) == {"ledger_bytes": None, "ledger_free_bytes": None}


def write_state(tmp_path, value):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "db_maintenance.json").write_text(value if isinstance(value, str) else json.dumps(value))
    return str(data)


def test_maintenance_ages_come_only_from_completed_runs(tmp_path):
    now = 2_000_000_000.0
    data = str(tmp_path / "data")
    assert health_report.db_maintenance_ages(data, now) == {
        "db_maintenance_state": "missing", "db_maintenance_daily_age_s": None,
        "db_maintenance_fts_age_s": None, "db_checkpoint_pending": None}
    write_state(tmp_path, {"daily_at": now - 90_000, "fts_at": now - 10, "checkpoint_pending": True})
    assert health_report.db_maintenance_ages(data, now) == {
        "db_maintenance_state": "present", "db_maintenance_daily_age_s": 90_000,
        "db_maintenance_fts_age_s": 10, "db_checkpoint_pending": True}
    # future, negative, boolean or non-numeric stamps are unknown, never an age
    for daily, fts in ((now + 5, True), (-1, "1"), (float("nan"), None)):
        write_state(tmp_path, {"daily_at": daily, "fts_at": fts})
        out = health_report.db_maintenance_ages(data, now)
        assert out["db_maintenance_daily_age_s"] is None and out["db_maintenance_fts_age_s"] is None
        # the writer drops a satisfied flag: absence is a settled False
        assert out["db_checkpoint_pending"] is False


@pytest.mark.parametrize("flag,expected", [(True, True), (False, False), ("invalid", None),
                                           (1, None), ([], None), (None, None)])
def test_checkpoint_flag_is_bool_or_unknown(tmp_path, flag, expected):
    data = write_state(tmp_path, {"daily_at": 1.0, "checkpoint_pending": flag})
    assert health_report.db_maintenance_ages(data, 2.0)["db_checkpoint_pending"] is expected


def test_a_scan_finishing_past_its_budget_is_unknown(backups, monkeypatch):
    (backups / "hand.db").write_bytes(b"m" * 5)
    clock = iter([0.0, 0.0, 1.0])          # start, the entry check, the end check
    monkeypatch.setattr(maintenance, "monotonic", lambda: next(clock))
    assert maintenance.backup_bytes_by_class() is None


@pytest.mark.parametrize("content", ["{broken", "[]", "x" * (health_report.DB_MAINT_STATE_MAX_BYTES + 1)])
def test_unreadable_maintenance_state_is_unknown(tmp_path, content):
    data = write_state(tmp_path, content)
    out = health_report.db_maintenance_ages(data, 2_000_000_000.0)
    assert out["db_maintenance_state"] == "unreadable" and out["db_maintenance_daily_age_s"] is None


def test_symlinked_maintenance_state_is_not_followed(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"daily_at": time.time()}))
    (data / "db_maintenance.json").symlink_to(target)
    assert health_report.db_maintenance_ages(str(data), time.time())["db_maintenance_state"] == "unreadable"


def test_early_warnings_report_capacity_without_paths(tmp_path, backups, monkeypatch):
    monkeypatch.setattr(health_report, "HOME", str(tmp_path))
    monkeypatch.setattr(health_report, "disk_floor_mb", lambda: 0)
    monkeypatch.setattr(health_report, "prev_health", lambda: {})
    (backups / "preupdate-a.db").write_bytes(b"p" * 4)
    db = sqlite3.connect(tmp_path / "synthetic.db")
    db.execute("CREATE TABLE t (v)")
    write_state(tmp_path, {"daily_at": time.time() - 3 * maintenance.DB_MAINT_INTERVAL_S})
    out = health_report.early_warnings(db, {}, None)
    assert out["backup_bytes_by_class"]["preupdate"] == 4 and out["manual_backup_bytes"] == 0
    assert type(out["ledger_bytes"]) is int and type(out["ledger_free_bytes"]) is int
    assert out["db_maintenance_state"] == "present"
    assert "db_maintenance_overdue" in out["warnings"]
    assert str(tmp_path) not in json.dumps(out)
    # one late run is not overdue; an unreadable record is reported as unknown
    write_state(tmp_path, {"daily_at": time.time() - maintenance.DB_MAINT_INTERVAL_S})
    assert "db_maintenance_overdue" not in health_report.early_warnings(db, {}, None)["warnings"]
    write_state(tmp_path, "{broken")
    assert "db_maintenance_unknown" in health_report.early_warnings(db, {}, None)["warnings"]
    db.close()
