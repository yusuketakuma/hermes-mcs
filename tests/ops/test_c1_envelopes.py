"""Synthetic /2 wire, grouping and collection tests with independent hash vectors."""
from collections import Counter
from copy import deepcopy
import hashlib
import json

import pytest

import c1_envelopes as envs
from c1_contract import C1ContractError, C1_FIELDS
import ext_contract as legacy

GEN = "gen-envelope-synth"
SHARED = {"meta", "coverage", "patient_coverage", "signals_truncated"}


def _record(kind, **values):
    return {"type": kind, "contract": "mcs-read-model/1",
            "snapshot_generation_id": GEN, **values}


def _records(lengths=()):
    records = [
        _record("meta", snapshot={"generation_id": GEN, "generated_at": 1000}),
        _record("coverage", coverage={
            "collection": {"patients": 0 if not lengths else 2, "messages": len(lengths),
                           "deleted": 0, "extraction_eligible": len(lengths),
                           "patients_incomplete": None},
            "extraction": None, "attachments": None})]
    if lengths:
        records += [
            _record("patient_coverage", project_id=1, fetch_state="complete",
                    coverage_ts=None, history_floor=None),
            _record("patient_coverage", project_id=2, fetch_state="pending",
                    coverage_ts=None, history_floor=0),
            _record("signals_truncated", total=12)]
    for mid, length in enumerate(lengths, 1001):
        records.append(_record(
            "message", project_id=1, message_id=mid, body_state="full",
            posted_at_ts=None, content_hash=None, parent_id=None, facts=[], relations=[]))
        body = "x" * length
        records.append(_record(
            "message_body", project_id=1, message_id=mid, body_text=body,
            body_format="text", body_sha256=hashlib.sha256(body.encode()).hexdigest(),
            body_truncated=False, sender_kind="unknown"))
    return records


def _profile(**values):
    return {"fields": list(C1_FIELDS), "patients": "all",
            "max_snapshot_age_s": 3600, "retention_days": 30, **values}


def _build(records, **values):
    return envs.build_envelopes(records, **{
        "auth_id": "auth-c1", "destination": "synthetic-local", "purpose": "synthetic-review",
        "profile": _profile(), "now": 1030, **values})


def _wire(value):
    # Independent oracle for these integer-only fixtures, not c1's serializer.
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_wire(value)).hexdigest()


def _resign(envelope):
    envelope["record_count"] = len(envelope["records"])
    envelope["records_sha256"] = _hash(envelope["records"])
    envelope["envelope_id"] = _hash({key: envelope.get(key) for key in (
        "contract", "auth_id", "snapshot_generation_id", "records_sha256", "part")})[:24]


def _signed(records, part=None):
    envelope = {
        "contract": "mcs-ext-export/2", "auth_id": "auth-c1", "destination": "synthetic-local",
        "purpose": "synthetic-review", "scope": "aggregate", "retention_days": 30,
        "created_at": 1030, "snapshot_generation_id": GEN, "snapshot_generated_at": 1000,
        "records": deepcopy(records)}
    if part is not None:
        envelope["part"] = part
    _resign(envelope)
    return envelope


def _resign_collection(envelopes):
    for envelope in envelopes:
        _resign(envelope)
    set_id = _hash([e["records_sha256"] for e in envelopes])
    for envelope in envelopes:
        envelope["part"]["set"] = set_id
        _resign(envelope)


def _split_inputs():
    records = _records((4000, 2000, 2000))
    shared = [r for r in records if r["type"] in SHARED]
    first = [r for r in records if r.get("message_id") == 1001]
    rest = [r for r in records if r.get("message_id") in (1002, 1003)]
    narrow = len(_wire(_signed(shared + first, {"index": 1, "count": 3, "set": "0" * 64})))
    wide = len(_wire(_signed(shared + rest, {"index": 2, "count": 2, "set": "0" * 64})))
    return records, narrow, wide


def test_heartbeat_has_no_part_and_matches_fixed_independent_bun_hashes():
    envelope = _build(_records())[0]
    assert "part" not in envelope
    assert envelope["records_sha256"] == \
        "a426ab5b5acd3f5893b67cf9a483d31977344c024d315e5cb28530377f8d04f9"
    assert envelope["envelope_id"] == "ca6923514a1631732be75897"
    assert envs.intent_hash(envelope) == \
        "4bb2fe5ddeac2970bad468d76f65c3376bacbccbaad6a5aad756c603c61edc48"
    assert envs.parse_envelope(envs.encode_envelope(envelope)) == envelope
    assert envs.validate_collection([envelope])["status"] == "complete"


