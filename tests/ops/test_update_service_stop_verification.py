"""Updater service cleanup and quiesce require actual stop evidence."""
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import update_services
import lifecycle_io
import mcs_util
import service_contract
import service_management
import runtime_inspection
import itertools
from types import SimpleNamespace

import pytest

import mcs_update


@pytest.mark.parametrize("status", [0, 1, 5, 124, 127, 113])
def test_membership_keeps_owned_plist_without_verified_absence(tmp_path, monkeypatch, status):
    label = "ai.mcs.extract-obsolete"
    plist = tmp_path / (label + ".plist")
    plist.write_text("synthetic owned service")
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(update_lifecycle_context, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_preflight, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_services, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(mcs_update, "MANIFEST_PATH", str(tmp_path / "absent-manifest.json"))
    monkeypatch.setattr(update_execution, 'MANIFEST_PATH', mcs_update.MANIFEST_PATH)
    monkeypatch.setattr(update_lifecycle_context, 'MANIFEST_PATH', mcs_update.MANIFEST_PATH)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda _cfg: "synthetic-hermes")
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda _exe: True)
    monkeypatch.setattr(service_management, 'inspect_cron', lambda _exe: [])
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        assert argv[0] == "launchctl" and argv[1] in ("bootout", "print")
        return SimpleNamespace(returncode=status if argv[1] == "print" else 0)

    monkeypatch.setattr(mcs_update, "_run", run)
    monkeypatch.setattr(update_services, '_run', mcs_update._run)
    monkeypatch.setattr(lifecycle_io, "run_bounded", run)
    errors = service_management.retire_to_snapshot({'agents': [], 'cron': []}, cfg=mcs_util.load_config(), paths=mcs_update._service_paths())
    assert [argv[1] for argv in calls] == ["bootout", "print"]
    assert bool(errors) is (status != 113)
    assert plist.exists() is (status != 113)
    if status != 113:
        assert plist.read_text() == "synthetic owned service"


@pytest.mark.parametrize("status", [1, 5, 124, 127, 113])
def test_quiesce_does_not_read_a_provider_error_as_a_stopped_drainer(monkeypatch, status):
    monkeypatch.setattr(mcs_update, "_write_marker", lambda: None)
    monkeypatch.setattr(update_services, '_write_marker', mcs_update._write_marker)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(service_contract, 'LEGACY_LABELS', ())
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ("ai.mcs.synthetic",))
    monkeypatch.setattr(mcs_update, "_stray_drainer_pids", lambda: [])
    monkeypatch.setattr(update_services, '_stray_drainer_pids', mcs_update._stray_drainer_pids)
    ticks = itertools.count()
    monkeypatch.setattr(mcs_update, "time", SimpleNamespace(
        monotonic=lambda: next(ticks), sleep=lambda _seconds: None))
    monkeypatch.setattr(update_apply, 'time', mcs_update.time)
    monkeypatch.setattr(update_cli, 'time', mcs_update.time)
    monkeypatch.setattr(update_execution, 'time', mcs_update.time)
    monkeypatch.setattr(update_lifecycle_context, 'time', mcs_update.time)
    monkeypatch.setattr(update_recovery, 'time', mcs_update.time)
    monkeypatch.setattr(update_repository, 'time', mcs_update.time)
    monkeypatch.setattr(update_restore, 'time', mcs_update.time)
    monkeypatch.setattr(update_runtime_migration, 'time', mcs_update.time)
    monkeypatch.setattr(update_services, 'time', mcs_update.time)
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=status if argv[1] == "print" else 0,
                               stdout="")

    monkeypatch.setattr(mcs_update, "_run", run)
    monkeypatch.setattr(update_services, '_run', mcs_update._run)
    if status == 113:
        assert mcs_update.quiesce() == ["ai.mcs.synthetic"]
    else:
        with pytest.raises(mcs_update.UpdateError, match="drainer_stop_failed"):
            mcs_update.quiesce()
    assert len(calls) <= 20
