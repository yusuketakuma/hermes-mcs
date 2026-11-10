"""Synthetic update-plan and backup disk-capacity boundaries."""
import update_apply
import update_cli
import update_execution
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import os
from types import SimpleNamespace

import maintenance
import pytest

from test_mcs_update import updater as updater


MIB = 1024 * 1024


@pytest.fixture
def capacity(updater, tmp_path, monkeypatch):
    # This is a synthetic byte file, never a real database or backup.
    source = tmp_path / "ledger.db"
    source.write_bytes(b"synthetic" * 137)
    cfg = {"runtime_mode": "hermes"}
    state = SimpleNamespace(floor=512, free=0, published=[], need_calls=[])
    monkeypatch.setattr(maintenance, "BACKUP_DIR", updater.BACKUP_DIR)
    monkeypatch.setattr(maintenance, "disk_floor_mb", lambda: state.floor)
    monkeypatch.setattr(maintenance.shutil, "disk_usage", lambda path: SimpleNamespace(free=state.free))
    real_need = maintenance._backup_need_mb

    def observed_need(path):
        state.need_calls.append(path)
        return real_need(path)

    monkeypatch.setattr(maintenance, "_backup_need_mb", observed_need)
    monkeypatch.setattr(maintenance.tempfile, "mkstemp", lambda **kwargs: (
        os.open(os.devnull, os.O_RDONLY), str(tmp_path / "synthetic-preupdate.db.tmp")))
    monkeypatch.setattr(maintenance, "_publish_backup", lambda *args, **kwargs: state.published.append(args))

    # Exercise the public plan and its real precheck, while preventing every
    # unrelated config, git, service, and installed-database lookup.
    monkeypatch.setattr(updater.mcs_util, "load_config", lambda: cfg)
    monkeypatch.setattr(updater, "_current_version", lambda: ("v1.0.0", "a" * 40))
    monkeypatch.setattr(update_cli, '_current_version', updater._current_version)
    monkeypatch.setattr(update_planning, '_current_version', updater._current_version)
    monkeypatch.setattr(updater.journal_store, "load_state", lambda path: {})
    monkeypatch.setattr(updater.restore_evidence, "database_version", lambda path: 1)
    monkeypatch.setattr(updater, "_git_out", lambda *a, **k: "b" * 40)
    monkeypatch.setattr(update_apply, '_git_out', updater._git_out)
    monkeypatch.setattr(update_planning, '_git_out', updater._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', updater._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', updater._git_out)
    monkeypatch.setattr(update_repository, '_git_out', updater._git_out)
    monkeypatch.setattr(update_restore, '_git_out', updater._git_out)
    monkeypatch.setattr(updater, "_git", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(update_apply, '_git', updater._git)
    monkeypatch.setattr(update_cli, '_git', updater._git)
    monkeypatch.setattr(update_planning, '_git', updater._git)
    monkeypatch.setattr(update_preflight, '_git', updater._git)
    monkeypatch.setattr(update_recovery, '_git', updater._git)
    monkeypatch.setattr(update_repository, '_git', updater._git)
    monkeypatch.setattr(update_restore, '_git', updater._git)
    monkeypatch.setattr(update_runtime_migration, '_git', updater._git)
    monkeypatch.setattr(updater, "precheck_tag", lambda tag: [])
    monkeypatch.setattr(update_execution, 'precheck_tag', updater.precheck_tag)
    monkeypatch.setattr(update_planning, 'precheck_tag', updater.precheck_tag)
    monkeypatch.setattr(updater, "_tree_clean", lambda **kwargs: True)
    monkeypatch.setattr(update_apply, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_execution, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_preflight, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_recovery, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_restore, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(update_runtime_migration, '_tree_clean', updater._tree_clean)
    monkeypatch.setattr(updater.mcs_config, "validate_config", lambda cfg: ([], {}))
    monkeypatch.setattr(updater.service_management, "runtime_gate_errors", lambda *a, **k: [])
    monkeypatch.setattr(updater.mcs_util, "hermes_exe", lambda cfg: "synthetic-hermes")
    monkeypatch.setattr(updater.runtime_inspection, "executable_available", lambda exe: True)
    monkeypatch.setattr(updater, "_migration_checkout_problem", lambda *a, **k: None)
    monkeypatch.setattr(update_preflight, '_migration_checkout_problem', updater._migration_checkout_problem)
    monkeypatch.setattr(updater, "_awaiting_consent_marker", lambda: None)
    monkeypatch.setattr(update_planning, '_awaiting_consent_marker', updater._awaiting_consent_marker)
    monkeypatch.setattr(update_recovery, '_awaiting_consent_marker', updater._awaiting_consent_marker)
    monkeypatch.setattr(updater.settings_migration_receipt, "pending", lambda data: False)
    monkeypatch.setattr(updater.update_migrations, "plan", lambda *a: (False, None, None))
    monkeypatch.setattr(updater, "_plugin_changed", lambda *a: False)
    monkeypatch.setattr(update_apply, '_plugin_changed', updater._plugin_changed)
    monkeypatch.setattr(update_planning, '_plugin_changed', updater._plugin_changed)
    monkeypatch.setattr(updater, "recorded_install_args", lambda: [])
    monkeypatch.setattr(update_planning, 'recorded_install_args', updater.recorded_install_args)
    monkeypatch.setattr(update_preflight, 'recorded_install_args', updater.recorded_install_args)
    monkeypatch.setattr(updater, "upgrade_notes", lambda *a: [])
    monkeypatch.setattr(update_planning, 'upgrade_notes', updater.upgrade_notes)
    monkeypatch.setattr(updater, "_recovery_notes", lambda *a: [])
    monkeypatch.setattr(update_planning, '_recovery_notes', updater._recovery_notes)

    state.size = source.stat().st_size
    state.updater = updater
    return state


@pytest.mark.parametrize("floor,free_margin,blocked", [
    (512, 128 * MIB, True),  # between the historical 64 and default 512 MiB floors
    (512, 512 * MIB - 1, True),
    (512, 512 * MIB, False),
    (512, 512 * MIB + 1, False),
    (0, 64 * MIB - 1, True),
    (0, 64 * MIB, False),
    (0, 64 * MIB + 1, False),
    (32, 64 * MIB - 1, True),
    (128, 128 * MIB - 1, True),
    (128, 128 * MIB, False),
])
def test_plan_uses_backup_capacity_and_preserves_old_margin(capacity, floor, free_margin, blocked):
    capacity.floor = floor
    capacity.free = 2 * capacity.size + free_margin
    expected_backup_bytes = 2 * capacity.size + floor * MIB
    assert maintenance._backup_need_mb(capacity.updater.LEDGER) * MIB == expected_backup_bytes
    capacity.need_calls.clear()

    result = capacity.updater.plan("v1.0.1")
    assert ("insufficient_disk" in result["blockers"]) == blocked
    assert result["route"] == ("blocked" if blocked else "apply")
    assert capacity.updater.LEDGER in capacity.need_calls
    assert not capacity.published


@pytest.mark.parametrize("floor,free_margin", [
    (512, 128 * MIB), (512, 512 * MIB - 1), (512, 512 * MIB),
    (0, 0), (0, 64 * MIB - 1), (0, 64 * MIB),
    (128, 128 * MIB - 1), (128, 128 * MIB),
])
def test_plan_and_actual_backup_capacity_gate_are_consistent(capacity, floor, free_margin):
    capacity.floor = floor
    capacity.free = 2 * capacity.size + free_margin
    result = capacity.updater.plan("v1.0.1")
    blocked = "insufficient_disk" in result["blockers"]
    backup_rejected = capacity.free < maintenance._backup_need_mb(capacity.updater.LEDGER) * MIB

    if backup_rejected:
        with pytest.raises(maintenance.MaintenanceError, match="backup_disk_low"):
            maintenance.preupdate_backup(capacity.updater.LEDGER)
        assert blocked and not capacity.published
    else:
        result_path = maintenance.preupdate_backup(capacity.updater.LEDGER)
        assert result_path.endswith("synthetic-preupdate.db")
        assert len(capacity.published) == 1
        # An additional precheck rejection is correct when the old 64 MiB
        # updater margin is stricter than the configured maintenance floor.
        assert blocked == (free_margin < max(floor, 64) * MIB)
