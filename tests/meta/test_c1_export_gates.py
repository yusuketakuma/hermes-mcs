"""C1 export-layer gates: offline contract modules, tick path never loads them."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _gates():
    spec = importlib.util.spec_from_file_location("ci_gates_c1", ROOT / "ci" / "gates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_c1_gates_registered_and_clean():
    gates = _gates()
    assert gates.GATES["ext_contract_offline"] is gates.gate_ext_contract_offline
    assert gates.GATES["tick_no_ext"] is gates.gate_tick_no_ext
    assert gates.gate_ext_contract_offline() == []
    assert gates.gate_tick_no_ext() == []


def test_c1_gates_flag_synthetic_violations(monkeypatch, tmp_path):
    gates = _gates()
    ops = tmp_path / "mcs" / "ops"
    ops.mkdir(parents=True)
    (ops / "ext_contract.py").write_text("import json\nfrom urllib.request import urlopen\n")
    (ops / "c1_records.py").write_text("import subprocess\n")
    (ops / "c1_future.py").write_text("import socket\n")  # new c1_* is covered
    (ops / "brain_export.py").write_text("import subprocess\n")  # not C1 layer
    ingest = tmp_path / "mcs" / "ingest"
    ingest.mkdir()
    (ingest / "run_check.py").write_text("import c1_records\nnext_page = 1\n")
    (tmp_path / "mcs" / "notify").mkdir()
    standalone = tmp_path / "mcs_standalone"
    standalone.mkdir()
    (standalone / "jobs.py").write_text("from c1_future import x\n")
    deploy = tmp_path / "deployment"
    deploy.mkdir()
    (deploy / "job.sh").write_text("python3 mcs/ops/ext_contract.py deliver\n")
    monkeypatch.setattr(gates, "MCS", tmp_path / "mcs")
    monkeypatch.setattr(gates, "ROOT", tmp_path)
    offline = gates.gate_ext_contract_offline()
    assert offline == ["c1_future.py:1 imports socket",
                       "c1_records.py:1 imports subprocess",
                       "ext_contract.py:2 imports urllib"]
    tick = gates.gate_tick_no_ext()
    assert sorted(tick) == ["job.sh:1 references C1 export module",
                            "jobs.py:1 references C1 export module",
                            "run_check.py:1 references C1 export module"]
