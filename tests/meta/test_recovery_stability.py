"""Interrupted recovery stays retryable and classifies only current locked state."""
import json
from pathlib import Path

import pytest

from test_mcs_recover import _applying, _consent, _mk_db, _rollback_state, rec

__all__ = ["rec"]


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


@pytest.mark.parametrize("runtime_mode", ["hermes", "standalone"])
def test_failed_schema_rollback_hold_survives_independent_recovery(
        rec, tmp_path, monkeypatch, runtime_mode):
    live = tmp_path / "data/ledger.db"
    backup = tmp_path / "data/backup.db"   # unreadable (missing) at first
    _mk_db(live, 8)
    state = _rollback_state(rec, tmp_path, str(backup))
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

    # a usable, approved backup completes the restore and ends the hold
    _mk_db(backup, 7)
    _consent(rec, live, backup)
    assert rec.recover() == 0
    assert calls == [True] and not Path(rec.MARKER_PATH).exists()
    assert rec._db_version(str(live)) == 7
    assert json.loads(Path(rec.STATE_PATH).read_text())["applying"] is None
