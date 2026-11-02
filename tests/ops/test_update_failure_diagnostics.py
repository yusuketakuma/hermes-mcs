"""Apply errors survive recovery holds without exposing child output or restarting."""
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
        monkeypatch.setattr(updater, name, forbidden)
    monkeypatch.setattr(updater.mcs_util, "load_config", lambda: {"runtime_mode": "standalone"})
    monkeypatch.setattr(updater, "_apply_validate_args", lambda *_a, **_k: None)
    monkeypatch.setattr(updater, "_candidate_commit", lambda *_a: None)
    monkeypatch.setattr(updater, "_apply_runtime_gate_errors", lambda *_a: [])
    monkeypatch.setattr(updater, "_install_options_snapshot", lambda: (None, None))
    monkeypatch.setattr(updater, "_release_legacy_hold", lambda *_a: False)
    monkeypatch.setattr(updater, "precheck_local", lambda *_a, **_k: [])
    monkeypatch.setattr(updater, "_head_sha", lambda **_k: target if merged else previous)
    monkeypatch.setattr(updater, "_apply_tag_gate", lambda *_a: (None, []))
    import maintenance
    monkeypatch.setattr(maintenance, "preupdate_backup", lambda *_a, **_k: None)
    monkeypatch.setattr(updater, "_read_manifest_snapshot", lambda **_k: {})
    monkeypatch.setattr(updater, "_baseline_check", lambda *_a, **_k: {})
    monkeypatch.setattr(updater, "_tree_clean", lambda **_k: True)
    monkeypatch.setattr(updater.update_migrations, "apply", lambda *_a: False)
    monkeypatch.setattr(updater, "_provider_migration_needed", lambda: hold == "provider_migration")
    monkeypatch.setattr(updater, "_plugin_changed", lambda *_a, **_k: False)
    monkeypatch.setattr(updater, "quiesce", lambda: None)
    monkeypatch.setattr(updater, "_plan_runtime_migration", lambda *_a: None)

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
    monkeypatch.setattr(updater, "_run_post_merge", post_merge)
    monkeypatch.setattr(updater, "_provider_rollback_preflight", deny_restore)
    monkeypatch.setattr(updater, "_ensure_recovery_marker", lambda: "synthetic-marker")
    monkeypatch.setattr(updater, "_pin_recovery_marker", lambda *_a: nullcontext())
    monkeypatch.setattr(updater, "_quiesce_for_mode", stop)
    # Exercise the provider hold's report without probing any real configuration.
    def provider_hold(**_kwargs):
        updater._report("provider_migration_recovery_blocked", "provider migration is not verified",
                        writers_stopped=True)
        return 1
    monkeypatch.setattr(updater, "_provider_migration_recovery_hold", provider_hold)
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
