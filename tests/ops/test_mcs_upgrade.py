"""Upgrade path — scripts/mcs_upgrade.py launcher + mcs_update plan /
reinstall. Fully synthetic: temp git repos, stubbed launchctl/install.sh.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import mcs_update
from ops_testkit import _git, _make_repo
import test_mcs_update
from test_mcs_update import _apply_env

updater = test_mcs_update.updater   # shared fixture

ROOT = Path(__file__).resolve().parents[2]


def _launcher():
    spec = importlib.util.spec_from_file_location(
        "mcs_upgrade_launcher", ROOT / "scripts" / "mcs_upgrade.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _release(work, bare, tag, files):
    for path, text in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text)
        _git(work, "add", path)
    _git(work, "commit", "-qm", tag)
    _git(work, "tag", tag)
    _git(work, "push", "-q", str(bare), "main", tag)
    return _git(work, "rev-parse", "HEAD").stdout.strip()


def test_repo_root_follows_mcs_update_repo(tmp_path):
    env = {**os.environ, "MCS_UPDATE_REPO": str(tmp_path)}
    out = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]);"
         "import _mcs_path, mcs_util, mcs_setup;"
         "print(mcs_util.REPO == mcs_setup.REPO_ROOT == sys.argv[2])",
         str(ROOT / "mcs"), str(tmp_path)],
        env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "True"


def test_launcher_runs_target_updater_against_live_repo(tmp_path, capfd):
    repo, bare = _make_repo(tmp_path)
    stub = ("import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'file': __file__,"
            " 'repo': os.environ['MCS_UPDATE_REPO'], 'cwd': os.getcwd()}))\n")
    _release(tmp_path / "remote-work", bare, "v1.2.0",
             {"mcs/ops/mcs_update.py": stub, "mcs/_mcs_path.py": ""})
    _git(repo, "fetch", "-q", "--tags")
    launcher = _launcher()
    assert launcher.main(["--repo", str(repo), "--no-fetch", "apply",
                          "--reinstall", "--install-arg=--no-llm"]) == 0
    seen = json.loads(capfd.readouterr().out)
    assert seen["argv"] == ["apply", "--tag", "v1.2.0", "--reinstall",
                            "--install-arg=--no-llm"]
    assert seen["repo"] == os.path.realpath(repo)
    assert not seen["file"].startswith(os.path.realpath(repo))
    assert not Path(seen["file"]).exists()          # temp copy removed
    # the live checkout itself was not touched
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_launcher_refuses_target_without_updater(tmp_path):
    repo, _ = _make_repo(tmp_path)
    with pytest.raises(SystemExit, match="no mcs/ops/mcs_update.py"):
        _launcher().main(["--repo", str(repo), "--no-fetch", "plan",
                          "--to", "v1.1.0"])


CHANGELOG = """# 変更履歴

## [Unreleased]

## [1.2.0] — 2026-10-03

### 新機能

- x

### 更新時の注意

- install.shを再実行してください。

## [1.1.0] — 2026-10-02

### 更新時の注意

- gatewayを再起動してください。

### 技術詳細

- t

## [1.0.0] — 2026-09-01

### 更新時の注意

