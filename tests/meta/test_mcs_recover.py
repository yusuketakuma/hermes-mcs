"""Synthetic mcs recover contracts; shared helpers preserve isolated runtime state."""
import restore_evidence
import json
import os
import subprocess
import importlib.util
import hashlib
import time
from contextlib import suppress
from pathlib import Path
import pytest
from ops_testkit import _git, _load
from mcs_recover_testkit import _EnsureLaunchd as _EnsureLaunchd, _MIGRATION as _MIGRATION, _applying as _applying, _assert_frozen as _assert_frozen, _backup_membership as _backup_membership, _commit_recovery_schema as _commit_recovery_schema, _consent as _consent, _held_world as _held_world, _journal_kept as _journal_kept, _make_repo as _make_repo, _mk_db as _mk_db, _rollback_journal as _rollback_journal, _rollback_state as _rollback_state, _standalone_recovery as _standalone_recovery, gateway_restarts as gateway_restarts, rec as rec


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
    marker = Path(rec.mcs_recovery_core.DATA).parent / '.install-options-migration.pending'
    marker.write_text('synthetic receipt')
    monkeypatch.setattr(rec.mcs_recovery_core, '_try_lock', lambda *_a: pytest.fail('must not take recovery locks'))
    assert rec.recover() == 2
    assert marker.read_text() == 'synthetic receipt'
    assert '移行結果を確認できません' in capsys.readouterr().out


def test_release_fence_holds_before_recovery_reports_or_lock_cleanup(rec, monkeypatch):
    import release_apply_owner
    runtime = os.path.dirname(rec.mcs_recovery_core.DATA)
    with release_apply_owner._control_store(runtime) as store:
        store.write('release-owner-activation.json', {'synthetic': True})
    monkeypatch.setattr(rec.mcs_recovery_core, '_report', lambda *_a, **_k: pytest.fail('report before release hold'))
    monkeypatch.setattr(rec, '_report_alert', lambda *_a, **_k: pytest.fail('alert before release hold'))
    monkeypatch.setattr(rec.mcs_recovery_core, '_try_lock', lambda *_a: pytest.fail('old locks before release hold'))
    monkeypatch.setattr(rec.mcs_recovery_core, '_remove_marker', lambda: pytest.fail('cleanup before release hold'))
    assert rec.recover() == 2
    assert rec.mcs_recovery_core._RELEASE_GATE_FD is None
    assert not Path(rec.mcs_recovery_core.REPORT_PATH).exists()


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
    with rec.mcs_recovery_core._release_scope(os.path.dirname(rec.mcs_recovery_core.DATA)) as fd:
        identity = os.fstat(fd).st_ino
        rec.mcs_recovery_core._run_owned(['synthetic'], 1, {})
        assert os.fstat(fd).st_ino == identity
    rec.mcs_recovery_core._run_owned(['synthetic'], 1, {})
    assert captured == [(identity,), ()] and rec.mcs_recovery_core._RELEASE_GATE_FD is None


def test_no_state_no_action(rec):
    assert rec.recover() == 0


def test_corrupt_state_escalates(rec, tmp_path):
    (tmp_path / "data" / "update_state.json").write_text("{broken")
    assert rec.recover() == 2
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
    assert report["result"] == "corrupt_state"


def test_pre_merge_interrupt_restores(rec, tmp_path):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = _applying(prev, stage="quiesce")
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 0
    after = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
    assert after["applying"] is None and after["stages"] == []
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
    assert report["result"] == "interrupted_pre_merge"


