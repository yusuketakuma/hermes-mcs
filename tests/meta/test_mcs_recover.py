"""mcs_recover — the repo-external interrupted-apply recovery tool.

Loads deployment/recovery/mcs_recover.py by path (it is not on the
mcs import roots — it must run standalone on a broken repo). Fully
synthetic temp repos/state; no real services touched.
"""
import restore_evidence
import scheduled_jobs
import json
import os
import subprocess
import importlib.util
import hashlib
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
from ops_testkit import _FakeLaunchd, _git, _load


def test_v1029_watchdog_retains_unknown_pending_receipt(tmp_path, monkeypatch):
    raw = (Path(__file__).parent / 'fixtures/mcs_recover_v1_0_29.py.txt').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == '12071648de2b24d02a4fb4b33347614140619ac8817f813297f9cfa9714edd3f'
    source = tmp_path / 'old_recover.py'
    source.write_bytes(raw)
    spec = importlib.util.spec_from_file_location('synthetic_v1029_recover', source)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    marker = tmp_path / '.install-options-migration.pending'
    marker.write_bytes(b'{"v":1,"kind":"install-options-migration"}\n')
    state = {'v': 1, 'applying': None, 'stages': [{'stage': 'settings_migration', 'at': 1}],
             'applied': [], 'attempts': {}, 'executed': {}}
    journal = data / 'update_state.json'
    journal.write_text(json.dumps(state))
    for name, value in {'DATA': data, 'STATE_PATH': journal, 'UPDATE_LOCK': data / 'update.lock',
                        'RUN_LOCK': data / 'run.lock', 'MARKER_PATH': data / 'update.marker',
                        'REPORT_PATH': data / 'recovery_report.json', 'REPO': tmp_path / 'repo'}.items():
        monkeypatch.setattr(old, name, str(value))
    monkeypatch.setattr(old, '_notify', lambda *_a, **_k: None)
    monkeypatch.setattr(old, '_head', lambda: 'synthetic-head')
    monkeypatch.setattr(old, '_restart_drainers', lambda **_k: [])
    monkeypatch.setattr(old, '_restart_services', lambda *_a, **_k: 0)
    monkeypatch.setattr(old, '_clean_stale_git_locks', lambda: [])
    monkeypatch.setattr(old, '_runtime_mode', lambda: 'hermes')
    reports = []
    monkeypatch.setattr(old, '_report', lambda result, *_a, **_k: reports.append(result))
    before = marker.read_bytes()
    assert old.recover() == 0
    assert reports == ['interrupted_pre_merge']
    assert json.loads(journal.read_text())['stages'] == []
    assert marker.read_bytes() == before


def test_new_watchdog_stops_before_recovery_when_receipt_exists(rec, monkeypatch, capsys):
    marker = Path(rec.DATA).parent / '.install-options-migration.pending'
    marker.write_text('synthetic receipt')
    monkeypatch.setattr(rec, '_try_lock', lambda *_a: pytest.fail('must not take recovery locks'))
    assert rec.recover() == 2
    assert marker.read_text() == 'synthetic receipt'
    assert '移行結果を確認できません' in capsys.readouterr().out


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


def test_release_fence_holds_before_recovery_reports_or_lock_cleanup(rec, monkeypatch):
    import release_apply_owner
    runtime = os.path.dirname(rec.DATA)
    with release_apply_owner._control_store(runtime) as store:
        store.write('release-owner-activation.json', {'synthetic': True})
    monkeypatch.setattr(rec, '_report', lambda *_a, **_k: pytest.fail('report before release hold'))
    monkeypatch.setattr(rec, '_report_alert', lambda *_a, **_k: pytest.fail('alert before release hold'))
    monkeypatch.setattr(rec, '_try_lock', lambda *_a: pytest.fail('old locks before release hold'))
    monkeypatch.setattr(rec, '_remove_marker', lambda: pytest.fail('cleanup before release hold'))
    assert rec.recover() == 2
    assert rec._RELEASE_GATE_FD is None
    assert not Path(rec.REPORT_PATH).exists()


def test_recovery_owned_child_inherits_gate_only_during_scope(rec, monkeypatch):
    captured = []

    class Child:
        returncode = 0

        def communicate(self, timeout):
            return '', ''

    def spawn(argv, **kwargs):
        captured.append(tuple(os.fstat(fd).st_ino for fd in kwargs.get('pass_fds', ())))
        return Child()

    monkeypatch.setattr(rec.subprocess, 'Popen', spawn)
    with rec._release_scope(os.path.dirname(rec.DATA)) as fd:
        identity = os.fstat(fd).st_ino
        rec._run_owned(['synthetic'], 1, {})
        assert os.fstat(fd).st_ino == identity
    rec._run_owned(['synthetic'], 1, {})
    assert captured == [(identity,), ()] and rec._RELEASE_GATE_FD is None


@pytest.fixture
def rec(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_notify", lambda *args: None)
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    original_run = subprocess.run

    def local_run(args, **kwargs):
        if args[0] == "launchctl":
            return subprocess.CompletedProcess(args, 0, "", "")
        return original_run(args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", local_run)
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
    monkeypatch.setattr(mod, "MANIFEST_PATH",
                        str(tmp_path / "data" / "service_manifest.json"))
    monkeypatch.setattr(mod, "SCRIPTS_DIR", str(tmp_path / "hermes-scripts"))
    monkeypatch.setattr(mod, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(mod, "RESIDENT_LABELS", ())
    os.makedirs(tmp_path / "data", exist_ok=True)
    return mod


@pytest.fixture
def gateway_restarts(rec, monkeypatch):
    calls = []
    original_popen = subprocess.Popen

    def local_popen(args, **kwargs):
        if len(args) > 2 and args[1:4] == ["-I", "-c", rec._GATEWAY_RESTART_PROGRAM]:
            calls.append(args)
            return None
        return original_popen(args, **kwargs)

    monkeypatch.setattr(rec.subprocess, "Popen", local_popen)
    return calls


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
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == "corrupt_state"


def test_pre_merge_interrupt_restores(rec, tmp_path):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = _applying(prev, stage="quiesce")
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 0
    after = json.loads(Path(rec.STATE_PATH).read_text())
    assert after["applying"] is None and after["stages"] == []
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == "interrupted_pre_merge"


@pytest.mark.parametrize('published', [False, True])
def test_old_watchdog_premerge_settings_stage_never_claims_update(rec, tmp_path, published):
    _make_repo(tmp_path)
    config, legacy = tmp_path / 'config.json', Path(rec.DATA) / 'install_options.json'
    config.write_text(json.dumps({'other': {'keep': True}, **({'install_args': []} if published else {})}))
    legacy.write_text('{"v": 1, "install_args": []}')
    original = (config.read_bytes(), legacy.read_bytes())
    state = {'v': 1, 'applying': None, 'stages': [{'stage': 'settings_migration', 'at': time.time() - 4000}],
             'applied': [], 'attempts': {}, 'executed': {}}
    Path(rec.STATE_PATH).write_text(json.dumps(state))
    assert rec.recover() == 0
    after = json.loads(Path(rec.STATE_PATH).read_text())
    assert after['applied'] == [] and after['applying'] is None and after['stages'] == []
    assert json.loads(Path(rec.REPORT_PATH).read_text())['result'] == 'interrupted_pre_merge'
    assert (config.read_bytes(), legacy.read_bytes()) == original


@pytest.mark.parametrize("dirty", ["update_path", "update_third",
                                    "unrelated_path", "index_only",
                                    "index_update_third"])
def test_mixed_tree_resets_to_prev(rec, tmp_path, dirty):
    repo = _make_repo(tmp_path)
    (repo / "g.txt").write_text("operator")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "c2")
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "f.txt").write_text("target")
    _git(repo, "commit", "-qam", "target")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", prev)
    # half-applied checkout touches only update paths; any other tracked
    # change is someone's manual edit and must survive
    path = "f.txt" if dirty.startswith("update") or dirty.startswith("index_update") else "g.txt"
    (repo / path).write_text("target" if dirty == "update_path" else "CHANGED")
    if dirty in ("index_only", "index_update_third"):
        # staged, then only the worktree file put back: git diff HEAD is empty, the index is not
        _git(repo, "add", path)
        (repo / path).write_text("one" if dirty == "index_update_third" else "operator")
    state = _applying(prev, sha=target, stage="applying")
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    report_path = Path(rec.REPORT_PATH)
    if dirty == "update_path":
        assert rec.recover() == 0
        assert (repo / "f.txt").read_text() == "one"
        assert json.loads(report_path.read_text())["result"] == "mixed_tree_reset"
    else:
        assert rec.recover() == 1
        if dirty == "index_update_third":
            assert (repo / "f.txt").read_text() == "one"
            assert _git(repo, "show", ":f.txt").stdout == "CHANGED"
        elif dirty == "update_third":
            assert (repo / "f.txt").read_text() == "CHANGED"
            assert _git(repo, "show", ":f.txt").stdout == "one"
        else:
            assert (repo / "g.txt").read_text() == ("operator" if dirty == "index_only" else "CHANGED")
            assert _git(repo, "show", ":g.txt").stdout == ("CHANGED" if dirty == "index_only" else "operator")
        report = json.loads(report_path.read_text())
        assert report["result"] == "escalate"
        assert report["detail"] == "local changes outside update: " + path
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == prev


@pytest.mark.parametrize("head_at", ["prev", "target"])
@pytest.mark.parametrize("dirty", ["worktree", "staged", "index_only"])
def test_recover_preserves_third_content_at_either_update_head(
        rec, tmp_path, head_at, dirty):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    path = repo / "f.txt"
    path.write_text("target")
    _git(repo, "commit", "-qam", "target")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    head = prev if head_at == "prev" else target
    _git(repo, "checkout", "-q", head)
    head_text = path.read_text()

    path.write_text("operator third content")
    if dirty in ("staged", "index_only"):
        _git(repo, "add", "f.txt")
    if dirty == "index_only":
        path.write_text(head_text)
    worktree_before = path.read_bytes()
    index_before = _git(repo, "ls-files", "--stage", "-z").stdout
    index_text_before = _git(repo, "show", ":f.txt").stdout
    state = _applying(prev, sha=target, stage="applying")
    Path(rec.STATE_PATH).write_text(json.dumps(state))

    assert rec.recover() == 1

    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert path.read_bytes() == worktree_before
    assert _git(repo, "ls-files", "--stage", "-z").stdout == index_before
    assert _git(repo, "show", ":f.txt").stdout == index_text_before
    assert json.loads(Path(rec.STATE_PATH).read_text()) == state
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == "escalate"
    assert report["detail"] == "local changes outside update: f.txt"


@pytest.mark.parametrize("head_at", ["prev", "target"])
def test_recover_accepts_known_index_and_worktree_mixture(
        rec, tmp_path, monkeypatch, head_at):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    path = repo / "f.txt"
    path.write_text("target")
    _git(repo, "commit", "-qam", "target")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    head = prev if head_at == "prev" else target
    head_text = "one" if head_at == "prev" else "target"
    other_text = "target" if head_at == "prev" else "one"
    _git(repo, "checkout", "-q", head)
    expected_index = _git(repo, "ls-files", "--stage", "-z").stdout
    # Index and worktree deliberately hold opposite, known commit versions.
    path.write_text(other_text)
    _git(repo, "add", "f.txt")
    path.write_text(head_text)
    state = _applying(prev, sha=target, stage="applying")
    Path(rec.STATE_PATH).write_text(json.dumps(state))
    monkeypatch.setattr(rec, "_reconcile_membership", lambda snapshot: [])

    assert rec.recover() == 0

    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert path.read_text() == head_text
    assert _git(repo, "show", ":f.txt").stdout == head_text
    assert _git(repo, "ls-files", "--stage", "-z").stdout == expected_index
    assert _git(repo, "status", "--porcelain", "-uno").stdout == ""
    after = json.loads(Path(rec.STATE_PATH).read_text())
    assert after["applying"] is None and after["stages"] == []
    assert after["executed"] == state["executed"]
    assert after["applied"] == ([] if head_at == "prev" else [state["applying"]])
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == ("mixed_tree_reset" if head_at == "prev" else "resumed")


def test_mixed_tree_preserves_untracked_update_collision(rec, tmp_path,
                                                         monkeypatch):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "f.txt").write_text("target")
    (repo / "added.txt").write_text("target content")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "target")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", prev)
    (repo / "added.txt").write_text("user content")
    (repo / "f.txt").write_text("target")
    state = _applying(prev, sha=target, stage="applying")
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    monkeypatch.setattr(rec, "_notify", lambda *args: None)

    assert rec.recover() == 0
    assert (repo / "added.txt").read_text() == "user content"
    assert (repo / "f.txt").read_text() == "one"


