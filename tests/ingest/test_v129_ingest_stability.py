"""v1.0.29 ingest stability: a constraint error quarantines one data/cmd
command, crash sites carry type + file:line only, rotation runs every
tick, and health carries fixed-code early warnings with counts only.
Synthetic files + a temp DB only."""
import json
import os
import sqlite3
import uuid

import pytest

import health_report
import job_ops
import maintenance
import run_check
from ingest_testkit import _ledger


@pytest.fixture
def db(tmp_path):
    store = _ledger(tmp_path)
    try:
        yield store
    finally:
        store.close()


def _command(path):
    path.write_text(json.dumps({
        "version": 1, "cmd": "ops.retry", "command_id": str(uuid.uuid4()),
        "actor": "operator:synthetic", "human_confirmed": True, "job_id": 1}))


def test_constraint_error_quarantines_the_command_and_the_next_one_runs(db, tmp_path, monkeypatch):
    import mcs_requests
    cmd = tmp_path / "cmd"
    cmd.mkdir()
    _command(cmd / "a.json")
    _command(cmd / "b.json")
    calls = []

    def apply(ledger, req):
        calls.append(req["command_id"])
        if len(calls) == 1:
            raise sqlite3.IntegrityError("synthetic constraint")
        return {"outcome": "applied"}

    monkeypatch.setattr(mcs_requests, "apply_command", apply)
    result = {"errors": []}
    job_ops.drain_commands(db, result, cmd_dir=str(cmd))
    assert len(calls) == 2
    assert sorted(p.name for p in cmd.iterdir()) == ["a.json.invalid"]
    assert result["errors"] == ["cmd_invalid: apply_failed IntegrityError"]


def test_crash_site_is_type_and_innermost_basename_line():
    def inner():
        raise RuntimeError("/Users/someone/secret text")
    try:
        inner()
    except RuntimeError as e:
        site = run_check._crash_site(e)
    assert site.startswith("RuntimeError@test_v129_ingest_stability.py:")
    assert site.rsplit(":", 1)[1].isdigit()
    assert "secret" not in site and "/" not in site


def test_rotation_runs_even_when_housekeeping_is_not_due(monkeypatch):
    rotated = []
    monkeypatch.setattr(maintenance, "rotate_log", lambda: rotated.append(1))
    monkeypatch.setattr(maintenance, "backup_overdue", lambda: False)
    monkeypatch.setattr(run_check, "_housekeeping", lambda r: pytest.fail("not due"))
    result = {"errors": []}
    run_check._overdue_housekeeping(result)
    assert rotated == [1] and result["errors"] == []


def test_early_warnings_are_codes_and_numbers_only(db, tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "cmd").mkdir(parents=True, exist_ok=True)
    (data / "cmd" / "x.json.invalid").write_text("{}")
    monkeypatch.setattr(health_report, "HOME", str(tmp_path))
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "ledger-20260101.db").write_bytes(b"x" * 10)
    (backups / "hand-copy.db").write_bytes(b"y" * 7)
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(health_report, "WAL_WARN_BYTES", -1)
    monkeypatch.setattr(health_report, "disk_floor_mb", lambda: 512)
    monkeypatch.setattr(health_report, "prev_health",
                        lambda: {"snapshot_verify_slow_runs": 1})
    main = next(r for r in db.db.execute("PRAGMA database_list") if r[1] == "main")[2]
    if not os.path.exists(main + "-wal"):
        with open(main + "-wal", "ab"):
            pass
    result = {"snapshot_timing": {"copy_s": 1.0, "verify_s": 7.0, "budget_s": 10.0}}
    free = 512 + health_report.ATTACH_DISK_MARGIN_MB + 1
    out = health_report.early_warnings(db.db, result, free)
    assert out["cmd_quarantined"] == 1
    assert isinstance(out["ledger_wal_bytes"], int)
    assert out["snapshot_verify_slow_runs"] == 2
    assert out["manual_backup_bytes"] == 7
    assert out["warnings"] == ["cmd_quarantined", "ledger_wal_large",
                               "snapshot_verify_near_budget", "disk_space_low_soon"]
    assert str(tmp_path) not in json.dumps(out)
    # a fast verify resets the streak; no publish carries it forward
    result["snapshot_timing"]["verify_s"] = 1.0
    assert health_report.early_warnings(db.db, result, None)["snapshot_verify_slow_runs"] == 0
    assert health_report.early_warnings(db.db, {}, None)["snapshot_verify_slow_runs"] == 1
    assert os.path.exists(backups / "hand-copy.db")       # never deleted
