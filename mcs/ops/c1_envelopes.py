"""Build and validate bounded /2 envelopes without storage or delivery effects.

Unsplit envelopes omit part entirely; split envelopes have count >= 2.
ID input is {contract, auth_id, snapshot_generation_id, records_sha256,
part}, with part=null only in the unsplit hash preimage, never on the wire.
Set hashes cover the index-ordered array of records hashes, before IDs.
Canonical record order is significant; no payload/signal identity folding
or clinical absence inference is performed here.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Literal, TypeAlias, TypedDict

from c1_contract import (
    C1ContractError, JSONValue, MAX_SAFE_INTEGER,
    canonical_json, validate_profile, validate_record,
)

CONTRACT = "mcs-ext-export/2"
MAX_WIRE_BYTES = 1_048_576
AUTH_ID_MAX = 256  # same limit as withdraw/1, so every sealed auth_id stays withdrawable
Record: TypeAlias = dict[str, JSONValue]
_SHARED = ("meta", "coverage", "signals_truncated", "patient_coverage")
_CONTEXT = (
    "contract", "auth_id", "destination", "purpose", "scope",
    "snapshot_generation_id", "snapshot_generated_at", "created_at", "retention_days")
_FIELDS = frozenset((*_CONTEXT, "record_count", "records_sha256", "records", "envelope_id"))
_EVIDENCE = frozenset(("contract", "auth_id", "snapshot_generation_id", "count", "set"))


class CollectionValidation(TypedDict):
    status: Literal["complete", "incomplete"]
    received_parts: int
    expected_parts: int | None
    evidence: Record | None


def _wire(value: JSONValue) -> bytes:
    return canonical_json(value).encode("utf-8")


def _hash(value: JSONValue) -> str:
    return hashlib.sha256(_wire(value)).hexdigest()


def _hex(value: JSONValue, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{" + str(length) + "}", value) is not None


def _time(value: JSONValue) -> int | float:
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        raise C1ContractError("snapshot_time_invalid")
    canonical_json(value)
    return value


def _message_key(record: Record) -> tuple[int, int]:
    pid, mid = record.get("project_id"), record.get("message_id")
    if type(pid) is not int or type(mid) is not int or pid <= 0 or mid <= 0:
        raise C1ContractError("record_identity_invalid")
    return pid, mid


def _part(envelope: Record) -> tuple[int, int, str] | None:
    if "part" not in envelope:
        return None
    part = envelope["part"]
    if not isinstance(part, dict) or set(part) != {"index", "count", "set"}:
        raise C1ContractError("part_invalid")
    index, count, set_id = part["index"], part["count"], part["set"]
    if (type(index) is not int or type(count) is not int
            or not 1 <= index <= count or not 2 <= count <= MAX_SAFE_INTEGER
            or not isinstance(set_id, str) or not _hex(set_id, 64)):
        raise C1ContractError("part_invalid")
    return index, count, set_id


def _group_records(records: object) -> tuple[list[Record], list[list[Record]], Record]:
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise C1ContractError("records_not_list")
    messages: dict[tuple[int, int], Record] = {}
    bodies: dict[tuple[int, int], Record] = {}
    shared: list[Record] = []
    singleton: set[str] = set()
    patients: set[int] = set()
    meta = None
    for record in records:
        assert isinstance(record, dict)
        if record.get("type") == "message_body":
            key = _message_key(record)
            if key in bodies:
                raise C1ContractError("record_identity_duplicate")
            bodies[key] = record
            continue
        validate_record(record)
        kind = record["type"]
        if kind == "message":
            key = _message_key(record)
            if key in messages:
                raise C1ContractError("record_identity_duplicate")
            messages[key] = record
        if kind in _SHARED:
            shared.append(record)
            if kind == "patient_coverage":
                pid = record["project_id"]
                assert isinstance(pid, int)  # validate_record rejects bool and null.
                if pid in patients:
                    raise C1ContractError("record_identity_duplicate")
                patients.add(pid)
            else:
                assert isinstance(kind, str)
                if kind in singleton:
                    raise C1ContractError("record_identity_duplicate")
                singleton.add(kind)
                if kind == "meta":
                    meta = record
    if meta is None:
        raise C1ContractError("envelope_meta_missing")
    if "coverage" not in singleton:
        raise C1ContractError("envelope_coverage_missing")
    for key, body in bodies.items():
        validate_record(body, message=messages.get(key))
    if any(r["snapshot_generation_id"] != meta["snapshot_generation_id"] for r in records):
        raise C1ContractError("snapshot_generation_mixed")
    groups: list[list[Record]] = []
    seen_messages: set[tuple[int, int]] = set()
    signals: dict[str, list[Record]] = {}
    for record in records:
        assert isinstance(record, dict)
        if record["type"] in ("message", "message_body"):
            key = _message_key(record)
            if key not in seen_messages:
                groups.append([messages[key]] + ([bodies[key]] if key in bodies else []))
                seen_messages.add(key)
        elif record["type"] == "signal":
            # CD-7 permits equal projected signals. Preserve their multiplicity,
            # colocating byte-identical occurrences so parts remain exclusive.
            identity = canonical_json(record)
            if identity not in signals:
                signals[identity] = []
                groups.append(signals[identity])
            signals[identity].append(record)
    return shared, groups, meta


def _envelope_id(envelope: Record) -> str:
    return _hash({key: envelope.get(key) for key in (
        "contract", "auth_id", "snapshot_generation_id", "records_sha256", "part")})[:24]


def _seal(context: Record, records: list[Record], part: Record | None = None) -> Record:
    envelope: Record = {
        **context, "records": deepcopy(records), "record_count": len(records),
        "records_sha256": _hash(records)}
    if part is not None:
        envelope["part"] = part
    envelope["envelope_id"] = _envelope_id(envelope)
    return envelope


def build_envelopes(records: object, *, profile: JSONValue,
                    auth_id: str, destination: str, purpose: str,
                    now: int | float, max_bytes: int = MAX_WIRE_BYTES) -> list[Record]:
    """Greedily pack validated records using an explicit, separately authorized profile.

    All input is validated before returning any envelope. Shared rows precede
    indivisible groups in first-occurrence order; bodies follow their message.
    Caller owns current human authorization, revocation and send-time checks.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_WIRE_BYTES:
        raise C1ContractError("max_bytes_invalid")
    validate_profile(profile)
    assert isinstance(profile, dict)
    shared, groups, meta = _group_records(records)
    snapshot = meta["snapshot"]
    assert isinstance(snapshot, dict)
    generated_at = _time(snapshot["generated_at"])
    created_at = _time(now)
    if created_at < generated_at:
        raise C1ContractError("snapshot_time_invalid")
    if created_at - generated_at > _time(profile["max_snapshot_age_s"]):
        raise C1ContractError("snapshot_stale")
    context: Record = {
        "contract": CONTRACT, "auth_id": auth_id, "destination": destination,
        "purpose": purpose, "scope": "aggregate", "created_at": created_at,
        "snapshot_generation_id": meta["snapshot_generation_id"],
        "snapshot_generated_at": generated_at, "retention_days": profile["retention_days"]}
    for value in (auth_id, destination, purpose):
        if not isinstance(value, str) or not value.strip():
            raise C1ContractError("envelope_metadata_invalid")
    if len(auth_id) > AUTH_ID_MAX:
        raise C1ContractError("envelope_metadata_invalid")
    canonical_json(context)
    single = _seal(context, shared + [r for group in groups for r in group])
    if len(_wire(single)) <= max_bytes:
        validate_envelope(single)
        return [single]
    shared_size = sum(len(_wire(record)) for record in shared)
    group_sizes = [sum(len(_wire(record)) for record in group) for group in groups]

    def size(payload_size: int, record_count: int, index: int, count: int) -> int:
        # Hashes and IDs have fixed widths; this includes actual integer digits,
        # JSON commas, UTF-8 metadata and the complete part object.
        empty: Record = {
            **context, "records": [], "record_count": record_count,
            "records_sha256": "0" * 64, "envelope_id": "0" * 24,
            "part": {"index": index, "count": count, "set": "0" * 64}}
        return len(_wire(empty)) + payload_size + max(0, record_count - 1)

    count = 2
    while True:
        chunks: list[list[Record]] = []
        chunk: list[Record] = []
        payload_size, record_count = shared_size, len(shared)
        if size(payload_size, record_count, 1, count) > max_bytes or not groups:
            raise C1ContractError("shared_records_too_large")
        for group, group_size in zip(groups, group_sizes):
            if size(payload_size + group_size, record_count + len(group),
                    len(chunks) + 1, count) > max_bytes:
                if chunk:
                    chunks.append(shared + chunk)
                    chunk = []
                    payload_size, record_count = shared_size, len(shared)
                if size(shared_size, len(shared), len(chunks) + 1, count) > max_bytes:
                    raise C1ContractError("shared_records_too_large")
                if size(payload_size + group_size, record_count + len(group),
                        len(chunks) + 1, count) > max_bytes:
                    raise C1ContractError("record_too_large")
            chunk.extend(group)
            payload_size += group_size
            record_count += len(group)
        chunks.append(shared + chunk)
        if len(chunks) == count:
            break
        # Increasing count-width can only shrink capacity. Repack until final
        # count/index widths are exact, rather than reserving a guessed budget.
        count = len(chunks)
    hashes = [_hash(chunk) for chunk in chunks]
    set_id = _hash(hashes)
    envelopes = [_seal(context, chunk, {"index": index, "count": count, "set": set_id})
                 for index, chunk in enumerate(chunks, 1)]
    for envelope in envelopes:
        validate_envelope(envelope)
        if len(_wire(envelope)) > max_bytes:
            raise C1ContractError("envelope_too_large")
    return envelopes


