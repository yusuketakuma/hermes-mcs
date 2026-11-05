"""Synthetic mcs update recovery contracts; shared helpers preserve isolated runtime state."""
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
import service_contract
import owned_process
import json
import os
import time
from pathlib import Path
import pytest
import mcs_update
import mcs_util
from ops_testkit import _git, _make_repo, _seed_consent
from mcs_update_testkit import _HANG_EXCS as _HANG_EXCS, _assert_old_schema_restore_held as _assert_old_schema_restore_held, _hang_git as _hang_git, _schema_bump_world as _schema_bump_world, prepared_run_log as prepared_run_log, updater as updater


# ---------------------------------------------------------------- recover

def test_recover_interrupted_pre_merge(updater, tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    mcs_update.REPO = str(repo)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
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
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
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
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
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
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
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
    monkeypatch.setattr(update_apply, 'REPO', updater.REPO)
    monkeypatch.setattr(update_cli, 'REPO', updater.REPO)
    monkeypatch.setattr(update_execution, 'REPO', updater.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', updater.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', updater.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', updater.REPO)
    monkeypatch.setattr(update_repository, 'REPO', updater.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', updater.REPO)
    monkeypatch.setattr(update_services, 'REPO', updater.REPO)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(updater, '_enqueue_notice', lambda *_a, **_k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', updater._enqueue_notice)
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
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
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
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
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
    monkeypatch.setattr(update_apply, '_local_changes_outside', updater._local_changes_outside)
    monkeypatch.setattr(update_recovery, '_local_changes_outside', updater._local_changes_outside)
    monkeypatch.setattr(update_restore, '_local_changes_outside', updater._local_changes_outside)
    monkeypatch.setattr(updater, "_git_out", observed_git_out)
    monkeypatch.setattr(update_apply, '_git_out', updater._git_out)
    monkeypatch.setattr(update_planning, '_git_out', updater._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', updater._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', updater._git_out)
    monkeypatch.setattr(update_repository, '_git_out', updater._git_out)
    monkeypatch.setattr(update_restore, '_git_out', updater._git_out)
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
    monkeypatch.setattr(update_recovery, '_clean_stale_git_locks', mcs_update._clean_stale_git_locks)
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait",
                        lambda **kw: None)      # busy — defer
    monkeypatch.setattr(update_apply, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
    monkeypatch.setattr(update_recovery, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
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
    monkeypatch.setattr(update_apply, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
    monkeypatch.setattr(update_recovery, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
    Path(mcs_update.MARKER_PATH).write_text(str(time.time()))
    assert updater.recover_interrupted() == 0
    assert not Path(mcs_update.MARKER_PATH).exists()


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
    monkeypatch.setattr(update_apply, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: pytest.fail("no outbox write"))
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
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


@pytest.mark.parametrize("exc", _HANG_EXCS)
def test_recover_escalates_when_services_reconcile_hangs(
        updater, prepared_run_log, tmp_path, monkeypatch, exc):
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
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
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


def test_recover_git_failure_escalates_outside_consent_hold(
        updater, tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
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
    restarts, notices = [], []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda: restarts.append(1) or [])
    monkeypatch.setattr(update_apply, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
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
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
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


# ------------------------------------------ escalation notice dedup

def test_recover_escalation_notifies_once_per_condition(
        updater, tmp_path, monkeypatch):
    """Daily check / consent respawn re-run recover on the same stuck
    journal: one notice per condition, again on change or after
    ESCALATE_REALERT_S; a failed enqueue is retried next pass."""
    repo, _ = _make_repo(tmp_path)
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
    monkeypatch.setattr(mcs_update, "restart_agents", lambda **k: [])
    monkeypatch.setattr(update_apply, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', mcs_update.restart_agents)
    notices, ok = [], [False]
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or ok[0])
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
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
    monkeypatch.setattr(update_apply, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_cli, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_execution, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_repository, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_services, 'REPO', mcs_update.REPO)
    calls = []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda **k: calls.append(k.get("bounce", True))
                        or [])
    monkeypatch.setattr(update_apply, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
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
    monkeypatch.setattr(update_apply, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_cli, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_execution, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_repository, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_services, 'REPO', mcs_update.REPO)
    calls = []
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda **k: calls.append(k.get("bounce", True))
                        or [])
    monkeypatch.setattr(update_apply, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(update_recovery, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(mcs_update, "_postcheck",
                        lambda s, t: ["version_mismatch"])
    monkeypatch.setattr(update_recovery, '_postcheck', mcs_update._postcheck)
    monkeypatch.setattr(update_runtime_migration, '_postcheck', mcs_update._postcheck)
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
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


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_recovery_marker_rejects_links_without_modifying_target(updater, tmp_path, kind):
    target = tmp_path / "synthetic-marker-target"
    target.write_text("synthetic unchanged")
    target.chmod(0o600)
    marker = Path(updater.MARKER_PATH)
    if kind == "symlink":
        marker.symlink_to(target)
    else:
        os.link(target, marker)
    with pytest.raises(OSError):
        updater._ensure_recovery_marker()
    assert target.read_text() == "synthetic unchanged"
    assert os.path.lexists(marker)


def test_recovery_marker_pin_survives_unlink_and_closes_both_fds(updater, monkeypatch):
    marker = Path(updater.MARKER_PATH)
    expected = updater._ensure_recovery_marker()
    opened = {}
    original_open = os.open

    def capture(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if path == str(marker.parent) and flags & os.O_DIRECTORY:
            opened["directory"] = fd
        elif path == marker.name and kwargs.get("dir_fd") == opened.get("directory"):
            opened["marker"] = fd
        return fd

    monkeypatch.setattr(updater.os, "open", capture)
    with pytest.raises(OSError, match="update_marker_unverifiable"):
        with updater._pin_recovery_marker(expected):
            assert (os.fstat(opened["marker"]).st_dev, os.fstat(opened["marker"]).st_ino) == expected
            assert os.fstat(opened["directory"]).st_ino == marker.parent.stat().st_ino
            marker.unlink()
            assert os.fstat(opened["marker"]).st_nlink == 0
            marker.write_text("synthetic replacement")
            marker.chmod(0o600)
            assert marker.stat().st_ino != expected[1]
            assert os.fstat(opened["marker"]).st_ino == expected[1]
    for fd in opened.values():
        with pytest.raises(OSError):
            os.fstat(fd)
    assert marker.read_text() == "synthetic replacement"