def test_exact_wire_byte_limit_is_checked_before_parse_not_after_reserialization():
    envelope = _build(_records())[0]
    wire = envs.encode_envelope(envelope)
    padded = wire + b" " * (1_048_576 - len(wire))
    assert envs.parse_envelope(padded) == envelope
    with pytest.raises(C1ContractError, match="envelope_too_large"):
        envs.parse_envelope(padded + b" ")
    # An oversized malformed document is refused on size before JSON parsing.
    with pytest.raises(C1ContractError, match="envelope_too_large"):
        envs.parse_envelope(b"{" * 1_048_577)


def test_canonical_envelope_exactly_one_mib_is_accepted_and_plus_one_refused():
    envelope = _build(_records())[0]
    envelope["purpose"] += "x" * (1_048_576 - len(_wire(envelope)))
    wire = envs.encode_envelope(envelope)
    assert len(wire) == 1_048_576
    assert envs.parse_envelope(wire) == envelope
    envelope["purpose"] += "x"
    with pytest.raises(C1ContractError, match="envelope_too_large"):
        envs.encode_envelope(envelope)


@pytest.mark.parametrize("raw", [b"", b"{", b"null", b"[]", b"\xff", b'{"x":"\\ud800"}', "{}"])
def test_invalid_wire_never_returns_an_envelope(raw):
    with pytest.raises(C1ContractError):
        envs.parse_envelope(raw)


@pytest.mark.parametrize("budget", [True, 0, -1, 1000.5, 1_048_577])
def test_split_budget_cannot_disable_or_widen_the_hard_limit(budget):
    with pytest.raises(C1ContractError, match="max_bytes_invalid"):
        _build(_records(), max_bytes=budget)


def test_actual_utf8_bytes_not_character_counts_control_unsplit_boundary():
    kwargs = {"purpose": "\u00e9" * 80}
    expected = _build(_records(), **kwargs)[0]
    size = len(_wire(expected))
    assert len(json.dumps(expected, ensure_ascii=False, separators=(",", ":"))) < size
    assert _build(_records(), max_bytes=size, **kwargs) == [expected]
    with pytest.raises(C1ContractError, match="shared_records_too_large"):
        _build(_records(), max_bytes=size - 1, **kwargs)


def test_greedy_split_is_deterministic_paired_exclusive_and_nonmutating():
    records, _, budget = _split_inputs()
    before = deepcopy(records)
    envelopes = _build(records, max_bytes=budget)
    assert len(envelopes) == 2
    assert envelopes == _build(records, max_bytes=budget)
    assert records == before
    expected_shared = [r for r in records if r["type"] in SHARED]
    actual = []
    for index, envelope in enumerate(envelopes, 1):
        assert envelope["part"]["index"] == index and envelope["part"]["count"] == 2
        assert len(envs.encode_envelope(envelope)) <= budget
        assert [r for r in envelope["records"] if r["type"] in SHARED] == expected_shared
        messages = {r["message_id"] for r in envelope["records"] if r["type"] == "message"}
        bodies = {r["message_id"] for r in envelope["records"] if r["type"] == "message_body"}
        assert messages == bodies
        actual.extend(r for r in envelope["records"] if r["type"] not in SHARED)
    assert Counter(_wire(r) for r in actual) == Counter(
        _wire(r) for r in records if r["type"] not in SHARED)
    assert envs.validate_collection(list(reversed(envelopes)))["status"] == "complete"
    envelopes[0]["records"][0]["snapshot"]["generated_at"] = 10
    assert records == before and envelopes[1]["records"][0] == before[0]


def test_repartitioned_identical_first_records_have_distinct_ids_and_intents():
    records, narrow, wide = _split_inputs()
    three, two = _build(records, max_bytes=narrow), _build(records, max_bytes=wide)
    assert len(three) == 3 and len(two) == 2
    assert three[0]["records"] == two[0]["records"]
    assert three[0]["records_sha256"] == two[0]["records_sha256"]
    assert three[0]["part"]["set"] != two[0]["part"]["set"]
    assert three[0]["envelope_id"] != two[0]["envelope_id"]
    assert envs.intent_hash(three[0]) != envs.intent_hash(two[0])


