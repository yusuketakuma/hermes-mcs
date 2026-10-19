"""外部搬出の承認・世代別envelope・receiptを、既存の形式と拒否条件で検証する。"""
from __future__ import annotations

import hashlib
import json
import re
import time

from c1_contract import C1_FIELDS, C1ContractError, validate_profile
from c1_envelopes import (AUTH_ID_MAX, CONTRACT as C1_CONTRACT,
                          RECEIPT_CONTRACT, intent_hash, validate_envelope)
from export_schema import (FORBIDDEN_KEYS, RECORD_TYPES, TYPE_FORBIDDEN,
                           validate_record)
from stats_math import finite_number
from mcs_util import canonical


AUTH_CONTRACT = "mcs-ext-auth/1"


EXT_CONTRACT = "mcs-ext-export/1"


RECEIPT_MAX_BYTES = 65_536


AUTH_FIELDS = frozenset({
    "contract", "auth_id", "purpose", "actor", "destination",
    "scope", "patients", "fields", "expires_at",
    "max_snapshot_age_s", "retention_days", "revoked",
    "confirm_human", "reason", "created_at",
})


AUTH_REQUIRED = ("auth_id", "purpose", "actor", "destination",
                 "expires_at", "confirm_human", "reason", "retention_days")


class ContractError(Exception):
    """Refusal — the export is held before any data leaves."""


def canonical_text(obj) -> str:
    return canonical(obj).decode("utf-8")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_authorization(raw, now: float, *, c1: bool = False) -> dict:
    if not isinstance(raw, dict):
        raise ContractError("auth_not_object")
    unknown = set(raw) - AUTH_FIELDS
    if unknown:
        raise ContractError(
            f"auth_unknown_fields:{sorted(unknown)[0]}")
    missing = [k for k in AUTH_REQUIRED if raw.get(k) in (None, "")]
    if missing:
        raise ContractError(f"auth_missing:{missing[0]}")
    if raw.get("contract") != AUTH_CONTRACT:
        raise ContractError("auth_contract_mismatch")
    if raw.get("scope", "aggregate") != "aggregate":
        raise ContractError("auth_scope_not_aggregate")
    if raw.get("confirm_human") is not True:
        raise ContractError("auth_not_human_confirmed")
    for key in ("auth_id", "purpose", "actor", "destination", "reason"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ContractError(f"auth_invalid:{key}")
    # Same cap as the withdraw/1 directive, so every exported envelope stays withdrawable.
    if len(raw["auth_id"]) > AUTH_ID_MAX:
        raise ContractError("auth_invalid:auth_id")
    if "revoked" in raw and type(raw["revoked"]) is not bool:
        raise ContractError("auth_invalid:revoked")
    if raw.get("revoked"):
        raise ContractError("auth_revoked")
    if not finite_number(now) or not finite_number(raw.get("expires_at")) \
            or raw["expires_at"] <= now:
        raise ContractError("auth_expired")
    for key in ("max_snapshot_age_s", "created_at"):
        if key in raw and (not finite_number(raw[key]) or raw[key] < 0):
            raise ContractError(f"auth_invalid:{key}")
    if type(raw["retention_days"]) is not int or raw["retention_days"] <= 0:
        raise ContractError("auth_invalid:retention_days")
    fields = raw.get("fields")
    if "fields" in raw:
        if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
            raise ContractError("auth_fields_invalid")
        bad = [f for f in fields if f not in (C1_FIELDS if c1 else RECORD_TYPES)]
        if bad:
            raise ContractError(f"auth_field_not_exportable:{bad[0]}")
    patients = raw.get("patients", "all")
    if patients != "all" and not (
            isinstance(patients, list)
            and all(type(p) is int and p > 0 for p in patients)):
        raise ContractError("auth_patients_invalid")
    if c1:
        if "fields" not in raw:
            raise ContractError("auth_fields_required")
        try:
            validate_profile({key: raw.get(key) for key in (
                "fields", "patients", "max_snapshot_age_s", "retention_days")})
        except C1ContractError as e:
            raise ContractError(e.code) from e
    # Omitted scope means aggregate; callers compare it against envelopes.
    return {"scope": "aggregate", **raw}


def _check_record_keys(rec: dict) -> None:
    """Nested scan — a smuggled fact dict carrying 'statement' inside a
    'facts' list must trip the same wall as a top-level key.

    /1 records only; /2 records are checked by c1_contract.validate_record."""
    if not isinstance(rec, dict) or not isinstance(rec.get("type"), str):
        raise ContractError("record_not_object")
    forbidden = FORBIDDEN_KEYS | TYPE_FORBIDDEN.get(
        rec.get("type"), frozenset())
    stack = [rec]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            bad = forbidden & set(node)
            if bad:
                raise ContractError(
                    f"forbidden_field:{sorted(bad)[0]}")
            stack.extend(node.values())
        elif isinstance(node, list | tuple):
            stack.extend(node)
    try:
        validate_record(rec)
    except (ValueError, TypeError, RecursionError) as e:
        raise ContractError("record_field_not_exportable") from e


def build_envelope(records: list[dict], auth: dict,
                   snapshot_generated_at: float | None,
                   now: float | None = None) -> dict:
    """Wrap validated export records in one versioned envelope.

    `records` are parsed ``export.jsonl`` lines (T15 machine surface).
    Per-patient eligibility is applied here: message records are
    filtered by ``auth["patients"]`` and attachments follow their
    message's eligibility."""
    now = time.time() if now is None else now
    auth = validate_authorization(auth, now)
    if not finite_number(snapshot_generated_at) or snapshot_generated_at > now:
        raise ContractError("snapshot_time_invalid")
    max_age = auth.get("max_snapshot_age_s")
    if max_age is not None and now - snapshot_generated_at > max_age:
        raise ContractError("snapshot_stale")
    allowed = set(auth.get("fields", RECORD_TYPES))
    patients = auth.get("patients", "all")
    eligible_pids = None if patients == "all" else set(patients)

    kept, kept_mids = [], set()
    generation = None
    if not isinstance(records, list):
        raise ContractError("records_not_list")
    for rec in records:
        if not isinstance(rec, dict):
            raise ContractError("record_not_object")
        # a None contract passes here but is refused below by _check_record_keys
        # (record_provenance_missing -> record_field_not_exportable)
        if rec.get("contract") not in (None, "mcs-read-model/1"):
            raise ContractError("record_contract_mismatch")
        rtype = rec.get("type")
        if rtype not in RECORD_TYPES:
            raise ContractError(f"record_type_unknown:{rtype}")
        if rtype not in allowed:
            raise ContractError(f"record_type_unauthorized:{rtype}")
        _check_record_keys(rec)
        gen = rec["snapshot_generation_id"]
        if generation is not None and gen != generation:
            raise ContractError("snapshot_generation_mixed")
        generation = gen
        if rtype == "meta":
            meta = rec.get("snapshot") or {}
            if meta.get("generated_at") != snapshot_generated_at \
                    or meta.get("generation_id", gen) != gen:
                raise ContractError("snapshot_metadata_mismatch")
        if eligible_pids is not None:
            if rtype in ("message", "signal") and rec.get("project_id") not in eligible_pids:
                continue
            # Whole-snapshot aggregates cannot be narrowed by deleting
            # patient rows. Omit them rather than leaking other patients.
            if rtype in ("coverage", "signals_truncated"):
                continue
            if rtype == "stat" and (not isinstance(rec.get("value"), dict)
                    or (rec["value"].get("scope") or {}).get("project_id") not in eligible_pids):
                continue
            stack = [rec]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    if "project_id" in node and node["project_id"] not in eligible_pids:
                        raise ContractError("record_patient_scope_mixed")
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)
        kept.append(rec)
        if rtype == "message":
            kept_mids.add(rec.get("message_id"))
    if eligible_pids is not None:
        # attachments carry no project_id — eligibility follows the
        # parent message (an attachment of an ineligible message drops)
        kept = [r for r in kept if r.get("type") != "attachment"
                or r.get("message_id") in kept_mids]

    body = canonical_text(kept)
    envelope = {
        "contract": EXT_CONTRACT,
        "auth_id": auth["auth_id"],
        "destination": auth["destination"],
        "purpose": auth["purpose"],
        "scope": "aggregate",
        "snapshot_generation_id": generation,
        "snapshot_generated_at": snapshot_generated_at,
        "created_at": now,
        "retention_days": auth.get("retention_days"),
        "record_count": len(kept),
        "records_sha256": _sha(body),
        "records": kept,
    }
    envelope["envelope_id"] = _envelope_id(envelope)
    return envelope


