"""Only verified launchd absence allows owned plist removal or runtime replacement."""
from types import SimpleNamespace

import pytest

import mcs_setup


@pytest.mark.parametrize("route", ["retire", "switch"])
@pytest.mark.parametrize("status", [1, 124, 127, 0, 113])
def test_retirement_requires_verified_absence(tmp_path, monkeypatch, route, status):
    label = mcs_setup.AGENT_LABELS[0]
    plist = tmp_path / (label + ".plist")
    plist.write_text("synthetic owned service")
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(tmp_path))
    calls, notes = [], []

    def run(argv, **_kwargs):
        calls.append(argv)
        assert argv[0] == "launchctl" and argv[1] in ("bootout", "print")
        return SimpleNamespace(returncode=status if argv[1] == "print" else 0)

    monkeypatch.setattr(mcs_setup, "_run", run)
    if route == "retire":
        result = mcs_setup._retire_agents({label}, notes.append, False)
    else:
        previous = {"runtime_mode": "hermes", "agents": [{"label": label}], "cron": []}
        result = mcs_setup._retire_previous_runtime(previous, "standalone", notes.append, False)
    assert [call[1] for call in calls] == ["bootout", "print"]
    assert result == (0 if status == 113 else 1)
    assert plist.exists() is (status != 113)
    if status != 113:
        assert plist.read_text() == "synthetic owned service"


@pytest.mark.parametrize("route", ["retire", "switch"])
def test_retirement_dry_run_never_contacts_services(tmp_path, monkeypatch, route):
    label = mcs_setup.AGENT_LABELS[0]
    plist = tmp_path / (label + ".plist")
    plist.write_text("synthetic owned service")
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "_run", lambda *_a, **_kw: pytest.fail("dry run"))
    if route == "retire":
        result = mcs_setup._retire_agents({label}, lambda _message: None, True)
    else:
        previous = {"runtime_mode": "hermes", "agents": [{"label": label}], "cron": []}
        result = mcs_setup._retire_previous_runtime(previous, "standalone", lambda _message: None, True)
    assert result == 0 and plist.read_text() == "synthetic owned service"