def test_git_status_error_does_not_complete_recovery(rec, tmp_path,
                                                     monkeypatch):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "f.txt").write_text("interrupted checkout")
    state = _applying(prev, sha="0" * 40, stage="applying")
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    original_git = rec._git

    def fail_status(args, timeout=rec.T_GIT):
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 128, "", "synthetic failure")
        return original_git(args, timeout)

    monkeypatch.setattr(rec, "_git", fail_status)
    monkeypatch.setattr(rec, "_notify", lambda *args: None)

    assert rec.recover() == 1
    assert (repo / "f.txt").read_text() == "interrupted checkout"
    assert json.loads(Path(rec.STATE_PATH).read_text())["applying"] == state["applying"]
    assert json.loads(Path(rec.REPORT_PATH).read_text())["result"] == "escalate"


def test_merge_head_aborted(rec, tmp_path):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / ".git" / "MERGE_HEAD").write_text(prev + "\n")
    state = _applying(prev, stage="merge")
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 0
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == "merge_aborted"


def test_if_stale_respects_fresh_apply(rec, tmp_path):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # applying recorded 10s ago — not stale, watchdog must not touch
    state = _applying(prev, stage="quiesce", ago=10)
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover(if_stale=True) == 0
    after = json.loads(Path(rec.STATE_PATH).read_text())
    assert after["applying"] is not None      # untouched
    assert not os.path.exists(rec.REPORT_PATH)


def test_if_stale_recovers_old_apply(rec, tmp_path):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = _applying(prev, stage="quiesce", ago=4000)
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover(if_stale=True) == 0
    after = json.loads(Path(rec.STATE_PATH).read_text())
    assert after["applying"] is None


def test_unclassifiable_escalates_no_destruction(rec, tmp_path):
    repo = _make_repo(tmp_path)
    # HEAD exists but prev_sha is bogus — nothing matches
    state = _applying("0" * 40, stage="merge")
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 1
    report = json.loads(Path(rec.REPORT_PATH).read_text())
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
    now = time.time()                        # the clock the code reads
    os.utime(lock, (now, now))
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
        after = json.loads(Path(rec.STATE_PATH).read_text())
        assert after["applying"] is not None
    finally:
        os.close(fd)


def test_busy_lock_closes_failed_descriptor(rec, monkeypatch):
    import fcntl
    held = os.open(rec.UPDATE_LOCK, os.O_WRONLY | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    attempted = []
    original_open = os.open

    def record_open(*args):
        fd = original_open(*args)
        attempted.append(fd)
        return fd

    monkeypatch.setattr(rec.os, "open", record_open)
    try:
        assert rec._try_lock(rec.UPDATE_LOCK) is None
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(attempted[0])
    finally:
        os.close(held)
        for fd in attempted:
            with suppress(OSError):
                os.close(fd)


@pytest.mark.parametrize("result", [None, subprocess.CompletedProcess([], 1, "", "failed")])
def test_unverifiable_git_status_is_not_clean(rec, monkeypatch, result):
    monkeypatch.setattr(rec, "_git", lambda *args: result)
    assert rec._clean() is not True


@pytest.mark.parametrize("rollback", [False, True])
def test_resumed_target_retains_apply_history_and_restarts_gateway(
        rec, tmp_path, monkeypatch, gateway_restarts, rollback):
    repo = _make_repo(tmp_path)
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = _applying("0" * 40, sha=target, stage="postcheck")
    previous = {"sha": "1" * 40, "tag": "v0.9.0"}
    reverted = {"sha": "0" * 40, "prev_sha": target, "tag": "v1.1.0"}
    state["applied"] = [previous, reverted]
    state["applying"].update(rollback=rollback, plugin_changed=True,
                             command_id="cid-resume")
    applying = dict(state["applying"])
    monkeypatch.setattr(rec, "_reconcile_membership", lambda snapshot: [])
    rec._save_state(state)

    assert rec.recover() == 0
    after = rec._load_state()
    assert after["applying"] is None and after["stages"] == []
    assert after["applied"] == ([previous] if rollback
                                else [previous, reverted, applying])
    expected = "rolled_back" if rollback else "applied"
    assert after["executed"]["cid-resume"]["result"] == expected
    if rollback:
        assert after["attempts"]["v1.1.0"]["result"] == "rolled_back"
    assert len(gateway_restarts) == 1


def test_unfinished_reinstall_escalates_instead_of_applied(
        rec, tmp_path, monkeypatch, gateway_restarts):
    """Regression: the watchdog promoted an interrupted --reinstall to
    applied (reinstall_done false), so plan blocked every later version."""
    repo = _make_repo(tmp_path)
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = _applying("0" * 40, sha=target, stage="post_merge")
    state["applying"].update(reinstall=True, reinstall_done=False)
    monkeypatch.setattr(rec, "_reconcile_membership", lambda snapshot: [])
    rec._save_state(state)

    assert rec.recover() == 1
    after = rec._load_state()
    assert after["applying"]["reinstall"] and after["applied"] == []
    assert gateway_restarts == []


def test_done_bookkeeping_restarts_gateway(rec, tmp_path, gateway_restarts):
    _make_repo(tmp_path)
    state = {"v": 1, "applying": None,
             "stages": [{"stage": "done", "at": time.time() - 4000}],
             "applied": [{"plugin_changed": True, "command_id": "cid-done"}]}
    rec._save_state(state)
    assert rec.recover() == 0
    after = rec._load_state()
    assert after["executed"]["cid-done"]["result"] == "applied"
    assert after["stages"] == []
    assert len(gateway_restarts) == 1


def test_gateway_restart_oserror_keeps_bookkeeping(rec, tmp_path,
                                                  monkeypatch):
    _make_repo(tmp_path)
    state = {"v": 1, "applying": None,
             "stages": [{"stage": "done", "at": time.time() - 4000}],
             "applied": [{"plugin_changed": True, "command_id": "cid-done"}]}
    rec._save_state(state)

    notices = []
    monkeypatch.setattr(rec, "_notify", notices.append)
    original_popen = subprocess.Popen

    def broken_restart(args, **kwargs):
        # only the detached gateway restart request fails to start; any
        # owned git still runs against the temporary repository
        if len(args) > 2 and args[1:4] == ["-I", "-c", rec._GATEWAY_RESTART_PROGRAM]:
            raise OSError("synthetic launchctl missing")
        return original_popen(args, **kwargs)
    monkeypatch.setattr(rec.subprocess, "Popen", broken_restart)
    assert rec.recover() == 1                     # a lost request is not success
    after = rec._load_state()
    assert after["executed"]["cid-done"]["result"] == "applied"
    assert after["stages"] == []                  # bookkeeping is never undone
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == "restart_request_failed"
    assert report["completed"] == "resumed_done"
    assert "synthetic" not in json.dumps(report)  # no OS diagnostic text
    assert any("再起動" in text for text in notices)
    restart = json.loads(Path(rec.DATA, "gateway_restart.json").read_text())
    assert restart["status"] == "failed" and "synthetic" not in json.dumps(restart)


@pytest.mark.parametrize("content", [
    None, b"", b"{bad", b"[]", b'{"phase": "awaiting_consent", "report_id": "r"}',
    b'{"phase": "restored"}', b'{"restored_at": 1}', b'{"phase": "other"}'])
def test_awaiting_consent_matches_notify_cards(tmp_path, monkeypatch,
                                               content):
    """The standalone hold-marker check keeps notify_spool's fail-closed
    verdict — the writer freeze depends on both agreeing."""
    import notify_spool
    mod = _load()
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    if content is not None:
        (tmp_path / "restore_pending.json").write_bytes(content)
    assert (mod._awaiting_consent() is None) \
        == (notify_spool.restore_awaiting_consent(str(tmp_path)) is None)


def test_membership_reconcile_removes_undesired_agents(rec, tmp_path,
                                                       monkeypatch):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "ai.mcs.extract-old.plist").write_text("<plist/>")
    (agents / "ai.mcs.llamaserver.plist").write_text("<plist/>")
    (agents / "unrelated.other.plist").write_text("<plist/>")
    snapshot = {"agents": [{"label": "ai.mcs.extract-drainer"}],
                "cron": []}
    commands = []

    def record_command(args, **kwargs):
        commands.append(args)
        # launchd answers 113 once the booted-out label is gone
        return subprocess.CompletedProcess(args, 113 if args[1] == "print" else 0, "")

    monkeypatch.setattr(rec.subprocess, "run", record_command)
    monkeypatch.setattr(rec.shutil, "which", lambda name: None)
    rec._reconcile_membership(snapshot)
    # undesired owned agent removed only after launchd confirms it is gone
    target = f"gui/{os.getuid()}/ai.mcs.extract-old"
    assert commands == [["launchctl", "bootout", target], ["launchctl", "print", target]]
    assert not (agents / "ai.mcs.extract-old.plist").exists()
    assert (agents / "ai.mcs.llamaserver.plist").exists()
    assert (agents / "unrelated.other.plist").exists()


