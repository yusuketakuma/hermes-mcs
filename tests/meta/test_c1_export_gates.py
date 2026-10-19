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
    exports = tmp_path / "mcs" / "export"
    exports.mkdir()
    (ops / "ext_contract.py").write_text("import json\nfrom urllib.request import urlopen\n")
    (exports / "c1_records.py").write_text("import subprocess\n")
    (exports / "c1_future.py").write_text("import socket\n")  # new c1_* is covered
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



def test_extracted_export_owners_remain_offline_and_outside_tick(monkeypatch, tmp_path):
    gates = _gates()
    exports = tmp_path / "mcs" / "export"
    exports.mkdir(parents=True)
    (exports / "export_contract.py").write_text("import socket\n")
    (exports / "export_delivery.py").write_text("import subprocess\n")
    ingest = tmp_path / "mcs" / "ingest"
    ingest.mkdir()
    (ingest / "tick.py").write_text("import export_contract\nfrom export_delivery import health\n")
    for root in ("notify",):
        (tmp_path / "mcs" / root).mkdir()
    (tmp_path / "mcs_standalone").mkdir()
    (tmp_path / "deployment").mkdir()
    monkeypatch.setattr(gates, "MCS", tmp_path / "mcs")
    monkeypatch.setattr(gates, "ROOT", tmp_path)
    assert gates.gate_ext_contract_offline() == [
        "export_contract.py:1 imports socket", "export_delivery.py:1 imports subprocess"]
    assert gates.gate_tick_no_ext() == [
        "tick.py:1 references C1 export module", "tick.py:2 references C1 export module"]
