"""mcs_update — updater state, tag detection, gates, receipt queue.

Fully synthetic: temp git repos + temp sqlite + stubbed subprocess/
network. No real MCS, Discord, Keychain, or external repo access.
"""

import environment_diagnostics
from ops_testkit import in_process_post_merge, select_updater_service_python
import restore_evidence
import scheduled_jobs
import service_contract
import service_management
import owned_process
import update_discovery
import release_apply_owner

import runtime_inspection
import json
import os
import py_compile
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import mcs_update
import mcs_util
from ops_testkit import (_FakeLaunchd, _git, _make_repo, _mk_schema, _receipts_db,
                         _seed_consent)

@pytest.mark.parametrize('authority', [False, True, 7, 'serialized-token', {}, 'unregistered'])
def test_private_release_capability_refusal_precedes_all_legacy_effects(updater, monkeypatch, authority):
    if authority == 'unregistered':
        authority = release_apply_owner._EffectCapability()
    def forbidden(*_args, **_kwargs):
        pytest.fail('release refusal reached legacy state, locks or cleanup')
    for name in ('acquire_update_lock', '_acquire_run_lock_wait', 'recover_interrupted',
                 '_remove_marker', '_enqueue_notice', '_report', '_apply_runtime_gate_errors'):
        monkeypatch.setattr(updater, name, forbidden)
    monkeypatch.setattr(updater.journal_store, 'load_state', forbidden)
    monkeypatch.setattr(updater.mcs_util, 'load_config', forbidden)
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='EFFECT_HOLD'):
        updater._apply_pipeline(None, '1' * 40, None, _release_cap=authority)


@pytest.mark.parametrize('native', [False, True])
def test_release_post_merge_requires_native_adoption_and_never_calls_legacy(updater, tmp_path, monkeypatch, native):
    saved = {'applying': {'sha': 'a' * 40, 'release_ref': {'synthetic': True}},
             'release_ref': {'synthetic': True}}
    monkeypatch.setattr(updater.journal_store, 'load_state', lambda *_: saved)
    monkeypatch.setenv(updater._UPDATE_PM_ENV, 'a' * 40)
    monkeypatch.delenv(release_apply_owner.CHILD_FDS_ENV, raising=False)
    monkeypatch.setattr(updater, '_call_legacy', lambda *_: pytest.fail('release fell back to legacy'))
    calls = []
    monkeypatch.setattr(updater, '_post_merge', lambda value: calls.append(value) or 0)
    if not native:
        assert updater.cmd_post_merge() == 2
        assert calls == []
        return
    fds = [os.open(tmp_path / f'lock-{i}', os.O_CREAT | os.O_RDWR, 0o600) for i in range(3)]
    reader, writer = os.pipe()
    try:
        monkeypatch.setenv(release_apply_owner.CHILD_FDS_ENV, ':'.join(map(str, (reader, *fds))))
        consumed = {**saved, 'applying': {**saved['applying'], 'release_child': {'phase': 'consumed'}}}

        def adopt(home, repo, *, ticket_fd, locks, deadline):
            assert (home, repo) == (updater.HOME, updater.REPO)
            assert ticket_fd == reader and locks == tuple(fds)
            assert time.monotonic() < deadline <= time.monotonic() + 2
            return consumed

        monkeypatch.setattr(release_apply_owner, '_adopt_post_merge', adopt)
        assert updater.cmd_post_merge() == 0
        assert calls == [consumed]
        for descriptor in fds:
            os.fstat(descriptor)  # The child scope borrows; it never closes native descriptors.
    finally:
        for descriptor in (*fds, reader, writer):
            os.close(descriptor)


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


def _standalone_heartbeat(updater, **updates):
    value = {"pid": os.getpid(), "generation": "a" * 32, "updated_at": time.time(),
             "update_in_progress": False, "children": {
                 name: {"pid": os.getpid(), "kind": "background"} for name in ("extract-0", "extract-2")}}
    value.update(updates)
    path = Path(updater.DATA, "standalone-status.json")
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return value


def test_standalone_precheck_does_not_require_hermes(updater, monkeypatch):
    monkeypatch.setattr(updater, "_tree_clean", lambda: True)
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


def test_standalone_quiesce_and_restart_keep_the_host_alive(updater, monkeypatch):
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {"runtime_mode": "standalone"})
    (Path(updater.DATA).parent / "config.json").write_text('{"runtime_mode":"standalone"}')
    _standalone_heartbeat(updater, update_in_progress=True, children={"update": {"pid": os.getpid(), "kind": "core"}})
    monkeypatch.setattr(updater, "_run", lambda *a, **k: pytest.fail("native service must not be stopped"))
    assert updater.quiesce() == ["extract-0", "extract-2"]
    assert Path(updater.MARKER_PATH).exists()
    _standalone_heartbeat(updater)
    assert updater.restart_agents() == []
    assert not Path(updater.MARKER_PATH).exists()


def test_standalone_quiesce_fails_closed_on_missing_host(updater, monkeypatch):
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {"runtime_mode": "standalone"})
    ticks = iter([0, 21])
    monkeypatch.setattr(updater.time, "monotonic", lambda: next(ticks))
    with pytest.raises(updater.UpdateError, match="standalone_quiesce_unverifiable"):
        updater.quiesce()


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
    with pytest.raises(updater.UpdateError, match="standalone_runtime_missing_in_target"):
        updater._rollback_tree({"prev_sha": "0" * 40})
    assert not any("reset" in call for call in calls)


