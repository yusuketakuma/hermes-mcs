"""mcs_update — updater state, tag detection, gates, receipt queue.

Fully synthetic: temp git repos + temp sqlite + stubbed subprocess/
network. No real MCS, Discord, Keychain, or external repo access.
"""
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
from ops_testkit import (_git, _make_repo, _mk_schema, _receipts_db,
                         _seed_consent)


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
    import mcs_setup
    monkeypatch.setattr(updater, "_tree_clean", lambda: True)
    monkeypatch.setattr(mcs_setup, "_standalone_py_problem", lambda cfg: None)
    monkeypatch.setattr(mcs_setup, "_hermes_exe", lambda cfg: pytest.fail("Hermes lookup forbidden"))
    assert updater.precheck_local({"runtime_mode": "standalone", "mcs_login_id": "u", "notify_target": "local"}) == []


def test_standalone_quiesce_and_restart_keep_the_host_alive(updater, monkeypatch):
    monkeypatch.setattr(updater, "load_config", lambda: {"runtime_mode": "standalone"})
    _standalone_heartbeat(updater, update_in_progress=True, children={"update": {"pid": os.getpid(), "kind": "core"}})
    monkeypatch.setattr(updater, "_run", lambda *a, **k: pytest.fail("native service must not be stopped"))
    assert updater.quiesce() == ["extract-0", "extract-2"]
    assert Path(updater.MARKER_PATH).exists()
    _standalone_heartbeat(updater)
    assert updater.restart_agents() == []
    assert not Path(updater.MARKER_PATH).exists()


def test_standalone_quiesce_fails_closed_on_missing_host(updater, monkeypatch):
    monkeypatch.setattr(updater, "load_config", lambda: {"runtime_mode": "standalone"})
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
    monkeypatch.setattr(updater, "load_config", lambda: {"runtime_mode": "standalone"})
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
    monkeypatch.setattr(mcs_setup, "HERMES_PY", str(selected))
    agents = runtime_home / "agents"
    agents.mkdir()
    import plistlib
    (agents / "org.mcs.recovery.plist").write_bytes(plistlib.dumps(
        {"ProgramArguments": [str(selected), "synthetic-recovery.py"]}))
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(agents))
    monkeypatch.setattr(mcs_setup, "_runtime_probe", lambda exe: _facts())
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
    state = updater._default_state()
    state["stages"].append({"stage": "local_checks", "at": 1.0})
    updater.save_state(state)
    loaded = updater.load_state()
    assert loaded["stages"][0]["stage"] == "local_checks"
    assert loaded["v"] == 1
    (tmp_path / "data" / "update_state.json").write_text("{not json")
    assert updater.load_state().get("_corrupt") is True


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
    state = updater._default_state()
    state[field] = value
    updater.save_state(state)
    assert updater.load_state() == {"_corrupt": True}
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
    assert updater.acquire_update_lock() is not None


