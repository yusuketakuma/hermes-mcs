"""Canonical promotion consumes only a complete regular evidence file."""
import mcs_util
import service_management
import json
import os
from types import SimpleNamespace

import pytest

import mcs_setup
from ops_testkit import bounded_python


@pytest.mark.parametrize("kind", ["oversized", "fifo", "symlink"])
def test_invalid_evidence_never_reaches_gate_or_config_write(tmp_path, monkeypatch, kind):
    evidence = tmp_path / "gate.json"
    raw = json.dumps({"schema_version": 1, "gate": {"pass": True,
                     "g6_eligible": True, "reasons": []},
                      "label_provenance": {"human": 1}}).encode()
    if kind == "oversized":
        monkeypatch.setattr(mcs_setup, "EVALUATION_INPUT_BYTES", len(raw))
        evidence.write_bytes(raw + b" ")
    elif kind == "fifo":
        os.mkfifo(evidence)
        bounded_python(tmp_path, """
from types import SimpleNamespace
import mcs_setup
import mcs_util
import service_management
mcs_setup._guard_config = lambda yes: True
mcs_util.load_config = lambda: {}
def forbidden(*args, **kwargs):
    raise SystemExit('unsafe evidence reached promotion or write')
mcs_setup._shipped_g6_criteria = forbidden
service_management.write_service_file = forbidden
assert mcs_setup.cmd_fact_source(SimpleNamespace(
    fact_source='canonical', gate_evidence=sys.argv[1])) == 1
""", evidence)
        return
    else:
        target = tmp_path / "target.json"
        target.write_bytes(raw)
        evidence.symlink_to(target)
    monkeypatch.setattr(mcs_setup, "_guard_config", lambda yes: True)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})

    def forbidden(*args, **kwargs):
        pytest.fail("unsafe evidence reached promotion or write")

    monkeypatch.setattr(mcs_setup, "_shipped_g6_criteria", forbidden)
    monkeypatch.setattr(service_management, 'write_service_file', forbidden)
    assert mcs_setup.cmd_fact_source(SimpleNamespace(
        fact_source="canonical", gate_evidence=str(evidence))) == 1