@pytest.mark.parametrize('published', [False, True])
def test_old_watchdog_premerge_settings_stage_never_claims_update(rec, tmp_path, published):
    _make_repo(tmp_path)
    config, legacy = tmp_path / 'config.json', Path(rec.mcs_recovery_core.DATA) / 'install_options.json'
    config.write_text(json.dumps({'other': {'keep': True}, **({'install_args': []} if published else {})}))
    legacy.write_text('{"v": 1, "install_args": []}')
    original = (config.read_bytes(), legacy.read_bytes())
    state = {'v': 1, 'applying': None, 'stages': [{'stage': 'settings_migration', 'at': time.time() - 4000}],
             'applied': [], 'attempts': {}, 'executed': {}}
    Path(rec.mcs_recovery_core.STATE_PATH).write_text(json.dumps(state))
    assert rec.recover() == 0
    after = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
    assert after['applied'] == [] and after['applying'] is None and after['stages'] == []
    assert json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())['result'] == 'interrupted_pre_merge'
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
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    report_path = Path(rec.mcs_recovery_core.REPORT_PATH)
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
    Path(rec.mcs_recovery_core.STATE_PATH).write_text(json.dumps(state))

    assert rec.recover() == 1

    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert path.read_bytes() == worktree_before
    assert _git(repo, "ls-files", "--stage", "-z").stdout == index_before
    assert _git(repo, "show", ":f.txt").stdout == index_text_before
    assert json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text()) == state
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
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
    Path(rec.mcs_recovery_core.STATE_PATH).write_text(json.dumps(state))
    monkeypatch.setattr(rec.mcs_recovery_services, "_reconcile_membership", lambda snapshot: [])

    assert rec.recover() == 0

    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert path.read_text() == head_text
    assert _git(repo, "show", ":f.txt").stdout == head_text
    assert _git(repo, "ls-files", "--stage", "-z").stdout == expected_index
    assert _git(repo, "status", "--porcelain", "-uno").stdout == ""
    after = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
    assert after["applying"] is None and after["stages"] == []
    assert after["executed"] == state["executed"]
    assert after["applied"] == ([] if head_at == "prev" else [state["applying"]])
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
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
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    monkeypatch.setattr(rec.mcs_recovery_services, "_notify", lambda *args: None)

    assert rec.recover() == 0
    assert (repo / "added.txt").read_text() == "user content"
    assert (repo / "f.txt").read_text() == "one"


def test_git_status_error_does_not_complete_recovery(rec, tmp_path,
                                                     monkeypatch):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "f.txt").write_text("interrupted checkout")
    state = _applying(prev, sha="0" * 40, stage="applying")
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    original_git = rec.mcs_recovery_core._git

    def fail_status(args, timeout=rec.mcs_recovery_core.T_GIT):
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 128, "", "synthetic failure")
        return original_git(args, timeout)

    monkeypatch.setattr(rec.mcs_recovery_core, "_git", fail_status)
    monkeypatch.setattr(rec.mcs_recovery_services, "_notify", lambda *args: None)

    assert rec.recover() == 1
    assert (repo / "f.txt").read_text() == "interrupted checkout"
    assert json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())["applying"] == state["applying"]
    assert json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())["result"] == "escalate"


def test_merge_head_aborted(rec, tmp_path):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / ".git" / "MERGE_HEAD").write_text(prev + "\n")
    state = _applying(prev, stage="merge")
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 0
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
    assert report["result"] == "merge_aborted"


def test_if_stale_respects_fresh_apply(rec, tmp_path):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # applying recorded 10s ago — not stale, watchdog must not touch
    state = _applying(prev, stage="quiesce", ago=10)
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover(if_stale=True) == 0
    after = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
    assert after["applying"] is not None      # untouched
    assert not os.path.exists(rec.mcs_recovery_core.REPORT_PATH)


def test_if_stale_recovers_old_apply(rec, tmp_path):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = _applying(prev, stage="quiesce", ago=4000)
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover(if_stale=True) == 0
    after = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
    assert after["applying"] is None


def test_unclassifiable_escalates_no_destruction(rec, tmp_path):
    repo = _make_repo(tmp_path)
    # HEAD exists but prev_sha is bogus — nothing matches
    state = _applying("0" * 40, stage="merge")
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 1
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
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
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 0
    assert lock.exists()                     # fresh — preserved
    old = time.time() - rec.mcs_recovery_core.GIT_LOCK_MIN_AGE_S - 60
    os.utime(lock, (old, old))
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 0
    assert not lock.exists()                 # stale — removed