def _envelope_id(envelope: dict) -> str:
    # Preserve the /1 idempotency key, including for persisted envelopes.
    return _sha(canonical_text({"auth_id": envelope["auth_id"],
                            "gen": envelope["snapshot_generation_id"],
                            "records_sha256": envelope["records_sha256"]}))[:24]


def export_intent_hash(envelope: dict) -> str:
    if envelope.get("contract") == C1_CONTRACT:
        return intent_hash(envelope)
    # Reusing an auth_id cannot change a previously dispatched intent.
    return _sha(canonical_text({key: envelope.get(key) for key in (
        "auth_id", "destination", "purpose", "scope", "retention_days",
        "snapshot_generation_id", "snapshot_generated_at", "records_sha256")}))


def validate_envelope_id(envelope_id) -> str:
    if not isinstance(envelope_id, str) or not re.fullmatch(r"[0-9a-f]{24}", envelope_id):
        raise ContractError("envelope_id_invalid")
    return envelope_id


def parse_receipt(raw):
    """Parse a /1 receipt or the exact historical LocalSink ack shape."""
    try:
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else \
            len(raw) if isinstance(raw, bytes) else len(canonical_text(raw).encode("utf-8"))
        if size > RECEIPT_MAX_BYTES:
            raise ContractError("receipt_too_large")
        receipt = json.loads(raw) if isinstance(raw, str | bytes) else \
            json.loads(canonical_text(raw))
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ContractError("receipt_invalid") from exc
    if not isinstance(receipt, dict):
        raise ContractError("receipt_not_object")
    validate_envelope_id(receipt.get("envelope_id"))
    legacy_receive = {"envelope_id", "records_sha256", "records", "acked_at"}
    legacy_delete = {"envelope_id", "deleted_at"}
    legacy_fields = set(receipt) if set(receipt) in (legacy_receive, legacy_delete) else None
    if set(receipt) == legacy_receive:
        receipt.update(contract=RECEIPT_CONTRACT, kind="receive", status="accepted")
    elif set(receipt) == legacy_delete:
        receipt.update(contract=RECEIPT_CONTRACT, kind="delete", deleted=True)
    elif receipt.get("contract") != RECEIPT_CONTRACT:
        raise ContractError("receipt_contract_mismatch")
    common = {"contract", "kind", "envelope_id"}
    if receipt.get("kind") == "receive" and receipt.get("status") == "accepted":
        required = common | {"status", "records_sha256", "records", "acked_at"}
        allowed = required | {"accepted"}
        if legacy_fields is None:
            required.add("accepted")
        if type(receipt.get("records")) is not int or receipt["records"] < 0 \
                or not finite_number(receipt.get("acked_at")):
            raise ContractError("receipt_metadata_invalid")
        if "accepted" in receipt:
            counts = receipt["accepted"]
            if not isinstance(counts, dict) \
                    or set(counts) - (set(RECORD_TYPES) | {"message_body", "patient_coverage"}) \
                    or any(type(n) is not int or n < 0 for n in counts.values()) \
                    or sum(counts.values()) != receipt["records"]:
                raise ContractError("receipt_counts_invalid")
    elif receipt.get("kind") == "receive" and receipt.get("status") == "rejected":
        required = common | {"status", "reasons", "rejected_at"}
        allowed = required | {"records_sha256"}
        reasons = receipt.get("reasons")
        if not finite_number(receipt.get("rejected_at")) \
                or not isinstance(reasons, list) or not 1 <= len(reasons) <= 20 \
                or any(not isinstance(r, str) or re.fullmatch(
                    r"[a-z][a-z0-9_]*(:[A-Za-z0-9_.-]{1,64})?", r) is None
                    for r in reasons):
            raise ContractError("receipt_reasons_invalid")
    elif receipt.get("kind") == "delete":
        required = common | {"deleted", "deleted_at"}
        allowed = required
        if type(receipt.get("deleted")) is not bool \
                or not finite_number(receipt.get("deleted_at")):
            raise ContractError("receipt_metadata_invalid")
    else:
        raise ContractError("receipt_status_invalid")
    if not required <= set(receipt) or set(receipt) - allowed:
        raise ContractError("receipt_fields_invalid")
    if "records_sha256" in receipt and (
            not isinstance(receipt["records_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", receipt["records_sha256"]) is None):
        raise ContractError("receipt_hash_invalid")
    return {key: receipt[key] for key in legacy_fields} if legacy_fields else receipt


def validate_export_envelope(envelope: dict) -> None:
    if isinstance(envelope, dict) and envelope.get("contract") == C1_CONTRACT:
        # validate_envelope already runs c1_contract.validate_record on every record
        try:
            validate_envelope(envelope)
        except C1ContractError as e:
            raise ContractError(e.code) from e
        return
    if not isinstance(envelope, dict) or envelope.get("contract") != EXT_CONTRACT:
        raise ContractError("sink_contract_mismatch")
    if envelope.get("scope") != "aggregate":
        raise ContractError("sink_scope_not_aggregate")
    records = envelope.get("records")
    if not isinstance(records, list):
        raise ContractError("records_not_list")
    for rec in records:
        _check_record_keys(rec)
        if rec["snapshot_generation_id"] != envelope.get("snapshot_generation_id"):
            raise ContractError("snapshot_generation_mixed")
    eid = validate_envelope_id(envelope.get("envelope_id"))
    allowed = {"contract", "auth_id", "destination", "purpose", "scope",
               "snapshot_generation_id", "snapshot_generated_at", "created_at",
               "retention_days", "record_count", "records_sha256", "records", "envelope_id"}
    if set(envelope) != allowed:
        raise ContractError("envelope_fields_invalid")
    for key in ("auth_id", "destination", "purpose"):
        if not isinstance(envelope[key], str) or not envelope[key].strip():
            raise ContractError("envelope_metadata_invalid")
    if type(envelope["retention_days"]) is not int or envelope["retention_days"] <= 0 \
            or not finite_number(envelope["created_at"]) \
            or not finite_number(envelope["snapshot_generated_at"]):
        raise ContractError("envelope_metadata_invalid")
    if type(envelope["record_count"]) is not int or envelope["record_count"] != len(records) \
            or envelope["records_sha256"] != _sha(canonical_text(records)) \
            or eid != _envelope_id(envelope):
        raise ContractError("envelope_integrity_invalid")
