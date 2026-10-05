import json

import mcs_setup
import mcs_util
from test_mcs_setup import _init_env, _services_env


def test_changed_standalone_plist_is_reloaded_into_launchd(monkeypatch, tmp_path):
    from mcs_standalone import service

    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    calls, args = _services_env(monkeypatch, tmp_path, loaded={mcs_setup.STANDALONE_LABEL})
    monkeypatch.setattr(mcs_setup, "load_config", lambda path=None: mcs_util.load_config(path) if path else {"runtime_mode": "standalone"})
    monkeypatch.setattr(mcs_setup, "_standalone_py_problem", lambda cfg: None)
    requests = []
    monkeypatch.setattr(service, "request_restart", lambda root: requests.append(root))
    stale = mcs_setup._standalone_service_path()
    (tmp_path / "agents").mkdir(exist_ok=True)
    with open(stale, "w") as f:
        f.write("<plist>old checkout</plist>")
    assert mcs_setup.cmd_services(args) == 0
    assert ["launchctl", "bootout", "gui/501/ai.mcs.standalone"] in calls
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in calls)
    assert requests == []


def test_init_repairs_invalid_stored_runtime_mode(monkeypatch, tmp_path):
    _init_env(monkeypatch, tmp_path, {"mcs_login_id": "u1", "notify_target": "slack",
                                      "runtime_mode": "Standalone"})
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init", "--yes", "--runtime-mode", "hermes"])
    assert mcs_setup.main() == 0
    assert json.loads((tmp_path / "c.json").read_text())["runtime_mode"] == "hermes"


def test_init_invalid_runtime_mode_set_writes_no_secret(monkeypatch, tmp_path, capsys):
    _init_env(monkeypatch, tmp_path, {"mcs_login_id": "u1", "notify_target": "slack"})
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init", "--yes", "--set", 'runtime_mode="standalon"'])
    assert mcs_setup.main() == 1
    assert "nothing written" in capsys.readouterr().out
    assert not (tmp_path / ".env").exists() and not (tmp_path / "c.json").exists()