def test_busy_update_lock_defers(rec, tmp_path):
    """A live apply holds update.lock — recovery must not interfere."""
    import fcntl
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = _applying(prev, stage="merge", ago=4000)
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    fd = os.open(rec.mcs_recovery_core.UPDATE_LOCK, os.O_WRONLY | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert rec.recover() == 0          # deferred, untouched
        after = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
        assert after["applying"] is not None
    finally:
        os.close(fd)


def test_busy_lock_closes_failed_descriptor(rec, monkeypatch):
    import fcntl
    held = os.open(rec.mcs_recovery_core.UPDATE_LOCK, os.O_WRONLY | os.O_CREAT)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    attempted = []
    original_open = os.open

    def record_open(*args):
        fd = original_open(*args)
        attempted.append(fd)
        return fd

    monkeypatch.setattr(rec.os, "open", record_open)
    try:
        assert rec.mcs_recovery_core._try_lock(rec.mcs_recovery_core.UPDATE_LOCK) is None
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(attempted[0])
    finally:
        os.close(held)
        for fd in attempted:
            with suppress(OSError):
                os.close(fd)


@pytest.mark.parametrize("result", [None, subprocess.CompletedProcess([], 1, "", "failed")])
def test_unverifiable_git_status_is_not_clean(rec, monkeypatch, result):
    monkeypatch.setattr(rec.mcs_recovery_core, "_git", lambda *args: result)
    assert rec.mcs_recovery_core._clean() is not True


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
    monkeypatch.setattr(rec.mcs_recovery_services, "_reconcile_membership", lambda snapshot: [])
    rec.mcs_recovery_core._save_state(state)

    assert rec.recover() == 0
    after = rec.mcs_recovery_core._load_state()
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
    monkeypatch.setattr(rec.mcs_recovery_services, "_reconcile_membership", lambda snapshot: [])
    rec.mcs_recovery_core._save_state(state)

    assert rec.recover() == 1
    after = rec.mcs_recovery_core._load_state()
    assert after["applying"]["reinstall"] and after["applied"] == []
    assert gateway_restarts == []


def test_done_bookkeeping_restarts_gateway(rec, tmp_path, gateway_restarts):
    _make_repo(tmp_path)
    state = {"v": 1, "applying": None,
             "stages": [{"stage": "done", "at": time.time() - 4000}],
             "applied": [{"plugin_changed": True, "command_id": "cid-done"}]}
    rec.mcs_recovery_core._save_state(state)
    assert rec.recover() == 0
    after = rec.mcs_recovery_core._load_state()
    assert after["executed"]["cid-done"]["result"] == "applied"
    assert after["stages"] == []
    assert len(gateway_restarts) == 1


def test_gateway_restart_oserror_keeps_bookkeeping(rec, tmp_path,
                                                  monkeypatch):
    _make_repo(tmp_path)
    state = {"v": 1, "applying": None,
             "stages": [{"stage": "done", "at": time.time() - 4000}],
             "applied": [{"plugin_changed": True, "command_id": "cid-done"}]}
    rec.mcs_recovery_core._save_state(state)

    notices = []
    monkeypatch.setattr(rec.mcs_recovery_services, "_notify", notices.append)
    original_popen = subprocess.Popen

    def broken_restart(args, **kwargs):
        # only the detached gateway restart request fails to start; any
        # owned git still runs against the temporary repository
        if len(args) > 2 and args[1:4] == ["-I", "-c", rec.mcs_recovery_gateway._GATEWAY_RESTART_PROGRAM]:
            raise OSError("synthetic launchctl missing")
        return original_popen(args, **kwargs)
    monkeypatch.setattr(rec.subprocess, "Popen", broken_restart)
    assert rec.recover() == 1                     # a lost request is not success
    after = rec.mcs_recovery_core._load_state()
    assert after["executed"]["cid-done"]["result"] == "applied"
    assert after["stages"] == []                  # bookkeeping is never undone
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
    assert report["result"] == "restart_request_failed"
    assert report["completed"] == "resumed_done"
    assert "synthetic" not in json.dumps(report)  # no OS diagnostic text
    assert any("再起動" in text for text in notices)
    restart = json.loads(Path(rec.mcs_recovery_core.DATA, "gateway_restart.json").read_text())
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
    monkeypatch.setattr(mod.mcs_recovery_core, "DATA", str(tmp_path))
    if content is not None:
        (tmp_path / "restore_pending.json").write_bytes(content)
    assert (mod.mcs_recovery_core._awaiting_consent() is None) \
        == (notify_spool.restore_awaiting_consent(str(tmp_path)) is None)


def test_recovery_repo_comes_from_install_sidecar(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.mcs_recovery_core, "RECOVERY_DIR", str(tmp_path))
    assert mod.mcs_recovery_core._installed_repo() == mod.mcs_recovery_core.HOME
    (tmp_path / "repo_path").write_text("/opt/checkout/hermes-mcs\n")
    assert mod.mcs_recovery_core._installed_repo() == "/opt/checkout/hermes-mcs"
    (tmp_path / "repo_path").write_text("relative/path\n")
    assert mod.mcs_recovery_core._installed_repo() == mod.mcs_recovery_core.HOME


def test_git_does_not_walk_into_parent_repository(rec, tmp_path,
                                                  monkeypatch):
    (tmp_path / "p").mkdir()
    parent = _make_repo(tmp_path / "p")
    child = parent / "not-a-checkout"
    child.mkdir()
    monkeypatch.setattr(rec.mcs_recovery_core, "REPO", str(child))
    monkeypatch.setattr(rec.subprocess, "run", subprocess.run)
    r = rec.mcs_recovery_core._git(["rev-parse", "HEAD"])
    assert r is not None and r.returncode != 0


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
    monkeypatch.setattr(rec.mcs_recovery_core, "LEDGER", str(live))
    monkeypatch.setattr(rec.mcs_recovery_services, "_reconcile_membership", lambda s: [])
    restarts, notices = [], []
    monkeypatch.setattr(rec.mcs_recovery_services, "_restart_drainers",
                        lambda: restarts.append(1) or [])
    monkeypatch.setattr(rec.mcs_recovery_services, "_notify", notices.append)
    Path(rec.mcs_recovery_core.MARKER_PATH).write_text("1")
    assert rec.recover() == 0                       # held
    hold = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())["restore_consent"]
    marker_path = tmp_path / "data" / "restore_pending.json"
    assert hold["report_id"] == json.loads(
        marker_path.read_text())["report_id"]
    live_before = live.read_bytes()
    original_git = rec.mcs_recovery_core._git

    def broken(args, timeout=rec.mcs_recovery_core.T_GIT):
        if args[0] == failing:
            return None                             # hung / unspawnable
        return original_git(args, timeout)
    monkeypatch.setattr(rec.mcs_recovery_core, "_git", broken)
    for _ in range(2):
        assert rec.recover() == 1
    assert restarts == []
    assert Path(rec.mcs_recovery_core.MARKER_PATH).exists()
    assert json.loads(marker_path.read_text())["phase"] == "awaiting_consent"
    state = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
    assert state["applying"]["rollback"] and state["restore_consent"]
    assert "cid-rb" not in state.get("executed", {})
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
    assert report["result"] == "restore_consent_blocked"
    assert live.read_bytes() == live_before         # digest intact
    assert notices == []                           # report-only restore hold


