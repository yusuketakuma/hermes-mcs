"""Regression: Hermes Slack cards accept auto projects and pass the route epoch to /mcs."""
from __future__ import annotations

import json

from adapters.common import commands
from hermes_plugin import card_workers


class _Ctx:
    def __init__(self, config):
        self.config = config

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def _config(root, **extra):
    return {"slack_adapter_enabled": True, "slack_team_id": "T1",
            "slack_application_id": "A1", "slack_channel_id": "C1",
            "slack_allowed_user_ids": ["U1"], "project_ids": [1],
            "data_root": str(root), **extra}


def test_empty_projects_allowed_with_auto(tmp_path):
    settings = card_workers._slack_adapter_settings(
        _Ctx(_config(tmp_path, project_ids=[], project_ids_auto=True)))
    assert settings is not None
    assert settings["project_ids"] == frozenset()
    assert settings["project_ids_auto"] is True
    assert card_workers._slack_adapter_settings(
        _Ctx(_config(tmp_path, project_ids=[]))) is None


def test_factory_settings_pass_the_command_epoch_gate(tmp_path, monkeypatch):
    (tmp_path / "flags").mkdir()
    (tmp_path / "flags" / "notify.json").write_text(json.dumps(
        {"interactive": True, "transport": "slack", "route_epoch": 3}))
    captured = {}

    class FakeSupervisor:
        def __init__(self, *, settings, **_):
            captured.update(settings)

        def start(self):
            pass

    from hermes_plugin.mcs_slack import tasks
    monkeypatch.setattr(tasks, "Supervisor", FakeSupervisor)
    assert card_workers.make_slack_factory(_Ctx(_config(tmp_path)))(
        object(), None) is not None
    assert captured["route_epoch"] == 3
    result = commands.answer(captured, "not json", user="U1", channel="C1")
    assert json.loads(result)["error"] == "bad_command"