@pytest.mark.parametrize("returncode", [0, 1])
def test_cron_job_removal_requires_successful_list(
        rec, tmp_path, monkeypatch, returncode):
    hermes = tmp_path / "synthetic-hermes"
    hermes.write_text("# synthetic executable placeholder")
    Path(rec.MANIFEST_PATH).write_text(json.dumps(
        {"cron": [{"id": "abcdef", "script": "/synthetic/mcs_removed.sh"}]}))
    monkeypatch.setattr(rec.shutil, "which", lambda name: str(hermes))
    calls = []

    def list_result(argv, **kwargs):
        calls.append(argv)
        if argv[1:] == ["cron", "list", "--all"]:
            return subprocess.CompletedProcess(
                argv, returncode,
                "  abcdef [disabled]\n    Script:  /synthetic/mcs_removed.sh\n",
                "synthetic listing failure")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rec.subprocess, "run", list_result)
    problems = rec._reconcile_membership({"cron": [], "agents": []})
    assert ("cron_list_unverifiable" in problems) == (returncode != 0)
    expected = [[str(hermes), "cron", "list", "--all"]]
    if returncode == 0:
        expected.append([str(hermes), "cron", "remove", "abcdef"])
    assert calls == expected


# ---------------------------------------------------- _restore_db contract
# Rollback recovery must FAIL CLOSED: an unreadable backup/live DB or a
# failed copy can never be a silent "nothing to do" — that would report
# success while old code runs against a newer schema (F-restore).

def _mk_db(path, version):
    import sqlite3
    con = sqlite3.connect(str(path))
    con.execute(f"PRAGMA user_version={version}")
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()


def _consent(rec, live, back, cid="cid-consent", report=None):
    """Commit a synthetic ops.restore_approve receipt bound to the
    current loss report — the same row the human-approval path writes."""
    import sqlite3
    if report is None:
        report = rec._loss_report(str(back))
    con = sqlite3.connect(str(live))
    con.execute("""CREATE TABLE IF NOT EXISTS command_receipts(
      command_id TEXT PRIMARY KEY, payload_hash TEXT, project_id INTEGER,
      request_id INTEGER,
      outcome TEXT CHECK(outcome IN ('applied','rejected')),
      receipt_json TEXT, processed_at REAL)""")
    con.execute(
        "INSERT OR REPLACE INTO command_receipts VALUES(?,?,NULL,NULL,"
        "'applied',?,?)",
        (cid, "h" * 64, json.dumps({
            "cmd": "ops.restore_approve", "scheduled": True,
            "command_id": cid, "report_id": report["report_id"],
            "backup_sha256": report["backup_sha256"],
            "backup_schema": report["backup_schema"]}), time.time()))
    con.commit()
    con.close()
    return report


def test_restore_db_rejects_unreadable_backup(rec, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger.db"
    _mk_db(live, 7)
    monkeypatch.setattr(rec, "LEDGER", str(live))
    err = rec._restore_db(str(tmp_path / "data" / "missing.db"))
    assert err and "backup_unreadable" in err
    # live DB untouched
    assert rec._db_version(str(live)) == 7


@pytest.mark.parametrize("payload", ["{broken", "[]", '{"phase":"invalid"}'])
def test_restore_preserves_unreadable_marker(rec, tmp_path, payload):
    marker = tmp_path / "data" / "restore_pending.json"
    marker.write_text(payload)
    assert rec._restore_db(str(tmp_path / "missing.db")) == "restore_marker_unreadable"
    assert marker.read_text() == payload


def test_reconcile_reports_failed_service_command(rec, tmp_path, monkeypatch):
    setup = tmp_path / "repo" / "mcs" / "ops" / "mcs_setup.py"
    setup.parent.mkdir(parents=True)
    setup.write_text("# synthetic")
    monkeypatch.setattr(rec, "_setup_python", lambda: "synthetic-python")
    monkeypatch.setattr(rec, "_run_owned", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, "", "failed"))
    assert "services_reconcile_failed" in rec._reconcile_membership(None)


