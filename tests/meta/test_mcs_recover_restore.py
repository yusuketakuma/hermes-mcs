"""Synthetic mcs recover restore contracts; shared helpers preserve isolated runtime state."""
import update_apply
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_restore
import restore_evidence
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from ops_testkit import _git, _load
from mcs_recover_testkit import _MIGRATION as _MIGRATION, _applying as _applying, _commit_recovery_schema as _commit_recovery_schema, _consent as _consent, _journal_kept as _journal_kept, _make_repo as _make_repo, _mk_db as _mk_db, _rollback_journal as _rollback_journal, _rollback_state as _rollback_state, gateway_restarts as gateway_restarts, rec as rec


def test_restore_db_rejects_unreadable_backup(rec, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger.db"
    _mk_db(live, 7)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    err = rec.mcs_recovery_database._restore_db(str(tmp_path / "data" / "missing.db"))
    assert err and "backup_unreadable" in err
    # live DB untouched
    assert rec.mcs_recovery_database._db_version(str(live)) == 7


@pytest.mark.parametrize("payload", ["{broken", "[]", '{"phase":"invalid"}'])
def test_restore_preserves_unreadable_marker(rec, tmp_path, payload):
    marker = tmp_path / "data" / "restore_pending.json"
    marker.write_text(payload)
    assert rec.mcs_recovery_database._restore_db(str(tmp_path / "missing.db")) == "restore_marker_unreadable"
    assert marker.read_text() == payload


def test_restore_db_rejects_unreadable_live(rec, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger.db"          # absent
    back = tmp_path / "data" / "backup.db"
    _mk_db(back, 7)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    assert rec.mcs_recovery_database._restore_db(str(back)) == "live_db_unreadable"


def test_restore_db_same_version_is_verified_noop(rec, tmp_path,
                                                  monkeypatch):
    """Live already at the backup schema — nothing copied, the live
    rows stay exactly as they were."""
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 7)
    _mk_db(back, 7)
    import sqlite3
    con = sqlite3.connect(str(live))
    con.execute("INSERT INTO t VALUES('sentinel')")
    con.commit()
    con.close()
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    assert rec.mcs_recovery_database._restore_db(str(back)) is None
    con = sqlite3.connect("file:" + str(live) + "?mode=ro", uri=True)
    assert con.execute("SELECT x FROM t").fetchone()[0] == "sentinel"
    con.close()


def test_restore_db_reports_copy_failure(rec, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    _consent(rec, live, back)
    def fail_copy(*args):
        raise OSError("synthetic copy failure")
    monkeypatch.setattr(rec.shutil, "copyfileobj", fail_copy)
    err = rec.mcs_recovery_database._restore_db(str(back))
    assert err and err.startswith("restore_failed:")
    assert rec.mcs_recovery_database._db_version(str(live)) == 8    # live DB untouched


def test_restore_db_holds_without_consent(rec, tmp_path, monkeypatch):
    """No bound receipt => no swap: the marker holds senders in
    awaiting_consent phase, the loss report is durable, the live DB is
    byte-identical, and the error token names the pending consent."""
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    before = (tmp_path / "data" / "ledger.db").read_bytes()
    err = rec.mcs_recovery_database._restore_db(str(back))
    assert err and err.startswith("restore_consent_pending:")
    assert (tmp_path / "data" / "ledger.db").read_bytes() == before
    marker = json.loads((tmp_path / "data"
                         / "restore_pending.json").read_text())
    assert marker["phase"] == "awaiting_consent"
    report = json.loads(
        (tmp_path / "data" / "restore_report.json").read_text())
    assert marker["report_id"] == report["report_id"]
    assert report["report_id"] == err.split(":", 1)[1]
    assert report["backup_schema"] == 7


@pytest.mark.parametrize("separator", ["#", "?", "%23"])
def test_restore_uses_exact_backup_path_with_uri_delimiters(
        rec, tmp_path, monkeypatch, separator):
    live = tmp_path / "data" / "ledger.db"
    prefix = tmp_path / "data" / "backup"
    back = Path(str(prefix) + separator + "snapshot.db")
    _mk_db(live, 8)
    decoy = Path(str(prefix) + "#snapshot.db") if separator == "%23" else prefix
    _mk_db(decoy, 8)
    _mk_db(back, 7)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    before = live.read_bytes()

    err = rec.mcs_recovery_database._restore_db(str(back))
    assert err and err.startswith("restore_consent_pending:")
    assert rec.mcs_recovery_database._db_version(str(back)) == 7
    assert live.read_bytes() == before
    report = json.loads((tmp_path / "data" / "restore_report.json").read_text())
    assert report["backup_schema"] == 7
    _consent(rec, live, back)
    _rollback_journal(rec, back)
    assert rec.mcs_recovery_database._restore_db(str(back)) == "restore_reconcile_pending"
    _journal_kept(rec)
    assert rec.mcs_recovery_database._db_version(str(live)) == 7


def test_restore_measures_and_approves_exact_live_uri_path(
        rec, tmp_path, monkeypatch):
    import sqlite3
    live = tmp_path / "data" / "ledger#?%23.db"
    back = tmp_path / "data" / "backup.db"
    decoy = tmp_path / "data" / "ledger"
    _mk_db(live, 8)
    _mk_db(decoy, 8)
    _mk_db(back, 7)
    with sqlite3.connect(live) as con:
        con.execute("CREATE TABLE messages(message_id INTEGER, posted_at_ts REAL)")
        con.executemany("INSERT INTO messages VALUES(?, ?)", [(1, 10), (2, 20)])
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    before = live.read_bytes()
    decoy_before = decoy.read_bytes()

    err = rec.mcs_recovery_database._restore_db(str(back))
    assert err and err.startswith("restore_consent_pending:")
    report = json.loads((tmp_path / "data" / "restore_report.json").read_text())
    assert report["stored_since_backup"]["messages"] == 2
    assert live.read_bytes() == before
    _consent(rec, live, back)
    _rollback_journal(rec, back)
    assert rec.mcs_recovery_database._restore_db(str(back)) == "restore_reconcile_pending"
    _journal_kept(rec)
    assert rec.mcs_recovery_database._db_version(str(live)) == 7
    assert decoy.read_bytes() == decoy_before


def test_restore_db_stale_consent_rejected(rec, tmp_path, monkeypatch):
    """A receipt bound to a DIFFERENT report never unlocks the swap —
    the approval must match the exact loss report bytes+schema."""
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    _consent(rec, live, back,
             report={"report_id": "f" * 64, "backup_sha256": "e" * 64,
                     "backup_schema": 7})
    err = rec.mcs_recovery_database._restore_db(str(back))
    assert err and err.startswith("restore_consent_pending:")
    assert rec.mcs_recovery_database._db_version(str(live)) == 8


def test_loss_report_matches_updater_and_binds_row_updates(rec, tmp_path, monkeypatch):
    import mcs_update
    import sqlite3
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    with sqlite3.connect(live) as con:
        con.execute("CREATE TABLE messages(message_id INTEGER, body_html TEXT)")
        con.execute("INSERT INTO messages VALUES(1, 'synthetic')")
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    monkeypatch.setattr(mcs_update, "LEDGER", str(live))
    monkeypatch.setattr(update_apply, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(mcs_update, "RESTORE_REPORT_PATH",
                        str(tmp_path / "data" / "report.json"))
    monkeypatch.setattr(update_restore, 'RESTORE_REPORT_PATH', mcs_update.RESTORE_REPORT_PATH)
    approved = _consent(rec, live, back)
    assert approved["report_id"] == restore_evidence.create_loss_report(live_path=mcs_update.LEDGER, backup_path=str(back), report_path=mcs_update.RESTORE_REPORT_PATH)["report_id"]
    with sqlite3.connect(live) as con:
        con.execute("UPDATE messages SET body_html='changed synthetic'")
    current = rec.mcs_recovery_database._loss_report(str(back))
    assert current["report_id"] == restore_evidence.create_loss_report(live_path=mcs_update.LEDGER, backup_path=str(back), report_path=mcs_update.RESTORE_REPORT_PATH)["report_id"]
    assert current["report_id"] != approved["report_id"]
    assert rec.mcs_recovery_database._consent_for(current) is None


def test_restore_db_verify_mismatch(rec, tmp_path, monkeypatch):
    """A restore that reads back the wrong schema must be reported —
    claiming success on an unverified copy is the bug this fixes."""
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    # live always reads 8 — even after the swap (the verify read)
    monkeypatch.setattr(rec.mcs_recovery_database, "_db_version",
                        lambda p: 7 if p == str(back) else 8)
    _consent(rec, live, back)
    _rollback_journal(rec, back)
    assert rec.mcs_recovery_database._restore_db(str(back)) == "restore_verify_failed"
    _journal_kept(rec)


def test_restore_db_replaces_and_verifies(rec, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    _consent(rec, live, back)
    _rollback_journal(rec, back)
    assert rec.mcs_recovery_database._restore_db(str(back)) == "restore_reconcile_pending"
    _journal_kept(rec)
    assert rec.mcs_recovery_database._db_version(str(live)) == 7
    marker = json.loads((tmp_path / "data"
                         / "restore_pending.json").read_text())
    assert marker["phase"] == "restored"


def test_recover_escalates_on_rollback_db_restore(rec, tmp_path,
                                                  monkeypatch):
    """The silent-skip bug: backup unreadable during rollback recovery
    must escalate, never 'resumed' (F-restore)."""
    _rollback_state(rec, tmp_path, str(tmp_path / "data" / "gone.db"))
    live = tmp_path / "data" / "ledger.db"
    _mk_db(live, 8)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    monkeypatch.setattr(rec.mcs_recovery_services, "_notify", lambda *a: pytest.fail("no notice during restore hold"))
    monkeypatch.setattr(rec.mcs_recovery_services, "_restart_drainers",
                        lambda **kw: pytest.fail("no writer restart before verified restore"))
    assert rec.recover() == 1
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
    assert report["result"] == "escalate"
    assert "backup_unreadable" in report["detail"]
    after = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
    assert "cid-rb" not in after["executed"]
    # applying record preserved for the human — not consumed
    assert after["applying"]["rollback"] is True


def test_recover_resumed_verifies_db_restore(rec, tmp_path, monkeypatch):
    """A verified copy retains its journal until compatible notification reconcile."""
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    _rollback_state(rec, tmp_path, str(back))
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    monkeypatch.setattr(rec.mcs_recovery_services, "_notify", lambda *a: pytest.fail("no notice during restore hold"))
    monkeypatch.setattr(rec.mcs_recovery_services, "_restart_drainers",
                        lambda **kw: pytest.fail("no writer restart before verified restore"))
    monkeypatch.setattr(rec.mcs_recovery_services, "_reconcile_membership", lambda s: [])
    _consent(rec, live, back)
    assert rec.recover() == 1
    assert rec.mcs_recovery_database._db_version(str(live)) == 7
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
    assert report["result"] == "restore_reconcile_pending" and "writers_stopped" not in report
    after = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
    assert "cid-rb" not in after["executed"]
    assert after["applying"]["restore_incomplete"] is True
    assert after["applied"] == []


def test_recover_holds_at_restore_consent(rec, tmp_path, monkeypatch):
    """Independent watchdog with NO consent receipt: publish the loss
    report, hold in awaiting_consent — no swap, applying state survives
    for the next watchdog pass (per-restore human approval gate)."""
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    _rollback_state(rec, tmp_path, str(back))
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    monkeypatch.setattr(rec.mcs_recovery_services, "_notify", lambda *a: None)
    monkeypatch.setattr(rec.mcs_recovery_services, "_reconcile_membership", lambda s: [])
    live_before = live.read_bytes()
    assert rec.recover() == 0
    assert live.read_bytes() == live_before        # no unauthorized swap
    state = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
    assert state["applying"]["rollback"] is True   # journal survives held
    marker = json.loads((tmp_path / "data"
                         / "restore_pending.json").read_text())
    assert marker["phase"] == "awaiting_consent"
    rep = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
    assert rep["result"] == "restore_consent_pending"
    assert rep["detail"].startswith("restore_consent_pending:")
    import restore_evidence
    assert rec.mcs_recovery_database.RESTORE_LOSS_NOTICE == restore_evidence.RESTORE_LOSS_NOTICE
    assert rec.mcs_recovery_database.RESTORE_LOSS_NOTICE in rep["detail"]
    assert rec.mcs_recovery_database.RESTORE_LOSS_NOTICE in rec._recovery_explanation(rep["detail"])
    # second pass is stable — still held, still no swap
    assert rec.recover() == 0
    assert live.read_bytes() == live_before


@pytest.mark.parametrize("shape", ["previous_clean", "previous_dirty", "merge_head",
                                   "missing_backup", "unknown_head"])
def test_incomplete_restore_never_finishes_without_restoring_target(
        rec, tmp_path, monkeypatch, shape):
    state = _applying("b" * 40, sha="a" * 40, stage="rollback")
    applying = state["applying"]
    applying.update(rollback=True, backup_path=str(tmp_path / "synthetic-backup.db"),
                    restore_incomplete=True, command_id="synthetic-held")
    if shape == "missing_backup":
        applying.pop("backup_path")
    rec.mcs_recovery_core._save_state(state)
    Path(rec.mcs_recovery_core.MARKER_PATH).write_text("synthetic writer freeze")
    if shape == "merge_head":
        git_dir = Path(rec.mcs_recovery_core.REPO, ".git")
        git_dir.mkdir(parents=True)
        (git_dir / "MERGE_HEAD").write_text("c" * 40)
    monkeypatch.setattr(rec.mcs_recovery_core, "_head", lambda: "c" * 40 if shape == "unknown_head" else "b" * 40)
    monkeypatch.setattr(rec.mcs_recovery_core, "_clean", lambda: shape != "previous_dirty")
    monkeypatch.setattr(rec.mcs_recovery_core, "_clean_stale_git_locks", lambda: [])

    def forbidden(*args, **kwargs):
        pytest.fail("incomplete restore must not reset, finish, restore an unbound DB, or restart writers")

    for target_owner, name in ((rec.mcs_recovery_core, '_git'), (rec.mcs_recovery_core, '_git_out'), (rec.mcs_recovery_database, '_restore_db'), (rec.mcs_recovery_services, '_restart_drainers'), (rec.mcs_recovery_services, '_restart_services'), (rec, '_finish')):
        monkeypatch.setattr(target_owner, name, forbidden)
    for _ in range(2):
        assert rec.recover() == 1
        after = rec.mcs_recovery_core._load_state()
        assert after["applying"] == applying and after["stages"] == state["stages"]
        assert "synthetic-held" not in after["executed"]
        assert Path(rec.mcs_recovery_core.MARKER_PATH).read_text() == "synthetic writer freeze"
        report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
        assert "writers_stopped" not in report
        assert "restore_incomplete" in report["detail"]


@pytest.mark.parametrize("at,held,reinstall_done,migration", [
    *[(at, held, done, _MIGRATION) for at in ("target", "prev") for held in (False, True)
      for done in (False, True)],
    # an empty or malformed record still holds: only the target updater may clear it
    *[("target", False, False, bad) for bad in ({}, None, False, [])]])
def test_runtime_migration_is_left_to_the_target_updater(rec, tmp_path, monkeypatch,
                                                         at, held, reinstall_done, migration):
    """An interrupted interpreter/venv migration belongs to the target
    version's mcs_update recover: three watchdog passes change nothing
    (journal, marker, tree, venv, backup, DB), restart nothing and notify
    once; once the updater has cleared the journal the next pass is normal."""
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "f.txt").write_text("target")
    _git(repo, "commit", "-qam", "target")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if at == "prev":
        _git(repo, "checkout", "-q", prev)
    (repo / "f.txt").write_text("half-written")            # a mid-migration tree
    venv, backup = tmp_path / "hermes-venv", tmp_path / "hermes-venv.prev"
    for d in (venv, backup):
        d.mkdir()
        (d / "pyvenv.cfg").write_text(d.name)
    db = tmp_path / "data" / "mcs.db"
    db.write_bytes(b"synthetic-db")
    state = _applying(prev, sha=target, stage="runtime_migration")
    state["applying"].update({"runtime_migration": (dict(migration, backup=str(backup))
                                                    if migration else migration),
                              "reinstall": True, "reinstall_done": reinstall_done})
    if held:
        state["applying"].update({"rollback": True, "backup_path": str(db) + ".bak"})
        state["restore_consent"] = {"report_id": "r1", "backup_path": str(db) + ".bak", "at": 1}
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    Path(rec.mcs_recovery_core.MARKER_PATH).write_text("{}")
    def snapshot():
        return (Path(rec.mcs_recovery_core.STATE_PATH).read_bytes(), Path(rec.mcs_recovery_core.MARKER_PATH).read_bytes(),
                (repo / "f.txt").read_text(), _git(repo, "rev-parse", "HEAD").stdout,
                (venv / "pyvenv.cfg").read_text(), (backup / "pyvenv.cfg").read_text(),
                db.read_bytes())
    assert not rec.mcs_recovery_core._load_state().get("_corrupt")   # the record is accepted as state, not as corruption
    before = snapshot()
    notices, restarts = [], []
    monkeypatch.setattr(rec.mcs_recovery_services, "_notify", lambda text: notices.append(text))
    monkeypatch.setattr(rec.mcs_recovery_services, "_restart_drainers", lambda **k: restarts.append(k) or [])
    monkeypatch.setattr(rec.mcs_recovery_services, "_restart_services", lambda *a, **k: restarts.append(a) or 0)
    for _ in range(3):
        assert rec.recover(if_stale=True) == 1
    assert snapshot() == before and restarts == []
    assert len(notices) == 1 and "mcs_update.py recover" in notices[0]
    assert json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())["result"] == "runtime_migration_pending"
    # the target updater recovered and cleared the journal: the watchdog's normal pass resumes
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump({"v": 1, "applying": None, "stages": [], "applied": [], "attempts": {}, "executed": {}}, f)
    assert rec.recover() == 0 and not os.path.exists(rec.mcs_recovery_core.MARKER_PATH)


@pytest.mark.parametrize("by", ["mcs_update", "mcs_recover"])
@pytest.mark.parametrize("journal_fault", ["none", "save", "corrupt", "missing"])
def test_restored_witness_holds_fresh_watchdog_retry(
        rec, tmp_path, monkeypatch, by, journal_fault):
    live, back = tmp_path / "data" / "ledger.db", tmp_path / "data" / "backup.db"
    _mk_db(live, 7)
    _mk_db(back, 7)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    _rollback_journal(rec, back)
    marker = tmp_path / "data" / "restore_pending.json"
    witness = {"v": 1, "phase": "restored", "by": by,
               "restored_at": 123, "at": 123, "backup_path": str(back)}
    marker.write_text(json.dumps(witness))
    Path(rec.mcs_recovery_core.MARKER_PATH).write_text("synthetic writer freeze")
    original_save = rec.mcs_recovery_core._save_state
    if journal_fault == "save":
        def failed_save(state):
            if (state.get("applying") or {}).get("restore_incomplete"):
                raise OSError("synthetic journal publish failure")
            return original_save(state)
        monkeypatch.setattr(rec.mcs_recovery_core, "_save_state", failed_save)
    elif journal_fault == "corrupt":
        Path(rec.mcs_recovery_core.STATE_PATH).write_text("{broken")
    elif journal_fault == "missing":
        Path(rec.mcs_recovery_core.STATE_PATH).unlink()
    db_before, marker_before = live.read_bytes(), marker.read_bytes()
    assert rec.mcs_recovery_database._restore_db(str(back)) == "restore_reconcile_pending"
    assert live.read_bytes() == db_before and marker.read_bytes() == marker_before
    # Reload the standalone script, not a module retaining an in-memory hold.
    fresh = _load()
    for name in ('REPO', 'DATA', 'STATE_PATH', 'LEDGER', 'UPDATE_LOCK', 'RUN_LOCK', 'MARKER_PATH', 'REPORT_PATH'):
        monkeypatch.setattr(fresh.mcs_recovery_core, name, getattr(rec.mcs_recovery_core, name))
    def forbidden(*args, **kwargs):
        pytest.fail("unreconciled restore must not run Git, write DB, restart or delete markers")
    for target_owner, name in ((fresh.mcs_recovery_core, '_git'), (fresh.mcs_recovery_core, '_head'), (fresh.mcs_recovery_services, '_restart_drainers'), (fresh.mcs_recovery_services, '_restart_services'), (fresh.mcs_recovery_core, '_remove_marker'), (fresh.mcs_recovery_services, '_notify'), (fresh.mcs_recovery_services, '_reconcile_membership')):
        monkeypatch.setattr(target_owner, name, forbidden)
    state_before = Path(rec.mcs_recovery_core.STATE_PATH).read_bytes() if Path(rec.mcs_recovery_core.STATE_PATH).exists() else None
    for _ in range(2):
        assert fresh.recover() == (2 if journal_fault == "corrupt" else 1)
        assert live.read_bytes() == db_before and marker.read_bytes() == marker_before
        assert Path(rec.mcs_recovery_core.MARKER_PATH).read_text() == "synthetic writer freeze"
        assert (Path(rec.mcs_recovery_core.STATE_PATH).read_bytes() if Path(rec.mcs_recovery_core.STATE_PATH).exists() else None) == state_before
    if journal_fault == "none":
        assert rec.mcs_recovery_core._load_state()["applying"]["restore_incomplete"] is True


def test_independent_copy_requires_reconcile_after_actual_swap_and_fresh_retry(rec, tmp_path, monkeypatch):
    live, back = tmp_path / "data" / "ledger.db", tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    _consent(rec, live, back)
    _rollback_journal(rec, back)
    assert rec.mcs_recovery_database._restore_db(str(back)) == "restore_reconcile_pending"
    assert rec.mcs_recovery_database._db_version(str(live)) == 7
    _journal_kept(rec)
    marker = tmp_path / "data" / "restore_pending.json"
    original_marker = marker.read_bytes()
    original_state = Path(rec.mcs_recovery_core.STATE_PATH).read_bytes()
    assert rec.mcs_recovery_core._load_state()["applying"]["restore_incomplete"] is True
    fresh = _load()
    for name in ('DATA', 'STATE_PATH', 'LEDGER'):
        monkeypatch.setattr(fresh.mcs_recovery_core, name, getattr(rec.mcs_recovery_core, name))
    assert fresh.mcs_recovery_database._restore_db(str(back)) == "restore_reconcile_pending"
    assert marker.read_bytes() == original_marker
    assert Path(rec.mcs_recovery_core.STATE_PATH).read_bytes() == original_state
    assert fresh.mcs_recovery_database._db_version(str(live)) == 7


@pytest.mark.parametrize("phase", ["awaiting_consent", "restored"])
def test_updater_reconcile_hold_does_not_claim_backup_adoption_marker(rec, tmp_path, phase):
    marker = tmp_path / "data" / "restore_pending.json"
    marker.write_text(json.dumps({"by": "mcs_backup_restore", "phase": phase,
                                  "restored_at": 123}))
    before = marker.read_bytes()
    assert rec.mcs_recovery_core._unreconciled_restore() is None
    assert marker.read_bytes() == before


def test_same_schema_without_restore_witness_still_resumes_without_copy(rec, tmp_path, monkeypatch):
    import sqlite3
    live, back = tmp_path / "data" / "ledger.db", tmp_path / "data" / "backup.db"
    _mk_db(live, 7)
    _mk_db(back, 7)
    connection = sqlite3.connect(str(live))
    try:
        connection.execute("INSERT INTO t VALUES('synthetic-live-sentinel')")
        connection.commit()
    finally:
        connection.close()
    _rollback_state(rec, tmp_path, str(back))
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    restarts = []
    monkeypatch.setattr(rec.mcs_recovery_services, "_reconcile_membership", lambda _snapshot: [])
    monkeypatch.setattr(rec.mcs_recovery_services, "_restart_drainers", lambda **kw: restarts.append("drainers") or [])
    monkeypatch.setattr(rec.mcs_recovery_services, "_restart_services", lambda *args: restarts.append("services") or 0)
    monkeypatch.setattr(rec.mcs_recovery_database, "_replace_database", lambda *args: pytest.fail("schema noop must not copy"))
    Path(rec.mcs_recovery_core.MARKER_PATH).write_text("synthetic update in progress")
    live_before = live.read_bytes()
    assert not (tmp_path / "data" / "restore_pending.json").exists()
    assert rec.recover() == 0
    assert live.read_bytes() == live_before and rec.mcs_recovery_database._db_version(str(live)) == 7
    state = rec.mcs_recovery_core._load_state()
    assert state["applying"] is None and state["stages"] == []
    assert state["executed"]["cid-rb"]["result"] == "rolled_back"
    assert "restore_consent" not in state and not Path(rec.mcs_recovery_core.MARKER_PATH).exists()
    assert restarts == ["drainers", "services"]
    assert json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())["result"] == "resumed"


@pytest.mark.parametrize("already_pending", [True, False], ids=["before-dialog", "after-recover"])
def test_interactive_pending_reconcile_requires_compatible_handler_not_more_consent(
        rec, tmp_path, monkeypatch, capsys, already_pending):
    import builtins
    marker = tmp_path / "data" / "restore_pending.json"
    witness = {"v": 1, "by": "mcs_recover", "phase": "restored", "restored_at": 123}
    state = {"applying": {"rollback": True}, "stages": [], "restore_consent": {"report_id": "synthetic"}}
    monkeypatch.setattr(rec.mcs_recovery_core, "_load_state", lambda: state)
    monkeypatch.setattr(rec.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    def recover():
        assert not already_pending
        marker.write_text(json.dumps(witness))
        return 1
    def answer(prompt):
        assert not already_pending
        return "2"
    monkeypatch.setattr(rec, "recover", recover)
    monkeypatch.setattr(builtins, "input", answer)
    if already_pending:
        marker.write_text(json.dumps(witness))
    assert rec.interactive_recovery() == 1
    text = capsys.readouterr().out
    assert "通知記録の照合が未完了" in text
    assert "自動監視の再確認を待ち" not in text
    assert json.loads(marker.read_text()) == witness


@pytest.mark.parametrize("version", [7, 9])
def test_matching_restored_writer_resumes_but_keeps_delivery_witness(rec, tmp_path, monkeypatch, version):
    live, back = tmp_path / "data" / "ledger.db", tmp_path / "data" / "backup.db"
    _mk_db(live, version + 1)
    _mk_db(back, version)
    _rollback_state(rec, tmp_path, str(back))
    _commit_recovery_schema(rec, tmp_path, version)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    _consent(rec, live, back)
    restarts = []
    marker = tmp_path / "data" / "restore_pending.json"
    def restart(**kwargs):
        assert rec.mcs_recovery_database._db_version(str(live)) == version
        assert json.loads(marker.read_text())["phase"] == "restored"
        restarts.append("writers")
        return []
    monkeypatch.setattr(rec.mcs_recovery_services, "_restart_drainers", restart)
    monkeypatch.setattr(rec.mcs_recovery_services, "_reconcile_membership", lambda _snapshot: [])
    monkeypatch.setattr(rec.mcs_recovery_services, "_restart_services", lambda *args: 0)
    Path(rec.mcs_recovery_core.MARKER_PATH).write_text("synthetic update hold")
    assert rec.recover() == 0
    assert rec.mcs_recovery_database._db_version(str(live)) == version and live.read_bytes() == back.read_bytes()
    state = rec.mcs_recovery_core._load_state()
    assert state["applying"] is None and state["stages"] == []
    assert state["executed"]["cid-rb"]["result"] == "rolled_back"
    assert restarts == ["writers"] and not Path(rec.mcs_recovery_core.MARKER_PATH).exists()
    witness = marker.read_bytes()
    assert json.loads(witness)["by"] == "mcs_recover"
    # Writer restart is not notification acceptance; runner keeps this witness
    # until its existing same-generation journal reconciliation is durable.
    assert marker.read_bytes() == witness


def test_fresh_retry_of_matching_restored_writer_keeps_delivery_marker(rec, tmp_path, monkeypatch):
    live, back = tmp_path / "data" / "ledger.db", tmp_path / "data" / "backup.db"
    _mk_db(live, 7)
    _mk_db(back, 7)
    _rollback_state(rec, tmp_path, str(back))
    _commit_recovery_schema(rec, tmp_path, 7)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    rec.mcs_recovery_database._mark_restored(str(back))
    marker = tmp_path / "data" / "restore_pending.json"
    witness = marker.read_bytes()
    fresh = _load()
    for name in ('REPO', 'DATA', 'STATE_PATH', 'LEDGER'):
        monkeypatch.setattr(fresh.mcs_recovery_core, name, getattr(rec.mcs_recovery_core, name))
    assert fresh.mcs_recovery_database._restored_writer_compatible() is True
    assert fresh.mcs_recovery_database._restore_db(str(back)) is None
    assert marker.read_bytes() == witness and fresh.mcs_recovery_database._db_version(str(live)) == 7


@pytest.mark.parametrize("fault", ["schema", "dynamic", "duplicate", "bytes", "head", "git_read", "source_read"])
def test_unproven_restored_writer_stays_held(rec, tmp_path, monkeypatch, fault):
    live, back = tmp_path / "data" / "ledger.db", tmp_path / "data" / "backup.db"
    _mk_db(live, 7)
    _mk_db(back, 7)
    _rollback_state(rec, tmp_path, str(back))
    canonical = _commit_recovery_schema(rec, tmp_path, 9 if fault == "schema" else 7)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    rec.mcs_recovery_database._mark_restored(str(back))
    if fault in ("dynamic", "duplicate", "bytes"):
        canonical.write_text({"dynamic": "SCHEMA_VERSION = int('7')\n",
                              "duplicate": "SCHEMA_VERSION = 7\nSCHEMA_VERSION = 7\n",
                              "bytes": canonical.read_text() + "# untracked byte drift\n"}[fault])
        monkeypatch.setattr(rec.mcs_recovery_core, "_clean", lambda: True)  # bytes proof must be independent
    elif fault == "head":
        monkeypatch.setattr(rec.mcs_recovery_core, "_head", lambda: "f" * 40)
    elif fault == "git_read":
        real_git = rec.mcs_recovery_core._git
        monkeypatch.setattr(rec.mcs_recovery_core, "_git", lambda args, *a, **kw: None if args[0] == "show" else real_git(args, *a, **kw))
    elif fault == "source_read":
        canonical.unlink()
    marker = tmp_path / "data" / "restore_pending.json"
    witness, contents = marker.read_bytes(), live.read_bytes()
    assert rec.mcs_recovery_database._restored_writer_compatible() is False
    assert rec.mcs_recovery_database._restore_db(str(back)) == "restore_reconcile_pending"
    assert live.read_bytes() == contents and marker.read_bytes() == witness
    assert rec.mcs_recovery_core._load_state()["applying"]["restore_incomplete"] is True


def test_existing_same_version_awaiting_consent_promotion_keeps_legacy_flow(rec, tmp_path, monkeypatch):
    live, back = tmp_path / "data" / "ledger.db", tmp_path / "data" / "backup.db"
    _mk_db(live, 7)
    _mk_db(back, 7)
    _rollback_state(rec, tmp_path, str(back))
    _commit_recovery_schema(rec, tmp_path, 7)
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    rec.mcs_recovery_database._mark_restored(str(back), phase="awaiting_consent", report_id="synthetic-still-held")
    marker = tmp_path / "data" / "restore_pending.json"
    before = marker.read_bytes()
    assert rec.mcs_recovery_database._restored_writer_compatible() is False  # new permission excludes awaiting phase
    assert rec.mcs_recovery_database._restore_db(str(back)) is None  # existing same-version legacy promotion stays intact
    promoted = json.loads(marker.read_text())
    assert promoted["phase"] == "restored" and marker.read_bytes() != before
    assert rec.mcs_recovery_database._restored_writer_compatible() is True and rec.mcs_recovery_database._db_version(str(live)) == 7
