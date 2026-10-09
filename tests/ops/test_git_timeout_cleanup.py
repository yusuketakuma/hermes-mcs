"""A git command that outlives its timeout is stopped as an owned session:
SIGTERM first so git removes its own index.lock, then SIGKILL, always
reaped. Temporary repositories only; nothing outside them is touched."""
import os
import signal
import subprocess
import sys
import time

import pytest

import mcs_update
from ops_testkit import _load

HOLD = "!sleep 37 | git update-index --index-info"   # git waits holding index.lock


def _repo(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "alias.hold", HOLD], check=True)
    return repo


def _module(monkeypatch, which, repo):
    module = mcs_update if which == "update" else _load()
    monkeypatch.setattr(module, "REPO", str(repo))
    children = []
    real = module.subprocess.Popen

    def track(*args, **kwargs):
        child = real(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(module.subprocess, "Popen", track)
    return module, children


def _live_members(pgid):
    """Live (non-zombie) processes of a group. A container's pid 1 may
    never reap orphaned zombies, so signal 0 alone cannot tell."""
    if os.path.isdir("/proc/self"):
        live = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat") as handle:
                    fields = handle.read().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(fields[2]) == pgid and fields[0] != "Z":
                live.append(int(entry))
        return live
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="],
                         capture_output=True, text=True, check=True).stdout
    return [int(pid) for pid, group, stat in (line.split()[:3] for line in out.splitlines())
            if int(group) == pgid and not stat.startswith("Z")]


def _group_gone(pgid, wait=3.0):
    deadline = time.monotonic() + wait
    while _live_members(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


@pytest.mark.parametrize("which", ["update", "recover"])
def test_timed_out_git_leaves_no_index_lock_and_no_process(tmp_path, monkeypatch, which):
    repo = _repo(tmp_path)
    module, children = _module(monkeypatch, which, repo)
    started = time.monotonic()
    if which == "update":
        with pytest.raises(mcs_update.UpdateError, match="git_timeout: hold"):
            module._git(["hold"], timeout=1)
    else:
        assert module._git(["hold"], timeout=1) is None
    elapsed = time.monotonic() - started
    owned = list(children)
    assert len(owned) == 1 and owned[0].returncode == -signal.SIGTERM  # reaped on TERM
    assert _group_gone(owned[0].pid)                  # sleep and git stopped too
    assert not (repo / ".git" / "index.lock").exists()
    reset = subprocess.run(["git", "-C", str(repo), "reset", "--hard"], capture_output=True)
    assert reset.returncode == 0                      # the next rollback is not blocked
    assert elapsed < 1 + module.GIT_TERM_GRACE_S + 6   # bounded worst case


@pytest.mark.parametrize("which", ["update", "recover"])
def test_term_ignoring_command_is_killed_and_reaped(monkeypatch, which):
    module = mcs_update if which == "update" else _load()
    stubborn = [sys.executable, "-c", "import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"]
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        module._run_owned(stubborn, 0.5, dict(os.environ))
    assert time.monotonic() - started < 0.5 + module.GIT_TERM_GRACE_S + 6


@pytest.mark.parametrize("which", ["update", "recover"])
def test_normal_return_is_the_same_completed_process(tmp_path, monkeypatch, which):
    repo = _repo(tmp_path)
    module, children = _module(monkeypatch, which, repo)
    result = module._git(["rev-parse", "--is-inside-work-tree"])
    assert isinstance(result, subprocess.CompletedProcess)
    assert (result.returncode, result.stdout.strip()) == (0, "true")
    assert children[0].returncode == 0


def test_update_failure_stays_redacted_through_the_owned_runner(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(mcs_update, "REPO", str(repo))
    result = mcs_update._git(["rev-parse", "SYNTHETIC_MISSING_REF"])
    assert result.returncode != 0 and result.stderr == f"exit={result.returncode}"


@pytest.mark.parametrize("which", ["update", "recover"])
def test_spawn_failure_keeps_each_tools_contract(monkeypatch, which):
    module = mcs_update if which == "update" else _load()

    def missing(*args, **kwargs):
        raise FileNotFoundError("synthetic git missing")
    monkeypatch.setattr(module.subprocess, "Popen", missing)
    if which == "update":
        with pytest.raises(mcs_update.UpdateError, match="git_spawn_failed"):
            module._git(["status"])
    else:
        assert module._git(["status"]) is None


@pytest.mark.parametrize("which", ["update", "recover"])
def test_interruption_stops_the_owned_session_and_reraises(tmp_path, monkeypatch, which):
    repo = _repo(tmp_path)
    module = mcs_update if which == "update" else _load()
    monkeypatch.setattr(module, "REPO", str(repo))
    children = []
    real_popen = module.subprocess.Popen

    def interrupted_once(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        original = child.communicate
        calls = []

        def communicate(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                time.sleep(0.3)                       # let git take its lock
                raise KeyboardInterrupt
            return original(*a, **k)
        child.communicate = communicate
        return child
    monkeypatch.setattr(module.subprocess, "Popen", interrupted_once)
    with pytest.raises(KeyboardInterrupt):
        module._git(["hold"], timeout=30)
    monkeypatch.setattr(module.subprocess, "Popen", real_popen)   # before ps checks
    assert children[0].returncode is not None
    assert _group_gone(children[0].pid)
    assert not (repo / ".git" / "index.lock").exists()


def test_reaped_leader_group_is_never_signalled(monkeypatch):
    """Once our child is reaped its pid may be reused: no signal is sent."""
    sent = []
    monkeypatch.setattr(mcs_update.os, "killpg", lambda *a: sent.append(a))
    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child.communicate()
    mcs_update._stop_owned(child)
    assert sent == []