@pytest.mark.parametrize("bad", [{"updated_at": 0}, {"pid": -1}, {"children": {"extract-0": []}}])
def test_standalone_heartbeat_is_evidence_not_an_assumption(updater, bad):
    _standalone_heartbeat(updater, **bad)
    assert updater._standalone_status() is None


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
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda exe: _facts())
    monkeypatch.setattr(mcs_update, "REPO", str(tmp_path / "repo"))
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(mcs_update, "STATE_PATH",
                        str(tmp_path / "data" / "update_state.json"))
    monkeypatch.setattr(mcs_update, "UPDATE_LOCK",
                        str(tmp_path / "data" / "update.lock"))
    monkeypatch.setattr(mcs_update, "RUN_LOCK",
                        str(tmp_path / "data" / "run.lock"))
    monkeypatch.setattr(mcs_update, "MARKER_PATH",
                        str(tmp_path / "data" / "marker"))
    monkeypatch.setattr(mcs_update, "REPORT_PATH",
                        str(tmp_path / "data" / "recovery_report.json"))
    monkeypatch.setattr(mcs_update, "RESTORE_REPORT_PATH",
                        str(tmp_path / "data" / "restore_report.json"))
    monkeypatch.setattr(mcs_update, "LEDGER", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(mcs_update, "BACKUP_DIR",
                        str(tmp_path / "data" / "backups"))
    monkeypatch.setattr(mcs_update, "MANIFEST_PATH",
                        str(tmp_path / "data" / "service_manifest.json"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    return mcs_update


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
    monkeypatch.setattr(recovery, "STATE_PATH", updater.STATE_PATH)
    state = updater.journal_store.default_state()
    state[field] = value
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.journal_store.load_state(updater.STATE_PATH) == {"_corrupt": True}
    assert recovery._load_state() == {"_corrupt": True}


def test_restore_does_not_replace_unreadable_consent_marker(updater, tmp_path):
    marker = tmp_path / "data" / "restore_pending.json"
    marker.write_text("{broken")
    with pytest.raises(updater.UpdateError, match="restore_marker_unreadable"):
        updater._restore_db(str(tmp_path / "backup.db"))
    assert marker.read_text() == "{broken"


def test_update_lock_nonblocking(updater):
    fd = updater.acquire_update_lock()
    assert fd is not None
    assert updater.acquire_update_lock() is None
    os.close(fd)
    reopened = updater.acquire_update_lock()
    assert reopened is not None
    os.close(reopened)


@pytest.mark.parametrize("interrupted", [False, True])
def test_rollback_keeps_manifest_and_restarts_changed_plugin(
        updater, tmp_path, monkeypatch, interrupted):
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    before = _git(repo, "rev-parse", "v1.0.0").stdout.strip()
    after = _git(repo, "rev-parse", "v1.1.0^{commit}").stdout.strip()
    desired = {"scripts": [], "agents": [{"label": "local.mcs-cmd"}], "cron": []}
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": after,
                         "prev_sha": before, "plugin_changed": True,
                         "manifest_snapshot": desired, "command_id": "apply-id"}
    monkeypatch.setattr(updater, "_services_reconcile", lambda: None)
    monkeypatch.setattr(updater, "restart_agents", lambda: [])
    monkeypatch.setattr(updater, "_postcheck", lambda *a: [])
    monkeypatch.setattr(updater, "_enqueue_notice", lambda *a, **k: True)
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
    monkeypatch.setattr(updater, "quiesce", lambda: [])
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


def test_detect_latest_prerelease_excluded(updater, monkeypatch):
    def fake_git(args, timeout=30, **_lif05_paths):
        class R:
            returncode = 0
            stdout = (("a" * 40) + "\trefs/tags/v1.2.0\n"
                      + ("b" * 40) + "\trefs/tags/v1.3.0-rc1\n"
                      + ("c" * 40) + "\trefs/tags/v1.3.0-rc1^{}\n")
            stderr = ""
        return R()
    monkeypatch.setattr(update_discovery, "git", fake_git)
    tag, sha = update_discovery.detect_latest(checkout=mcs_update.REPO, environment=mcs_update.os.environ)
    assert tag == "v1.2.0"
    tag, sha = update_discovery.detect_latest(include_prerelease=True, checkout=mcs_update.REPO, environment=mcs_update.os.environ)
    # opt-in picks the prerelease — and peels it to the commit sha
    assert tag == "v1.3.0-rc1"
    assert sha == "c" * 40


def test_prerelease_numeric_components_follow_semver_order():
    names = ["v1.2.0-rc.2", "v1.2.0-rc.10", "v1.2.0"]
    assert sorted(reversed(names), key=mcs_update.update_policy.version_key) == names
    assert mcs_update.update_policy.version_key(["invalid"]) is None


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
    monkeypatch.setattr(updater, '_ls_tree_paths', lambda *args: [])
    def git(argv):
        denied = argv[:2] == ['diff', '--quiet'] and changed.startswith(tuple(argv[argv.index('--') + 1:]))
        return subprocess.CompletedProcess(argv, int(denied), '', '')
    monkeypatch.setattr(updater, '_git', git)
    monkeypatch.setattr(updater, '_git_out', lambda argv: 'SCHEMA_VERSION = 8' if argv[0] == 'show' else '')
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
    monkeypatch.setattr(updater, "_ls_tree_paths", lambda *args: [])
    monkeypatch.setattr(updater, "_git", lambda argv:
                        subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(updater, "_git_out", lambda argv:
                        "SCHEMA_VERSION = 8" if argv[0] == "show" else "")
    monkeypatch.setattr(updater.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "[]", ""))

    assert updater.precheck_tag("v1.1.0") == []


def test_precheck_schema_unreadable_ledger_is_unknown_not_zero(
        updater, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger.db"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b"not a sqlite database" * 64)
    monkeypatch.setattr(updater, "LEDGER", str(live))
    monkeypatch.setattr(updater, "_ls_tree_paths", lambda *args: [])
    monkeypatch.setattr(updater, "_git", lambda argv:
                        subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(updater, "_git_out", lambda argv:
                        "SCHEMA_VERSION = 8" if argv[0] == "show" else "")
    monkeypatch.setattr(updater.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "[]", ""))

    errors = updater.precheck_tag("v1.1.0")
    assert "schema_version_unknown" in errors
    assert not any(e.startswith("schema_bump:") for e in errors)


def _cron_listing(script, ansi=False):
    """Synthetic `hermes cron list --all` in the hermes_cli/cron.py layout:
    `  <id> [badge]` then `    <Label:><pad to 11><value>` rows."""
    jid, badge = ("\x1b[33mabcdef\x1b[0m", "\x1b[31m[disabled]\x1b[0m") if ansi \
        else ("abcdef", "[disabled]")
    return (f"Scheduled Jobs (profile: default)\n\n  {jid} {badge}\n"
            f"    Name:      synthetic-job\n    Schedule:  */10 * * * *\n"
            f"    Repeat:    \u221e\n    Script:    {script}\n\n")


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

    updater._rollback_tree({"sha": target, "prev_sha": previous})

    assert path.read_text() == "user content after interrupted update"


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


# ------------------------------------------------------------ receipts

def _rec(cid, cmd, at, **kw):
    return json.dumps({"cmd": cmd, "scheduled": True,
                       "command_id": cid, **kw})


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


# ------------------------------------------------------------- apply path

def _apply_env(updater, monkeypatch, *, precheck_errors=(),
               cfg=None, sha=None):
    """Stub every apply() external boundary; the temp repo + real
    tags remain real (fetch/rev-parse/merge hit the file remote)."""
    repo, _ = _make_repo(Path(mcs_update.DATA).parent)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "precheck_local", lambda c: [])
    monkeypatch.setattr(mcs_update, "precheck_tag",
                        lambda t: list(precheck_errors))
    monkeypatch.setattr(update_discovery, 'remote_tag_sha',
                        lambda t, **_lif05_paths: sha)
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: cfg or {})
    monkeypatch.setattr(mcs_update, "_baseline_check", lambda c: [])
    monkeypatch.setattr(mcs_update, "quiesce", lambda: [])
    monkeypatch.setattr(mcs_update, "restart_agents", lambda: [])
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(mcs_update, "_postcheck", lambda s, e: [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)
    import maintenance
    monkeypatch.setattr(maintenance, "preupdate_backup",
                        lambda db: str(db) + ".bak")
    return repo


@pytest.mark.parametrize("path,needs_restart", [
    ("hermes_plugin/card_workers.py", True),
    ("adapters/common/worker.py", True),
    ("adapters/slack/actions.py", True),
    ("adapters/discord/delivery.py", True),
    ("adapters/lineworks/actions.py", False),
    ("lineworks_adapter/__main__.py", False),
])
def test_apply_persists_adapter_restart_decision_before_restarting_gateway(
        updater, tmp_path, monkeypatch, path, needs_restart):
    repo = _apply_env(updater, monkeypatch)
    remote_work = tmp_path / "remote-work"
    changed = remote_work / path
    changed.parent.mkdir(parents=True)
    changed.write_text("# synthetic adapter update\n")
    _git(remote_work, "add", path)
    _git(remote_work, "commit", "-qm", "adapter update")
    _git(remote_work, "tag", "v1.2.0")
    _git(remote_work, "push", "-q", str(tmp_path / "remote.git"), "main", "v1.2.0")
    sha = _git(remote_work, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda tag, **_lif05_paths: sha)
    monkeypatch.setattr(updater, "_run_post_merge",
                        in_process_post_merge(updater, monkeypatch))
    restarts = []

    def restart(cfg):
        saved = updater.journal_store.load_state(updater.STATE_PATH)
        assert saved["applying"] is None
        assert saved["applied"][-1]["plugin_changed"] is True
        assert saved["executed"]["cid-adapter"]["result"] == "applied"
        restarts.append(True)

    monkeypatch.setattr(updater, "restart_gateway", restart)
    assert updater.apply("v1.2.0", sha, "cid-adapter") == 0
    saved = updater.journal_store.load_state(updater.STATE_PATH)
    assert saved["applied"][-1]["plugin_changed"] is needs_restart
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == sha
    assert restarts == ([True] if needs_restart else [])


def test_bail_consumes_failed_receipt(updater, tmp_path, monkeypatch):
    """A failed apply attempt MUST consume the receipt — otherwise the
    next daily check retries it forever (A)."""
    repo = _apply_env(updater, monkeypatch, precheck_errors=["boom"])
    (tmp_path / "remote-work" / "v2.txt").write_text("x")
    _git(tmp_path / "remote-work", "add", ".")
    _git(tmp_path / "remote-work", "commit", "-qm", "c")
    _git(tmp_path / "remote-work", "tag", "v9.9.9")
    _git(tmp_path / "remote-work", "push", "-q",
         str(tmp_path / "remote.git"), "main", "v9.9.9")
    sha = _git(repo, "rev-list", "-n1", "v9.9.9",
               check=False).stdout.strip()
    if not sha:
        _git(repo, "fetch", "-q", "--tags")
        sha = _git(repo, "rev-list", "-n1", "v9.9.9").stdout.strip()
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: sha)
    rc = updater.apply("v9.9.9", sha, "cid-fail")
    assert rc == 1
    after = updater.journal_store.load_state(updater.STATE_PATH)
    assert after["executed"]["cid-fail"]["result"] == "failed"
    assert after["attempts"]["v9.9.9"]["result"] == "failed"


def test_apply_transient_lock_failure_keeps_receipt(updater, tmp_path,
                                                    monkeypatch):
    """run_lock_timeout never consumes the receipt — nothing was
    attempted; a later check may legitimately retry."""
    repo = _apply_env(updater, monkeypatch)
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: sha)
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait",
                        lambda **kw: None)
    rc = updater.apply("v1.1.0", sha, "cid-wait")
    assert rc == 2
    after = updater.journal_store.load_state(updater.STATE_PATH)
    assert "cid-wait" not in after.get("executed", {})
    assert after["stages"] == []       # journal cleaned, not wedged


def test_apply_update_lock_busy_returns_cleanly(updater, tmp_path,
                                               monkeypatch, capsys):
    """A live updater holds update.lock — a second apply must exit
    without touching state rather than queue another attempt."""
    _apply_env(updater, monkeypatch)
    before = updater.journal_store.load_state(updater.STATE_PATH)
    fd = updater.acquire_update_lock()
    try:
        assert updater.apply("v1.1.0", "a" * 40, "cid-2") == 2
        assert 'この更新は開始していません' in capsys.readouterr().out
        assert updater.journal_store.load_state(updater.STATE_PATH) == before
    finally:
        os.close(fd)


def test_scheduled_approval_stays_pending_when_apply_lock_becomes_busy(updater, tmp_path, monkeypatch):
    _apply_env(updater, monkeypatch, cfg={'update': {'mode': 'notify'}})
    db = tmp_path / 'synthetic-receipts.db'
    con = _receipts_db(db)
    cid, target = 'synthetic-approved', 'a' * 40
    con.execute("INSERT INTO command_receipts VALUES(?,?,NULL,NULL,'applied',?,?)",
                (cid, 'h' * 64, _rec(cid, 'ops.update_apply', 1,
                                     tag='v1.2.0', target_sha=target), 1))
    con.commit()
    con.close()
    monkeypatch.setattr(updater, 'LEDGER', str(db))
    monkeypatch.setattr(update_discovery, 'detect_latest', lambda *_a, **_k: (None, None))
    monkeypatch.setattr(update_discovery, 'current_version', lambda **_k: ('v1.0.0', 'b' * 40))
    monkeypatch.setattr(updater, '_apply_runtime_gate_errors', lambda *_a, **_k: [])
    acquire = updater.acquire_update_lock
    calls = []

    def busy_after_scan():
        calls.append(True)
        return acquire() if len(calls) == 1 else None

    monkeypatch.setattr(updater, 'acquire_update_lock', busy_after_scan)
    assert updater.cmd_check() == 2
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert cid not in state.get('executed', {})
    assert [entry['command_id'] for entry in updater.scan_pending_approvals(state)[0]] == [cid]
    monkeypatch.setattr(updater, 'acquire_update_lock', acquire)
    seen = []
    monkeypatch.setattr(updater, 'apply', lambda *args: seen.append(args) or 0)
    assert updater.cmd_check() == 0
    assert len(seen) == 1 and seen[0][2] == cid


