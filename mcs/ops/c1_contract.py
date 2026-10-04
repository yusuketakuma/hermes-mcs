"""Validate opt-in C1 values without choosing an envelope wire contract.

Canonical input is a parsed JSON tree of builtin dict/list/str/bool/null,
safe integers and finite binary64 floats. Nonzero magnitudes below 1e-6,
magnitudes at least 1e21 and integral values outside +/- (2**53 - 1) are
refused. Floats use Python's shortest round-trip decimal digits expanded
to fixed notation; integral floats and negative zero use integer notation.
Keys sort by Unicode code point, arrays retain their order, and surrogate
code points are refused. This is a restricted local canonical domain, not
an arbitrary RFC 8785 implementation or a raw-JSON duplicate-key parser.

The existing read-model record header and aggregate allowlist are reused.
No envelope version, envelope identity, part, authorization lifecycle or
sender classification is selected here. Callers must validate current
human authorization separately before using the four-field C1 profile.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import TypeAlias

from export_schema import (
    FORBIDDEN_KEYS, TYPE_FORBIDDEN, _BASE, validate_record as _validate_aggregate)

JSONValue: TypeAlias = (
    str | int | float | bool | None | Sequence["JSONValue"] | Mapping[str, "JSONValue"])
C1_FIELDS = (
    "meta", "coverage", "message", "message_body", "patient_coverage",
    "signal", "signals_truncated")
SENDER_KINDS = (
    "self_org", "physician", "nurse", "care_manager",
    "other_professional", "patient_family", "unknown")
MAX_BODY_BYTES = 8192
MAX_SAFE_INTEGER = 2**53 - 1
_HEADER = ("type", "contract", "snapshot_generation_id")


class C1ContractError(ValueError):
    """A stable refusal code, never input text or patient content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _number(value: JSONValue) -> int | float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise C1ContractError("number_type_invalid")
    if (abs(value) >= 1e21 or 0 < abs(value) < 1e-6
            or abs(value) > MAX_SAFE_INTEGER
            or not math.isfinite(value)):
        raise C1ContractError("number_domain_invalid")
    return value


