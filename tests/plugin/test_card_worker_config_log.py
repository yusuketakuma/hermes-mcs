"""An opted-in card worker with incomplete config must say so once in
the log instead of silently never delivering queued cards."""
import logging
import json

from hermes_plugin import card_workers
from adapters.common import commands


class _Ctx:
    def __init__(self, config):
        self.config = config

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def test_incomplete_opt_in_logs_worker_disabled(caplog):
    caplog.set_level(logging.INFO, logger="hermes.plugin")
    assert card_workers.make_discord_factory(
        _Ctx({"interactive": True}))(None, None) is None
    assert card_workers.make_slack_factory(
        _Ctx({"slack_adapter_enabled": True}))(None, None) is None
    lines = [(r.name, r.getMessage()) for r in caplog.records]
    assert lines == [
        ("hermes.plugin.mcs_discord",
         '{"event": "worker_disabled", "reason": "config_incomplete"}'),
        ("hermes.plugin.mcs_slack",
         '{"event": "worker_disabled", "reason": "config_incomplete"}')]


INCOMPLETE_AS_HERMES_LOADS = r"""
import importlib.util, logging, sys, types
root = sys.argv[2]
sys.modules["hermes_plugins"] = types.ModuleType("hermes_plugins")
sys.path[:] = [p for p in sys.path if p not in ("", root)]
spec = importlib.util.spec_from_file_location(
    "hermes_plugins.mcs", sys.argv[1], submodule_search_locations=[sys.argv[1].rsplit("/", 1)[0]])
mod = importlib.util.module_from_spec(spec)
sys.modules["hermes_plugins.mcs"] = mod
spec.loader.exec_module(mod)
from hermes_plugins.mcs import card_workers
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(name)s %(message)s")
class Ctx:
    def get_config(self, key, default=None):
        return {"interactive": True, "slack_adapter_enabled": True}.get(key, default)
assert card_workers.make_discord_factory(Ctx())(None, None) is None
assert card_workers.make_slack_factory(Ctx())(None, None) is None
"""


# Regression: Hermes loads the plugin without the repo root on sys.path; the
# config_incomplete log used to raise "No module named 'adapters'" there.
def test_incomplete_opt_in_logs_when_loaded_as_hermes_does(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    plugin = Path(card_workers.__file__).resolve().parent / "__init__.py"
    out = subprocess.run([sys.executable, "-c", INCOMPLETE_AS_HERMES_LOADS, str(plugin),
                          str(plugin.parents[1])],
                         capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [
        'hermes.plugin.mcs_discord {"event": "worker_disabled", "reason": "config_incomplete"}',
        'hermes.plugin.mcs_slack {"event": "worker_disabled", "reason": "config_incomplete"}']


def test_opted_out_stays_silent(caplog):
    caplog.set_level(logging.INFO, logger="hermes.plugin")
    assert card_workers.make_discord_factory(_Ctx({}))(None, None) is None
    assert card_workers.make_slack_factory(_Ctx({}))(None, None) is None
    assert caplog.records == []


# Regression: Hermes Slack cards accept auto projects and pass the route epoch to /mcs.
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
            return True

    from adapters.slack import tasks
    monkeypatch.setattr(tasks, "Supervisor", FakeSupervisor)
    from types import SimpleNamespace
    assert card_workers.make_slack_factory(_Ctx(_config(tmp_path)))(
        object(), SimpleNamespace(_owner_profile=None)) is not None
    assert captured["route_epoch"] == 3
    result = commands.answer(captured, "not json", user="U1", channel="C1")
    assert json.loads(result)["error"] == "bad_command"


def test_dispatch_type_error_is_an_operation_failure_not_bad_command(
        tmp_path, monkeypatch):
    """Only a parse error is the caller's bad_command; a TypeError raised
    inside an op handler is operation_failed, as in the Hermes handler."""
    (tmp_path / "flags").mkdir()
    (tmp_path / "flags" / "notify.json").write_text(json.dumps(
        {"interactive": True, "transport": "slack", "route_epoch": 3}))
    settings = {"transport": "slack", "allowed_user_ids": ["U1"],
                "channel_id": "C1", "data_root": str(tmp_path),
                "route_epoch": 3, "team_id": "T1", "profile": "p",
                "application_id": "A1"}

    def broken(*_args):
        raise TypeError("synthetic handler bug")

    monkeypatch.setattr(commands, "dispatch", broken)
    result = commands.answer(settings, '{"op":"summary"}', user="U1",
                             channel="C1")
    assert json.loads(result)["error"] == "operation_failed"
    assert json.loads(commands.answer(settings, "not json", user="U1",
                                      channel="C1"))["error"] == "bad_command"