@pytest.mark.parametrize("interrupted", [False, True])
def test_rollback_keeps_manifest_and_restarts_changed_plugin(
        updater, tmp_path, monkeypatch, interrupted):
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    before = _git(repo, "rev-parse", "v1.0.0").stdout.strip()
    after = _git(repo, "rev-parse", "v1.1.0^{commit}").stdout.strip()
    desired = {"scripts": [], "agents": [{"label": "local.mcs-cmd"}], "cron": []}
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": after,
                         "prev_sha": before, "plugin_changed": True,
                         "manifest_snapshot": desired, "command_id": "apply-id"}
    monkeypatch.setattr(updater, "_services_reconcile", lambda: None)
    monkeypatch.setattr(updater, "restart_agents", lambda: [])
    monkeypatch.setattr(updater, "_postcheck", lambda *a: [])
    monkeypatch.setattr(updater, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(updater, "load_config", lambda: {})
    assert updater._post_merge(state) == 0
    state = updater.load_state()
    assert state["applied"][-1]["manifest_snapshot"] == desired
    state["stages"] = []
    restarts, memberships = [], []

    def restart(cfg):
        # The decision survives popping the entry, and durable bookkeeping
        # must precede a restart that could terminate this caller.
        persisted = updater.load_state()
        assert persisted["applied"] == []
        assert persisted["executed"]["rollback-id"]["result"] == "rolled_back"
        restarts.append(True)

    monkeypatch.setattr(updater, "restart_gateway", restart)
    monkeypatch.setattr(updater, "quiesce", lambda: [])
    monkeypatch.setattr(updater, "_reconcile_membership",
                        lambda manifest: memberships.append(manifest) or [])
    if interrupted:
        state["applying"] = {"tag": "rollback:v1.1.0", "sha": before,
                             "prev_sha": after, "rollback": True,
                             "plugin_changed": True, "manifest_snapshot": desired,
                             "command_id": "rollback-id", "at": time.time()}
        state["stages"] = [{"stage": "rollback", "at": time.time()}]
        _git(repo, "reset", "--hard", before)
    updater.save_state(state)
    assert (updater.recover_interrupted() if interrupted
            else updater.rollback("rollback-id")) == 0
    assert updater.load_state()["applied"] == []
    assert restarts == [True]
    assert memberships == [desired]


# ------------------------------------------------------------ tag parsing

def test_remote_tag_sha_annotated_and_lightweight(updater, tmp_path):
    repo, _ = _make_repo(tmp_path)
    monkeypatch_repo = str(repo)
    import mcs_update as mu
    mu.REPO = monkeypatch_repo
    lite = mu.remote_tag_sha("v1.0.0")
    ann = mu.remote_tag_sha("v1.1.0")
    commit_lite = _git(repo, "rev-list", "-n1", "v1.0.0").stdout.strip()
    commit_ann = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    assert lite == commit_lite           # lightweight: direct = commit
    assert ann == commit_ann             # annotated: peeled = commit
    assert ann != _git(repo, "rev-parse", "v1.1.0").stdout.strip()


def test_detect_latest_semver_max(updater, tmp_path):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    tag, sha = mcs_update.detect_latest()
    assert tag == "v1.1.0"
    assert len(sha) == 40


def test_detect_latest_prerelease_excluded(updater, monkeypatch):
    def fake_git(args, timeout=30):
        class R:
            returncode = 0
            stdout = (("a" * 40) + "\trefs/tags/v1.2.0\n"
                      + ("b" * 40) + "\trefs/tags/v1.3.0-rc1\n"
                      + ("c" * 40) + "\trefs/tags/v1.3.0-rc1^{}\n")
            stderr = ""
        return R()
    monkeypatch.setattr(mcs_update, "_git", fake_git)
    tag, sha = mcs_update.detect_latest()
    assert tag == "v1.2.0"
    tag, sha = mcs_update.detect_latest(include_prerelease=True)
    # opt-in picks the prerelease — and peels it to the commit sha
    assert tag == "v1.3.0-rc1"
    assert sha == "c" * 40


def test_prerelease_numeric_components_follow_semver_order():
    names = ["v1.2.0-rc.2", "v1.2.0-rc.10", "v1.2.0"]
    assert sorted(reversed(names), key=mcs_update._ver_key) == names
    assert mcs_update._ver_key(["invalid"]) is None


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
    candidates, consumed = updater.scan_pending_approvals(updater._default_state())
    assert [candidate["command_id"] for candidate in candidates] == ["valid"]
    assert consumed == []


def test_defuse_mentions():
    out = mcs_update.defuse_mentions(
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
    ("hermes_plugin/mcs_delivery/__init__.py", True, False),
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
    impact = updater.impact_summary("synthetic-before", "v1.1.0")
    assert any("gateway restart" in line for line in impact) is gateway
    assert any("LINE WORKS" in line and "再起動" in line for line in impact) is lineworks


# ------------------------------------------------------- protected paths

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


@pytest.mark.parametrize("returncode", [0, 1])
def test_cron_job_removal_requires_successful_list(updater, monkeypatch, returncode):
    import mcs_setup
    script = mcs_setup.CRON_JOBS[0][2]
    monkeypatch.setattr(updater, "load_config", lambda: {})
    monkeypatch.setattr(mcs_setup, "_hermes_exe", lambda cfg: "synthetic-hermes")
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda path: True)
    calls = []

    def list_result(argv, **kwargs):
        calls.append(argv)
        if argv[1:] == ["cron", "list", "--all"]:
            return subprocess.CompletedProcess(
                argv, returncode,
                f"  abcdef [disabled]\n    Script:  {updater.SCRIPTS_DIR}/{script}\n",
                "synthetic listing failure")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(updater.subprocess, "run", list_result)
    problems = updater._reconcile_membership({"cron": [], "agents": []})
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
    import mcs_setup

    monkeypatch.setattr(updater, "load_config", lambda: {})
    monkeypatch.setattr(mcs_setup, "_hermes_exe", lambda cfg: "synthetic-hermes")
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda path: True)
    if evidence is not None:
        path = Path(updater.MANIFEST_PATH)
        path.write_text(json.dumps({"cron": [evidence]}))
        path.chmod(0o600)
    calls = []
    def execute(argv, **kwargs):
        calls.append(argv)
        output = f"  abcdef [disabled]\n    Script:  {script}\n" if argv[2] == "list" else ""
        return subprocess.CompletedProcess(argv, 0, output, "")
    monkeypatch.setattr(updater.subprocess, "run", execute)
    assert updater._reconcile_membership({"cron": [], "agents": []}) == []
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
    monkeypatch.setattr(updater, "_reconcile_membership", lambda manifest: [])
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
        updater._default_state())
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
        updater._default_state())
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
    state = updater._default_state()
    state["executed"] = {"c1": {"result": "applied", "at": 1}}
    cand, consumed = updater.scan_pending_approvals(state)
    assert cand == [] and consumed == []


