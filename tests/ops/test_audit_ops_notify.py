"""Synthetic regressions: Discord guild scope, LINE WORKS callback order,
updater recovery keeps unrelated manual edits."""
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
import mcs_util
import service_contract
import asyncio
import json
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcs_update
import test_mcs_update
from adapters.lineworks import __main__ as cli
from adapters.lineworks import server
from hermes_plugin.card_workers import _interactive_settings
from lineworks_testkit import NOW, SECRET, event, raw, signature, world
from ops_testkit import _git, _load, _make_repo, patch_lifecycle_lookup

updater = test_mcs_update.updater   # shared fixture


class _Ctx:
    def __init__(self, values):
        self.values = values

    def get_config(self, key, default=None):
        return self.values.get(key, default)


def test_discord_cards_require_guild_id(tmp_path):
    values = {"interactive": True, "data_root": str(tmp_path), "channel_id": "42",
              "application_id": "999", "snapshot": str(tmp_path / "s.db"),
              "inbox": str(tmp_path / "cmd"), "allowed_user_ids": ["1"],
              "allowed_chat_ids": ["42"], "project_ids": [1]}
    # without the guild every delayed followup fails the origin check
    assert _interactive_settings(_Ctx(values)) is None
    values["guild_id"] = 7
    assert _interactive_settings(_Ctx(values))["guild_id"] == "7"


def test_lineworks_callback_is_handled_before_a_slow_delivery_tick(monkeypatch, tmp_path):
    w = world(tmp_path)
    clock = [NOW]
    box = server.CallbackInbox(Path(w.dirs["state"]) / "callbacks", w.settings,
                               SECRET, clock=lambda: clock[0])
    body = raw(event())
    assert box.accept(body, signature(body), w.settings["application_id"]) == 200
    pending = box.pending()[0]
    os.utime(pending, (NOW, NOW))
    observed, callbacks = [], {}

    async def noop():
        pass

    async def tick():
        clock[0] = NOW + 1300       # a 429 retry wait outlived the callback window
        observed.append("tick")
        callbacks[signal.SIGTERM]()

    async def handle(value):
        observed.append(value)

    worker = SimpleNamespace(acquire_scope_lock=lambda: True, reconcile=noop, tick=tick,
                             stop=lambda: None, release_scope_lock=lambda: None)
    http = SimpleNamespace(serve_forever=lambda: None, shutdown=lambda: None,
                           server_close=lambda: None)
    monkeypatch.setattr(cli, "settings", lambda _root: w.settings)
    monkeypatch.setattr(cli, "load_credentials", lambda *_: (w.client, SECRET))
    monkeypatch.setattr(cli, "DeliveryWorker", lambda **kw: worker)
    monkeypatch.setattr(cli, "CallbackInbox", lambda *_: box)
    monkeypatch.setattr(cli, "Actions", lambda *_: SimpleNamespace(
        handle=handle, sweep_followups=noop))
    monkeypatch.setattr(cli, "callback_server", lambda *a, **kw: http)
    monkeypatch.setattr(cli, "threading", SimpleNamespace(
        Thread=lambda **kw: SimpleNamespace(start=lambda: None, join=lambda **k: None)))

    async def run():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: callbacks.__setitem__(sig, cb))
        monkeypatch.setattr(loop, "remove_signal_handler", lambda _sig: True)
        await asyncio.wait_for(cli.serve(tmp_path, 8788), timeout=1)

    asyncio.run(run())
    assert observed == [event(), "tick"]
    assert json.loads(pending.with_suffix(".done").read_text()) == {"result": "processed"}


@pytest.mark.parametrize("at", ["prev", "target"])
@pytest.mark.parametrize("dirty", ["update_path", "unrelated_path", "index_only",
                                  "third_worktree", "third_staged", "third_index_only"])
