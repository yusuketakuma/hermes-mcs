"""Regression: /mcs settings accept an empty project list when project_ids_auto is on."""
from __future__ import annotations

import hermes_plugin


class _Ctx:
    def __init__(self, config):
        self.config = config

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def _config(**extra):
    return {"snapshot": "/x/s.db", "inbox": "/x/cmd",
            "allowed_user_ids": ["1"], "allowed_chat_ids": ["2"],
            "project_ids": [], **extra}


def test_empty_projects_allowed_with_auto():
    settings = hermes_plugin._settings(_Ctx(_config(project_ids_auto=True)))
    assert settings is not None
    assert settings["project_ids"] == frozenset()
    assert settings["project_ids_auto"] is True


def test_empty_projects_rejected_without_auto():
    assert hermes_plugin._settings(_Ctx(_config())) is None
    assert hermes_plugin._settings(_Ctx(_config(project_ids_auto="true"))) is None


def test_invalid_projects_still_rejected_with_auto():
    assert hermes_plugin._settings(
        _Ctx(_config(project_ids=[0], project_ids_auto=True))) is None
