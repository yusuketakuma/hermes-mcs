"""Malformed recovery membership snapshots cannot authorize any service mutation."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("snapshot", [
    1, "synthetic", ["synthetic"],
    {"cron": None}, {"agents": None},
    {"cron": "synthetic"}, {"agents": {"label": "synthetic"}},
    {"cron": [None]}, {"agents": [None]},
    {"cron": [{"script": 3}]}, {"agents": [{"label": []}]},
])
def test_invalid_service_snapshot_returns_failure_before_external_actions(monkeypatch, snapshot):
    spec = importlib.util.spec_from_file_location(
        "synthetic_recovery_shape", ROOT / "deployment/recovery/mcs_recover.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_runtime_mode", lambda: "hermes")
    monkeypatch.setattr(module, "_runtime_config", lambda: {})
    monkeypatch.setattr(module.os.path, "isfile", lambda path: False)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid snapshot reached an external operation")

    monkeypatch.setattr(module, "_launchctl", forbidden)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    monkeypatch.setattr(module.glob, "glob", forbidden)
    assert module._reconcile_membership(snapshot) == ["service_snapshot_invalid"]
