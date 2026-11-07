"""Synthetic mcs update testkit contracts; shared helpers preserve isolated runtime state."""
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
from ops_testkit import select_updater_service_python
import service_management
import owned_process
import update_discovery
import runtime_inspection
from dataclasses import replace
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
import pytest
import mcs_update
import mcs_util
from ops_testkit import _git, _make_repo, _mk_schema


_HANG_EXCS = [subprocess.TimeoutExpired(["launchctl"], 30),
              FileNotFoundError("launchctl")]


def _standalone_heartbeat(updater, **updates):
    value = {"pid": os.getpid(), "generation": "a" * 32, "updated_at": time.time(),
             "update_in_progress": False, "children": {
                 name: {"pid": os.getpid(), "kind": "background"} for name in ("extract-0", "extract-2", "extract-night")}}
    value.update(updates)
    path = Path(updater.DATA, "standalone-status.json")
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return value


@pytest.fixture
def updater(tmp_path, monkeypatch):
    """mcs_update pointed at temp dirs — REPORT_PATH included so a
    recovery path can never scribble on the real runtime."""
    import mcs_setup
    from test_runtime_compatibility import _facts
    runtime_home = tmp_path / "runtime"
    (runtime_home / "venv/bin").mkdir(parents=True)
    selected = runtime_home / "venv/bin/python3"
    selected.touch()
    selected.chmod(0o700)
    monkeypatch.setattr(mcs_setup, "HOME", str(runtime_home))
    monkeypatch.setattr(mcs_update, "HOME", str(runtime_home))
    monkeypatch.setattr(update_apply, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_cli, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_execution, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_lifecycle_context, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_preflight, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_recovery, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_runtime_migration, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(runtime_home / "config.json"))
    monkeypatch.setattr(mcs_setup, "HERMES_PY", str(selected))
    select_updater_service_python(monkeypatch, mcs_update, str(selected))
    agents = runtime_home / "agents"
    agents.mkdir()
    import plistlib
    (agents / "org.mcs.recovery.plist").write_bytes(plistlib.dumps(
        {"ProgramArguments": [str(selected), "synthetic-recovery.py"]}))
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(agents))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(agents))
    monkeypatch.setattr(update_lifecycle_context, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_preflight, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_services, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda exe: _facts())
    monkeypatch.setattr(mcs_update, "REPO", str(tmp_path / "repo"))
    monkeypatch.setattr(update_apply, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_cli, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_execution, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_repository, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_services, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(update_apply, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_lifecycle_context, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_planning, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_preflight, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_recovery, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_restore, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_services, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(mcs_update, "STATE_PATH",
                        str(tmp_path / "data" / "update_state.json"))
    monkeypatch.setattr(update_apply, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_cli, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_execution, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_lifecycle_context, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_planning, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_preflight, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_recovery, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_restore, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_runtime_migration, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(mcs_update, "UPDATE_LOCK",
                        str(tmp_path / "data" / "update.lock"))
    monkeypatch.setattr(update_lifecycle_context, 'UPDATE_LOCK', mcs_update.UPDATE_LOCK)
    monkeypatch.setattr(update_runtime_migration, 'UPDATE_LOCK', mcs_update.UPDATE_LOCK)
    monkeypatch.setattr(mcs_update, "RUN_LOCK",
                        str(tmp_path / "data" / "run.lock"))
    monkeypatch.setattr(update_lifecycle_context, 'RUN_LOCK', mcs_update.RUN_LOCK)
    monkeypatch.setattr(update_runtime_migration, 'RUN_LOCK', mcs_update.RUN_LOCK)
    monkeypatch.setattr(mcs_update, "MARKER_PATH",
                        str(tmp_path / "data" / "marker"))
    monkeypatch.setattr(update_lifecycle_context, 'MARKER_PATH', mcs_update.MARKER_PATH)
    monkeypatch.setattr(update_runtime_migration, 'MARKER_PATH', mcs_update.MARKER_PATH)
    monkeypatch.setattr(update_services, 'MARKER_PATH', mcs_update.MARKER_PATH)
    monkeypatch.setattr(mcs_update, "REPORT_PATH",
                        str(tmp_path / "data" / "recovery_report.json"))
    monkeypatch.setattr(update_lifecycle_context, 'REPORT_PATH', mcs_update.REPORT_PATH)
    monkeypatch.setattr(mcs_update, "RESTORE_REPORT_PATH",
                        str(tmp_path / "data" / "restore_report.json"))
    monkeypatch.setattr(update_restore, 'RESTORE_REPORT_PATH', mcs_update.RESTORE_REPORT_PATH)
    monkeypatch.setattr(mcs_update, "LEDGER", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(update_apply, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(mcs_update, "BACKUP_DIR",
                        str(tmp_path / "data" / "backups"))
    monkeypatch.setattr(mcs_update, "MANIFEST_PATH",
                        str(tmp_path / "data" / "service_manifest.json"))
    monkeypatch.setattr(update_execution, 'MANIFEST_PATH', mcs_update.MANIFEST_PATH)
    monkeypatch.setattr(update_lifecycle_context, 'MANIFEST_PATH', mcs_update.MANIFEST_PATH)
    os.makedirs(tmp_path / "data", exist_ok=True)
    return mcs_update


@pytest.fixture
def prepared_run_log(updater, tmp_path, monkeypatch):
    """Prepare and verify the real log helper only inside this test's runtime."""
    import run_log_install

    data = Path(updater.HOME) / "data"
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    recovery = tmp_path / "run-log-recovery"
    original_paths = updater._service_paths
    monkeypatch.setattr(updater, "_service_paths", lambda: replace(
        original_paths(), recovery_dir=str(recovery)))
    monkeypatch.setattr(update_execution, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_lifecycle_context, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_planning, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_preflight, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_recovery, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_restore, '_service_paths', updater._service_paths)
    source = updater._RUN_LOG_SOURCE
    assert isinstance(source, bytes) and not updater._RUN_LOG_SOURCE_ERROR
    updater._prepare_run_log()
    helper = Path(run_log_install.helper_path(
        updater.HOME, str(recovery), require_installed=True))
    assert helper.read_bytes() == source
    assert (data / "mcs.log").is_file()
    return helper


def _cron_listing(script, ansi=False):
    """Synthetic `hermes cron list --all` in the hermes_cli/cron.py layout:
    `  <id> [badge]` then `    <Label:><pad to 11><value>` rows."""
    jid, badge = ("\x1b[33mabcdef\x1b[0m", "\x1b[31m[disabled]\x1b[0m") if ansi \
        else ("abcdef", "[disabled]")
    return (f"Scheduled Jobs (profile: default)\n\n  {jid} {badge}\n"
            f"    Name:      synthetic-job\n    Schedule:  */10 * * * *\n"
            f"    Repeat:    \u221e\n    Script:    {script}\n\n")


# ------------------------------------------------------------ receipts

def _rec(cid, cmd, at, **kw):
    return json.dumps({"cmd": cmd, "scheduled": True,
                       "command_id": cid, **kw})


# ------------------------------------------------------------- apply path

def _apply_env(updater, monkeypatch, *, precheck_errors=(),
               cfg=None, sha=None):
    """Stub every apply() external boundary; the temp repo + real
    tags remain real (fetch/rev-parse/merge hit the file remote)."""
    monkeypatch.setattr(updater, "_provider_migration_needed", lambda: False)
    monkeypatch.setattr(update_apply, '_provider_migration_needed', updater._provider_migration_needed)
    monkeypatch.setattr(update_recovery, '_provider_migration_needed', updater._provider_migration_needed)
    repo, _ = _make_repo(Path(mcs_update.DATA).parent)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "precheck_local", lambda c: [])
    monkeypatch.setattr(update_apply, 'precheck_local', mcs_update.precheck_local)
    monkeypatch.setattr(update_planning, 'precheck_local', mcs_update.precheck_local)
    monkeypatch.setattr(mcs_update, "precheck_tag",
                        lambda t: list(precheck_errors))
    monkeypatch.setattr(update_execution, 'precheck_tag', mcs_update.precheck_tag)
    monkeypatch.setattr(update_planning, 'precheck_tag', mcs_update.precheck_tag)
    monkeypatch.setattr(update_discovery, 'remote_tag_sha',
                        lambda t, **_lif05_paths: sha)
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: cfg or {})
    monkeypatch.setattr(mcs_update, "_baseline_check", lambda c: [])
    monkeypatch.setattr(update_apply, '_baseline_check', mcs_update._baseline_check)
    monkeypatch.setattr(mcs_update, "quiesce", lambda: [])
    monkeypatch.setattr(update_apply, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(mcs_update, "restart_agents", lambda: [])
    monkeypatch.setattr(update_apply, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(update_recovery, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(mcs_update, "_postcheck", lambda s, e: [])
    monkeypatch.setattr(update_recovery, '_postcheck', mcs_update._postcheck)
    monkeypatch.setattr(update_runtime_migration, '_postcheck', mcs_update._postcheck)
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)
    monkeypatch.setattr(update_services, 'restart_gateway', mcs_update.restart_gateway)
    import maintenance
    monkeypatch.setattr(maintenance, "preupdate_backup",
                        lambda db: str(db) + ".bak")
    return repo


def _rollback_journal(updater, backup):
    """The production precondition of a DB replace: a rollback-shaped
    applying journal (apply bail / rollback() / recover all write one)."""
    state = updater.journal_store.load_state(updater.STATE_PATH)
    state["applying"] = {"tag": "v1.1.0", "sha": "b" * 40, "prev_sha": "a" * 40,
                         "rollback": True, "backup_path": str(backup), "at": time.time()}
    updater.journal_store.save_state(updater.STATE_PATH, state)


def _journal_kept(updater):
    applying = updater.journal_store.load_state(updater.STATE_PATH)["applying"]
    assert applying["rollback"] is True and "restore_staging" not in applying


# ------------------------------------------------- restore consent gate
# R2/T13: a schema-bump rollback that would replace the live DB must
# wait for a NEW human approval bound to the exact backup bytes +
# schema + loss report. An earlier update/rollback approval never
# substitutes; every writer/sender stays frozen while it waits.

def _schema_bump_world(updater, tmp_path, monkeypatch):
    """applied v1.1.0 entry flagged schema_bump with a real v7 backup;
    live DB sits one schema ahead at v8. Every external boundary is
    stubbed; git reset and the DB files stay real."""
    # Provider migration has separate hold tests; this fixture isolates DB rollback.
    monkeypatch.setattr(updater, "_provider_migration_needed", lambda: False)
    monkeypatch.setattr(update_apply, '_provider_migration_needed', updater._provider_migration_needed)
    monkeypatch.setattr(update_recovery, '_provider_migration_needed', updater._provider_migration_needed)
    repo, _ = _make_repo(tmp_path)
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
    before = _git(repo, "rev-parse", "v1.0.0").stdout.strip()
    after = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    live = str(tmp_path / "data" / "ledger.db")   # under DATA — the
    # marker/data_root paths must coincide like production
    monkeypatch.setattr(mcs_update, "LEDGER", live)
    monkeypatch.setattr(update_apply, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', mcs_update.LEDGER)
    back = tmp_path / "data" / "backups" / "preupdate.db"
    back.parent.mkdir(parents=True, exist_ok=True)
    _mk_schema(back, 7, messages=2)
    _mk_schema(live, 8, messages=5)          # 3 rows since the backup
    # keep quiesce's real marker write — only launchd stops are stubbed
    monkeypatch.setattr(mcs_update, "quiesce",
                        lambda: (mcs_update._write_marker(), [])[1])
    monkeypatch.setattr(update_apply, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(update_recovery, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(service_management, 'retire_to_snapshot', lambda m, **_service_kwargs: [])
    monkeypatch.setattr(mcs_update, "_postcheck", lambda s, e: [])
    monkeypatch.setattr(update_recovery, '_postcheck', mcs_update._postcheck)
    monkeypatch.setattr(update_runtime_migration, '_postcheck', mcs_update._postcheck)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)
    monkeypatch.setattr(update_services, 'restart_gateway', mcs_update.restart_gateway)
    restarts = []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda: restarts.append(1) or [])
    monkeypatch.setattr(update_apply, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', mcs_update.restart_agents)
    state = updater.journal_store.default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": after,
                         "prev_sha": before, "schema_bump": True,
                         "backup_path": str(back), "at": time.time()}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    return repo, live, str(back), before, after, restarts


def _live_version(path):
    con = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def _assert_old_schema_restore_held(updater, live, back, restarts, command_id,
                                    prior_execution=None):
    """Approved copy preserves old schema while notification reconcile stays held."""
    import notify_spool
    assert _live_version(live) == 7
    assert restarts == []
    assert Path(updater.MARKER_PATH).exists()
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state["applying"]["rollback"] is True
    assert state["applying"]["restore_incomplete"] is True
    assert state["restore_consent"]
    if prior_execution is None:
        assert command_id not in state.get("executed", {})
    else:
        assert state["executed"][command_id] == prior_execution
    marker = notify_spool.restore_pending(updater.DATA)
    assert marker is not None
    assert marker["phase"] == "restored"
    assert marker["backup_path"] == back
    report = json.loads(Path(updater.RESTORE_REPORT_PATH).read_text())
    assert marker["report_id"] == report["report_id"]
    assert report["backup_schema"] == 7
    assert report["backup_sha256"] == updater._file_sha256(back)
    connection = sqlite3.connect("file:" + live + "?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    finally:
        connection.close()


# ---------------------------------------- hung / missing launchctl (H4)
# A launchctl that hangs past T_GIT (TimeoutExpired) or cannot start
# (FileNotFoundError) for ONE label must be recorded as that label's
# problem; the loop still reaches every other agent and the marker ends
# where the spec requires — never an uncaught raise mid-restart.

class _HungLaunchd:
    """launchctl stub: `verb` for any argv mentioning `label` raises
    `exc`; everything else behaves (bootstrap loads, print shows pid)."""

    def __init__(self, label, verb, exc):
        self.label, self.verb, self.exc = label, verb, exc
        self.loaded = set()
        self.calls = []

    def __call__(self, argv, *args, **kwargs):
        verb, target = argv[1], argv[-1]
        label = os.path.basename(target).removesuffix(".plist") \
            if verb == "bootstrap" else target.rsplit("/", 1)[-1]
        self.calls.append((verb, label))
        if verb == self.verb and label == self.label:
            raise self.exc
        rc, out = 0, ""
        if verb == "bootout":
            self.loaded.discard(label)
        elif verb == "bootstrap":
            self.loaded.add(label)
        elif verb == "print":
            rc = 0 if label in self.loaded else 113
            out = "\tpid = 4242\n" if rc == 0 else ""
        return subprocess.CompletedProcess(argv, rc, out, "")


# ------------------------------- git failure inside recover_interrupted
# _head_sha/_tree_clean/reset/merge --abort raise UpdateError; recover
# must decide instead of escaping with drainers down and the marker up.

def _hang_git(monkeypatch):
    # _git's process boundary is the owned-session runner
    real_owned = owned_process.run_owned

    def run(argv, timeout, env, stdin=None):
        if argv[0] == "git":
            raise subprocess.TimeoutExpired(argv, timeout)
        return real_owned(argv, timeout, env, stdin)
    monkeypatch.setattr(owned_process, 'run_owned', run)


class _EnsureLaunchd:
    """launchctl stub with per-label loaded/running state; labels in
    `hung` time out on every verb, labels in `inert` load but never
    get a pid."""

    def __init__(self, loaded=(), running=(), hung=(), inert=()):
        self.loaded, self.running = set(loaded), set(running)
        self.hung, self.inert = set(hung), set(inert)
        self.calls = []

    def __call__(self, argv, *args, **kwargs):
        verb, target = argv[1], argv[-1]
        label = os.path.basename(target).removesuffix(".plist") \
            if verb == "bootstrap" else target.rsplit("/", 1)[-1]
        self.calls.append((verb, label))
        if label in self.hung:
            raise subprocess.TimeoutExpired(argv, 10)
        rc, out = 0, ""
        if verb == "bootout":
            self.loaded.discard(label)
            self.running.discard(label)
        elif verb in ("bootstrap", "kickstart"):
            if verb == "bootstrap":
                self.loaded.add(label)
            if label in self.loaded and label not in self.inert:
                self.running.add(label)
        elif verb == "print":
            rc = 0 if label in self.loaded else 113
            out = "\tpid = 4242\n" if label in self.running else ""
        return subprocess.CompletedProcess(argv, rc, out, "")


# --------- every escalation inside a consent hold keeps the freeze

def _hold_condition(repo, back, before, after, kind):
    """Apply one non-git escalation condition; returns its undo."""
    if kind == "merge_head":
        path = repo / ".git" / "MERGE_HEAD"
        path.write_text(after + "\n")
        return path.unlink
    if kind == "unclassifiable":
        _git(repo, "commit", "--allow-empty", "-qm", "drift")
        return lambda: _git(repo, "reset", "-q", "--hard", before)
    if kind == "head_prev":
        _git(repo, "reset", "-q", "--hard", after)
        return lambda: _git(repo, "reset", "-q", "--hard", before)
    moved = back + ".away"                           # backup_invalid
    os.rename(back, moved)
    return lambda: os.rename(moved, back)