def test_updater_recovery_resets_only_update_paths(updater, tmp_path, monkeypatch, dirty, at):
    repo, bare = _make_repo(tmp_path)
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
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(mcs_update, "restart_agents", lambda **k: [])
    monkeypatch.setattr(update_apply, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(mcs_update, "restart_services", lambda *a, **k: None)
    monkeypatch.setattr(update_apply, 'restart_services', mcs_update.restart_services)
    monkeypatch.setattr(update_recovery, 'restart_services', mcs_update.restart_services)
    monkeypatch.setattr(update_services, 'restart_services', mcs_update.restart_services)
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(update_recovery, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(mcs_update, "_postcheck", lambda *a: [])
    monkeypatch.setattr(update_recovery, '_postcheck', mcs_update._postcheck)
    monkeypatch.setattr(update_runtime_migration, '_postcheck', mcs_update._postcheck)
    work = tmp_path / "remote-work"
    (work / "g.txt").write_text("operator")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "c3")
    _git(work, "tag", "v1.1.5")
    (work / "f.txt").write_text("three")
    _git(work, "commit", "-qam", "c4")
    _git(work, "tag", "v1.2.0")
    _git(work, "push", "-q", str(bare), "main", "v1.1.5", "v1.2.0")
    _git(repo, "fetch", "-q", "--tags")
    _git(repo, "checkout", "-q", "v1.1.5")
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    target = _git(repo, "rev-list", "-n1", "v1.2.0").stdout.strip()
    head = prev if at == "prev" else target
    _git(repo, "checkout", "-q", "--detach", head)
    path = "f.txt" if dirty == "update_path" or dirty.startswith("third_") else "g.txt"
    original = (repo / path).read_text()
    # A mixed checkout contains known versions; a third edit has no such provenance.
    changed = ("three" if at == "prev" else "two") if dirty == "update_path" else "MANUAL THIRD CONTENT"
    (repo / path).write_text(changed)
    if dirty in ("index_only", "third_staged", "third_index_only"):
        _git(repo, "add", path)
    if dirty in ("index_only", "third_index_only"):
        (repo / path).write_text(original)
    state = updater.journal_store.default_state()
    state["applying"] = {"tag": "v1.2.0", "sha": target, "prev_sha": prev,
                         "at": time.time() - 4000}
    state["stages"] = [{"stage": "merge", "at": time.time() - 3000}]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    journal = Path(updater.STATE_PATH).read_bytes()
    worktree = (repo / path).read_bytes()
    index = _git(repo, "show", f":{path}").stdout
    if dirty == "update_path":
        assert updater.recover_interrupted() == 0
        assert (repo / "f.txt").read_text() == ("two" if at == "prev" else "three")
        assert _git(repo, "show", ":f.txt").stdout == ("two" if at == "prev" else "three")
    else:
        assert updater.recover_interrupted() == 1
        assert (repo / path).read_bytes() == worktree
        assert _git(repo, "show", f":{path}").stdout == index
        assert Path(updater.STATE_PATH).read_bytes() == journal
        report = json.loads(Path(updater.REPORT_PATH).read_text())
        assert report["detail"] == "local changes outside update: " + path
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head


def _provenance_repo(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _git(repo, "config", "core.filemode", "true")
    for name in ("untouched.txt", "deleted.txt", "rename-from.txt"):
        (repo / name).write_text(name)
    (repo / "f.txt").chmod(0o644)
    (repo / ".gitignore").write_text("ignored.txt\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "previous tree")
    prev = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "f.txt").write_text("three")
    (repo / "added.txt").write_text("known addition")
    (repo / "empty-added.txt").touch()
    (repo / "deleted.txt").unlink()
    (repo / "rename-from.txt").rename(repo / "rename-to.txt")
    (repo / "nested").mkdir()
    (repo / "nested/target.txt").write_text("known nested addition")
    (repo / "ignored.txt").write_text("known ignored addition")
    _git(repo, "add", "-f", "ignored.txt")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "target tree")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, prev, target


def _provenance_snapshot(repo):
    files = {}
    for path in repo.rglob("*"):
        relative = path.relative_to(repo)
        if relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            files[str(relative)] = ("symlink", os.readlink(path))
        elif path.is_dir():
            files[str(relative)] = ("directory", path.stat().st_mode & 0o777)
        elif path.is_file():
            files[str(relative)] = (path.read_bytes(), path.stat().st_mode & 0o777)
    return (_git(repo, "rev-parse", "HEAD").stdout,
            _git(repo, "ls-files", "--stage", "-v").stdout, files)


@pytest.mark.parametrize("helper", ["updater", "watchdog"])
@pytest.mark.parametrize("case", [
    "added_untracked", "deleted_recreated", "rename_source_recreated", "ignored_collision",
    "directory_prefix_collision", "filemode_disabled", "assume_unchanged", "assume_unchanged_known",
    "skip_worktree", "skip_worktree_known", "intent_to_add", "control_path",
    "unrelated_untracked", "known_mixed_versions", "known_absences",
])
def test_recovery_provenance_checks_filesystem_not_only_git_status(
        updater, tmp_path, monkeypatch, helper, case):
    repo, prev, target = _provenance_repo(tmp_path)
    at_target = case in ("deleted_recreated", "rename_source_recreated", "filemode_disabled")
    _git(repo, "checkout", "-q", "--detach", target if at_target else prev)
    protected = None
    unknown = case in ("assume_unchanged", "assume_unchanged_known", "skip_worktree",
                       "skip_worktree_known", "intent_to_add", "control_path")
    if case in ("added_untracked", "deleted_recreated", "rename_source_recreated", "ignored_collision"):
        protected = {"added_untracked": "added.txt", "deleted_recreated": "deleted.txt",
                     "rename_source_recreated": "rename-from.txt", "ignored_collision": "ignored.txt"}[case]
        (repo / protected).write_text("MANUAL THIRD CONTENT")
        if case == "ignored_collision":
            assert _git(repo, "check-ignore", "ignored.txt").returncode == 0
    elif case == "directory_prefix_collision":
        (repo / "nested").write_text("MANUAL PREFIX FILE")
    elif case == "filemode_disabled":
        protected = "f.txt"
        _git(repo, "config", "core.filemode", "false")
        (repo / protected).chmod(0o755)
        assert _git(repo, "status", "--porcelain").stdout == ""
    elif case.startswith(("assume_unchanged", "skip_worktree")):
        protected = "untouched.txt"
        assume = case.startswith("assume_unchanged")
        _git(repo, "update-index", "--assume-unchanged" if assume else "--skip-worktree", protected)
        if not case.endswith("_known"):
            (repo / protected).write_text("MANUAL HIDDEN CONTENT")
        (repo / "f.txt").write_text("three")
        assert _git(repo, "ls-files", "-v", protected).stdout.startswith("h " if assume else "S ")
        assert protected not in _git(repo, "diff", "--name-only").stdout
        assert "f.txt" in _git(repo, "diff", "--name-only").stdout
    elif case == "intent_to_add":
        (repo / "empty-added.txt").touch()
        _git(repo, "add", "-N", "empty-added.txt")
        staged = _git(repo, "ls-files", "--stage", "empty-added.txt").stdout.split()
        assert staged[:2] == ["100644", _git(repo, "rev-parse", f"{target}:empty-added.txt").stdout.strip()]
    elif case == "control_path":
        name = "control\twith\rcarriage\nnewline.txt"
        (repo / name).write_text("MANUAL CONTROL PATH")
        _git(repo, "add", name)
    elif case == "unrelated_untracked":
        (repo / "operator-notes.txt").write_text("keep these unrelated notes")
    elif case == "known_mixed_versions":
        (repo / "f.txt").write_text("three")
        _git(repo, "add", "f.txt")
        (repo / "f.txt").write_text("two")
    else:
        (repo / "added.txt").write_text("known addition")
        (repo / "deleted.txt").unlink()

    tool = updater if helper == "updater" else _load()
    patch_lifecycle_lookup(monkeypatch, tool, "REPO", str(repo))
    before = _provenance_snapshot(repo)
    refused = False
    try:
        foreign = tool._local_changes_outside(target, prev) if at_target else tool._local_changes_outside(prev, target)
    except updater.UpdateError:
        assert helper == "updater" and (unknown or case == "directory_prefix_collision")
        foreign = None
        refused = True
    if unknown:
        assert foreign is None
        assert helper == "watchdog" or refused
    elif protected is not None:
        assert foreign is not None and protected in foreign
    elif case == "directory_prefix_collision":
        assert foreign is None or any(path == "nested" or path.startswith("nested/") for path in foreign)
    else:
        assert foreign == []
    assert _provenance_snapshot(repo) == before


def test_rollback_preserves_third_content_before_restoring_runtime(updater, tmp_path, monkeypatch):
    repo, prev, target = _provenance_repo(tmp_path)
    _git(repo, "checkout", "-q", "--detach", target)
    (repo / "f.txt").write_text("MANUAL THIRD CONTENT")
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
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {"runtime_mode": "hermes"})
    monkeypatch.setattr(updater, "_rollback_layout_error", lambda entry: None)
    monkeypatch.setattr(update_apply, '_rollback_layout_error', updater._rollback_layout_error)
    monkeypatch.setattr(update_restore, '_rollback_layout_error', updater._rollback_layout_error)
    monkeypatch.setattr(updater, "_restore_runtime", lambda entry: pytest.fail("runtime restore before refusal"))
    monkeypatch.setattr(update_recovery, '_restore_runtime', updater._restore_runtime)
    monkeypatch.setattr(update_restore, '_restore_runtime', updater._restore_runtime)
    git = updater._git

    def no_reset(args, *positional, **kwargs):
        assert args[0] != "reset", "reset before refusing third content"
        return git(args, *positional, **kwargs)

    monkeypatch.setattr(updater, "_git", no_reset)
    monkeypatch.setattr(update_apply, '_git', updater._git)
    monkeypatch.setattr(update_cli, '_git', updater._git)
    monkeypatch.setattr(update_planning, '_git', updater._git)
    monkeypatch.setattr(update_preflight, '_git', updater._git)
    monkeypatch.setattr(update_recovery, '_git', updater._git)
    monkeypatch.setattr(update_repository, '_git', updater._git)
    monkeypatch.setattr(update_restore, '_git', updater._git)
    monkeypatch.setattr(update_runtime_migration, '_git', updater._git)
    before = _provenance_snapshot(repo)
    with pytest.raises(updater.UpdateError) as stopped:
        updater._rollback_tree({"prev_sha": prev, "sha": target})
    assert "f.txt" in str(stopped.value)
    assert _provenance_snapshot(repo) == before


