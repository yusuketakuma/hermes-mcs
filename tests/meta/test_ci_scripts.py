"""Regression tests for the CI gates themselves — holes were found in
ci/gates.py (FIX-G1/G2), ci/mine_gates.py and scripts/development/update_readme.py
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


@pytest.mark.parametrize("module_name", ["mcs_worker", "mcs_adapter"])
@pytest.mark.parametrize("operation", ["api", "download", "cdp_json", "cdp_eval"])
def test_mcs_worker_guard_blocks_production_child(module_name, operation):
    import importlib
    module = importlib.import_module(module_name)
    # Even without the guard, zero timeout prevents a child from spawning.
    with pytest.raises(RuntimeError, match="live MCS/CDP workers are disabled"):
        module.bounded_call({"operation": operation}, timeout=0)


def test_shadow_driver_bootstraps_its_runtime_outside_repository(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import runpy, sys; runpy.run_path(sys.argv[1]); "
         "from semantic import llm_chat; "
         "from semantic_evaluation import run_shadow_e2e; "
         "assert callable(llm_chat) and callable(run_shadow_e2e)",
         str(ROOT / "scripts" / "development" / "semantic_shadow_e2e.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_readme_generator_bootstraps_outside_repository(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts/development/update_readme.py"), "--check"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


# ---------- FIX-UR1: generator failure must fail the run ----------


def test_update_readme_check_fails_when_generator_fails(monkeypatch, capsys):
    ur = _load("update_readme_check", "scripts/development/update_readme.py")
    monkeypatch.setitem(ur.GENERATORS, "signals", _boom)
    monkeypatch.setattr(sys, "argv", ["update_readme.py", "--check"])
    assert ur.main() == 1
    assert "signals" in capsys.readouterr().err


def test_update_readme_write_fails_without_partial_write(
        monkeypatch, tmp_path):
    ur = _load("update_readme_write", "scripts/development/update_readme.py")
    readme = tmp_path / "README.md"
    readme.write_text(
        "x\n<!-- BEGIN GENERATED:signals -->\nold\n"
        "<!-- END GENERATED:signals -->\n")
    monkeypatch.setattr(ur, "README", readme)
    monkeypatch.setattr(ur, "DEV_DOC", tmp_path / "absent.md")
    monkeypatch.setitem(ur.GENERATORS, "signals", _boom)
    monkeypatch.setattr(sys, "argv", ["update_readme.py"])
    assert ur.main() == 1
    assert "old" in readme.read_text()   # untouched — no partial write


# ---------- generated blocks moved out of README into docs/ ----------

def test_update_readme_check_flags_stale_dev_doc(monkeypatch, tmp_path):
    ur = _load("update_readme_dev_check", "scripts/development/update_readme.py")
    readme = tmp_path / "README.md"
    readme.write_text("no markers — user doc only\n")
    dev = tmp_path / "DEVELOPMENT.md"
    dev.write_text(
        "<!-- BEGIN GENERATED:signals -->\nSTALE\n"
        "<!-- END GENERATED:signals -->\n")
    monkeypatch.setattr(ur, "README", readme)
    monkeypatch.setattr(ur, "DEV_DOC", dev)
    monkeypatch.setitem(ur.GENERATORS, "signals", lambda: "FRESH")
    monkeypatch.setattr(sys, "argv", ["update_readme.py", "--check"])
    assert ur.main() == 1


def test_update_readme_write_regenerates_dev_doc(monkeypatch, tmp_path):
    ur = _load("update_readme_dev_write", "scripts/development/update_readme.py")
    monkeypatch.setattr(ur, "README", tmp_path / "absent.md")  # skipped
    dev = tmp_path / "DEVELOPMENT.md"
    dev.write_text(
        "<!-- BEGIN GENERATED:signals -->\nSTALE\n"
        "<!-- END GENERATED:signals -->\n")
    monkeypatch.setattr(ur, "DEV_DOC", dev)
    monkeypatch.setitem(ur.GENERATORS, "signals", lambda: "FRESH")
    monkeypatch.setattr(sys, "argv", ["update_readme.py"])
    assert ur.main() == 0
    assert "FRESH" in dev.read_text()


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


@pytest.mark.parametrize(("call", "allowed"), [
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


@pytest.mark.parametrize("decoy", [
    "# def test_synthetic_decoy(): pass\n",
    'text = "def test_synthetic_decoy(): pass"\n',
    '"""def test_synthetic_decoy(): pass"""\n',
])
def test_mine_gates_requires_test_definitions_instead_of_text(monkeypatch, tmp_path, decoy):
    mg = _load("ci_mine_real_defs", "ci/mine_gates.py")
    (tmp_path / "test_synthetic.py").write_text(
        decoy + "def test_synthetic_real(): pass\n"
        "class TestSynthetic:\n def test_synthetic_method(self): pass\n")
    monkeypatch.setattr(mg, "TESTS", tmp_path)
    assert mg.test_names() == {"test_synthetic_real", "test_synthetic_method"}


@pytest.mark.parametrize(("relative", "source", "allowed"), [
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


@pytest.mark.parametrize(("relative", "source", "allowed"), [
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


@pytest.mark.parametrize("filename", ["standalone.py", "runtime_compat.py"])
def test_independent_sdk_exception_keeps_core_and_ambient_secrets_blocked(monkeypatch, tmp_path, filename):
    gates = _load("ci_standalone_sdk", "ci/gates.py")
    adapters = tmp_path / "adapters"
    standalone = tmp_path / "mcs_standalone"
    path = adapters / "slack" / filename
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(gates, "ADAPTERS", adapters)
    monkeypatch.setattr(gates, "STANDALONE", standalone)
    monkeypatch.setattr(gates, "_py_files", lambda *args: [path])
    deferred = "def connect():\n from slack_sdk.web.async_client import AsyncWebClient\n"
    guard = ('import os\nasync def connect():\n'
             ' if "SLACK_CLIENT_ID" in os.environ and "SLACK_CLIENT_SECRET" in os.environ:\n'
             '  raise ValueError("slack_ambient_oauth_not_allowed")\n')
    path.write_text(deferred)
    assert gates.gate_stdlib_only() == []
    path.write_text("from slack_sdk.web.async_client import AsyncWebClient\n")
    assert gates.gate_stdlib_only()  # SDKs cannot load on core/plugin import
    path.write_text(guard)
    assert gates.gate_plugin_sandbox() == []
    path.write_text(guard + ' secret = os.environ["SLACK_BOT_TOKEN"]\n')
    assert gates.gate_plugin_sandbox()
    path.write_text(deferred)
    forbidden = adapters / "slack" / "actions.py"
    forbidden.write_text(deferred)
    monkeypatch.setattr(gates, "_py_files", lambda *args: [forbidden])
    assert gates.gate_stdlib_only()  # regular Hermes adapters gain no SDK ownership


@pytest.mark.parametrize(("source", "locked"), [
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


def test_suite_guard_denies_any_path_on_live_llm_authority():
    """/slots, /v1/models and a localhost spelling of the production
    llama-server are as live as the chat endpoint."""
    import bounded_http
    for url in ("http://127.0.0.1:8080/slots",
                "http://localhost:8080/v1/models"):
        with pytest.raises(RuntimeError, match="live LLM/Jev"):
            bounded_http.bounded_http_request(url, "GET", None, 1)


# ---------- module table: first sentence, name prefix only ----------

def test_update_readme_module_description_keeps_hyphenated_words(tmp_path):
    ur = _load("update_readme_docline", "scripts/development/update_readme.py")
    cases = {
        "a.py": ('"""Durable medication-event detail assessment."""',
                 "Durable medication-event detail assessment."),
        "b.py": ('"""Auto-metrics benchmark for the pipeline."""',
                 "Auto-metrics benchmark for the pipeline."),
        "c.py": ('"""MCS self-update — detection, apply, rollback."""',
                 "detection, apply, rollback."),
        "d.py": ('"""Semantic audit gates: code-level checks and\n'
                 'the status decision."""',
                 "Semantic audit gates: code-level checks and the status "
                 "decision."),
    }
    for name, (src, expected) in cases.items():
        path = tmp_path / name
        path.write_text(src + "\n")
        assert ur._clean(ur._mod_docline(path), strip_prefix=True) \
            == expected
