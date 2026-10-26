"""Source inventory data cannot hide executable tick references or unknown fields."""
import copy
import json
from pathlib import Path

import pytest

from test_c1_export_gates import _gates

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def inventory_gate(monkeypatch, tmp_path):
    gates = _gates()
    for name in ("mcs/ingest", "mcs/notify", "mcs_standalone", "deployment"):
        (tmp_path / name).mkdir(parents=True)
    monkeypatch.setattr(gates, "ROOT", tmp_path)
    monkeypatch.setattr(gates, "MCS", tmp_path / "mcs")
    inventory = json.loads((ROOT / "deployment/sdc-release-owner.json").read_text())
    assert any(gates._EXT_REFERENCE.search(row["path"]) for row in inventory["files"])
    return gates, tmp_path, inventory


def test_closed_inventory_paths_are_data_but_tick_references_still_fail(inventory_gate):
    gates, root, inventory = inventory_gate
    (root / "deployment/sdc-release-owner.json").write_text(json.dumps(inventory))
    assert gates.gate_tick_no_ext() == []
    (root / "deployment/tick.json").write_text('{"command":"python -m c1_records"}')
    assert gates.gate_tick_no_ext() == ["tick.json:1 references C1 export module"]


@pytest.mark.parametrize("change", ["schema", "extra", "row_command", "duplicate", "unsafe", "mode"])
def test_malformed_or_command_bearing_inventory_is_denied(inventory_gate, change):
    gates, root, original = inventory_gate
    value = copy.deepcopy(original)
    if change == "schema":
        value["schema_version"] = "runtime-config.v1"
    elif change == "extra":
        value["command"] = "python -m c1_records"
    elif change == "row_command":
        value["files"][0]["command"] = "python -m c1_records"
    elif change == "unsafe":
        value["files"][0]["path"] = "mcs/../c1_records.py"
    elif change == "mode":
        value["files"][0]["mode"] = "120000"
    raw = json.dumps(value)
    if change == "duplicate":
        raw = raw.replace("{", '{"command":"python -m c1_records",', 1)
        raw = raw[:-1] + ',"command":"ignored duplicate"}'
    (root / "deployment/sdc-release-owner.json").write_text(raw)
    assert gates.gate_tick_no_ext() == ["sdc-release-owner.json: invalid release source inventory"]
