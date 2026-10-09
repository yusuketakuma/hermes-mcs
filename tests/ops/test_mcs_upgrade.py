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


def test_launcher_requires_a_fetched_release_tag_not_a_same_named_branch(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _git(repo, "checkout", "-qb", "v1.2.0")
    target = repo / "mcs/ops/mcs_update.py"
    target.parent.mkdir(parents=True)
    target.write_text("raise SystemExit(0)\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic non-release branch")
    before = _git(repo, "rev-parse", "HEAD").stdout
    with pytest.raises(SystemExit, match="git ls-tree failed"):
        _launcher().main(["--repo", str(repo), "--no-fetch", "plan", "--to", "v1.2.0"])
    assert _git(repo, "rev-parse", "HEAD").stdout == before
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_launcher_uses_the_release_tag_despite_an_ambiguous_ref(tmp_path, capfd):
    repo, bare = _make_repo(tmp_path)
    stub = "print('synthetic release updater')\n"
    _release(tmp_path / "remote-work", bare, "v1.2.0", {"mcs/ops/mcs_update.py": stub})
    _git(repo, "fetch", "-q", "--tags")
    _git(repo, "update-ref", "refs/v1.2.0", "HEAD")
    assert _launcher().main(["--repo", str(repo), "--no-fetch", "plan", "--to", "v1.2.0"]) == 0
    assert capfd.readouterr().out.strip() == "synthetic release updater"


@pytest.mark.parametrize("mode,kind,path,error", [
    ("120000", "blob", "mcs/link.py", "unsupported entry"),
    ("160000", "commit", "mcs/submodule", "unsupported entry"),
    ("100644", "blob", "mcs/../escape.py", "unsafe path"),
    ("100644", "blob", "/escape.py", "unsafe path"),
])
def test_extract_rejects_nonfiles_and_unsafe_paths(tmp_path, monkeypatch, mode, kind, path, error):
    launcher = _launcher()
    monkeypatch.setattr(launcher, "_git", lambda *_args, **_kw:
                        f"{mode} {kind} {'a' * 40}\t{path}\0")
    with pytest.raises(SystemExit, match=error):
        launcher.extract("synthetic", "v1.2.0", str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_extract_preserves_binary_blobs_and_executable_bit(tmp_path, monkeypatch):
    launcher = _launcher()
    blob = b"#!/bin/sh\n# synthetic binary \xff\x00\n"

    def git(_repo, *args, binary=False):
        if args[0] == "ls-tree":
            assert args[2] == "refs/tags/v1.2.0"
            return f"100755 blob {'a' * 40}\tmcs/synthetic.sh\0"
        assert args == ("cat-file", "blob", "a" * 40) and binary
        return blob

    monkeypatch.setattr(launcher, "_git", git)
    launcher.extract("synthetic", "v1.2.0", str(tmp_path))
    target = tmp_path / "mcs/synthetic.sh"
    assert target.read_bytes() == blob
    assert target.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("failure", [OSError(2, "synthetic missing executable"), KeyboardInterrupt()])
def test_launcher_cleans_staging_on_launch_error_or_cancellation(tmp_path, monkeypatch, failure):
    launcher = _launcher()
    staged = tmp_path / "staging"
    staged.mkdir(mode=0o700)
    monkeypatch.setattr(launcher.tempfile, "mkdtemp", lambda **_kw: str(staged))
    monkeypatch.setattr(launcher, "_git", lambda *_args, **_kw: str(tmp_path))

    def extract(*_args):
        target = staged / "mcs/ops/mcs_update.py"
        target.parent.mkdir(parents=True)
        target.write_text("# synthetic updater\n")

    def run(*_args, **_kw):
        raise failure

    monkeypatch.setattr(launcher, "extract", extract)
    monkeypatch.setattr(launcher.subprocess, "run", run)
    expected = SystemExit if isinstance(failure, OSError) else KeyboardInterrupt
    with pytest.raises(expected):
        launcher.main(["--repo", str(tmp_path), "--no-fetch", "plan", "--to", "v1.2.0"])
    assert not staged.exists()


def test_git_unavailable_reports_launch_failure(tmp_path, monkeypatch):
    launcher = _launcher()

    def run(*_args, **_kw):
        raise FileNotFoundError(2, "synthetic missing git")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    with pytest.raises(SystemExit, match="git tag failed: synthetic missing git"):
        launcher.latest_tag(str(tmp_path))


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
    monkeypatch.setattr(updater, "_rollback_tree", lambda e, on_hold=None: rolled.append(e))
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


def test_reinstall_done_clears_a_promoted_unfinished_reinstall(
        updater, monkeypatch, tmp_path):
    """Regression: nothing could clear reinstall_incomplete after the
    operator finished install.sh by hand."""
    _plan_env(updater, monkeypatch, tmp_path, [])
    monkeypatch.setattr(updater, "_head_sha", lambda: "a" * 40)
    state = updater._default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": "b" * 40, "reinstall": True}]
    updater.save_state(state)
    assert updater.reinstall_done() == 2            # HEAD is elsewhere
    state["applied"][0]["sha"] = "a" * 40
    updater.save_state(state)
    assert updater.reinstall_done() == 0
    assert "reinstall_incomplete" not in updater.plan("v1.2.0")["blockers"]
    assert updater.reinstall_done() == 2            # nothing left to clear


def test_reinstall_done_lets_recover_finish_the_journal(updater, monkeypatch):
    monkeypatch.setattr(updater, "_head_sha", lambda: "a" * 40)
    calls = []
    monkeypatch.setattr(updater, "recover_interrupted",
                        lambda: calls.append(updater.load_state()) or 0)
    state = updater._default_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "a" * 40, "reinstall": True}
    updater.save_state(state)
    assert updater.reinstall_done() == 0
    assert calls[0]["applying"]["reinstall_done"] is True


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


def test_launcher_copy_notice_never_migrates_the_live_db(updater, tmp_path):
    """Regression: a bail/escalate notice from the launcher's temp copy
    opened the live DB with the target version's Ledger and bumped its
    schema while the live tree still ran the old version."""
    import sqlite3
    live = Path(updater.REPO) / "mcs"
    live.mkdir(parents=True, exist_ok=True)
    (live / "_mcs_path.py").write_text("")
    (live / "ledger.py").write_text(
        "import json, sqlite3\n"
        "class Ledger:\n"
        "    def __init__(self, path):\n"
        "        self.db = sqlite3.connect(path)\n"
        "    def outbox_add(self, kind, pid, payload):\n"
        "        self.db.execute('CREATE TABLE IF NOT EXISTS seen(v)')\n"
        "        self.db.execute('INSERT INTO seen VALUES(?)',"
        " (json.dumps([kind, payload]),))\n"
        "        self.db.commit()\n")
    con = sqlite3.connect(updater.LEDGER)
    con.execute("PRAGMA user_version=7")
    con.close()
    assert updater._enqueue_notice("[MCS] 更新 v9 を中止しました: x")
    con = sqlite3.connect(updater.LEDGER)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 7
    kind, payload = json.loads(con.execute("SELECT v FROM seen").fetchone()[0])
    con.close()
    assert kind == "update_notice" and "中止" in payload["text"]


def test_extracted_current_updater_imports_with_recovery_helper(tmp_path, monkeypatch):
    launcher = _launcher()
    files = {str(p.relative_to(ROOT)): p.read_bytes()
             for p in (ROOT / "mcs").rglob("*.py")}
    helper = "deployment/recovery/mcs_recover.py"
    files[helper] = (ROOT / helper).read_bytes()

    def git(repo, *args, binary=False):
        if args[0] == "ls-tree":
            assert args[4:] == ("mcs", helper)
            return "".join(f"100644 blob {name}\t{name}\0" for name in files)
        assert args[:2] == ("cat-file", "blob") and binary
        return files[args[2]]

    monkeypatch.setattr(launcher, "_git", git)
    launcher.extract("unused", "v1.0.16", str(tmp_path))
    out = subprocess.run(
        [sys.executable, str(tmp_path / "mcs/ops/mcs_update.py"), "--help"],
        capture_output=True, text=True, check=True)
    assert "rollback" in out.stdout
    assert (tmp_path / helper).read_bytes() == files[helper]


@pytest.mark.parametrize("binary", [False, True])
def test_launcher_git_failure_never_reports_remote_credential_text(tmp_path, monkeypatch, binary):
    launcher = _launcher()
    secret = "SYNTHETIC_CREDENTIAL_CANARY"
    text = "fatal: unable to access https://fictional:" + secret + "@example.invalid/repo"
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 128, b"" if binary else "",
                                                    text.encode() if binary else text))
    with pytest.raises(SystemExit) as failure:
        launcher._git(str(tmp_path), "fetch", binary=binary)
    assert "git fetch failed" in str(failure.value)
    assert secret not in str(failure.value)
    assert "example.invalid" not in str(failure.value)
