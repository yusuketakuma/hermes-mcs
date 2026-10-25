"""Doctor must report (not crash on) a malformed notify block."""
import lifecycle_io
import mcs_util
import runtime_inspection
import json

import pytest

import local_llm
import mcs_setup
from ops_testkit import git_reads_or




@pytest.fixture
def malformed_config(monkeypatch):
    def install(cfg):
        monkeypatch.setattr(mcs_util, 'load_config', lambda: cfg)
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda exe: {
        "python": "3.14.8", "sqlite": "3.51.3",
        "packages": {"discord.py": None, "slack-bolt": None, "slack-sdk": None}})
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version_info", (3, 51, 3))
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version", "3.51.3")
    monkeypatch.setattr(lifecycle_io, 'run_bounded', git_reads_or(lambda *a, **k: pytest.fail("service probe")))
    monkeypatch.setattr(local_llm, "bounded_request", lambda *a: pytest.fail("network"))
    return install


@pytest.mark.parametrize("notify", [
    "slack",                                          # whole block not an object
    {"interactive": "slack", "slack": "synthetic"},   # transport scope not an object
])
def test_doctor_reports_malformed_notify_as_blocked(malformed_config, capsys, notify):
    malformed_config({"mcs_login_id": "synthetic-id", "notify_target": "local",
                      "notify": notify})
    # validate_config already classifies this config as an error; doctor
    # must return its bounded report with configuration=blocked, exit 1.
    assert mcs_setup.validate_config({"mcs_login_id": "x", "notify_target": "local",
                                      "notify": notify})[0]
    assert mcs_setup.main(["doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["configuration"]["status"] == "blocked"