def test_restore_db_rejects_unreadable_live(rec, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger.db"          # absent
    back = tmp_path / "data" / "backup.db"
    _mk_db(back, 7)
    monkeypatch.setattr(rec, "LEDGER", str(live))
    assert rec._restore_db(str(back)) == "live_db_unreadable"


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
    monkeypatch.setattr(rec, "LEDGER", str(live))
    assert rec._restore_db(str(back)) is None
    con = sqlite3.connect("file:" + str(live) + "?mode=ro", uri=True)
    assert con.execute("SELECT x FROM t").fetchone()[0] == "sentinel"
    con.close()


def test_restore_db_reports_copy_failure(rec, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    monkeypatch.setattr(rec, "LEDGER", str(live))
    _consent(rec, live, back)
    def fail_copy(*args):
        raise OSError("synthetic copy failure")
    monkeypatch.setattr(rec.shutil, "copyfileobj", fail_copy)
    err = rec._restore_db(str(back))
    assert err and err.startswith("restore_failed:")
    assert rec._db_version(str(live)) == 8    # live DB untouched


def test_restore_db_holds_without_consent(rec, tmp_path, monkeypatch):
    """No bound receipt => no swap: the marker holds senders in
    awaiting_consent phase, the loss report is durable, the live DB is
    byte-identical, and the error token names the pending consent."""
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    monkeypatch.setattr(rec, "LEDGER", str(live))
    before = (tmp_path / "data" / "ledger.db").read_bytes()
    err = rec._restore_db(str(back))
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


def _rollback_journal(rec, backup):
    """The production precondition of a DB replace: a rollback-shaped
    applying journal (recover only replaces from that journal)."""
    state = rec._load_state()
    state["applying"] = {"tag": "v1.1.0", "sha": "b" * 40, "prev_sha": "a" * 40,
                         "rollback": True, "backup_path": str(backup), "at": time.time()}
    rec._save_state(state)


def _journal_kept(rec):
    applying = rec._load_state()["applying"]
    assert applying["rollback"] is True and "restore_staging" not in applying


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
    monkeypatch.setattr(rec, "LEDGER", str(live))
    before = live.read_bytes()

    err = rec._restore_db(str(back))
    assert err and err.startswith("restore_consent_pending:")
    assert rec._db_version(str(back)) == 7
    assert live.read_bytes() == before
    report = json.loads((tmp_path / "data" / "restore_report.json").read_text())
    assert report["backup_schema"] == 7
    _consent(rec, live, back)
    _rollback_journal(rec, back)
    assert rec._restore_db(str(back)) == "restore_reconcile_pending"
    _journal_kept(rec)
    assert rec._db_version(str(live)) == 7


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
    monkeypatch.setattr(rec, "LEDGER", str(live))
    before = live.read_bytes()
    decoy_before = decoy.read_bytes()

    err = rec._restore_db(str(back))
    assert err and err.startswith("restore_consent_pending:")
    report = json.loads((tmp_path / "data" / "restore_report.json").read_text())
    assert report["stored_since_backup"]["messages"] == 2
    assert live.read_bytes() == before
    _consent(rec, live, back)
    _rollback_journal(rec, back)
    assert rec._restore_db(str(back)) == "restore_reconcile_pending"
    _journal_kept(rec)
    assert rec._db_version(str(live)) == 7
    assert decoy.read_bytes() == decoy_before


def test_restore_db_stale_consent_rejected(rec, tmp_path, monkeypatch):
    """A receipt bound to a DIFFERENT report never unlocks the swap —
    the approval must match the exact loss report bytes+schema."""
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    monkeypatch.setattr(rec, "LEDGER", str(live))
    _consent(rec, live, back,
             report={"report_id": "f" * 64, "backup_sha256": "e" * 64,
                     "backup_schema": 7})
    err = rec._restore_db(str(back))
    assert err and err.startswith("restore_consent_pending:")
    assert rec._db_version(str(live)) == 8


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
    monkeypatch.setattr(rec, "LEDGER", str(live))
    monkeypatch.setattr(mcs_update, "LEDGER", str(live))
    monkeypatch.setattr(mcs_update, "RESTORE_REPORT_PATH",
                        str(tmp_path / "data" / "report.json"))
    approved = _consent(rec, live, back)
    assert approved["report_id"] == restore_evidence.create_loss_report(live_path=mcs_update.LEDGER, backup_path=str(back), report_path=mcs_update.RESTORE_REPORT_PATH)["report_id"]
    with sqlite3.connect(live) as con:
        con.execute("UPDATE messages SET body_html='changed synthetic'")
    current = rec._loss_report(str(back))
    assert current["report_id"] == restore_evidence.create_loss_report(live_path=mcs_update.LEDGER, backup_path=str(back), report_path=mcs_update.RESTORE_REPORT_PATH)["report_id"]
    assert current["report_id"] != approved["report_id"]
    assert rec._consent_for(current) is None


def test_restore_db_verify_mismatch(rec, tmp_path, monkeypatch):
    """A restore that reads back the wrong schema must be reported —
    claiming success on an unverified copy is the bug this fixes."""
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    monkeypatch.setattr(rec, "LEDGER", str(live))
    # live always reads 8 — even after the swap (the verify read)
    monkeypatch.setattr(rec, "_db_version",
                        lambda p: 7 if p == str(back) else 8)
    _consent(rec, live, back)
    _rollback_journal(rec, back)
    assert rec._restore_db(str(back)) == "restore_verify_failed"
    _journal_kept(rec)


def test_restore_db_replaces_and_verifies(rec, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    monkeypatch.setattr(rec, "LEDGER", str(live))
    _consent(rec, live, back)
    _rollback_journal(rec, back)
    assert rec._restore_db(str(back)) == "restore_reconcile_pending"
    _journal_kept(rec)
    assert rec._db_version(str(live)) == 7
    marker = json.loads((tmp_path / "data"
                         / "restore_pending.json").read_text())
    assert marker["phase"] == "restored"


def _rollback_state(rec, tmp_path, backup_path):
    """Journal state for the post-merge rollback-recovery path:
    HEAD == applying.sha (tree already converged), rollback flagged."""
    repo = _make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = _applying("0" * 40, sha=head, stage="applying")
    state["applying"].update(
        {"rollback": True, "command_id": "cid-rb",
         "backup_path": backup_path})
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    return state


def test_recover_escalates_on_rollback_db_restore(rec, tmp_path,
                                                  monkeypatch):
    """The silent-skip bug: backup unreadable during rollback recovery
    must escalate, never 'resumed' (F-restore)."""
    _rollback_state(rec, tmp_path, str(tmp_path / "data" / "gone.db"))
    live = tmp_path / "data" / "ledger.db"
    _mk_db(live, 8)
    monkeypatch.setattr(rec, "LEDGER", str(live))
    monkeypatch.setattr(rec, "_notify", lambda *a: pytest.fail("no notice during restore hold"))
    monkeypatch.setattr(rec, "_restart_drainers",
                        lambda **kw: pytest.fail("no writer restart before verified restore"))
    assert rec.recover() == 1
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == "escalate"
    assert "backup_unreadable" in report["detail"]
    after = json.loads(Path(rec.STATE_PATH).read_text())
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
    monkeypatch.setattr(rec, "LEDGER", str(live))
    monkeypatch.setattr(rec, "_notify", lambda *a: pytest.fail("no notice during restore hold"))
    monkeypatch.setattr(rec, "_restart_drainers",
                        lambda **kw: pytest.fail("no writer restart before verified restore"))
    monkeypatch.setattr(rec, "_reconcile_membership", lambda s: [])
    _consent(rec, live, back)
    assert rec.recover() == 1
    assert rec._db_version(str(live)) == 7
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == "restore_reconcile_pending" and "writers_stopped" not in report
    after = json.loads(Path(rec.STATE_PATH).read_text())
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
    monkeypatch.setattr(rec, "LEDGER", str(live))
    monkeypatch.setattr(rec, "_notify", lambda *a: None)
    monkeypatch.setattr(rec, "_reconcile_membership", lambda s: [])
    live_before = live.read_bytes()
    assert rec.recover() == 0
    assert live.read_bytes() == live_before        # no unauthorized swap
    state = json.loads(Path(rec.STATE_PATH).read_text())
    assert state["applying"]["rollback"] is True   # journal survives held
    marker = json.loads((tmp_path / "data"
                         / "restore_pending.json").read_text())
    assert marker["phase"] == "awaiting_consent"
    rep = json.loads(Path(rec.REPORT_PATH).read_text())
    assert rep["result"] == "restore_consent_pending"
    assert rep["detail"].startswith("restore_consent_pending:")
    import restore_evidence
    assert rec.RESTORE_LOSS_NOTICE == restore_evidence.RESTORE_LOSS_NOTICE
    assert rec.RESTORE_LOSS_NOTICE in rep["detail"]
    assert rec.RESTORE_LOSS_NOTICE in rec._recovery_explanation(rep["detail"])
    # second pass is stable — still held, still no swap
    assert rec.recover() == 0
    assert live.read_bytes() == live_before


def test_known_cron_scripts_cover_setup_cron_jobs():
    """mcs_setup.CRON_JOBS scripts must all live in KNOWN_CRON_SCRIPTS —
    a missing entry makes a snapshot-based recovery delete a live,
    desired cron job (the mcs_health.sh drift class)."""
    rec = _load()
    desired = {script for _name, _sched, script in scheduled_jobs.CRON_JOBS}
    assert desired <= rec.KNOWN_CRON_SCRIPTS
    assert "mcs_offsite.sh" in rec.KNOWN_CRON_SCRIPTS
    assert "ai.mcs.cron.mcs-offsite" not in rec.KNOWN_AGENT_LABELS


@pytest.mark.parametrize(("listed", "evidence", "owned"), [
    ("mcs_offsite.sh", [], False),
    ("/shared/mcs_offsite.sh", [], False),
    ("mcs_custom.sh", [], False),
    ("mcs_offsite.sh", [{"id": "abcdef", "script": "mcs_offsite.sh"}], True),
    ("mcs_offsite.sh", [{"id": "badbad", "script": "mcs_offsite.sh"}], False),
    ("/shared/mcs_offsite.sh", [{"id": "abcdef", "script": "mcs_offsite.sh"}], False),
    ("mcs_offsite.sh", [{"id": "abcdef", "script": "mcs_check.sh"}], False),
    ("mcs_custom.sh", [None, {}, {"id": "abcdef", "script": "mcs_custom.sh"}], True),
])
def test_cron_owner_requires_id_script_pair_or_canonical_identity(
        rec, listed, evidence, owned):
    assert rec._owned_cron("abcdef", listed, evidence) is owned


def test_canonical_offsite_identity_is_distinct_from_shared_basename(rec):
    canonical = str(Path(rec.SCRIPTS_DIR, "mcs_offsite.sh"))
    assert rec._owned_cron("abcdef", canonical, [])
    assert not rec._owned_cron(
        "abcdef", str(Path(rec.SCRIPTS_DIR, "mcs_shared.sh")), [])


def _backup_membership(rec, tmp_path, monkeypatch, *, enabled=False, template=False,
                       proof="manifest", listed="mcs_offsite.sh", returncode=0):
    root = Path(rec.DATA).parent
    root.joinpath("config.json").write_text(json.dumps(
        {"runtime_mode": "hermes", "backup": {"enabled": enabled}}))
    if template:
        wrapper = Path(rec.REPO, "deployment/scripts/mcs_offsite.sh")
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text("# synthetic template: never executed")
    evidence = {"cron": [{"id": "abcdef", "script": "mcs_offsite.sh"}]}
    if proof == "manifest":
        Path(rec.MANIFEST_PATH).write_text(json.dumps(evidence))
    hermes = tmp_path / "synthetic-hermes"
    hermes.write_text("# synthetic placeholder: never executed")
    monkeypatch.setattr(rec.shutil, "which", lambda name: str(hermes))
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        if argv == [str(hermes), "cron", "list", "--all"]:
            return subprocess.CompletedProcess(
                argv, returncode,
                f"  abcdef [disabled]\n    Script:  {listed}\n"
                "  123abc [active]\n    Script:  /shared/mcs_custom.sh\n"
                "  456def [active]\n    Script:  mcs_health.sh\n", "")
        assert argv == [str(hermes), "cron", "remove", "abcdef"]
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(rec.subprocess, "run", run)
    return hermes, calls, evidence


@pytest.mark.parametrize(("enabled", "template", "retired"), [
    (False, False, True), (False, True, True),
    (True, False, True), (True, True, False),
])
def test_optional_backup_current_desire_requires_opt_in_and_restored_capability(
        rec, tmp_path, monkeypatch, enabled, template, retired):
    hermes, calls, _ = _backup_membership(
        rec, tmp_path, monkeypatch, enabled=enabled, template=template)
    rec._reconcile_membership({"cron": [], "agents": []})
    expected = [[str(hermes), "cron", "list", "--all"]]
    if retired:
        expected.append([str(hermes), "cron", "remove", "abcdef"])
    assert calls == expected


def test_snapshot_desire_protects_backup_without_installing_it(rec, tmp_path, monkeypatch):
    hermes, calls, snapshot = _backup_membership(
        rec, tmp_path, monkeypatch, proof="snapshot")
    rec._reconcile_membership(snapshot)
    assert calls == [[str(hermes), "cron", "list", "--all"]]


@pytest.mark.parametrize("listed", ["mcs_offsite.sh", "/shared/mcs_offsite.sh"])
def test_missing_or_retargeted_backup_ownership_never_deletes_shared_job(
        rec, tmp_path, monkeypatch, listed):
    hermes, calls, _ = _backup_membership(
        rec, tmp_path, monkeypatch, proof="none", listed=listed)
    rec._reconcile_membership({"cron": [], "agents": []})
    assert calls == [[str(hermes), "cron", "list", "--all"]]


def test_canonical_identity_can_retire_old_owned_backup(rec, tmp_path, monkeypatch):
    hermes, calls, _ = _backup_membership(
        rec, tmp_path, monkeypatch, proof="none",
        listed=str(Path(rec.SCRIPTS_DIR, "mcs_offsite.sh")))
    rec._reconcile_membership({"cron": [], "agents": []})
    assert calls == [[str(hermes), "cron", "list", "--all"],
                     [str(hermes), "cron", "remove", "abcdef"]]


@pytest.mark.parametrize("contents", ["broken", "[]", '{"cron":null}', '{"cron":[null]}'])
def test_unusable_manifest_does_not_turn_basename_into_ownership(
        rec, tmp_path, monkeypatch, contents):
    hermes, calls, _ = _backup_membership(rec, tmp_path, monkeypatch, proof="none")
    Path(rec.MANIFEST_PATH).write_text(contents)
    rec._reconcile_membership({"cron": [], "agents": []})
    assert calls == [[str(hermes), "cron", "list", "--all"]]


def test_failed_job_list_never_retires_optional_backup(rec, tmp_path, monkeypatch):
    hermes, calls, _ = _backup_membership(rec, tmp_path, monkeypatch, returncode=1)
    assert "cron_list_unverifiable" in rec._reconcile_membership({})
    assert calls == [[str(hermes), "cron", "list", "--all"]]


@pytest.mark.parametrize("backup", [None, [], {}, {"enabled": "false"}])
def test_unknown_backup_intent_holds_owned_job(rec, tmp_path, monkeypatch, backup):
    hermes, calls, _ = _backup_membership(rec, tmp_path, monkeypatch)
    Path(rec.DATA).parent.joinpath("config.json").write_text(json.dumps({"backup": backup}))
    assert "backup_config_unverifiable" in rec._reconcile_membership({})
    assert calls == [[str(hermes), "cron", "list", "--all"]]


def test_backup_manifest_never_adds_external_scheduler_to_standalone_host(
        rec, tmp_path, monkeypatch):
    _standalone_recovery(rec)
    Path(rec.MANIFEST_PATH).write_text(json.dumps(
        {"cron": [{"id": "abcdef", "script": "mcs_offsite.sh"}]}))
    setup = Path(rec.REPO, "mcs/ops/mcs_setup.py")
    setup.parent.mkdir(parents=True)
    setup.write_text("# synthetic services entry: never executed")
    monkeypatch.setattr(rec, "_setup_python", lambda: "/synthetic/python")
    calls = []
    def run(argv, *args, **kw):
        assert argv == ["/synthetic/python", str(setup), "services"]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(rec, "_run_owned", run)
    assert rec._reconcile_membership({"cron": [], "agents": []}) == []
    assert calls == [["/synthetic/python", str(setup), "services"]]


def _standalone_recovery(rec):
    Path(rec.DATA).parent.joinpath("config.json").write_text(json.dumps({"runtime_mode": "standalone"}))
    value = {"pid": os.getpid(), "generation": "b" * 32, "updated_at": time.time(),
             "children": {name: {"pid": os.getpid(), "kind": "background"}
                          for name in ("extract-0", "extract-2")}}
    path = Path(rec.DATA, "standalone-status.json")
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return value


def test_standalone_recovery_uses_own_python_and_one_scheduler(rec, tmp_path, monkeypatch):
    _standalone_recovery(rec)
    exe = tmp_path / "venv/bin/python3"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o700)
    setup = tmp_path / "repo/mcs/ops/mcs_setup.py"
    setup.parent.mkdir(parents=True)
    setup.write_text("# fixture")
    calls = []
    monkeypatch.setattr(rec, "_run_owned", lambda argv, *args, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert rec._setup_python() == str(exe)
    assert rec._reconcile_membership({"agents": [], "cron": []}) == []
    assert calls == [[str(exe), str(setup), "services"]]


def test_standalone_recovery_restores_drainers_and_requests_restart(rec, monkeypatch):
    value = _standalone_recovery(rec)
    monkeypatch.setattr(rec, "_launchctl", lambda *a, **k: pytest.fail("native host must not be killed"))
    Path(rec.MARKER_PATH).write_text("marker")
    assert rec._restart_drainers() == []
    assert not Path(rec.MARKER_PATH).exists()
    rec._restart_gateway()
    path = Path(rec.DATA, "standalone-restart.request")
    assert json.loads(path.read_text())["generation"] == value["generation"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_standalone_recovery_does_not_restart_an_unverified_host(rec):
    _standalone_recovery(rec)
    Path(rec.DATA, "standalone-status.json").unlink()
    rec._restart_gateway()
    assert not Path(rec.DATA, "standalone-restart.request").exists()


def test_reconcile_uses_install_interpreter_not_watchdog_python(
        rec, tmp_path, monkeypatch):
    """Services use the install-time venv, independent of watchdog Python."""
    setup = tmp_path / "repo" / "mcs" / "ops" / "mcs_setup.py"
    setup.parent.mkdir(parents=True)
    setup.write_text("# synthetic")
    venv_py = tmp_path / "venv-python"
    venv_py.write_text("#!/bin/sh\nexit 0\n")
    venv_py.chmod(0o755)
    monkeypatch.setattr(rec, "HERMES_PY", str(venv_py))
    calls = []

    def run(args, *extra, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rec, "_run_owned", run)
    assert rec._reconcile_membership(None) == ["cron_list_unverifiable"]
    assert [str(venv_py), str(setup), "services"] in calls


def test_reconcile_without_capable_interpreter_is_unavailable(
        rec, tmp_path, monkeypatch):
    setup = tmp_path / "repo" / "mcs" / "ops" / "mcs_setup.py"
    setup.parent.mkdir(parents=True)
    setup.write_text("# synthetic")
    monkeypatch.setattr(rec, "HERMES_PY", str(tmp_path / "absent"))
    monkeypatch.setattr(rec.sys, "version_info", (3, 9, 6))
    assert "services_reconcile_unavailable" in \
        rec._reconcile_membership(None)


def test_recovery_repo_comes_from_install_sidecar(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "RECOVERY_DIR", str(tmp_path))
    assert mod._installed_repo() == mod.HOME
    (tmp_path / "repo_path").write_text("/opt/checkout/hermes-mcs\n")
    assert mod._installed_repo() == "/opt/checkout/hermes-mcs"
    (tmp_path / "repo_path").write_text("relative/path\n")
    assert mod._installed_repo() == mod.HOME


def test_git_does_not_walk_into_parent_repository(rec, tmp_path,
                                                  monkeypatch):
    (tmp_path / "p").mkdir()
    parent = _make_repo(tmp_path / "p")
    child = parent / "not-a-checkout"
    child.mkdir()
    monkeypatch.setattr(rec, "REPO", str(child))
    monkeypatch.setattr(rec.subprocess, "run", subprocess.run)
    r = rec._git(["rev-parse", "HEAD"])
    assert r is not None and r.returncode != 0


def test_hung_launchctl_is_reported_not_raised(rec, monkeypatch, tmp_path):
    def hung(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

    monkeypatch.setattr(rec.subprocess, "run", hung)
    monkeypatch.setattr(rec, "RESIDENT_LABELS", ("ai.mcs.extract-drainer",))
    clock = iter(range(0, 10_000, 20))
    monkeypatch.setattr(rec, "time", SimpleNamespace(
        time=time.time, monotonic=lambda: next(clock), sleep=lambda s: None))
    assert rec._restart_drainers() == ["ai.mcs.extract-drainer"]


def test_restart_drainers_wait_ignores_wall_clock_jump(rec, monkeypatch):
    """A sleep/NTP jump of the wall clock must not expire the 15s pid wait."""
    wall, mono, pids = [1000.0], [0.0], iter([None, None, 4242, 4242])

    def sleep(s):
        mono[0] += s
        wall[0] += 3600                # system clock jumps an hour per poll
    monkeypatch.setattr(rec, "time", SimpleNamespace(
        time=lambda: wall[0], monotonic=lambda: mono[0], sleep=sleep))
    monkeypatch.setattr(rec, "_launchctl", lambda args: None)
    monkeypatch.setattr(rec, "_agent_pid", lambda label, unknown=None: next(pids))
    monkeypatch.setattr(rec, "RESIDENT_LABELS", ("ai.mcs.x",))
    assert rec._restart_drainers() == []


@pytest.mark.parametrize("outcomes,late,problems,boots", [
    ([5, 0], False, [], 2),
    ([5, 5, 5], True, [], 3),
    ([5, 5, 5], False, ["ai.mcs.x"], 3),
    # exit 0 is not proof: the label must answer `print` afterwards
    ([0], False, ["ai.mcs.x"], 1),
])
def test_restart_drainers_retries_transient_bootstrap(
        rec, tmp_path, monkeypatch, outcomes, late, problems, boots):
    fake = _FakeLaunchd(outcomes, late_load=late, loads=outcomes != [0])
    sleeps = []
    clock = iter(range(0, 10 ** 6, 5))
    monkeypatch.setattr(rec.subprocess, "run", fake)
    monkeypatch.setattr(rec, "time", SimpleNamespace(
        time=time.time, monotonic=lambda: next(clock), sleep=sleeps.append))
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "ai.mcs.x.plist").write_text("<plist/>")
    monkeypatch.setattr(rec, "RESIDENT_LABELS", ("ai.mcs.x",))
    assert rec._restart_drainers() == problems
    assert fake.calls[0] == "bootout"
    assert fake.calls.count("bootstrap") == boots
    assert sleeps.count(1) == sum(rc != 0 for rc in outcomes)


# ------------------- consent hold survives a git failure (= mcs_update)

@pytest.mark.parametrize("failing", ["rev-parse", "status"])
def test_git_failure_inside_consent_hold_keeps_the_freeze(
        rec, tmp_path, monkeypatch, failing):
    """The watchdog's own hold is journaled (restore_consent); a later
    pass whose git fails keeps drainers down, both markers, 'applying'
    and the receipt, reports restore_consent_blocked and never touches
    the ledger — the old code escalated: drainers up, marker gone."""
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    _rollback_state(rec, tmp_path, str(back))
    monkeypatch.setattr(rec, "LEDGER", str(live))
    monkeypatch.setattr(rec, "_reconcile_membership", lambda s: [])
    restarts, notices = [], []
    monkeypatch.setattr(rec, "_restart_drainers",
                        lambda: restarts.append(1) or [])
    monkeypatch.setattr(rec, "_notify", notices.append)
    Path(rec.MARKER_PATH).write_text("1")
    assert rec.recover() == 0                       # held
    hold = json.loads(Path(rec.STATE_PATH).read_text())["restore_consent"]
    marker_path = tmp_path / "data" / "restore_pending.json"
    assert hold["report_id"] == json.loads(
        marker_path.read_text())["report_id"]
    live_before = live.read_bytes()
    original_git = rec._git

    def broken(args, timeout=rec.T_GIT):
        if args[0] == failing:
            return None                             # hung / unspawnable
        return original_git(args, timeout)
    monkeypatch.setattr(rec, "_git", broken)
    for _ in range(2):
        assert rec.recover() == 1
    assert restarts == []
    assert Path(rec.MARKER_PATH).exists()
    assert json.loads(marker_path.read_text())["phase"] == "awaiting_consent"
    state = json.loads(Path(rec.STATE_PATH).read_text())
    assert state["applying"]["rollback"] and state["restore_consent"]
    assert "cid-rb" not in state.get("executed", {})
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == "restore_consent_blocked"
    assert live.read_bytes() == live_before         # digest intact
    assert notices == []                           # report-only restore hold


def test_git_failure_outside_hold_still_escalates(rec, tmp_path,
                                                  monkeypatch):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    with open(rec.STATE_PATH, "w") as f:
        json.dump(_applying(prev, stage="merge"), f)
    Path(rec.MARKER_PATH).write_text("1")
    monkeypatch.setattr(rec, "_git", lambda args, timeout=rec.T_GIT: None)
    notices = []
    monkeypatch.setattr(rec, "_notify", notices.append)
    assert rec.recover() == 1
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == "escalate"
    assert report["detail"] == "git unverifiable: rev-parse HEAD"
    assert not Path(rec.MARKER_PATH).exists()
    assert "保存状態を確認できません" in notices[0]
    assert "git unverifiable" not in notices[0]


# ------------------------------------------ escalation notice dedup

def test_escalation_notifies_once_per_condition(rec, tmp_path,
                                               monkeypatch):
    """Every watchdog pass re-escalates the same stuck journal: notify
    the first time, again only on a changed condition or after
    ESCALATE_REALERT_S — never on each 900s pass."""
    _make_repo(tmp_path)
    notices = []
    monkeypatch.setattr(rec, "_notify", notices.append)
    state = _applying("0" * 40, stage="merge")      # unclassifiable
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    for _ in range(3):
        assert rec.recover() == 1
    assert len(notices) == 1
    # a different report in between (state change) re-arms the alert
    rec._report("restore_consent_pending", "x")
    assert rec.recover() == 1
    assert len(notices) == 2
    # changed journal = new condition
    state["applying"]["tag"] = "v1.2.0"
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 1
    assert len(notices) == 3
    # unchanged but past the re-alert interval
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    report["notified_at"] -= rec.ESCALATE_REALERT_S
    Path(rec.REPORT_PATH).write_text(json.dumps(report))
    assert rec.recover() == 1
    assert len(notices) == 4
    assert rec.recover() == 1
    assert len(notices) == 4


def test_alert_key_matches_updater():
    """Both tools dedup against one recovery_report.json."""
    import mcs_update
    recovery = _load()
    state = _applying("p" * 40)
    for detail in ("git unverifiable: git_timeout: rev-parse",
                   "unclassifiable repo state — HEAD=abc",
                   "resume incomplete: a,b"):
        assert recovery._alert_key(state, detail) \
            == mcs_update._alert_key(state, detail)
    assert recovery.ESCALATE_REALERT_S == mcs_update.ESCALATE_REALERT_S


def test_recovery_constants_and_journal_checks_match_updater(tmp_path):
    import sqlite3

    import mcs_update
    recovery = _load()
    for name in ("STALE_S", "_STAGING_NAME", "_STAGING_KEYS"):
        assert getattr(recovery, name) == getattr(mcs_update, name), name
    db = tmp_path / "synthetic.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version = 7")
    con.close()
    for path in (db, tmp_path / "missing.db"):
        assert recovery._db_version(str(path)) == restore_evidence.database_version(str(path))
    valid = _applying("p" * 40)
    states = [valid, {"v": 1}, {"v": 1, "applying": None, "restore_consent": {}},
              None, [], {"v": 2}, {"v": True}, {"v": 1, "stages": {}},
              {"v": 1, "stages": [1]}, {"v": 1, "applying": "x"},
              {"v": 1, "executed": []}, {"v": 1, "stages": [{"stage": 1}]},
              {"v": 1, "applied": [{"at": -1}]}, {"v": 1, "applied": [{"at": "1"}]},
              {"v": 1, "stages": [{"stage": "x", "at": 1e12}]}]
    for state in states:
        assert recovery._valid_state(state) == mcs_update.journal_store.valid_state(state), state
    assert recovery._valid_state(valid) and not recovery._valid_state({"v": 2})


# ---------- repeated escalation ensures drainers (= mcs_update)

def test_repeated_escalation_ensures_drainers_instead_of_bouncing(
        rec, tmp_path, monkeypatch):
    """The 900s watchdog re-escalates the same stuck journal: bounce
    only the first time, later passes only ensure drainers run (the old
    code bootout+bootstrapped healthy drainers every pass)."""
    _make_repo(tmp_path)
    calls = []
    monkeypatch.setattr(rec, "_restart_drainers",
                        lambda **k: calls.append(k.get("bounce", True))
                        or [])
    state = _applying("0" * 40, stage="merge")      # unclassifiable
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    for _ in range(3):
        assert rec.recover() == 1
    assert calls == [True, False, False]
    state["applying"]["tag"] = "v1.2.0"
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 1
    assert calls[-1] is True


class _EnsureLaunchd:
    """launchctl stub with per-label loaded/running state; `hung`
    labels time out on every verb."""

    def __init__(self, loaded=(), running=(), hung=()):
        self.loaded, self.running = set(loaded), set(running)
        self.hung = set(hung)
        self.calls = []

    def __call__(self, argv, *args, **kwargs):
        verb, target = argv[1], argv[-1]
        label = os.path.basename(target)[:-6] \
            if verb == "bootstrap" else target.rsplit("/", 1)[-1]
        self.calls.append((verb, label))
        if label in self.hung:
            raise subprocess.TimeoutExpired(argv, 30)
        rc, out = 0, ""
        if verb == "bootout":
            self.loaded.discard(label)
            self.running.discard(label)
        elif verb in ("bootstrap", "kickstart"):
            if verb == "bootstrap":
                self.loaded.add(label)
            if label in self.loaded:
                self.running.add(label)
        elif verb == "print":
            rc = 0 if label in self.loaded else 113
            out = "\tpid = 4242\n" if label in self.running else ""
        return subprocess.CompletedProcess(argv, rc, out, "")


def test_restart_drainers_ensure_mode_never_bounces_a_running_drainer(
        rec, tmp_path, monkeypatch):
    fake = _EnsureLaunchd(loaded={"ai.mcs.run", "ai.mcs.stopped"},
                          running={"ai.mcs.run"}, hung={"ai.mcs.hung"})
    monkeypatch.setattr(rec.subprocess, "run", fake)
    monkeypatch.setattr(rec.time, "sleep", lambda s: None)
    agents = tmp_path / "agents"
    agents.mkdir()
    labels = ("ai.mcs.run", "ai.mcs.stopped", "ai.mcs.gone", "ai.mcs.hung")
    for label in labels:
        (agents / (label + ".plist")).write_text("<plist/>")
    monkeypatch.setattr(rec, "RESIDENT_LABELS", labels)
    clock = iter(range(0, 10 ** 6))
    monkeypatch.setattr(rec, "time", SimpleNamespace(
        time=time.time, monotonic=lambda: next(clock), sleep=lambda s: None))
    assert rec._restart_drainers(bounce=False) == ["ai.mcs.hung"]
    assert not any(verb == "bootout" for verb, _ in fake.calls)
    assert [c for c in fake.calls if c[1] == "ai.mcs.run"] \
        == [("print", "ai.mcs.run")]
    assert ("kickstart", "ai.mcs.stopped") in fake.calls
    assert ("bootstrap", "ai.mcs.stopped") not in fake.calls
    assert ("bootstrap", "ai.mcs.gone") in fake.calls
    assert ("bootstrap", "ai.mcs.hung") in fake.calls   # fail closed


def test_drainers_key_matches_updater():
    import mcs_update
    recovery = _load()
    state = _applying("p" * 40)
    for head in ("h" * 40, "", None):
        assert recovery._drainers_key(state, head) \
            == mcs_update._drainers_key(state, head)


# ------ every escalation inside a consent hold keeps the freeze

def _held_world(rec, tmp_path, monkeypatch):
    live = tmp_path / "data" / "ledger.db"
    back = tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    _rollback_state(rec, tmp_path, str(back))
    monkeypatch.setattr(rec, "LEDGER", str(live))
    monkeypatch.setattr(rec, "_reconcile_membership", lambda s: [])
    restarts, notices = [], []
    monkeypatch.setattr(rec, "_restart_drainers",
                        lambda **k: restarts.append(k) or [])
    monkeypatch.setattr(rec, "_notify", notices.append)
    Path(rec.MARKER_PATH).write_text("1")
    assert rec.recover() == 0                       # held
    return live, back, restarts, notices


def _assert_frozen(rec, tmp_path, live, live_before, restarts):
    assert restarts == []
    assert Path(rec.MARKER_PATH).exists()
    marker = tmp_path / "data" / "restore_pending.json"
    assert json.loads(marker.read_text())["phase"] == "awaiting_consent"
    state = json.loads(Path(rec.STATE_PATH).read_text())
    assert state["applying"]["rollback"] and state["restore_consent"]
    assert "cid-rb" not in state.get("executed", {})
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == "restore_consent_blocked"
    assert live.read_bytes() == live_before


@pytest.mark.parametrize("kind", ["merge_head", "unclassifiable",
                                  "backup_gone"])
def test_non_git_escalation_inside_consent_hold_keeps_the_freeze(
        rec, tmp_path, monkeypatch, kind):
    """Journal inconsistency / restore failure inside a hold used to
    escalate (drainers up on the newer-schema DB, marker gone, receipt
    consumed). The freeze reports locally without sending. After
    consent, the DB copy succeeds but notification reconciliation still
    requires a compatible writer, so the journal and markers remain held."""
    live, back, restarts, notices = _held_world(rec, tmp_path,
                                                monkeypatch)
    repo = tmp_path / "repo"
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if kind == "merge_head":
        (repo / ".git" / "MERGE_HEAD").write_text(head + "\n")
        undo = (repo / ".git" / "MERGE_HEAD").unlink
    elif kind == "unclassifiable":
        _git(repo, "commit", "--allow-empty", "-qm", "drift")
        undo = lambda: _git(repo, "reset", "-q", "--hard", head)  # noqa: E731
    else:
        os.rename(back, str(back) + ".away")
        undo = lambda: os.rename(str(back) + ".away", back)  # noqa: E731
    live_before = live.read_bytes()
    for _ in range(2):
        assert rec.recover() == 1
    _assert_frozen(rec, tmp_path, live, live_before, restarts)
    assert notices == []                           # report-only restore hold
    undo()
    _consent(rec, live, back)
    assert rec.recover() == 1  # copied, but compatible notification reconcile is still pending
    assert rec._db_version(str(live)) == 7
    assert live.read_bytes() == back.read_bytes()
    state = json.loads(Path(rec.STATE_PATH).read_text())
    assert "cid-rb" not in state.get("executed", {})
    assert state["restore_consent"] and state["applying"]["restore_incomplete"] is True
    assert restarts == []
    assert Path(rec.MARKER_PATH).exists()
    marker = tmp_path / "data" / "restore_pending.json"
    assert json.loads(marker.read_text())["phase"] == "restored"
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == "restore_reconcile_pending" and "writers_stopped" not in report
    held_db, held_state, held_marker = live.read_bytes(), Path(rec.STATE_PATH).read_bytes(), marker.read_bytes()
    assert rec.recover() == 1
    assert live.read_bytes() == held_db and Path(rec.STATE_PATH).read_bytes() == held_state
    assert marker.read_bytes() == held_marker and restarts == []
    assert notices == []  # pending reconcile never sends


def test_marker_only_hold_survives_git_failure(rec, tmp_path,
                                               monkeypatch):
    """A hold entered by a watchdog that did not journal it has only
    the awaiting_consent marker: a git failure keeps the freeze (old
    code escalated), the hold is recorded from the marker. Consent permits
    the DB copy; compatible notification reconciliation remains required."""
    live, back, restarts, notices = _held_world(rec, tmp_path,
                                                monkeypatch)
    state = json.loads(Path(rec.STATE_PATH).read_text())
    rid = state.pop("restore_consent")["report_id"]
    Path(rec.STATE_PATH).write_text(json.dumps(state))
    live_before = live.read_bytes()
    original_git = rec._git

    def broken(args, timeout=rec.T_GIT):
        return None if args[0] == "rev-parse" else original_git(args,
                                                                timeout)
    monkeypatch.setattr(rec, "_git", broken)
    assert rec.recover() == 1
    _assert_frozen(rec, tmp_path, live, live_before, restarts)
    hold = json.loads(Path(rec.STATE_PATH).read_text())["restore_consent"]
    assert hold["from_marker"] is True and hold["report_id"] == rid
    monkeypatch.setattr(rec, "_git", original_git)
    _consent(rec, live, back)
    assert rec.recover() == 1  # consent authorizes copying, not notification reconciliation
    assert rec._db_version(str(live)) == 7
    assert live.read_bytes() == back.read_bytes()
    state = json.loads(Path(rec.STATE_PATH).read_text())
    assert "cid-rb" not in state.get("executed", {})
    assert state["restore_consent"] and state["applying"]["restore_incomplete"] is True
    assert restarts == [] and Path(rec.MARKER_PATH).exists()
    marker = tmp_path / "data" / "restore_pending.json"
    assert json.loads(marker.read_text())["phase"] == "restored"
    report = json.loads(Path(rec.REPORT_PATH).read_text())
    assert report["result"] == "restore_reconcile_pending" and "writers_stopped" not in report
    held_db, held_state, held_marker = live.read_bytes(), Path(rec.STATE_PATH).read_bytes(), marker.read_bytes()
    assert rec.recover() == 1
    assert live.read_bytes() == held_db and Path(rec.STATE_PATH).read_bytes() == held_state
    assert marker.read_bytes() == held_marker and restarts == []


def test_notify_kills_a_wedged_standalone_sender(tmp_path, monkeypatch):
    """communicate()'s 60s cap must reap the child — a wedged sender
    must not outlive the watchdog."""
    mod = _load()
    monkeypatch.setattr(mod, "HOME", str(tmp_path))
    monkeypatch.setattr(mod, "DATA", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    (repo / "mcs_standalone").mkdir(parents=True)
    (repo / "mcs_standalone" / "__main__.py").write_text("#")
    monkeypatch.setattr(mod, "REPO", str(repo))
    python = tmp_path / "venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("#!")
    python.chmod(0o755)
    (tmp_path / "config.json").write_text(json.dumps(
        {"runtime_mode": "standalone", "notify_target": "slack:C0SYNTH"}))
    calls = []

    class Wedged:
        def communicate(self, *a, **k):
            raise subprocess.TimeoutExpired("send", 60)

        def kill(self):
            calls.append("kill")

        def wait(self):
            calls.append("wait")

    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: Wedged())
    mod._notify("synthetic")
    assert calls == ["kill", "wait"]


def test_notify_refuses_a_symlinked_standalone_config(tmp_path, monkeypatch):
    """_notify reads config through _runtime_config's symlink guard —
    a symlinked config.json must not drive any sender."""
    mod = _load()
    monkeypatch.setattr(mod, "HOME", str(tmp_path))
    monkeypatch.setattr(mod, "DATA", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    (repo / "mcs_standalone").mkdir(parents=True)
    (repo / "mcs_standalone" / "__main__.py").write_text("#")
    monkeypatch.setattr(mod, "REPO", str(repo))
    python = tmp_path / "venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("#!")
    python.chmod(0o755)
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(
        {"runtime_mode": "standalone", "notify_target": "slack:C0SYNTH"}))
    (tmp_path / "config.json").symlink_to(real)
    calls = []
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda *a, **k: calls.append(a) or None)
    mod._notify("synthetic")
    assert calls == []


@pytest.mark.parametrize("print_code", [1, 124])
def test_membership_keeps_an_agent_plist_launchd_did_not_report_gone(
        rec, tmp_path, monkeypatch, print_code):
    agents = tmp_path / "agents"
    agents.mkdir()
    plist = agents / "ai.mcs.extract-old.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(rec.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(
        args, print_code if args[1] == "print" else 0, ""))
    problems = rec._reconcile_membership({"agents": [], "cron": []})
    assert plist.exists()
    assert "agent_retirement_unverifiable:ai.mcs.extract-old" in problems


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
    rec._save_state(state)
    Path(rec.MARKER_PATH).write_text("synthetic writer freeze")
    if shape == "merge_head":
        git_dir = Path(rec.REPO, ".git")
        git_dir.mkdir(parents=True)
        (git_dir / "MERGE_HEAD").write_text("c" * 40)
    monkeypatch.setattr(rec, "_head", lambda: "c" * 40 if shape == "unknown_head" else "b" * 40)
    monkeypatch.setattr(rec, "_clean", lambda: shape != "previous_dirty")
    monkeypatch.setattr(rec, "_clean_stale_git_locks", lambda: [])

    def forbidden(*args, **kwargs):
        pytest.fail("incomplete restore must not reset, finish, restore an unbound DB, or restart writers")

    for name in ("_git", "_git_out", "_restore_db", "_restart_drainers", "_restart_services", "_finish"):
        monkeypatch.setattr(rec, name, forbidden)
    for _ in range(2):
        assert rec.recover() == 1
        after = rec._load_state()
        assert after["applying"] == applying and after["stages"] == state["stages"]
        assert "synthetic-held" not in after["executed"]
        assert Path(rec.MARKER_PATH).read_text() == "synthetic writer freeze"
        report = json.loads(Path(rec.REPORT_PATH).read_text())
        assert "writers_stopped" not in report
        assert "restore_incomplete" in report["detail"]