def test_scheduled_check_skips_when_update_lock_is_busy(updater, tmp_path, monkeypatch):
    _apply_env(updater, monkeypatch, cfg={'update': {'mode': 'notify'}})
    monkeypatch.setattr(update_discovery, 'detect_latest', lambda *_a, **_k: (None, None))
    monkeypatch.setattr(update_discovery, 'current_version', lambda **_k: ('v1.0.0', 'b' * 40))
    monkeypatch.setattr(updater, 'scan_pending_approvals', lambda *_a: pytest.fail('must defer scan'))
    before = updater.journal_store.load_state(updater.STATE_PATH)
    fd = updater.acquire_update_lock()
    try:
        assert updater.cmd_check() == 0
        assert updater.journal_store.load_state(updater.STATE_PATH) == before
    finally:
        os.close(fd)


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


# ---------------------------------------------------------------- recover

def test_recover_interrupted_pre_merge(updater, tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v9.9.9",
                         "sha": "f" * 40,        # != HEAD → pre-merge
                         "prev_sha": head,
                         "at": time.time() - 4000}
    state["stages"] = [{"stage": "quiesce", "at": time.time() - 3000}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    rc = updater.recover_interrupted()
    assert rc == 0
    after = updater.journal_store.load_state(updater.STATE_PATH)
    assert after["applying"] is None and after["stages"] == []


def test_recover_merge_head_abort(updater, tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / ".git" / "MERGE_HEAD").write_text(prev + "\n")
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": prev, "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    rc = updater.recover_interrupted()
    assert rc == 0
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == prev


def test_recover_escalates_unclassifiable(updater, tmp_path,
                                          monkeypatch):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    state = updater.journal_store.default_state()
    # HEAD moved somewhere unknown mid-merge
    state["applying"] = {"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": "0" * 40, "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    rc = updater.recover_interrupted()
    assert rc == 1
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "escalate"
    # destructive actions must not have run
    assert _git(repo, "rev-parse", "HEAD").returncode == 0


def test_recover_pre_applying_remnant_is_cleaned(updater, tmp_path,
                                                 monkeypatch):
    """Stages without `applying` used to wedge every future update —
    a crash before the 'applying' journal wrote nothing to the tree,
    so recovery must simply clean up (F10)."""
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    state = updater.journal_store.default_state()
    state["stages"] = [{"stage": "local_checks", "at": time.time() - 4000},
                       {"stage": "fetch", "at": time.time() - 3900}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.recover_interrupted() == 0
    after = updater.journal_store.load_state(updater.STATE_PATH)
    assert after["stages"] == [] and after["applying"] is None
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "interrupted_pre_merge"


@pytest.mark.parametrize('published', [False, True])
def test_recover_interrupted_premerge_settings_migration_preserves_data(updater, tmp_path, monkeypatch, published):
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, 'REPO', str(repo))
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(updater, '_enqueue_notice', lambda *_a, **_k: True)
    config = Path(mcs_util.CONF_PATH)
    legacy = Path(updater.DATA) / 'install_options.json'
    value = {'unknown': {'keep': True}, **({'install_args': ['--no-llm']} if published else {})}
    config.write_text(json.dumps(value))
    legacy.write_text('{"v": 1, "install_args": ["--no-llm"]}')
    original = (config.read_bytes(), legacy.read_bytes())
    state = updater.journal_store.default_state()
    state['stages'] = [{'stage': 'settings_migration', 'at': time.time() - 4000}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.recover_interrupted() == 0
    after = updater.journal_store.load_state(updater.STATE_PATH)
    assert after['applied'] == [] and after['applying'] is None and after['stages'] == []
    assert (config.read_bytes(), legacy.read_bytes()) == original


def test_recover_completes_done_bookkeeping(updater, tmp_path,
                                            monkeypatch):
    """Child wrote 'done' then died before the parent consumed the
    receipt — recovery completes the bookkeeping (H5)."""
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    state = updater.journal_store.default_state()
    state["stages"] = [{"stage": "done", "at": time.time() - 100}]
    state["applied"] = [{"tag": "v1.1.0", "sha": "b" * 40,
                         "prev_sha": "a" * 40, "command_id": "cid-9",
                         "at": time.time()}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.recover_interrupted() == 0
    after = updater.journal_store.load_state(updater.STATE_PATH)
    assert after["stages"] == []
    assert after["executed"]["cid-9"]["result"] == "applied"
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "resumed_done"


def test_recover_mixed_tree_after_merge_stage_crash(updater, tmp_path,
                                                    monkeypatch):
    """'merge' is journaled BEFORE the merge — a crash during checkout
    leaves HEAD==prev with a dirty tree and no MERGE_HEAD. Stage-label
    gating could never reach this path; measurement must (F1)."""
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    target = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    # simulate a partially-written checkout: tracked file has target
    # content, update-introduced file exists untracked
    (repo / "f.txt").write_text("three")
    (repo / "introduced.txt").write_text("new")
    work = tmp_path / "remote-work"
    (work / "introduced.txt").write_text("new")
    (work / "f.txt").write_text("three")    # the dirty path is one the update touches
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "c3")
    _git(work, "tag", "v1.2.0")
    _git(work, "push", "-q", str(tmp_path / "remote.git"),
         "main", "v1.2.0")
    _git(repo, "fetch", "-q", "--tags")
    target = _git(repo, "rev-list", "-n1", "v1.2.0").stdout.strip()
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.2.0", "sha": target,
                         "prev_sha": prev, "command_id": "cid-mix",
                         "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    trace = []
    real_changes, real_git_out = updater._local_changes_outside, updater._git_out

    def observed_changes(old_sha, new_sha):
        trace.append(("changes", old_sha, new_sha))
        return real_changes(old_sha, new_sha)

    def observed_git_out(argv, *args, **kwargs):
        if argv[:2] == ["reset", "--hard"]:
            trace.append(("reset", argv[2]))
        return real_git_out(argv, *args, **kwargs)

    monkeypatch.setattr(updater, "_local_changes_outside", observed_changes)
    monkeypatch.setattr(updater, "_git_out", observed_git_out)
    assert updater.recover_interrupted() == 0
    assert trace == [("changes", target, prev), ("reset", prev)]
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == prev
    assert _git(repo, "status", "--porcelain", "-uno").stdout.strip() == ""
    assert (repo / "introduced.txt").read_text() == "new"
    after = updater.journal_store.load_state(updater.STATE_PATH)
    # the failed apply's receipt is consumed — no crash/apply loop
    assert after["executed"]["cid-mix"]["result"] == \
        "interrupted_recovered"


def test_recover_defers_when_run_lock_busy(updater, tmp_path,
                                           monkeypatch):
    """A busy run.lock means a live tick — recovery must defer and
    touch NOTHING (H1): no git lock removal, no state mutation."""
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    removed = []
    monkeypatch.setattr(mcs_update, "_clean_stale_git_locks",
                        lambda: removed.append(1) or [])
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait",
                        lambda **kw: None)      # busy — defer
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": "0" * 40, "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.recover_interrupted() == 0
    assert removed == []
    # journal untouched — still waiting for a quiet moment
    assert updater.journal_store.load_state(updater.STATE_PATH)["applying"]["tag"] == "v1.1.0"


def test_recover_removes_orphan_marker(updater, tmp_path, monkeypatch):
    """Clean journal + leftover marker = dead writer — the marker must
    go or helper launchers stay suppressed forever (H9)."""
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait",
                        lambda **kw: os.open(mcs_update.RUN_LOCK,
                                             os.O_WRONLY | os.O_CREAT))
    Path(mcs_update.MARKER_PATH).write_text(str(time.time()))
    assert updater.recover_interrupted() == 0
    assert not Path(mcs_update.MARKER_PATH).exists()


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
    monkeypatch.setattr(mcs_update, "quiesce", lambda: None)
    restarted = []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda: restarted.append(1) or [])
    monkeypatch.setattr(
        mcs_update, "_rollback_tree",
        lambda e: (_ for _ in ()).throw(OSError("disk gone")))
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    monkeypatch.setattr(mcs_update, "_gateway_restart_if_needed",
                        lambda s: None)
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


# ------------------------------------------------- restore consent gate
# R2/T13: a schema-bump rollback that would replace the live DB must
# wait for a NEW human approval bound to the exact backup bytes +
# schema + loss report. An earlier update/rollback approval never
# substitutes; every writer/sender stays frozen while it waits.

def _schema_bump_world(updater, tmp_path, monkeypatch):
    """applied v1.1.0 entry flagged schema_bump with a real v7 backup;
    live DB sits one schema ahead at v8. Every external boundary is
    stubbed; git reset and the DB files stay real."""
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    before = _git(repo, "rev-parse", "v1.0.0").stdout.strip()
    after = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    live = str(tmp_path / "data" / "ledger.db")   # under DATA — the
    # marker/data_root paths must coincide like production
    monkeypatch.setattr(mcs_update, "LEDGER", live)
    back = tmp_path / "data" / "backups" / "preupdate.db"
    back.parent.mkdir(parents=True, exist_ok=True)
    _mk_schema(back, 7, messages=2)
    _mk_schema(live, 8, messages=5)          # 3 rows since the backup
    # keep quiesce's real marker write — only launchd stops are stubbed
    monkeypatch.setattr(mcs_update, "quiesce",
                        lambda: (mcs_update._write_marker(), [])[1])
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(service_management, 'retire_to_snapshot', lambda m, **_service_kwargs: [])
    monkeypatch.setattr(mcs_update, "_postcheck", lambda s, e: [])
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)
    restarts = []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda: restarts.append(1) or [])
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
    monkeypatch.setattr(updater, "_reconcile_restored", lambda: None)
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
    monkeypatch.setattr(updater, "_reconcile_restored", lambda: None)
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


