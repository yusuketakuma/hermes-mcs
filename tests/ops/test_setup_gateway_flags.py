"""First setup publishes synthetic route flags before starting any Hermes card worker."""
import json
from pathlib import Path

import pytest

import mcs_setup
import mcs_util
import notify_spool
import service_management
from test_mcs_setup import _init_env


def _config(transport, mode="hermes"):
    channel = {"slack": "C1234567890", "discord": "1234567890", "lineworks": "room1"}[transport]
    scope = {"profile": "synthetic", "application_id": "123", "channel_id": channel,
             "allowed_user_ids": ["U123"], "project_ids": [101]}
    scope["guild_id" if transport == "discord" else "team_id"] = "456"
    cfg = {"mcs_login_id": "synthetic", "notify_target": transport + ":" + channel,
           "notify": {"interactive": transport, "route_epoch": 7, transport: scope}}
    if transport == "slack":
        # an existing separate system route keeps channel setup a no-op
        cfg["notify_system_target"] = "slack:C9999999999"
    if mode is not None:
        cfg["runtime_mode"] = mode
    return cfg


@pytest.mark.parametrize("transport", ["slack", "discord"])
@pytest.mark.parametrize("mode", [None, "hermes"])
def test_first_hermes_gateway_sees_route_flags_and_directories(monkeypatch, tmp_path, transport, mode):
    cfg = _config(transport, mode)
    _init_env(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(mcs_util, "hermes_exe", lambda *a: "/synthetic/hermes")
    monkeypatch.setattr(mcs_setup.runtime_inspection, "executable_available", lambda path: path == "/synthetic/hermes")
    observed = []

    def gateway(current, exe, note, dry):
        assert not dry and exe == "/synthetic/hermes"
        root = tmp_path / "data"
        flags = json.loads((root / "flags" / "notify.json").read_text())
        assert flags["transport"] == transport
        assert flags["route_epoch"] == 7
        assert flags["interactive"] is True and flags["kill_switch"] is False
        assert flags["runtime_mode"] == "hermes"
        assert flags["restore_pending"] is False
        for directory in notify_spool.notify_dirs(str(root)).values():
            assert Path(directory).is_dir()
            assert Path(directory).stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "c.json").is_file()
        observed.append(flags)
        return 0

    monkeypatch.setattr(service_management, "sync_gateway", gateway)
    assert not (tmp_path / "data").exists()
    assert mcs_setup.main(["init", "--yes"]) == 0
    assert mcs_setup.main(["init", "--yes"]) == 0  # unchanged flags return False, not a write failure
    assert len(observed) == 2
    saved = json.loads((tmp_path / "c.json").read_text())
    assert saved["notify"] == cfg["notify"]
    assert saved.get("runtime_mode") == mode


@pytest.mark.parametrize("failed_step", ["ensure_dirs", "publish_flags"])
def test_flags_failure_keeps_saved_config_and_does_not_start_gateway(monkeypatch, tmp_path, capsys, failed_step):
    cfg = _config("slack")
    _init_env(monkeypatch, tmp_path, cfg)
    seen = []
    monkeypatch.setattr(mcs_setup, "_apply_plugin_integration", lambda cfg, args: seen.append(args) or True)
    monkeypatch.setattr(service_management, "sync_gateway", lambda *a, **k: pytest.fail("gateway must not start"))

    def failed(*args):
        raise OSError("synthetic private error must not appear")

    monkeypatch.setattr(notify_spool, failed_step, failed)
    assert mcs_setup.main(["init", "--yes"]) == 1
    assert seen[0]._config_saved is True
    saved = json.loads((tmp_path / "c.json").read_text())
    assert saved["notify"] == cfg["notify"]
    output = capsys.readouterr().out
    assert "設定は保存しましたが" in output and "mcs setup" in output
    assert "synthetic private error" not in output


@pytest.mark.parametrize("transport,mode", [("slack", "standalone"), ("discord", "standalone"),
                                           ("lineworks", "hermes")])
def test_existing_independent_routes_still_publish_without_hermes_gateway(monkeypatch, tmp_path, transport, mode):
    cfg = _config(transport, mode)
    _init_env(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(mcs_setup, "_init_standalone_connectors", lambda *a: True)
    monkeypatch.setattr(service_management, "sync_gateway", lambda *a, **k: pytest.fail("Hermes gateway"))
    assert mcs_setup.main(["init", "--yes"]) == 0
    flags = json.loads((tmp_path / "data" / "flags" / "notify.json").read_text())
    assert flags["transport"] == transport and flags["route_epoch"] == 7
    assert flags["runtime_mode"] == mode


def test_noninteractive_init_does_not_publish_or_start_gateway(monkeypatch, tmp_path):
    cfg = {"mcs_login_id": "synthetic", "notify_target": "slack:C123",
           "notify_system_target": "local", "notify": {"interactive": "off"}}
    _init_env(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(notify_spool, "ensure_dirs", lambda *a: pytest.fail("notification directory write"))
    monkeypatch.setattr(notify_spool, "publish_flags", lambda *a: pytest.fail("flags write"))
    monkeypatch.setattr(service_management, "sync_gateway", lambda *a, **k: pytest.fail("gateway start"))
    assert mcs_setup.main(["init", "--yes"]) == 0
    assert not (tmp_path / "data" / "flags").exists()
