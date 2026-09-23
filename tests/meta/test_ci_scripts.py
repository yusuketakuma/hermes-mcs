"""Regression tests for the CI gates themselves — holes were found in
ci/gates.py (FIX-G1/G2), ci/mine_gates.py and scripts/update_readme.py
(FIX-UR1). Each gate is imported as a module and driven against a
synthetic tree."""
import importlib.util
import subprocess
import sys
from pathlib import Path

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