def test_recover_holds_on_orphaned_restore_consent(updater, tmp_path,
                                                  monkeypatch):
    """restore_consent with a non-rollback applying record is journal
    corruption — never classify away the hold (a 'finish' would wedge
    every send grant forever) and, as every escalation inside a hold,
    keep the freeze: no drainer restart, marker kept, no outbox write;
    the report tells the human (only a human can repair the journal)."""
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda **k: pytest.fail("hold must not restart"))
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: pytest.fail("no outbox write"))
    Path(mcs_update.MARKER_PATH).write_text("1")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": head, "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    state["restore_consent"] = {"report_id": "a" * 64}
    updater.journal_store.save_state(updater.STATE_PATH, state)
    rc = updater.recover_interrupted()
    assert rc == 1
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "restore_consent_blocked"
    assert "restore_consent" in report["detail"]
    assert Path(mcs_update.MARKER_PATH).exists()
    # journal preserved for a human — nothing classified away
    after = updater.journal_store.load_state(updater.STATE_PATH)
    assert after["applying"] is not None and after["restore_consent"]


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
    from ops_testkit import _load
    _repo, live, back, _before, _after, _restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    recovery = _load() if independent else updater
    monkeypatch.setattr(recovery, "LEDGER", live)
    monkeypatch.setattr(recovery, "DATA", updater.DATA)
    writer = sqlite3.connect(live)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE messages SET body_html='durable synthetic update'")
        writer.commit()
        _seed_consent(live, back)
        wal = Path(live + "-wal")
        before = wal.read_bytes()

        def fail_copy(*args):
            raise OSError("synthetic copy failure")

        monkeypatch.setattr(recovery.shutil, "copyfileobj", fail_copy)
        if independent:
            assert recovery._restore_db(back).startswith("restore_failed:")
        else:
            with pytest.raises(updater.UpdateError, match="restore_failed"):
                recovery._restore_db(back)
        assert wal.read_bytes() == before
        assert writer.execute("SELECT body_html FROM messages LIMIT 1").fetchone()[0] == (
            "durable synthetic update")
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
    repo, _ = _make_repo(tmp_path)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    live = str(tmp_path / "data" / "ledger.db")
    monkeypatch.setattr(mcs_update, "LEDGER", live)
    back = tmp_path / "data" / "backups" / "pre.db"
    back.parent.mkdir(parents=True, exist_ok=True)
    _mk_schema(back, 7, messages=2)
    _mk_schema(live, 8, messages=5)
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(mcs_update, "precheck_local", lambda c: [])
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: sha)
    monkeypatch.setattr(mcs_update, "precheck_tag",
                        lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: str(back))
    monkeypatch.setattr(mcs_update, "_baseline_check", lambda c: [])
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
    monkeypatch.setattr(mcs_update, "restart_agents", restart)
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(service_management, 'retire_to_snapshot', lambda m, **_service_kwargs: [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)

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
    monkeypatch.setattr(mcs_update, "_run_post_merge", post_merge)
    assert mcs_update.apply("v1.1.0", sha, "cid-apply") == 2
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
    repo, _ = _make_repo(tmp_path)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    live = str(tmp_path / "data" / "ledger.db")
    monkeypatch.setattr(mcs_update, "LEDGER", live)
    back = tmp_path / "data" / "backups" / "pre.db"
    back.parent.mkdir(parents=True, exist_ok=True)
    _mk_schema(back, 7, messages=2)
    _mk_schema(live, 8, messages=5)
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(mcs_update, "precheck_local", lambda c: [])
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: sha)
    monkeypatch.setattr(mcs_update, "precheck_tag",
                        lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: str(back))
    monkeypatch.setattr(mcs_update, "_baseline_check", lambda c: [])
    def quiesce():
        mcs_update._write_marker()
        if mcs_update.journal_store.load_state(mcs_update.STATE_PATH).get("restore_consent"):
            raise mcs_update.UpdateError("drainer_stop_failed: synthetic")
        return []

    monkeypatch.setattr(mcs_update, "quiesce", quiesce)
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda: (mcs_update._remove_marker(), [])[1])
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(service_management, 'retire_to_snapshot', lambda m, **_service_kwargs: [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)

    def post_merge(_s, *, run_lock_fd):
        raise mcs_update.UpdateError("post_merge_failed: synthetic")

    monkeypatch.setattr(mcs_update, "_run_post_merge", post_merge)
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
    monkeypatch.setattr(mcs_update, "quiesce",
                        lambda: pytest.fail("rollback must not quiesce"))
    assert mcs_update.rollback() == 0
    assert calls == [1]
    assert mcs_update.journal_store.load_state(mcs_update.STATE_PATH)["applying"]["tag"] == "v1.0.6"


@pytest.mark.parametrize("outcomes,late,problems,boots,loads", [
    ([5, 0], False, [], 2, True),
    ([5, 5, 5], True, [], 3, True),
    ([5, 5, 5], False, ["bootstrap_failed:ai.mcs.x"], 3, True),
    # exit 0 is not proof: the label must answer `print` afterwards
    ([0], False, ["bootstrap_failed:ai.mcs.x"], 1, False),
])
def test_restart_agents_retries_transient_bootstrap(
        tmp_path, monkeypatch, outcomes, late, problems, boots, loads):
    from types import SimpleNamespace
    fake = _FakeLaunchd(outcomes, late_load=late, loads=loads)
    sleeps = []
    monkeypatch.setattr(mcs_update.subprocess, "run", fake)
    monkeypatch.setattr(service_management, "time",
                        SimpleNamespace(time=time.time, sleep=sleeps.append))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ("ai.mcs.x",))
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_agent_pid",
                        lambda label: 4242 if fake.loaded else None)
    monkeypatch.setattr(mcs_update, "_remove_marker", lambda: None)
    assert mcs_update.restart_agents() == problems
    assert fake.calls[0] == "bootout"
    assert fake.calls.count("bootstrap") == boots
    assert sleeps.count(1) == sum(rc != 0 for rc in outcomes)


@pytest.mark.parametrize("outcomes,late,loads,problems", [
    ([5, 0], False, True, []),
    ([5, 5, 5], True, True, []),
    ([5, 5, 5], False, True, ["watcher_not_loaded:ai.mcs.w"]),
    ([0], False, False, ["watcher_not_loaded:ai.mcs.w"]),
])
def test_restart_agents_watcher_uses_verified_bootstrap(
        tmp_path, monkeypatch, outcomes, late, loads, problems):
    """An unloaded watcher goes through the shared verified bootstrap —
    its result decides, not a second ad-hoc `print`."""
    from types import SimpleNamespace
    fake = _FakeLaunchd(outcomes, late_load=late, loads=loads)
    monkeypatch.setattr(mcs_update.subprocess, "run", fake)
    monkeypatch.setattr(service_management, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ("ai.mcs.w",))
    monkeypatch.setattr(mcs_update, "_remove_marker", lambda: None)
    assert mcs_update.restart_agents() == problems
    assert fake.calls == ["print"] + ["bootstrap"] * len(outcomes) + ["print"]


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


_HANG_EXCS = [subprocess.TimeoutExpired(["launchctl"], 30),
              FileNotFoundError("launchctl")]


@pytest.mark.parametrize("exc", _HANG_EXCS)
@pytest.mark.parametrize("label,verb,problems", [
    ("ai.mcs.a", "bootout", []),
    ("ai.mcs.a", "bootstrap", ["bootstrap_failed:ai.mcs.a"]),
    ("ai.mcs.a", "print", ["bootstrap_failed:ai.mcs.a"]),
    ("local.mcs-w", "print", ["watcher_not_loaded:local.mcs-w"]),
    ("local.mcs-w", "bootstrap", ["watcher_not_loaded:local.mcs-w"]),
])
def test_restart_agents_survives_hung_launchctl(
        updater, tmp_path, monkeypatch, label, verb, problems, exc):
    from types import SimpleNamespace
    fake = _HungLaunchd(label, verb, exc)
    monkeypatch.setattr(mcs_update.subprocess, "run", fake)
    monkeypatch.setattr(service_management, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS',
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ("local.mcs-w",))
    Path(mcs_update.MARKER_PATH).write_text("1")
    assert mcs_update.restart_agents() == problems
    # the loop went on past the wedged label to every later agent
    assert ("bootstrap", "ai.mcs.b") in fake.calls
    assert fake.calls[-1][1] == "local.mcs-w"
    assert not os.path.exists(mcs_update.MARKER_PATH)


