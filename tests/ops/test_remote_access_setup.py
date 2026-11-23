"""Remote access: a Mac other devices reach saves its tailnet name, publishes with `tailscale serve` and runs resident."""
import json
from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

import mcs_config
import mcs_local_chat
import remote_access_setup as remote
import service_contract

REPO = Path(__file__).resolve().parents[2]
HOST = "mac-mini.example-tailnet.ts.net"


@pytest.mark.parametrize("value, ok", [
    ({}, True),
    ({"tailnet_host": HOST}, True),
    ({"tailnet_host": HOST, "port": 8770, "route_settings": "/private/route.json"}, True),
    ({"tailnet_host": "mac-mini.local"}, False),
    ({"tailnet_host": HOST, "port": 80}, False),
    ({"tailnet_host": HOST, "route_settings": "route.json"}, False),
    ({"tailnet_host": HOST, "funnel": True}, False),
    ([], False),
])
def test_remote_access_settings_are_checked(value, ok):
    errors, _ = mcs_config.validate_config({"mcs_login_id": "x", "notify_target": "y", "remote_access": value})
    assert (not any(e.startswith("remote_access") for e in errors)) is ok
    assert mcs_config.remote_access({"remote_access": value}) == (value if ok and value else None)


def test_doctor_expects_the_resident_agent_only_on_a_mac_set_up_for_other_devices():
    for mode in ("standalone", "hermes"):
        base = {"runtime_mode": mode, "sdc": {"enabled": False}}
        assert service_contract.REMOTE_ACCESS_LABEL not in service_contract.diagnosed_agent_labels(base)
        assert service_contract.REMOTE_ACCESS_LABEL not in service_contract.diagnosed_agent_labels(base | {"remote_access": {}})
        configured = base | {"remote_access": {"tailnet_host": HOST}}
        assert service_contract.REMOTE_ACCESS_LABEL in service_contract.diagnosed_agent_labels(configured)
    assert service_contract.owns_agent(service_contract.REMOTE_ACCESS_LABEL)


@pytest.mark.parametrize("mode", ["standalone", "hermes"])
def test_services_install_and_retire_the_resident_agent_beside_either_runtime(mode, tmp_path, monkeypatch):
    import service_management

    agents = tmp_path / "agents"
    agents.mkdir()
    paths = SimpleNamespace(platform="darwin", agents_dir=str(agents), checkout=str(REPO), uid=501)
    converged = []
    monkeypatch.setattr(service_management, "agent_loaded", lambda label, *, paths: False)
    monkeypatch.setattr(service_management, "agent_reconcile",
                        lambda label, dst, note, dry, *, paths: converged.append(label) or True)
    retired = []
    monkeypatch.setattr(service_management, "retire_agents",
                        lambda labels, note, dry, *, paths: retired.append(set(labels)) or 0)
    subs = {"PYTHON": "/venv/python3", "REPO": str(REPO), "DATA": str(tmp_path / "data"), "RUN_LOG_HELPER": "/h.py"}
    manifest = {"agents": []}
    cfg = {"runtime_mode": mode, "remote_access": {"tailnet_host": HOST}}
    assert service_management.sync_remote_access(subs, cfg, manifest, lambda _m: None, False, paths=paths) == 0
    assert (agents / "ai.mcs.local-chat.plist").exists() and converged == ["ai.mcs.local-chat"]
    assert manifest["agents"][0]["label"] == "ai.mcs.local-chat" and manifest["agents"][0]["loaded"] is True
    assert service_management.sync_remote_access(subs, {"runtime_mode": mode, "remote_access": {}}, manifest,
                                                 lambda _m: None, False, paths=paths) == 0
    assert retired == [{"ai.mcs.local-chat"}]


def test_the_agent_template_takes_every_setting_from_the_config():
    text = (REPO / "deployment/launchagents" / f"{service_contract.REMOTE_ACCESS_LABEL}.plist").read_text()
    rendered = service_contract.render_agent(text, {"PYTHON": "/venv/python3", "REPO": "/repo", "DATA": "/root/data",
                                                    "RUN_LOG_HELPER": "/root/helper.py"})
    agent = plistlib.loads(rendered.encode())
    assert agent["Label"] == service_contract.REMOTE_ACCESS_LABEL and agent["KeepAlive"] is True
    assert agent["ProgramArguments"][-3:] == ["/venv/python3", "/repo/mcs/ops/mcs_local_chat.py", "--resident"]