def validate_envelope(envelope: JSONValue) -> None:
    """Check observable /2 content, not unseen auth, receipt, age policy or authenticity."""
    if not isinstance(envelope, dict) or envelope.get("contract") != CONTRACT:
        raise C1ContractError("sink_contract_mismatch")
    if not _FIELDS <= envelope.keys() or envelope.keys() - (_FIELDS | {"part"}):
        raise C1ContractError("envelope_fields_invalid")
    if len(_wire(envelope)) > MAX_WIRE_BYTES:
        raise C1ContractError("envelope_too_large")
    if envelope["scope"] != "aggregate":
        raise C1ContractError("sink_scope_not_aggregate")
    for key in ("auth_id", "destination", "purpose"):
        value = envelope[key]
        if not isinstance(value, str) or not value.strip():
            raise C1ContractError("envelope_metadata_invalid")
    if len(envelope["auth_id"]) > AUTH_ID_MAX:
        raise C1ContractError("envelope_metadata_invalid")
    retention = envelope["retention_days"]
    if type(retention) is not int or not 1 <= retention <= 30:
        raise C1ContractError("envelope_retention_invalid")
    generated_at = _time(envelope["snapshot_generated_at"])
    if _time(envelope["created_at"]) < generated_at:
        raise C1ContractError("snapshot_time_invalid")
    _part(envelope)
    _, _, meta = _group_records(envelope["records"])
    snapshot = meta["snapshot"]
    assert isinstance(snapshot, dict)
    if (envelope["snapshot_generation_id"] != meta["snapshot_generation_id"]
            or generated_at != snapshot["generated_at"]):
        raise C1ContractError("snapshot_metadata_mismatch")
    records = envelope["records"]
    assert isinstance(records, list)
    if not _hex(envelope["envelope_id"], 24):
        raise C1ContractError("envelope_id_invalid")
    if (type(envelope["record_count"]) is not int or envelope["record_count"] != len(records)
            or not _hex(envelope["records_sha256"], 64)
            or envelope["records_sha256"] != _hash(records)
            or envelope["envelope_id"] != _envelope_id(envelope)):
        raise C1ContractError("envelope_integrity_invalid")