def test_launchd_waits_ignore_wall_clock_jump(updater, monkeypatch):
    """quiesce/restart_agents pid waits run on the monotonic clock: a
    sleep/NTP jump of time.time() must not end them on the first poll."""
    from types import SimpleNamespace
    wall, mono = [1000.0], [0.0]

    def sleep(s):
        mono[0] += s
        wall[0] += 3600                # system clock jumps an hour per poll
    monkeypatch.setattr(mcs_update, "time", SimpleNamespace(
        time=lambda: wall[0], monotonic=lambda: mono[0], sleep=sleep))
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ("ai.mcs.a",))
    monkeypatch.setattr(service_contract, 'LEGACY_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_run", lambda argv:
                        subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(mcs_update, "_bootstrap_agent", lambda label, plist: True)
    monkeypatch.setattr(mcs_update, "_stray_drainer_pids", lambda: [])
    pids = iter([4242, 4242, None, None])
    monkeypatch.setattr(mcs_update, "_agent_pid", lambda label, unknown=None: next(pids))
    assert mcs_update.quiesce() == ["ai.mcs.a"]
    pids = iter([None, None, 4343])
    assert mcs_update.restart_agents() == []


@pytest.mark.parametrize("exc", _HANG_EXCS)
def test_quiesce_never_takes_unverifiable_stop_as_stopped(
        updater, monkeypatch, exc):
    """A hung `print` cannot prove the drainer stopped — quiesce fails
    closed (drainer_stop_failed) instead of merging under a live one;
    a hung `bootout` alone is fine once `print` confirms the stop."""
    from types import SimpleNamespace
    clock = [1000.0]
    monkeypatch.setattr(mcs_update, "time", SimpleNamespace(
        time=lambda: clock[0], monotonic=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS',
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(mcs_update, "_stray_drainer_pids", lambda: [])
    monkeypatch.setattr(mcs_update.subprocess, "run",
                        _HungLaunchd("ai.mcs.a", "bootout", exc))
    assert mcs_update.quiesce() == ["ai.mcs.a", "ai.mcs.b"]
    monkeypatch.setattr(mcs_update.subprocess, "run",
                        _HungLaunchd("ai.mcs.b", "print", exc))
    with pytest.raises(mcs_update.UpdateError,
                       match="drainer_stop_failed: ai.mcs.b"):
        mcs_update.quiesce()


@pytest.mark.parametrize("exc", _HANG_EXCS)
def test_rollback_partial_quiesce_restarts_despite_hung_launchctl(
        updater, tmp_path, monkeypatch, exc):
    """quiesce stops drainer a, then b's stop is unverifiable: rollback
    must still restart a (H4), report b, consume the receipt and drop
    the marker — not escape with a raw TimeoutExpired/OSError."""
    from types import SimpleNamespace
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait",
                        lambda **kw: os.open(mcs_update.RUN_LOCK,
                                             os.O_WRONLY | os.O_CREAT))
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
    monkeypatch.setattr(service_management, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS',
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_stray_drainer_pids", lambda: [])
    monkeypatch.setattr(mcs_update, "_rollback_tree",
                        lambda e: pytest.fail("must not reset"))
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    state = updater.journal_store.default_state()
    state["applied"] = [{"tag": "v1.1.0",
                         "sha": _git(repo, "rev-parse", "HEAD").stdout.strip(),
                         "prev_sha": _git(repo, "rev-list", "-n1", "v1.0.0").stdout.strip(),
                         "at": time.time()}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.rollback("cid-rb") == 1
    assert ("bootstrap", "ai.mcs.a") in fake.calls   # a brought back
    assert "ai.mcs.a" in fake.loaded
    detail = updater.journal_store.load_state(updater.STATE_PATH)["executed"]["cid-rb"]["detail"]
    assert "drainer_stop_failed: ai.mcs.b" in detail
    assert "bootstrap_failed:ai.mcs.b" in detail
    assert not os.path.exists(mcs_update.MARKER_PATH)
    assert len(fake.calls) < 100  # finite stop polling and restart retries


@pytest.mark.parametrize("exc", _HANG_EXCS)
def test_recover_escalates_when_services_reconcile_hangs(
        updater, tmp_path, monkeypatch, exc):
    """head == target resume: a hung/missing `mcs_setup services` child
    escalates (drainers back, marker gone, receipt consumed) instead of
    escaping recover with drainers still quiesced."""
    import sys
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    real_run = owned_process.run_owned

    def run(argv, timeout, env, stdin=None):
        if argv[0] == sys.executable:
            raise exc
        return real_run(argv, timeout, env, stdin)
    monkeypatch.setattr(owned_process, 'run_owned', run)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": head,
                         "prev_sha": "0" * 40, "command_id": "cid-a",
                         "at": time.time() - 4000}
    state["stages"] = [{"stage": "post_merge", "at": time.time() - 3000}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    Path(mcs_update.MARKER_PATH).write_text("1")
    assert updater.recover_interrupted() == 1
    after = updater.journal_store.load_state(updater.STATE_PATH)
    assert after["executed"]["cid-a"]["result"] == "escalated"
    assert "services_failed" in after["executed"]["cid-a"]["detail"]
    assert not os.path.exists(mcs_update.MARKER_PATH)


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


def test_recover_git_failure_escalates_outside_consent_hold(
        updater, tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    restarts, notices = [], []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda: restarts.append(1) or [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "a" * 40,
                         "prev_sha": "b" * 40, "command_id": "cid-a",
                         "at": time.time()}
    state["stages"] = [{"stage": "merge", "at": time.time()}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    Path(mcs_update.MARKER_PATH).write_text("1")
    _hang_git(monkeypatch)
    assert updater.recover_interrupted() == 1
    after = updater.journal_store.load_state(updater.STATE_PATH)
    assert after["executed"]["cid-a"]["result"] == "escalated"
    assert "git_timeout" in after["executed"]["cid-a"]["detail"]
    assert after["applying"]["sha"] == "a" * 40   # journal kept: retry
    assert restarts == [1]
    assert not os.path.exists(mcs_update.MARKER_PATH)
    assert notices and "要手動対応" in notices[0]


def test_recover_git_failure_keeps_consent_hold_then_converges(
        updater, tmp_path, monkeypatch):
    """Inside a restore-consent hold an unmeasurable tree keeps the
    freeze (drainers down, both markers, receipt pending, no outbox
    write that would void the loss report). After repair, the approved old-schema
    copy stays held until compatible notification reconciliation is possible."""
    import notify_spool
    repo, live, back, _b, _a, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    assert updater.rollback("cid-rb") == 2
    notices = []
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    real_owned = owned_process.run_owned
    _hang_git(monkeypatch)
    assert updater.recover_interrupted() == 1
    assert restarts == [] and notices == []
    assert Path(mcs_update.MARKER_PATH).exists()
    assert notify_spool.restore_awaiting_consent(
        str(tmp_path / "data")) is not None
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert state["restore_consent"] and state["applying"]["rollback"]
    assert "cid-rb" not in state.get("executed", {})
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "restore_consent_blocked"
    monkeypatch.setattr(owned_process, 'run_owned', real_owned)
    _seed_consent(live, back)
    assert updater.recover_interrupted() == 1
    _assert_old_schema_restore_held(updater, live, back, restarts, "cid-rb")
    assert notices == []


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


@pytest.mark.parametrize("outcome", [
    subprocess.TimeoutExpired(["pgrep"], 30),
    FileNotFoundError("pgrep"),
    subprocess.CompletedProcess(["pgrep"], 3, "", "internal error")])
def test_quiesce_fails_closed_when_stray_check_unverifiable(
        updater, monkeypatch, outcome):
    def run(argv, *a, **k):
        assert argv[0] == "pgrep"
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    monkeypatch.setattr(mcs_update.subprocess, "run", run)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    assert mcs_update._stray_drainer_pids() is None
    with pytest.raises(mcs_update.UpdateError,
                       match="stray_drainer_unverifiable"):
        mcs_update.quiesce()


def test_stray_check_no_match_is_clean(updater, monkeypatch):
    monkeypatch.setattr(
        mcs_update.subprocess, "run",
        lambda argv, *a, **k: subprocess.CompletedProcess(argv, 1, "", ""))
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    assert mcs_update._stray_drainer_pids() == []
    assert mcs_update.quiesce() == []


# ------------------------------------ bounded restart (T_POST_MERGE)

def test_restart_agents_all_hung_stays_within_budget(
        updater, tmp_path, monkeypatch):
    """Every launchctl call hangs until its timeout: restart_agents must
    finish far enough under T_POST_MERGE that the post-merge child (with
    services 120s + postcheck) is not killed mid-restart, reporting the
    labels it never reached."""
    from types import SimpleNamespace
    clock = [0.0]

    def advance(s):
        clock[0] += s

    def run(argv, *a, **k):
        advance(k["timeout"])
        raise subprocess.TimeoutExpired(argv, k["timeout"])
    fake_time = SimpleNamespace(time=lambda: clock[0],
                                monotonic=lambda: clock[0], sleep=advance)
    monkeypatch.setattr(mcs_update.subprocess, "run", run)
    monkeypatch.setattr(mcs_update, "time", fake_time)
    monkeypatch.setattr(service_management, "time", fake_time)
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS',
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS',
                        ("local.mcs-w", "local.mcs-v"))
    problems = mcs_update.restart_agents()
    # worst case: a label started just before the budget runs out costs
    # 78s more; services 120 + postcheck 206 follow in the same child
    assert clock[0] <= mcs_update.RESTART_BUDGET_S + 78
    assert mcs_update.RESTART_BUDGET_S + 78 + 326 < mcs_update.T_POST_MERGE
    assert problems == ["bootstrap_failed:ai.mcs.a",
                        "bootstrap_failed:ai.mcs.b",
                        "watcher_not_loaded:local.mcs-w",
                        "restart_deadline:local.mcs-v"]


# ------------------- apply delegating to recover never bails its journal

def test_apply_never_bails_a_foreign_journal_when_recover_raises(
        updater, tmp_path, monkeypatch):
    """A journal that appears between apply's first read and its locked
    re-read belongs to another run. recover_interrupted raising (e.g.
    OSError from save_state) must propagate like in rollback(), never
    reach bail(): the old code _rollback_tree'd that journal unlocked
    and overwrote it with this apply's failure."""
    foreign = updater.journal_store.default_state()
    foreign["applying"] = {"tag": "v1.1.0", "sha": "a" * 40,
                           "prev_sha": "b" * 40, "command_id": "other",
                           "at": time.time()}
    foreign["stages"] = [{"stage": "merge", "at": time.time()}]
    real_load = updater.journal_store.load_state
    reads = []

    def load_state(path):
        reads.append(1)
        return updater.journal_store.default_state() if len(reads) == 1 \
            else real_load(path)
    updater.journal_store.save_state(updater.STATE_PATH, foreign)
    before = Path(updater.STATE_PATH).read_bytes()
    monkeypatch.setattr(mcs_update.journal_store, "load_state", load_state)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    rolled, notices = [], []
    monkeypatch.setattr(mcs_update, "_rollback_tree", rolled.append)
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)

    def recover():
        raise OSError("disk full")
    monkeypatch.setattr(mcs_update, "recover_interrupted", recover)
    with pytest.raises(OSError, match="disk full"):
        updater.apply("v1.2.0", None, "mine")
    assert rolled == [] and notices == []
    assert Path(updater.STATE_PATH).read_bytes() == before
    fd = updater.acquire_update_lock()              # released
    assert fd is not None
    os.close(fd)


# ------------------------------------------ escalation notice dedup

def test_recover_escalation_notifies_once_per_condition(
        updater, tmp_path, monkeypatch):
    """Daily check / consent respawn re-run recover on the same stuck
    journal: one notice per condition, again on change or after
    ESCALATE_REALERT_S; a failed enqueue is retried next pass."""
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    monkeypatch.setattr(mcs_update, "restart_agents", lambda **k: [])
    notices, ok = [], [False]
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or ok[0])
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "a" * 40,
                         "prev_sha": "b" * 40, "at": time.time()}
    state["stages"] = [{"stage": "merge", "at": time.time()}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    _hang_git(monkeypatch)
    assert updater.recover_interrupted() == 1       # enqueue failed
    ok[0] = True
    for _ in range(3):
        assert updater.recover_interrupted() == 1
    assert len(notices) == 2                        # retry, then quiet
    state["stages"].append({"stage": "post_merge", "at": time.time()})
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.recover_interrupted() == 1
    assert len(notices) == 3                        # new condition
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    report["notified_at"] -= mcs_update.ESCALATE_REALERT_S
    Path(mcs_update.REPORT_PATH).write_text(json.dumps(report))
    assert updater.recover_interrupted() == 1
    assert len(notices) == 4


# ------------- repeated escalation: ensure drainers run, never re-bounce

def test_repeated_escalation_ensures_drainers_instead_of_bouncing(
        updater, tmp_path, monkeypatch):
    """Daily check / consent respawn re-escalate the same stuck journal:
    only the first escalation bounces drainers (bootout+bootstrap); later
    passes of the same journal + HEAD only ensure they run — the old code
    bounced healthy drainers on every pass. A changed journal bounces."""
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    calls = []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda **k: calls.append(k.get("bounce", True))
                        or [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "a" * 40,
                         "prev_sha": "b" * 40, "at": time.time()}
    state["stages"] = [{"stage": "merge", "at": time.time()}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    for _ in range(3):
        assert updater.recover_interrupted() == 1   # unclassifiable
    assert calls == [True, False, False]
    assert not os.path.exists(mcs_update.MARKER_PATH)
    state["stages"].append({"stage": "post_merge", "at": time.time()})
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.recover_interrupted() == 1
    assert calls[-1] is True                        # new condition


def test_resume_retry_bounces_once_then_only_ensures(
        updater, tmp_path, monkeypatch):
    """head == target resume whose postcheck keeps failing: the first
    pass restarts once (not again in its own escalate); later passes
    only ensure; a pass that had to reset a dirty tree bounces again
    (the code the drainers run changed)."""
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    calls = []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda **k: calls.append(k.get("bounce", True))
                        or [])
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(mcs_update, "_postcheck",
                        lambda s, t: ["version_mismatch"])
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # a real prev: the dirty-tree reset checks the path is one the update touches
    prev = _git(repo, "rev-list", "-n1", "v1.0.0").stdout.strip()
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": head,
                         "prev_sha": prev, "at": time.time()}
    state["stages"] = [{"stage": "post_merge", "at": time.time()}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater.recover_interrupted() == 1
    assert calls == [True, False]
    assert updater.recover_interrupted() == 1
    assert calls[2:] == [False, False]
    # A provable mixed checkout can reset; a third edit must instead be held.
    (repo / "f.txt").write_text("one")
    assert updater.recover_interrupted() == 1
    assert calls[4:] == [True, False]


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


def test_restart_agents_ensure_mode_never_bounces_a_running_drainer(
        updater, tmp_path, monkeypatch):
    from types import SimpleNamespace
    fake = _EnsureLaunchd(loaded={"ai.mcs.run", "ai.mcs.stopped"},
                          running={"ai.mcs.run"}, hung={"ai.mcs.hung"})
    monkeypatch.setattr(mcs_update.subprocess, "run", fake)
    monkeypatch.setattr(service_management, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS',
                        ("ai.mcs.run", "ai.mcs.stopped", "ai.mcs.gone",
                         "ai.mcs.hung"))
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    assert mcs_update.restart_agents(bounce=False) == [
        "bootstrap_failed:ai.mcs.hung"]
    assert not any(verb == "bootout" for verb, _ in fake.calls)
    assert [c for c in fake.calls if c[1] == "ai.mcs.run"] \
        == [("print", "ai.mcs.run")]                 # left alone
    assert ("kickstart", "ai.mcs.stopped") in fake.calls
    assert ("bootstrap", "ai.mcs.stopped") not in fake.calls
    assert ("bootstrap", "ai.mcs.gone") in fake.calls
    assert fake.running >= {"ai.mcs.run", "ai.mcs.stopped", "ai.mcs.gone"}
    # unverifiable is never read as running: a start was attempted
    assert ("bootstrap", "ai.mcs.hung") in fake.calls


def test_restart_agents_slow_drainer_does_not_starve_the_next(
        updater, tmp_path, monkeypatch):
    """The 15s pid wait used to overwrite the RESTART_BUDGET_S deadline:
    one drainer that never came up made every later label
    restart_deadline without even being started (H4)."""
    from types import SimpleNamespace
    fake = _EnsureLaunchd(inert={"ai.mcs.a"})
    clock = iter(range(0, 10 ** 6))
    monkeypatch.setattr(mcs_update.subprocess, "run", fake)
    monkeypatch.setattr(mcs_update, "time", SimpleNamespace(
        time=time.time, monotonic=lambda: next(clock), sleep=lambda s: None))
    monkeypatch.setattr(service_management, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS',
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    assert mcs_update.restart_agents() == ["drainer_not_running:ai.mcs.a"]
    assert "ai.mcs.b" in fake.running


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


@pytest.mark.parametrize("content", [
    None, b"", b"{bad", b"[]", b'{"phase": "awaiting_consent", "report_id": "r"}',
    b'{"phase": "restored"}', b'{"restored_at": 1}', b'{"phase": "other"}'])
def test_awaiting_consent_marker_matches_notify_cards(tmp_path, monkeypatch,
                                                     content):
    """recover reads the hold marker without importing notify_spool (a
    rolled-back tree may predate it) — same fail-closed verdict."""
    import notify_spool
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path))
    if content is not None:
        (tmp_path / "restore_pending.json").write_bytes(content)
    assert (mcs_update._awaiting_consent_marker() is None) \
        == (notify_spool.restore_awaiting_consent(str(tmp_path)) is None)


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
    monkeypatch.setattr(mcs_update, "impact_summary", lambda a, b: [])
    monkeypatch.setattr(mcs_update, "scan_pending_approvals",
                        lambda state: ([], []))
    calls = []

    def fake_git(args, timeout=None):
        calls.append(args)
        rc = ancestry_rc if args[0] == "merge-base" else 0
        return subprocess.CompletedProcess(args, rc, "", "")
    monkeypatch.setattr(mcs_update, "_git", fake_git)
    notices, applied = [], []
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    monkeypatch.setattr(mcs_update, "apply",
                        lambda *a, **k: applied.append(a) or 0)
    assert mcs_update.cmd_check() == 0
    assert ["merge-base", "--is-ancestor", "a" * 40, "HEAD"] in calls
    assert bool(notices) is expect_notice
    assert bool(applied) is expect_notice


# A failed post-merge schema-bump rollback in apply()'s bail must stay
# recoverable: recovery retries the DB restore (or escalates) instead of
# 'finishing' as interrupted_pre_merge with old code on the newer DB.
def test_failed_bail_rollback_is_not_finished_as_pre_merge(
        updater, tmp_path, monkeypatch):  # noqa: F811
    import maintenance
    notices = []
    repo, _ = _make_repo(tmp_path)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    live = str(tmp_path / "data" / "ledger.db")
    monkeypatch.setattr(mcs_update, "LEDGER", live)
    back = tmp_path / "data" / "backups" / "pre.db"
    back.parent.mkdir(parents=True, exist_ok=True)
    _mk_schema(back, 7, messages=2)
    _mk_schema(live, 8, messages=5)
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(mcs_update, "precheck_local", lambda c: [])
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: sha)
    monkeypatch.setattr(mcs_update, "precheck_tag",
                        lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: str(back))
    monkeypatch.setattr(mcs_update, "_baseline_check", lambda c: [])
    monkeypatch.setattr(mcs_update, "quiesce",
                        lambda: (mcs_update._write_marker(), [])[1])
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda **k: (mcs_update._remove_marker(), [])[1])
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(service_management, 'retire_to_snapshot', lambda m, **_service_kwargs: [])
    monkeypatch.setattr(mcs_update, "_postcheck", lambda s, e: [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: notices.append(a[0]) or True)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)

    def post_merge(_sha, *, run_lock_fd):
        back.write_bytes(b"garbage")          # restore will fail
        raise mcs_update.UpdateError("post_merge_failed: synthetic")
    monkeypatch.setattr(mcs_update, "_run_post_merge", post_merge)

    assert mcs_update.apply("v1.1.0", sha, "cid") == 1
    # frozen DB (restore incomplete): report only, no outbox write
    assert notices == []
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "escalate" and report["writers_stopped"]
    assert Path(mcs_update.MARKER_PATH).exists()
    st = mcs_update.journal_store.load_state(mcs_update.STATE_PATH)
    assert st["applying"]["restore_incomplete"] is True
    assert st["executed"]["cid"]["result"] == "failed"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    # journal is rollback-shaped: target is prev, restore still owed
    assert st["applying"]["rollback"] is True
    assert st["applying"]["sha"] == before

    mcs_update.recover_interrupted()
    assert notices == []
    st = mcs_update.journal_store.load_state(mcs_update.STATE_PATH)
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] != "interrupted_pre_merge"
    assert report["result"] == "escalate"
    assert "rollback db restore" in report["detail"]
    assert st["applying"] is not None          # still visible to humans
    assert (st.get("executed") or {}).get("cid", {}).get("result") \
        != "interrupted_recovered"
    assert restore_evidence.database_version(live) == 8


