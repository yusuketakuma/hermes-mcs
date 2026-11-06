"""Synthetic mcs update restore contracts; shared helpers preserve isolated runtime state."""
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import update_services
import restore_evidence
import service_contract
import service_management
import owned_process
import update_discovery
import runtime_inspection
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
import pytest
import mcs_update
import mcs_util
from ops_testkit import _git, _make_repo, _mk_schema, _receipts_db, _seed_consent
from mcs_update_testkit import _HANG_EXCS as _HANG_EXCS, _HungLaunchd as _HungLaunchd, _assert_old_schema_restore_held as _assert_old_schema_restore_held, _cron_listing as _cron_listing, _hang_git as _hang_git, _hold_condition as _hold_condition, _journal_kept as _journal_kept, _live_version as _live_version, _rollback_journal as _rollback_journal, _schema_bump_world as _schema_bump_world, prepared_run_log as prepared_run_log, updater as updater


def test_restore_does_not_replace_unreadable_consent_marker(updater, tmp_path):
    marker = tmp_path / "data" / "restore_pending.json"
    marker.write_text("{broken")
    with pytest.raises(updater.UpdateError, match="restore_marker_unreadable"):
        updater._restore_db(str(tmp_path / "backup.db"))
    assert marker.read_text() == "{broken"