@pytest.mark.parametrize("flag", ["--help", "-h", "--unknown"])
def test_cli_guidance_never_recovers(rec, monkeypatch, capsys, flag):
    monkeypatch.setattr(rec.sys, "argv", ["mcs_recover.py", flag])
    monkeypatch.setattr(rec, "recover", lambda **kw: pytest.fail("must not recover"))
    monkeypatch.setattr(rec, "_load_state", lambda: pytest.fail("must not read state"))
    result = rec.main()
    captured = capsys.readouterr()
    if flag == "--unknown":
        assert result == 2
        assert "復旧は実行していません" in captured.err
    else:
        assert result == 0
        assert "人の承認" in captured.out
        assert "--status" in captured.out
        assert "変更しません" in captured.out


def test_status_keeps_json_contract(rec, monkeypatch, capsys):
    state = {"v": 1, "applying": None, "stages": [], "applied": []}
    monkeypatch.setattr(rec.sys, "argv", ["mcs_recover.py", "--status"])
    monkeypatch.setattr(rec, "_load_state", lambda: state)
    monkeypatch.setattr(rec, "_head", lambda: "synthetic-sha")
    monkeypatch.setattr(rec, "_clean", lambda: True)
    monkeypatch.setattr(rec, "recover", lambda **kw: pytest.fail("must not recover"))
    assert rec.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "state": state, "head": "synthetic-sha", "clean": True,
        "settings_migration_pending": False}


