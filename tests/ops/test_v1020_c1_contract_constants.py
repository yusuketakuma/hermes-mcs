"""The /1 CLI shares the C1 contract constants, and reconcile --all exit codes stay as documented."""
import ast
import json
from pathlib import Path

import pytest

import c1_envelopes as envs
import c1_receiver
import ext_contract as ext
from test_c1_delivery import authorization  # noqa: F401
from test_c1_envelopes import _build, _records
from test_ext_contract import NOW, _auth


def test_receipt_contract_has_one_definition_and_receiver_does_not_import_ext():
    assert ext.RECEIPT_CONTRACT is envs.RECEIPT_CONTRACT == "mcs-ext-receipt/1"
    assert c1_receiver.RECEIPT_CONTRACT is envs.RECEIPT_CONTRACT
    tree = ast.parse(Path(c1_receiver.__file__).read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "ext_contract" not in imported


def test_withdraw_cli_choices_and_max_bytes_default_follow_c1_envelopes(tmp_path, capsys):
    with pytest.raises(SystemExit):
        ext.main(["withdraw", "--state", str(tmp_path), "--sink", str(tmp_path),
                  "--envelope-id", "0" * 24, "--reason", "not_a_reason"])
    err = capsys.readouterr().err
    assert all(r in err for r in envs.WITHDRAW_REASONS)
    # the default --max-bytes is not an explicit /2 option on a /1 deliver
    records = tmp_path / "r.jsonl"
    records.write_text("")
    code = ext.main(["deliver", "--auth", str(_auth(tmp_path)), "--state", str(tmp_path / "s"),
                     "--sink", str(tmp_path / "k"), "--records", str(records)])
    assert json.loads(capsys.readouterr().out).get("reason") != "c1_option_required"
    assert code == 1


@pytest.mark.parametrize("length,ok", [(envs.AUTH_ID_MAX, True), (envs.AUTH_ID_MAX + 1, False)])
def test_auth_id_cap_is_the_withdraw_cap(tmp_path, length, ok):
    path = _auth(tmp_path, auth_id="a" * length)
    if ok:
        assert ext.load_authorization(path, NOW)["auth_id"] == "a" * length
    else:
        with pytest.raises(ext.ContractError, match="auth_invalid:auth_id"):
            ext.load_authorization(path, NOW)


def test_reconcile_all_exits_1_with_a_refused_entry_and_still_lists_held(
        tmp_path, authorization, capsys):  # noqa: F811
    held, = _build(_records((10,)), now=1030.0)
    refused, = _build(_records((20,)), now=1030.0)
    sink = ext.HandoffSink(tmp_path / "sink")
    exporter = ext.GovernedExporter(tmp_path / "state")
    assert exporter.deliver(held, sink, auth_path=authorization)["status"] == "held"
    raw = json.loads(authorization.read_text())
    authorization.write_text(json.dumps({**raw, "revoked": True}))
    assert exporter.deliver(refused, sink, auth_path=authorization)["status"] == "refused"
    capsys.readouterr()
    base = ["reconcile", "--state", str(tmp_path / "state"), "--sink", str(sink.root),
            "--sink-kind", "handoff"]
    assert ext.main([*base, "--all"]) == 1
    statuses = {r["envelope_id"]: r["status"]
                for r in json.loads(capsys.readouterr().out)["results"]}
    assert statuses == {held["envelope_id"]: "held", refused["envelope_id"]: "refused"}
    assert ext.main([*base, "--envelope-id", held["envelope_id"]]) == 2
