"""A failed tick still keeps the local daily backup, and an unread walk
cut by the deadline records the patients it never fetched. Synthetic
ledger and adapter stubs only; no MCS request, Keychain or send."""
import os
import sys
import time
from types import SimpleNamespace

import pytest

import job_ops
import ledger
import maintenance
import mcs_adapter
import notify_flush
import run_check
from ingest_testkit import _message, _unread_patient


def _tick_world(tmp_path, monkeypatch, failure, *, overdue):
    """A full tick whose unread listing fails with ``failure``."""
    calls = []

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            calls.append(path)
            raise failure

        def _request(self, *args, **kwargs):
            pytest.fail("external request forbidden")

    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    config = tmp_path / "config.json"
    config.write_text('{"deep_history":false}', encoding="utf-8")
    for name, value in {
        "HOME": tmp_path, "DB": data / "ledger.db",
        "ATTACH_DIR": data / "attachments", "LOCKFILE": data / "run.lock",
        "CONF_PATH": config, "CACHE": tmp_path / "absent-token.json",
        "HEALTH_FILE": data / "health.json",
    }.items():
        monkeypatch.setattr(run_check, name, str(value))
    for name, value in {
        "BACKUP_DIR": data / "backups", "SNAPSHOT_DIR": data / "snapshots",
        "LOGFILE": data / "run.log",
    }.items():
        monkeypatch.setattr(maintenance, name, str(value))
    if overdue:
        old = data / "backups" / "ledger-20000101.db"
        old.write_bytes(b"synthetic old backup")
        stamp = time.time() - maintenance.BACKUP_OVERDUE_S - 3600
        os.utime(old, (stamp, stamp))
    backups = []
    monkeypatch.setattr(maintenance, "daily_backup",
                        lambda db: backups.append(db) or "ok")
    drain = job_ops.drain_commands
    monkeypatch.setattr(job_ops, "drain_commands", lambda db, result:
                        drain(db, result, str(data / "cmd")))
    monkeypatch.setattr(run_check, "MCSAdapter", Adapter)
    monkeypatch.setattr(run_check, "_attempt_relogin",
                        lambda *a, **k: "keychain_locked")
    monkeypatch.setattr(notify_flush, "flush", lambda *a, **k:
                        pytest.fail("notification forbidden"))
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify", "--no-backfill"])
    return calls, backups


FAILURES = {
    "mcs_error": (mcs_adapter.MCSError("network_error", retryable=True), 1),
    "session": (mcs_adapter.SessionExpired("session_expired"), 2),
}


@pytest.mark.parametrize("kind", sorted(FAILURES))
def test_overdue_backup_still_runs_when_mcs_fails(tmp_path, monkeypatch, capsys, kind):
    failure, code = FAILURES[kind]
    calls, backups = _tick_world(tmp_path, monkeypatch, failure, overdue=True)
    assert run_check.main() == code
    capsys.readouterr()
    assert backups == [run_check.DB]                  # the local backup is kept
    assert calls == ["/projects"]                     # no extra MCS request


@pytest.mark.parametrize("kind", sorted(FAILURES))
def test_failed_tick_without_an_overdue_backup_adds_nothing(tmp_path, monkeypatch,
                                                            capsys, kind):
    failure, code = FAILURES[kind]
    calls, backups = _tick_world(tmp_path, monkeypatch, failure, overdue=False)
    assert run_check.main() == code                   # exit codes unchanged
    capsys.readouterr()
    assert backups == []          # no backup yet is not overdue (first install)
    assert calls == ["/projects"]


def test_backup_failure_after_mcs_failure_keeps_the_run_result(tmp_path, monkeypatch,
                                                              capsys):
    failure, code = FAILURES["mcs_error"]
    _tick_world(tmp_path, monkeypatch, failure, overdue=True)

    def broken(db):
        raise maintenance.MaintenanceError("backup_verify_failed")
    monkeypatch.setattr(maintenance, "daily_backup", broken)
    assert run_check.main() == code
    out = capsys.readouterr().out
    assert "backup: backup_verify_failed" in out and "network_error" in out


# ---------- unread walk cut by the deadline ----------

