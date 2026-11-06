"""Adapter packages resolve to the checkout however the host names the plugin."""
from pathlib import Path

import pytest


def test_lineworks_cli_entry_is_only_the_module_wrapper():
    import importlib.util
    import lineworks_adapter

    assert not hasattr(lineworks_adapter, "LineWorksClient")
    assert Path(importlib.util.find_spec("lineworks_adapter.__main__").origin) == (
        Path(__file__).resolve().parents[3] / "lineworks_adapter" / "__main__.py")


@pytest.mark.parametrize("relative,source,expected", [
    ("discord/cards.py", "def view():\n    import discord\n    return discord.ui.LayoutView()\n", False),
    ("discord/cards.py", "def view():\n    import discord\n    return discord.ui.MediaGallery()\n", False),
    ("discord/cards.py", "def view():\n    import discord\n    return discord.ui.File('attachment://synthetic.pdf')\n", False),
    ("discord/cards.py", "def view():\n    import discord\n    return discord.MediaGalleryItem('attachment://synthetic.png')\n", False),
    ("discord/delivery.py", "def file():\n    import discord\n    return discord.File('synthetic.pdf')\n", True),
    ("discord/cards.py", "def bot():\n    import discord\n    return discord.Client()\n", True),
    ("discord/new.py", "def view():\n    import discord\n    return discord.ui.LayoutView()\n", True),
    ("slack/delivery.py", "import socket\n", True),
    ("common/worker.py", "import asyncio\nasync def wait():\n    await asyncio.sleep(0)\n", False),
    ("common/commands.py", "import asyncio\nasync def run():\n    task = asyncio.create_task(asyncio.to_thread(str, 1))\n    await asyncio.wait({task})\n", False),
    ("common/commands.py", "import asyncio\nasync def run():\n    await asyncio.open_connection('synthetic', 1)\n", True),
    ("common/commands.py", "from asyncio import create_task\n", True),
    ("common/forms.py", "import asyncio\nasync def run():\n    await asyncio.wait({})\n", True),
    ("common/worker.py", "import asyncio\nasync def connect():\n    await asyncio.open_connection('synthetic', 1)\n", True),
    ("common/paths.py", "import socket\n", True),
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


def test_plugin_under_host_package_name_binds_checkout_adapters(tmp_path):
    """Hermes discovers the symlinked plugin as `hermes_plugins.mcs`, with
    no checkout on sys.path. The card factories must still bind the
    repo's `adapters.*` modules (no plugin-local alias packages)."""
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[3]
    code = """
import importlib, importlib.util, json, sys, types
from pathlib import Path
repo, data_root = Path(sys.argv[1]), Path(sys.argv[2])
for sdk in ('discord', 'slack_sdk', 'slack_bolt'):
    sys.modules[sdk] = None
try:
    import adapters  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    raise AssertionError('checkout leaked onto sys.path')
plugin = repo / 'hermes_plugin' / '__init__.py'
sys.modules['hermes_plugins'] = types.ModuleType('hermes_plugins')
spec = importlib.util.spec_from_file_location(
    'hermes_plugins.mcs', plugin, submodule_search_locations=[str(plugin.parent)])
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
workers = importlib.import_module('hermes_plugins.mcs.card_workers')
assert 'adapters' not in sys.modules, 'plugin import must stay lazy'

class Ctx:
    profile_name = 'default'
    def __init__(self, config):
        self.config = config
    def get_config(self, key, default=None):
        return self.config.get(key, default)

# Command invocation also bootstraps the shared implementation, with no SDK.
assert json.loads(mod._make_handler(Ctx({}))("{}")) == {
    "ok": False, "error": "native_context_required"}
assert Path(sys.modules['adapters.common.dispatch'].__file__).resolve() == (
    repo / 'adapters' / 'common' / 'dispatch.py')
assert 'hermes_plugin' not in sys.modules

flags = data_root / 'flags' / 'notify.json'
flags.parent.mkdir(parents=True)
flags.write_text(json.dumps({'interactive': False}))
base = {'data_root': str(data_root), 'snapshot': 'synthetic-snapshot',
        'inbox': 'synthetic-inbox', 'allowed_user_ids': ['111'],
        'allowed_chat_ids': ['222'], 'project_ids': [1]}
slack = Ctx({**base, 'slack_adapter_enabled': True, 'slack_team_id': 'T_SYNTHETIC',
             'slack_application_id': 'A_SYNTHETIC', 'slack_channel_id': 'C_SYNTHETIC',
             'slack_allowed_user_ids': ['111']})
# flags say not interactive: the factory bootstraps and reads them, binds nothing
assert workers.make_slack_factory(slack)(object(), None) is None
paths = sys.modules['adapters.common.paths']
assert Path(paths.__file__).resolve() == repo / 'adapters' / 'common' / 'paths.py'

bound = []
class Fake:
    def __init__(self, **kwargs):
        bound.append(kwargs)
    def start(self):
        return True
for transport in ('slack', 'discord'):
    tasks = importlib.import_module('adapters.%s.tasks' % transport)
    assert Path(tasks.__file__).resolve() == repo / 'adapters' / transport / 'tasks.py'
    tasks.Supervisor = Fake
flags.write_text(json.dumps({'interactive': True, 'transport': 'slack'}))
assert isinstance(workers.make_slack_factory(slack)(object(), None), Fake)
discord = Ctx({**base, 'interactive': True, 'application_id': '333', 'channel_id': '444',
               'guild_id': '555'})
native = types.SimpleNamespace(user=types.SimpleNamespace(id=333), application_id=333)
flags.write_text(json.dumps({'interactive': True, 'transport': 'discord', 'route_epoch': 3}))
assert isinstance(workers.make_discord_factory(discord)(native, None), Fake)
assert len(bound) == 2
assert bound[-1]['settings']['route_epoch'] == 3
assert not [name for name in sys.modules if name.startswith('hermes_plugins.mcs.mcs_')]
"""
    data_root = tmp_path / "data"
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(repo), str(data_root)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
