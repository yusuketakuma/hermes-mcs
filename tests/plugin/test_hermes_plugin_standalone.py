"""The plugin as Hermes loads it (spec_from_file_location, repo root NOT on
sys.path): registration works in hermes mode and stands down in standalone."""
import json
import subprocess
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "hermes_plugin" / "__init__.py"
LOADER = r"""
import importlib.util, json, sys, types
root = sys.argv[2]
sys.modules["hermes_plugins"] = types.ModuleType("hermes_plugins")   # as Hermes's loader
sys.path[:] = [p for p in sys.path if p not in ("", root)]
spec = importlib.util.spec_from_file_location(
    "hermes_plugins.mcs", sys.argv[1], submodule_search_locations=[sys.argv[1].rsplit("/", 1)[0]])
mod = importlib.util.module_from_spec(spec)
sys.modules["hermes_plugins.mcs"] = mod
spec.loader.exec_module(mod)
calls = []
class Ctx:
    def get_config(self, key, default=None):
        return sys.argv[3] if key == "data_root" else default
    def register_command(self, *a, **k): calls.append("command")
    def register_platform_handler(self, *a): calls.append("platform")
mod.register(Ctx())
print(json.dumps(calls))
"""


def _register(tmp_path, mode):
    (tmp_path / "flags").mkdir(exist_ok=True)
    (tmp_path / "flags" / "notify.json").write_text(json.dumps({"runtime_mode": mode}))
    out = subprocess.run([sys.executable, "-c", LOADER, str(PLUGIN),
                          str(PLUGIN.parents[1]), str(tmp_path)],
                         capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_registration_without_repo_root_on_sys_path(tmp_path):
    assert _register(tmp_path, "hermes") == ["command", "platform", "platform"]
    assert _register(tmp_path, "standalone") == []


def test_a_broken_probe_never_blocks_registration(tmp_path):
    (tmp_path / "flags").mkdir()
    (tmp_path / "flags" / "notify.json").write_text("{not json")
    out = subprocess.run([sys.executable, "-c", LOADER, str(PLUGIN), str(PLUGIN.parents[1]),
                          str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert json.loads(out.stdout) == ["command", "platform", "platform"]
    out = subprocess.run([sys.executable, "-c", LOADER, str(PLUGIN), str(PLUGIN.parents[1]),
                          str(tmp_path / "missing")], capture_output=True, text=True, timeout=60)
    assert json.loads(out.stdout) == ["command", "platform", "platform"]


def test_card_factories_stand_down_in_standalone(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_plugin import card_workers
    (tmp_path / "flags").mkdir()
    (tmp_path / "flags" / "notify.json").write_text(json.dumps(
        {"runtime_mode": "standalone", "interactive": True, "transport": "slack"}))
    monkeypatch.setattr(card_workers, "_interactive_settings",
                        lambda ctx, native=None: {"data_root": str(tmp_path)})
    monkeypatch.setattr(card_workers, "_slack_adapter_settings",
                        lambda ctx: {"data_root": str(tmp_path)})
    ctx = SimpleNamespace()
    assert card_workers.make_discord_factory(ctx)(object(), None) is None
    assert card_workers.make_slack_factory(ctx)(object(), None) is None
