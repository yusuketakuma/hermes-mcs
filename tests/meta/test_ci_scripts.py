"""Regression tests for the CI gates themselves — holes were found in
ci/gates.py (FIX-G1/G2), ci/mine_gates.py and scripts/update_readme.py
(FIX-UR1). Each gate is imported as a module and driven against a
synthetic tree."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _boom():
    raise RuntimeError("generator exploded")


def test_shadow_driver_bootstraps_its_runtime_outside_repository(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import runpy, sys; runpy.run_path(sys.argv[1]); "
         "from semantic import llm_chat; "
         "from semantic_evaluation import run_shadow_e2e; "
         "assert callable(llm_chat) and callable(run_shadow_e2e)",
         str(ROOT / "scripts" / "semantic_shadow_e2e.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


# ---------- FIX-UR1: generator failure must fail the run ----------


def test_update_readme_check_fails_when_generator_fails(monkeypatch, capsys):
    ur = _load("update_readme_check", "scripts/update_readme.py")
    monkeypatch.setitem(ur.GENERATORS, "signals", _boom)
    monkeypatch.setattr(sys, "argv", ["update_readme.py", "--check"])
    assert ur.main() == 1
    assert "signals" in capsys.readouterr().err


def test_update_readme_write_fails_without_partial_write(
        monkeypatch, tmp_path):
    ur = _load("update_readme_write", "scripts/update_readme.py")
    readme = tmp_path / "README.md"
    readme.write_text(
        "x\n<!-- BEGIN GENERATED:signals -->\nold\n"
        "<!-- END GENERATED:signals -->\n")
    monkeypatch.setattr(ur, "README", readme)
    monkeypatch.setitem(ur.GENERATORS, "signals", _boom)
    monkeypatch.setattr(sys, "argv", ["update_readme.py"])
    assert ur.main() == 1
    assert "old" in readme.read_text()   # untouched — no partial write


# ---------- FIX-G1: sqlite connect must carry mode=ro on ITS line ------


def test_snapshot_gate_flags_connect_line_without_mode_ro(
        monkeypatch, tmp_path):
    gates = _load("ci_gates_ro", "ci/gates.py")
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import sqlite3\n"
        "from pathlib import Path\n"
        "uri = Path(p).as_uri() + '?mode=ro'\n"      # ro hidden elsewhere
        "db = sqlite3.connect(uri, uri=True)\n")     # connect lacks it
    ok = tmp_path / "ok.py"
    ok.write_text(
        "import sqlite3\n"
        "db = sqlite3.connect(Path(p).as_uri() + '?mode=ro', uri=True)\n")
    monkeypatch.setattr(gates, "_py_files", lambda *a: [bad, ok])
    bads = gates.gate_snapshot_readonly()
    assert any("bad.py" in b for b in bads)
    assert not any("ok.py" in b for b in bads)


@pytest.mark.parametrize("call,allowed", [
    ("sqlite3.connect(\n 'file:synthetic?mode=ro', uri=True)", True),
    ("sqlite3.connect(\n f'file:{path}?mode=ro', uri=True)", True),
    ("sqlite3.connect('file:synthetic?mode=ro')", False),
    ("sqlite3.connect(path, uri=True) # mode=ro", False),
    ("sqlite3.connect('file:synthetic?mode=rw&cache=mode=ro', uri=True)", False),
    ("sqlite3.connect('file:synthetic?mode=ro&mode=rw', uri=True)", False),
])
def test_snapshot_gate_checks_the_uri_argument(monkeypatch, tmp_path, call, allowed):
    gates = _load("ci_uri", "ci/gates.py")
    path = tmp_path / "reader.py"
    path.write_text("db = " + call + "\n")
    monkeypatch.setattr(gates, "_py_files", lambda *args: [path])
    assert bool(gates.gate_snapshot_readonly()) is not allowed


def test_platform_gates_distinguish_provisioning_and_uri_encoding(monkeypatch, tmp_path):
    gates = _load("ci_platform", "ci/gates.py")
    mcs = tmp_path / "mcs"
    setup = mcs / "ops" / "mcs_setup.py"
    setup.parent.mkdir(parents=True)
    setup.write_text('def _apply_plugin_integration():\n'
                     ' return os.environ.get("DISCORD_BOT_TOKEN")\n')
    monkeypatch.setattr(gates, "MCS", mcs)
    monkeypatch.setattr(gates, "_py_files", lambda *args: [setup])
    assert gates.gate_no_direct_platform_api() == []
    setup.write_text('def _hermes_config_set(key, value):\n'
                     ' return key == "DISCORD_BOT_TOKEN"\n')
    assert gates.gate_no_direct_platform_api() == []
    setup.write_text('def collector():\n return os.environ.get("DISCORD_BOT_TOKEN")\n')
    assert gates.gate_no_direct_platform_api()
    setup.write_text('def _apply_plugin_integration():\n return "https://discord.com/api"\n')
    assert gates.gate_no_direct_platform_api()
    for source, allowed in [("from urllib.parse import quote\n", True),
                            ("from urllib.request import urlopen\n", False),
                            ("import urllib\n", False)]:
        setup.write_text(source)
        assert bool(gates.gate_plugin_sandbox()) is not allowed


# ---------- FIX-G2 / mine_gates: nested records must be scanned --------


def test_records_isolation_audits_nested_files(monkeypatch, tmp_path):
    gates = _load("ci_gates_iso", "ci/gates.py")
    rec = tmp_path / "docs" / "dev-records" / "sub"
    rec.mkdir(parents=True)
    (rec / "evil.py").write_text("print(1)")
    monkeypatch.setattr(gates, "ROOT", tmp_path)
    assert any("evil.py" in b for b in gates.gate_records_isolation())


def test_mine_gates_extracts_ids_from_nested_records(monkeypatch, tmp_path):
    mg = _load("ci_mine_nested", "ci/mine_gates.py")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "record.md").write_text("fix: FIX-NEST9 landed here")
    monkeypatch.setattr(mg, "RECORDS", tmp_path)
    assert "FIX-NEST9" in mg.extract_ids()["defects"]


@pytest.mark.parametrize("relative,source,allowed", [
    ("mcs_discord/cards.py", "def make():\n import discord\n return discord.ui.View()\n", True),
    ("mcs_discord/cards.py", "import discord\n", False),
    ("other.py", "def make():\n import discord\n", False),
    ("mcs_discord/actions.py", "def make():\n import discord\n return discord.Client()\n", False),
    ("mcs_discord/actions.py", "def make():\n from discord import Client as Bot\n return Bot()\n", False),
    ("mcs_discord/actions.py", "def make():\n import discord as d\n return getattr(d, 'Client')()\n", False),
])
def test_sdk_gate_allows_only_lazy_host_ui(monkeypatch, tmp_path, relative, source, allowed):
    gates = _load("ci_sdk", "ci/gates.py")
    plugin = tmp_path / "plugin"
    path = plugin / relative
    path.parent.mkdir(parents=True)
    path.write_text(source)
    monkeypatch.setattr(gates, "PLUGIN", plugin)
    monkeypatch.setattr(gates, "_py_files", lambda *args: [path])
    assert bool(gates.gate_stdlib_only()) is not allowed


@pytest.mark.parametrize("relative,source,allowed", [
    ("mcs_discord/tasks.py", "import asyncio\nasync def poll():\n await asyncio.sleep(1)\n", True),
    ("other.py", "import asyncio\n", False),
    ("mcs_discord/actions.py", "import asyncio as a\nasync def run():\n await a.create_subprocess_exec('bad')\n", False),
    ("mcs_discord/delivery.py", "import asyncio\nasync def run():\n await asyncio.open_connection('host', 80)\n", False),
    ("mcs_discord/tasks.py", "import asyncio\nrun = getattr(asyncio, 'create_subprocess_exec')\n", False),
    ("mcs_discord/tasks.py", "from asyncio import create_subprocess_exec\n", False),
    ("mcs_discord/tasks.py", "import subprocess\n", False),
])
def test_adapter_async_gate_keeps_process_and_network_blocked(monkeypatch, tmp_path, relative, source, allowed):
    gates = _load("ci_async", "ci/gates.py")
    plugin = tmp_path / "plugin"
    path = plugin / relative
    path.parent.mkdir(parents=True)
    path.write_text(source)
    monkeypatch.setattr(gates, "PLUGIN", plugin)
    monkeypatch.setattr(gates, "_py_files", lambda *args: [path])
    assert bool(gates.gate_plugin_sandbox()) is not allowed


@pytest.mark.parametrize("source,locked", [
    ('"acquire_run_lock"\ndef write():\n return Ledger("synthetic.db")\n', False),
    ('def unused():\n acquire_run_lock()\n'
     'def write():\n return Ledger("synthetic.db")\n', False),
    ('def write():\n return Ledger("synthetic.db")\n'
     'def main():\n write()\n'
     'def locked():\n acquire_run_lock()\n write()\n', False),
    ('def write():\n acquire_run_lock()\n'
     ' return Ledger("synthetic.db")\n', True),
    ('def write():\n return Ledger("synthetic.db")\n'
     'def main():\n acquire_run_lock()\n return write()\n', True),
    ('def _lock():\n return acquire_run_lock()\n'
     'def write():\n return ledger.Ledger("synthetic.db")\n'
     'def main():\n _lock()\n return write()\n', True),
])
def test_writer_gate_checks_lock_on_each_caller_path(
        monkeypatch, tmp_path, source, locked):
    gates = _load("ci_writer", "ci/gates.py")
    path = tmp_path / "synthetic_writer.py"
    path.write_text(source)
    monkeypatch.setattr(gates, "_py_files", lambda *args: [path])
    assert bool(gates.gate_writer_lock()) is not locked