def test_final_two_digit_part_indices_and_count_fit_exact_budget():
    records = _records((128,) * 12)
    shared = [r for r in records if r["type"] in SHARED]
    pair = [r for r in records if r.get("message_id") == 1012]
    budget = len(_wire(_signed(shared + pair, {"index": 12, "count": 12, "set": "0" * 64})))
    envelopes = _build(records, max_bytes=budget)
    assert len(envelopes) == 12
    assert len(envs.encode_envelope(envelopes[-1])) == budget
    assert all(len(envs.encode_envelope(e)) <= budget for e in envelopes)
    assert envs.validate_collection(envelopes)["status"] == "complete"
    with pytest.raises(C1ContractError, match="record_too_large"):
        _build(records, max_bytes=budget - 1)


def test_a_large_real_collection_is_split_under_the_hard_wire_limit():
    records = _records((8192,) * 125)
    envelopes = _build(records)
    assert len(envelopes) > 1
    assert all(len(envs.encode_envelope(e)) <= 1_048_576 for e in envelopes)
    assert sum(sum(r["type"] == "message_body" for r in e["records"]) for e in envelopes) == 125
    assert envs.validate_collection(envelopes)["status"] == "complete"


def test_one_indivisible_pair_is_never_truncated_or_dropped_to_fit():
    records = _records((8192,))
    shared = [r for r in records if r["type"] in SHARED]
    budget = len(_wire(_signed(shared, {"index": 1, "count": 2, "set": "0" * 64}))) + 1
    before = deepcopy(records)
    with pytest.raises(C1ContractError, match="record_too_large"):
        _build(records, max_bytes=budget)
    assert records == before


def test_body_input_order_does_not_allow_an_orphan_or_separate_part():
    records = _records((128, 128))
    body = records.pop()
    records.insert(0, body)
    envelope = _build(records)[0]
    mids = [r["message_id"] for r in envelope["records"] if r["type"] in ("message", "message_body")]
    assert mids == [1002, 1002, 1001, 1001]
    assert envs.validate_collection([envelope])["status"] == "complete"


def test_equal_projected_signals_keep_multiplicity_without_cross_part_duplicates():
    records, _, budget = _split_inputs()
    signal = _record("signal", project_id=1, signal_type="rx_period_expiry",
                     evidence={"message_ids": [1001]}, detected_at=1000)
    records += [signal, deepcopy(signal)]
    envelopes = _build(records, max_bytes=budget)
    parts = [e for e in envelopes if any(r["type"] == "signal" for r in e["records"])]
    assert len(parts) == 1
    assert sum(r["type"] == "signal" for r in parts[0]["records"]) == 2
    assert envs.validate_collection(envelopes)["status"] == "complete"


@pytest.mark.parametrize("profile", [
    _profile(fields=list(legacy.RECORD_TYPES)), _profile(patients=[1]),
    _profile(retention_days=31), _profile(max_snapshot_age_s=None),
])
def test_builder_checks_explicit_profile_without_changing_legacy_grants(profile):
    with pytest.raises(C1ContractError):
        _build(_records(), profile=profile)


@pytest.mark.parametrize("over", [
    {"now": 999}, {"now": True}, {"now": 4601}, {"now": float("nan")},
    {"auth_id": ""}, {"destination": None}, {"purpose": "\ud800"},
])
def test_builder_refuses_bad_context_or_stale_snapshot(over):
    with pytest.raises(C1ContractError):
        _build(_records(), **over)


def test_receiver_does_not_claim_sender_freshness_or_human_authorization_inspection():
    envelope = _signed(_records())
    envelope["created_at"] = 999999
    assert envs.validate_envelope(envelope) is None
    assert envs.parse_envelope(_wire(envelope)) == envelope


def test_creation_time_does_not_change_identity_but_intent_binds_destination():
    original = _build(_records())[0]
    later = _build(_records(), now=1040)[0]
    changed = _build(_records(), destination="other-local")[0]
    assert original["envelope_id"] == later["envelope_id"] == changed["envelope_id"]
    assert envs.intent_hash(original) == envs.intent_hash(later)
    assert envs.intent_hash(original) != envs.intent_hash(changed)