def test_scan_missing_ledger(updater, tmp_path):
    updater.LEDGER = str(tmp_path / "nonexistent.db")
    cand, consumed = updater.scan_pending_approvals(
        updater._default_state())
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

    candidates, consumed = updater.scan_pending_approvals(updater._default_state())
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
        updater._default_state())
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
    monkeypatch.setattr(mcs_update, "remote_tag_sha",
                        lambda t: sha)
    monkeypatch.setattr(mcs_update, "load_config",
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
    ("hermes_plugin/mcs_delivery/__init__.py", True),
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
    monkeypatch.setattr(updater, "remote_tag_sha", lambda tag: sha)
    monkeypatch.setattr(updater, "_run_post_merge",
                        lambda expected: updater._post_merge(updater.load_state()))
    restarts = []

    def restart(cfg):
        saved = updater.load_state()
        assert saved["applying"] is None
        assert saved["applied"][-1]["plugin_changed"] is True
        assert saved["executed"]["cid-adapter"]["result"] == "applied"
        restarts.append(True)

    monkeypatch.setattr(updater, "restart_gateway", restart)
    assert updater.apply("v1.2.0", sha, "cid-adapter") == 0
    saved = updater.load_state()
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
    monkeypatch.setattr(mcs_update, "remote_tag_sha", lambda t: sha)
    rc = updater.apply("v9.9.9", sha, "cid-fail")
    assert rc == 1
    after = updater.load_state()
    assert after["executed"]["cid-fail"]["result"] == "failed"
    assert after["attempts"]["v9.9.9"]["result"] == "failed"


def test_apply_transient_lock_failure_keeps_receipt(updater, tmp_path,
                                                    monkeypatch):
    """run_lock_timeout never consumes the receipt — nothing was
    attempted; a later check may legitimately retry."""
    repo = _apply_env(updater, monkeypatch)
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    monkeypatch.setattr(mcs_update, "remote_tag_sha", lambda t: sha)
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait",
                        lambda **kw: None)
    rc = updater.apply("v1.1.0", sha, "cid-wait")
    assert rc == 2
    after = updater.load_state()
    assert "cid-wait" not in after.get("executed", {})
    assert after["stages"] == []       # journal cleaned, not wedged


def test_apply_update_lock_busy_returns_cleanly(updater, tmp_path,
                                               monkeypatch):
    """A live updater holds update.lock — a second apply must exit
    without touching state rather than queue another attempt."""
    _apply_env(updater, monkeypatch)
    fd = updater.acquire_update_lock()
    try:
        assert updater.apply("v1.1.0", "a" * 40, "cid-2") == 0
    finally:
        os.close(fd)


def test_schema_bump_never_auto_applies(updater, tmp_path, monkeypatch):
    """mode=auto + a schema-bumping tag => refused; only a human
    receipt may proceed past this gate (R2)."""
    repo = _apply_env(updater, monkeypatch,
                      precheck_errors=["schema_bump:7->8"],
                      cfg={"update": {"mode": "auto"}})
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    monkeypatch.setattr(mcs_update, "remote_tag_sha", lambda t: sha)
    rc = updater.apply("v1.1.0", sha, None)   # auto path: no receipt
    assert rc == 1
    after = updater.load_state()
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
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = updater._default_state()
    state["applying"] = {"tag": "v9.9.9",
                         "sha": "f" * 40,        # != HEAD → pre-merge
                         "prev_sha": head,
                         "at": time.time() - 4000}
    state["stages"] = [{"stage": "quiesce", "at": time.time() - 3000}]
    updater.save_state(state)
    rc = updater.recover_interrupted()
    assert rc == 0
    after = updater.load_state()
    assert after["applying"] is None and after["stages"] == []


def test_recover_merge_head_abort(updater, tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / ".git" / "MERGE_HEAD").write_text(prev + "\n")
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": prev, "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.save_state(state)
    rc = updater.recover_interrupted()
    assert rc == 0
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == prev