def test_git_failure_outside_hold_still_escalates(rec, tmp_path,
                                                  monkeypatch):
    repo = _make_repo(tmp_path)
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(_applying(prev, stage="merge"), f)
    Path(rec.mcs_recovery_core.MARKER_PATH).write_text("1")
    monkeypatch.setattr(rec.mcs_recovery_core, "_git", lambda args, timeout=rec.mcs_recovery_core.T_GIT: None)
    notices = []
    monkeypatch.setattr(rec.mcs_recovery_services, "_notify", notices.append)
    assert rec.recover() == 1
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
    assert report["result"] == "escalate"
    assert report["detail"] == "git unverifiable: rev-parse HEAD"
    assert not Path(rec.mcs_recovery_core.MARKER_PATH).exists()
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
    monkeypatch.setattr(rec.mcs_recovery_services, "_notify", notices.append)
    state = _applying("0" * 40, stage="merge")      # unclassifiable
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    for _ in range(3):
        assert rec.recover() == 1
    assert len(notices) == 1
    # a different report in between (state change) re-arms the alert
    rec.mcs_recovery_core._report("restore_consent_pending", "x")
    assert rec.recover() == 1
    assert len(notices) == 2
    # changed journal = new condition
    state["applying"]["tag"] = "v1.2.0"
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 1
    assert len(notices) == 3
    # unchanged but past the re-alert interval
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
    report["notified_at"] -= rec.mcs_recovery_core.ESCALATE_REALERT_S
    Path(rec.mcs_recovery_core.REPORT_PATH).write_text(json.dumps(report))
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
        assert recovery.mcs_recovery_core._alert_key(state, detail) \
            == mcs_update._alert_key(state, detail)
    assert recovery.mcs_recovery_core.ESCALATE_REALERT_S == mcs_update.ESCALATE_REALERT_S


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
        assert recovery.mcs_recovery_database._db_version(str(path)) == restore_evidence.database_version(str(path))
    valid = _applying("p" * 40)
    states = [valid, {"v": 1}, {"v": 1, "applying": None, "restore_consent": {}},
              None, [], {"v": 2}, {"v": True}, {"v": 1, "stages": {}},
              {"v": 1, "stages": [1]}, {"v": 1, "applying": "x"},
              {"v": 1, "executed": []}, {"v": 1, "stages": [{"stage": 1}]},
              {"v": 1, "applied": [{"at": -1}]}, {"v": 1, "applied": [{"at": "1"}]},
              {"v": 1, "stages": [{"stage": "x", "at": 1e12}]}]
    for state in states:
        assert recovery.mcs_recovery_core._valid_state(state) == mcs_update.journal_store.valid_state(state), state
    assert recovery.mcs_recovery_core._valid_state(valid) and not recovery.mcs_recovery_core._valid_state({"v": 2})


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
    assert rec.mcs_recovery_database._db_version(str(live)) == 7
    assert live.read_bytes() == back.read_bytes()
    state = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
    assert "cid-rb" not in state.get("executed", {})
    assert state["restore_consent"] and state["applying"]["restore_incomplete"] is True
    assert restarts == []
    assert Path(rec.mcs_recovery_core.MARKER_PATH).exists()
    marker = tmp_path / "data" / "restore_pending.json"
    assert json.loads(marker.read_text())["phase"] == "restored"
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
    assert report["result"] == "restore_reconcile_pending" and "writers_stopped" not in report
    held_db, held_state, held_marker = live.read_bytes(), Path(rec.mcs_recovery_core.STATE_PATH).read_bytes(), marker.read_bytes()
    assert rec.recover() == 1
    assert live.read_bytes() == held_db and Path(rec.mcs_recovery_core.STATE_PATH).read_bytes() == held_state
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
    state = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
    rid = state.pop("restore_consent")["report_id"]
    Path(rec.mcs_recovery_core.STATE_PATH).write_text(json.dumps(state))
    live_before = live.read_bytes()
    original_git = rec.mcs_recovery_core._git

    def broken(args, timeout=rec.mcs_recovery_core.T_GIT):
        return None if args[0] == "rev-parse" else original_git(args,
                                                                timeout)
    monkeypatch.setattr(rec.mcs_recovery_core, "_git", broken)
    assert rec.recover() == 1
    _assert_frozen(rec, tmp_path, live, live_before, restarts)
    hold = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())["restore_consent"]
    assert hold["from_marker"] is True and hold["report_id"] == rid
    monkeypatch.setattr(rec.mcs_recovery_core, "_git", original_git)
    _consent(rec, live, back)
    assert rec.recover() == 1  # consent authorizes copying, not notification reconciliation
    assert rec.mcs_recovery_database._db_version(str(live)) == 7
    assert live.read_bytes() == back.read_bytes()
    state = json.loads(Path(rec.mcs_recovery_core.STATE_PATH).read_text())
    assert "cid-rb" not in state.get("executed", {})
    assert state["restore_consent"] and state["applying"]["restore_incomplete"] is True
    assert restarts == [] and Path(rec.mcs_recovery_core.MARKER_PATH).exists()
    marker = tmp_path / "data" / "restore_pending.json"
    assert json.loads(marker.read_text())["phase"] == "restored"
    report = json.loads(Path(rec.mcs_recovery_core.REPORT_PATH).read_text())
    assert report["result"] == "restore_reconcile_pending" and "writers_stopped" not in report
    held_db, held_state, held_marker = live.read_bytes(), Path(rec.mcs_recovery_core.STATE_PATH).read_bytes(), marker.read_bytes()
    assert rec.recover() == 1
    assert live.read_bytes() == held_db and Path(rec.mcs_recovery_core.STATE_PATH).read_bytes() == held_state
    assert marker.read_bytes() == held_marker and restarts == []


@pytest.mark.parametrize("flag", ["--help", "-h", "--unknown"])
def test_cli_guidance_never_recovers(rec, monkeypatch, capsys, flag):
    monkeypatch.setattr(rec.sys, "argv", ["mcs_recover.py", flag])
    monkeypatch.setattr(rec, "recover", lambda **kw: pytest.fail("must not recover"))
    monkeypatch.setattr(rec.mcs_recovery_core, "_load_state", lambda: pytest.fail("must not read state"))
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
    monkeypatch.setattr(rec.mcs_recovery_core, "_load_state", lambda: state)
    monkeypatch.setattr(rec.mcs_recovery_core, "_head", lambda: "synthetic-sha")
    monkeypatch.setattr(rec.mcs_recovery_core, "_clean", lambda: True)
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
    monkeypatch.setattr(rec.mcs_recovery_services, "_restart_drainers",
                        lambda **k: calls.append(k.get("bounce", True)) or [])
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(_applying(prev, sha=target, stage="applying"), f)
    for _ in range(3):
        assert rec.recover() == 1
    assert calls == [True, False, False]
    assert (repo / "g.txt").read_text() == "MANUAL"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == target