@pytest.mark.parametrize("helper", ["updater", "watchdog"])
def test_recovery_keeps_a_dangling_symlink_without_following_it(
        updater, tmp_path, monkeypatch, helper):
    repo, prev, target = _provenance_repo(tmp_path)
    _git(repo, "checkout", "-q", "--detach", prev)
    path = repo / "f.txt"
    path.unlink()
    path.symlink_to("missing-target")
    tool = updater if helper == "updater" else _load()
    patch_lifecycle_lookup(monkeypatch, tool, "REPO", str(repo))
    index = _git(repo, "ls-files", "--stage", "-v").stdout

    assert tool._local_changes_outside(prev, target) == ["f.txt"]

    assert path.is_symlink() and os.readlink(path) == "missing-target"
    assert not (repo / "missing-target").exists()
    assert _git(repo, "ls-files", "--stage", "-v").stdout == index


def _file_directory_repo(tmp_path):
    repo, _ = _make_repo(tmp_path)
    (repo / ".gitignore").write_text("shape/ignored.txt\n")
    (repo / "shape").write_text("known file endpoint")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "file endpoint")
    file_commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "shape").unlink()
    (repo / "shape").mkdir()
    (repo / "shape/known.txt").write_text("known directory endpoint")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "directory endpoint")
    directory_commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, file_commit, directory_commit