def parse_envelope(raw: bytes) -> Record:
    """Cap actual UTF-8 wire bytes before parsing; duplicate keys follow JSON last-wins."""
    if not isinstance(raw, bytes):
        raise C1ContractError("envelope_bytes_required")
    if len(raw) > MAX_WIRE_BYTES:
        raise C1ContractError("envelope_too_large")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise C1ContractError("envelope_json_invalid") from None
    validate_envelope(envelope)
    return envelope


def encode_envelope(envelope: JSONValue) -> bytes:
    """Return validated canonical UTF-8 wire bytes, without a trailing newline."""
    validate_envelope(envelope)
    return _wire(envelope)


def intent_hash(envelope: JSONValue) -> str:
    """Bind /2 intent including part; creation wall time is deliberately excluded."""
    validate_envelope(envelope)
    assert isinstance(envelope, dict)
    return _hash({key: envelope.get(key) for key in (
        *_CONTEXT[:7], "retention_days", "records_sha256", "part")})


WITHDRAW_CONTRACT = "mcs-ext-withdraw/1"
WITHDRAW_MAX_BYTES = 4096
WITHDRAW_REASONS = (
    "operator_request", "authorization_revoked", "content_correction",
    "generation_set_conflict")


def encode_withdrawal(envelope_id: str, auth_id: str, reason: str) -> bytes:
    """Return a bounded directive; it carries IDs and a fixed code, never free text."""
    directive: Record = {"contract": WITHDRAW_CONTRACT, "envelope_id": envelope_id,
                         "auth_id": auth_id, "reason": reason}
    raw = _wire(directive)
    parse_withdrawal(raw)
    return raw