@pytest.mark.parametrize("interrupted", [False, True])
def test_rollback_keeps_manifest_and_restarts_changed_plugin(
        updater, prepared_run_log, tmp_path, monkeypatch, interrupted):
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    before = _git(repo, "rev-parse", "v1.0.0").stdout.strip()
    after = _git(repo, "rev-parse", "v1.1.0^{commit}").stdout.strip()
    desired = {"scripts": [], "agents": [{"label": "local.mcs-cmd"}], "cron": []}
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": after,
                         "prev_sha": before, "plugin_changed": True,
                         "manifest_snapshot": desired, "command_id": "apply-id"}
    monkeypatch.setattr(updater, "_services_reconcile", lambda: None)
    monkeypatch.setattr(update_recovery, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(updater, "restart_agents", lambda: [])
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(updater, "_postcheck", lambda *a: [])
    monkeypatch.setattr(update_recovery, '_postcheck', updater._postcheck)
    monkeypatch.setattr(update_runtime_migration, '_postcheck', updater._postcheck)
    monkeypatch.setattr(updater, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    assert updater._post_merge(state) == 0
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state["applied"][-1]["manifest_snapshot"] == desired
    state["stages"] = []
    restarts, memberships = [], []

    def restart(cfg):
        # The decision survives popping the entry, and durable bookkeeping
        # must precede a restart that could terminate this caller.
        persisted = updater.journal_store.load_state(updater.STATE_PATH)
        assert persisted["applied"] == []
        assert persisted["executed"]["rollback-id"]["result"] == "rolled_back"
        restarts.append(True)

    monkeypatch.setattr(updater, "restart_gateway", restart)
    monkeypatch.setattr(update_services, 'restart_gateway', updater.restart_gateway)
    monkeypatch.setattr(updater, "quiesce", lambda: [])
    monkeypatch.setattr(update_apply, 'quiesce', updater.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', updater.quiesce)
    monkeypatch.setattr(service_management, 'retire_to_snapshot',
                        lambda manifest, **_service_kwargs: memberships.append(manifest) or [])
    if interrupted:
        state["applying"] = {"tag": "rollback:v1.1.0", "sha": before,
                             "prev_sha": after, "rollback": True,
                             "plugin_changed": True, "manifest_snapshot": desired,
                             "command_id": "rollback-id", "at": time.time()}
        state["stages"] = [{"stage": "rollback", "at": time.time()}]
        _git(repo, "reset", "--hard", before)
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert (updater.recover_interrupted() if interrupted
            else updater.rollback("rollback-id")) == 0
    assert updater.journal_store.load_state(updater.STATE_PATH)["applied"] == []
    assert restarts == [True]
    assert memberships == [desired]


@pytest.mark.parametrize(("script", "evidence", "removed"), [
    ("mcs_offsite.sh", None, False),
    ("other/mcs_offsite.sh", {"id": "abcdef", "script": "mcs_offsite.sh"}, False),
    ("mcs_offsite.sh", {"id": "111111", "script": "mcs_offsite.sh"}, False),
    ("mcs_offsite.sh", {"id": "abcdef", "script": "mcs_offsite.sh"}, True),
])
def test_rollback_offsite_cron_requires_matching_owned_identity(
        updater, monkeypatch, script, evidence, removed):

    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda cfg: "synthetic-hermes")
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda path: True)
    if evidence is not None:
        path = Path(updater.MANIFEST_PATH)
        path.write_text(json.dumps({"cron": [evidence]}))
        path.chmod(0o600)
    calls = []
    def execute(argv, **kwargs):
        calls.append(argv)
        output = _cron_listing(script) if argv[2] == "list" else ""
        return subprocess.CompletedProcess(argv, 0, output, "")
    monkeypatch.setattr(updater.subprocess, "run", execute)
    monkeypatch.setattr(owned_process, "run_owned", execute)
    assert service_management.retire_to_snapshot({'cron': [], 'agents': []}, cfg=mcs_util.load_config(), paths=updater._service_paths()) == []
    assert (["synthetic-hermes", "cron", "remove", "abcdef"] in calls) is removed


@pytest.mark.parametrize("name", ["user-added.txt", "新規ファイル.txt", "sp ace.txt"])
def test_rollback_preserves_untracked_update_name(updater, tmp_path, monkeypatch, name):
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    previous = _git(repo, "rev-parse", "HEAD").stdout.strip()
    path = repo / name
    path.write_text("release content")
    _git(repo, "add", path.name)
    _git(repo, "commit", "-qm", "release adds path")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "reset", "--hard", previous)
    path.write_text("user content after interrupted update")
    monkeypatch.setattr(service_management, 'retire_to_snapshot', lambda manifest, **_service_kwargs: [])
    monkeypatch.setattr(updater, "_services_reconcile", lambda: None)
    monkeypatch.setattr(update_recovery, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', updater._services_reconcile)

    updater._rollback_tree({"sha": target, "prev_sha": previous})

    assert path.read_text() == "user content after interrupted update"


def test_restore_db_removes_wal_sidecars(updater, tmp_path, monkeypatch):
    """SQLite settles old WAL sidecars before replacing the live DB (F9)."""
    import ledger as _ledger  # noqa: F401 — real module, tmp DBs only
    live = str(tmp_path / "ledger.db")
    back = str(tmp_path / "backup.db")
    mcs_update.LEDGER = live
    for path, ver, tag in ((live, 7, "live"), (back, 6, "back")):
        con = sqlite3.connect(path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(f"PRAGMA user_version={ver}")
        con.execute("CREATE TABLE t(x)")
        con.execute("INSERT INTO t VALUES(?)", (tag,))
        con.commit()
        con.close()
    (Path(live).parent / "ledger.db-wal").write_bytes(b"stalewal")
    (Path(live).parent / "ledger.db-shm").write_bytes(b"staleshm")
    # valid_mcs_db is the live-schema gate — stub it to accept both
    monkeypatch.setattr(_ledger, "valid_mcs_db", lambda p: True)
    _seed_consent(live, back)
    _rollback_journal(mcs_update, back)
    # The deliberately tiny t-table fixture tests copy/sidecar ownership,
    # not a current Ledger schema. Never migrate it for reconciliation.
    monkeypatch.setattr(_ledger, "Ledger",
                        lambda *a, **kw: pytest.fail("old schema must not open new writer"))
    replace = updater._replace_database
    copied = []

    def checked_replace(*args):
        replace(*args)
        # A later read of the WAL-mode backup can create fresh sidecars.
        assert not Path(live + "-wal").exists()
        assert not Path(live + "-shm").exists()
        copied.append(True)

    monkeypatch.setattr(updater, "_replace_database", checked_replace)
    monkeypatch.setattr(update_restore, '_replace_database', updater._replace_database)
    with pytest.raises(mcs_update.UpdateError, match="restore_reconcile_schema_blocked"):
        mcs_update._restore_db(back)
    assert copied == [True]
    _journal_kept(mcs_update)
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["restore_incomplete"] is True
    marker = json.loads(Path(updater.DATA, "restore_pending.json").read_text())
    assert marker["phase"] == "restored"
    assert marker["by"] == "mcs_update"
    assert marker["report_id"] == json.loads(Path(updater.RESTORE_REPORT_PATH).read_text())["report_id"]
    # Copy succeeded, but old-schema notification reconciliation remains
    # held. Both the backup row and its schema version must survive.
    con = sqlite3.connect("file:" + live + "?mode=ro", uri=True)
    assert con.execute("SELECT x FROM t").fetchone()[0] == "back"
    assert con.execute("PRAGMA user_version").fetchone()[0] == 6
    con.close()


def test_restore_db_skips_same_version(updater, tmp_path, monkeypatch):
    """No restore when live already matches the backup schema — a
    failed apply may never have migrated the DB."""
    import ledger as _ledger
    live = str(tmp_path / "ledger.db")
    back = str(tmp_path / "backup.db")
    mcs_update.LEDGER = live
    for path in (live, back):
        con = sqlite3.connect(path)
        con.execute("PRAGMA user_version=7")
        con.execute("CREATE TABLE sentinel(x)")
        con.commit()
        con.execute("INSERT INTO sentinel VALUES(?)", (path,))
        con.commit()
        con.close()
    monkeypatch.setattr(_ledger, "valid_mcs_db", lambda p: True)
    mcs_update._restore_db(back)
    con = sqlite3.connect("file:" + live + "?mode=ro", uri=True)
    row = con.execute("SELECT x FROM sentinel").fetchone()[0]
    con.close()
    assert row == live               # untouched — no copy happened
    assert _live_version(live) == 7
    assert not Path(updater.DATA, "restore_pending.json").exists()


def test_restore_db_io_error_is_update_error(updater, tmp_path,
                                             monkeypatch):
    """A raw OSError out of _restore_db would slip past every caller's
    `except UpdateError` (apply bail, recover escalate, rollback
    rb_error) — it must be converted so drainers are never left
    quiesced by an unhandled raise."""
    import ledger as _ledger
    live = str(tmp_path / "ledger.db")
    back = str(tmp_path / "backup.db")
    mcs_update.LEDGER = live
    for path, ver in ((live, 8), (back, 7)):
        con = sqlite3.connect(path)
        con.execute(f"PRAGMA user_version={ver}")
        con.execute("CREATE TABLE t(x)")
        con.commit()
        con.close()
    monkeypatch.setattr(_ledger, "valid_mcs_db", lambda p: True)
    _seed_consent(live, back)
    def fail_copy(*args):
        raise OSError("synthetic copy failure")
    monkeypatch.setattr(mcs_update.shutil, "copyfileobj", fail_copy)
    with pytest.raises(mcs_update.UpdateError):
        mcs_update._restore_db(back)
    con = sqlite3.connect("file:" + live + "?mode=ro", uri=True)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 8
    con.close()


def test_rollback_restarts_agents_on_unexpected_error(
        updater, tmp_path, monkeypatch):
    """_rollback_tree raising ANY exception (not just UpdateError) must
    still reach restart_agents — quiesced drainers can never be left
    down by an unhandled raise (H4)."""
    repo, _ = _make_repo(tmp_path)           # fixture REPO == this
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait",
                        lambda **kw: os.open(mcs_update.RUN_LOCK,
                                             os.O_WRONLY | os.O_CREAT))
    monkeypatch.setattr(update_apply, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
    monkeypatch.setattr(update_recovery, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
    monkeypatch.setattr(mcs_update, "quiesce", lambda: None)
    monkeypatch.setattr(update_apply, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', mcs_update.quiesce)
    restarted = []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda: restarted.append(1) or [])
    monkeypatch.setattr(update_apply, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(
        mcs_update, "_rollback_tree",
        lambda e: (_ for _ in ()).throw(OSError("disk gone")))
    monkeypatch.setattr(update_apply, '_rollback_tree', mcs_update._rollback_tree)
    monkeypatch.setattr(update_recovery, '_rollback_tree', mcs_update._rollback_tree)
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(mcs_update, "_gateway_restart_if_needed",
                        lambda s: None)
    monkeypatch.setattr(update_recovery, '_gateway_restart_if_needed', mcs_update._gateway_restart_if_needed)
    state = updater.journal_store.default_state()
    state["applied"] = [{"tag": "v1.1.0",
                         "sha": _git(repo, "rev-parse", "HEAD").stdout.strip(),
                         "prev_sha": _git(repo, "rev-list", "-n1", "v1.0.0").stdout.strip(),
                         "at": time.time()}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    rc = updater.rollback("cid-rb")
    assert rc == 1
    assert restarted == [1]                  # drainers brought back up
    after = updater.journal_store.load_state(updater.STATE_PATH)
    assert after["executed"]["cid-rb"]["result"] == "rollback_failed"


@pytest.mark.parametrize("separator", ["#", "?", "%23"])
def test_restore_uses_exact_backup_path_with_uri_delimiters(
        updater, tmp_path, monkeypatch, separator):
    live = tmp_path / "data" / "ledger.db"
    prefix = tmp_path / "data" / "backup"
    back = Path(str(prefix) + separator + "snapshot.db")
    decoy = Path(str(prefix) + "#snapshot.db") if separator == "%23" else prefix
    _mk_schema(live, 8, messages=3)
    _mk_schema(decoy, 8, messages=3)
    _mk_schema(back, 7, messages=1)
    monkeypatch.setattr(updater, "LEDGER", str(live))
    monkeypatch.setattr(update_apply, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(updater, "_reconcile_restored", lambda: None)
    monkeypatch.setattr(update_restore, '_reconcile_restored', updater._reconcile_restored)
    before = live.read_bytes()

    with pytest.raises(updater.RestoreConsentPending) as error:
        updater._restore_db(str(back))
    assert restore_evidence.database_version(str(back)) == 7
    assert error.value.report["backup_schema"] == 7
    assert error.value.report["stored_since_backup"]["messages"] == 2
    assert live.read_bytes() == before
    _seed_consent(str(live), str(back))
    _rollback_journal(updater, back)
    updater._restore_db(str(back))
    _journal_kept(updater)
    assert restore_evidence.database_version(str(live)) == 7


def test_restore_measures_and_approves_exact_live_uri_path(
        updater, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger#?%23.db"
    back = tmp_path / "data" / "backup.db"
    decoy = tmp_path / "data" / "ledger"
    _mk_schema(live, 8, messages=3)
    _mk_schema(decoy, 8, messages=0)
    _mk_schema(back, 7, messages=1)
    monkeypatch.setattr(updater, "LEDGER", str(live))
    monkeypatch.setattr(update_apply, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(updater, "_reconcile_restored", lambda: None)
    monkeypatch.setattr(update_restore, '_reconcile_restored', updater._reconcile_restored)
    before = live.read_bytes()
    decoy_before = decoy.read_bytes()

    with pytest.raises(updater.RestoreConsentPending) as error:
        updater._restore_db(str(back))
    assert error.value.report["stored_since_backup"]["messages"] == 2
    assert live.read_bytes() == before
    _seed_consent(str(live), str(back))
    _rollback_journal(updater, back)
    updater._restore_db(str(back))
    _journal_kept(updater)
    assert restore_evidence.database_version(str(live)) == 7
    assert decoy.read_bytes() == decoy_before


def test_rollback_schema_bump_holds_for_consent(updater, tmp_path,
                                                monkeypatch, capsys):
    """No bound receipt => the rollback stops before the DB replace:
    tree already reset, drainers down, BOTH markers up, loss report
    durable, live DB byte-identical (T13)."""
    repo, live, back, before, _after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    live_bytes = Path(live).read_bytes()
    rc = updater.rollback("cid-rb")
    assert rc == 2
    assert restore_evidence.RESTORE_LOSS_NOTICE in capsys.readouterr().out
    notice = json.loads(Path(updater.REPORT_PATH).read_text())
    assert restore_evidence.RESTORE_LOSS_NOTICE in notice["detail"]
    assert Path(live).read_bytes() == live_bytes   # no unauthorized swap
    assert _live_version(live) == 8
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert restarts == []                  # drainers stay quiesced
    assert Path(mcs_update.MARKER_PATH).exists()
    marker = json.loads(
        (tmp_path / "data" / "restore_pending.json").read_text())
    assert marker["phase"] == "awaiting_consent"
    report = json.loads(
        (tmp_path / "data" / "restore_report.json").read_text())
    assert marker["report_id"] == report["report_id"]
    assert report["backup_schema"] == 7
    assert report["stored_since_backup"]["messages"] == 3
    assert report["intervening_messages"] == 3
    assert report["backup_sha256"] == mcs_update._file_sha256(back)
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state["applying"]["rollback"] is True
    assert state["restore_consent"]["report_id"] == report["report_id"]
    # the hold must NOT consume the rollback receipt — it stays pending
    # until the restore actually finishes
    assert "cid-rb" not in state.get("executed", {})


def test_rollback_consent_keeps_old_schema_reconcile_held(updater, tmp_path,
                                                monkeypatch):
    """After the bound ops.restore_approve receipt lands, recovery
    re-enters _restore_db: the approved copy preserves the backup schema,
    while incompatible notification reconcile keeps writers held."""
    repo, live, back, before, _after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    assert updater.rollback("cid-rb") == 2
    _seed_consent(live, back)
    assert updater.recover_interrupted() == 1
    assert _live_version(live) == 7        # copy must never remigrate
    con = sqlite3.connect("file:" + live + "?mode=ro", uri=True)
    assert con.execute(
        "SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    con.close()
    _assert_old_schema_restore_held(updater, live, back, restarts, "cid-rb")


def test_rollback_wrong_report_consent_stays_held(updater, tmp_path,
                                                  monkeypatch):
    """A receipt bound to a DIFFERENT loss report never unlocks the
    swap — approval is bound to exact bytes+schema+loss (R2)."""
    _repo, live, back, _before, _after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    assert updater.rollback("cid-rb") == 2
    _seed_consent(live, back,
                  report={"report_id": "f" * 64,
                          "backup_sha256": "e" * 64,
                          "backup_schema": 7})
    assert updater.recover_interrupted() == 0
    assert _live_version(live) == 8        # still untouched
    import notify_spool
    marker = notify_spool.restore_awaiting_consent(
        str(tmp_path / "data"))
    assert marker is not None              # hold stands
    assert restarts == []


def test_rollback_update_receipt_cannot_satisfy_consent(
        updater, tmp_path, monkeypatch):
    """The earlier ops.update_apply approval that authorized the update
    must NOT count as restore consent — a distinct op name is required
    (the human must have seen the loss report first)."""
    _repo, live, back, _before, _after, _restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    assert updater.rollback("cid-rb") == 2
    # seed an applied ops.update_apply receipt — same payload shape the
    # earlier approval produced, minus the restore binding
    report = restore_evidence.create_loss_report(live_path=mcs_update.LEDGER, backup_path=back, report_path=mcs_update.RESTORE_REPORT_PATH)
    con = _receipts_db(live)
    con.execute(
        "INSERT OR REPLACE INTO command_receipts VALUES(?,?,NULL,NULL,"
        "'applied',?,?)",
        ("cid-apply-old", "h" * 64, json.dumps({
            "cmd": "ops.update_apply", "scheduled": True,
            "command_id": "cid-apply-old", "tag": "v1.1.0",
            "report_id": report["report_id"],          # even forged
            "backup_sha256": report["backup_sha256"],
            "backup_schema": report["backup_schema"]}),
         time.time()))
    con.commit()
    con.close()
    assert updater.recover_interrupted() == 0
    assert _live_version(live) == 8        # never swapped
    import notify_spool
    assert notify_spool.restore_awaiting_consent(
        str(tmp_path / "data")) is not None


def test_loss_report_binds_live_state(updater, tmp_path, monkeypatch):
    """report_id is deterministic over (backup bytes, backup schema,
    live-vs-backup deltas) — any write slipping past the writer hold
    changes the id and invalidates every stale consent (T13)."""
    _repo, live, back, _b, _a, _r = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    r1 = restore_evidence.create_loss_report(live_path=mcs_update.LEDGER, backup_path=back, report_path=mcs_update.RESTORE_REPORT_PATH)
    r2 = restore_evidence.create_loss_report(live_path=mcs_update.LEDGER, backup_path=back, report_path=mcs_update.RESTORE_REPORT_PATH)
    assert r1["report_id"] == r2["report_id"]     # deterministic
    # a write that slips in invalidates the bound report
    con = sqlite3.connect(live)
    con.execute(
        "INSERT INTO messages(message_id,project_id,posted_at,"
        "posted_at_ts,body_html,body_state,content_hash,first_seen)"
        " VALUES(99,1,'2026-01-02',999,'<b>y</b>','full','h9',1)")
    con.commit()
    con.close()
    r3 = restore_evidence.create_loss_report(live_path=mcs_update.LEDGER, backup_path=back, report_path=mcs_update.RESTORE_REPORT_PATH)
    assert r3["report_id"] != r1["report_id"]
    assert r3["intervening_messages"] == 4


def test_restore_consent_expires_when_existing_message_changes(
        updater, tmp_path, monkeypatch):
    _repo, live, back, _before, _after, _restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    approved = _seed_consent(live, back)
    with sqlite3.connect(live) as con:
        con.execute("UPDATE messages SET body_html='changed synthetic text'")
    current = restore_evidence.create_loss_report(live_path=updater.LEDGER, backup_path=back, report_path=updater.RESTORE_REPORT_PATH)
    assert current["stored_since_backup"] == approved["stored_since_backup"]
    assert current["report_id"] != approved["report_id"]
    assert restore_evidence.find_restore_consent(current, live_path=updater.LEDGER) is None


@pytest.mark.parametrize("independent", [False, True])
def test_failed_restore_copy_preserves_live_wal(updater, tmp_path, monkeypatch, independent):
    from ops_testkit import _load, patch_lifecycle_lookup
    _repo, live, back, _before, _after, _restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    recovery = _load() if independent else updater
    for name in ("LEDGER", "DATA", "STATE_PATH"):
        patch_lifecycle_lookup(monkeypatch, recovery, name, getattr(updater, name))
    state = updater.journal_store.load_state(updater.STATE_PATH)
    state["applying"] = {**state["applied"][-1], "rollback": True}
    updater.journal_store.save_state(updater.STATE_PATH, state)
    writer = sqlite3.connect(live)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE messages SET body_html='durable synthetic update'")
        writer.commit()
        approved = _seed_consent(live, back)
        receipts = writer.execute("SELECT * FROM command_receipts").fetchall()
        database_before = Path(live).read_bytes()
        wal = Path(live + "-wal")
        before = wal.read_bytes()
        copies = []

        def fail_copy(*args):
            copies.append(True)
            raise OSError("synthetic copy failure")

        monkeypatch.setattr(recovery.shutil, "copyfileobj", fail_copy)
        if independent:
            assert recovery._restore_db(back).startswith("restore_failed:")
        else:
            with pytest.raises(updater.UpdateError, match="restore_failed"):
                recovery._restore_db(back)
        assert copies == [True]
        assert Path(live).read_bytes() == database_before
        assert wal.read_bytes() == before
        assert writer.execute("SELECT body_html FROM messages LIMIT 1").fetchone()[0] == (
            "durable synthetic update")
        assert writer.execute("SELECT * FROM command_receipts").fetchall() == receipts
        hold = updater.notify_spool.restore_pending(updater.DATA)
        assert hold["phase"] == "awaiting_consent" and hold["report_id"] == approved["report_id"]
    finally:
        writer.close()


@pytest.mark.parametrize("approved_swap_failure", [False, True, "approved_restore"])
def test_apply_bail_consent_hold_keeps_writers_stopped(updater, tmp_path,
                                                       monkeypatch, approved_swap_failure):
    """A post-merge failure on a schema-bump release that lands in the
    restore-consent hold keeps the same invariant as rollback(): the
    drainers the NEW-code child restarted are quiesced again and the
    update marker stays up until consent."""
    import maintenance
    monkeypatch.setattr(updater, "_provider_migration_needed", lambda: False)
    monkeypatch.setattr(update_apply, '_provider_migration_needed', updater._provider_migration_needed)
    monkeypatch.setattr(update_recovery, '_provider_migration_needed', updater._provider_migration_needed)
    repo, _ = _make_repo(tmp_path)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_cli, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_execution, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_repository, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_services, 'REPO', mcs_update.REPO)
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    live = str(tmp_path / "data" / "ledger.db")
    monkeypatch.setattr(mcs_update, "LEDGER", live)
    monkeypatch.setattr(update_apply, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', mcs_update.LEDGER)
    back = tmp_path / "data" / "backups" / "pre.db"
    back.parent.mkdir(parents=True, exist_ok=True)
    _mk_schema(back, 7, messages=2)
    _mk_schema(live, 8, messages=5)
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(mcs_update, "precheck_local", lambda c: [])
    monkeypatch.setattr(update_apply, 'precheck_local', mcs_update.precheck_local)
    monkeypatch.setattr(update_planning, 'precheck_local', mcs_update.precheck_local)
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: sha)
    monkeypatch.setattr(mcs_update, "precheck_tag",
                        lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(update_execution, 'precheck_tag', mcs_update.precheck_tag)
    monkeypatch.setattr(update_planning, 'precheck_tag', mcs_update.precheck_tag)
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: str(back))
    monkeypatch.setattr(mcs_update, "_baseline_check", lambda c: [])
    monkeypatch.setattr(update_apply, '_baseline_check', mcs_update._baseline_check)
    events = []

    def quiesce():
        events.append("quiesce")
        mcs_update._write_marker()
        return []

    def restart():
        events.append("restart")
        mcs_update._remove_marker()   # real restart_agents drops it
        return []

    monkeypatch.setattr(mcs_update, "quiesce", quiesce)
    monkeypatch.setattr(update_apply, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(mcs_update, "restart_agents", restart)
    monkeypatch.setattr(update_apply, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(update_recovery, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(service_management, 'retire_to_snapshot', lambda m, **_service_kwargs: [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)
    monkeypatch.setattr(update_services, 'restart_gateway', mcs_update.restart_gateway)

    def post_merge(_state, *, run_lock_fd):
        st = mcs_update.journal_store.load_state(mcs_update.STATE_PATH)
        mcs_update.journal(st, "services")
        restart()
        mcs_update.journal(st, "restart")
        raise mcs_update.UpdateError(
            "post_merge_failed: postcheck_failed: new_env_error: x")

    if approved_swap_failure:
        monkeypatch.setattr(restore_evidence, "find_restore_consent", lambda report, **_paths: "synthetic-consent")
    if approved_swap_failure is True:
        def fail_swap(path, expected_sha, before_replace):
            before_replace()
            raise OSError("synthetic replace failure")

        monkeypatch.setattr(mcs_update, "_replace_database", fail_swap)
        monkeypatch.setattr(update_restore, '_replace_database', mcs_update._replace_database)
    monkeypatch.setattr(mcs_update, "_run_post_merge", post_merge)
    monkeypatch.setattr(update_apply, '_run_post_merge', mcs_update._run_post_merge)
    expected = 1 if approved_swap_failure else 2
    assert mcs_update.apply("v1.1.0", sha, "cid-apply") == expected
    st = mcs_update.journal_store.load_state(mcs_update.STATE_PATH)
    assert st.get("restore_consent")
    assert events[-1] == "quiesce" and "restart" in events
    assert Path(mcs_update.MARKER_PATH).exists()
    if approved_swap_failure == "approved_restore":
        assert st["applying"]["restore_incomplete"] is True
        db = sqlite3.connect(live)
        try:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 7
            assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        finally:
            db.close()
        import notify_spool
        marker = notify_spool.restore_pending(mcs_update.DATA)
        assert marker is not None and marker["phase"] == "restored"


def test_apply_consent_hold_survives_a_failing_quiesce(updater, tmp_path,
                                                       monkeypatch):
    """Recording the hold comes first: a quiesce failure after it must
    not lose restore_consent or drop the update marker."""
    import maintenance
    monkeypatch.setattr(updater, "_provider_migration_needed", lambda: False)
    monkeypatch.setattr(update_apply, '_provider_migration_needed', updater._provider_migration_needed)
    monkeypatch.setattr(update_recovery, '_provider_migration_needed', updater._provider_migration_needed)
    repo, _ = _make_repo(tmp_path)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    monkeypatch.setattr(update_apply, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_cli, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_execution, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_repository, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_services, 'REPO', mcs_update.REPO)
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    live = str(tmp_path / "data" / "ledger.db")
    monkeypatch.setattr(mcs_update, "LEDGER", live)
    monkeypatch.setattr(update_apply, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', mcs_update.LEDGER)
    back = tmp_path / "data" / "backups" / "pre.db"
    back.parent.mkdir(parents=True, exist_ok=True)
    _mk_schema(back, 7, messages=2)
    _mk_schema(live, 8, messages=5)
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(mcs_update, "precheck_local", lambda c: [])
    monkeypatch.setattr(update_apply, 'precheck_local', mcs_update.precheck_local)
    monkeypatch.setattr(update_planning, 'precheck_local', mcs_update.precheck_local)
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: sha)
    monkeypatch.setattr(mcs_update, "precheck_tag",
                        lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(update_execution, 'precheck_tag', mcs_update.precheck_tag)
    monkeypatch.setattr(update_planning, 'precheck_tag', mcs_update.precheck_tag)
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: str(back))
    monkeypatch.setattr(mcs_update, "_baseline_check", lambda c: [])
    monkeypatch.setattr(update_apply, '_baseline_check', mcs_update._baseline_check)
    def quiesce():
        mcs_update._write_marker()
        if mcs_update.journal_store.load_state(mcs_update.STATE_PATH).get("restore_consent"):
            raise mcs_update.UpdateError("drainer_stop_failed: synthetic")
        return []

    monkeypatch.setattr(mcs_update, "quiesce", quiesce)
    monkeypatch.setattr(update_apply, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda: (mcs_update._remove_marker(), [])[1])
    monkeypatch.setattr(update_apply, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(update_recovery, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(service_management, 'retire_to_snapshot', lambda m, **_service_kwargs: [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)
    monkeypatch.setattr(update_services, 'restart_gateway', mcs_update.restart_gateway)

    def post_merge(_s, *, run_lock_fd):
        raise mcs_update.UpdateError("post_merge_failed: synthetic")

    monkeypatch.setattr(mcs_update, "_run_post_merge", post_merge)
    monkeypatch.setattr(update_apply, '_run_post_merge', mcs_update._run_post_merge)
    assert mcs_update.apply("v1.1.0", sha, "cid-apply") == 2
    st = mcs_update.journal_store.load_state(mcs_update.STATE_PATH)
    assert st.get("restore_consent")
    assert (st.get("applying") or {}).get("rollback") is True
    assert Path(mcs_update.MARKER_PATH).exists()


def test_manual_rollback_defers_to_recovery_of_interrupted_apply(
        updater, monkeypatch):
    """An interrupted apply journal belongs to recovery — a manual
    rollback of the last APPLIED entry must not overwrite it."""
    state = {"v": 1, "applied": [{"tag": "v1.0.5", "sha": "a" * 40,
                                  "prev_sha": "b" * 40}],
             "applying": {"tag": "v1.0.6", "sha": "c" * 40,
                          "prev_sha": "a" * 40, "at": time.time()},
             "stages": [{"stage": "merge", "at": time.time()}],
             "attempts": {}, "executed": {}}
    mcs_update.journal_store.save_state(mcs_update.STATE_PATH, state)
    calls = []
    monkeypatch.setattr(mcs_update, "recover_interrupted",
                        lambda *a, **k: calls.append(1) or 0)
    monkeypatch.setattr(update_apply, 'recover_interrupted', mcs_update.recover_interrupted)
    monkeypatch.setattr(update_cli, 'recover_interrupted', mcs_update.recover_interrupted)
    monkeypatch.setattr(update_recovery, 'recover_interrupted', mcs_update.recover_interrupted)
    monkeypatch.setattr(mcs_update, "quiesce",
                        lambda: pytest.fail("rollback must not quiesce"))
    monkeypatch.setattr(update_apply, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', mcs_update.quiesce)
    assert mcs_update.rollback() == 0
    assert calls == [1]
    assert mcs_update.journal_store.load_state(mcs_update.STATE_PATH)["applying"]["tag"] == "v1.0.6"


@pytest.mark.parametrize("exc", _HANG_EXCS)
def test_rollback_partial_quiesce_reports_unverified_restart(
        updater, tmp_path, monkeypatch, exc):
    """quiesce stops drainer a, then b's stop is unverifiable: rollback
    must report the unverified restart and consume the receipt without
    launching new workers before every stop is verified."""
    from types import SimpleNamespace
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait",
                        lambda **kw: os.open(mcs_update.RUN_LOCK,
                                             os.O_WRONLY | os.O_CREAT))
    monkeypatch.setattr(update_apply, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
    monkeypatch.setattr(update_recovery, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
    real_run = subprocess.run
    fake = _HungLaunchd("ai.mcs.b", "print", exc)
    fake.loaded = {"ai.mcs.a", "ai.mcs.b"}
    monkeypatch.setattr(
        mcs_update.subprocess, "run",
        lambda argv, *a, **k: (fake if argv[0] == "launchctl"
                               else real_run)(argv, *a, **k))
    clock = [time.monotonic()]
    monkeypatch.setattr(mcs_update, "time", SimpleNamespace(
        time=time.time, monotonic=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(update_apply, 'time', mcs_update.time)
    monkeypatch.setattr(update_cli, 'time', mcs_update.time)
    monkeypatch.setattr(update_execution, 'time', mcs_update.time)
    monkeypatch.setattr(update_lifecycle_context, 'time', mcs_update.time)
    monkeypatch.setattr(update_recovery, 'time', mcs_update.time)
    monkeypatch.setattr(update_repository, 'time', mcs_update.time)
    monkeypatch.setattr(update_restore, 'time', mcs_update.time)
    monkeypatch.setattr(update_runtime_migration, 'time', mcs_update.time)
    monkeypatch.setattr(update_services, 'time', mcs_update.time)
    monkeypatch.setattr(service_management, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(update_lifecycle_context, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_preflight, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_services, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS',
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_stray_drainer_pids", lambda: [])
    monkeypatch.setattr(update_services, '_stray_drainer_pids', mcs_update._stray_drainer_pids)
    monkeypatch.setattr(mcs_update, "_rollback_tree",
                        lambda e: pytest.fail("must not reset"))
    monkeypatch.setattr(update_apply, '_rollback_tree', mcs_update._rollback_tree)
    monkeypatch.setattr(update_recovery, '_rollback_tree', mcs_update._rollback_tree)
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    state = updater.journal_store.default_state()
    state["applied"] = [{"tag": "v1.1.0",
                         "sha": _git(repo, "rev-parse", "HEAD").stdout.strip(),
                         "prev_sha": _git(repo, "rev-list", "-n1", "v1.0.0").stdout.strip(),
                         "at": time.time()}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.rollback("cid-rb") == 1
    assert not any(verb == "bootstrap" for verb, _ in fake.calls)
    assert "ai.mcs.a" not in fake.loaded
    detail = updater.journal_store.load_state(updater.STATE_PATH)["executed"]["cid-rb"]["detail"]
    assert "drainer_stop_failed: ai.mcs.b" in detail
    assert "drainer_stop_failed:ai.mcs.b" in detail
    assert not os.path.exists(mcs_update.MARKER_PATH)
    assert len(fake.calls) < 100  # finite stop polling and restart retries


@pytest.mark.parametrize("kind", ["merge_head", "unclassifiable",
                                  "head_prev", "backup_gone"])
def test_non_git_escalation_inside_consent_hold_keeps_the_freeze(
        updater, tmp_path, monkeypatch, kind):
    """Journal inconsistency / restore failure inside a hold used to
    escalate: drainers restarted on the newer-schema DB, marker gone,
    receipt consumed and an outbox notice that voids the consent. Now
    the freeze holds, and once the condition clears the documented
    consent receipt permits the copy, but the old-schema reconcile remains held."""
    import notify_spool
    repo, live, back, before, after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    assert updater.rollback("cid-rb") == 2
    notices = []
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    undo = _hold_condition(repo, back, before, after, kind)
    live_before = Path(live).read_bytes()
    for _ in range(2):
        assert updater.recover_interrupted() == 1
    assert restarts == [] and notices == []
    assert Path(live).read_bytes() == live_before
    assert Path(mcs_update.MARKER_PATH).exists()
    assert notify_spool.restore_awaiting_consent(
        str(tmp_path / "data")) is not None
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state["restore_consent"] and state["applying"]["rollback"]
    assert "cid-rb" not in state.get("executed", {})
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "restore_consent_blocked"
    undo()
    _seed_consent(live, back)
    assert updater.recover_interrupted() == 1
    _assert_old_schema_restore_held(updater, live, back, restarts, "cid-rb")
    assert notices == []
    con = sqlite3.connect(live)
    assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    con.close()


@pytest.mark.parametrize("unreadable", [False, True])
def test_marker_only_hold_survives_git_failure(
        updater, tmp_path, monkeypatch, unreadable):
    """A hold entered before holds were journaled has only the
    awaiting_consent marker: a git failure must still keep the freeze
    (old code escalated — no restore_consent record) and the hold is
    recorded in the journal from the marker; after consent the old-schema
    copy succeeds while its notification reconciliation remains held."""
    repo, live, back, _b, _a, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    assert updater.rollback("cid-rb") == 2
    marker = tmp_path / "data" / "restore_pending.json"
    rid = json.loads(marker.read_text())["report_id"]
    if unreadable:
        marker.write_text("{broken")
    state = updater.journal_store.load_state(updater.STATE_PATH)
    del state["restore_consent"]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    notices = []
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    real_owned = owned_process.run_owned
    _hang_git(monkeypatch)
    assert updater.recover_interrupted() == 1
    assert restarts == [] and notices == []
    assert Path(mcs_update.MARKER_PATH).exists()
    state = updater.journal_store.load_state(updater.STATE_PATH)
    hold = state["restore_consent"]
    assert hold["from_marker"] is True
    assert hold["report_id"] == (None if unreadable else rid)
    assert "cid-rb" not in state.get("executed", {})
    assert json.loads(Path(mcs_update.REPORT_PATH).read_text())[
        "result"] == "restore_consent_blocked"
    if unreadable:
        return                  # only a human can repair the marker
    monkeypatch.setattr(owned_process, 'run_owned', real_owned)
    _seed_consent(live, back)
    assert updater.recover_interrupted() == 1
    _assert_old_schema_restore_held(updater, live, back, restarts, "cid-rb")
    assert notices == []


def test_rollback_applying_journal_shape():
    """rollback() and the consent hold share one journal shape — the
    one mcs_recover classifies as rollback_shaped (target = prev_sha)."""
    entry = {"tag": "v1.1.0", "sha": "b" * 40, "prev_sha": "a" * 40,
             "plugin_changed": True, "schema_bump": False,
             "backup_path": "/x.db", "manifest_snapshot": {},
             "command_id": "cid-apply"}
    rec = mcs_update._rollback_applying(entry, entry["sha"], "cid-rb")
    assert set(rec) == {"tag", "sha", "prev_sha", "rollback",
                        "plugin_changed", "schema_bump", "backup_path",
                        "manifest_snapshot", "command_id", "at"}
    assert (rec["tag"], rec["sha"], rec["prev_sha"], rec["rollback"],
            rec["command_id"]) == ("rollback:v1.1.0", "a" * 40, "b" * 40,
                                   True, "cid-rb")
    with pytest.raises(KeyError):
        mcs_update._rollback_applying({}, None, None)
