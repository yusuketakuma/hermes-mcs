"""Burnt-out reply jobs stay failed under thread read,
torn cmd files are quarantined, and housekeeping runs past the deadline
once the daily backup is overdue. Synthetic stubs only — no real MCS."""
import collection_stages
import notify_spool
import health_report
import json
import os
import sys
import time

import pytest
from types import SimpleNamespace
from unittest.mock import Mock

import job_ops
import run_check
from common_testkit import ledger_fixture



db = ledger_fixture(name="db")


def _store(db, mid, parent, body_state):
    db.db.execute(
        "INSERT INTO messages(message_id,project_id,parent_id,posted_at,"
        "posted_at_ts,body_text,body_state,content_hash,first_seen) "
        "VALUES(?,1,?,'2026-10-06T09:00',1,'本文',?,?,?)",
        (mid, parent, body_state, f"{mid:064x}", time.time()))
    db.db.commit()


def _adapter(unread, server):
    answers = list(unread)
    calls = []
    return SimpleNamespace(
        calls=calls,
        thread_unread=lambda pid, parent: calls.append("check") or answers.pop(0),
        fetch_thread=lambda pid, parent: [SimpleNamespace(message_id=i) for i in server],
        read_thread=lambda pid, parent: calls.append("clear") or set(server))


def _fail_reply_job(db, mid, parent):
    db.job_add("reply", 1, mid, parent_id=parent)
    db.db.execute("UPDATE fetch_jobs SET state='failed',attempts=9 "
                  "WHERE message_id=?", (mid,))
    db.db.commit()


def test_failed_reply_job_for_unstored_reply_is_not_revived(db):
    _store(db, 11, 10, "full")
    _fail_reply_job(db, 12, 10)
    collection_stages.stage_thread_read(_adapter([True], [11, 12]), db,
                                {"errors": []}, time.monotonic() + 120)
    row = db.db.execute("SELECT state,attempts FROM fetch_jobs").fetchone()
    assert tuple(row) == ("failed", 9)


def test_snippet_reply_with_burnt_out_job_no_longer_blocks_the_clear(db):
    _store(db, 11, 10, "snippet")
    _fail_reply_job(db, 11, 10)
    adapter = _adapter([True, False], [11])
    result = {"errors": []}
    collection_stages.stage_thread_read(adapter, db, result, time.monotonic() + 120)
    assert adapter.calls == ["check", "clear", "check"]
    assert result["threads_marked_read"] == [10]
    row = db.db.execute("SELECT state,attempts FROM fetch_jobs").fetchone()
    assert tuple(row) == ("failed", 9)


def test_torn_cmd_file_is_quarantined_after_an_hour(db, tmp_path):
    cmd = tmp_path / "cmd"
    cmd.mkdir()
    fresh, torn = cmd / "a.json", cmd / "b.json"
    fresh.write_text('{"cmd": ')
    torn.write_text('{"cmd": ')
    old = time.time() - job_ops.CMD_TORN_S - 60
    os.utime(torn, (old, old))
    result = {"errors": []}
    job_ops.drain_commands(db, result, cmd_dir=str(cmd))
    assert sorted(p.name for p in cmd.iterdir()) == ["a.json", "b.json.invalid"]
    assert result["errors"] == ["cmd_invalid: unparsable"]


def test_overdue_backup_makes_housekeeping_run_past_the_deadline(
        tmp_path, monkeypatch, capsys):
    import maintenance
    import notify_cards
    import notify_cmds
    data = tmp_path / "data"
    for name, value in {"HOME": tmp_path, "DB": data / "ledger.db",
                        "ATTACH_DIR": data / "attachments",
                        "LOCKFILE": data / "run.lock",
                        "HEALTH_FILE": data / "health.json"}.items():
        monkeypatch.setattr(
            collection_stages if name in {"ATTACH_DIR", "with_relogin"} else (health_report if name == "HEALTH_FILE" else run_check),
            name, str(value))
    data.mkdir()
    clock = [100.0]
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(run_check, "_config", lambda: {})
    monkeypatch.setattr(run_check, "_semantic_enabled", lambda *a: False)
    monkeypatch.setattr(run_check, "_code_changed", lambda *a: False)
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 10000)
    monkeypatch.setattr(run_check, "MCSAdapter",
                        lambda **kw: SimpleNamespace(set_deadline=lambda d: None))
    monkeypatch.setattr(run_check, "_stage_fetch", lambda *a: clock.__setitem__(
        0, clock[0] + run_check.RUN_DEADLINE_S + 1))
    later, housekeeping = Mock(), Mock()
    for name in ("_run_jobs", "stage_derive", "_deliver", "with_relogin",
                 "_run_semantic", "_run_metadata_shadow"):
        monkeypatch.setattr(
            collection_stages if name in {"ATTACH_DIR", "with_relogin"} else (health_report if name == "HEALTH_FILE" else run_check),
            name, later)
    monkeypatch.setattr(run_check, "_housekeeping", housekeeping)
    monkeypatch.setattr(maintenance, "backup_overdue", lambda: True)
    monkeypatch.setattr(notify_cmds, "drain_int_commands", lambda *a, **k: None)
    for name in ("publish_flags", "clear_snapshot_dirty"):
        monkeypatch.setattr((notify_spool if name in {'publish_flags'} else notify_cards), name, lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "publish_snapshot", lambda *a, **k: True)
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify"])
    assert run_check._main() == 0
    later.assert_not_called()
    housekeeping.assert_called_once()
    result = json.loads(capsys.readouterr().out)
    assert "housekeeping" not in result["deferred_stages"]


def test_command_whose_apply_fails_deterministically_is_quarantined(db, tmp_path, monkeypatch):
    """A handler data error would re-fire on every drain and fail every
    tick: the file moves aside and the tick goes on. A storage error (and
    a not-ready schema, see test_mcs_features) still propagates so the
    command is never consumed."""
    import sqlite3
    import uuid
    import mcs_requests
    cmd = tmp_path / "cmd"
    cmd.mkdir()
    (cmd / "a.json").write_text(json.dumps({
        "version": 1, "cmd": "ops.retry", "command_id": str(uuid.uuid4()),
        "actor": "operator:synthetic", "human_confirmed": True, "job_id": 1}))

    def corrupt(*_args, **_kw):
        raise ValueError("synthetic corrupt receipt")
    monkeypatch.setattr(mcs_requests, "apply_command", corrupt)
    result = {"errors": []}
    job_ops.drain_commands(db, result, cmd_dir=str(cmd))
    assert sorted(p.name for p in cmd.iterdir()) == ["a.json.invalid"]
    assert result["errors"] == ["cmd_invalid: apply_failed ValueError"]

    (cmd / "b.json").write_text(json.dumps({
        "version": 1, "cmd": "ops.retry", "command_id": str(uuid.uuid4()),
        "actor": "operator:synthetic", "human_confirmed": True, "job_id": 1}))

    def locked(*_args, **_kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(mcs_requests, "apply_command", locked)
    with pytest.raises(sqlite3.OperationalError):
        job_ops.drain_commands(db, {"errors": []}, cmd_dir=str(cmd))
    assert (cmd / "b.json").exists()