@pytest.mark.parametrize("detail,expected", [
    ("git unverifiable: rev-parse HEAD", "保存状態を確認できません"),
    ("rollback db restore: restore_consent_pending:synthetic", "人の承認待ち"),
    ("rollback db restore: restore_failed: synthetic-private-path", "保留を維持"),
    ("restore_reconcile_pending", "通知記録の照合が未完了"),
    ("reinstall_incomplete", "再インストール"),
    ("unclassifiable repo state — synthetic", "自動変更を見送りました"),
    ("unexpected synthetic-private-path", "復旧を完了できません"),
])
def test_recovery_notice_explains_reason_without_raw_details(rec, detail, expected):
    message = rec._recovery_explanation(detail)
    assert expected in message
    assert "synthetic" not in message


def test_refused_reset_at_target_does_not_bounce_drainers_every_pass(rec, tmp_path, monkeypatch):
    """CR: HEAD at target with a manual edit outside the update escalates
    without resetting; that must not count as a tree reset, so only the
    first pass bounces the drainers and the edit is kept."""
    repo = _make_repo(tmp_path)
    (repo / "g.txt").write_text("operator")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "c2")
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "f.txt").write_text("target")
    _git(repo, "commit", "-qam", "target")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "g.txt").write_text("MANUAL")
    calls = []
    monkeypatch.setattr(rec, "_restart_drainers",
                        lambda **k: calls.append(k.get("bounce", True)) or [])
    with open(rec.STATE_PATH, "w") as f:
        json.dump(_applying(prev, sha=target, stage="applying"), f)
    for _ in range(3):
        assert rec.recover() == 1
    assert calls == [True, False, False]
    assert (repo / "g.txt").read_text() == "MANUAL"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == target


