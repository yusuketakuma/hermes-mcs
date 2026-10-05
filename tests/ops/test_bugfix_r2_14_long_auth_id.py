"""An auth_id the withdraw/1 directive cannot carry must never be exported."""
import json
from pathlib import Path

import pytest

import ext_contract as ext
from test_ext_contract_cli import _cli, _flags, fixed_clock, inputs  # noqa: F401


def test_long_auth_id_is_refused_before_export(inputs, capsys):  # noqa: F811
    path = Path(inputs["auth"])
    auth = json.loads(path.read_text())
    auth["auth_id"] = "a" * 257
    path.write_text(json.dumps(auth))
    code, result = _cli(capsys, ["deliver", "--sink-kind", "handoff", *_flags(inputs)])
    assert code != 0 and result["reason"] == "auth_invalid:auth_id"
    assert not list(Path(inputs["sink"]).rglob("*.json"))


def test_stage_withdrawal_removes_copy_even_if_directive_is_refused(tmp_path):
    sink = ext.HandoffSink(tmp_path / "sink")
    eid = "86e294983d15ad712e92405e"
    folder = sink.root / "envelopes"
    folder.mkdir(mode=0o700)
    (folder / f"{eid}.json").write_text('{"synthetic": true}')
    with pytest.raises(ext.ContractError):
        sink.stage_withdrawal(eid, "a" * 300, "synthetic")
    assert not (folder / f"{eid}.json").exists()
