"""Regressions: refused journal entries, legacy outbox receipts, malformed records."""
import json

import pytest

import ext_contract as ext
from test_c1_cli import world  # noqa: F401
from test_c1_delivery import authorization  # noqa: F401
from test_c1_envelopes import _build, _records
from test_c1_withdraw_receive import _cli, _bundle, staged  # noqa: F401
from test_ext_contract import NOW, _auth


def test_reconcile_all_skips_refused_entry(tmp_path, authorization, capsys):  # noqa: F811
    a, = _build(_records((10,)), now=1030.0)
    b, = _build(_records((20,)), now=1030.0)
    sink = ext.HandoffSink(tmp_path / "sink")
    ex = ext.GovernedExporter(tmp_path / "state")
    assert ex.deliver(a, sink, auth_path=authorization)["status"] == "held"
    raw = json.loads(authorization.read_text())
    raw["revoked"] = True
    authorization.write_text(json.dumps(raw))
    assert ex.deliver(b, sink, auth_path=authorization)["status"] == "refused"
    assert ex.reconcile(b["envelope_id"], sink)["status"] == "refused"
    capsys.readouterr()
    ext.main(["reconcile", "--state", str(tmp_path / "state"), "--sink", str(sink.root),
              "--sink-kind", "handoff", "--all"])
    out = capsys.readouterr().out
    assert "sink_binding_mismatch" not in out
    assert a["envelope_id"] in out


def test_receive_skips_legacy_receipt_in_outbox(staged, tmp_path, capsys):  # noqa: F811
    state, sink, eids = staged
    _, out = _cli(capsys, ["withdraw", "--state", str(state), "--sink", str(sink),
                           "--envelope-id", eids[0]])
    assert out["status"] == "delete_held"
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"envelope_id": eids[0], "deleted_at": 1031}))
    _, out = _cli(capsys, ["reconcile", "--state", str(state), "--sink", str(sink),
                           "--receipts", str(legacy)])
    _, out = _cli(capsys, ["receive", "--receiver-root", str(tmp_path / "r"),
                           "--source-label", "s", "--input", str(sink),
                           "--receipts-out", str(tmp_path / "x.ndjson")])
    assert "rejected" not in out["transport"]
    assert not [r for r in _bundle(tmp_path / "x.ndjson")
                if r["envelope_id"] == eids[0] and r.get("status") == "rejected"]


@pytest.mark.parametrize("line", ['{"type":"meta","snapshot":null}', '[]'])
def test_malformed_records_refused(tmp_path, monkeypatch, capsys, line):
    monkeypatch.setattr(ext.time, "time", lambda: NOW)
    f = tmp_path / "f.jsonl"
    f.write_text(line + "\n")
    code = ext.main(["deliver", "--auth", str(_auth(tmp_path)), "--state", str(tmp_path / "s"),
                     "--sink", str(tmp_path / "k"), "--records", str(f)])
    assert code == 1 and json.loads(capsys.readouterr().out)["status"] == "refused"