_MIGRATION = {"from": "3.14.7", "to": "3.14.8"}


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
    with open(rec.STATE_PATH, "w") as f:
        json.dump(state, f)
    Path(rec.MARKER_PATH).write_text("{}")
    def snapshot():
        return (Path(rec.STATE_PATH).read_bytes(), Path(rec.MARKER_PATH).read_bytes(),
                (repo / "f.txt").read_text(), _git(repo, "rev-parse", "HEAD").stdout,
                (venv / "pyvenv.cfg").read_text(), (backup / "pyvenv.cfg").read_text(),
                db.read_bytes())
    assert not rec._load_state().get("_corrupt")   # the record is accepted as state, not as corruption
    before = snapshot()
    notices, restarts = [], []
    monkeypatch.setattr(rec, "_notify", lambda text: notices.append(text))
    monkeypatch.setattr(rec, "_restart_drainers", lambda **k: restarts.append(k) or [])
    monkeypatch.setattr(rec, "_restart_services", lambda *a, **k: restarts.append(a) or 0)
    for _ in range(3):
        assert rec.recover(if_stale=True) == 1
    assert snapshot() == before and restarts == []
    assert len(notices) == 1 and "mcs_update.py recover" in notices[0]
    assert json.loads(Path(rec.REPORT_PATH).read_text())["result"] == "runtime_migration_pending"
    # the target updater recovered and cleared the journal: the watchdog's normal pass resumes
    with open(rec.STATE_PATH, "w") as f:
        json.dump({"v": 1, "applying": None, "stages": [], "applied": [], "attempts": {}, "executed": {}}, f)
    assert rec.recover() == 0 and not os.path.exists(rec.MARKER_PATH)


