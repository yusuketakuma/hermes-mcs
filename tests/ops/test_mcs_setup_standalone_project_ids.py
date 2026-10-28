"""Standalone init rejects --plugin-project-ids it cannot fully honour,
exactly like the Hermes path, before any secret, .env or config write.
Temporary HOME and stubs only; no keychain, Hermes or real config."""
import service_contract
import service_management
import lifecycle_io
import json
from types import SimpleNamespace

import pytest

import mcs_setup
from test_mcs_setup import _init_env
from test_mcs_setup_standalone import config


def _run(monkeypatch, tmp_path, ids):
    _init_env(monkeypatch, tmp_path, config())
    monkeypatch.setattr(mcs_setup.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0))
    stored = []
    monkeypatch.setattr(lifecycle_io, "store_keychain",
                        lambda *a: stored.append("keychain") or True)
    monkeypatch.setenv("MCS_SETUP_PASSWORD", "synthetic-password")
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes", "--plugin-project-ids", ids])
    return mcs_setup.main(), stored


@pytest.mark.parametrize("ids", ["12,1a3", "0", "12,-3", "abc", " , "])
def test_unusable_project_ids_write_nothing(monkeypatch, tmp_path, capsys, ids):
    code, stored = _run(monkeypatch, tmp_path, ids)
    assert code == 1
    assert "プロジェクトID" in capsys.readouterr().out
    assert stored == []
    assert json.loads((tmp_path / "c.json").read_text()) == config()
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("ids,want", [("7, 8", [7, 8]), ("[7,8]", [7, 8]), ("9", [9])])
def test_valid_project_ids_are_saved_whole(monkeypatch, tmp_path, ids, want):
    code, stored = _run(monkeypatch, tmp_path, ids)
    assert code == 0 and stored == ["keychain"]
    saved = json.loads((tmp_path / "c.json").read_text())
    assert saved["notify"]["discord"]["project_ids"] == want


@pytest.mark.parametrize("mode", ["hermes", "standalone"])
def test_desired_agents_are_only_the_repo_templates(monkeypatch, tmp_path, mode):
    """No per-job calendar agent is ever rendered: the standalone host
    schedules its own jobs, Hermes mode uses hermes cron."""
    cfg = config() if mode == "standalone" else {"runtime_mode": "hermes"}
    subs = {"PYTHON": "/synthetic/python", "REPO": str(tmp_path), "DATA": str(tmp_path / "data"),
            "RUNTIME_HOME": str(tmp_path), "WATCHDOG_GRACE": "60"}
    agents = service_management.desired_agents(subs, cfg, paths=mcs_setup._service_paths())
    assert set(agents) == set(service_contract.desired_agent_labels(cfg))
    assert not any(label.startswith(service_contract.CRON_LABEL_PREFIX) for label in agents)
    assert all("__PYTHON__" not in body for body in agents.values())
