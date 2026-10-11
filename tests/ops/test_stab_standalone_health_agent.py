"""The standalone host's external health watch: rendering, install, retire, doctor (stubbed)."""
import plistlib
from types import SimpleNamespace

import mcs_setup
from mcs_standalone import service


def test_rendered_health_watch_runs_outside_the_host(tmp_path):
    [(name, body)] = service.render_health(tmp_path, platform="darwin")
    value = plistlib.loads(body.encode())
    assert name == "local.mcs-health.plist" and value["Label"] == mcs_setup.HEALTH_LABEL
    assert value["ProgramArguments"][1].endswith("/mcs/ingest/health_watch.py")
    assert value["ProgramArguments"][2:] == ["--home", str(tmp_path)]
    assert value["StartInterval"] == 300 and value["Umask"] == 0o077
    units = dict(service.render_health(tmp_path, platform="linux"))
    assert set(units) == set(mcs_setup.HEALTH_UNITS)
    assert "Type=oneshot" in units["mcs-health.service"]
    assert f'Environment="MCS_ROOT={tmp_path}"' in units["mcs-health.service"]
    assert "OnUnitActiveSec=300s" in units["mcs-health.timer"]


def _linux(monkeypatch, tmp_path, active=True):
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "_standalone_service_path",
                        lambda: str(tmp_path / "units" / mcs_setup.STANDALONE_UNIT))
    (tmp_path / "units").mkdir()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        code = 0 if "is-active" not in argv or active else 3
        return SimpleNamespace(returncode=code, stdout="", stderr="")

    monkeypatch.setattr(mcs_setup, "_run", run)
    return calls


def test_linux_services_install_and_enable_the_health_timer(monkeypatch, tmp_path):
    calls = _linux(monkeypatch, tmp_path)
    manifest = {"agents": []}
    assert mcs_setup._sync_standalone_health(manifest, lambda msg: None, False) == 0
    assert (tmp_path / "units" / "mcs-health.timer").stat().st_mode & 0o077 == 0
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "restart", "mcs-health.timer"] in calls
    assert [row["label"] for row in manifest["agents"]] == list(mcs_setup.HEALTH_UNITS)
    calls.clear()
    assert mcs_setup._sync_standalone_health({"agents": []}, lambda msg: None, False) == 0
    assert calls == [["systemctl", "--user", "is-active", "--quiet", "mcs-health.timer"]]


def test_linux_switch_back_retires_the_health_timer(monkeypatch, tmp_path):
    calls = _linux(monkeypatch, tmp_path)
    for name in mcs_setup.HEALTH_UNITS:
        (tmp_path / "units" / name).write_text("synthetic")
    assert mcs_setup._retire_agents({mcs_setup.STANDALONE_LABEL}, lambda msg: None, True) == 0
    assert calls == [] and (tmp_path / "units" / "mcs-health.timer").exists()
    assert mcs_setup._retire_agents({mcs_setup.STANDALONE_LABEL}, lambda msg: None, False) == 0
    assert ["systemctl", "--user", "disable", "--now", "mcs-health.timer"] in calls
    assert not any((tmp_path / "units" / name).exists() for name in mcs_setup.HEALTH_UNITS)


def test_doctor_reports_missing_watch_and_host_problems(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path, active=False)
    import health_watch
    monkeypatch.setattr(health_watch, "standalone_host_reasons", lambda home, now, cfg: (
        "degraded", ["standalone_child_failing"], {"standalone_child_failing": "cmd 3回連続"}))
    monkeypatch.setattr(mcs_setup, "load_config", lambda path=None: {"runtime_mode": "standalone"})
    errors, warnings = mcs_setup._check_standalone_health()
    assert errors == []
    assert warnings[0].startswith("standalone health watch not installed")
    assert warnings[1].startswith("standalone host: standalone_child_failing (cmd 3回連続)")
    for name, body in service.render_health(tmp_path, platform="linux"):
        (tmp_path / "units" / name).write_text(body)
    errors, _ = mcs_setup._check_standalone_health()
    assert errors == ["standalone health watch installed but not loaded — run `mcs_setup.py services`"]
