"""mcs_recover — the repo-external interrupted-apply recovery tool.

Loads deployment/recovery/mcs_recover.py by path (it is not on the
mcs import roots — it must run standalone on a broken repo). Fully
synthetic temp repos/state; no real services touched.
"""
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import pytest


def _load():
    path = (Path(__file__).resolve().parents[2]
            / "deployment" / "recovery" / "mcs_recover.py")
    spec = importlib.util.spec_from_file_location("mcs_recover", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo, *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
    return r


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("one")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "c1")
    _git(repo, "tag", "v1.0.0")
    return repo


@pytest.fixture
def rec(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "REPO", str(tmp_path / "repo"))
    monkeypatch.setattr(mod, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(mod, "STATE_PATH",
                        str(tmp_path / "data" / "update_state.json"))
    monkeypatch.setattr(mod, "UPDATE_LOCK",
                        str(tmp_path / "data" / "update.lock"))
    monkeypatch.setattr(mod, "RUN_LOCK",
                        str(tmp_path / "data" / "run.lock"))
    monkeypatch.setattr(mod, "MARKER_PATH",
                        str(tmp_path / "data" / "marker"))
    monkeypatch.setattr(mod, "REPORT_PATH",
                        str(tmp_path / "data" / "recovery_report.json"))
    monkeypatch.setattr(mod, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(mod, "RESIDENT_LABELS", ())
    os.makedirs(tmp_path / "data", exist_ok=True)
    return mod


def _applying(prev, tag="v1.1.0", sha="t" * 40, stage="quiesce",
              ago=4000):
    return {"v": 1,
            "applying": {"tag": tag, "sha": sha, "prev_sha": prev,
                         "at": time.time() - ago},
            "stages": [{"stage": stage, "at": time.time() - ago + 100}],
            "applied": [], "attempts": {}, "executed": {}}


def test_no_state_no_action(rec):
    assert rec.recover() == 0


def test_corrupt_state_escalates(rec, tmp_path):
    (tmp_path / "data" / "update_state.json").write_text("{broken")
    assert rec.recover() == 2
    report = json.load(open(rec.REPORT_PATH))
    assert report["result"] == "corrupt_state"


def test_pre_merge_interrupt_restores(rec, tmp_path):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = _applying(prev, stage="quiesce")
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 0
    after = json.load(open(rec.STATE_PATH))
    assert after["applying"] is None and after["stages"] == []
    report = json.load(open(rec.REPORT_PATH))
    assert report["result"] == "interrupted_pre_merge"


def test_mixed_tree_resets_to_prev(rec, tmp_path):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # simulate a half-applied checkout: tracked file mutated, no
    # MERGE_HEAD — merge --abort can't help, reset --hard can
    (repo / "f.txt").write_text("CORRUPTED")
    state = _applying(prev, stage="applying")
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 0
    assert (repo / "f.txt").read_text() == "one"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == prev
    report = json.load(open(rec.REPORT_PATH))
    assert report["result"] in ("mixed_tree_reset",)


def test_merge_head_aborted(rec, tmp_path):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / ".git" / "MERGE_HEAD").write_text(prev + "\n")
    state = _applying(prev, stage="merge")
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 0
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    report = json.load(open(rec.REPORT_PATH))
    assert report["result"] == "merge_aborted"


def test_if_stale_respects_fresh_apply(rec, tmp_path):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # applying recorded 10s ago — not stale, watchdog must not touch
    state = _applying(prev, stage="quiesce", ago=10)
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover(if_stale=True) == 0
    after = json.load(open(rec.STATE_PATH))
    assert after["applying"] is not None      # untouched
    assert not os.path.exists(rec.REPORT_PATH)


def test_if_stale_recovers_old_apply(rec, tmp_path):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = _applying(prev, stage="quiesce", ago=4000)
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover(if_stale=True) == 0
    after = json.load(open(rec.STATE_PATH))
    assert after["applying"] is None


def test_unclassifiable_escalates_no_destruction(rec, tmp_path):
    repo = _make_repo(tmp_path)
    # HEAD exists but prev_sha is bogus — nothing matches
    state = _applying("0" * 40, stage="merge")
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 1
    report = json.load(open(rec.REPORT_PATH))
    assert report["result"] == "escalate"
    assert _git(repo, "rev-parse", "HEAD").returncode == 0


def test_stale_git_locks_removed(rec, tmp_path):
    """A crashed apply can leave .git/*.lock — but only locks OLDER
    than GIT_LOCK_MIN_AGE_S are removed; a fresh lock may belong to a
    live unrelated git process (H3)."""
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    state = _applying(prev, stage="quiesce", ago=4000)
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 0
    assert lock.exists()                     # fresh — preserved
    old = time.time() - rec.GIT_LOCK_MIN_AGE_S - 60
    os.utime(lock, (old, old))
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 0
    assert not lock.exists()                 # stale — removed


def test_busy_update_lock_defers(rec, tmp_path):
    """A live apply holds update.lock — recovery must not interfere."""
    import fcntl
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = _applying(prev, stage="merge", ago=4000)
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    fd = os.open(rec.UPDATE_LOCK, os.O_WRONLY | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert rec.recover() == 0          # deferred, untouched
        after = json.load(open(rec.STATE_PATH))
        assert after["applying"] is not None
    finally:
        os.close(fd)


def test_membership_reconcile_removes_undesired_agents(rec, tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "ai.mcs.extract-old.plist").write_text("<plist/>")
    (agents / "ai.mcs.llamaserver.plist").write_text("<plist/>")
    (agents / "unrelated.other.plist").write_text("<plist/>")
    snapshot = {"agents": [{"label": "ai.mcs.extract-drainer"}],
                "cron": []}
    rec._reconcile_membership(snapshot)
    # undesired owned agent removed; excluded + foreign survive
    assert not (agents / "ai.mcs.extract-old.plist").exists()
    assert (agents / "ai.mcs.llamaserver.plist").exists()
    assert (agents / "unrelated.other.plist").exists()
