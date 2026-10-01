"""Canonical adapter packages and legacy entry points share runtime state."""
import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_lineworks_compatibility_entry_uses_canonical_classes_and_cli_wrapper():
    import importlib.util
    import lineworks_adapter
    from adapters import lineworks

    assert lineworks_adapter.ClientError is lineworks.ClientError
    assert lineworks_adapter.Credentials is lineworks.Credentials
    assert lineworks_adapter.LineWorksClient is lineworks.LineWorksClient
    assert Path(importlib.util.find_spec("lineworks_adapter.__main__").origin) == (
        Path(__file__).resolve().parents[2] / "lineworks_adapter" / "__main__.py")


@pytest.mark.parametrize("transport,module", [
    ("slack", "cards"), ("slack", "actions"), ("slack", "delivery"),
    ("slack", "tasks"), ("slack", "paths"), ("discord", "cards"),
    ("discord", "actions"), ("discord", "delivery"), ("discord", "tasks"),
])
def test_canonical_and_legacy_adapter_imports_share_instance(transport, module):
    canonical = importlib.import_module(f"adapters.{transport}.{module}")
    legacy = importlib.import_module(f"hermes_plugin.mcs_{transport}.{module}")
    expected = Path(__file__).resolve().parents[2] / "adapters" / transport / (module + ".py")
    assert Path(canonical.__file__).resolve() == expected
    assert Path(legacy.__file__).resolve() == expected
    assert canonical is legacy


@pytest.mark.parametrize("transport", ["slack", "discord"])
@pytest.mark.parametrize("legacy_first", [False, True])
def test_adapter_aliases_work_in_either_import_order_without_sdks(tmp_path, transport,
                                                               legacy_first):
    import subprocess
    import sys

    code = """
import importlib
import sys
sys.path.insert(0, sys.argv[1])
for sdk in ('discord', 'slack_sdk', 'slack_bolt'):
    sys.modules[sdk] = None
transport = sys.argv[2]
prefixes = ['adapters.' + transport, 'hermes_plugin.mcs_' + transport]
if sys.argv[3] == 'True':
    prefixes.reverse()
for name in ('cards', 'actions', 'delivery', 'tasks') + (('paths',) if transport == 'slack' else ()):
    first = importlib.import_module(prefixes[0] + '.' + name)
    second = importlib.import_module(prefixes[1] + '.' + name)
    assert first is second
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[2]),
         transport, str(legacy_first)],
        cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("legacy_guard", [False, True])
@pytest.mark.parametrize("fault", [500, ConnectionResetError(54, "synthetic reset")])
def test_mixed_discord_imports_keep_retry_suppression(legacy_guard, fault):
    from adapters.discord import cards as canonical
    from hermes_plugin.mcs_discord import cards as legacy
    from test_mcs_discord_single_post import WireBot

    installer, sender = (legacy, canonical) if legacy_guard else (canonical, legacy)
    bot = WireBot(faults=[fault])
    assert installer.single_post_ready(bot)
    with pytest.raises(sender.DiscordRetrySuppressed):
        asyncio.run(sender.single_post(
            bot, lambda: bot.http.request("POST", "/synthetic/messages")))
    assert len(bot.wire.posts()) == 1 and bot.wire.committed == 1


def test_slack_reconnect_hands_off_across_import_names(monkeypatch):
    from adapters.slack import tasks as canonical
    from hermes_plugin.mcs_delivery import registry
    from hermes_plugin.mcs_slack import tasks as legacy

    settings = {"transport": "slack", "team_id": "T_SYNTHETIC",
                "profile": "default", "application_id": "A_SYNTHETIC",
                "channel_id": "C_SYNTHETIC"}
    stopped = []
    monkeypatch.setattr(canonical, "_LIVE", {
        registry.scope_key(settings): SimpleNamespace(unload=lambda: stopped.append(True)),
    })

    def spawn(coro, **kwargs):
        coro.close()
        return SimpleNamespace(done=lambda: False)

    successor = object.__new__(legacy.Supervisor)
    successor._settings = settings
    successor._worker_id = "synthetic"
    successor._ctx = SimpleNamespace(spawn_task=spawn, on_unload=lambda fn: None)
    assert successor.start()
    assert stopped == [True]
    assert canonical._LIVE[registry.scope_key(settings)] is successor


@pytest.mark.parametrize("relative,source,expected", [
    ("discord/cards.py", "def view():\n    import discord\n    return discord.ui.LayoutView()\n", False),
    ("discord/cards.py", "def bot():\n    import discord\n    return discord.Client()\n", True),
    ("discord/new.py", "def view():\n    import discord\n    return discord.ui.LayoutView()\n", True),
    ("slack/delivery.py", "import socket\n", True),
    ("slack/actions.py", "import asyncio\nasync def wait():\n    await asyncio.sleep(0)\n", False),
    ("slack/actions.py", "import asyncio\nasync def connect():\n    await asyncio.open_connection('synthetic', 1)\n", True),
    ("lineworks/client.py", "import urllib.request\nimport subprocess\n", False),
    ("lineworks/server.py", "import urllib.request\n", True),
    ("lineworks/actions.py", "import asyncio\nasync def connect():\n    await asyncio.open_connection('synthetic', 1)\n", True),
    ("lineworks/__main__.py", "import asyncio\ndef run():\n    asyncio.run(main())\n", False),
    ("lineworks/__main__.py", "import asyncio\ntry:\n    wait()\nexcept asyncio.TimeoutError:\n    pass\n", False),
    ("lineworks/actions.py", "import asyncio\ntry:\n    wait()\nexcept asyncio.TimeoutError:\n    pass\n", True),
    ("slack/actions.py", "import asyncio\ntry:\n    wait()\nexcept asyncio.TimeoutError:\n    pass\n", True),
    ("lineworks/client.py", "import os\nvalue = os.environ.get('SYNTHETIC_SECRET')\n", True),
    ("slack/delivery.py", "import subprocess\n", True),
])
def test_canonical_adapter_keeps_sdk_and_network_boundaries(tmp_path, monkeypatch,
                                                           relative, source, expected):
    from ci import gates

    for name in ("MCS", "PLUGIN", "ADAPTERS"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        monkeypatch.setattr(gates, name, directory)
    candidate = gates.ADAPTERS / relative
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(source)
    violations = gates.gate_stdlib_only() + gates.gate_plugin_sandbox()
    assert bool(violations) is expected


@pytest.mark.parametrize("transport", ["slack", "discord"])
def test_legacy_adapter_bootstraps_when_host_loads_another_namespace(tmp_path, transport):
    import subprocess
    import sys

    shim = Path(__file__).resolve().parents[2] / "hermes_plugin" / ("mcs_" + transport) / "__init__.py"
    # -I removes checkout/PYTHONPATH/user site paths. The host may discover
    # a symlinked plugin under its own package name rather than hermes_plugin.
    code = """
import importlib
import importlib.util
import sys
from pathlib import Path
shim = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('synthetic_host_adapter', shim,
                                            submodule_search_locations=[str(shim.parent)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
for name in ('cards', 'actions', 'delivery', 'tasks') + (('paths',) if sys.argv[2] == 'slack' else ()):
    module = importlib.import_module(spec.name + '.' + name)
    assert Path(module.__file__).resolve().parent == shim.parents[2] / 'adapters' / sys.argv[2]
    assert module is importlib.import_module('adapters.' + sys.argv[2] + '.' + name)
"""
    result = subprocess.run([sys.executable, "-I", "-c", code, str(shim), transport],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