- 対象外の古い注意。
"""


def test_upgrade_notes_cover_only_skipped_versions(updater, tmp_path):
    repo, bare = _make_repo(tmp_path)
    updater.REPO = str(repo)
    _release(tmp_path / "remote-work", bare, "v1.2.0",
             {"CHANGELOG.md": CHANGELOG})
    _git(repo, "fetch", "-q", "--tags")
    notes = updater.upgrade_notes("v1.2.0", "v1.0.0")
    assert [n["version"] for n in notes] == ["1.1.0", "1.2.0"]
    assert notes[0]["notes"] == "- gatewayを再起動してください。"
    assert updater.upgrade_notes("v1.2.0", "v1.1.0")[0]["version"] == "1.2.0"


def _plan_env(updater, monkeypatch, tmp_path, tag_errors, *, legacy=False,
              cfg=None):
    repo, bare = _make_repo(tmp_path)
    updater.REPO = str(repo)
    if not legacy:
        _git(repo, "checkout", "-q", "main")
        (repo / "mcs/ops").mkdir(parents=True)
        (repo / "mcs/ops/mcs_update.py").write_text("")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "updater")
    _release(tmp_path / "remote-work", bare, "v1.2.0",
             {"CHANGELOG.md": CHANGELOG})
    _git(repo, "fetch", "-q", "--tags")
    monkeypatch.setattr(updater, "load_config", lambda: cfg or {})
    monkeypatch.setattr(updater, "precheck_local", lambda c: [])
    monkeypatch.setattr(updater, "precheck_tag", lambda t: list(tag_errors))
    return repo


def test_plan_routes_install_change_to_reinstall(updater, monkeypatch, tmp_path):
    _plan_env(updater, monkeypatch, tmp_path, ["install_sh_changed"])
    p = updater.plan("v1.2.0")
    assert p["route"] == "reinstall" and p["blockers"] == []
    assert p["reinstall"] == ["install_sh_changed"]
    assert [n["version"] for n in p["notes"]] == ["1.2.0"]


@pytest.mark.parametrize("kw,errors,blocker", [
    ({}, ["tree_dirty"], "tree_dirty"),
    ({"legacy": True}, [], "legacy_source_manual"),
    ({"cfg": {"runtime_mode": "standalone"}}, [], "standalone_external_apply"),
])
def test_plan_blocks(updater, monkeypatch, tmp_path, kw, errors, blocker):
    _plan_env(updater, monkeypatch, tmp_path, errors, **kw)
    p = updater.plan("v1.2.0")
    assert p["route"] == "blocked" and blocker in p["blockers"]


def test_plan_reports_interrupted_update(updater, monkeypatch, tmp_path):
    _plan_env(updater, monkeypatch, tmp_path, [])
    state = updater._default_state()
    state["stages"].append({"stage": "merge", "at": 1.0})
    updater.save_state(state)
    assert "update_in_progress_or_interrupted" in updater.plan("v1.2.0")["blockers"]


def _install_tag(updater, monkeypatch, tmp_path):
    repo = _apply_env(updater, monkeypatch,
                      precheck_errors=["install_sh_changed"])
    sha = _release(tmp_path / "remote-work", tmp_path / "remote.git",
                   "v1.2.0", {"install.sh": "#!/bin/sh\n"})
    monkeypatch.setattr(updater, "remote_tag_sha", lambda t: sha)
    monkeypatch.setattr(updater, "_run_post_merge",
                        lambda expected: updater._post_merge(updater.load_state()))
    return repo, sha


def test_install_change_still_blocks_without_reinstall(updater, monkeypatch,
                                                       tmp_path):
    _install_tag(updater, monkeypatch, tmp_path)
    assert updater.apply("v1.2.0", None, None) == 1
    assert "install_sh_changed" in updater.load_state()["attempts"]["v1.2.0"]["detail"]


class _Install:
    """subprocess.Popen stand-in for install.sh."""
    calls: list = []
    rc = 0

    def __init__(self, argv, **kw):
        assert kw["start_new_session"] and kw["stdin"] == subprocess.DEVNULL
        self.calls.append(argv)
        self.pid, self.returncode = 0, self.rc

    def communicate(self, timeout=None):
        return ("NG brew" if self.rc else "ok"), None


def _fake_install(monkeypatch, rc=0):
    """Replace only the install.sh spawn — subprocess.run (git) keeps
    the real Popen."""
    calls = []
    fake = type("Install", (_Install,), {"calls": calls, "rc": rc})
    real = subprocess.Popen

    def popen(argv, *a, **kw):
        if argv and argv[0] == "/bin/sh":
            return fake(argv, **kw)
        return real(argv, *a, **kw)
    monkeypatch.setattr(mcs_update.subprocess, "Popen", popen)
    return calls


def test_reinstall_runs_install_before_services(updater, monkeypatch, tmp_path):
    repo, sha = _install_tag(updater, monkeypatch, tmp_path)
    calls = _fake_install(monkeypatch)
    monkeypatch.setattr(updater, "_services_reconcile",
                        lambda: calls.append("services"))
    rc = updater.apply("v1.2.0", None, None, reinstall=True,
                       install_args=("--no-llm",))
    assert rc == 0, updater.load_state()["attempts"]
    assert calls == [["/bin/sh", str(repo / "install.sh"), "--no-llm",
                      "--no-services"], "services"]
    applied = updater.load_state()["applied"][-1]
    assert applied["reinstall_done"] is True and applied["sha"] == sha


def test_failed_reinstall_rolls_the_tree_back(updater, monkeypatch, tmp_path):
    repo, _ = _install_tag(updater, monkeypatch, tmp_path)
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _fake_install(monkeypatch, rc=1)
    rolled = []
    monkeypatch.setattr(updater, "_rollback_tree", lambda e: rolled.append(e))
    assert updater.apply("v1.2.0", None, None, reinstall=True) == 1
    assert rolled and rolled[0]["prev_sha"] == before
    assert "install_failed" in updater.load_state()["attempts"]["v1.2.0"]["detail"]


@pytest.mark.parametrize("cid,kw", [
    ("cid-1", {"reinstall": True}),                       # receipt path
    (None, {"install_args": ("--no-llm",)}),              # args w/o reinstall
    (None, {"reinstall": True, "install_args": ("--mode",)}),
])
def test_reinstall_is_operator_only(updater, monkeypatch, tmp_path, cid, kw):
    _install_tag(updater, monkeypatch, tmp_path)
    assert updater.apply("v1.2.0", None, cid, **kw) == 2


def test_quiesce_stops_legacy_drainer_only_when_installed(updater, monkeypatch,
                                                          tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(agents))
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ())
    monkeypatch.setattr(mcs_update, "_stray_drainer_pids", lambda: [])
    ran = []
    monkeypatch.setattr(mcs_update, "_run", lambda argv, **k: ran.append(argv))
    mcs_update.quiesce()
    assert ran == []
    (agents / "ai.mcs.extract-drainer-rt.plist").write_text("")
    assert mcs_update.quiesce() == []               # never restarted
    assert ran[0][:2] == ["launchctl", "bootout"]
    assert ran[0][2].endswith("/ai.mcs.extract-drainer-rt")


def test_interrupted_reinstall_is_never_rerun_unattended(updater, monkeypatch):
    calls = _fake_install(monkeypatch)
    updater._reinstall({"reinstall": True, "reinstall_done": True})
    assert calls == []


def test_plan_flags_an_unfinished_reinstall(updater, monkeypatch, tmp_path):
    _plan_env(updater, monkeypatch, tmp_path, [])
    state = updater._default_state()
    state["applied"] = [{"tag": "v1.1.0", "reinstall": True}]
    updater.save_state(state)
    assert "reinstall_incomplete" in updater.plan("v1.2.0")["blockers"]


def test_plan_reports_gate_crash_as_blocker(updater, monkeypatch, tmp_path):
    _plan_env(updater, monkeypatch, tmp_path, [])

    def boom(tag):
        raise updater.UpdateError("cat-file failed")
    monkeypatch.setattr(updater, "precheck_tag", boom)
    p = updater.plan("v1.2.0")
    assert p["route"] == "blocked" and p["blockers"][0].startswith("plan_failed")


def test_launcher_apply_refuses_standalone(updater, monkeypatch):
    monkeypatch.setenv("MCS_UPDATE_REPO", "/nonexistent")
    monkeypatch.setattr(updater, "load_config",
                        lambda: {"runtime_mode": "standalone"})
    assert updater.apply("v1.2.0", None, None) == 2
