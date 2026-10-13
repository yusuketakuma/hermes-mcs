"""The health read of the local daily backup: newest file by mtime, its
.ok marker, and nothing else — synthetic backup directories only."""
import os
import sqlite3
import time

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
    real = os.scandir

    class Entry:
        def __init__(self, e):
            self._e, self.name, self.path = e, e.name, e.path

        def stat(self, **kw):
            if self.path == str(gone):
                raise FileNotFoundError(self.path)
            return self._e.stat(**kw)

    class Scan:
        def __init__(self, path):
            self._it = real(path)

        def __enter__(self):
            return (Entry(e) for e in self._it.__enter__())

        def __exit__(self, *exc):
            return self._it.__exit__(*exc)
    monkeypatch.setattr(maintenance.os, "scandir", Scan)
    status = _status()
    assert status["state"] == "verified" and status["newest_age_s"] == pytest.approx(86400)


def test_unreadable_and_impossible_states_carry_no_path_or_error(backups, monkeypatch):
    path = _backup(backups, "20260101", 60)
    os.utime(path, (NOW + 600, NOW + 600))               # mtime ahead of the clock
    assert _status() == {"state": "unknown", "newest_age_s": None, "observed_at": NOW}
    def denied(*_a, **_k):
        raise PermissionError(str(backups))
    monkeypatch.setattr(maintenance.os, "scandir", denied)
    status = _status()
    assert status == {"state": "unreadable", "newest_age_s": None, "observed_at": NOW}
    assert str(backups) not in repr(status)


@pytest.mark.parametrize("kind", ["fifo", "huge", "symlink", "directory"])
def test_a_hostile_marker_is_unverified_without_blocking(backups, tmp_path, kind):
    path = _backup(backups, "20260101", 60, marker=False)
    good = f"{path.stat().st_size} {path.stat().st_mtime_ns}"
    marker = backups / "ledger-20260101.db.ok"
    if kind == "fifo":
        os.mkfifo(marker)                                 # a blocking open would hang here
    elif kind == "huge":
        marker.write_text(good + " " * (maintenance.OK_MARKER_MAX + 10))
    elif kind == "symlink":
        target = tmp_path / "elsewhere"
        target.write_text(good)
        marker.symlink_to(target)
    else:
        marker.mkdir()
    assert _status()["state"] == "unverified"


def test_a_symlinked_backup_is_not_a_backup(backups, tmp_path):
    real = tmp_path / "outside.db"
    real.write_bytes(b"synthetic")
    (backups / "ledger-20260105.db").symlink_to(real)
    assert _status()["state"] == "missing"


def test_the_scan_is_bounded(backups, monkeypatch):
    _backup(backups, "20260101", 60)
    monkeypatch.setattr(maintenance, "LOCAL_STATUS_SCAN_MAX", 1)  # .db + .ok exceed it
    assert _status() == {"state": "unknown", "newest_age_s": None, "observed_at": NOW}
    monkeypatch.setattr(maintenance, "LOCAL_STATUS_SCAN_MAX", 256)
    clock = iter([0.0])
    monkeypatch.setattr(maintenance, "monotonic", lambda: next(clock, 10.0))
    assert _status()["state"] == "unknown"


def test_a_deadline_abort_then_the_next_run_publishes_a_verified_backup(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    src = tmp_path / "ledger.db"
    with sqlite3.connect(src) as con:
        con.execute("CREATE TABLE t(x)")
        con.executemany("INSERT INTO t VALUES(?)", [("x" * 2000,)] * 200)
    con.close()
    monkeypatch.setattr(maintenance, "BACKUP_STEP_PAGES", 1)
    monkeypatch.setattr(maintenance, "backup_time_needed_s", lambda _p: 0)
    monkeypatch.setattr(maintenance, "_valid", lambda p, d: os.path.exists(p))
    clock = iter([0.0])
    monkeypatch.setattr(maintenance, "monotonic", lambda: next(clock, 100.0))
    with pytest.raises(maintenance.MaintenanceError, match="backup_deadline_exceeded"):
        maintenance.daily_backup(str(src), deadline=50.0)
    assert maintenance.local_backup_status(now=time.time())["state"] == "missing"
    monkeypatch.setattr(maintenance, "monotonic", time.monotonic)
    assert maintenance.daily_backup(str(src)) is None
    status = maintenance.local_backup_status(now=time.time())
    assert status["state"] == "verified" and 0 <= status["newest_age_s"] < 60