def parse_withdrawal(raw: bytes) -> Record:
    """Validate a directive with its own 4 KiB cap, separate from envelopes."""
    if not isinstance(raw, bytes):
        raise C1ContractError("withdraw_bytes_required")
    if len(raw) > WITHDRAW_MAX_BYTES:
        raise C1ContractError("withdraw_too_large")
    try:
        directive = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise C1ContractError("withdraw_json_invalid") from None
    if not isinstance(directive, dict) or directive.get("contract") != WITHDRAW_CONTRACT:
        raise C1ContractError("withdraw_contract_mismatch")
    if set(directive) != {"contract", "envelope_id", "auth_id", "reason"}:
        raise C1ContractError("withdraw_fields_invalid")
    if not _hex(directive["envelope_id"], 24):
        raise C1ContractError("envelope_id_invalid")
    auth_id = directive["auth_id"]
    if not isinstance(auth_id, str) or not auth_id.strip() or len(auth_id) > AUTH_ID_MAX:
        raise C1ContractError("withdraw_auth_invalid")
    if directive["reason"] not in WITHDRAW_REASONS:
        raise C1ContractError("withdraw_reason_invalid")
    return directive


def validate_collection(envelopes: list[Record], *,
                        current_set: Record | None = None) -> CollectionValidation:
    """Return completeness evidence, never select or persist an authoritative current.

    current_set is trusted metadata returned as evidence by an earlier COMPLETE
    validation for the same auth/generation. No receiver storage is inspected.
    Incomplete input returns no evidence and does not assert clinical absence.
    Equal projected signal occurrences are allowed within a part (CD-7), but
    an exact signal payload may not be duplicated across parts.
    """
    if not isinstance(envelopes, list):
        raise C1ContractError("collection_not_list")
    if current_set is not None:
        if not isinstance(current_set, dict) or set(current_set) != _EVIDENCE:
            raise C1ContractError("current_set_invalid")
        canonical_json(current_set)
        n = current_set["count"]
        if (current_set["contract"] != CONTRACT or type(n) is not int
                or not 1 <= n <= MAX_SAFE_INTEGER or not _hex(current_set["set"], 64)
                or any(not isinstance(value, str) or not value.strip()
                       for value in (current_set["auth_id"],
                                     current_set["snapshot_generation_id"]))):
            raise C1ContractError("current_set_invalid")
    if not envelopes:
        return {"status": "incomplete", "received_parts": 0, "expected_parts": None,
                "evidence": None}
    for envelope in envelopes:
        validate_envelope(envelope)
    first = envelopes[0]
    first_part = _part(first)
    count = first_part[1] if first_part else 1
    set_id = first_part[2] if first_part else _hash([first["records_sha256"]])
    evidence: Record = {
        "contract": CONTRACT, "auth_id": first["auth_id"],
        "snapshot_generation_id": first["snapshot_generation_id"], "count": count, "set": set_id}
    if current_set is not None:
        if any(current_set[key] != evidence[key] for key in (
                "contract", "auth_id", "snapshot_generation_id")):
            raise C1ContractError("current_set_scope_mismatch")
        if current_set != evidence:
            raise C1ContractError("generation_set_conflict")
    indexed: dict[int, Record] = {}
    common = None
    exclusive: set[tuple[str, str]] = set()
    for envelope in envelopes:
        part = _part(envelope)
        if (part is None) != (first_part is None):
            raise C1ContractError("collection_split_mixed")
        if any(envelope[key] != first[key] for key in _CONTEXT):
            raise C1ContractError("collection_metadata_mixed")
        if part and (part[1] != count or part[2] != set_id):
            raise C1ContractError("collection_set_mixed")
        index = part[0] if part else 1
        if index in indexed:
            raise C1ContractError("collection_index_duplicate")
        indexed[index] = envelope
        shared, groups, _ = _group_records(envelope["records"])
        shared_bytes = sorted(canonical_json(record) for record in shared)
        if common is not None and shared_bytes != common:
            raise C1ContractError("collection_common_mismatch")
        common = shared_bytes
        keys = set()
        for group in groups:
            record = group[0]
            keys.add(("signal", canonical_json(record)) if record["type"] == "signal"
                     else ("message", canonical_json(list(_message_key(record)))))
        if keys & exclusive:
            raise C1ContractError("collection_records_duplicate")
        exclusive.update(keys)
    # Range checks + uniqueness + cardinality prove all indices exist without
    # allocating a potentially enormous range from an untrusted part.count.
    if len(indexed) != count:
        return {"status": "incomplete", "received_parts": len(indexed),
                "expected_parts": count, "evidence": None}
    if _hash([indexed[index]["records_sha256"] for index in sorted(indexed)]) != set_id:
        raise C1ContractError("collection_set_hash_invalid")
    return {"status": "complete", "received_parts": count,
            "expected_parts": count, "evidence": evidence}
