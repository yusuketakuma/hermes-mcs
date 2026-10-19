"""Updater service cleanup and quiesce require actual stop evidence."""
import runtime_inspection
import itertools
from types import SimpleNamespace

import pytest

import mcs_setup
import mcs_update


@pytest.mark.parametrize("status", [0, 1, 5, 124, 127, 113])
def test_membership_keeps_owned_plist_without_verified_absence(tmp_path, monkeypatch, status):
    label = "ai.mcs.extract-obsolete"
    plist = tmp_path / (label + ".plist")
    plist.write_text("synthetic owned service")
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(mcs_update, "MANIFEST_PATH", str(tmp_path / "absent-manifest.json"))
    monkeypatch.setattr(mcs_update, "load_config", lambda: {})
    monkeypatch.setattr(mcs_update, "hermes_exe", lambda _cfg: "synthetic-hermes")
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda _exe: True)
    monkeypatch.setattr(mcs_setup, "_cron_list", lambda _exe: [])
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        assert argv[0] == "launchctl" and argv[1] in ("bootout", "print")
        return SimpleNamespace(returncode=status if argv[1] == "print" else 0)

    monkeypatch.setattr(mcs_update, "_run", run)
    errors = mcs_update._reconcile_membership({"agents": [], "cron": []})
    assert [argv[1] for argv in calls] == ["bootout", "print"]
    assert bool(errors) is (status != 113)
    assert plist.exists() is (status != 113)
    if status != 113:
        assert plist.read_text() == "synthetic owned service"


@pytest.mark.parametrize("status", [1, 5, 124, 127, 113])
def test_quiesce_does_not_read_a_provider_error_as_a_stopped_drainer(monkeypatch, status):
    monkeypatch.setattr(mcs_update, "_write_marker", lambda: None)
    monkeypatch.setattr(mcs_update, "load_config", lambda: {})
    monkeypatch.setattr(mcs_update, "LEGACY_LABELS", ())
    monkeypatch.setattr(mcs_update, "RESIDENT_LABELS", ("ai.mcs.synthetic",))
    monkeypatch.setattr(mcs_update, "_stray_drainer_pids", lambda: [])
    ticks = itertools.count()
    monkeypatch.setattr(mcs_update, "time", SimpleNamespace(
        monotonic=lambda: next(ticks), sleep=lambda _seconds: None))
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=status if argv[1] == "print" else 0,
                               stdout="")

    monkeypatch.setattr(mcs_update, "_run", run)
    if status == 113:
        assert mcs_update.quiesce() == ["ai.mcs.synthetic"]
    else:
        with pytest.raises(mcs_update.UpdateError, match="drainer_stop_failed"):
            mcs_update.quiesce()
    assert len(calls) <= 20