def _integer(value: JSONValue, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        raise C1ContractError("integer_invalid")
    return value


def _object(value: JSONValue, required: tuple[str, ...],
            optional: tuple[str, ...] = ()) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        raise C1ContractError("object_required")
    if not set(required) <= value.keys() or value.keys() - set(required + optional):
        raise C1ContractError("fields_invalid")
    return value


def _canonical(value: JSONValue) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        number = _number(value)
        if type(number) is int or number.is_integer():
            return str(int(number))
        return format(Decimal(repr(number)), "f")
    if type(value) is str:
        value.encode("utf-8")  # Reject surrogate code points, including in keys.
        return json.dumps(value, ensure_ascii=False)
    if type(value) is list:
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if type(value) is dict and all(type(key) is str for key in value):
        return "{" + ",".join(
            _canonical(key) + ":" + _canonical(value[key]) for key in sorted(value)) + "}"
    raise C1ContractError("canonical_type_invalid")


def canonical_json(value: JSONValue) -> str:
    """Return deterministic JSON in the module's restricted numeric domain."""
    try:
        return _canonical(value)
    except UnicodeError:
        raise C1ContractError("unicode_invalid") from None
    except RecursionError:
        raise C1ContractError("canonical_nesting_invalid") from None


def validate_profile(profile: JSONValue) -> None:
    """Check only fields/patients/max_snapshot_age_s/retention_days, not auth.

    Supply this exact four-key projection of a separately checked human
    authorization. It grants no defaults, adds no fields, and mutates nothing.
    """
    if isinstance(profile, dict) and "fields" not in profile:
        raise C1ContractError("auth_fields_required")
    policy = _object(profile, (
        "fields", "patients", "max_snapshot_age_s", "retention_days"))
    canonical_json(policy)
    fields = policy["fields"]
    if (not isinstance(fields, list) or len(fields) != len(C1_FIELDS)
            or any(type(field) is not str for field in fields)
            or set(fields) != set(C1_FIELDS)):
        raise C1ContractError("profile_fields_invalid")
    if policy["patients"] != "all":
        raise C1ContractError("profile_patients_invalid")
    if not 0 <= _number(policy["max_snapshot_age_s"]) <= 3600:
        raise C1ContractError("profile_snapshot_age_invalid")
    if _integer(policy["retention_days"], 1) > 30:
        raise C1ContractError("profile_retention_invalid")


def validate_record(record: JSONValue, *,
                    message: JSONValue = None) -> None:
    """Validate one C1 record; a body requires its corresponding message.

    Body association checks project/message IDs and the existing read-model
    contract/generation against a validated full-body message. It does not
    establish snapshot authenticity or classify a sender. Coverage nulls
    remain unknown; ledger floor conversion belongs to the generator.
    """
    if not isinstance(record, dict) or record.get("type") not in C1_FIELDS:
        kind = record.get("type") if isinstance(record, dict) else None
        raise C1ContractError("record_type_not_accepted" + (
            ":" + kind if isinstance(kind, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", kind) else ""))
    canonical_json(record)
    # Name familiar raw-content keys before the generic allowlist refusal;
    # only the paired body's root body_text is exempt.
    forbidden = FORBIDDEN_KEYS | TYPE_FORBIDDEN.get(record["type"], frozenset())
    stack: list[JSONValue] = [{k: v for k, v in record.items() if k != "body_text"}
                              if record["type"] == "message_body" else record]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if bad := forbidden & node.keys():
                raise C1ContractError("forbidden_field:" + sorted(bad)[0])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    if any(not _BASE[key](record.get(key)) for key in _HEADER):
        raise C1ContractError("record_provenance_invalid")
    if "content_omitted" in record and type(record["content_omitted"]) is not bool:
        raise C1ContractError("record_content_omitted_invalid")
    kind = record["type"]
    if kind == "message_body":
        _object(record, _HEADER + (
            "project_id", "message_id", "body_text", "body_format",
            "body_sha256", "body_truncated", "sender_kind"), ("content_omitted",))
        _integer(record["project_id"], 1)
        _integer(record["message_id"], 1)
        if type(record["body_text"]) is not str or record["body_format"] != "text":
            raise C1ContractError("body_format_invalid")
        if (type(record["body_truncated"]) is not bool
                or record["sender_kind"] not in SENDER_KINDS):
            raise C1ContractError("body_metadata_invalid")
        body = record["body_text"].encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            raise C1ContractError("body_too_large")
        if (type(record["body_sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", record["body_sha256"]) is None
                or hashlib.sha256(body).hexdigest() != record["body_sha256"]):
            raise C1ContractError("body_hash_invalid")
        if not isinstance(message, dict) or message.get("type") != "message":
            raise C1ContractError("body_message_required")
        validate_record(message)
        if (message["body_state"] != "full" or any(
                record[key] != message[key] for key in (
                    "contract", "snapshot_generation_id", "project_id", "message_id"))):
            raise C1ContractError("body_message_mismatch")
        return
    if kind == "patient_coverage":
        _object(record, _HEADER + (
            "project_id", "fetch_state", "coverage_ts", "history_floor"),
            ("content_omitted",))
        _integer(record["project_id"], 1)
        if record["fetch_state"] not in ("pending", "complete", "incomplete"):
            raise C1ContractError("patient_coverage_state_invalid")
        if record["coverage_ts"] is not None and _number(record["coverage_ts"]) < 0:
            raise C1ContractError("patient_coverage_time_invalid")
        floor = record["history_floor"]
        if floor is not None and (type(floor) is not int or not 0 <= floor <= MAX_SAFE_INTEGER):
            raise C1ContractError("patient_coverage_floor_invalid")
        return
    legacy_record = record
    if kind == "coverage":
        coverage = _object(record.get("coverage"),
                           ("collection", "extraction", "attachments"))
        collection = _object(coverage["collection"], (
            "patients", "messages", "deleted", "extraction_eligible", "patients_incomplete"))
        for count in collection.values():
            if count is not None:
                _integer(count)
        # Reuse the old nested allowlist without mutating it or the input.
        legacy_record = {**record, "coverage": {**coverage, "collection": {
            key: value for key, value in collection.items() if key != "patients_incomplete"}}}
    try:
        _validate_aggregate(legacy_record)
    except (ValueError, TypeError, RecursionError):
        raise C1ContractError("record_field_not_exportable") from None
    if kind == "meta":
        snapshot = _object(record.get("snapshot"), ("generation_id", "generated_at"),
                           ("published",))
        if (snapshot["generation_id"] != record["snapshot_generation_id"]
                or _number(snapshot["generated_at"]) < 0
                or ("published" in snapshot and type(snapshot["published"]) is not bool)):
            raise C1ContractError("snapshot_metadata_invalid")
    if kind == "message":
        _integer(record.get("project_id"), 1)
        _integer(record.get("message_id"), 1)
        if record.get("body_state") not in ("full", "snippet", "unknown", "deleted"):
            raise C1ContractError("message_body_state_invalid")
        for key in ("state", "extraction_eligible", "extraction", "facts", "relations"):
            if key in record and record[key] is None:
                raise C1ContractError("message_field_null")
    if kind == "signal":
        _integer(record.get("project_id"), 1)
    if kind == "signals_truncated":
        _integer(record.get("total"))