@pytest.mark.parametrize("by", ["mcs_update", "mcs_recover"])
@pytest.mark.parametrize("journal_fault", ["none", "save", "corrupt", "missing"])
def test_restored_witness_holds_fresh_watchdog_retry(
        rec, tmp_path, monkeypatch, by, journal_fault):
    live, back = tmp_path / "data" / "ledger.db", tmp_path / "data" / "backup.db"
    _mk_db(live, 7)
    _mk_db(back, 7)
    monkeypatch.setattr(rec, "LEDGER", str(live))
    _rollback_journal(rec, back)
    marker = tmp_path / "data" / "restore_pending.json"
    witness = {"v": 1, "phase": "restored", "by": by,
               "restored_at": 123, "at": 123, "backup_path": str(back)}
    marker.write_text(json.dumps(witness))
    Path(rec.MARKER_PATH).write_text("synthetic writer freeze")
    original_save = rec._save_state
    if journal_fault == "save":
        def failed_save(state):
            if (state.get("applying") or {}).get("restore_incomplete"):
                raise OSError("synthetic journal publish failure")
            return original_save(state)
        monkeypatch.setattr(rec, "_save_state", failed_save)
    elif journal_fault == "corrupt":
        Path(rec.STATE_PATH).write_text("{broken")
    elif journal_fault == "missing":
        Path(rec.STATE_PATH).unlink()
    db_before, marker_before = live.read_bytes(), marker.read_bytes()
    assert rec._restore_db(str(back)) == "restore_reconcile_pending"
    assert live.read_bytes() == db_before and marker.read_bytes() == marker_before
    # Reload the standalone script, not a module retaining an in-memory hold.
    fresh = _load()
    for name in ("REPO", "DATA", "STATE_PATH", "LEDGER", "UPDATE_LOCK", "RUN_LOCK", "MARKER_PATH", "REPORT_PATH"):
        monkeypatch.setattr(fresh, name, getattr(rec, name))
    def forbidden(*args, **kwargs):
        pytest.fail("unreconciled restore must not run Git, write DB, restart or delete markers")
    for name in ("_git", "_head", "_restart_drainers", "_restart_services", "_remove_marker", "_notify", "_reconcile_membership"):
        monkeypatch.setattr(fresh, name, forbidden)
    state_before = Path(rec.STATE_PATH).read_bytes() if Path(rec.STATE_PATH).exists() else None
    for _ in range(2):
        assert fresh.recover() == (2 if journal_fault == "corrupt" else 1)
        assert live.read_bytes() == db_before and marker.read_bytes() == marker_before
        assert Path(rec.MARKER_PATH).read_text() == "synthetic writer freeze"
        assert (Path(rec.STATE_PATH).read_bytes() if Path(rec.STATE_PATH).exists() else None) == state_before
    if journal_fault == "none":
        assert rec._load_state()["applying"]["restore_incomplete"] is True


def test_independent_copy_requires_reconcile_after_actual_swap_and_fresh_retry(rec, tmp_path, monkeypatch):
    live, back = tmp_path / "data" / "ledger.db", tmp_path / "data" / "backup.db"
    _mk_db(live, 8)
    _mk_db(back, 7)
    monkeypatch.setattr(rec, "LEDGER", str(live))
    _consent(rec, live, back)
    _rollback_journal(rec, back)
    assert rec._restore_db(str(back)) == "restore_reconcile_pending"
    assert rec._db_version(str(live)) == 7
    _journal_kept(rec)
    marker = tmp_path / "data" / "restore_pending.json"
    original_marker = marker.read_bytes()
    original_state = Path(rec.STATE_PATH).read_bytes()
    assert rec._load_state()["applying"]["restore_incomplete"] is True
    fresh = _load()
    for name in ("DATA", "STATE_PATH", "LEDGER"):
        monkeypatch.setattr(fresh, name, getattr(rec, name))
    assert fresh._restore_db(str(back)) == "restore_reconcile_pending"
    assert marker.read_bytes() == original_marker
    assert Path(rec.STATE_PATH).read_bytes() == original_state
    assert fresh._db_version(str(live)) == 7


@pytest.mark.parametrize("phase", ["awaiting_consent", "restored"])
def test_updater_reconcile_hold_does_not_claim_backup_adoption_marker(rec, tmp_path, phase):
    marker = tmp_path / "data" / "restore_pending.json"
    marker.write_text(json.dumps({"by": "mcs_backup_restore", "phase": phase,
                                  "restored_at": 123}))
    before = marker.read_bytes()
    assert rec._unreconciled_restore() is None
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
    monkeypatch.setattr(rec, "LEDGER", str(live))
    restarts = []
    monkeypatch.setattr(rec, "_reconcile_membership", lambda _snapshot: [])
    monkeypatch.setattr(rec, "_restart_drainers", lambda **kw: restarts.append("drainers") or [])
    monkeypatch.setattr(rec, "_restart_services", lambda *args: restarts.append("services") or 0)
    monkeypatch.setattr(rec, "_replace_database", lambda *args: pytest.fail("schema noop must not copy"))
    Path(rec.MARKER_PATH).write_text("synthetic update in progress")
    live_before = live.read_bytes()
    assert not (tmp_path / "data" / "restore_pending.json").exists()
    assert rec.recover() == 0
    assert live.read_bytes() == live_before and rec._db_version(str(live)) == 7
    state = rec._load_state()
    assert state["applying"] is None and state["stages"] == []
    assert state["executed"]["cid-rb"]["result"] == "rolled_back"
    assert "restore_consent" not in state and not Path(rec.MARKER_PATH).exists()
    assert restarts == ["drainers", "services"]
    assert json.loads(Path(rec.REPORT_PATH).read_text())["result"] == "resumed"


@pytest.mark.parametrize("already_pending", [True, False], ids=["before-dialog", "after-recover"])
def test_interactive_pending_reconcile_requires_compatible_handler_not_more_consent(
        rec, tmp_path, monkeypatch, capsys, already_pending):
    import builtins
    marker = tmp_path / "data" / "restore_pending.json"
    witness = {"v": 1, "by": "mcs_recover", "phase": "restored", "restored_at": 123}
    state = {"applying": {"rollback": True}, "stages": [], "restore_consent": {"report_id": "synthetic"}}
    monkeypatch.setattr(rec, "_load_state", lambda: state)
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



def _commit_recovery_schema(rec, tmp_path, version):
    repo = tmp_path / "repo"
    canonical = repo / "mcs" / "core" / "ledger.py"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("SCHEMA_VERSION = " + str(version) + "\nraise AssertionError('restored code must not be imported')\n")
    _git(repo, "add", "mcs/core/ledger.py")
    _git(repo, "commit", "-qm", "synthetic canonical schema")
    state = rec._load_state()
    state["applying"]["sha"] = _git(repo, "rev-parse", "HEAD").stdout.strip()
    rec._save_state(state)
    return canonical


@pytest.mark.parametrize("version", [7, 9])
def test_matching_restored_writer_resumes_but_keeps_delivery_witness(rec, tmp_path, monkeypatch, version):
    live, back = tmp_path / "data" / "ledger.db", tmp_path / "data" / "backup.db"
    _mk_db(live, version + 1)
    _mk_db(back, version)
    _rollback_state(rec, tmp_path, str(back))
    _commit_recovery_schema(rec, tmp_path, version)
    monkeypatch.setattr(rec, "LEDGER", str(live))
    _consent(rec, live, back)
    restarts = []
    marker = tmp_path / "data" / "restore_pending.json"
    def restart(**kwargs):
        assert rec._db_version(str(live)) == version
        assert json.loads(marker.read_text())["phase"] == "restored"
        restarts.append("writers")
        return []
    monkeypatch.setattr(rec, "_restart_drainers", restart)
    monkeypatch.setattr(rec, "_reconcile_membership", lambda _snapshot: [])
    monkeypatch.setattr(rec, "_restart_services", lambda *args: 0)
    Path(rec.MARKER_PATH).write_text("synthetic update hold")
    assert rec.recover() == 0
    assert rec._db_version(str(live)) == version and live.read_bytes() == back.read_bytes()
    state = rec._load_state()
    assert state["applying"] is None and state["stages"] == []
    assert state["executed"]["cid-rb"]["result"] == "rolled_back"
    assert restarts == ["writers"] and not Path(rec.MARKER_PATH).exists()
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
    monkeypatch.setattr(rec, "LEDGER", str(live))
    rec._mark_restored(str(back))
    marker = tmp_path / "data" / "restore_pending.json"
    witness = marker.read_bytes()
    fresh = _load()
    for name in ("REPO", "DATA", "STATE_PATH", "LEDGER"):
        monkeypatch.setattr(fresh, name, getattr(rec, name))
    assert fresh._restored_writer_compatible() is True
    assert fresh._restore_db(str(back)) is None
    assert marker.read_bytes() == witness and fresh._db_version(str(live)) == 7


@pytest.mark.parametrize("fault", ["schema", "dynamic", "duplicate", "bytes", "head", "git_read", "source_read"])
def test_unproven_restored_writer_stays_held(rec, tmp_path, monkeypatch, fault):
    live, back = tmp_path / "data" / "ledger.db", tmp_path / "data" / "backup.db"
    _mk_db(live, 7)
    _mk_db(back, 7)
    _rollback_state(rec, tmp_path, str(back))
    canonical = _commit_recovery_schema(rec, tmp_path, 9 if fault == "schema" else 7)
    monkeypatch.setattr(rec, "LEDGER", str(live))
    rec._mark_restored(str(back))
    if fault in ("dynamic", "duplicate", "bytes"):
        canonical.write_text({"dynamic": "SCHEMA_VERSION = int('7')\n",
                              "duplicate": "SCHEMA_VERSION = 7\nSCHEMA_VERSION = 7\n",
                              "bytes": canonical.read_text() + "# untracked byte drift\n"}[fault])
        monkeypatch.setattr(rec, "_clean", lambda: True)  # bytes proof must be independent
    elif fault == "head":
        monkeypatch.setattr(rec, "_head", lambda: "f" * 40)
    elif fault == "git_read":
        real_git = rec._git
        monkeypatch.setattr(rec, "_git", lambda args, *a, **kw: None if args[0] == "show" else real_git(args, *a, **kw))
    elif fault == "source_read":
        canonical.unlink()
    marker = tmp_path / "data" / "restore_pending.json"
    witness, contents = marker.read_bytes(), live.read_bytes()
    assert rec._restored_writer_compatible() is False
    assert rec._restore_db(str(back)) == "restore_reconcile_pending"
    assert live.read_bytes() == contents and marker.read_bytes() == witness
    assert rec._load_state()["applying"]["restore_incomplete"] is True


def test_existing_same_version_awaiting_consent_promotion_keeps_legacy_flow(rec, tmp_path, monkeypatch):
    live, back = tmp_path / "data" / "ledger.db", tmp_path / "data" / "backup.db"
    _mk_db(live, 7)
    _mk_db(back, 7)
    _rollback_state(rec, tmp_path, str(back))
    _commit_recovery_schema(rec, tmp_path, 7)
    monkeypatch.setattr(rec, "LEDGER", str(live))
    rec._mark_restored(str(back), phase="awaiting_consent", report_id="synthetic-still-held")
    marker = tmp_path / "data" / "restore_pending.json"
    before = marker.read_bytes()
    assert rec._restored_writer_compatible() is False  # new permission excludes awaiting phase
    assert rec._restore_db(str(back)) is None  # existing same-version legacy promotion stays intact
    promoted = json.loads(marker.read_text())
    assert promoted["phase"] == "restored" and marker.read_bytes() != before
    assert rec._restored_writer_compatible() is True and rec._db_version(str(live)) == 7
