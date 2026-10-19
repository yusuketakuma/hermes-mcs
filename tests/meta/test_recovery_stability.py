"""Interrupted recovery stays retryable and classifies only current locked state."""
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import ledger
import notify_reconcile
import notify_spool
from ops_testkit import _git
from test_mcs_recover import (
    _applying, _consent, _mk_db, _rollback_state, gateway_restarts, rec,
)

__all__ = ["gateway_restarts", "rec"]


def test_failed_drainer_recovery_preserves_journal_and_retries_without_bounce(rec, monkeypatch):
    state = _applying("p" * 40)
    state["applying"]["command_id"] = "synthetic-command"
    Path(rec.STATE_PATH).write_text(json.dumps(state))
    Path(rec.MARKER_PATH).write_text("synthetic")
    monkeypatch.setattr(rec, "_head", lambda: "p" * 40)
    monkeypatch.setattr(rec, "_clean", lambda: True)
    calls = []

    def restart(bounce=True):
        calls.append(bounce)
        return ["ai.mcs.extract-drainer"] if len(calls) == 1 else []

    monkeypatch.setattr(rec, "_restart_drainers", restart)
    assert rec.recover() == 1
    held = json.loads(Path(rec.STATE_PATH).read_text())
    assert held == state               # no completed receipt or attempt before verified restart
    assert json.loads(Path(rec.REPORT_PATH).read_text())["result"] == "recovery_incomplete"
    assert rec.recover(if_stale=True) == 0
    after = json.loads(Path(rec.STATE_PATH).read_text())
    assert after["applying"] is None and after["stages"] == []
    assert after["executed"]["synthetic-command"]["result"] == "interrupted_recovered"
    assert calls == [True, False]


def test_watchdog_rechecks_freshness_of_state_loaded_under_lock(rec, monkeypatch):
    snapshots = iter([_applying("p" * 40, ago=4000), _applying("p" * 40, ago=0)])
    monkeypatch.setattr(rec, "_load_state", lambda: next(snapshots))
    monkeypatch.setattr(rec, "_clean_stale_git_locks", lambda: (_ for _ in ()).throw(
        AssertionError("fresh journal must remain untouched")))
    assert rec.recover(if_stale=True) == 0


def test_approved_restore_swap_failure_keeps_journal_and_sender_hold(rec, tmp_path, monkeypatch):
    live = tmp_path / "data/ledger.db"
    backup = tmp_path / "data/backup.db"
    _mk_db(live, 8)
    _mk_db(backup, 7)
    _rollback_state(rec, tmp_path, str(backup))
    monkeypatch.setattr(rec, "LEDGER", str(live))
    _consent(rec, live, backup)
    before = live.read_bytes()
    Path(rec.MARKER_PATH).write_text("synthetic")
    restarts = []
    monkeypatch.setattr(rec, "_restart_drainers", lambda **kw: restarts.append(kw) or [])

    def fail_swap(path, expected_sha, before_replace):
        before_replace()                 # exactly the window after 'restored' is durable
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(rec, "_replace_database", fail_swap)
    assert rec.recover() == 1
    assert restarts == [] and Path(rec.MARKER_PATH).exists()
    state = json.loads(Path(rec.STATE_PATH).read_text())
    assert state["restore_consent"] and state["applying"]["rollback"]
    assert json.loads(Path(rec.DATA, "restore_pending.json").read_text())["phase"] == "awaiting_consent"
    assert json.loads(Path(rec.REPORT_PATH).read_text())["result"] == "restore_consent_blocked"
    assert live.read_bytes() == before


def _pin_current_writer(rec, tmp_path):
    """Pin the actual writer bytes in the clean synthetic recovery checkout."""
    canonical = tmp_path / "repo/mcs/core/ledger.py"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(Path(ledger.__file__).read_bytes())
    _git(canonical.parents[2], "add", "mcs/core/ledger.py")
    _git(canonical.parents[2], "commit", "-qm", "current synthetic writer checkout")
    state = rec._load_state()
    state["applying"]["sha"] = _git(canonical.parents[2], "rev-parse", "HEAD").stdout.strip()
    rec._save_state(state)