def test_recover_escalates_unclassifiable(updater, tmp_path,
                                          monkeypatch):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    state = updater._default_state()
    # HEAD moved somewhere unknown mid-merge
    state["applying"] = {"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": "0" * 40, "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.save_state(state)
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
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    state = updater._default_state()
    state["stages"] = [{"stage": "local_checks", "at": time.time() - 4000},
                       {"stage": "fetch", "at": time.time() - 3900}]
    updater.save_state(state)
    assert updater.recover_interrupted() == 0
    after = updater.load_state()
    assert after["stages"] == [] and after["applying"] is None
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "interrupted_pre_merge"


def test_recover_completes_done_bookkeeping(updater, tmp_path,
                                            monkeypatch):
    """Child wrote 'done' then died before the parent consumed the
    receipt — recovery completes the bookkeeping (H5)."""
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    state = updater._default_state()
    state["stages"] = [{"stage": "done", "at": time.time() - 100}]
    state["applied"] = [{"tag": "v1.1.0", "sha": "b" * 40,
                         "prev_sha": "a" * 40, "command_id": "cid-9",
                         "at": time.time()}]
    updater.save_state(state)
    assert updater.recover_interrupted() == 0
    after = updater.load_state()
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
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    target = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    # simulate a partially-written checkout: tracked file has target
    # content, update-introduced file exists untracked
    (repo / "f.txt").write_text("partial-checkout-state")
    (repo / "introduced.txt").write_text("new")
    work = tmp_path / "remote-work"
    (work / "introduced.txt").write_text("new")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "c3")
    _git(work, "tag", "v1.2.0")
    _git(work, "push", "-q", str(tmp_path / "remote.git"),
         "main", "v1.2.0")
    _git(repo, "fetch", "-q", "--tags")
    target = _git(repo, "rev-list", "-n1", "v1.2.0").stdout.strip()
    state = updater._default_state()
    state["applying"] = {"tag": "v1.2.0", "sha": target,
                         "prev_sha": prev, "command_id": "cid-mix",
                         "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.save_state(state)
    assert updater.recover_interrupted() == 0
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == prev
    assert _git(repo, "status", "--porcelain", "-uno").stdout.strip() == ""
    assert (repo / "introduced.txt").read_text() == "new"
    after = updater.load_state()
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
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": "0" * 40, "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.save_state(state)
    assert updater.recover_interrupted() == 0
    assert removed == []
    # journal untouched — still waiting for a quiet moment
    assert updater.load_state()["applying"]["tag"] == "v1.1.0"


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


def test_restore_db_removes_wal_sidecars(updater, tmp_path, monkeypatch):
    """Restoring over a WAL-mode DB must delete -wal/-shm first — a
    stale WAL replayed against the restored file corrupts it (F9)."""
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
    mcs_update._restore_db(back)
    assert not Path(live + "-wal").exists()
    assert not Path(live + "-shm").exists()
    # _restore_db opens a Ledger for reconcile, which migrates the
    # restored file to SCHEMA_VERSION — the proof the swap happened is
    # the backup's own row, not user_version
    con = sqlite3.connect("file:" + live + "?mode=ro", uri=True)
    assert con.execute("SELECT x FROM t").fetchone()[0] == "back"
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
    state = updater._default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": _git(repo, "rev-parse", "HEAD")
                         .stdout.strip(), "at": time.time()}]
    updater.save_state(state)
    rc = updater.rollback("cid-rb")
    assert rc == 1
    assert restarted == [1]                  # drainers brought back up
    after = updater.load_state()
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
    monkeypatch.setattr(mcs_update, "_reconcile_membership", lambda m: [])
    monkeypatch.setattr(mcs_update, "_postcheck", lambda s, e: [])
    monkeypatch.setattr(mcs_update, "load_config", lambda: {})
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)
    restarts = []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda: restarts.append(1) or [])
    state = updater._default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": after,
                         "prev_sha": before, "schema_bump": True,
                         "backup_path": str(back), "at": time.time()}]
    updater.save_state(state)
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
    assert updater._db_version(str(back)) == 7
    assert error.value.report["backup_schema"] == 7
    assert error.value.report["stored_since_backup"]["messages"] == 2
    assert live.read_bytes() == before
    _seed_consent(str(live), str(back))
    updater._restore_db(str(back))
    assert updater._db_version(str(live)) == 7


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
    updater._restore_db(str(back))
    assert updater._db_version(str(live)) == 7
    assert decoy.read_bytes() == decoy_before


def test_rollback_schema_bump_holds_for_consent(updater, tmp_path,
                                                monkeypatch):
    """No bound receipt => the rollback stops before the DB replace:
    tree already reset, drainers down, BOTH markers up, loss report
    durable, live DB byte-identical (T13)."""
    repo, live, back, before, _after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    live_bytes = Path(live).read_bytes()
    rc = updater.rollback("cid-rb")
    assert rc == 2
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
    state = updater.load_state()
    assert state["applying"]["rollback"] is True
    assert state["restore_consent"]["report_id"] == report["report_id"]
    # the hold must NOT consume the rollback receipt — it stays pending
    # until the restore actually finishes
    assert "cid-rb" not in state.get("executed", {})