def test_resident_arguments_come_from_the_saved_settings(tmp_path):
    with pytest.raises(ValueError):
        mcs_local_chat.resident_argv({"remote_access": {}}, tmp_path)
    argv = mcs_local_chat.resident_argv({"remote_access": {"tailnet_host": HOST}}, tmp_path)
    assert argv == ["--snapshot", str(tmp_path / "data/snapshots/ledger-snapshot.db"), "--all-patients",
                    "--homecare-db", str(tmp_path / "data/homecare.db"), "--port", "8770", "--tailnet-host", HOST]
    routed = mcs_local_chat.resident_argv(
        {"remote_access": {"tailnet_host": HOST, "port": 9000, "route_settings": "/private/route.json"}}, tmp_path)
    assert routed[routed.index("--port") + 1] == "9000" and routed[-2:] == ["--route-settings", "/private/route.json"]


def test_resident_without_settings_starts_nothing(monkeypatch):
    import mcs_util
    monkeypatch.setattr(mcs_util, "load_config_checked", lambda *a, **k: {})
    monkeypatch.setattr(mcs_local_chat, "ChatServer", lambda *a, **k: pytest.fail("no server without settings"))
    with pytest.raises(SystemExit) as stopped:
        mcs_local_chat.main(["--resident"])
    assert "mcs setup remote" in str(stopped.value.code)


@pytest.fixture
def tailnet(monkeypatch):
    import config_edit
    import mcs_util

    state = SimpleNamespace(calls=[], saved=[], services=0, cfg={}, status={
        "BackendState": "Running", "Self": {"DNSName": HOST.upper() + "."}})

    def run(cli, *argv):
        state.calls.append(list(argv))
        if argv[:1] == ("status",):
            return SimpleNamespace(returncode=0, stdout=json.dumps(state.status))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(remote, "tailscale_cli", lambda: "/stub/tailscale")
    monkeypatch.setattr(remote, "_run", run)
    monkeypatch.setattr(mcs_util, "load_config_checked", lambda *a, **k: state.cfg)
    monkeypatch.setattr(config_edit, "apply", lambda changes: (state.saved.append(changes), ({}, []))[1])

    def services(args):
        state.services += 1
        return 0

    state.run = lambda **options: remote.command(
        SimpleNamespace(**({"port": None, "route_settings": None, "off": False} | options)), services=services)
    return state


def test_setup_saves_the_name_publishes_to_the_tailnet_only_and_registers_the_agent(tailnet, capsys):
    assert tailnet.run() == 0
    assert tailnet.saved == [{"remote_access.tailnet_host": HOST, "remote_access.port": 8770}]
    assert ["serve", "--bg", "http://127.0.0.1:8770"] in tailnet.calls
    assert not any("funnel" in call for call in tailnet.calls)
    assert tailnet.services == 1 and f"https://{HOST}/" in capsys.readouterr().out


def test_setup_stops_before_saving_until_tailscale_is_signed_in(tailnet, capsys):
    tailnet.status = {"BackendState": "NeedsLogin"}
    assert tailnet.run() == 1
    assert tailnet.saved == [] and tailnet.services == 0
    assert "サインイン" in capsys.readouterr().out
    tailnet.status = {"BackendState": "Running", "Self": {"DNSName": "100.64.0.1"}}
    assert tailnet.run() == 1 and tailnet.saved == []


def test_setup_needs_tailscale(tailnet, monkeypatch, capsys):
    monkeypatch.setattr(remote, "tailscale_cli", lambda: None)
    assert tailnet.run() == 1 and tailnet.calls == [] and "--with-tailscale" in capsys.readouterr().out


def test_route_settings_must_be_private(tailnet, tmp_path):
    settings = tmp_path / "route.json"
    settings.write_text("{}")
    settings.chmod(0o644)
    assert tailnet.run(route_settings=str(settings)) == 1 and tailnet.saved == []
    settings.chmod(0o600)
    assert tailnet.run(route_settings=str(settings)) == 0
    assert tailnet.saved[-1]["remote_access.route_settings"] == str(settings.resolve())


def test_off_clears_the_settings_and_retires_the_agent(tailnet):
    tailnet.cfg = {"remote_access": {"tailnet_host": HOST, "port": 8770}}
    assert tailnet.run(off=True) == 0
    assert tailnet.saved == [{"remote_access.tailnet_host": None, "remote_access.port": None}]
    assert tailnet.services == 1 and tailnet.calls == []
