"""Synthetic mcs update apply contracts; shared helpers preserve isolated runtime state."""
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
from ops_testkit import in_process_post_merge
import restore_evidence
import service_management
import update_discovery
import json
import os
import time
from pathlib import Path
import pytest
import mcs_update
import mcs_util
from ops_testkit import _git, _make_repo, _mk_schema, _receipts_db
from mcs_update_testkit import _apply_env as _apply_env, _rec as _rec, prepared_run_log as prepared_run_log, updater as updater


@pytest.mark.parametrize("path,needs_restart", [
    ("hermes_plugin/card_workers.py", True),
    ("adapters/common/worker.py", True),
    ("adapters/slack/actions.py", True),
    ("adapters/discord/delivery.py", True),
    ("adapters/lineworks/actions.py", False),
    ("lineworks_adapter/__main__.py", False),
])
def test_apply_persists_adapter_restart_decision_before_restarting_gateway(
        updater, prepared_run_log, tmp_path, monkeypatch, path, needs_restart):
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
    monkeypatch.setattr(update_apply, '_run_post_merge', updater._run_post_merge)
    restarts = []

    def restart(cfg):
        saved = updater.journal_store.load_state(updater.STATE_PATH)
        assert saved["applying"] is None
        assert saved["applied"][-1]["plugin_changed"] is True
        assert saved["executed"]["cid-adapter"]["result"] == "applied"
        restarts.append(True)

    monkeypatch.setattr(updater, "restart_gateway", restart)
    monkeypatch.setattr(update_services, 'restart_gateway', updater.restart_gateway)
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
    monkeypatch.setattr(update_apply, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
    monkeypatch.setattr(update_recovery, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
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
    monkeypatch.setattr(update_apply, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', updater.LEDGER)
    monkeypatch.setattr(update_discovery, 'detect_latest', lambda *_a, **_k: (None, None))
    monkeypatch.setattr(update_discovery, 'current_version', lambda **_k: ('v1.0.0', 'b' * 40))
    monkeypatch.setattr(updater, '_apply_runtime_gate_errors', lambda *_a, **_k: [])
    monkeypatch.setattr(update_apply, '_apply_runtime_gate_errors', updater._apply_runtime_gate_errors)
    acquire = updater.acquire_update_lock
    calls = []

    def busy_after_scan():
        calls.append(True)
        return acquire() if len(calls) == 1 else None

    monkeypatch.setattr(updater, 'acquire_update_lock', busy_after_scan)
    monkeypatch.setattr(update_apply, 'acquire_update_lock', updater.acquire_update_lock)
    monkeypatch.setattr(update_cli, 'acquire_update_lock', updater.acquire_update_lock)
    monkeypatch.setattr(update_recovery, 'acquire_update_lock', updater.acquire_update_lock)
    assert updater.cmd_check() == 2
    state = updater.journal_store.load_state(updater.STATE_PATH)
    assert cid not in state.get('executed', {})
    assert [entry['command_id'] for entry in updater.scan_pending_approvals(state)[0]] == [cid]
    monkeypatch.setattr(updater, 'acquire_update_lock', acquire)
    monkeypatch.setattr(update_apply, 'acquire_update_lock', updater.acquire_update_lock)
    monkeypatch.setattr(update_cli, 'acquire_update_lock', updater.acquire_update_lock)
    monkeypatch.setattr(update_recovery, 'acquire_update_lock', updater.acquire_update_lock)
    seen = []
    monkeypatch.setattr(updater, 'apply', lambda *args: seen.append(args) or 0)
    monkeypatch.setattr(update_cli, 'apply', updater.apply)
    assert updater.cmd_check() == 0
    assert len(seen) == 1 and seen[0][2] == cid


def test_scheduled_check_skips_when_update_lock_is_busy(updater, tmp_path, monkeypatch):
    _apply_env(updater, monkeypatch, cfg={'update': {'mode': 'notify'}})
    monkeypatch.setattr(update_discovery, 'detect_latest', lambda *_a, **_k: (None, None))
    monkeypatch.setattr(update_discovery, 'current_version', lambda **_k: ('v1.0.0', 'b' * 40))
    monkeypatch.setattr(updater, 'scan_pending_approvals', lambda *_a: pytest.fail('must defer scan'))
    monkeypatch.setattr(update_cli, 'scan_pending_approvals', updater.scan_pending_approvals)
    before = updater.journal_store.load_state(updater.STATE_PATH)
    fd = updater.acquire_update_lock()
    try:
        assert updater.cmd_check() == 0
        assert updater.journal_store.load_state(updater.STATE_PATH) == before
    finally:
        os.close(fd)


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
    monkeypatch.setattr(update_apply, '_rollback_tree', mcs_update._rollback_tree)
    monkeypatch.setattr(update_recovery, '_rollback_tree', mcs_update._rollback_tree)
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda text, **k: notices.append(text) or True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)

    def recover():
        raise OSError("disk full")
    monkeypatch.setattr(mcs_update, "recover_interrupted", recover)
    monkeypatch.setattr(update_apply, 'recover_interrupted', mcs_update.recover_interrupted)
    monkeypatch.setattr(update_cli, 'recover_interrupted', mcs_update.recover_interrupted)
    monkeypatch.setattr(update_recovery, 'recover_interrupted', mcs_update.recover_interrupted)
    with pytest.raises(OSError, match="disk full"):
        updater.apply("v1.2.0", None, "mine")
    assert rolled == [] and notices == []
    assert Path(updater.STATE_PATH).read_bytes() == before
    fd = updater.acquire_update_lock()              # released
    assert fd is not None
    os.close(fd)


# A failed post-merge schema-bump rollback in apply()'s bail must stay
# recoverable: recovery retries the DB restore (or escalates) instead of
# 'finishing' as interrupted_pre_merge with old code on the newer DB.

def test_failed_bail_rollback_is_not_finished_as_pre_merge(
        updater, tmp_path, monkeypatch):  # noqa: F811
    import maintenance
    monkeypatch.setattr(updater, "_provider_migration_needed", lambda: False)
    monkeypatch.setattr(update_apply, '_provider_migration_needed', updater._provider_migration_needed)
    monkeypatch.setattr(update_recovery, '_provider_migration_needed', updater._provider_migration_needed)
    notices = []
    repo, _ = _make_repo(tmp_path)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
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
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    sha = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    live = str(tmp_path / "data" / "ledger.db")
    monkeypatch.setattr(mcs_update, "LEDGER", live)
    monkeypatch.setattr(update_apply, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_execution, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_lifecycle_context, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_planning, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_preflight, 'LEDGER', mcs_update.LEDGER)
    monkeypatch.setattr(update_restore, 'LEDGER', mcs_update.LEDGER)
    back = tmp_path / "data" / "backups" / "pre.db"
    back.parent.mkdir(parents=True, exist_ok=True)
    _mk_schema(back, 7, messages=2)
    _mk_schema(live, 8, messages=5)
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(mcs_update, "precheck_local", lambda c: [])
    monkeypatch.setattr(update_apply, 'precheck_local', mcs_update.precheck_local)
    monkeypatch.setattr(update_planning, 'precheck_local', mcs_update.precheck_local)
    monkeypatch.setattr(update_discovery, 'remote_tag_sha', lambda t, **_lif05_paths: sha)
    monkeypatch.setattr(mcs_update, "precheck_tag",
                        lambda t: ["schema_bump:7->8"])
    monkeypatch.setattr(update_execution, 'precheck_tag', mcs_update.precheck_tag)
    monkeypatch.setattr(update_planning, 'precheck_tag', mcs_update.precheck_tag)
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda p: str(back))
    monkeypatch.setattr(mcs_update, "_baseline_check", lambda c: [])
    monkeypatch.setattr(update_apply, '_baseline_check', mcs_update._baseline_check)
    monkeypatch.setattr(mcs_update, "quiesce",
                        lambda: (mcs_update._write_marker(), [])[1])
    monkeypatch.setattr(update_apply, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(mcs_update, "restart_agents",
                        lambda **k: (mcs_update._remove_marker(), [])[1])
    monkeypatch.setattr(update_apply, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(update_recovery, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_restore, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(update_runtime_migration, '_services_reconcile', mcs_update._services_reconcile)
    monkeypatch.setattr(service_management, 'retire_to_snapshot', lambda m, **_service_kwargs: [])
    monkeypatch.setattr(mcs_update, "_postcheck", lambda s, e: [])
    monkeypatch.setattr(update_recovery, '_postcheck', mcs_update._postcheck)
    monkeypatch.setattr(update_runtime_migration, '_postcheck', mcs_update._postcheck)
    monkeypatch.setattr(mcs_update, "_enqueue_notice",
                        lambda *a, **k: notices.append(a[0]) or True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(mcs_update, "restart_gateway", lambda c: None)
    monkeypatch.setattr(update_services, 'restart_gateway', mcs_update.restart_gateway)

    def post_merge(_sha, *, run_lock_fd):
        back.write_bytes(b"garbage")          # restore will fail
        raise mcs_update.UpdateError("post_merge_failed: synthetic")
    monkeypatch.setattr(mcs_update, "_run_post_merge", post_merge)
    monkeypatch.setattr(update_apply, '_run_post_merge', mcs_update._run_post_merge)

    assert mcs_update.apply("v1.1.0", sha, "cid") == 1
    # frozen DB (restore incomplete): report only, no outbox write
    assert notices == []
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] == "restore_consent_blocked"
    assert "writers_stopped" not in report
    assert Path(mcs_update.MARKER_PATH).exists()
    st = mcs_update.journal_store.load_state(mcs_update.STATE_PATH)
    assert st["applying"]["restore_incomplete"] is True
    assert st["executed"]["cid"]["result"] == "failed"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    # journal is rollback-shaped: target is prev, restore still owed
    assert st["applying"]["rollback"] is True
    assert st["applying"]["sha"] == before
    failed_receipt = dict(st["executed"]["cid"])

    mcs_update.recover_interrupted()
    assert notices == []
    st = mcs_update.journal_store.load_state(mcs_update.STATE_PATH)
    report = json.loads(Path(mcs_update.REPORT_PATH).read_text())
    assert report["result"] != "interrupted_pre_merge"
    assert report["result"] == "restore_consent_blocked"
    assert "rollback db restore" in report["detail"]
    assert st["applying"] is not None          # still visible to humans
    assert st["applying"]["restore_incomplete"] is True
    assert Path(mcs_update.MARKER_PATH).exists()
    assert "restore_consent" not in st  # A failed backup is not a pending human loss report.
    assert st["executed"]["cid"] == failed_receipt
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
    monkeypatch.setattr(update_apply, 'quiesce', updater.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', updater.quiesce)
    monkeypatch.setattr(updater, "_run_post_merge", restarted_child)
    monkeypatch.setattr(update_apply, '_run_post_merge', updater._run_post_merge)
    monkeypatch.setattr(updater, "_rollback_tree", restore)
    monkeypatch.setattr(update_apply, '_rollback_tree', updater._rollback_tree)
    monkeypatch.setattr(update_recovery, '_rollback_tree', updater._rollback_tree)
    monkeypatch.setattr(updater, "restart_agents", lambda: events.append("restart") or [])
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
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


@pytest.mark.parametrize("mode", ["hermes", "standalone"])
@pytest.mark.parametrize("blocked", ["provider", "runtime"])
@pytest.mark.parametrize("failure", ["none", "marker_before", "stop", "marker_after", "marker_swap"])
def test_post_merge_preflight_hold_reestablishes_verified_stop(updater, monkeypatch, mode, blocked, failure):
    repo = _apply_env(updater, monkeypatch, cfg={"runtime_mode": mode})
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    target = _git(repo, "rev-parse", "v1.1.0^{commit}").stdout.strip()
    monkeypatch.setattr(update_discovery, "remote_tag_sha", lambda tag, **_kwargs: target)
    monkeypatch.setattr(updater, "_plan_runtime_migration", lambda *_args: None)
    monkeypatch.setattr(update_apply, '_plan_runtime_migration', updater._plan_runtime_migration)
    monkeypatch.setattr(update_runtime_migration, '_plan_runtime_migration', updater._plan_runtime_migration)
    events, saved = [], {}
    restarted = [False]

    def load():
        if restarted[0]:
            raise ValueError("synthetic provider pending")
        return {"runtime_mode": mode}

    def initial_quiesce():
        events.append("initial_stop")
        updater._write_marker()
        return []

    def child(_sha, *, run_lock_fd):
        assert Path(updater.MARKER_PATH).exists()
        saved["journal"] = updater.journal_store.load_state(updater.STATE_PATH)
        updater._remove_marker()
        restarted[0] = True
        events.append("child_restarted")
        raise updater.UpdateError("post_merge_failed: postcheck_failed: synthetic")

    def preflight(_target):
        events.append("provider_preflight")
        if blocked == "provider":
            raise updater.ProviderRollbackUnverified("provider_rollback_settings_unverified")

    def runtime(_entry):
        events.append("runtime_preflight")
        if blocked == "runtime":
            raise updater.UpdateError("runtime_restore_checkout_changed")

    ensure = updater._ensure_recovery_marker
    def marker():
        events.append("marker")
        count = events.count("marker")
        if failure == ("marker_before" if count == 1 else "marker_after"):
            raise OSError("synthetic marker failure")
        return ensure()

    def stop(selected):
        assert selected == mode and Path(updater.MARKER_PATH).exists()
        events.append("stop:" + selected)
        if failure == "stop":
            raise updater.UpdateError("synthetic stop failure")
        if failure == "marker_swap":
            replacement = Path(updater.DATA) / "synthetic-marker-replacement"
            replacement.write_text("synthetic")
            replacement.chmod(0o600)
            os.replace(replacement, updater.MARKER_PATH)
        return []

    monkeypatch.setattr(mcs_util, "load_config", load)
    monkeypatch.setattr(updater, "quiesce", initial_quiesce)
    monkeypatch.setattr(update_apply, 'quiesce', updater.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', updater.quiesce)
    monkeypatch.setattr(updater, "_run_post_merge", child)
    monkeypatch.setattr(update_apply, '_run_post_merge', updater._run_post_merge)
    monkeypatch.setattr(updater, "_provider_rollback_preflight", preflight)
    monkeypatch.setattr(update_apply, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(update_recovery, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(update_restore, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(updater, "_check_runtime_restore", runtime)
    monkeypatch.setattr(update_apply, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(update_recovery, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(update_runtime_migration, '_check_runtime_restore', updater._check_runtime_restore)
    monkeypatch.setattr(updater, "_ensure_recovery_marker", marker)
    monkeypatch.setattr(update_apply, '_ensure_recovery_marker', updater._ensure_recovery_marker)
    monkeypatch.setattr(update_recovery, '_ensure_recovery_marker', updater._ensure_recovery_marker)
    monkeypatch.setattr(updater, "_quiesce_for_mode", stop)
    monkeypatch.setattr(update_apply, '_quiesce_for_mode', updater._quiesce_for_mode)
    monkeypatch.setattr(update_recovery, '_quiesce_for_mode', updater._quiesce_for_mode)
    monkeypatch.setattr(update_runtime_migration, '_quiesce_for_mode', updater._quiesce_for_mode)
    monkeypatch.setattr(update_services, '_quiesce_for_mode', updater._quiesce_for_mode)
    monkeypatch.setattr(updater, "_rollback_tree", lambda *_a, **_k: pytest.fail("unexpected rollback"))
    monkeypatch.setattr(update_apply, '_rollback_tree', updater._rollback_tree)
    monkeypatch.setattr(update_recovery, '_rollback_tree', updater._rollback_tree)
    monkeypatch.setattr(updater, "restart_agents", lambda *_a, **_k: pytest.fail("unexpected restart"))
    monkeypatch.setattr(update_apply, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', updater.restart_agents)
    monkeypatch.setattr(updater, "_enqueue_notice", lambda *_a, **_k: pytest.fail("unexpected notice"))
    monkeypatch.setattr(update_apply, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', updater._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', updater._enqueue_notice)

    assert updater.apply("v1.1.0", target, "synthetic-command") == 1
    assert updater._head_sha() == target
    state = updater.journal_store.load_state(updater.STATE_PATH)
    # The failed apply is durable even when rollback preflight holds before
    # command-receipt settlement. Every other journal field remains intact.
    attempt = state["attempts"].pop("v1.1.0")
    assert set(attempt) == {"at", "detail", "result"}
    assert attempt["result"] == "failed"
    assert attempt["detail"] == "post_merge_failed: postcheck_failed: synthetic"
    assert type(attempt["at"]) is float and attempt["at"] > 0
    assert state == saved["journal"]
    report = json.loads(Path(updater.REPORT_PATH).read_text())
    assert report["result"] == "runtime_restore_blocked"
    assert report["writers_stopped"] is (failure == "none")
    assert events[:2] == ["initial_stop", "child_restarted"]
    assert ("provider_preflight" in events) and ("runtime_preflight" in events) is (blocked == "runtime")
    assert events.count("marker") == (1 if failure in ("marker_before", "stop") else 2)
    assert ("stop:" + mode in events) is (failure != "marker_before")
    assert Path(updater.MARKER_PATH).exists() is (failure != "marker_before")


def test_manual_apply_accepts_different_args_before_busy_lock(updater, monkeypatch, capsys):
    config = Path(mcs_util.CONF_PATH)
    config.write_text('{"install_args": ["--no-llm"]}')
    monkeypatch.delenv('MCS_UPDATE_OPTIONS_EXPECTED', raising=False)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(updater, '_apply_runtime_gate_errors', lambda *_: [])
    monkeypatch.setattr(update_apply, '_apply_runtime_gate_errors', updater._apply_runtime_gate_errors)
    snapshot = updater._install_options_snapshot
    captured = []
    monkeypatch.setattr(updater, '_install_options_snapshot', lambda: captured.append(snapshot()) or captured[-1])
    monkeypatch.setattr(update_apply, '_install_options_snapshot', updater._install_options_snapshot)
    monkeypatch.setattr(update_runtime_migration, '_install_options_snapshot', updater._install_options_snapshot)
    monkeypatch.setattr(updater, 'acquire_update_lock', lambda: None)
    monkeypatch.setattr(update_apply, 'acquire_update_lock', updater.acquire_update_lock)
    monkeypatch.setattr(update_cli, 'acquire_update_lock', updater.acquire_update_lock)
    monkeypatch.setattr(update_recovery, 'acquire_update_lock', updater.acquire_update_lock)
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
    monkeypatch.setattr(update_apply, '_apply_runtime_gate_errors', updater._apply_runtime_gate_errors)
    monkeypatch.setattr(updater, '_install_options_snapshot', lambda: pytest.fail('recovery must take priority'))
    monkeypatch.setattr(update_apply, '_install_options_snapshot', updater._install_options_snapshot)
    monkeypatch.setattr(update_runtime_migration, '_install_options_snapshot', updater._install_options_snapshot)
    monkeypatch.setattr(updater, 'recover_interrupted', lambda: 7)
    monkeypatch.setattr(update_apply, 'recover_interrupted', updater.recover_interrupted)
    monkeypatch.setattr(update_cli, 'recover_interrupted', updater.recover_interrupted)
    monkeypatch.setattr(update_recovery, 'recover_interrupted', updater.recover_interrupted)
    assert updater._apply_pipeline('v1.0.23', None, None, reinstall=True) == 7