@pytest.mark.parametrize("over", [
    {"contract": "mcs-ext-export/1"}, {"scope": "detail"},
    {"auth_id": None}, {"purpose": " "}, {"destination": True},
    {"retention_days": True}, {"retention_days": 0}, {"retention_days": 31},
    {"retention_days": None}, {"created_at": True}, {"created_at": None},
    {"snapshot_generated_at": True}, {"snapshot_generated_at": 1001},
    {"snapshot_generation_id": None}, {"snapshot_generation_id": "other"},
    {"record_count": True}, {"record_count": 3}, {"record_count": None},
    {"records_sha256": "0" * 64}, {"envelope_id": "0" * 24},
    {"envelope_id": "../x"}, {"extra": None}, {"part": None},
    {"part": {"index": 1, "count": 1, "set": "0" * 64}},
    {"part": {"index": 0, "count": 2, "set": "0" * 64}},
    {"part": {"index": 3, "count": 2, "set": "0" * 64}},
    {"part": {"index": True, "count": 2, "set": "0" * 64}},
    {"part": {"index": 1, "count": 2, "set": "X" * 64}},
    {"part": {"index": 1, "count": 2, "set": "0" * 64, "extra": None}},
])
def test_envelope_metadata_integrity_and_part_fields_are_strict(over):
    with pytest.raises(C1ContractError):
        envs.validate_envelope({**_signed(_records()), **over})


@pytest.mark.parametrize("kind", [
    "missing_meta", "missing_coverage", "duplicate_meta", "duplicate_message",
    "duplicate_body", "duplicate_patient", "orphan", "wrong_project",
    "wrong_body_hash", "snippet_body", "mixed_generation", "stat", "attachment",
    "nested_body", "null_state", "negative_floor",
])
def test_rehashed_invalid_records_are_rejected_at_builder_and_receiver(kind):
    records = _records((32,))
    message = next(r for r in records if r["type"] == "message")
    body = next(r for r in records if r["type"] == "message_body")
    if kind in ("missing_meta", "missing_coverage"):
        records = [r for r in records if r["type"] != kind.removeprefix("missing_")]
    elif kind.startswith("duplicate_"):
        target = {"meta": "meta", "message": "message", "body": "message_body",
                  "patient": "patient_coverage"}[kind.removeprefix("duplicate_")]
        records.append(deepcopy(next(r for r in records if r["type"] == target)))
    elif kind == "orphan":
        records.remove(message)
    elif kind == "wrong_project":
        body["project_id"] = 2
    elif kind == "wrong_body_hash":
        body["body_sha256"] = "0" * 64
    elif kind == "snippet_body":
        message["body_state"] = "snippet"
    elif kind == "mixed_generation":
        message["snapshot_generation_id"] = "other"
    elif kind in ("stat", "attachment"):
        records.append(_record(kind))
    elif kind == "nested_body":
        message["facts"] = [{"fact_id": "f1", "body_text": "SYNTHETIC"}]
    elif kind == "null_state":
        message["body_state"] = None
    else:
        next(r for r in records if r["type"] == "patient_coverage")["history_floor"] = -1
    with pytest.raises(C1ContractError):
        _build(records)
    with pytest.raises(C1ContractError):
        envs.parse_envelope(_wire(_signed(records)))


def test_empty_and_missing_parts_are_incomplete_without_current_evidence():
    records, narrow, _ = _split_inputs()
    three = _build(records, max_bytes=narrow)
    assert envs.validate_collection([]) == {
        "status": "incomplete", "received_parts": 0, "expected_parts": None, "evidence": None}
    assert envs.validate_collection([three[0], three[2]]) == {
        "status": "incomplete", "received_parts": 2, "expected_parts": 3, "evidence": None}


def test_huge_advertised_count_is_incomplete_without_allocating_a_missing_range():
    envelope = _signed(_records(), {"index": 1, "count": 9007199254740991, "set": "0" * 64})
    assert envs.validate_collection([envelope]) == {
        "status": "incomplete", "received_parts": 1,
        "expected_parts": 9007199254740991, "evidence": None}