# Synthetic regressions for update rollback, delayed notices and restart scope.
@pytest.mark.parametrize("stop_fails", [False, True])
def test_post_merge_bail_stops_restarted_writers_before_rollback(updater, monkeypatch, stop_fails):
    repo = _apply_env(updater, monkeypatch)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    previous = updater._head_sha()
    target = _git(repo, "rev-parse", "v1.1.0^{commit}").stdout.strip()
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda tag, **_lif05_paths: target)
    monkeypatch.setattr(service_management, 'retire_to_snapshot', lambda snapshot, **_service_kwargs: [])
    events = []

    def quiesce():
        events.append("quiesce")
        updater._write_marker()
        if stop_fails and events.count("quiesce") == 2:
            raise updater.UpdateError("drainer_stop_failed: synthetic")
        return []

    def restarted_child(expected, *, run_lock_fd):
        events.append("child_restart")
        updater._remove_marker()
        raise updater.UpdateError("post_merge_failed: synthetic")

    rollback = updater._rollback_tree

    def restore(entry, on_hold):
        events.append("rollback")
        assert Path(updater.MARKER_PATH).exists()
        rollback(entry, on_hold)

    monkeypatch.setattr(updater, "quiesce", quiesce)
    monkeypatch.setattr(updater, "_run_post_merge", restarted_child)
    monkeypatch.setattr(updater, "_rollback_tree", restore)
    monkeypatch.setattr(updater, "restart_agents", lambda: events.append("restart") or [])
    assert updater.apply("v1.1.0", target, "synthetic-command") == 1
    assert events[:3] == ["quiesce", "child_restart", "quiesce"]
    state = updater.journal_store.load_state(updater.STATE_PATH)
    if stop_fails:
        assert "rollback" not in events and "restart" not in events
        assert updater._head_sha() == target
        assert state["applying"] and state["stages"]
        assert Path(updater.MARKER_PATH).exists()
    else:
        assert events[3:] == ["rollback", "restart"]
        assert updater._head_sha() == previous
        assert not state["applying"] and not state["stages"]
        assert not Path(updater.MARKER_PATH).exists()


