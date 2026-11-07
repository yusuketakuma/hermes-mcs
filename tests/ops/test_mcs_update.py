"""Synthetic mcs update contracts; shared helpers preserve isolated runtime state."""
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
import environment_diagnostics
import scheduled_jobs
import service_management
import owned_process
import update_discovery
import runtime_inspection
import json
import os
import py_compile
import subprocess
import sys
import time
from pathlib import Path
import pytest
import mcs_update
import mcs_util
from ops_testkit import _git, _make_repo, _mk_schema, _receipts_db, _seed_consent as _seed_consent
from mcs_update_testkit import _EnsureLaunchd as _EnsureLaunchd, _HANG_EXCS as _HANG_EXCS, _HungLaunchd as _HungLaunchd, _apply_env as _apply_env, _assert_old_schema_restore_held as _assert_old_schema_restore_held, _cron_listing as _cron_listing, _hang_git as _hang_git, _hold_condition as _hold_condition, _journal_kept as _journal_kept, _live_version as _live_version, _rec as _rec, _rollback_journal as _rollback_journal, _schema_bump_world as _schema_bump_world, _standalone_heartbeat as _standalone_heartbeat, prepared_run_log as prepared_run_log, updater as updater


@pytest.mark.parametrize('native,failed', [(True, False), (True, True), (False, False)])
def test_post_merge_provider_migration_precedes_services(updater, monkeypatch, native, failed):
    calls = []
    state = updater.journal_store.default_state()
    state['applying'] = {'tag': 'v1.1.0', 'sha': 'a' * 40, 'prev_sha': 'b' * 40,
                         **({'release_ref': {'synthetic': True}} if native else {})}
    def migrate(root):
        assert root == updater.HOME
        calls.append('migrate')
        if failed:
            raise updater.update_migrations.MigrationError('synthetic')
        return True
    monkeypatch.setattr(updater.update_migrations, 'migrate_provider_settings', migrate)
    monkeypatch.setattr(updater, '_plan_runtime_migration', lambda *_args: None)
    monkeypatch.setattr(update_apply, '_plan_runtime_migration', updater._plan_runtime_migration)
    monkeypatch.setattr(update_runtime_migration, '_plan_runtime_migration', updater._plan_runtime_migration)
    monkeypatch.setattr(updater, '_prepare_run_log', lambda **_kwargs: calls.append('run_log'))
    monkeypatch.setattr(update_runtime_migration, '_prepare_run_log', updater._prepare_run_log)
    monkeypatch.setattr(updater, '_services_reconcile', lambda: calls.append('services'))
    monkeypatch.setattr(update_recovery, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', updater._services_reconcile)
    monkeypatch.setattr(updater, 'restart_agents', lambda: calls.append('restart') or [])
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(updater, '_postcheck', lambda *_args: [])
    monkeypatch.setattr(update_recovery, '_postcheck', updater._postcheck)
    monkeypatch.setattr(update_runtime_migration, '_postcheck', updater._postcheck)
    monkeypatch.setattr(updater, 'journal', lambda *_args: None)
    monkeypatch.setattr(update_apply, 'journal', updater.journal)
    monkeypatch.setattr(update_recovery, 'journal', updater.journal)
    monkeypatch.setattr(update_runtime_migration, 'journal', updater.journal)
    monkeypatch.setattr(updater.journal_store, 'save_state', lambda *_args: None)
    monkeypatch.setattr(updater, '_enqueue_notice', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(update_apply, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', updater._enqueue_notice)
    if failed:
        with pytest.raises(updater.UpdateError, match='provider_settings_migration_unverified'):
            updater._post_merge(state, native_provider_migration=True)
        assert calls == ['migrate'] and state['applying'] is not None
    else:
        assert updater._post_merge(state, native_provider_migration=native) == 0
        assert calls == (['migrate'] if native else []) + ['run_log', 'services', 'restart']


def test_repo_fixture_clones_main_with_master_default(tmp_path, monkeypatch):
    """Fixture HEAD must be valid independently of the host Git default."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "init.defaultBranch")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "master")
    repo, bare = _make_repo(tmp_path)
    assert _git(bare, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
    assert _git(repo, "branch", "--show-current").stdout.strip() == "main"
    assert (_git(repo, "rev-parse", "HEAD").stdout
            == _git(repo, "rev-parse", "v1.1.0^{commit}").stdout)
    assert (repo / "f.txt").read_text() == "two"


def test_standalone_precheck_does_not_require_hermes(updater, monkeypatch):
    monkeypatch.setattr(updater, "_tree_clean", lambda: True)
    monkeypatch.setattr(update_apply, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_execution, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_preflight, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_recovery, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_restore, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_runtime_migration, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(runtime_inspection, "python_problem", lambda exe: None)
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda cfg: pytest.fail("Hermes lookup forbidden"))
    assert updater.precheck_local({"runtime_mode": "standalone", "mcs_login_id": "u", "notify_target": "local"}) == []


@pytest.mark.parametrize("mode", ["hermes", "standalone"])
def test_runtime_venv_accepts_old_checkout_without_log_helper(updater, mode):
    cfg = {"runtime_mode": mode}
    selected = service_management.selected_python(cfg, paths=updater._service_paths())
    venv = Path(selected).parent.parent
    checkout = venv.parent if mode == "hermes" else None
    if checkout is not None:
        (checkout / ".git").mkdir(parents=True)
    assert not Path(updater.REPO, "mcs", "core", "run_log.py").exists()
    assert updater._runtime_venv(cfg) == (str(venv), str(checkout) if checkout else None)


def test_standalone_update_requests_restart_for_the_observed_generation(updater):
    value = _standalone_heartbeat(updater)
    updater.restart_gateway({"runtime_mode": "standalone"})
    path = Path(updater.DATA, "standalone-restart.request")
    request = json.loads(path.read_text())
    assert request["generation"] == value["generation"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_standalone_refuses_rollback_to_a_tag_without_runtime(updater, monkeypatch):
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {"runtime_mode": "standalone"})
    calls = []
    monkeypatch.setattr(updater, "_git", lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 1, "", ""))
    monkeypatch.setattr(update_apply, '_git', updater._git)
    monkeypatch.setattr(update_cli, '_git', updater._git)
    monkeypatch.setattr(update_planning, '_git', updater._git)
    monkeypatch.setattr(update_preflight, '_git', updater._git)
    monkeypatch.setattr(update_recovery, '_git', updater._git)
    monkeypatch.setattr(update_repository, '_git', updater._git)
    monkeypatch.setattr(update_restore, '_git', updater._git)
    monkeypatch.setattr(update_runtime_migration, '_git', updater._git)
    with pytest.raises(updater.UpdateError, match="standalone_runtime_missing_in_target"):
        updater._rollback_tree({"prev_sha": "0" * 40})
    assert not any("reset" in call for call in calls)


@pytest.mark.parametrize("bad", [{"updated_at": 0}, {"pid": -1}, {"children": {"extract-0": []}}])
def test_standalone_heartbeat_is_evidence_not_an_assumption(updater, bad):
    _standalone_heartbeat(updater, **bad)
    assert updater._standalone_status() is None


# ---------------------------------------------------------------- state

def test_state_roundtrip_and_corrupt(updater, tmp_path):
    state = updater.journal_store.default_state()
    state["stages"].append({"stage": "local_checks", "at": 1.0})
    updater.journal_store.save_state(updater.STATE_PATH, state)
    loaded = updater.journal_store.load_state(updater.STATE_PATH)
    assert loaded["stages"][0]["stage"] == "local_checks"
    assert loaded["v"] == 1
    (tmp_path / "data" / "update_state.json").write_text("{not json")
    assert updater.journal_store.load_state(updater.STATE_PATH).get("_corrupt") is True


@pytest.mark.parametrize("field,value", [
    ("v", True), ("stages", {}), ("stages", [None]),
    ("stages", [{"stage": "merge", "at": float("nan")}]),
    ("applying", []), ("applying", {"at": float("inf")}),
    ("applied", ["invalid"]), ("executed", []), ("attempts", []),
])
def test_corrupt_journal_shapes_are_rejected_by_both_readers(
        updater, tmp_path, monkeypatch, field, value):
    from ops_testkit import _load
    recovery = _load()
    monkeypatch.setattr(recovery.mcs_recovery_core, "STATE_PATH", updater.STATE_PATH)
    state = updater.journal_store.default_state()
    state[field] = value
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.journal_store.load_state(updater.STATE_PATH) == {"_corrupt": True}
    assert recovery.mcs_recovery_core._load_state() == {"_corrupt": True}


def test_update_lock_nonblocking(updater):
    fd = updater.acquire_update_lock()
    assert fd is not None
    assert updater.acquire_update_lock() is None
    os.close(fd)
    reopened = updater.acquire_update_lock()
    assert reopened is not None
    os.close(reopened)


# ------------------------------------------------------------ tag parsing

def test_remote_tag_sha_annotated_and_lightweight(updater, tmp_path):
    repo, _ = _make_repo(tmp_path)
    monkeypatch_repo = str(repo)
    import mcs_update as mu
    mu.REPO = monkeypatch_repo
    lite = update_discovery.remote_tag_sha("v1.0.0", checkout=mcs_update.REPO, environment=mcs_update.os.environ)
    ann = update_discovery.remote_tag_sha("v1.1.0", checkout=mcs_update.REPO, environment=mcs_update.os.environ)
    commit_lite = _git(repo, "rev-list", "-n1", "v1.0.0").stdout.strip()
    commit_ann = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    assert lite == commit_lite           # lightweight: direct = commit
    assert ann == commit_ann             # annotated: peeled = commit
    assert ann != _git(repo, "rev-parse", "v1.1.0").stdout.strip()


def test_detect_latest_semver_max(updater, tmp_path):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    tag, sha = update_discovery.detect_latest(checkout=mcs_update.REPO, environment=mcs_update.os.environ)
    assert tag == "v1.1.0"
    assert len(sha) == 40


def test_malformed_approval_cannot_break_queue_or_veto_valid_apply(updater, tmp_path):
    con = _receipts_db(updater.LEDGER)
    receipts = [
        ("valid", {"cmd": "ops.update_apply", "scheduled": True,
                   "tag": "v1.2.0", "target_sha": "a" * 40}),
        ("bad", {"cmd": "ops.update_apply", "scheduled": True,
                 "tag": ["invalid"], "target_sha": "a" * 40}),
        ("unscheduled", {"cmd": "ops.update_rollback", "scheduled": False}),
    ]
    for timestamp, (cid, receipt) in enumerate(receipts):
        con.execute("INSERT INTO command_receipts VALUES(?,?,NULL,NULL,'applied',?,?)",
                    (cid, "h" * 64, json.dumps(receipt), timestamp))
    con.commit()
    con.close()
    candidates, consumed = updater.scan_pending_approvals(updater.journal_store.default_state())
    assert [candidate["command_id"] for candidate in candidates] == ["valid"]
    assert consumed == []


def test_defuse_mentions():
    out = mcs_update.update_policy.defuse_mentions(
        "ping @everyone <@123> <@&456> <#789> @here")
    assert "@everyone" not in out and "@here" not in out
    assert "<@123>" not in out and "<#789>" not in out
    assert "＠everyone" in out


def test_impact_summary(updater, tmp_path):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    cur = _git(repo, "rev-parse", "HEAD~0").stdout.strip()
    # tag touches only f.txt — no impact flags
    assert mcs_update.impact_summary(cur, "v1.1.0") == []


@pytest.mark.parametrize("path,gateway,lineworks", [
    ("hermes_plugin/card_workers.py", True, False),
    ("adapters/common/worker.py", True, True),
    ("adapters/slack/actions.py", True, False),
    ("adapters/discord/delivery.py", True, False),
    ("adapters/lineworks/actions.py", False, True),
    ("lineworks_adapter/__main__.py", False, True),
    ("adapters/README.md", False, False),
])
def test_impact_summary_distinguishes_gateway_and_independent_adapter(
        updater, monkeypatch, path, gateway, lineworks):
    monkeypatch.setattr(updater, "_git_out", lambda args: "M\t" + path + "\n")
    monkeypatch.setattr(update_apply, '_git_out', updater._git_out)
    monkeypatch.setattr(update_planning, '_git_out', updater._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', updater._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', updater._git_out)
    monkeypatch.setattr(update_repository, '_git_out', updater._git_out)
    monkeypatch.setattr(update_restore, '_git_out', updater._git_out)
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: {"notify": {"interactive": "lineworks"}})
    impact = updater.impact_summary("synthetic-before", "v1.1.0")
    assert any("通知接続（gateway）の再起動" in line for line in impact) is gateway
    assert any("LINE WORKS" in line and "再起動" in line for line in impact) is lineworks
    # without the LINE WORKS adapter (plan() restarts none) there is no note
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    impact = updater.impact_summary("synthetic-before", "v1.1.0")
    assert not any("LINE WORKS" in line for line in impact)


# ------------------------------------------------------- protected paths

@pytest.mark.parametrize('changed', ['deployment/sdc-release-lock.json', 'deployment/sdc-wheels/synthetic.whl'])
def test_precheck_tag_rejects_dependency_lock_changes(updater, tmp_path, monkeypatch, changed):
    _mk_schema(tmp_path / 'ledger.db', 8)
    monkeypatch.setattr(updater, 'LEDGER', str(tmp_path / 'ledger.db'))
    monkeypatch.setattr(update_apply, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(updater, '_ls_tree_paths', lambda *args: [])
    monkeypatch.setattr(update_preflight, '_ls_tree_paths', updater._ls_tree_paths)
    monkeypatch.setattr(update_repository, '_ls_tree_paths', updater._ls_tree_paths)
    def git(argv):
        denied = argv[:2] == ['diff', '--quiet'] and changed.startswith(tuple(argv[argv.index('--') + 1:]))
        return subprocess.CompletedProcess(argv, int(denied), '', '')
    monkeypatch.setattr(updater, '_git', git)
    monkeypatch.setattr(update_apply, '_git', updater._git)
    monkeypatch.setattr(update_cli, '_git', updater._git)
    monkeypatch.setattr(update_planning, '_git', updater._git)
    monkeypatch.setattr(update_preflight, '_git', updater._git)
    monkeypatch.setattr(update_recovery, '_git', updater._git)
    monkeypatch.setattr(update_repository, '_git', updater._git)
    monkeypatch.setattr(update_restore, '_git', updater._git)
    monkeypatch.setattr(update_runtime_migration, '_git', updater._git)
    monkeypatch.setattr(updater, '_git_out', lambda argv: 'SCHEMA_VERSION = 8' if argv[0] == 'show' else '')
    monkeypatch.setattr(update_apply, '_git_out', updater._git_out)
    monkeypatch.setattr(update_planning, '_git_out', updater._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', updater._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', updater._git_out)
    monkeypatch.setattr(update_repository, '_git_out', updater._git_out)
    monkeypatch.setattr(update_restore, '_git_out', updater._git_out)
    monkeypatch.setattr(updater.subprocess, 'run', lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, '[]', ''))
    assert 'release_dependency_lock_changed' in updater.precheck_tag('v1.1.0')


def test_precheck_tag_rejects_protected_and_symlink(updater, tmp_path,
                                                    monkeypatch):
    repo, _ = _make_repo(tmp_path)
    work = tmp_path / "remote-work"
    (work / "data").mkdir(exist_ok=True)
    (work / "data" / "evil").write_text("x")
    (work / "link.sh").unlink(missing_ok=True)
    os.symlink("f.txt", work / "link.sh")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "evil")
    _git(work, "tag", "v9.9.9")
    _git(work, "push", "-q", str(tmp_path / "remote.git"),
         "main", "v9.9.9")
    mcs_update.REPO = str(repo)
    _git(repo, "fetch", "-q", "--tags")
    errors = mcs_update.precheck_tag("v9.9.9")
    assert any("protected_path" in e for e in errors)
    assert any("bad_entry_type" in e for e in errors)


def test_precheck_schema_uses_exact_live_uri_path(updater, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger#?%23.db"
    _mk_schema(live, 8)
    _mk_schema(tmp_path / "data" / "ledger", 9)
    monkeypatch.setattr(updater, "LEDGER", str(live))
    monkeypatch.setattr(update_apply, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(updater, "_ls_tree_paths", lambda *args: [])
    monkeypatch.setattr(update_preflight, '_ls_tree_paths', updater._ls_tree_paths)
    monkeypatch.setattr(update_repository, '_ls_tree_paths', updater._ls_tree_paths)
    monkeypatch.setattr(updater, "_git", lambda argv:
                        subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(update_apply, '_git', updater._git)
    monkeypatch.setattr(update_cli, '_git', updater._git)
    monkeypatch.setattr(update_planning, '_git', updater._git)
    monkeypatch.setattr(update_preflight, '_git', updater._git)
    monkeypatch.setattr(update_recovery, '_git', updater._git)
    monkeypatch.setattr(update_repository, '_git', updater._git)
    monkeypatch.setattr(update_restore, '_git', updater._git)
    monkeypatch.setattr(update_runtime_migration, '_git', updater._git)
    monkeypatch.setattr(updater, "_git_out", lambda argv:
                        "SCHEMA_VERSION = 8" if argv[0] == "show" else "")
    monkeypatch.setattr(update_apply, '_git_out', updater._git_out)
    monkeypatch.setattr(update_planning, '_git_out', updater._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', updater._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', updater._git_out)
    monkeypatch.setattr(update_repository, '_git_out', updater._git_out)
    monkeypatch.setattr(update_restore, '_git_out', updater._git_out)
    monkeypatch.setattr(updater.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "[]", ""))

    assert updater.precheck_tag("v1.1.0") == []


def test_precheck_schema_unreadable_ledger_is_unknown_not_zero(
        updater, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger.db"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b"not a sqlite database" * 64)
    monkeypatch.setattr(updater, "LEDGER", str(live))
    monkeypatch.setattr(update_apply, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(updater, "_ls_tree_paths", lambda *args: [])
    monkeypatch.setattr(update_preflight, '_ls_tree_paths', updater._ls_tree_paths)
    monkeypatch.setattr(update_repository, '_ls_tree_paths', updater._ls_tree_paths)
    monkeypatch.setattr(updater, "_git", lambda argv:
                        subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(update_apply, '_git', updater._git)
    monkeypatch.setattr(update_cli, '_git', updater._git)
    monkeypatch.setattr(update_planning, '_git', updater._git)
    monkeypatch.setattr(update_preflight, '_git', updater._git)
    monkeypatch.setattr(update_recovery, '_git', updater._git)
    monkeypatch.setattr(update_repository, '_git', updater._git)
    monkeypatch.setattr(update_restore, '_git', updater._git)
    monkeypatch.setattr(update_runtime_migration, '_git', updater._git)
    monkeypatch.setattr(updater, "_git_out", lambda argv:
                        "SCHEMA_VERSION = 8" if argv[0] == "show" else "")
    monkeypatch.setattr(update_apply, '_git_out', updater._git_out)
    monkeypatch.setattr(update_planning, '_git_out', updater._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', updater._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', updater._git_out)
    monkeypatch.setattr(update_repository, '_git_out', updater._git_out)
    monkeypatch.setattr(update_restore, '_git_out', updater._git_out)
    monkeypatch.setattr(updater.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "[]", ""))

    errors = updater.precheck_tag("v1.1.0")
    assert "schema_version_unknown" in errors
    assert not any(e.startswith("schema_bump:") for e in errors)


@pytest.mark.parametrize(("output", "problems", "removed"), [
    ("ansi", [], True),
    ("No scheduled jobs in profile 'default'.\nCreate one with 'hermes cron create ...'", [], False),
    ("Jobs:\n  - id=abcdef script=mcs_backup.sh\n", ["cron_list_unverifiable"], False),
])
def test_cron_reconcile_shares_the_setup_parser(updater, monkeypatch, output, problems, removed):
    """Coloured ids, the per-profile empty message and a format drift go
    through service_management.inspect_cron: drift is reported, never silently skipped."""
    script = scheduled_jobs.CRON_JOBS[0][2]
    if output == "ansi":
        output = _cron_listing(f"{updater.SCRIPTS_DIR}/{script}", ansi=True)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda cfg: "synthetic-hermes")
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda path: True)
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, output if argv[2] == "list" else "", "")
    monkeypatch.setattr(updater.subprocess, "run", execute)
    monkeypatch.setattr(owned_process, "run_owned", execute)
    assert service_management.retire_to_snapshot({'cron': [], 'agents': []}, cfg=mcs_util.load_config(), paths=updater._service_paths()) == problems
    assert (["synthetic-hermes", "cron", "remove", "abcdef"] in calls) is removed


@pytest.mark.parametrize("returncode", [0, 1])
def test_cron_job_removal_requires_successful_list(updater, monkeypatch, returncode):
    script = scheduled_jobs.CRON_JOBS[0][2]
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda cfg: "synthetic-hermes")
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda path: True)
    calls = []

    def list_result(argv, **kwargs):
        calls.append(argv)
        if argv[1:] == ["cron", "list", "--all"]:
            return subprocess.CompletedProcess(
                argv, returncode,
                _cron_listing(f"{updater.SCRIPTS_DIR}/{script}"),
                "synthetic listing failure")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(updater.subprocess, "run", list_result)
    monkeypatch.setattr(owned_process, "run_owned", list_result)
    problems = service_management.retire_to_snapshot({'cron': [], 'agents': []}, cfg=mcs_util.load_config(), paths=updater._service_paths())
    assert problems == (["cron_list_unverifiable"] if returncode != 0 else [])
    expected = [["synthetic-hermes", "cron", "list", "--all"]]
    if returncode == 0:
        expected.append(["synthetic-hermes", "cron", "remove", "abcdef"])
    assert calls == expected


def test_stale_git_lock_cleanup(updater, tmp_path):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    now = time.time()                        # the clock the code reads
    os.utime(lock, (now, now))
    # a FRESH lock may belong to a live git process — kept (H3)
    assert updater._clean_stale_git_locks() == []
    assert lock.exists()
    old = time.time() - mcs_update.GIT_LOCK_MIN_AGE_S - 60
    os.utime(lock, (old, old))
    assert updater._clean_stale_git_locks() == [str(lock)]
    assert not lock.exists()


def test_scan_pending_newest_tag_and_veto(updater, tmp_path):
    db = tmp_path / "ledger.db"
    con = _receipts_db(db)
    sha = "a" * 40
    rows = [
        ("cid-old", _rec("cid-old", "ops.update_apply", 1,
                         tag="v1.0.0", target_sha=sha, base_sha=sha), 1),
        ("cid-new", _rec("cid-new", "ops.update_apply", 2,
                         tag="v1.2.0", target_sha=sha, base_sha=sha), 2),
        ("cid-rb", _rec("cid-rb", "ops.update_rollback", 3), 3),
    ]
    con.executemany(
        "INSERT INTO command_receipts VALUES(?,?,NULL,NULL,'applied',?,?)",
        [(c, "h" * 64, j, t) for c, j, t in rows])
    con.commit()
    con.close()
    updater.LEDGER = str(db)
    cand, consumed = updater.scan_pending_approvals(
        updater.journal_store.default_state())
    # the rollback vetoed both applies; only it remains as a candidate
    assert [c["command_id"] for c in cand] == ["cid-rb"]
    assert cand[0]["rollback"] is True
    assert sorted(dict(consumed).keys()) == ["cid-new", "cid-old"]
    assert set(dict(consumed).values()) == {"vetoed"}


def test_scan_pending_supersede(updater, tmp_path):
    db = tmp_path / "ledger.db"
    con = _receipts_db(db)
    sha = "a" * 40
    for cid, tag, at in (("c1", "v1.0.0", 1), ("c2", "v1.2.0", 2),
                         ("c3", "v1.10.0", 3)):
        con.execute(
            "INSERT INTO command_receipts VALUES(?,?,NULL,NULL,"
            "'applied',?,?)",
            (cid, "h" * 64, _rec(cid, "ops.update_apply", at, tag=tag,
                                 target_sha=sha), at))
    con.commit()
    con.close()
    updater.LEDGER = str(db)
    cand, consumed = updater.scan_pending_approvals(
        updater.journal_store.default_state())
    # numeric semver: v1.10.0 > v1.2.0 (string compare would pick v1.2.0)
    assert [c["tag"] for c in cand] == ["v1.10.0"]
    assert sorted(dict(consumed).values()) == ["superseded"] * 2


def test_scan_skips_executed(updater, tmp_path):
    db = tmp_path / "ledger.db"
    con = _receipts_db(db)
    sha = "a" * 40
    con.execute(
        "INSERT INTO command_receipts VALUES(?,?,NULL,NULL,'applied',?,?)",
        ("c1", "h" * 64, _rec("c1", "ops.update_apply", 1, tag="v1.0.0",
                              target_sha=sha), 1))
    con.commit()
    con.close()
    updater.LEDGER = str(db)
    state = updater.journal_store.default_state()
    state["executed"] = {"c1": {"result": "applied", "at": 1}}
    cand, consumed = updater.scan_pending_approvals(state)
    assert cand == [] and consumed == []


def test_scan_missing_ledger(updater, tmp_path):
    updater.LEDGER = str(tmp_path / "nonexistent.db")
    cand, consumed = updater.scan_pending_approvals(
        updater.journal_store.default_state())
    assert cand == [] and consumed == []


def test_scan_approvals_uses_exact_live_uri_path(updater, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger#?%23.db"
    decoy = tmp_path / "data" / "ledger"
    for path, cid in ((live, "actual"), (decoy, "decoy")):
        with _receipts_db(path) as con:
            con.execute(
                "INSERT INTO command_receipts VALUES(?,?,NULL,NULL,'applied',?,?)",
                (cid, "h" * 64, _rec(cid, "ops.update_rollback", 1), 1))
        con.close()
    monkeypatch.setattr(updater, "LEDGER", str(live))
    monkeypatch.setattr(update_apply, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', updater.LEDGER)

    candidates, consumed = updater.scan_pending_approvals(updater.journal_store.default_state())
    assert [candidate["command_id"] for candidate in candidates] == ["actual"]
    assert consumed == []


def test_scan_same_tag_newest_receipt_wins(updater, tmp_path):
    """Two approvals of the same tag: the NEWER receipt wins — it
    carries the freshest sha pin; the older is superseded (B)."""
    db = tmp_path / "ledger.db"
    con = _receipts_db(db)
    sha_old, sha_new = "a" * 40, "b" * 40
    for cid, sha, at in (("c-old", sha_old, 1), ("c-new", sha_new, 2)):
        con.execute(
            "INSERT INTO command_receipts VALUES(?,?,NULL,NULL,"
            "'applied',?,?)",
            (cid, "h" * 64, _rec(cid, "ops.update_apply", at,
                                 tag="v1.2.0", target_sha=sha), at))
    con.commit()
    con.close()
    updater.LEDGER = str(db)
    cand, consumed = updater.scan_pending_approvals(
        updater.journal_store.default_state())
    assert [c["command_id"] for c in cand] == ["c-new"]
    assert cand[0]["target_sha"] == sha_new
    assert dict(consumed) == {"c-old": "superseded"}


def test_schema_bump_never_auto_applies(updater, tmp_path, monkeypatch):
    """mode=auto + a schema-bumping tag => refused; only a human
    receipt may proceed past this gate (R2)."""
    repo = _apply_env(updater, monkeypatch,
                      precheck_errors=["schema_bump:7->8"],
                      cfg={"update": {"mode": "auto"}})
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: sha)
    rc = updater.apply("v1.1.0", sha, None)   # auto path: no receipt
    assert rc == 1
    after = updater.journal_store.load_state(updater.STATE_PATH)
    assert after["attempts"]["v1.1.0"]["result"] == "failed"
    assert "schema_bump_auto_blocked" in \
        after["attempts"]["v1.1.0"]["detail"]


# ----------------------------------------------------------------- config

def test_update_config_validation():
    import mcs_setup
    errs, _ = mcs_setup.validate_config(
        {"mcs_login_id": "u", "notify_target": "x",
         "update": {"mode": "notify", "auto_delay_h": 24}})
    assert not any("update" in e for e in errs)
    errs, _ = mcs_setup.validate_config(
        {"mcs_login_id": "u", "notify_target": "x",
         "update": {"mode": "bogus"}})
    assert any("update.mode" in e for e in errs)
    errs, _ = mcs_setup.validate_config(
        {"mcs_login_id": "u", "notify_target": "x",
         "update": {"auto_delay_h": -1}})
    assert any("update.auto_delay_h" in e for e in errs)


# ------------------------------------------ stray check fails closed

@pytest.mark.parametrize("name,via_c,expected", [
    ("extract_llm.py", False, True),
    ("semantic_drain.py", False, True),
    ("extract_llm.pyc", False, False),
    ("not_extract_llm.py", False, False),
    ("not_semantic_drain.py", False, False),
    ("extract_llm.py", True, False),
    ("extract_llm.py", "embedded", False),
])
@pytest.mark.parametrize("capitalized", [False, True])
def test_native_pgrep_stray_script_contract(updater, tmp_path, name, via_c, expected, capitalized):
    """Native pgrep must find only interpreter + exact drainer scripts."""
    script = tmp_path / name
    source = tmp_path / "worker_source.py" if name.endswith(".pyc") else script
    source.write_text("import time; time.sleep(30)\n")
    if name.endswith(".pyc"):
        py_compile.compile(str(source), cfile=str(script), doraise=True)
    executable = sys.executable
    if capitalized:
        executable = str(tmp_path / "Python")
        os.symlink(sys.executable, executable)
    argv = ([executable, "-c", "import time; time.sleep(30)", str(script)]
            if via_c else [executable, str(script)])
    if via_c == "embedded":
        argv[-1] = "/python " + str(script)
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # Let exec settle; only this owned synthetic child's PID is checked.
        time.sleep(0.15)
        assert child.poll() is None
        found = updater._stray_drainer_pids()
        assert found is not None
        assert (child.pid in found) is expected
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("content", [
    None, b"", b"{bad", b"[]", b'{"phase": "awaiting_consent", "report_id": "r"}',
    b'{"phase": "restored"}', b'{"restored_at": 1}', b'{"phase": "other"}'])
def test_awaiting_consent_marker_matches_notify_cards(tmp_path, monkeypatch,
                                                     content):
    """recover reads the hold marker without importing notify_spool (a
    rolled-back tree may predate it) — same fail-closed verdict."""
    import notify_spool
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path))
    monkeypatch.setattr(update_apply, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_lifecycle_context, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_planning, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_preflight, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_recovery, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_restore, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_services, 'DATA', mcs_update.DATA)
    if content is not None:
        (tmp_path / "restore_pending.json").write_bytes(content)
    assert (mcs_update._awaiting_consent_marker() is None) \
        == (notify_spool.restore_awaiting_consent(str(tmp_path)) is None)


@pytest.mark.parametrize("ancestry_rc, expect_notice", [(0, False), (1, True),
                                                        (128, True)])
def test_check_skips_notice_and_auto_when_head_contains_tag(
        updater, monkeypatch, ancestry_rc, expect_notice):
    """HEAD ahead of the newest tag is not an update; only rc 0 proves it."""
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {
        "update": {"mode": "auto", "auto_delay_h": 0}})
    monkeypatch.setattr(update_discovery, 'detect_latest',
                        lambda pre, **_lif05_paths: ("v9.0.0", "a" * 40))
    monkeypatch.setattr(update_discovery, 'current_version',
                        lambda**_lif05_paths: ("v9.0.0", "b" * 40))
    monkeypatch.setattr(mcs_update, "fetch_notes", lambda tag: "notes")
    monkeypatch.setattr(update_cli, 'fetch_notes', mcs_update.fetch_notes)
    monkeypatch.setattr(mcs_update, "impact_summary", lambda a, b: [])
    monkeypatch.setattr(update_cli, 'impact_summary', mcs_update.impact_summary)
    monkeypatch.setattr(mcs_update, "scan_pending_approvals",
                        lambda state: ([], []))
    monkeypatch.setattr(update_cli, 'scan_pending_approvals', mcs_update.scan_pending_approvals)
    calls = []

    def fake_git(args, timeout=None):
        calls.append(args)
        rc = ancestry_rc if args[0] == "merge-base" else 0
        return subprocess.CompletedProcess(args, rc, "", "")
    monkeypatch.setattr(mcs_update, "_git", fake_git)
    monkeypatch.setattr(update_apply, '_git', mcs_update._git)
    monkeypatch.setattr(update_cli, '_git', mcs_update._git)
    monkeypatch.setattr(update_planning, '_git', mcs_update._git)
    monkeypatch.setattr(update_preflight, '_git', mcs_update._git)
    monkeypatch.setattr(update_recovery, '_git', mcs_update._git)
    monkeypatch.setattr(update_repository, '_git', mcs_update._git)
    monkeypatch.setattr(update_restore, '_git', mcs_update._git)
    monkeypatch.setattr(update_runtime_migration, '_git', mcs_update._git)
    notices, applied = [], []
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(mcs_update, "apply",
                        lambda *a, **k: applied.append(a) or 0)
    monkeypatch.setattr(update_cli, 'apply', mcs_update.apply)
    assert mcs_update.cmd_check() == 0
    assert ["merge-base", "--is-ancestor", "a" * 40, "HEAD"] in calls
    assert bool(notices) is expect_notice
    assert bool(applied) is expect_notice


def test_drug_info_hook_never_delays_recovery_or_an_execution(updater, monkeypatch):
    monkeypatch.setattr(mcs_util, "load_config", lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(update_discovery, "detect_latest", lambda pre, **_paths: (None, None))
    monkeypatch.setattr(update_discovery, "current_version", lambda **_paths: ("v1.0.0", "b" * 40))
    monkeypatch.setattr(updater, "scan_pending_approvals", lambda state: ([], []))
    monkeypatch.setattr(update_cli, 'scan_pending_approvals', updater.scan_pending_approvals)
    hooks = []
    monkeypatch.setattr(updater, "_drug_info_notices", hooks.append)
    monkeypatch.setattr(update_cli, '_drug_info_notices', updater._drug_info_notices)
    assert updater.cmd_check() == 0 and len(hooks) == 1      # nothing to execute
    monkeypatch.setattr(updater, "scan_pending_approvals",
                        lambda state: ([{"rollback": True, "command_id": "c1"}], []))
    monkeypatch.setattr(update_cli, 'scan_pending_approvals', updater.scan_pending_approvals)
    monkeypatch.setattr(updater, "rollback", lambda command_id=None: 0)
    monkeypatch.setattr(update_cli, 'rollback', updater.rollback)
    assert updater.cmd_check() == 0 and len(hooks) == 1      # approved rollback first
    state = updater.journal_store.default_state()
    state["stages"].append({"stage": "local_checks", "at": 1.0})
    updater.journal_store.save_state(updater.STATE_PATH, state)
    monkeypatch.setattr(updater, "recover_interrupted", lambda: 0)
    monkeypatch.setattr(update_apply, 'recover_interrupted', updater.recover_interrupted)
    monkeypatch.setattr(update_cli, 'recover_interrupted', updater.recover_interrupted)
    monkeypatch.setattr(update_recovery, 'recover_interrupted', updater.recover_interrupted)
    assert updater.cmd_check() == 0 and len(hooks) == 1      # recovery first


def test_core_change_requires_gateway_restart_and_notice(updater, monkeypatch):
    def diff(args):
        if "--name-status" in args:
            return "M\tmcs/notify/notify_cards.py\n"
        return "mcs/notify/notify_cards.py\0" if "mcs" in args else ""

    monkeypatch.setattr(updater, "_git_out", diff)
    monkeypatch.setattr(update_apply, '_git_out', updater._git_out)
    monkeypatch.setattr(update_planning, '_git_out', updater._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', updater._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', updater._git_out)
    monkeypatch.setattr(update_repository, '_git_out', updater._git_out)
    monkeypatch.setattr(update_restore, '_git_out', updater._git_out)
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: {"notify": {"interactive": "lineworks"}})
    assert updater._plugin_changed("v1.1.0", {})
    impact = updater.impact_summary("synthetic-before", "v1.1.0")
    assert any("通知接続（gateway）の再起動" in line for line in impact)
    assert any("LINE WORKS" in line and "再起動" in line for line in impact)


def test_lineworks_impact_only_when_the_adapter_is_in_use(updater, monkeypatch):
    monkeypatch.setattr(updater, "_git_out", lambda args: "M\tmcs/notify/notify_cards.py\n")
    monkeypatch.setattr(update_apply, '_git_out', updater._git_out)
    monkeypatch.setattr(update_planning, '_git_out', updater._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', updater._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', updater._git_out)
    monkeypatch.setattr(update_repository, '_git_out', updater._git_out)
    monkeypatch.setattr(update_restore, '_git_out', updater._git_out)
    for cfg in ({}, {"notify": {"interactive": "slack"}}, {"notify": "lineworks"}):
        monkeypatch.setattr(mcs_util, 'load_config', lambda cfg=cfg: cfg)
        impact = updater.impact_summary("synthetic-before", "v1.1.0")
        assert any("通知接続（gateway）の再起動" in line for line in impact)
        assert not any("LINE WORKS" in line for line in impact)


def test_checkout_ahead_of_latest_tag_does_not_refetch_daily(updater, monkeypatch):
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(update_discovery, 'detect_latest', lambda pre, **_lif05_paths: ("v1.1.0", "a" * 40))
    monkeypatch.setattr(update_discovery, 'current_version', lambda**_lif05_paths: ("v1.1.0-3-gsynthetic", "b" * 40))
    monkeypatch.setattr(updater, "scan_pending_approvals", lambda state: ([], []))
    monkeypatch.setattr(update_cli, 'scan_pending_approvals', updater.scan_pending_approvals)
    calls = []
    monkeypatch.setattr(updater, "_git", lambda args, **kw: calls.append(args[0])
                        or subprocess.CompletedProcess(args, 0, "", ""))   # ancestor of HEAD
    monkeypatch.setattr(update_apply, '_git', updater._git)
    monkeypatch.setattr(update_cli, '_git', updater._git)
    monkeypatch.setattr(update_planning, '_git', updater._git)
    monkeypatch.setattr(update_preflight, '_git', updater._git)
    monkeypatch.setattr(update_recovery, '_git', updater._git)
    monkeypatch.setattr(update_repository, '_git', updater._git)
    monkeypatch.setattr(update_restore, '_git', updater._git)
    monkeypatch.setattr(update_runtime_migration, '_git', updater._git)
    fetched = []
    monkeypatch.setattr(updater, "fetch_notes", lambda tag: fetched.append(tag) or "notes")
    monkeypatch.setattr(update_cli, 'fetch_notes', updater.fetch_notes)
    monkeypatch.setattr(updater, "impact_summary", lambda current, tag: [])
    monkeypatch.setattr(update_cli, 'impact_summary', updater.impact_summary)
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text, **kw: pytest.fail("no notice"))
    monkeypatch.setattr(update_apply, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', updater._enqueue_notice)
    assert updater.cmd_check() == 0         # first sighting of the tag fetches once
    assert fetched == ["v1.1.0"] and calls.count("fetch") == 1
    for _ in range(2):
        assert updater.cmd_check() == 0
    assert fetched == ["v1.1.0"] and calls.count("fetch") == 1
    assert updater.journal_store.load_state(updater.STATE_PATH).get("notified_at") is None


def test_invalid_runtime_mode_is_reported_not_raised(updater, tmp_path, monkeypatch):
    from ops_testkit import _make_repo
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
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {"runtime_mode": "synthetic-invalid"})
    monkeypatch.setattr(updater, "precheck_local", lambda cfg: [])
    monkeypatch.setattr(update_apply, 'precheck_local', updater.precheck_local)
    monkeypatch.setattr(update_planning, 'precheck_local', updater.precheck_local)
    result = updater.plan("v1.1.0")
    assert result["route"] == "blocked"
    assert any(b.startswith("plan_failed: runtime_mode_invalid") for b in result["blockers"])
    monkeypatch.setattr(updater, "_git_out", lambda args: "M\tmcs/notify/notify_cards.py\n")
    monkeypatch.setattr(update_apply, '_git_out', updater._git_out)
    monkeypatch.setattr(update_planning, '_git_out', updater._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', updater._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', updater._git_out)
    monkeypatch.setattr(update_repository, '_git_out', updater._git_out)
    monkeypatch.setattr(update_restore, '_git_out', updater._git_out)
    with pytest.raises(ValueError):
        updater.impact_summary("synthetic-before", "v1.1.0")
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {
        "runtime_mode": "synthetic-invalid", "update": {"mode": "notify"}})
    monkeypatch.setattr(update_discovery, 'detect_latest', lambda pre, **_lif05_paths: ("v1.1.0", "a" * 40))
    monkeypatch.setattr(update_discovery, 'current_version', lambda**_lif05_paths: ("v1.0.0", "b" * 40))
    monkeypatch.setattr(updater, "scan_pending_approvals", lambda state: ([], []))
    monkeypatch.setattr(update_cli, 'scan_pending_approvals', updater.scan_pending_approvals)
    monkeypatch.setattr(updater, "_git", lambda args, **kw: subprocess.CompletedProcess(args, 1, "", ""))
    monkeypatch.setattr(update_apply, '_git', updater._git)
    monkeypatch.setattr(update_cli, '_git', updater._git)
    monkeypatch.setattr(update_planning, '_git', updater._git)
    monkeypatch.setattr(update_preflight, '_git', updater._git)
    monkeypatch.setattr(update_recovery, '_git', updater._git)
    monkeypatch.setattr(update_repository, '_git', updater._git)
    monkeypatch.setattr(update_restore, '_git', updater._git)
    monkeypatch.setattr(update_runtime_migration, '_git', updater._git)
    monkeypatch.setattr(updater, "fetch_notes", lambda tag: "notes")
    monkeypatch.setattr(update_cli, 'fetch_notes', updater.fetch_notes)
    notices = []
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text, **kw: notices.append(text) or True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', updater._enqueue_notice)
    assert updater.cmd_check() == 0 and notices


@pytest.mark.parametrize("body, expected", [
    ('{"v": 1, "install_args": ["--no-recovery", "--no-llm"]}', ["--no-llm", "--no-recovery"]),
    ('{"v": 1, "install_args": []}', []),
    (None, None),                                        # pre-1.0.22 install: no record
    ("not json", None),
    ('["--no-llm"]', None),
    ('{"install_args": ["--no-services"]}', None),       # never an operator opt-out
    ('{"install_args": [{"x": 1}]}', None),
    ('{"install_args": "--no-llm"}', None),
])
def test_recorded_install_args_only_trusts_known_opt_outs(tmp_path, monkeypatch, body, expected):
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path))
    monkeypatch.setattr(update_apply, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_lifecycle_context, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_planning, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_preflight, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_recovery, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_restore, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_services, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(mcs_update.mcs_util, "CONF_PATH", str(tmp_path / "config.json"))
    if body is not None:
        (tmp_path / "install_options.json").write_text(body)
    assert mcs_update.recorded_install_args() == expected


def test_recorded_install_args_ignores_a_symlinked_record(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path))
    monkeypatch.setattr(update_apply, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_lifecycle_context, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_planning, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_preflight, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_recovery, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_restore, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_services, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(mcs_update.mcs_util, "CONF_PATH", str(tmp_path / "config.json"))
    (tmp_path / "real.json").write_text('{"install_args": ["--no-llm"]}')
    (tmp_path / "install_options.json").symlink_to(tmp_path / "real.json")
    assert mcs_update.recorded_install_args() is None


def test_empty_rollback_keeps_receipt_code_and_japanese_display(updater, capsys):
    assert mcs_update.rollback(command_id="synthetic-empty-rollback") == 0
    state = mcs_update.journal_store.load_state(mcs_update.STATE_PATH)
    assert state["executed"]["synthetic-empty-rollback"]["result"] == "nothing_to_rollback"
    assert "元に戻す更新はありません" in capsys.readouterr().out


def test_updater_subcommand_parser_keeps_japanese_help(capsys):
    with pytest.raises(SystemExit) as stopped:
        mcs_update._build_parser().parse_args(["apply", "--help"])
    assert stopped.value.code == 0
    output = capsys.readouterr().out
    assert "使い方:" in output
    assert "オプション:" in output
    assert "この説明を表示して終了する" in output


@pytest.mark.parametrize("original,translated", mcs_update.update_policy.ENV_DIAGNOSTIC_TRANSLATIONS)
def test_postcheck_accepts_only_same_legacy_diagnostic(
        updater, monkeypatch, original, translated):
    import ledger
    from types import SimpleNamespace

    values = tuple(f"synthetic-{i}" for i in range(original.count("{}")))
    before = original.format(*values)
    after = translated.format(*values)
    monkeypatch.setattr(mcs_update, "_head_sha", lambda: "synthetic-sha")
    monkeypatch.setattr(update_apply, '_head_sha', mcs_update._head_sha)
    monkeypatch.setattr(update_execution, '_head_sha', mcs_update._head_sha)
    monkeypatch.setattr(update_recovery, '_head_sha', mcs_update._head_sha)
    monkeypatch.setattr(update_repository, '_head_sha', mcs_update._head_sha)
    monkeypatch.setattr(update_restore, '_head_sha', mcs_update._head_sha)
    monkeypatch.setattr(update_runtime_migration, '_head_sha', mcs_update._head_sha)
    monkeypatch.setattr(mcs_update, "_tree_clean", lambda: True)
    monkeypatch.setattr(update_apply, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_execution, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_preflight, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_recovery, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_restore, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_runtime_migration, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(mcs_update, "_agent_pid", lambda label: 123)
    monkeypatch.setattr(update_execution, '_agent_pid', mcs_update._agent_pid)
    monkeypatch.setattr(update_services, '_agent_pid', mcs_update._agent_pid)
    monkeypatch.setattr(ledger, "Ledger", lambda path:
                        SimpleNamespace(db=SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr(environment_diagnostics, "check_environment", lambda cfg, **facts: ([after], []))
    # An old English journal must tolerate exactly the same Japanese finding.
    state = {"baseline_check": [before]}
    assert mcs_update._postcheck(state, "synthetic-sha") == []
    assert mcs_update._postcheck({"baseline_check": [after]}, "synthetic-sha") == []
    assert mcs_update._postcheck({}, "synthetic-sha") == ["new_env_error: " + after]
    # A changed path, status, slot count or broker detail must still block.
    for i in range(len(values)):
        changed = list(values)
        changed[i] += "-changed"
        new_error = translated.format(*changed)
        monkeypatch.setattr(environment_diagnostics, "check_environment",
                            lambda cfg, error=new_error, **facts: ([error], []))
        assert mcs_update._postcheck(state, "synthetic-sha") == ["new_env_error: " + new_error]
    unexpected = "synthetic-new-environment-failure"
    monkeypatch.setattr(environment_diagnostics, "check_environment", lambda cfg, **facts: ([after, unexpected], []))
    assert mcs_update._postcheck(state, "synthetic-sha") == ["new_env_error: " + unexpected]


def test_legacy_baseline_matches_current_environment_probe(updater, monkeypatch):
    import mcs_setup

    monkeypatch.setattr(environment_diagnostics.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup, "CHROME_BIN", "/synthetic-missing-chrome")
    monkeypatch.setattr(environment_diagnostics.os.path, "exists", lambda path: False)
    monkeypatch.setattr(environment_diagnostics, "check_llm", lambda cfg: ([], []))
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda cfg: "synthetic-hermes")
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda exe: True)
    monkeypatch.setattr(environment_diagnostics, "env_value", lambda *args, **kwargs: None)
    monkeypatch.delenv("MCS_LLM_ADMISSION", raising=False)
    errors, _ = mcs_setup.check_environment({})
    assert len(errors) == 1
    assert mcs_update.update_policy.environment_error_identity(errors[0]) == (
        mcs_update.update_policy.environment_error_identity("Chrome binary missing: /synthetic-missing-chrome"))


def test_legacy_baseline_matches_current_slot_probe(monkeypatch):
    import local_llm

    monkeypatch.setattr(local_llm, "resolve", lambda cfg: ("synthetic-endpoint", "model"))
    monkeypatch.setattr(local_llm, "probe_urls", lambda endpoint: ("models", "slots"))
    monkeypatch.setattr(local_llm, "SLOT_COUNT", 3)
    monkeypatch.setattr(local_llm, "bounded_request",
                        lambda url, *args: (200, {}, b"[{}]" if url == "slots" else b"{}"))
    errors, _ = environment_diagnostics.check_llm({})
    assert len(errors) == 1
    original = "llama-server advertises 1 slots but the selected count is 3 — fix -np or lower SLOT_COUNT"
    assert mcs_update.update_policy.environment_error_identity(errors[0]) == mcs_update.update_policy.environment_error_identity(original)
    changed = original.replace("1 slots", "2 slots")
    assert mcs_update.update_policy.environment_error_identity(errors[0]) != mcs_update.update_policy.environment_error_identity(changed)


def test_unstartable_command_reports_only_the_error_type(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file", "/synthetic/private/launchctl")
    monkeypatch.setattr(mcs_update.subprocess, "run", boom)
    r = mcs_update._run(["launchctl", "print"])
    assert (r.returncode, r.stderr) == (127, "FileNotFoundError")


def test_marker_is_private_and_never_follows_a_link(updater, tmp_path):
    updater._write_marker()
    assert (os.stat(updater.MARKER_PATH).st_mode & 0o777) == 0o600
    os.unlink(updater.MARKER_PATH)
    os.symlink(tmp_path / "elsewhere", updater.MARKER_PATH)
    with pytest.raises(OSError):
        updater._write_marker()
    assert not (tmp_path / "elsewhere").exists()


def test_guided_apply_stops_if_approved_options_changed(updater, monkeypatch, tmp_path, capsys):
    config = Path(mcs_util.CONF_PATH)
    config.write_text('{"install_args": ["--no-llm"]}')
    monkeypatch.setenv('MCS_UPDATE_OPTIONS_EXPECTED', '[]')
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(updater, '_apply_runtime_gate_errors', lambda *_: [])
    monkeypatch.setattr(update_apply, '_apply_runtime_gate_errors', updater._apply_runtime_gate_errors)
    assert updater._apply_pipeline('v1.0.23', None, None, reinstall=True) == 1
    assert '導入設定が変更' in capsys.readouterr().out
    assert not Path(updater.STATE_PATH).exists()
    assert json.loads(config.read_text()) == {'install_args': ['--no-llm']}


def test_install_options_snapshot_propagates_transient_read_failure(updater, monkeypatch):
    original = updater.install_options._legacy
    calls = []
    def interrupted(path):
        calls.append(True)
        if len(calls) == 2:
            raise PermissionError('synthetic transient read failure')
        return original(path)
    monkeypatch.setattr(updater.install_options, '_legacy', interrupted)
    with pytest.raises(PermissionError):
        updater._install_options_snapshot()
    assert len(calls) == 2


@pytest.mark.parametrize('fact_source', ['shadow', 'canonical'])
def test_precheck_tag_stages_exact_candidate_runtime_assets(tmp_path, monkeypatch, fact_source):
    import update_policy

    root = Path(__file__).resolve().parents[2]
    files = {str(p.relative_to(root)): p.read_bytes()
             for p in (root / 'mcs').rglob('*.py')}
    files.update({name: (root / name).read_bytes()
                  for name in update_policy.CANDIDATE_RUNTIME_ASSETS})
    # A sibling asset must never enter the candidate runtime closure.
    files['evaluation/unrelated.json'] = b'{"synthetic": true}'
    entries = [('100644', 'blob', name, name) for name in files]
    cfg = {'semantic': {'fact_source': fact_source}, 'cloud_llm': {'mode': 'off'}}
    if fact_source == 'canonical':
        cfg['semantic']['fact_source_gate'] = 'synthetic'
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    monkeypatch.setattr(update_preflight, 'HOME', str(tmp_path))
    monkeypatch.setattr(update_preflight.mcs_util, 'load_config', lambda: cfg)
    monkeypatch.setattr(update_preflight, '_precheck_target_tree', lambda *_: ([], entries))
    monkeypatch.setattr(update_preflight, '_precheck_target_runtime_files', lambda *_: [])
    run = subprocess.run
    probes = []

    def candidate_run(argv, **kwargs):
        if argv[0] == 'git':
            assert argv[3:5] == ['cat-file', 'blob']
            return subprocess.CompletedProcess(argv, 0, files[argv[5]], b'')
        staged = Path(argv[-1]).parent
        for name in update_policy.CANDIDATE_RUNTIME_ASSETS:
            assert (staged / name).read_bytes() == files[name]
        assert not (staged / 'evaluation/unrelated.json').exists()
        probes.append(argv)
        return run(argv, **kwargs)

    monkeypatch.setattr(update_preflight, '_run_with_lifetime', candidate_run)
    errors = update_preflight.precheck_tag('v1.2.0')
    assert len(probes) == 1
    assert not any(e.startswith('preflight_') for e in errors), errors
    semantic_errors = [e for e in errors if 'semantic' in e]
    if fact_source == 'shadow':
        assert semantic_errors == []
    else:
        assert len(semantic_errors) == 1
        assert 'semantic_fact_source_evidence_mismatch' in semantic_errors[0]


@pytest.mark.parametrize('mode,kind', [('120000', 'blob'), ('160000', 'commit')])
def test_precheck_tag_rejects_nonregular_runtime_asset(monkeypatch, mode, kind):
    import update_policy

    asset = update_policy.CANDIDATE_RUNTIME_ASSETS[0]
    entries = [(mode, kind, 'a' * 40, asset)]
    monkeypatch.setattr(update_preflight.mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(update_preflight, '_precheck_target_tree', lambda *_: ([], entries))
    monkeypatch.setattr(update_preflight, '_precheck_target_runtime_files', lambda *_: [])

    def no_blob_or_probe(*_args, **_kwargs):
        pytest.fail('nonregular asset must be rejected before reading or executing it')

    monkeypatch.setattr(update_preflight, '_run_with_lifetime', no_blob_or_probe)
    errors = update_preflight.precheck_tag('v1.2.0')
    assert errors == [f'preflight_failed: bad_entry_type: {mode} {asset}']