@pytest.mark.parametrize("helper", ["updater", "watchdog"])
@pytest.mark.parametrize("direction", ["file_to_directory", "directory_to_file"])
@pytest.mark.parametrize("at", ["source", "destination"])
def test_recovery_allows_known_file_directory_transitions(
        updater, tmp_path, monkeypatch, helper, direction, at):
    repo, file_commit, directory_commit = _file_directory_repo(tmp_path)
    origin, destination = ((file_commit, directory_commit) if direction == "file_to_directory"
                           else (directory_commit, file_commit))
    _git(repo, "checkout", "-q", "--detach", origin if at == "source" else destination)
    tool = updater if helper == "updater" else _load()
    patch_lifecycle_lookup(monkeypatch, tool, "REPO", str(repo))
    before = _provenance_snapshot(repo)
    try:
        assert tool._local_changes_outside(origin, destination) == []
    finally:
        assert _provenance_snapshot(repo) == before


@pytest.mark.parametrize("helper", ["updater", "watchdog"])
@pytest.mark.parametrize("collision", [
    "tracked_third", "untracked_child", "ignored_child", "nested_empty_directory", "symlink_ancestor",
])
def test_file_directory_reset_preserves_unknown_children(
        updater, tmp_path, monkeypatch, helper, collision):
    repo, file_commit, directory_commit = _file_directory_repo(tmp_path)
    private = None
    if collision == "tracked_third":
        (repo / "shape/known.txt").write_text("MANUAL THIRD CONTENT")
    elif collision in ("untracked_child", "ignored_child"):
        name = "ignored.txt" if collision == "ignored_child" else "operator-notes.txt"
        (repo / "shape" / name).write_text("MANUAL CHILD CONTENT")
        if collision == "ignored_child":
            assert _git(repo, "check-ignore", "shape/ignored.txt").returncode == 0
    elif collision == "nested_empty_directory":
        (repo / "shape/operator/empty").mkdir(parents=True)
    else:
        private = tmp_path / "synthetic-private-target"
        private.mkdir()
        (private / "known.txt").write_text("SYNTHETIC PRIVATE CONTENT")
        (repo / "shape/known.txt").unlink()
        (repo / "shape").rmdir()
        (repo / "shape").symlink_to(private, target_is_directory=True)
    tool = updater if helper == "updater" else _load()
    patch_lifecycle_lookup(monkeypatch, tool, "REPO", str(repo))
    before = _provenance_snapshot(repo)
    private_before = ((private / "known.txt").read_bytes() if private is not None else None)
    try:
        foreign = tool._local_changes_outside(directory_commit, file_commit)
    except updater.UpdateError:
        assert helper == "updater"
        foreign = None
    assert foreign is None or foreign
    assert _provenance_snapshot(repo) == before
    if private is not None:
        assert (repo / "shape").is_symlink()
        assert os.readlink(repo / "shape") == str(private)
        assert (private / "known.txt").read_bytes() == private_before