@pytest.mark.parametrize("runtime_mode", ["hermes", "standalone"])
def test_failed_schema_rollback_hold_survives_independent_recovery(
        rec, tmp_path, monkeypatch, runtime_mode):
    live = tmp_path / "data/ledger.db"
    backup = tmp_path / "data/backup.db"   # unreadable (missing) at first
    _mk_db(live, 8)
    _rollback_state(rec, tmp_path, str(backup))
    _pin_current_writer(rec, tmp_path)
    state = rec._load_state()
    state["applying"]["restore_incomplete"] = True   # set by apply's bail
    Path(rec.STATE_PATH).write_text(json.dumps(state))
    (tmp_path / "config.json").write_text(json.dumps({"runtime_mode": runtime_mode}))
    monkeypatch.setattr(rec, "LEDGER", str(live))
    monkeypatch.setattr(rec, "_reconcile_membership", lambda snapshot: [])
    monkeypatch.setattr(rec, "_standalone_drainer_problems", lambda: [])
    calls = []
    restart = rec._restart_drainers
    monkeypatch.setattr(rec, "_restart_drainers",
                        lambda bounce=True: calls.append(bounce) or restart(bounce))
    Path(rec.MARKER_PATH).write_text("synthetic")
    before = live.read_bytes()
    assert rec.recover() == 1
    assert calls == [] and Path(rec.MARKER_PATH).exists()
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == "escalate" and report["writers_stopped"] is True
    assert "backup_unreadable" in report["detail"]
    assert json.loads(Path(rec.STATE_PATH).read_text())["applying"]["restore_incomplete"]
    assert live.read_bytes() == before
    assert not Path(rec.DATA, "restore_pending.json").exists()

    # An approved old-schema copy is exact, but cannot resume this current writer.
    _mk_db(backup, 7)
    report = _consent(rec, live, backup)
    assert rec.recover() == 1
    assert calls == [] and Path(rec.MARKER_PATH).exists()
    assert rec._db_version(str(live)) == 7
    assert live.read_bytes() == backup.read_bytes()
    restored = Path(rec.DATA, "restore_pending.json")
    witness = json.loads(restored.read_bytes())
    assert witness["phase"] == "restored" and witness["by"] == "mcs_recover"
    assert witness["backup_path"] == str(backup) and witness["report_id"] == report["report_id"]
    held = rec._load_state()
    assert held["applying"]["rollback"] and held["applying"]["restore_incomplete"]
    assert held["restore_consent"]["report_id"] == report["report_id"]
    assert held["executed"]["cid-rb"]["result"] == "escalated"  # prior missing-backup outcome
    assert json.loads(Path(rec.REPORT_PATH).read_bytes())["result"] == "restore_reconcile_pending"
    assert not Path(rec.DATA, notify_spool.RESTORE_RECEIPT).exists()
    stable = {path: path.read_bytes() for path in (
        live, backup, restored, Path(rec.STATE_PATH), Path(rec.DATA, "restore_report.json"))}
    assert rec.recover() == 1
    assert {path: path.read_bytes() for path in stable} == stable
    assert calls == [] and Path(rec.MARKER_PATH).exists()


