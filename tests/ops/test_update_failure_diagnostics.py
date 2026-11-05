"""Apply errors survive recovery holds without exposing child output or restarting."""
from ops_testkit import patch_lifecycle_lookup
import update_apply
import update_cli
import update_execution
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import update_services
import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from test_mcs_update import updater  # noqa: F401


@pytest.mark.parametrize("hold", ["runtime", "stop_unverified", "provider_migration", "journal_unwritable"])
def test_post_merge_failure_survives_early_recovery_hold(
        updater, monkeypatch, capsys, hold):  # noqa: F811
    previous, target = "a" * 40, "b" * 40
    failure = "post_merge_failed: SYNTHETIC_PRIVATE_CHILD_DIAGNOSTIC"
    merged = False
    applying_at_failure = []

    def forbidden(*_args, **_kwargs):
        pytest.fail("recovery hold must not reset, restart, notify, or remove marker")

    for name in ("_rollback_tree", "restart_agents", "restart_services", "_enqueue_notice",
                 "_remove_marker"):
        patch_lifecycle_lookup(monkeypatch, updater, name, forbidden)
    monkeypatch.setattr(updater.mcs_util, "load_config", lambda: {"runtime_mode": "standalone"})
    monkeypatch.setattr(updater, "_apply_validate_args", lambda *_a, **_k: None)
    monkeypatch.setattr(update_apply, '_apply_validate_args', updater._apply_validate_args)
    monkeypatch.setattr(updater, "_candidate_commit", lambda *_a: None)
    monkeypatch.setattr(update_apply, '_candidate_commit', updater._candidate_commit)
    monkeypatch.setattr(update_planning, '_candidate_commit', updater._candidate_commit)
    monkeypatch.setattr(updater, "_apply_runtime_gate_errors", lambda *_a: [])
    monkeypatch.setattr(update_apply, '_apply_runtime_gate_errors', updater._apply_runtime_gate_errors)
    monkeypatch.setattr(updater, "_install_options_snapshot", lambda: (None, None))
    monkeypatch.setattr(update_apply, '_install_options_snapshot', updater._install_options_snapshot)
    monkeypatch.setattr(update_runtime_migration, '_install_options_snapshot', updater._install_options_snapshot)
    monkeypatch.setattr(updater, "_release_legacy_hold", lambda *_a: False)
    monkeypatch.setattr(update_apply, '_release_legacy_hold', updater._release_legacy_hold)
    monkeypatch.setattr(update_cli, '_release_legacy_hold', updater._release_legacy_hold)
    monkeypatch.setattr(update_recovery, '_release_legacy_hold', updater._release_legacy_hold)
    monkeypatch.setattr(updater, "precheck_local", lambda *_a, **_k: [])
    monkeypatch.setattr(update_apply, 'precheck_local', updater.precheck_local)
    monkeypatch.setattr(update_planning, 'precheck_local', updater.precheck_local)
    monkeypatch.setattr(updater, "_head_sha", lambda **_k: target if merged else previous)
    monkeypatch.setattr(update_apply, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_execution, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_recovery, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_repository, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_restore, '_head_sha', updater._head_sha)
    monkeypatch.setattr(update_runtime_migration, '_head_sha', updater._head_sha)
    monkeypatch.setattr(updater, "_apply_tag_gate", lambda *_a: (None, []))
    monkeypatch.setattr(update_apply, '_apply_tag_gate', updater._apply_tag_gate)
    import maintenance
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda *_a, **_k: None)
    monkeypatch.setattr(updater, "_read_manifest_snapshot", lambda **_k: {})
    monkeypatch.setattr(update_apply, '_read_manifest_snapshot', updater._read_manifest_snapshot)
    monkeypatch.setattr(updater, "_baseline_check", lambda *_a, **_k: {})
    monkeypatch.setattr(update_apply, '_baseline_check', updater._baseline_check)
    monkeypatch.setattr(updater, "_tree_clean", lambda **_k: True)
    monkeypatch.setattr(update_apply, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_execution, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_preflight, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_recovery, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_restore, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_runtime_migration, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(updater.update_migrations, "apply", lambda *_a: False)
    monkeypatch.setattr(updater, "_provider_migration_needed", lambda: hold == "provider_migration")
    monkeypatch.setattr(update_apply, '_provider_migration_needed', updater._provider_migration_needed)
    monkeypatch.setattr(update_recovery, '_provider_migration_needed', updater._provider_migration_needed)
    monkeypatch.setattr(updater, "_plugin_changed", lambda *_a, **_k: False)
    monkeypatch.setattr(update_apply, '_plugin_changed', updater._plugin_changed)
    monkeypatch.setattr(update_planning, '_plugin_changed', updater._plugin_changed)
    monkeypatch.setattr(updater, "quiesce", lambda: None)
    monkeypatch.setattr(update_apply, 'quiesce', updater.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', updater.quiesce)
    monkeypatch.setattr(updater, "_plan_runtime_migration", lambda *_a: None)
    monkeypatch.setattr(update_apply, '_plan_runtime_migration', updater._plan_runtime_migration)
    monkeypatch.setattr(update_runtime_migration, '_plan_runtime_migration', updater._plan_runtime_migration)

    def merge(*_args, **_kwargs):
        nonlocal merged
        merged = True
        return SimpleNamespace(returncode=0)

    def post_merge(*_args, **_kwargs):
        current = updater.journal_store.load_state(updater.STATE_PATH)
        # The child can advance beyond the parent's post_merge snapshot.
        current["stages"].append({"stage": "reinstall", "at": 123})
        current["applying"]["runtime_snapshot"] = {"synthetic": True}
        updater.journal_store.save_state(updater.STATE_PATH, current)
        applying_at_failure.append(copy.deepcopy(current["applying"]))
        applying_at_failure.append(copy.deepcopy(current["stages"]))
        raise updater.UpdateError(failure)

    def deny_restore(*_args):
        raise updater.UpdateError("provider_rollback_target_unverified")

    def stop(*_args):
        if hold == "stop_unverified":
            raise OSError("SYNTHETIC_PRIVATE_STOP_DIAGNOSTIC")

    monkeypatch.setattr(updater, "_git", merge)
    monkeypatch.setattr(update_apply, '_git', updater._git)
    monkeypatch.setattr(update_cli, '_git', updater._git)
    monkeypatch.setattr(update_planning, '_git', updater._git)
    monkeypatch.setattr(update_preflight, '_git', updater._git)
    monkeypatch.setattr(update_recovery, '_git', updater._git)
    monkeypatch.setattr(update_repository, '_git', updater._git)
    monkeypatch.setattr(update_restore, '_git', updater._git)
    monkeypatch.setattr(update_runtime_migration, '_git', updater._git)
    monkeypatch.setattr(updater, "_run_post_merge", post_merge)
    monkeypatch.setattr(update_apply, '_run_post_merge', updater._run_post_merge)
    monkeypatch.setattr(updater, "_provider_rollback_preflight", deny_restore)
    monkeypatch.setattr(update_apply, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(update_recovery, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(update_restore, '_provider_rollback_preflight', updater._provider_rollback_preflight)
    monkeypatch.setattr(updater, "_ensure_recovery_marker", lambda: "synthetic-marker")
    monkeypatch.setattr(update_apply, '_ensure_recovery_marker', updater._ensure_recovery_marker)
    monkeypatch.setattr(update_recovery, '_ensure_recovery_marker', updater._ensure_recovery_marker)
    monkeypatch.setattr(updater, "_pin_recovery_marker", lambda *_a: nullcontext())
    monkeypatch.setattr(update_apply, '_pin_recovery_marker', updater._pin_recovery_marker)
    monkeypatch.setattr(update_recovery, '_pin_recovery_marker', updater._pin_recovery_marker)
    monkeypatch.setattr(update_runtime_migration, '_pin_recovery_marker', updater._pin_recovery_marker)
    monkeypatch.setattr(updater, "_quiesce_for_mode", stop)
    monkeypatch.setattr(update_apply, '_quiesce_for_mode', updater._quiesce_for_mode)
    monkeypatch.setattr(update_recovery, '_quiesce_for_mode', updater._quiesce_for_mode)
    monkeypatch.setattr(update_runtime_migration, '_quiesce_for_mode', updater._quiesce_for_mode)
    monkeypatch.setattr(update_services, '_quiesce_for_mode', updater._quiesce_for_mode)
    # Exercise the provider hold's report without probing any real configuration.
    def provider_hold(**_kwargs):
        updater._report("provider_migration_recovery_blocked", "provider migration is not verified",
                        writers_stopped=True)
        return 1
    monkeypatch.setattr(updater, "_provider_migration_recovery_hold", provider_hold)
    monkeypatch.setattr(update_apply, '_provider_migration_recovery_hold', updater._provider_migration_recovery_hold)
    monkeypatch.setattr(update_recovery, '_provider_migration_recovery_hold', updater._provider_migration_recovery_hold)
    if hold == "journal_unwritable":
        save = updater.journal_store.save_state

        def refuse_diagnostic(path, state):
            if state.get("attempts"):
                raise OSError("synthetic journal write failure")
            return save(path, state)

        monkeypatch.setattr(updater.journal_store, "save_state", refuse_diagnostic)

    def apply():
        return updater._apply_pipeline(None, None, "synthetic-command", base_sha=previous,
                                       candidate_sha=target, confirm_human=True,
                                       reason="synthetic approval")

    if hold == "journal_unwritable":
        with pytest.raises(OSError, match="synthetic journal write failure"):
            apply()
    else:
        assert apply() == 1
    saved = updater.journal_store.load_state(updater.STATE_PATH)
    assert saved["applying"] == applying_at_failure[0]
    assert saved["stages"] == applying_at_failure[1]
    if hold == "journal_unwritable":
        assert not saved.get("attempts")
        return
    assert saved["attempts"]["candidate:" + target]["detail"] == failure
    assert saved["attempts"]["candidate:" + target]["result"] == "failed"
    assert "synthetic-command" not in saved.get("executed", {})
    with open(updater.REPORT_PATH) as stream:
        report = json.load(stream)
    assert report["writers_stopped"] is (hold != "stop_unverified")
    if hold == "provider_migration":
        assert report["result"] == "provider_migration_recovery_blocked"
    else:
        assert report["result"] == "runtime_restore_blocked"
        assert report["apply_failure"] == failure
        assert report["detail"].startswith("provider_rollback_target_unverified")
    output = capsys.readouterr()
    assert "SYNTHETIC_PRIVATE" not in output.out + output.err
