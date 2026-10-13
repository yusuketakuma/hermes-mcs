"""The health read of the local daily backup: newest file by mtime, its
.ok marker, and nothing else — synthetic backup directories only."""
import os
import sqlite3

import pytest

import maintenance

NOW = 2_000_000_000.0


@pytest.fixture
def backups(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    d.mkdir()
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(d))
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("status opened a DB"))
    return d


def _backup(d, day, age_s, *, marker=True):
    path = d / f"ledger-{day}.db"
    path.write_bytes(b"synthetic backup bytes")
    os.utime(path, (NOW - age_s, NOW - age_s))
    if marker:
        maintenance._write_ok_marker(str(path))
    return path


def _status():
    return maintenance.local_backup_status(now=NOW)


def test_no_backup_directory_or_file_is_missing(backups, monkeypatch):
    assert _status() == {"state": "missing", "newest_age_s": None, "observed_at": NOW}
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups / "absent"))
    assert _status()["state"] == "missing"


def test_newest_verified_backup_reports_its_age(backups):
    _backup(backups, "20260101", 3 * 86400)
    _backup(backups, "20260103", 3600)
    assert _status() == {"state": "verified", "newest_age_s": pytest.approx(3600), "observed_at": NOW}


def test_a_backup_changed_after_its_marker_is_unverified(backups):
    path = _backup(backups, "20260103", 3600)
    path.write_bytes(b"synthetic bytes rewritten")
    os.utime(path, (NOW - 60, NOW - 60))
    assert _status()["state"] == "unverified"
    _backup(backups, "20260104", 30, marker=False)       # no marker at all
    assert _status()["state"] == "unverified"


def test_a_late_backup_keeps_the_old_verified_one_and_its_true_age(backups):
    # deadline-skipped or failed attempts leave only an interrupted temp file
    _backup(backups, "20260101", 3 * 86400)
    (backups / "ledger-20260104-abc.db.tmp").write_bytes(b"partial")
    status = _status()
    assert status["state"] == "verified" and status["newest_age_s"] == pytest.approx(3 * 86400)


def test_a_failed_daily_attempt_keeps_the_previous_verified_backup(backups, monkeypatch, tmp_path):
    _backup(backups, "20260101", 86400)
    db = tmp_path / "ledger.db"
    db.write_bytes(b"synthetic")
    monkeypatch.setattr(maintenance, "_valid", lambda *_: False)
    def broken(_db, tmp, _dest, deadline=None):
        os.unlink(tmp)
        raise maintenance.MaintenanceError("backup_verify_failed")
    monkeypatch.setattr(maintenance, "_publish_backup", broken)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.daily_backup(str(db))
    assert _status()["state"] == "verified"


def test_a_file_rotated_mid_scan_is_skipped(backups, monkeypatch):
    _backup(backups, "20260101", 86400)
    gone = _backup(backups, "20260102", 60)
    real_stat = os.stat
    def stat(path, *a, **k):
        if os.fspath(path) == str(gone):
            raise FileNotFoundError(path)
        return real_stat(path, *a, **k)
    monkeypatch.setattr(maintenance.os, "stat", stat)
    status = _status()
    assert status["state"] == "verified" and status["newest_age_s"] == pytest.approx(86400)


def test_unreadable_and_impossible_states_carry_no_path_or_error(backups, monkeypatch):
    path = _backup(backups, "20260101", 60)
    os.utime(path, (NOW + 600, NOW + 600))               # mtime ahead of the clock
    assert _status() == {"state": "unknown", "newest_age_s": None, "observed_at": NOW}
    def denied(*_a, **_k):
        raise PermissionError(str(backups))
    monkeypatch.setattr(maintenance.os, "stat", denied)
    status = _status()
    assert status == {"state": "unreadable", "newest_age_s": None, "observed_at": NOW}
    assert str(backups) not in repr(status)