@pytest.mark.parametrize("runtime_mode", ["hermes", "standalone"])
def test_compatible_schema_restore_reconciles_and_resumes(
        rec, tmp_path, monkeypatch, gateway_restarts, runtime_mode):
    """Restore a real current schema, resume writers, then settle its delivery witness."""
    live, backup = tmp_path / "data/ledger.db", tmp_path / "data/backup.db"
    source = tmp_path / "synthetic-source.db"
    writer = ledger.Ledger(str(source))
    try:
        event_id = writer.outbox_add_tx("new_messages", None, {"message_ids": [1]}, route="text")
        writer.db.commit()
    finally:
        writer.close()
    original = source.read_bytes()
    shutil.copy2(source, live)
    # The live DB represents a newer update. Never open this future schema with Ledger.
    with closing(sqlite3.connect(live)) as db:
        db.execute(f"PRAGMA user_version={ledger.SCHEMA_VERSION + 1}")
    _rollback_state(rec, tmp_path, str(backup))
    _pin_current_writer(rec, tmp_path)
    state = rec._load_state()
    state["applying"]["restore_incomplete"] = True
    state["applying"]["plugin_changed"] = True
    rec._save_state(state)
    (tmp_path / "config.json").write_text(json.dumps({"runtime_mode": runtime_mode}))
    monkeypatch.setattr(rec, "LEDGER", str(live))
    monkeypatch.setattr(rec, "_reconcile_membership", lambda snapshot: [])
    monkeypatch.setattr(rec, "_standalone_drainer_problems", lambda: [])
    calls = []
    restart = rec._restart_drainers
    monkeypatch.setattr(rec, "_restart_drainers",
                        lambda bounce=True: calls.append(bounce) or restart(bounce))
    Path(rec.MARKER_PATH).write_text("synthetic")
    damaged = live.read_bytes()
    assert rec.recover() == 1
    assert calls == [] and Path(rec.MARKER_PATH).exists() and live.read_bytes() == damaged
    shutil.copy2(source, backup)
    consent = _consent(rec, live, backup)
    assert consent["backup_schema"] == ledger.SCHEMA_VERSION
    assert rec.recover() == 0
    assert calls == [True] and not Path(rec.MARKER_PATH).exists()
    assert rec._db_version(str(live)) == ledger.SCHEMA_VERSION
    assert live.read_bytes() == backup.read_bytes() == original
    assert json.loads(Path(rec.STATE_PATH).read_text())["applying"] is None
    assert rec._load_state()["executed"]["cid-rb"]["result"] == "rolled_back"
    assert len(gateway_restarts) == (1 if runtime_mode == "hermes" else 0)
    witness = notify_spool.restore_pending(str(live.parent))
    assert witness is not None
    assert witness["phase"] == "restored" and witness["report_id"] == consent["report_id"]
    assert not Path(rec.DATA, notify_spool.RESTORE_RECEIPT).exists()
    # Model the resumed writer's actual reconcile, not a service/reconcile success stub.
    writer = ledger.Ledger(str(live))
    try:
        receipt = notify_reconcile.reconcile_after_restore(writer, {})
        assert receipt["journal_incomplete"] is False
        assert receipt["text_held"] == [{"event_id": event_id, "kind": "new_messages",
                                        "reason": "restore_text_unverified"}]
        row = writer.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (event_id,)).fetchone()
        assert row["state"] == "failed" and row["next_try"] is None
        assert json.loads(row["progress"])["hold_reason"] == "restore_text_unverified"
        assert notify_spool.restore_pending(str(live.parent)) is None
        assert json.loads(Path(rec.DATA, notify_spool.RESTORE_RECEIPT).read_bytes()) == receipt
        assert writer.db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
    finally:
        writer.close()
    assert source.read_bytes() == backup.read_bytes() == original


def test_frozen_gateway_program_carries_the_atomic_json_writer(rec, tmp_path):
    namespace = {"__name__": "synthetic_frozen"}
    exec(compile(rec._GATEWAY_RESTART_PROGRAM.rsplit("sys.exit(", 1)[0],
                 "<frozen>", "exec"), namespace)
    out = tmp_path / "frozen-data"
    namespace["_gateway_restart_report"](str(out), "requested", error="synthetic")
    written = out / "gateway_restart.json"
    assert json.loads(written.read_text())["status"] == "requested"
    assert written.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in out.iterdir()] == ["gateway_restart.json"]


def test_atomic_json_failure_keeps_target_and_leaves_no_temp(rec, monkeypatch):
    Path(rec.REPORT_PATH).write_text('{"result": "synthetic-previous"}')

    def fail(src, dst):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(rec.os, "replace", fail)
    rec._report("escalate", "synthetic")         # best-effort: swallowed
    with pytest.raises(OSError):
        rec._save_state({"v": 1})                # state writes raise
    assert json.loads(Path(rec.REPORT_PATH).read_text())["result"] == "synthetic-previous"
    assert not [p for p in Path(rec.DATA).iterdir() if p.name.endswith(".tmp")]