def test_rollback_consent_converges_via_recover(updater, tmp_path,
                                                monkeypatch):
    """After the bound ops.restore_approve receipt lands, recovery
    re-enters _restore_db: swap happens, reconcile clears the marker,
    drainers restart, the rollback completes (T13 happy path)."""
    repo, live, back, before, _after, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    assert updater.rollback("cid-rb") == 2
    _seed_consent(live, back)
    assert updater.recover_interrupted() == 0
    assert _live_version(live) >= 7        # backup bytes restored
    con = sqlite3.connect("file:" + live + "?mode=ro", uri=True)
    assert con.execute(
        "SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    con.close()
    # reconcile consumed the marker — no stale hold pinning senders
    import notify_cards
    assert notify_cards.restore_pending(str(tmp_path / "data")) is None
    state = updater.load_state()
    assert state["applying"] is None
    assert "restore_consent" not in state
    assert state["executed"]["cid-rb"]["result"] == "rolled_back"
    assert restarts == [1]                 # drainers came back up


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
    import notify_cards
    marker = notify_cards.restore_awaiting_consent(
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
    report = mcs_update._restore_loss_report(back)
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
    import notify_cards
    assert notify_cards.restore_awaiting_consent(
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
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda **k: pytest.fail("hold must not restart"))
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: pytest.fail("no outbox write"))
    Path(mcs_update.MARKER_PATH).write_text("1")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": head, "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    state["restore_consent"] = {"report_id": "a" * 64}
    updater.save_state(state)
    rc = updater.recover_interrupted()
    assert rc == 1
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "restore_consent_blocked"
    assert "restore_consent" in report["detail"]
    assert Path(mcs_update.MARKER_PATH).exists()
    # journal preserved for a human — nothing classified away
    after = updater.load_state()
    assert after["applying"] is not None and after["restore_consent"]


def test_loss_report_binds_live_state(updater, tmp_path, monkeypatch):
    """report_id is deterministic over (backup bytes, backup schema,
    live-vs-backup deltas) — any write slipping past the writer hold
    changes the id and invalidates every stale consent (T13)."""
    _repo, live, back, _b, _a, _r = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    r1 = mcs_update._restore_loss_report(back)
    r2 = mcs_update._restore_loss_report(back)
    assert r1["report_id"] == r2["report_id"]     # deterministic
    # a write that slips in invalidates the bound report
    con = sqlite3.connect(live)
    con.execute(
        "INSERT INTO messages(message_id,project_id,posted_at,"
        "posted_at_ts,body_html,body_state,content_hash,first_seen)"
        " VALUES(99,1,'2026-01-02',999,'<b>y</b>','full','h9',1)")
    con.commit()
    con.close()
    r3 = mcs_update._restore_loss_report(back)
    assert r3["report_id"] != r1["report_id"]
    assert r3["intervening_messages"] == 4


def test_restore_consent_expires_when_existing_message_changes(
        updater, tmp_path, monkeypatch):
    _repo, live, back, _before, _after, _restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    approved = _seed_consent(live, back)
    with sqlite3.connect(live) as con:
        con.execute("UPDATE messages SET body_html='changed synthetic text'")
    current = updater._restore_loss_report(back)
    assert current["stored_since_backup"] == approved["stored_since_backup"]
    assert current["report_id"] != approved["report_id"]
    assert updater._restore_consent(current) is None


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


def test_apply_bail_consent_hold_keeps_writers_stopped(updater, tmp_path,
                                                       monkeypatch):
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
    monkeypatch.setattr(mcs_update, "load_config",
                        lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(mcs_update, "precheck_local", lambda c: [])
    monkeypatch.setattr(mcs_update, "remote_tag_sha", lambda t: sha)
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
    monkeypatch.setattr(mcs_update, "_reconcile_membership", lambda m: [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)

    def post_merge(_state):
        st = mcs_update.load_state()
        mcs_update.journal(st, "services")
        restart()
        mcs_update.journal(st, "restart")
        raise mcs_update.UpdateError(
            "post_merge_failed: postcheck_failed: new_env_error: x")

    monkeypatch.setattr(mcs_update, "_run_post_merge", post_merge)
    assert mcs_update.apply("v1.1.0", sha, "cid-apply") == 2
    st = mcs_update.load_state()
    assert st.get("restore_consent")
    assert events[-1] == "quiesce" and "restart" in events
    assert Path(mcs_update.MARKER_PATH).exists()


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
    monkeypatch.setattr(mcs_update, "load_config",
                        lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(mcs_update, "precheck_local", lambda c: [])
    monkeypatch.setattr(mcs_update, "remote_tag_sha", lambda t: sha)
    monkeypatch.setattr(mcs_update, "precheck_tag",
                        lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: str(back))
    monkeypatch.setattr(mcs_update, "_baseline_check", lambda c: [])
    calls = {"n": 0}

    def quiesce():
        calls["n"] += 1
        mcs_update._write_marker()
        if calls["n"] >= 2:
            raise mcs_update.UpdateError("drainer_stop_failed: synthetic")
        return []

    monkeypatch.setattr(mcs_update, "quiesce", quiesce)
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda: (mcs_update._remove_marker(), [])[1])
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(mcs_update, "_reconcile_membership", lambda m: [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)

    def post_merge(_s):
        raise mcs_update.UpdateError("post_merge_failed: synthetic")

    monkeypatch.setattr(mcs_update, "_run_post_merge", post_merge)
    assert mcs_update.apply("v1.1.0", sha, "cid-apply") == 2
    st = mcs_update.load_state()
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
    mcs_update.save_state(state)
    calls = []
    monkeypatch.setattr(mcs_update, "recover_interrupted",
                        lambda *a, **k: calls.append(1) or 0)
    monkeypatch.setattr(mcs_update, "quiesce",
                        lambda: pytest.fail("rollback must not quiesce"))
    assert mcs_update.rollback() == 0
    assert calls == [1]
    assert mcs_update.load_state()["applying"]["tag"] == "v1.0.6"


class _FakeLaunchd:
    """launchctl stub: bootstrap exit codes come from `outcomes`; a
    successful bootstrap (or `late_load`) marks the label loaded."""

    def __init__(self, outcomes, late_load=False, loads=True):
        self.outcomes = list(outcomes)
        self.late_load = late_load
        self.loads = loads
        self.loaded = False
        self.calls = []

    def __call__(self, argv, *args, **kwargs):
        verb = argv[1]
        self.calls.append(verb)
        rc, out, err = 0, "", ""
        if verb == "bootout":
            self.loaded = False
        elif verb == "bootstrap":
            rc = self.outcomes.pop(0)
            if rc == 0:
                self.loaded = self.loads
            else:
                err = "Bootstrap failed: 5: Input/output error"
                out = "success"          # misleading output is ignored
                if not self.outcomes and self.late_load:
                    self.loaded = True
        elif verb == "print":
            rc = 0 if self.loaded else 113
            out = "\tpid = 4242\n" if self.loaded else ""
        return subprocess.CompletedProcess(argv, rc, out, err)


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
    monkeypatch.setattr(mcs_util, "time",
                        SimpleNamespace(time=time.time, sleep=sleeps.append))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ("ai.mcs.x",))
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
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
    monkeypatch.setattr(mcs_util, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ("ai.mcs.w",))
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
    monkeypatch.setattr(mcs_util, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS",
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ("local.mcs-w",))
    Path(mcs_update.MARKER_PATH).write_text("1")
    assert mcs_update.restart_agents() == problems
    # the loop went on past the wedged label to every later agent
    assert ("bootstrap", "ai.mcs.b") in fake.calls
    assert fake.calls[-1][1] == "local.mcs-w"
    assert not os.path.exists(mcs_update.MARKER_PATH)


@pytest.mark.parametrize("exc", _HANG_EXCS)
def test_quiesce_never_takes_unverifiable_stop_as_stopped(
        updater, monkeypatch, exc):
    """A hung `print` cannot prove the drainer stopped — quiesce fails
    closed (drainer_stop_failed) instead of merging under a live one;
    a hung `bootout` alone is fine once `print` confirms the stop."""
    from types import SimpleNamespace
    clock = [1000.0]
    monkeypatch.setattr(mcs_update, "time", SimpleNamespace(
        time=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS",
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
    clock = [time.time()]
    monkeypatch.setattr(mcs_update, "time", SimpleNamespace(
        time=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(mcs_util, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS",
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "_stray_drainer_pids", lambda: [])
    monkeypatch.setattr(mcs_update, "_rollback_tree",
                        lambda e: pytest.fail("must not reset"))
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    state = updater._default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": "t" * 40,
                         "prev_sha": _git(repo, "rev-parse", "HEAD")
                         .stdout.strip(), "at": time.time()}]
    updater.save_state(state)
    assert updater.rollback("cid-rb") == 1
    assert ("bootstrap", "ai.mcs.a") in fake.calls   # a brought back
    assert "ai.mcs.a" in fake.loaded
    detail = updater.load_state()["executed"]["cid-rb"]["detail"]
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
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    real_run = subprocess.run

    def run(argv, *a, **k):
        if argv[0] == sys.executable:
            raise exc
        return real_run(argv, *a, **k)
    monkeypatch.setattr(mcs_update.subprocess, "run", run)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": head,
                         "prev_sha": "0" * 40, "command_id": "cid-a",
                         "at": time.time() - 4000}
    state["stages"] = [{"stage": "post_merge", "at": time.time() - 3000}]
    updater.save_state(state)
    Path(mcs_update.MARKER_PATH).write_text("1")
    assert updater.recover_interrupted() == 1
    after = updater.load_state()
    assert after["executed"]["cid-a"]["result"] == "escalated"
    assert "services_failed" in after["executed"]["cid-a"]["detail"]
    assert not os.path.exists(mcs_update.MARKER_PATH)


# ------------------------------- git failure inside recover_interrupted
# _head_sha/_tree_clean/reset/merge --abort raise UpdateError; recover
# must decide instead of escaping with drainers down and the marker up.

def _hang_git(monkeypatch):
    real_run = subprocess.run

    def run(argv, *a, **k):
        if argv[0] == "git":
            raise subprocess.TimeoutExpired(argv, k.get("timeout"))
        return real_run(argv, *a, **k)
    monkeypatch.setattr(mcs_update.subprocess, "run", run)


def test_recover_git_failure_escalates_outside_consent_hold(
        updater, tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    restarts, notices = [], []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda: restarts.append(1) or [])
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "a" * 40,
                         "prev_sha": "b" * 40, "command_id": "cid-a",
                         "at": time.time()}
    state["stages"] = [{"stage": "merge", "at": time.time()}]
    updater.save_state(state)
    Path(mcs_update.MARKER_PATH).write_text("1")
    _hang_git(monkeypatch)
    assert updater.recover_interrupted() == 1
    after = updater.load_state()
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
    write that would void the loss report) and the next pass converges."""
    import notify_cards
    repo, live, back, _b, _a, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    assert updater.rollback("cid-rb") == 2
    notices = []
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    real_run = subprocess.run
    _hang_git(monkeypatch)
    assert updater.recover_interrupted() == 1
    assert restarts == [] and notices == []
    assert Path(mcs_update.MARKER_PATH).exists()
    assert notify_cards.restore_awaiting_consent(
        str(tmp_path / "data")) is not None
    state = updater.load_state()
    assert state["restore_consent"] and state["applying"]["rollback"]
    assert "cid-rb" not in state.get("executed", {})
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "restore_consent_blocked"
    monkeypatch.setattr(mcs_update.subprocess, "run", real_run)
    _seed_consent(live, back)
    assert updater.recover_interrupted() == 0
    assert updater.load_state()["executed"]["cid-rb"]["result"] \
        == "rolled_back"
    assert restarts == [1]


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
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    assert mcs_update._stray_drainer_pids() is None
    with pytest.raises(mcs_update.UpdateError,
                       match="stray_drainer_unverifiable"):
        mcs_update.quiesce()


def test_stray_check_no_match_is_clean(updater, monkeypatch):
    monkeypatch.setattr(
        mcs_update.subprocess, "run",
        lambda argv, *a, **k: subprocess.CompletedProcess(argv, 1, "", ""))
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
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
                                sleep=advance)
    monkeypatch.setattr(mcs_update.subprocess, "run", run)
    monkeypatch.setattr(mcs_update, "time", fake_time)
    monkeypatch.setattr(mcs_util, "time", fake_time)
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS",
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS",
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
    foreign = updater._default_state()
    foreign["applying"] = {"tag": "v1.1.0", "sha": "a" * 40,
                           "prev_sha": "b" * 40, "command_id": "other",
                           "at": time.time()}
    foreign["stages"] = [{"stage": "merge", "at": time.time()}]
    real_load = updater.load_state
    reads = []

    def load_state():
        reads.append(1)
        return updater._default_state() if len(reads) == 1 \
            else real_load()
    updater.save_state(foreign)
    before = Path(updater.STATE_PATH).read_bytes()
    monkeypatch.setattr(mcs_update, "load_state", load_state)
    monkeypatch.setattr(mcs_update, "load_config", lambda: {})
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
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "a" * 40,
                         "prev_sha": "b" * 40, "at": time.time()}
    state["stages"] = [{"stage": "merge", "at": time.time()}]
    updater.save_state(state)
    _hang_git(monkeypatch)
    assert updater.recover_interrupted() == 1       # enqueue failed
    ok[0] = True
    for _ in range(3):
        assert updater.recover_interrupted() == 1
    assert len(notices) == 2                        # retry, then quiet
    state["stages"].append({"stage": "post_merge", "at": time.time()})
    updater.save_state(state)
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
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "a" * 40,
                         "prev_sha": "b" * 40, "at": time.time()}
    state["stages"] = [{"stage": "merge", "at": time.time()}]
    updater.save_state(state)
    for _ in range(3):
        assert updater.recover_interrupted() == 1   # unclassifiable
    assert calls == [True, False, False]
    assert not os.path.exists(mcs_update.MARKER_PATH)
    state["stages"].append({"stage": "post_merge", "at": time.time()})
    updater.save_state(state)
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
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": head,
                         "prev_sha": "0" * 40, "at": time.time()}
    state["stages"] = [{"stage": "post_merge", "at": time.time()}]
    updater.save_state(state)
    assert updater.recover_interrupted() == 1
    assert calls == [True, False]
    assert updater.recover_interrupted() == 1
    assert calls[2:] == [False, False]
    (repo / "f.txt").write_text("dirty")
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
    monkeypatch.setattr(mcs_util, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS",
                        ("ai.mcs.run", "ai.mcs.stopped", "ai.mcs.gone",
                         "ai.mcs.hung"))
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
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
        time=lambda: next(clock), sleep=lambda s: None))
    monkeypatch.setattr(mcs_util, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS",
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(mcs_update, "WATCHER_LABELS", ())
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
    consent receipt still converges to rolled_back."""
    import notify_cards
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
    assert notify_cards.restore_awaiting_consent(
        str(tmp_path / "data")) is not None
    state = updater.load_state()
    assert state["restore_consent"] and state["applying"]["rollback"]
    assert "cid-rb" not in state.get("executed", {})
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "restore_consent_blocked"
    undo()
    _seed_consent(live, back)
    assert updater.recover_interrupted() == 0
    assert updater.load_state()["executed"]["cid-rb"]["result"] \
        == "rolled_back"
    from ledger import SCHEMA_VERSION
    # Consuming the consent receipt reopens the restored DB with the current writer.
    assert _live_version(live) == SCHEMA_VERSION and restarts == [1]
    con = sqlite3.connect(live)
    assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    con.close()


@pytest.mark.parametrize("unreadable", [False, True])
def test_marker_only_hold_survives_git_failure(
        updater, tmp_path, monkeypatch, unreadable):
    """A hold entered before holds were journaled has only the
    awaiting_consent marker: a git failure must still keep the freeze
    (old code escalated — no restore_consent record) and the hold is
    recorded in the journal from the marker; the consent converges."""
    repo, live, back, _b, _a, restarts = _schema_bump_world(
        updater, tmp_path, monkeypatch)
    assert updater.rollback("cid-rb") == 2
    marker = tmp_path / "data" / "restore_pending.json"
    rid = json.loads(marker.read_text())["report_id"]
    if unreadable:
        marker.write_text("{broken")
    state = updater.load_state()
    del state["restore_consent"]
    updater.save_state(state)
    notices = []
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    real_run = subprocess.run
    _hang_git(monkeypatch)
    assert updater.recover_interrupted() == 1
    assert restarts == [] and notices == []
    assert Path(mcs_update.MARKER_PATH).exists()
    state = updater.load_state()
    hold = state["restore_consent"]
    assert hold["from_marker"] is True
    assert hold["report_id"] == (None if unreadable else rid)
    assert "cid-rb" not in state.get("executed", {})
    assert json.loads(Path(mcs_update.REPORT_PATH).read_text())[
        "result"] == "restore_consent_blocked"
    if unreadable:
        return                  # only a human can repair the marker
    monkeypatch.setattr(mcs_update.subprocess, "run", real_run)
    _seed_consent(live, back)
    assert updater.recover_interrupted() == 0
    assert updater.load_state()["executed"]["cid-rb"]["result"] \
        == "rolled_back"


@pytest.mark.parametrize("content", [
    None, b"", b"{bad", b"[]", b'{"phase": "awaiting_consent", "report_id": "r"}',
    b'{"phase": "restored"}', b'{"restored_at": 1}', b'{"phase": "other"}'])
def test_awaiting_consent_marker_matches_notify_cards(tmp_path, monkeypatch,
                                                     content):
    """recover reads the hold marker without importing notify_cards (a
    rolled-back tree may predate it) — same fail-closed verdict."""
    import notify_cards
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path))
    if content is not None:
        (tmp_path / "restore_pending.json").write_bytes(content)
    assert (mcs_update._awaiting_consent_marker() is None) \
        == (notify_cards.restore_awaiting_consent(str(tmp_path)) is None)


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
    monkeypatch.setattr(mcs_update, "load_config", lambda: {
        "update": {"mode": "auto", "auto_delay_h": 0}})
    monkeypatch.setattr(mcs_update, "detect_latest",
                        lambda pre: ("v9.0.0", "a" * 40))
    monkeypatch.setattr(mcs_update, "current_version",
                        lambda: ("v9.0.0", "b" * 40))
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