def test_rollback_untracked_collision_refuses_before_journal_and_recovery_is_noop(
        updater, tmp_path, monkeypatch):
    repo, prev, target = _provenance_repo(tmp_path)
    (repo / "deleted.txt").write_text("UNTRACKED OPERATOR CONTENT")
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
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {"runtime_mode": "hermes"})
    events, notices = [], []
    monkeypatch.setattr(updater, "quiesce", lambda: events.append("quiesce") or [])
    monkeypatch.setattr(update_apply, 'quiesce', updater.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', updater.quiesce)
    monkeypatch.setattr(updater, "restart_agents", lambda **k: events.append("restart") or [])
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(updater, "restart_services", lambda *a, **k: events.append("services"))
    monkeypatch.setattr(update_apply, 'restart_services', updater.restart_services)
    monkeypatch.setattr(update_recovery, 'restart_services', updater.restart_services)
    monkeypatch.setattr(update_services, 'restart_services', updater.restart_services)
    monkeypatch.setattr(updater, "_restore_runtime", lambda entry: events.append("runtime"))
    monkeypatch.setattr(update_recovery, '_restore_runtime', updater._restore_runtime)
    monkeypatch.setattr(update_restore, '_restore_runtime', updater._restore_runtime)
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text, **k: notices.append(text) or True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', updater._enqueue_notice)
    entry = {"tag": "v1.0.26", "sha": target, "prev_sha": prev, "at": time.time() - 4000}
    state = updater.journal_store.default_state()
    state["applied"] = [entry]
    updater.journal_store.save_state(updater.STATE_PATH, state)
    assert updater._tree_clean()
    before = _provenance_snapshot(repo)
    cid = "synthetic-untracked-rollback"

    assert updater.rollback(cid) == 1
    refused = updater.journal_store.load_state(updater.STATE_PATH)
    events_after_refusal = list(events)
    notice_count = len(notices)
    journal = Path(updater.STATE_PATH).read_bytes()
    assert updater.recover_interrupted() == 0
    recovered = updater.journal_store.load_state(updater.STATE_PATH)

    assert refused["applied"] == recovered["applied"] == [entry]
    assert _provenance_snapshot(repo) == before
    assert {
        "applying": refused["applying"], "stages": refused["stages"],
        "events_after_refusal": events_after_refusal, "events_after_recovery": events,
        "receipt_after_refusal": refused["executed"][cid]["result"],
        "receipt_after_recovery": recovered["executed"][cid]["result"],
        "recovery_notices": notices[notice_count:],
    } == {
        "applying": None, "stages": [],
        "events_after_refusal": [], "events_after_recovery": [],
        "receipt_after_refusal": "rollback_failed", "receipt_after_recovery": "rollback_failed",
        "recovery_notices": [],
    }
    assert Path(updater.STATE_PATH).read_bytes() == journal
