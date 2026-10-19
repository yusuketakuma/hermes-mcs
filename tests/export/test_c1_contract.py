"""Pure C1 boundary tests with fixed numeric and unchanged legacy byte goldens."""

import c1_envelopes
import export_contract
import export_delivery
import export_schema
import copy
import hashlib
import math
from decimal import Decimal
import ast
import json
from pathlib import Path

import pytest

import c1_contract as c1
import c1_envelopes as envs
import c1_receiver
import ext_contract as ext
from test_c1_delivery import authorization  # noqa: F401  (pytest fixture)
from test_c1_envelopes import _build, _records
from test_ext_contract import NOW, _auth


# Expected strings are fixed independently of the serializer under test.
NUMBER_CASES = [
    (0, "0"), (-0.0, "0"), (1.0, "1"), (-1.0, "-1"),
    (1_800_000_000.0, "1800000000"), (0.000001, "0.000001"),
    (-0.000001, "-0.000001"), (0.0000010000000000000002, "0.0000010000000000000002"),
    (0.00001, "0.00001"), (0.1, "0.1"), (1.5, "1.5"),
    (1.2345678901234567, "1.2345678901234567"),
    (1.0000000000000002, "1.0000000000000002"),
    (9007199254740991, "9007199254740991"),
    (-9007199254740991, "-9007199254740991"),
    (9007199254740991.0, "9007199254740991"),
]


def _record(kind, **values):
    return {"type": kind, "contract": "mcs-read-model/1",
            "snapshot_generation_id": "synthetic-generation", **values}


def _message(**values):
    return _record("message", **{
        "project_id": 1, "message_id": 1001, "body_state": "full",
        "content_hash": None, "posted_at_ts": None, "parent_id": None,
        "facts": [], "relations": [], **values})