def test_drug_info_hook_never_delays_recovery_or_an_execution(updater, monkeypatch):
    monkeypatch.setattr(mcs_util, "load_config", lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(update_discovery, "detect_latest", lambda pre, **_paths: (None, None))
    monkeypatch.setattr(update_discovery, "current_version", lambda **_paths: ("v1.0.0", "b" * 40))
    monkeypatch.setattr(updater, "scan_pending_approvals", lambda state: ([], []))
    hooks = []
    monkeypatch.setattr(updater, "_drug_info_notices", hooks.append)
    assert updater.cmd_check() == 0 and len(hooks) == 1      # nothing to execute
    monkeypatch.setattr(updater, "scan_pending_approvals",
                        lambda state: ([{"rollback": True, "command_id": "c1"}], []))
    monkeypatch.setattr(updater, "rollback", lambda command_id=None: 0)
    assert updater.cmd_check() == 0 and len(hooks) == 1      # approved rollback first
    state = updater.journal_store.default_state()
    state["stages"].append({"stage": "local_checks", "at": 1.0})
    updater.journal_store.save_state(updater.STATE_PATH, state)
    monkeypatch.setattr(updater, "recover_interrupted", lambda: 0)
    assert updater.cmd_check() == 0 and len(hooks) == 1      # recovery first


def test_deferred_update_notice_refetches_release_context(updater, monkeypatch):
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(update_discovery, 'detect_latest', lambda pre, **_lif05_paths: ("v1.1.0", "a" * 40))
    monkeypatch.setattr(update_discovery, 'current_version', lambda**_lif05_paths: ("v1.0.0", "b" * 40))
    monkeypatch.setattr(updater, "scan_pending_approvals", lambda state: ([], []))
    monkeypatch.setattr(updater, "_git", lambda args, **kw:
                        subprocess.CompletedProcess(args, int(args[0] == "merge-base"), "", ""))
    fetched, notices = [], []
    monkeypatch.setattr(updater, "fetch_notes", lambda tag: fetched.append(tag) or "synthetic release notes")
    monkeypatch.setattr(updater, "impact_summary", lambda current, tag: ["synthetic restart required"])
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text, **kw:
                        notices.append(text) or len(notices) > 1)
    assert updater.cmd_check() == 0
    assert updater.journal_store.load_state(updater.STATE_PATH)["latest_tag"] == "v1.1.0"
    assert updater.journal_store.load_state(updater.STATE_PATH).get("notified_at") != "v1.1.0"
    assert updater.cmd_check() == 0
    assert fetched == ["v1.1.0", "v1.1.0"]
    assert "synthetic release notes" in notices[1]
    assert "synthetic restart required" in notices[1]
    assert updater.journal_store.load_state(updater.STATE_PATH)["notified_at"] == "v1.1.0"
    assert updater.cmd_check() == 0
    assert len(fetched) == 2 and len(notices) == 2


def test_core_change_requires_gateway_restart_and_notice(updater, monkeypatch):
    def diff(args):
        if "--name-status" in args:
            return "M\tmcs/notify/notify_cards.py\n"
        return "mcs/notify/notify_cards.py\0" if "mcs" in args else ""

    monkeypatch.setattr(updater, "_git_out", diff)
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: {"notify": {"interactive": "lineworks"}})
    assert updater._plugin_changed("v1.1.0", {})
    impact = updater.impact_summary("synthetic-before", "v1.1.0")
    assert any("通知接続（gateway）の再起動" in line for line in impact)
    assert any("LINE WORKS" in line and "再起動" in line for line in impact)


def test_lineworks_impact_only_when_the_adapter_is_in_use(updater, monkeypatch):
    monkeypatch.setattr(updater, "_git_out", lambda args: "M\tmcs/notify/notify_cards.py\n")
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
    calls = []
    monkeypatch.setattr(updater, "_git", lambda args, **kw: calls.append(args[0])
                        or subprocess.CompletedProcess(args, 0, "", ""))   # ancestor of HEAD
    fetched = []
    monkeypatch.setattr(updater, "fetch_notes", lambda tag: fetched.append(tag) or "notes")
    monkeypatch.setattr(updater, "impact_summary", lambda current, tag: [])
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text, **kw: pytest.fail("no notice"))
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
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {"runtime_mode": "synthetic-invalid"})
    monkeypatch.setattr(updater, "precheck_local", lambda cfg: [])
    result = updater.plan("v1.1.0")
    assert result["route"] == "blocked"
    assert any(b.startswith("plan_failed: runtime_mode_invalid") for b in result["blockers"])
    monkeypatch.setattr(updater, "_git_out", lambda args: "M\tmcs/notify/notify_cards.py\n")
    with pytest.raises(ValueError):
        updater.impact_summary("synthetic-before", "v1.1.0")
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {
        "runtime_mode": "synthetic-invalid", "update": {"mode": "notify"}})
    monkeypatch.setattr(update_discovery, 'detect_latest', lambda pre, **_lif05_paths: ("v1.1.0", "a" * 40))
    monkeypatch.setattr(update_discovery, 'current_version', lambda**_lif05_paths: ("v1.0.0", "b" * 40))
    monkeypatch.setattr(updater, "scan_pending_approvals", lambda state: ([], []))
    monkeypatch.setattr(updater, "_git", lambda args, **kw: subprocess.CompletedProcess(args, 1, "", ""))
    monkeypatch.setattr(updater, "fetch_notes", lambda tag: "notes")
    notices = []
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text, **kw: notices.append(text) or True)
    assert updater.cmd_check() == 0 and notices


@pytest.mark.parametrize("print_code", [113, 1, 124])
def test_membership_removes_an_agent_plist_only_after_launchd_says_gone(
        updater, tmp_path, monkeypatch, print_code):
    agents = tmp_path / "update-agents"
    agents.mkdir()
    plist = agents / "ai.mcs.extract-retired.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(updater, "AGENTS_DIR", str(agents))
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda path: False)
    monkeypatch.setattr(updater.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, print_code if argv[1] == "print" else 0, "", ""))
    monkeypatch.setattr(owned_process, "run_owned", lambda argv, **kw: subprocess.CompletedProcess(
        argv, print_code if argv[1] == "print" else 0, "", ""))
    problems = service_management.retire_to_snapshot({'cron': [], 'agents': []}, cfg=mcs_util.load_config(), paths=updater._service_paths())
    gone = print_code == 113
    assert plist.exists() is not gone
    assert ("agent_retirement_unverifiable:ai.mcs.extract-retired" in problems) is not gone


@pytest.mark.parametrize(("code", "expected"), [(113, None), (1, "unknown"), (124, "unknown")])
def test_agent_pid_treats_only_not_found_as_stopped(updater, monkeypatch, code, expected):
    monkeypatch.setattr(updater.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, code, "", ""))
    assert updater._agent_pid("ai.mcs.synthetic", unknown="unknown") == expected


def test_stopping_an_update_child_closes_its_pipes(updater):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    real = child.communicate

    def interrupted(*a, **k):        # reaping must still close the pipes
        raise subprocess.TimeoutExpired(child.args, 5)
    child.communicate = interrupted
    updater._stop_update_child(child)
    child.communicate = real
    assert child.returncode is not None
    assert child.stdout.closed and child.stderr.closed


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
    monkeypatch.setattr(mcs_update.mcs_util, "CONF_PATH", str(tmp_path / "config.json"))
    if body is not None:
        (tmp_path / "install_options.json").write_text(body)
    assert mcs_update.recorded_install_args() == expected


def test_recorded_install_args_ignores_a_symlinked_record(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path))
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
    monkeypatch.setattr(mcs_update, "_tree_clean", lambda: True)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(mcs_update, "_agent_pid", lambda label: 123)
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


@pytest.mark.parametrize('marker', ['release-owner.json', 'release-owner-activation.json'])
@pytest.mark.parametrize('entry', ['apply', 'rollback', 'recover', 'reinstall_done', 'check'])
def test_release_activation_blocks_legacy_before_journal_or_cleanup(updater, tmp_path, monkeypatch, marker, entry):
    import release_apply_owner
    # pytest can reuse a removed tmp_path; the private census must belong to this test user too.
    user_home = tmp_path / 'synthetic-user-home'
    user_home.mkdir(mode=0o700)
    monkeypatch.setenv('HOME', str(user_home))
    root = release_apply_owner._control_root(updater.HOME)
    with release_apply_owner._control_store(updater.HOME) as store:
        store.write(marker, {'synthetic': True})
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {'sdc': {'state_root': str(root)}, 'update': {'mode': 'notify'}})
    monkeypatch.setattr(updater, '_apply_runtime_gate_errors', lambda *_: [])
    monkeypatch.setattr(updater, 'journal', lambda *_: pytest.fail('legacy journal forbidden'))
    monkeypatch.setattr(updater, '_remove_marker', lambda: pytest.fail('cleanup forbidden'))
    monkeypatch.setattr(updater, '_acquire_run_lock_wait', lambda **_: pytest.fail('legacy run lock forbidden'))
    before = Path(updater.STATE_PATH).read_bytes() if Path(updater.STATE_PATH).exists() else None
    if entry == 'apply':
        result = updater.apply('v1.1.0', 'a' * 40, None)
    elif entry == 'rollback':
        result = updater.rollback()
    elif entry == 'reinstall_done':
        result = updater.reinstall_done()
    elif entry == 'check':
        monkeypatch.setattr(update_discovery, 'detect_latest', lambda *_, **__: ('v1.1.0', 'a' * 40))
        monkeypatch.setattr(update_discovery, 'current_version', lambda **_: ('v1.0.0', 'b' * 40))
        monkeypatch.setattr(updater, '_tag_is_newer', lambda *_: True)
        monkeypatch.setattr(updater, '_git', lambda *_, **__: pytest.fail('fetch forbidden'))
        result = updater.cmd_check()
    else:
        result = updater.recover_interrupted()
    assert result == 2
    assert (Path(updater.STATE_PATH).read_bytes() if Path(updater.STATE_PATH).exists() else None) == before