@pytest.mark.parametrize("fault,code", [
    ("duplicate_index", "collection_index_duplicate"),
    ("mixed_count", "collection_set_mixed"),
    ("mixed_set", "collection_set_mixed"),
    ("bad_set_hash", "collection_set_hash_invalid"),
    ("common", "collection_common_mismatch"),
    ("message", "collection_records_duplicate"),
    ("signal", "collection_records_duplicate"),
    ("auth", "collection_metadata_mixed"),
    ("generation", "collection_metadata_mixed"),
    ("purpose", "collection_metadata_mixed"),
    ("retention", "collection_metadata_mixed"),
    ("created_at", "collection_metadata_mixed"),
])
def test_individually_valid_parts_cannot_form_an_invalid_collection(fault, code):
    records, _, wide = _split_inputs()
    parts = _build(records, max_bytes=wide)
    if fault == "duplicate_index":
        parts[1]["part"]["index"] = 1
    elif fault == "mixed_count":
        parts[1]["part"]["count"] = 3
    elif fault in ("mixed_set", "bad_set_hash"):
        for part in parts[1:] if fault == "mixed_set" else parts:
            part["part"]["set"] = "0" * 64
    elif fault == "common":
        next(r for r in parts[1]["records"] if r["type"] == "patient_coverage")["coverage_ts"] = 50
    elif fault == "message":
        parts[1]["records"] += deepcopy(
            [r for r in parts[0]["records"] if r["type"] not in SHARED])
    elif fault == "signal":
        signal = _record("signal", project_id=1, signal_type="rx_period_expiry",
                         evidence={"message_ids": [1001]})
        for part in parts:
            part["records"].append(deepcopy(signal))
    elif fault == "generation":
        parts[1]["snapshot_generation_id"] = "other"
        for record in parts[1]["records"]:
            record["snapshot_generation_id"] = "other"
            if record["type"] == "meta":
                record["snapshot"]["generation_id"] = "other"
    else:
        field = {"auth": "auth_id", "purpose": "purpose",
                 "retention": "retention_days", "created_at": "created_at"}[fault]
        parts[1][field] = {"retention": 29, "created_at": 1040}.get(fault, "other")
    if fault not in ("mixed_set", "bad_set_hash"):
        _resign_collection(parts)
    else:
        for part in parts:
            _resign(part)
    assert all(envs.validate_envelope(part) is None for part in parts)
    with pytest.raises(C1ContractError, match=code):
        envs.validate_collection(parts)


def test_mixed_split_and_unsplit_collections_are_rejected():
    records, _, wide = _split_inputs()
    mixed = [_build(records)[0], _build(records, max_bytes=wide)[0]]
    with pytest.raises(C1ContractError, match="collection_split_mixed"):
        envs.validate_collection(mixed)


def test_current_set_evidence_allows_replay_but_rejects_alternative_set_parts():
    records, narrow, wide = _split_inputs()
    first = _build(records, max_bytes=wide)
    alternative = _build(records, max_bytes=narrow)
    complete = envs.validate_collection(first)
    before = deepcopy(complete)
    assert envs.validate_collection(first, current_set=complete["evidence"]) == complete
    for candidate in (alternative, alternative[:1], _build(records)):
        with pytest.raises(C1ContractError, match="generation_set_conflict"):
            envs.validate_collection(candidate, current_set=complete["evidence"])
    assert complete == before


@pytest.mark.parametrize("over", [
    {"count": True}, {"count": 0}, {"set": None}, {"extra": None}, {"contract": "mcs-ext-export/1"},
])
def test_invalid_current_set_evidence_is_not_trusted(over):
    candidate = _build(_records())
    evidence = {**envs.validate_collection(candidate)["evidence"], **over}
    with pytest.raises(C1ContractError, match="current_set_invalid"):
        envs.validate_collection(candidate, current_set=evidence)


def test_current_set_evidence_from_another_scope_cannot_select_current():
    candidate = _build(_records())
    evidence = envs.validate_collection(candidate)["evidence"]
    evidence["auth_id"] = "other-auth"
    with pytest.raises(C1ContractError, match="current_set_scope_mismatch"):
        envs.validate_collection(candidate, current_set=evidence)


def test_v1_remains_distinct_and_its_numeric_bytes_are_unchanged():
    assert legacy._canonical({"x": 1800000000.0}) == '{"x":1800000000.0}'
    envelope = _build(_records())[0]
    legacy._validate_envelope(envelope)
    with pytest.raises(legacy.ContractError):
        legacy._validate_envelope({**envelope, "contract": legacy.EXT_CONTRACT})