def test_patients_never_fetched_before_the_deadline_are_incomplete(tmp_path, monkeypatch):
    db = ledger.Ledger(str(tmp_path / "ledger.db"))
    clock = {"now": 0.0}
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock["now"])
    fetched = []

    def fetch(pid, ts):
        fetched.append(pid)
        clock["now"] = 10.0                          # the deadline passes here
        return mcs_adapter.MessageBatch(messages=[], reached=True)
    adapter = SimpleNamespace(
        list_unread=lambda: mcs_adapter.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1), _unread_patient(2),
                                     _unread_patient(3)]),
        fetch_unread_messages=fetch,
        fetch_unread_replies=lambda *_: mcs_adapter.ReplyBatch([], []),
        mark_patient_read=lambda *a, **k: pytest.fail("no ack"))
    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": []}
    run_check.stage_unread(adapter, db, SimpleNamespace(mark_read=False), result,
                           5.0, db.begin_run(None))
    assert fetched == [1]
    assert result["incomplete"] == [2, 3]
    assert "deadline_exceeded" in result["errors"]
    assert run_check._collection(result)["collection"] == "incomplete"
    # nothing is recorded about them: never fetched is not 'no messages'
    assert db.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    db.close()


def test_an_unread_walk_within_the_deadline_stays_complete(tmp_path):
    db = ledger.Ledger(str(tmp_path / "ledger.db"))
    adapter = SimpleNamespace(
        list_unread=lambda: mcs_adapter.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1)]),
        fetch_unread_messages=lambda *_: mcs_adapter.MessageBatch(
            messages=[_message(unread=True)], reached=True),
        fetch_unread_replies=lambda *_: mcs_adapter.ReplyBatch([], []),
        mark_patient_read=lambda *a, **k: None)
    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": []}
    run_check.stage_unread(adapter, db, SimpleNamespace(mark_read=False), result,
                           time.monotonic() + 30, db.begin_run(None))
    assert result["incomplete"] == [] and result["errors"] == []
    assert run_check._collection(result)["collection"] == "ok"
    db.close()


# ---------- the failure epilogue itself cannot write the run row ----------

@pytest.mark.parametrize("kind", sorted(FAILURES))
def test_run_row_write_failure_still_writes_health_and_keeps_exit_code(
        tmp_path, monkeypatch, capsys, kind):
    import sqlite3
    failure, code = FAILURES[kind]
    _tick_world(tmp_path, monkeypatch, failure, overdue=False)
    real = ledger.Ledger.finish_run

    def locked(self, run_id, status, *a, **k):
        if status in ("failed", "session_expired"):
            raise sqlite3.OperationalError("database is locked")
        return real(self, run_id, status, *a, **k)
    monkeypatch.setattr(ledger.Ledger, "finish_run", locked)
    assert run_check.main() == code
    out = capsys.readouterr().out
    assert "run_record_failed" in out
    assert os.path.exists(run_check.HEALTH_FILE)


@pytest.mark.parametrize("held", [True, False])
def test_run_lock_lost_keeps_its_write_contract_when_the_record_fails(
        tmp_path, monkeypatch, capsys, held):
    import sqlite3
    _tick_world(tmp_path, monkeypatch, AssertionError("unused"), overdue=True)

    def lose_lock(*a, **k):
        raise run_check.RunLockLost(held=held)
    monkeypatch.setattr(run_check, "_stage_fetch", lose_lock)
    writes = []
    real = ledger.Ledger.finish_run

    def record(self, run_id, status, *a, **k):
        writes.append(status)
        if status == "partial":
            raise sqlite3.OperationalError("database is locked")
        return real(self, run_id, status, *a, **k)
    monkeypatch.setattr(ledger.Ledger, "finish_run", record)
    backups = []
    monkeypatch.setattr(maintenance, "daily_backup", lambda db: backups.append(db) or "ok")
    assert run_check.main() == 1
    out = capsys.readouterr().out
    assert backups == []                               # never housekeeping on lock loss
    if held:
        assert writes == ["partial"] and "run_record_failed" in out
        assert os.path.exists(run_check.HEALTH_FILE)    # health only
    else:
        assert writes == [] and not os.path.exists(run_check.HEALTH_FILE)