def _body(text="SYNTHETIC", **values):
    return _record("message_body", **{
        "project_id": 1, "message_id": 1001, "body_text": text,
        "body_format": "text", "body_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "body_truncated": False, "sender_kind": "unknown", **values})


def _profile(**values):
    return {"fields": list(c1.C1_FIELDS), "patients": "all",
            "max_snapshot_age_s": 3600, "retention_days": 30, **values}


def _coverage():
    return _record("coverage", coverage={
        "collection": {"patients": None, "messages": 0, "deleted": 0,
                       "extraction_eligible": None, "patients_incomplete": None},
        "extraction": None, "attachments": None})


@pytest.mark.parametrize("value,expected", NUMBER_CASES)
def test_canonical_numbers_have_fixed_expected_bytes(value, expected):
    assert c1.canonical_json(value) == expected


@pytest.mark.parametrize("value", [
    float("nan"), float("inf"), -float("inf"), 1e21, -1e21,
    1e-7, -1e-7, math.nextafter(1e-6, 0), math.nextafter(-1e-6, 0),
    9007199254740992, -9007199254740992, 9007199254740992.0,
    10**1000,
])
def test_canonical_rejects_out_of_domain_numbers(value):
    with pytest.raises(c1.C1ContractError, match="number_domain_invalid"):
        c1.canonical_json(value)


def test_canonical_preserves_array_order_and_unicode_code_point_key_order():
    value = {"z": [True, None, -0.0, 1.5], "\U0001f600": "\x00\n\u00e9", "\ue000": "x"}
    before = copy.deepcopy(value)
    result = c1.canonical_json(value)
    assert result == '{"z":[true,null,0,1.5],"\ue000":"x","\U0001f600":"\\u0000\\n\u00e9"}'
    assert value == before


@pytest.mark.parametrize("value", ["\ud800", "\udfff", {"\ud800": 1}, ["\udfff"]])
def test_canonical_rejects_invalid_unicode(value):
    with pytest.raises(c1.C1ContractError, match="unicode_invalid"):
        c1.canonical_json(value)


@pytest.mark.parametrize("value", [(1, 2), {1: "x"}, {1, 2}, b"x", Decimal("1.5")])
def test_canonical_rejects_non_json_types(value):
    with pytest.raises(c1.C1ContractError, match="canonical_type_invalid"):
        c1.canonical_json(value)


def test_canonical_rejects_cycles_with_a_fixed_code():
    value = []
    value.append(value)
    with pytest.raises(c1.C1ContractError, match="canonical_nesting_invalid"):
        c1.canonical_json(value)


def test_old_bytes_golden_and_default_grant_remain_separate():
    value = {"x": 1800000000.0, "small": 0.000001, "zero": -0.0}
    assert export_contract.canonical_text(value) == '{"small":1e-06,"x":1800000000.0,"zero":-0.0}'
    assert c1.canonical_json(value) == '{"small":0.000001,"x":1800000000,"zero":0}'
    assert set(export_schema.RECORD_TYPES) == {
        "meta", "coverage", "stat", "signal", "signals_truncated", "attachment", "message"}
    with pytest.raises(export_contract.ContractError, match="forbidden_field:body_text"):
        export_contract._check_record_keys(_body())


@pytest.mark.parametrize("age,retention", [(0, 1), (1e-6, 30), (3600, 30)])
def test_explicit_profile_is_nonmutating(age, retention):
    profile = _profile(max_snapshot_age_s=age, retention_days=retention,
                       fields=list(reversed(c1.C1_FIELDS)))
    before = copy.deepcopy(profile)
    assert c1.validate_profile(profile) is None
    assert profile == before


@pytest.mark.parametrize("over", [
    {"fields": None}, {"fields": []}, {"fields": list(export_schema.RECORD_TYPES)},
    {"fields": ["meta"] * 7}, {"fields": list(c1.C1_FIELDS[:-1]) + [None]},
    {"patients": None}, {"patients": []}, {"patients": [1]},
    {"max_snapshot_age_s": None}, {"max_snapshot_age_s": True},
    {"max_snapshot_age_s": -1}, {"max_snapshot_age_s": 3600.01},
    {"max_snapshot_age_s": float("inf")}, {"max_snapshot_age_s": 1e-7},
    {"retention_days": None}, {"retention_days": True}, {"retention_days": 1.5},
    {"retention_days": 0}, {"retention_days": 31}, {"unknown": None},
])
def test_profile_rejects_implicit_or_invalid_grants(over):
    with pytest.raises(c1.C1ContractError):
        c1.validate_profile(_profile(**over))


@pytest.mark.parametrize("key", [
    "fields", "patients", "max_snapshot_age_s", "retention_days"])
def test_profile_requires_every_policy_field(key):
    profile = _profile()
    del profile[key]
    with pytest.raises(c1.C1ContractError):
        c1.validate_profile(profile)


@pytest.mark.parametrize("text", ["", "SYNTHETIC", "\U0001f600" * 2048])
@pytest.mark.parametrize("truncated", [False, True])
def test_body_accepts_transmitted_bytes_and_empty_file_only_posts(text, truncated):
    body = _body(text, body_truncated=truncated)
    before = copy.deepcopy(body)
    assert c1.validate_record(body, message=_message()) is None
    assert body == before


@pytest.mark.parametrize("sender", [
    "self_org", "physician", "nurse", "care_manager",
    "other_professional", "patient_family", "unknown"])
def test_body_accepts_only_declared_sender_categories(sender):
    assert c1.validate_record(_body(sender_kind=sender), message=_message()) is None


@pytest.mark.parametrize("over", [
    {"body_text": None}, {"body_text": {"note": "SYNTHETIC"}},
    {"body_text": "\ud800"}, {"body_format": "html"}, {"body_format": None},
    {"body_sha256": None}, {"body_sha256": "0" * 64}, {"body_sha256": "A" * 64},
    {"body_truncated": None}, {"body_truncated": 1},
    {"sender_kind": None}, {"sender_kind": "pharmacist"}, {"sender_kind": []},
    {"project_id": None}, {"project_id": True}, {"project_id": 0},
    {"message_id": None}, {"message_id": True}, {"message_id": 1.5},
    {"content_omitted": None}, {"sender_name": "SYNTHETIC"},
    {"patient_name": None}, {"statement": "SYNTHETIC"},
    {"unknown": None},
])
def test_body_rejects_extra_null_and_invalid_fields(over):
    with pytest.raises(c1.C1ContractError):
        c1.validate_record(_body(**over), message=_message())


@pytest.mark.parametrize("truncated", [False, True])
def test_body_8193_utf8_bytes_is_rejected_even_with_truncation_flag(truncated):
    body = _body("\U0001f600" * 2048 + "x", body_truncated=truncated)
    with pytest.raises(c1.C1ContractError, match="body_too_large"):
        c1.validate_record(body, message=_message())


@pytest.mark.parametrize("key", [
    "type", "contract", "snapshot_generation_id", "project_id", "message_id",
    "body_text", "body_format", "body_sha256", "body_truncated", "sender_kind"])
def test_body_requires_each_field(key):
    body = _body()
    del body[key]
    with pytest.raises(c1.C1ContractError):
        c1.validate_record(body, message=_message())


@pytest.mark.parametrize("message", [
    None, _message(project_id=2), _message(message_id=1002),
    _message(snapshot_generation_id="another-generation"),
    _message(body_state="snippet"), _message(body_state="unknown"),
    _message(body_state="deleted"), _message(body_state=None),
    _message(project_id=True), _message(body_text="SYNTHETIC"),
])
def test_body_requires_the_same_full_message_project_and_generation(message):
    with pytest.raises(c1.C1ContractError):
        c1.validate_record(_body(), message=message)


@pytest.mark.parametrize("floor", [None, 0, 1790000000])
@pytest.mark.parametrize("coverage", [None, 0, 1790000000.5])
def test_patient_coverage_preserves_unknown_and_does_not_map_floor(floor, coverage):
    record = _record("patient_coverage", project_id=1, fetch_state="complete",
                     coverage_ts=coverage, history_floor=floor)
    before = copy.deepcopy(record)
    assert c1.validate_record(record) is None
    assert record == before


@pytest.mark.parametrize("over", [
    {"fetch_state": None}, {"fetch_state": "unknown"}, {"fetch_state": True},
    {"coverage_ts": -1}, {"coverage_ts": True}, {"coverage_ts": float("nan")},
    {"coverage_ts": 1e-7}, {"history_floor": -1}, {"history_floor": True},
    {"history_floor": 0.5}, {"history_floor": 9007199254740992},
    {"project_id": None}, {"project_id": True}, {"patient_name": None},
])
def test_patient_coverage_rejects_unmapped_or_invalid_fields(over):
    record = _record("patient_coverage", **{
        "project_id": 1, "fetch_state": "pending", "coverage_ts": None,
        "history_floor": None, **over})
    with pytest.raises(c1.C1ContractError):
        c1.validate_record(record)


@pytest.mark.parametrize("key", [
    "project_id", "fetch_state", "coverage_ts", "history_floor"])
def test_patient_coverage_requires_even_nullable_fields(key):
    record = _record("patient_coverage", project_id=1, fetch_state="incomplete",
                     coverage_ts=None, history_floor=None)
    del record[key]
    with pytest.raises(c1.C1ContractError):
        c1.validate_record(record)


def test_collection_coverage_preserves_unknown_without_mutating_old_schema():
    record = _coverage()
    before = copy.deepcopy(record)
    assert c1.validate_record(record) is None
    assert record == before
    with pytest.raises(export_contract.ContractError, match="record_field_not_exportable"):
        export_contract._check_record_keys(record)


@pytest.mark.parametrize("value", [True, -1, 0.5, 9007199254740992, "unknown"])
def test_collection_counts_are_nullable_safe_nonnegative_integers(value):
    record = _coverage()
    coverage = record["coverage"]
    assert isinstance(coverage, dict)
    collection = coverage["collection"]
    assert isinstance(collection, dict)
    collection["patients_incomplete"] = value
    with pytest.raises(c1.C1ContractError):
        c1.validate_record(record)


def test_coverage_rejects_unknown_nested_content():
    record = _coverage()
    coverage = record["coverage"]
    assert isinstance(coverage, dict)
    coverage["extraction"] = {"extract_v1": {"statement": "SYNTHETIC"}}
    with pytest.raises(c1.C1ContractError, match="forbidden_field:statement"):
        c1.validate_record(record)
    coverage["extraction"] = {"extract_v1": {"unlisted": "SYNTHETIC"}}
    with pytest.raises(c1.C1ContractError, match="record_field_not_exportable"):
        c1.validate_record(record)


@pytest.mark.parametrize("record", [
    _record("meta", snapshot={"generation_id": "synthetic-generation", "generated_at": 1.5}),
    _message(),
    _record("signal", project_id=1, signal_type="rx_period_expiry",
            evidence={"message_ids": [1001]}),
    _record("signals_truncated", total=0),
])
def test_remaining_recognized_records_use_the_existing_allowlist(record):
    assert c1.validate_record(record) is None


@pytest.mark.parametrize("record", [
    _record("meta", snapshot={"generation_id": "other", "generated_at": 1}),
    _record("meta", snapshot={"generation_id": "synthetic-generation", "generated_at": True}),
    _record("meta", snapshot={"generation_id": "synthetic-generation", "generated_at": None}),
    _record("signals_truncated", total=None), _record("signals_truncated", total=True),
    _record("stat"), _record("attachment"), _record("future"),
    _message(facts=[{"fact_id": "f1", "statement": "SYNTHETIC"}]),
    _message(body_state="null"), _message(content_omitted=None), _message(facts=None),
])
def test_required_states_and_nested_fields_do_not_gain_a_body_exemption(record):
    with pytest.raises(c1.C1ContractError):
        c1.validate_record(record)


# The /1 CLI shares the C1 contract constants, and reconcile --all exit codes stay as documented.
def test_receipt_contract_has_one_definition_and_receiver_does_not_import_ext():
    assert c1_envelopes.RECEIPT_CONTRACT is envs.RECEIPT_CONTRACT == "mcs-ext-receipt/1"
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
        assert export_delivery.load_authorization(path, NOW)["auth_id"] == "a" * length
    else:
        with pytest.raises(export_contract.ContractError, match="auth_invalid:auth_id"):
            export_delivery.load_authorization(path, NOW)


def test_reconcile_all_exits_1_with_a_refused_entry_and_still_lists_held(
        tmp_path, authorization, capsys):  # noqa: F811
    held, = _build(_records((10,)), now=1030.0)
    refused, = _build(_records((20,)), now=1030.0)
    sink = export_delivery.HandoffSink(tmp_path / "sink")
    exporter = export_delivery.GovernedExporter(tmp_path / "state")
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