def test_release_reference_and_unsafe_fixed_control_root_are_legacy_holds(updater):
    import release_apply_owner
    assert updater._release_legacy_blocked({'applying': {'release_ref': {}}})
    assert not updater._release_legacy_blocked({})
    with release_apply_owner._control_store(updater.HOME) as store:
        root = store.root
    gate = root / 'release-entry.lock'
    gate.chmod(0o644)
    assert updater._release_legacy_blocked({})
    assert gate.stat().st_mode & 0o777 == 0o644  # No repair by the reader.
    gate.chmod(0o600)
    (root / 'release-owner-activation.json').write_bytes(b'invalid-json')
    assert updater._release_legacy_blocked({})


def test_fixed_release_fence_survives_lost_sdc_config(updater, monkeypatch):
    import release_apply_owner
    with release_apply_owner._control_store(updater.HOME) as store:
        store.write('release-owner-activation.json', {'synthetic': True})
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(updater, '_apply_pipeline', lambda *_a, **_k: pytest.fail('legacy pipeline after config loss'))
    assert updater.apply('v1.1.0', 'a' * 40, None) == 2


@pytest.mark.parametrize('entry', [False, True, 'saved-receipt', {}, None])
def test_post_merge_native_issuer_refuses_unregistered_entry_without_launch(updater, monkeypatch, entry):
    if entry is None:
        entry = release_apply_owner._EffectCapability()
    monkeypatch.setattr(updater.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('native refusal spawned a child'))
    with pytest.raises(release_apply_owner.ReleaseApplyError, match='CHILD_HOLD'):
        updater._run_post_merge('a' * 40, run_lock_fd=10, _release_entry=entry)


def test_static_release_target_checks_keep_tree_and_runtime_refusals_without_running_candidate(updater, tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    work = tmp_path / 'remote-work'
    (work / 'mcs/core').mkdir(parents=True)
    (work / 'mcs/core/ledger.py').write_text('SCHEMA_VERSION = 8\n')
    marker = tmp_path / 'candidate-was-executed'
    (work / 'mcs/mcs_setup.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n')
    (work / 'data').mkdir()
    (work / 'data/synthetic').write_text('synthetic')
    (work / 'link.sh').symlink_to('f.txt')
    (work / 'install.sh').write_text('# synthetic installer\n')
    (work / 'deployment').mkdir()
    (work / 'deployment/sdc-release-lock.json').write_text('{}')
    (work / 'deployment/requirements-standalone.txt').write_text('synthetic')
    _git(work, 'add', '.')
    _git(work, 'commit', '-qm', 'synthetic target')
    target = _git(work, 'rev-parse', 'HEAD').stdout.strip()
    _git(work, 'push', '-q', str(tmp_path / 'remote.git'), 'main')
    _git(repo, 'fetch', '-q', 'origin')
    _mk_schema(tmp_path / 'ledger.db', 8)
    monkeypatch.setattr(updater, 'REPO', str(repo))
    monkeypatch.setattr(updater, 'LEDGER', str(tmp_path / 'ledger.db'))
    run = updater._run_with_lifetime
    def only_git(argv, **kwargs):
        assert argv[0] == 'git', 'static target checks ran candidate code'
        return run(argv, **kwargs)
    monkeypatch.setattr(updater, '_run_with_lifetime', only_git)
    errors, entries = updater._precheck_target_tree(target, False)
    assert entries and any(e.startswith('protected_path:') for e in errors)
    assert any(e.startswith('bad_entry_type:') for e in errors)
    assert updater._precheck_target_runtime_files(target, True) == [
        'install_sh_changed', 'release_dependency_lock_changed', 'standalone_requirements_changed']
    assert not marker.exists()


def test_release_static_schema_must_match_the_authenticated_core(updater, tmp_path, monkeypatch):
    monkeypatch.setattr(updater, '_ls_tree_paths', lambda *_a, **_k: [])
    monkeypatch.setattr(updater, '_git', lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, '', ''))
    monkeypatch.setattr(updater, '_git_out', lambda argv, **kwargs: 'SCHEMA_VERSION = 9' if argv[0] == 'show' else '')
    errors, _ = updater._precheck_target_tree('a' * 40, False, deadline=time.monotonic() + 3,
        installed_schema=8, expected_target_schema=8)
    assert 'release_schema_target_mismatch' in errors


def test_release_git_scope_uses_fixed_binary_and_environment_after_quiesce(tmp_path, monkeypatch):
    import os
    import shlex
    import subprocess
    import mcs_update as updater
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['/usr/bin/git', '-C', str(repo), 'init', '-q'], check=True)
    (repo / 'tracked').write_text('synthetic\n')
    subprocess.run(['/usr/bin/git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['/usr/bin/git', '-C', str(repo), '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
        'commit', '-qm', 'synthetic'], check=True)
    expected = subprocess.check_output(['/usr/bin/git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['/usr/bin/git', '-C', str(repo), 'config', 'core.worktree', str(tmp_path / 'foreign')], check=True)
    marker = tmp_path / 'untrusted-git-ran'
    fake = tmp_path / 'bin'
    fake.mkdir()
    executable = fake / 'git'
    executable.write_text('#!/bin/sh\nprintf invoked > ' + shlex.quote(str(marker)) + '\nexit 77\n')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(fake))
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'foreign.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(tmp_path / 'foreign'))
    monkeypatch.setattr(updater, 'REPO', str(repo))
    with updater._release_git_scope():
        assert updater._head_sha() == expected
        assert updater._tree_clean()
    assert not marker.exists()
    assert os.environ['PATH'] == str(fake)
    assert updater._RELEASE_GIT_SCOPE.get() is False


def test_guided_apply_stops_if_approved_options_changed(updater, monkeypatch, tmp_path, capsys):
    config = Path(mcs_util.CONF_PATH)
    config.write_text('{"install_args": ["--no-llm"]}')
    monkeypatch.setenv('MCS_UPDATE_OPTIONS_EXPECTED', '[]')
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(updater, '_apply_runtime_gate_errors', lambda *_: [])
    assert updater._apply_pipeline('v1.0.23', None, None, reinstall=True) == 1
    assert '導入設定が変更' in capsys.readouterr().out
    assert not Path(updater.STATE_PATH).exists()
    assert json.loads(config.read_text()) == {'install_args': ['--no-llm']}


def test_manual_apply_accepts_different_args_before_busy_lock(updater, monkeypatch, capsys):
    config = Path(mcs_util.CONF_PATH)
    config.write_text('{"install_args": ["--no-llm"]}')
    monkeypatch.delenv('MCS_UPDATE_OPTIONS_EXPECTED', raising=False)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(updater, '_apply_runtime_gate_errors', lambda *_: [])
    snapshot = updater._install_options_snapshot
    captured = []
    monkeypatch.setattr(updater, '_install_options_snapshot', lambda: captured.append(snapshot()) or captured[-1])
    monkeypatch.setattr(updater, 'acquire_update_lock', lambda: None)
    assert updater._apply_pipeline('v1.0.23', None, None, reinstall=True, install_args=('--no-brew',)) == 2
    assert '別の導入または更新が進行中' in capsys.readouterr().out
    assert captured[0][1] == ['--no-llm']
    assert not Path(updater.STATE_PATH).exists()


def test_interrupted_update_recovery_precedes_changed_guided_options(updater, monkeypatch):
    state = updater.journal_store.default_state()
    state['applying'] = {'tag': 'v1.0.23', 'sha': 'a' * 40, 'prev_sha': 'b' * 40}
    updater.journal_store.save_state(updater.STATE_PATH, state)
    monkeypatch.setenv('MCS_UPDATE_OPTIONS_EXPECTED', '[]')
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(updater, '_apply_runtime_gate_errors', lambda *_: [])
    monkeypatch.setattr(updater, '_install_options_snapshot', lambda: pytest.fail('recovery must take priority'))
    monkeypatch.setattr(updater, 'recover_interrupted', lambda: 7)
    assert updater._apply_pipeline('v1.0.23', None, None, reinstall=True) == 7


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


@pytest.mark.parametrize(('config', 'legacy', 'expected'), [
    ('{}', None, 'none'),
    ('{}', '{"v": 1, "install_args": []}', 'required'),
    ('{"install_args": []}', '{"v": 1, "install_args": []}', 'none'),
    ('{"install_args": []}', '{"v": 1, "install_args": ["--no-llm"]}', 'unverified'),
    ('{}', '{broken', 'unverified'),
])
def test_release_migration_probe_is_read_only_and_returns_only_fixed_tokens(updater, tmp_path, config, legacy, expected):
    path = Path(mcs_util.CONF_PATH)
    path.write_text(config)
    record = Path(updater.DATA) / 'install_options.json'
    if legacy is not None:
        record.write_text(legacy)
    before = path.read_bytes(), record.read_bytes() if record.exists() else None
    assert updater._release_migration_probe(time.monotonic() + 10) == expected
    assert (path.read_bytes(), record.read_bytes() if record.exists() else None) == before


def test_release_migration_probe_refuses_timeout(updater, monkeypatch):
    def expired(*_a, **_k):
        raise subprocess.TimeoutExpired('synthetic', 1)
    monkeypatch.setattr(owned_process, 'run_owned', expired)
    assert updater._release_migration_probe(time.monotonic() + 10) == 'unverified'
