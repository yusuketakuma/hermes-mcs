#!/usr/bin/env python3
"""Governed external export contract — disabled by default (T16).

A provider-neutral authorization + envelope + journal layer around the
T15 machine read model. There is deliberately NO network code here: the
only demonstrable destination is :class:`LocalSink`, an in-process fake
consumer backed by a directory. Wiring a real connector requires a
named provider, an access review and a separate permission — see
``docs/specs/external-export-contract.md``.

Guarantees enforced in code:

- every export attempt is bound to an ``mcs-ext-auth/1`` authorization
  file (purpose/actor/destination/scope/per-patient eligibility/expiry/
  retention) — revoked, expired, malformed or detail-scope
  authorizations are refused before any bytes move;
- every record leaving the process is aggregate-scope only and passes a
  field whitelist — the default /1 path rejects raw content at both the
  producer and the sink; explicit C1 validation permits only the paired
  ``message_body.body_text`` field, not other raw-content keys;
- sends are idempotent by ``envelope_id`` — an acknowledged envelope is
  never re-sent, and an unacknowledged one is HELD, never retried
  blindly (a lost acknowledgement leaves the outcome ``unknown`` until
  reconciled or explicitly re-authorized);
- withdrawal propagates a delete directive whose acknowledgement is
  independently journaled — an unacknowledged delete is held, not
  claimed;
- every action appends to an audit log — refusals included.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402,F401
from export_schema import (  # noqa: E402
    FORBIDDEN_KEYS, RECORD_TYPES, TYPE_FORBIDDEN, finite_number, validate_record)
from mcs_util import atomic_write  # noqa: E402

AUTH_CONTRACT = "mcs-ext-auth/1"
EXT_CONTRACT = "mcs-ext-export/1"
RECEIPT_CONTRACT = "mcs-ext-receipt/1"
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


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, obj) -> None:
    """Atomic write — a crash must not leave a torn journal/ack."""
    atomic_write(str(path), lambda fh: fh.write(_canonical(obj)), tmp_prefix=".t-")


def load_authorization(path, now: float | None = None, *, c1: bool = False) -> dict:
    """Load and validate an ``mcs-ext-auth/1`` authorization file.

    Refuses: unknown fields (typo-tolerance is how authorizations
    silently widen), missing required fields, non-aggregate scope,
    missing human confirmation, revocation and expiry."""
    now = time.time() if now is None else now
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError, RecursionError) as e:
        raise ContractError(f"auth_unreadable:{type(e).__name__}") from e
    return _validate_authorization(raw, now, c1=c1)


def _validate_authorization(raw, now: float, *, c1: bool = False) -> dict:
    from c1_contract import C1_FIELDS, C1ContractError, validate_profile

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


def _check_record_keys(rec: dict, *, c1: bool = False, message=None) -> None:
    """Nested scan — a smuggled fact dict carrying 'statement' inside a
    'facts' list must trip the same wall as a top-level key."""
    if not isinstance(rec, dict) or not isinstance(rec.get("type"), str):
        raise ContractError("record_not_object")
    forbidden = FORBIDDEN_KEYS | TYPE_FORBIDDEN.get(
        rec.get("type"), frozenset())
    stack = [{k: v for k, v in rec.items() if k != "body_text"}
             if c1 and rec.get("type") == "message_body" else rec]
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
        if c1:
            from c1_contract import validate_record as validate_c1_record
            validate_c1_record(rec, message=message)
        else:
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
    auth = _validate_authorization(auth, now)
    if not finite_number(snapshot_generated_at) or snapshot_generated_at > now:
        raise ContractError("snapshot_time_invalid")
    max_age = auth.get("max_snapshot_age_s")
    if max_age is not None and (snapshot_generated_at is None
                                or now - snapshot_generated_at > max_age):
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

    body = _canonical(kept)
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
    return _sha(_canonical({"auth_id": envelope["auth_id"],
                            "gen": envelope["snapshot_generation_id"],
                            "records_sha256": envelope["records_sha256"]}))[:24]


def _intent_hash(envelope: dict) -> str:
    if envelope.get("contract") == "mcs-ext-export/2":
        from c1_envelopes import intent_hash
        return intent_hash(envelope)
    # Reusing an auth_id cannot change a previously dispatched intent.
    return _sha(_canonical({key: envelope.get(key) for key in (
        "auth_id", "destination", "purpose", "scope", "retention_days",
        "snapshot_generation_id", "snapshot_generated_at", "records_sha256")}))


def _check_id(envelope_id) -> str:
    if not isinstance(envelope_id, str) or not re.fullmatch(r"[0-9a-f]{24}", envelope_id):
        raise ContractError("envelope_id_invalid")
    return envelope_id


def parse_receipt(raw):
    """Parse a /1 receipt or the exact historical LocalSink ack shape."""
    try:
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else \
            len(raw) if isinstance(raw, bytes) else len(_canonical(raw).encode("utf-8"))
        if size > RECEIPT_MAX_BYTES:
            raise ContractError("receipt_too_large")
        receipt = json.loads(raw) if isinstance(raw, str | bytes) else \
            json.loads(_canonical(raw))
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ContractError("receipt_invalid") from exc
    if not isinstance(receipt, dict):
        raise ContractError("receipt_not_object")
    _check_id(receipt.get("envelope_id"))
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


def _validate_envelope(envelope: dict) -> None:
    if isinstance(envelope, dict) and envelope.get("contract") == "mcs-ext-export/2":
        from c1_envelopes import C1ContractError, validate_envelope
        try:
            validate_envelope(envelope)
        except C1ContractError as e:
            raise ContractError(e.code) from e
        messages = {(r["project_id"], r["message_id"]): r
                    for r in envelope["records"] if r["type"] == "message"}
        for record in envelope["records"]:
            _check_record_keys(record, c1=True, message=messages.get(
                (record.get("project_id"), record.get("message_id"))))
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
    eid = _check_id(envelope.get("envelope_id"))
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
            or envelope["records_sha256"] != _sha(_canonical(records)) \
            or eid != _envelope_id(envelope):
        raise ContractError("envelope_integrity_invalid")


class LocalSink:
    """The reference fake consumer — a directory, nothing else.

    ``<dir>/envelopes/<id>.json``  received payloads (deduped by id)
    ``<dir>/acks/<id>.json``       delivery acknowledgements
    ``<dir>/deletions/<id>.json``  deletion acknowledgements
    Test knobs ``drop_ack``/``drop_delete_ack`` simulate a lost
    acknowledgement — the sink stored the payload but the sender
    cannot tell, which is exactly the ``unknown`` outcome the
    contract must hold rather than retry blindly.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.drop_ack = False
        self.drop_delete_ack = False

    def receive(self, envelope: dict) -> str:
        _validate_envelope(envelope)
        eid = envelope["envelope_id"]
        dest = self.root / "envelopes" / f"{eid}.json"
        if dest.exists():
            stored = json.loads(dest.read_text(encoding="utf-8"))
            _validate_envelope(stored)
            if _intent_hash(stored) != _intent_hash(envelope):
                raise ContractError("sink_identity_conflict")
        if not dest.exists():                      # idempotent store
            if envelope["contract"] == "mcs-ext-export/2":
                from c1_envelopes import encode_envelope
                payload = encode_envelope(envelope).decode("utf-8")
                atomic_write(str(dest), lambda fh: fh.write(payload), tmp_prefix=".t-")
            else:
                _write_json(dest, envelope)
        if not self.drop_ack:
            counts = {}
            for record in envelope["records"]:
                counts[record["type"]] = counts.get(record["type"], 0) + 1
            _write_json(self.root / "acks" / f"{eid}.json",
                        {"contract": RECEIPT_CONTRACT, "kind": "receive",
                         "status": "accepted", "accepted": counts,
                         "envelope_id": eid, "acked_at": time.time(),
                         "records": envelope["record_count"],
                         "records_sha256": envelope["records_sha256"]})
        return eid

    def receive_wire(self, raw: bytes) -> str:
        """Validate bounded /2 wire bytes before local reference receipt."""
        from c1_envelopes import C1ContractError, parse_envelope
        try:
            envelope = parse_envelope(raw)
        except C1ContractError as e:
            raise ContractError(e.code) from e
        return self.receive(envelope)

    def delete(self, envelope_id: str) -> None:
        _check_id(envelope_id)
        path = self.root / "envelopes" / f"{envelope_id}.json"
        path.unlink(missing_ok=True)
        if path.parent.exists():
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        if not self.drop_delete_ack:
            _write_json(
                self.root / "deletions" / f"{envelope_id}.json",
                {"contract": RECEIPT_CONTRACT, "kind": "delete",
                 "deleted": True, "envelope_id": envelope_id,
                 "deleted_at": time.time()})

    def has(self, envelope_id: str) -> bool:
        _check_id(envelope_id)
        return (self.root / "envelopes" / f"{envelope_id}.json").exists()

    def ack(self, envelope_id: str):
        _check_id(envelope_id)
        p = self.root / "acks" / f"{envelope_id}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return None

    def matches(self, envelope_id: str, records_sha256: str, intent_sha256: str) -> bool:
        _check_id(envelope_id)
        try:
            stored = json.loads((self.root / "envelopes" / f"{envelope_id}.json")
                                .read_text(encoding="utf-8"))
            _validate_envelope(stored)
            return stored["envelope_id"] == envelope_id \
                and stored["records_sha256"] == records_sha256 \
                and _intent_hash(stored) == intent_sha256
        except (OSError, ValueError, TypeError, ContractError, RecursionError):
            return False


class HandoffSink(LocalSink):
    """Synthetic staging outbox; receipts come only from manual import."""

    def __init__(self, root):
        super().__init__(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.drop_ack = True
        self.drop_delete_ack = True

    def receive(self, envelope):
        self.drop_ack = True
        eid = super().receive(envelope)
        os.chmod(self.root / "envelopes", 0o700)
        return eid

    def delete(self, envelope_id):
        # Removing an outbox copy is not evidence of receiver deletion.
        self.drop_delete_ack = True
        super().delete(envelope_id)

    def discard(self, envelope_id):
        """Remove only settled staging copies, preserving receipt evidence."""
        self.delete(envelope_id)
        (self.root / "withdrawals" / f"{envelope_id}.json").unlink(missing_ok=True)

    def stage_withdrawal(self, envelope_id, auth_id, reason):
        """Stage an mcs-ext-withdraw/1 directive; only a receiver delete receipt settles it.

        Cleanup (discard) never creates a directive; this explicit call does,
        and also stops handing over a still-staged envelope copy."""
        from c1_envelopes import C1ContractError, encode_withdrawal
        try:
            raw = encode_withdrawal(_check_id(envelope_id), auth_id, reason).decode("utf-8")
        except C1ContractError as e:
            raise ContractError(e.code) from e
        self.delete(envelope_id)
        folder = self.root / "withdrawals"
        folder.mkdir(mode=0o700, exist_ok=True)
        atomic_write(str(folder / f"{envelope_id}.json"), lambda fh: fh.write(raw),
                     tmp_prefix=".t-")


class GovernedExporter:
    """Journal + audit around one authorization and one sink.

    ``state_dir`` holds ``journal/<envelope_id>.json`` and
    ``audit.jsonl``. Statuses: ``sent`` (payload left, ack outcome
    unknown — HELD), ``acked``, ``held``, ``refused``, ``withdrawn``,
    ``delete_held``, ``rejected`` (terminal receiver refusal).
    """

    def __init__(self, state_dir):
        self.dir = Path(state_dir)
        (self.dir / "journal").mkdir(mode=0o700, parents=True, exist_ok=True)

    @contextmanager
    def _locked(self):
        fd = os.open(self.dir / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _audit(self, action: str, outcome: str, **kw) -> None:
        if "auth_id" not in kw:
            prior = self._journal(kw["envelope_id"]) if kw.get("envelope_id") else None
            kw["auth_id"] = prior.get("auth_id") if prior else None
        entry = {"ts": time.time(), "action": action,
                 "outcome": outcome, **kw}
        fd = os.open(self.dir / "audit.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_canonical(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _journal(self, envelope_id: str) -> dict | None:
        _check_id(envelope_id)
        p = self.dir / "journal" / f"{envelope_id}.json"
        if not p.exists():
            return None
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as e:
            raise ContractError("journal_unreadable") from e
        if not isinstance(entry, dict) or entry.get("envelope_id") != envelope_id \
                or entry.get("status") not in (
                    "sent", "held", "acked", "refused", "withdrawn", "delete_held",
                    "rejected") \
                or not isinstance(entry.get("attempts"), list):
            raise ContractError("journal_invalid")
        return entry

    def _set_journal(self, envelope_id: str, status: str, **kw) -> dict:
        entry = self._journal(envelope_id) or {
            "envelope_id": envelope_id, "attempts": []}
        entry["status"] = status
        entry.update(kw)
        entry["attempts"].append(
            {"ts": time.time(), "status": status})
        _write_json(self.dir / "journal" / f"{envelope_id}.json", entry)
        return entry

    def deliver(self, envelope: dict, sink: LocalSink,
                auth_path=None) -> dict:
        """One governed delivery attempt — idempotent, fail-held."""
        with self._locked():
            return self._deliver(envelope, sink, auth_path)

    def _deliver(self, envelope, sink, auth_path):
        eid = None
        auth = None
        try:
            # Own an immutable JSON copy across validation and dispatch.
            envelope = json.loads(_canonical(envelope))
            _validate_envelope(envelope)
            eid = envelope["envelope_id"]
            if auth_path is None:
                raise ContractError("auth_required")
            c1 = envelope["contract"] == "mcs-ext-export/2"
            now = time.time()
            auth = load_authorization(auth_path, now, c1=c1)
            if c1:
                if any(envelope[key] != auth[key] for key in (
                        "auth_id", "destination", "purpose", "scope", "retention_days")):
                    raise ContractError("authorization_envelope_mismatch")
                if envelope["created_at"] > now \
                        or envelope["snapshot_generated_at"] > now:
                    raise ContractError("snapshot_time_invalid")
                if now - envelope["snapshot_generated_at"] > auth["max_snapshot_age_s"]:
                    raise ContractError("snapshot_stale")
            else:
                expected = build_envelope(envelope["records"], auth,
                                          envelope["snapshot_generated_at"], now=now)
                if any(envelope[key] != expected[key] for key in expected if key != "created_at"):
                    raise ContractError("authorization_envelope_mismatch")
            prior = self._journal(eid)
            sink_root = str(sink.root.resolve())
            if prior and prior["status"] != "refused" and prior.get("sink_root") != sink_root:
                raise ContractError("sink_binding_mismatch")
            if prior and prior["status"] != "refused" \
                    and prior.get("intent_sha256") != _intent_hash(envelope):
                raise ContractError("envelope_identity_conflict")
            if prior and prior["status"] == "acked":
                self._audit("deliver", "already_acked",
                            envelope_id=eid)
                return {"status": "already_acked", "envelope_id": eid}
            if prior and prior["status"] == "rejected":
                self._audit("deliver", "rejected", envelope_id=eid)
                return {"status": "rejected", "envelope_id": eid}
            if prior and prior["status"] in ("sent", "held"):
                # outcome is UNKNOWN — holding beats re-sending PHI
                self._audit("deliver", "held_unknown_outcome",
                            envelope_id=eid)
                return {"status": "held",
                        "reason": "ack_unknown", "envelope_id": eid}
            if prior and prior["status"] in ("withdrawn",
                                             "delete_held"):
                # a withdrawn envelope never silently re-sends
                self._audit("deliver", "refused", envelope_id=eid,
                            reason="envelope_withdrawn")
                return {"status": "refused",
                        "reason": "envelope_withdrawn",
                        "envelope_id": eid}
            # Reserve durably before any effect. A crash, sink exception or
            # lost response leaves sent/unknown, never an unjournaled retry.
            self._set_journal(eid, "sent", sink_root=sink_root,
                              auth_id=auth["auth_id"],
                              intent_sha256=_intent_hash(envelope),
                              records_sha256=envelope["records_sha256"],
                              record_count=envelope["record_count"],
                              record_types=dict(Counter(r["type"] for r in envelope["records"])),
                              export_contract=envelope["contract"],
                              part=envelope.get("part"),
                              snapshot_generation_id=envelope["snapshot_generation_id"])
            try:
                sink.receive(envelope)
                ack = sink.ack(eid)
            except Exception as exc:
                self._audit("deliver", "sent_ack_unknown", envelope_id=eid,
                            auth_id=auth["auth_id"], reason=type(exc).__name__)
                return {"status": "held", "reason": "ack_unknown", "envelope_id": eid}
            if self._valid_rejection(ack, self._journal(eid)):
                self._set_journal(eid, "rejected")
                self._audit("deliver", "rejected", envelope_id=eid)
                return {"status": "rejected", "envelope_id": eid}
            if not self._valid_ack(ack, self._journal(eid)) \
                    or not sink.matches(eid, envelope["records_sha256"], _intent_hash(envelope)):
                self._audit("deliver", "sent_ack_unknown",
                            envelope_id=eid, auth_id=auth["auth_id"])
                return {"status": "held",
                        "reason": "ack_unknown", "envelope_id": eid}
            self._set_journal(eid, "acked", acked_at=ack.get("acked_at"))
            self._audit("deliver", "acked", envelope_id=eid, auth_id=auth["auth_id"])
            return {"status": "acked", "envelope_id": eid}
        except (ContractError, ValueError, TypeError, RecursionError) as e:
            # A later authorization refusal must not erase an earlier send
            # or withdrawal; that would turn an unknown outcome into a retry.
            if eid is not None and not (self.dir / "journal" / f"{eid}.json").exists():
                self._set_journal(eid, "refused")
            reason = str(e) if isinstance(e, ContractError) else "envelope_invalid"
            self._audit("deliver", "refused", envelope_id=eid,
                        auth_id=auth.get("auth_id") if auth else None, reason=reason)
            return {"status": "refused", "reason": reason,
                    "envelope_id": eid}

    @staticmethod
    def _valid_ack(ack, entry):
        try:
            ack = parse_receipt(ack)
        except ContractError:
            return False
        if entry is not None and entry.get("export_contract") == "mcs-ext-export/2" \
                and ack.get("accepted") != entry.get("record_types"):
            return False
        if entry is not None and "accepted" in ack and "record_types" in entry \
                and ack["accepted"] != entry["record_types"]:
            return False
        return ack.get("kind", "receive") == "receive" \
            and ack.get("status", "accepted") == "accepted" \
            and entry is not None \
            and ack.get("envelope_id") == entry["envelope_id"] \
            and finite_number(ack.get("acked_at")) \
            and type(ack.get("records")) is int \
            and isinstance(entry.get("records_sha256"), str) \
            and ack.get("records_sha256") == entry.get("records_sha256") \
            and ack.get("records") == entry.get("record_count")

    @staticmethod
    def _valid_rejection(receipt, entry):
        try:
            receipt = parse_receipt(receipt)
        except ContractError:
            return False
        return entry is not None and receipt.get("kind") == "receive" \
            and receipt.get("status") == "rejected" \
            and receipt["envelope_id"] == entry["envelope_id"] \
            and ("records_sha256" not in receipt
                 or receipt["records_sha256"] == entry.get("records_sha256"))

    def import_receipts(self, path, sink: LocalSink):
        """Reconcile manually supplied JSON/NDJSON receipts; never send."""
        source = Path(path)
        files = sorted(p for p in source.iterdir() if p.is_file()) \
            if source.is_dir() else [source]
        receipts = []
        for file in files:
            raw = file.read_bytes()
            try:
                receipts.append(parse_receipt(raw))
            except ContractError:
                # NDJSON is only a bundle, each line keeps the /1 contract.
                lines = [line for line in raw.splitlines() if line.strip()]
                if not lines:
                    raise ContractError("receipt_not_object") from None
                receipts.extend(parse_receipt(line) for line in lines)
        results = []
        sink_root = str(sink.root.resolve())
        with self._locked():
            # Bind every receipt before applying any: a bundle is all or nothing.
            for receipt in receipts:
                prior = self._journal(receipt["envelope_id"])
                if prior is None or prior.get("sink_root") != sink_root:
                    raise ContractError("receipt_journal_mismatch")
            for receipt in receipts:
                eid = receipt["envelope_id"]
                prior = self._journal(eid)
                kind = receipt.get("kind", "delete" if "deleted_at" in receipt else "receive")
                if kind == "receive":
                    if self._valid_rejection(receipt, prior) \
                            and self._valid_ack(sink.ack(eid), prior):
                        # Conflicting evidence never overwrites an accepted receipt.
                        self._audit("reconcile", "receipt_conflict", envelope_id=eid)
                        results.append({"status": "held", "reason": "receipt_conflict",
                                        "envelope_id": eid})
                        continue
                    valid = self._valid_ack(receipt, prior) \
                        or self._valid_rejection(receipt, prior)
                    folder = "acks"
                else:
                    valid = prior["status"] in ("delete_held", "withdrawn")
                    folder = "deletions"
                if not valid:
                    self._audit("reconcile", "receipt_mismatch", envelope_id=eid)
                    results.append({"status": "held", "reason": "receipt_mismatch",
                                    "envelope_id": eid})
                    continue
                if prior["status"] not in ("acked", "rejected", "withdrawn"):
                    _write_json(sink.root / folder / f"{eid}.json", receipt)
                results.append(self._reconcile(eid, sink))
        return results

    def reconcile(self, envelope_id: str, sink: LocalSink) -> dict:
        """Resolve an unknown outcome by inspecting the sink — a
        stored payload + lost ack reconciles to acked WITHOUT a
        retransmission; absent payload stays held for a human."""
        with self._locked():
            try:
                return self._reconcile(envelope_id, sink)
            except ContractError as exc:
                self._audit("reconcile", "refused", auth_id=None, reason=str(exc))
                raise

    def _reconcile(self, envelope_id, sink):
        prior = self._journal(envelope_id)
        if prior is None:
            self._audit("reconcile", "no_journal", envelope_id=envelope_id)
            return {"status": "no_journal", "envelope_id": envelope_id}
        if prior.get("sink_root") != str(sink.root.resolve()):
            raise ContractError("sink_binding_mismatch")
        if prior["status"] in ("acked", "withdrawn", "refused", "rejected"):
            if isinstance(sink, HandoffSink) and prior["status"] != "refused":
                sink.discard(envelope_id)
            self._audit("reconcile", prior["status"], envelope_id=envelope_id)
            return {"status": prior["status"], "envelope_id": envelope_id}
        if prior["status"] == "delete_held":
            ack = self._delete_ack(sink, envelope_id)
            if ack is not None and not sink.has(envelope_id):
                self._set_journal(envelope_id, "withdrawn")
                if isinstance(sink, HandoffSink):
                    sink.discard(envelope_id)
                self._audit("reconcile", "withdrawn", envelope_id=envelope_id)
                return {"status": "withdrawn", "envelope_id": envelope_id}
            self._audit("reconcile", "delete_held", envelope_id=envelope_id)
            return {"status": "delete_held", "envelope_id": envelope_id}
        if self._valid_rejection(sink.ack(envelope_id), prior):
            self._set_journal(envelope_id, "rejected")
            if isinstance(sink, HandoffSink):
                sink.discard(envelope_id)
            self._audit("reconcile", "rejected", envelope_id=envelope_id)
            return {"status": "rejected", "envelope_id": envelope_id}
        if sink.matches(envelope_id, prior.get("records_sha256"), prior.get("intent_sha256")) \
                and self._valid_ack(sink.ack(envelope_id), prior):
            self._set_journal(envelope_id, "acked")
            if isinstance(sink, HandoffSink):
                sink.discard(envelope_id)
            self._audit("reconcile", "acked_by_sink_state",
                        envelope_id=envelope_id)
            return {"status": "acked", "envelope_id": envelope_id}
        if sink.has(envelope_id):
            # payload is there, ack is not — still unknown, still held
            self._audit("reconcile", "still_unknown",
                        envelope_id=envelope_id)
            return {"status": "held", "reason": "ack_unknown",
                    "envelope_id": envelope_id}
        self._audit("reconcile", "not_received",
                    envelope_id=envelope_id)
        return {"status": "held", "reason": "not_received",
                "envelope_id": envelope_id}

    def withdraw(self, envelope_id: str, sink: LocalSink,
                 reason: str = "operator_request") -> dict:
        """Propagate a deletion directive; its acknowledgement is
        journaled independently — an unacknowledged delete is held."""
        with self._locked():
            try:
                return self._withdraw(envelope_id, sink, reason)
            except ContractError as exc:
                self._audit("withdraw", "refused", auth_id=None, reason=str(exc))
                raise

    def withdraw_generation(self, generation_id: str, sink: LocalSink,
                            reason: str = "operator_request") -> list:
        """Expand one generation from the durable journal, never the outbox."""
        with self._locked():
            try:
                targets = []
                for path in sorted(_journal_paths(self.dir)):
                    if re.fullmatch(r"[0-9a-f]{24}\.json", path.name) is None:
                        continue
                    entry = self._journal(path.stem)
                    if entry.get("snapshot_generation_id") == generation_id \
                            and entry["status"] != "refused":
                        if entry.get("sink_root") != str(sink.root.resolve()):
                            raise ContractError("sink_binding_mismatch")
                        targets.append(entry["envelope_id"])
                if not targets:
                    raise ContractError("withdraw_generation_unknown")
                return [self._withdraw(eid, sink, reason) for eid in targets]
            except ContractError as exc:
                self._audit("withdraw", "refused", auth_id=None, reason=str(exc))
                raise

    def _withdraw(self, envelope_id, sink, reason="operator_request"):
        from c1_envelopes import WITHDRAW_REASONS
        if reason not in WITHDRAW_REASONS:
            raise ContractError("withdraw_reason_invalid")
        prior = self._journal(envelope_id)
        if prior is None:
            raise ContractError("withdraw_no_delivery")
        if prior.get("sink_root") != str(sink.root.resolve()):
            raise ContractError("sink_binding_mismatch")
        if prior["status"] in ("withdrawn", "delete_held"):
            return self._reconcile(envelope_id, sink)
        if isinstance(sink, HandoffSink) and not prior.get("auth_id"):
            raise ContractError("withdraw_auth_unknown")
        self._set_journal(envelope_id, "delete_held", withdraw_reason=reason)
        try:
            if isinstance(sink, HandoffSink):
                sink.stage_withdrawal(envelope_id, prior["auth_id"], reason)
            else:
                sink.delete(envelope_id)
        except Exception as exc:
            self._audit("withdraw", "delete_ack_unknown", envelope_id=envelope_id,
                        reason=type(exc).__name__)
            return {"status": "delete_held", "envelope_id": envelope_id}
        ack = self._delete_ack(sink, envelope_id)
        if ack is None or sink.has(envelope_id):
            self._set_journal(envelope_id, "delete_held")
            self._audit("withdraw", "delete_ack_unknown",
                        envelope_id=envelope_id)
            return {"status": "delete_held",
                    "envelope_id": envelope_id}
        self._set_journal(envelope_id, "withdrawn")
        self._audit("withdraw", "withdrawn", envelope_id=envelope_id)
        return {"status": "withdrawn", "envelope_id": envelope_id}

    @staticmethod
    def _delete_ack(sink: LocalSink, envelope_id: str):
        _check_id(envelope_id)
        p = sink.root / "deletions" / f"{envelope_id}.json"
        if not p.exists():
            return None
        try:
            ack = parse_receipt(p.read_bytes())
        except (OSError, ContractError):
            return None
        if ack.get("envelope_id") == envelope_id and ack.get("kind", "delete") == "delete" \
                and ack.get("deleted", True) is True:
            return ack
        return None

    def audit_entries(self) -> list:
        p = self.dir / "audit.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in
                p.read_text(encoding="utf-8").splitlines() if line]


def _read_health_json(path: Path):
    """Read at most one bounded regular file without following a link or FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > RECEIPT_MAX_BYTES:
            raise ContractError("health_file_size_or_type")
        raw = stream.read(RECEIPT_MAX_BYTES + 1)
        if len(raw) > RECEIPT_MAX_BYTES:
            raise ContractError("health_file_size_or_type")
        return json.loads(raw)


def _journal_paths(state, max_entries=None):
    """Scan the local journal; only health bounds it (invalid names count too).

    Withdraw and reconcile stay uncapped: the journal is never pruned, so a
    cap would block deletion propagation once enough parts were handed off.
    """
    root = Path(state).absolute()
    directory = root / "journal"
    if root.resolve() != root or directory.is_symlink():
        raise ContractError("journal_directory_invalid")
    with os.scandir(directory) as entries:
        for count, entry in enumerate(entries, 1):
            if max_entries is not None and count > max_entries:
                raise ContractError("journal_entry_limit")
            yield Path(entry.path)


class AuthorizationHealth(TypedDict):
    state: str
    seconds_left: float | None


class ExportHealth(TypedDict):
    status: str
    complete: bool
    counts: dict[str, int]
    reasons: list[str]
    oldest_held_age_s: float | None
    authorization: AuthorizationHealth
    payload_checked: bool


def health(state: str | Path, *, auth_path: str | Path | None = None,
           sink_root: str | Path | None = None, max_entries: int = 1000) -> ExportHealth:
    """Read only bounded counts and fixed diagnostics, never journal/actor text."""
    if type(max_entries) is not int or not 1 <= max_entries <= 10_000:
        raise ContractError("health_entry_limit_invalid")
    counts: dict[str, int] = dict.fromkeys(
        ("held", "delete_held", "acked", "withdrawn", "refused", "rejected", "corrupt"), 0)
    report: ExportHealth = {
        "status": "unknown", "complete": True, "counts": counts,
        "reasons": [], "oldest_held_age_s": None,
        "authorization": {"state": "not_checked", "seconds_left": None},
        "payload_checked": sink_root is not None}
    now = time.time()
    held_times = []
    reasons = set()
    try:
        for path in _journal_paths(state, max_entries):
            try:
                eid = _check_id(path.stem)
                if path.suffix != ".json":
                    raise ContractError("journal_invalid")
                entry = _read_health_json(path)
                if (not isinstance(entry, dict) or entry.get("envelope_id") != eid
                        or entry.get("status") not in (
                            "sent", "held", "delete_held", "acked", "withdrawn",
                            "refused", "rejected")
                        or not isinstance(entry.get("attempts"), list)):
                    raise ContractError("journal_invalid")
                status = "held" if entry["status"] == "sent" else entry["status"]
                counts[status] += 1
                if status == "held":
                    reasons.add("ack_unknown")
                    timestamps = [attempt["ts"] for attempt in entry["attempts"]
                                  if isinstance(attempt, dict)
                                  and finite_number(attempt.get("ts"))
                                  and 0 <= attempt["ts"] <= now]
                    if timestamps:
                        held_times.append(min(timestamps))
                    else:
                        reasons.add("held_age_unknown")
                    if sink_root is not None:
                        sink = Path(sink_root)
                        if entry.get("sink_root") != str(sink.resolve()):
                            reasons.add("sink_binding_mismatch")
                        else:
                            payload = sink / "envelopes" / f"{eid}.json"
                            try:
                                if not stat.S_ISREG(payload.lstat().st_mode):
                                    reasons.add("payload_missing")
                            except OSError:
                                reasons.add("payload_missing")
                elif status == "delete_held":
                    reasons.add("receiver_delete_unconfirmed")
            except (ContractError, OSError, ValueError, TypeError, RecursionError):
                counts["corrupt"] += 1
                reasons.add("journal_invalid_or_unreadable")
                report["complete"] = False
    except ContractError as exc:
        reasons.add(str(exc))
        report["complete"] = False
    except OSError:
        reasons.add("journal_scan_incomplete")
        report["complete"] = False
    if held_times:
        report["oldest_held_age_s"] = now - min(held_times)
    if auth_path is not None:
        try:
            raw = _read_health_json(Path(auth_path))
            from c1_contract import C1_FIELDS
            fields = raw.get("fields") if isinstance(raw, dict) else None
            _validate_authorization(raw, now, c1=isinstance(fields, list)
                                    and set(fields) == set(C1_FIELDS))
            report["authorization"] = {
                "state": "valid", "seconds_left": raw["expires_at"] - now}
        except ContractError as exc:
            token = str(exc).split(":", 1)[0]
            state = {"auth_expired": "expired", "auth_revoked": "revoked"}.get(
                token, "invalid")
            report["authorization"] = {"state": state, "seconds_left": None}
            reasons.add("authorization_" + state)
        except (OSError, ValueError, TypeError, RecursionError):
            report["authorization"] = {"state": "unreadable", "seconds_left": None}
            reasons.add("authorization_unreadable")
    report["reasons"] = sorted(reasons)
    report["status"] = "ok" if report["complete"] and not reasons else "unknown"
    return report


_RECEIVER_FAULTS = ("receiver_capacity", "receiver_state", "receiver_directory",
                    "receiver_lock", "receiver_source", "receiver_limits", "receiver_time")


def _write_new_receipts(path: str, bundle: str) -> None:
    # Publish a complete private file without replacing any competing file
    # or symlink. The initial --receipts-out check alone cannot protect it.
    parent = str(Path(path).parent)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".r-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(bundle)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise ContractError("receipts_out_exists") from None
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        with suppress(OSError):
            os.unlink(tmp)


def _receive(args) -> dict:
    """Feed handed-over files to the synthetic reference receiver.

    Contract refusals become terminal rejected (or deleted:false) receipts.
    Receiver storage faults raise instead, so the sender stays held. The
    transport tally never claims collection completeness; that is reported
    separately from the receiver's own diagnostics."""
    from c1_envelopes import MAX_WIRE_BYTES, WITHDRAW_CONTRACT, C1ContractError
    from c1_receiver import ReferenceReceiver
    if (args.input is None) != (args.receipts_out is None):
        raise ContractError("receive_input_and_receipts_out_required")
    if args.receipts_out is not None and os.path.lexists(args.receipts_out):
        raise ContractError("receipts_out_exists")
    try:
        receiver = ReferenceReceiver(args.receiver_root, source_label=args.source_label)
        # Maintenance is part of this mutating receive command, including a
        # run without input. Historical receipt/tombstone metadata is retained.
        expiry = receiver.expire()
    except C1ContractError as e:
        raise ContractError(e.code) from e
    transport: Counter = Counter()
    receipts = []
    if args.input is not None:
        source = Path(args.input)
        if source.is_symlink():
            raise ContractError("receive_input_unsafe")
        files = sorted(p for p in source.rglob("*.json") if p.is_file() and not p.is_symlink()
                       and not p.name.startswith(".")) if source.is_dir() else [source]
        for path in files:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                raise ContractError("receive_input_unsafe") from None
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ContractError("receive_input_unsafe")
                raw = stream.read(MAX_WIRE_BYTES + 1)
            try:
                peek = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                peek = None
            eid = peek.get("envelope_id") if isinstance(peek, dict) else None
            eid = eid if isinstance(eid, str) and re.fullmatch(r"[0-9a-f]{24}", eid) else None
            contract = peek.get("contract") if isinstance(peek, dict) else None
            if contract is not None and contract not in ("mcs-ext-export/2", WITHDRAW_CONTRACT):
                # Receipts or /1 copies in the same outbox are not inputs to this receiver.
                transport["skipped"] += 1
                continue
            withdraw = contract == WITHDRAW_CONTRACT
            try:
                if withdraw:
                    receipts.append(receiver.withdraw_wire(raw))
                    transport["deleted"] += 1
                else:
                    receipts.append(receiver.ack(receiver.receive_wire(raw)))
                    transport["accepted"] += 1
                continue
            except C1ContractError as e:
                if e.code.startswith(_RECEIVER_FAULTS):
                    raise ContractError(e.code) from e
                code = e.code
            if eid is None:
                transport["unidentified"] += 1
            elif withdraw:
                transport["delete_failed"] += 1
                receipts.append({"contract": RECEIPT_CONTRACT, "kind": "delete",
                                 "envelope_id": eid, "deleted": False, "deleted_at": time.time()})
            else:
                transport["rejected"] += 1
                receipt = {"contract": RECEIPT_CONTRACT, "kind": "receive", "status": "rejected",
                           "envelope_id": eid, "reasons": [code], "rejected_at": time.time()}
                claimed = peek.get("records_sha256")
                if isinstance(claimed, str) and re.fullmatch(r"[0-9a-f]{64}", claimed):
                    receipt["records_sha256"] = claimed
                receipts.append(receipt)
        bundle = "".join(_canonical(parse_receipt(r)) + "\n" for r in receipts)
        _write_new_receipts(args.receipts_out, bundle)
    return {"transport": dict(transport), "receipts": len(receipts),
            "expired": expiry["expired"],
            "receiver": receiver.diagnostics(), "binding": "synthetic_reference_receiver"}


def main(argv=None) -> int:
    import argparse
    from c1_envelopes import build_envelopes, encode_envelope
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest="action", required=True)
    for action in ("deliver", "handoff", "reconcile", "withdraw", "health"):
        command = commands.add_parser(action)
        command.add_argument("--state", required=action != "handoff",
                             help="explicit local journal/audit dir")
        if action in ("deliver", "handoff", "health"):
            command.add_argument("--auth", required=action == "deliver",
                                 help="mcs-ext-auth/1 authorization file")
        if action in ("deliver", "handoff"):
            source = command.add_mutually_exclusive_group(required=action == "deliver")
            source.add_argument("--records", help="existing export.jsonl")
            source.add_argument("--snapshot", help="published static snapshot, requires --c1")
            command.add_argument("--c1", action="store_true", help="explicit export/2")
            command.add_argument("--only-with-facts", action="store_true")
            command.add_argument("--since-days", type=int)
            command.add_argument("--classify-senders", action="store_true",
                                 help="opt-in sender_kind from self ID/explicit orgs/profession")
            command.add_argument("--signal-limit", type=int, default=200)
            command.add_argument("--max-bytes", type=int, default=1_048_576)
            command.add_argument("--dry-run", action="store_true")
        command.add_argument("--sink", required=action not in ("health", "handoff"),
                             help="explicit local outbox or fake consumer directory")
        if action in ("deliver", "reconcile", "withdraw"):
            command.add_argument("--sink-kind", choices=("local", "handoff"),
                                 default="local" if action == "deliver" else "handoff",
                                 help="local self-acks as a fake consumer; handoff never self-acks")
        if action == "reconcile":
            target = command.add_mutually_exclusive_group(required=True)
            target.add_argument("--envelope-id")
            target.add_argument("--all", action="store_true")
            target.add_argument("--receipts", help="manual receipt JSON, NDJSON or directory")
        if action == "withdraw":
            target = command.add_mutually_exclusive_group(required=True)
            target.add_argument("--envelope-id")
            target.add_argument("--generation", help="every journaled envelope of one generation")
            command.add_argument("--reason", default="operator_request",
                                 choices=("operator_request", "authorization_revoked",
                                          "content_correction", "generation_set_conflict"))
        if action == "health":
            command.add_argument("--max-entries", type=int, default=1000,
                                 help="bounded journal scan, 1..10000")
    receive = commands.add_parser(
        "receive", help="synthetic /2 reference receiver; transport receipts are not currentness")
    receive.add_argument("--receiver-root", required=True, help="dedicated private directory")
    receive.add_argument("--source-label", required=True)
    receive.add_argument("--input", help="handed-over envelope/directive file or outbox directory")
    receive.add_argument("--receipts-out", help="new NDJSON receipt bundle, required with --input")
    hints = commands.add_parser("link-hints", help="Hermes local terminal only, no export")
    hints.add_argument("--snapshot", help="published static snapshot")
    hints.add_argument("--limit", type=int, default=200)
    hints.add_argument("--cursor")
    argv = list(sys.argv[1:] if argv is None else argv)
    legacy = bool(argv and argv[0].startswith("--") and argv[0] != "--help")
    if legacy:
        argv.insert(0, "deliver")
    args = ap.parse_args(argv)
    try:
        res: dict[str, object]
        env = None
        envelopes = []
        selection = None
        if args.action == "link-hints":
            from mcs_runtime import mode
            from mcs_setup import HOME, load_config
            from mcs_view import View
            if mode(load_config()) != "hermes" or not sys.stdout.isatty():
                raise ContractError("link_hints_local_terminal_required")
            view = View(args.snapshot or str(
                Path(HOME) / "data" / "snapshots" / "ledger-snapshot.db"))
            try:
                page = view._page(
                    """SELECT p.project_id AS _key, p.project_id, p.patient_name,
                       (SELECT MAX(m.posted_at_ts) FROM messages m
                        WHERE m.project_id=p.project_id) AS last_message_at
                       FROM patients p WHERE 1=1""",
                    [], ("p.project_id",), ["link-hints"], args.limit, args.cursor)
                items = page["items"]
                assert isinstance(items, list)
                for row in items:
                    timestamp = row["last_message_at"]
                    if not isinstance(timestamp, int | float) \
                            or not finite_number(timestamp) or timestamp <= 0:
                        row["last_message_at"] = None
                res = {**page, "snapshot": view.meta, "binding": "local_hint_only"}
                print(json.dumps(res, ensure_ascii=False, allow_nan=False))
                return 0
            finally:
                view.close()
        if args.action == "handoff" and not any(
                (args.auth, args.state, args.sink, args.records, args.snapshot)):
            from mcs_setup import HOME, load_config
            profile = load_config().get("ext_export")
            if not isinstance(profile, dict) or set(profile) != {
                    "auth", "state_dir", "outbox", "since_days"}:
                raise ContractError("handoff_profile_required")
            for key in ("auth", "state_dir", "outbox"):
                if not isinstance(profile[key], str) or not Path(profile[key]).is_absolute():
                    raise ContractError("handoff_profile_path_invalid")
            if type(profile["since_days"]) is not int or profile["since_days"] < 1:
                raise ContractError("handoff_profile_window_invalid")
            args.auth, args.state, args.sink = (
                profile["auth"], profile["state_dir"], profile["outbox"])
            args.snapshot = str(Path(HOME) / "data" / "snapshots" / "ledger-snapshot.db")
            args.c1, args.only_with_facts = True, True
            if args.since_days is None:
                args.since_days = profile["since_days"]
        if args.action == "handoff" and not all(
                (args.auth, args.state, args.sink, args.records or args.snapshot)):
            ap.error("handoff requires --auth, --state, --sink and one source")
        if args.action == "receive":
            res = _receive(args)
            print(json.dumps(res, ensure_ascii=False, allow_nan=False))
            return 0 if set(res["transport"]) <= {"accepted", "deleted", "skipped"} else 2
        if args.action == "health":
            res = health(args.state, auth_path=args.auth, sink_root=args.sink,
                         max_entries=args.max_entries)
            print(json.dumps(res, ensure_ascii=False, allow_nan=False))
            return 0 if res["status"] == "ok" else 2
        if args.action in ("deliver", "handoff"):
            if not args.c1 and (args.snapshot or args.only_with_facts
                               or args.since_days is not None or args.signal_limit != 200
                               or args.max_bytes != 1_048_576 or args.classify_senders):
                raise ContractError("c1_option_required")
            if args.classify_senders and not args.snapshot:
                raise ContractError("classify_senders_snapshot_required")
            auth = load_authorization(args.auth, c1=args.c1)
            if args.c1:
                from c1_contract import C1ContractError
                from c1_records import SnapshotError, assemble_records, select_records
                for directory in (args.state, args.sink):
                    path = Path(directory)
                    if not path.is_absolute() or path.is_symlink():
                        raise ContractError("c1_private_directory_required")
                    if path.exists():
                        st = path.stat()
                        if not path.is_dir() or st.st_uid != os.getuid() or st.st_mode & 0o077:
                            raise ContractError("c1_private_directory_required")
                try:
                    if args.snapshot:
                        from mcs_view import View
                        view = View(args.snapshot)
                        try:
                            policy = None
                            if args.classify_senders:
                                from mcs_setup import load_config
                                from mcs_signals import self_sender_id
                                orgs = (load_config().get("signals") or {}).get(
                                    "self_organizations")
                                policy = {"self_sender_id": self_sender_id(view.db),
                                          "self_organizations": [
                                              o for o in orgs if isinstance(o, str) and o]
                                          if isinstance(orgs, list) else []}
                            records = assemble_records(
                                view.db, signal_limit=args.signal_limit,
                                sender_policy=policy)["records"]
                        finally:
                            view.close()
                    else:
                        records = [json.loads(line) for line in
                                   Path(args.records).read_text(
                                       encoding="utf-8").splitlines() if line.strip()]
                    selection = select_records(
                        records, auth["fields"], only_with_facts=args.only_with_facts,
                        since_days=args.since_days)
                    envelopes = build_envelopes(
                        selection["records"], profile={k: auth[k] for k in (
                            "fields", "patients", "max_snapshot_age_s", "retention_days")},
                        auth_id=auth["auth_id"], destination=auth["destination"],
                        purpose=auth["purpose"], now=time.time(), max_bytes=args.max_bytes)
                    for envelope in envelopes:
                        _validate_envelope(envelope)
                except (C1ContractError, SnapshotError) as e:
                    raise ContractError(e.code) from e
            else:
                records = [json.loads(line) for line in
                           Path(args.records).read_text(
                               encoding="utf-8").splitlines() if line.strip()]
                gen_at = next((r.get("snapshot", {}).get("generated_at")
                               for r in records if r.get("type") == "meta"), None)
                env = build_envelope(records, auth, gen_at)
                envelopes = [env]
            if args.dry_run:
                res = {"status": "dry_run", "envelopes": [{
                    "envelope_id": e["envelope_id"], "part": e.get("part"),
                    "records": e["record_count"],
                    "bytes": len(encode_envelope(e)) if args.c1
                    else len(_canonical(e).encode("utf-8"))} for e in envelopes]}
                if selection is not None:
                    res.update({k: selection[k] for k in ("dropped_types", "messages_dropped")})
                print(json.dumps(res, ensure_ascii=False, allow_nan=False))
                return 0
        elif getattr(args, "envelope_id", None) is not None:
            _check_id(args.envelope_id)
        kind = "handoff" if args.action == "handoff" else args.sink_kind
        sink = HandoffSink(args.sink) if kind == "handoff" else LocalSink(args.sink)
        exporter = GovernedExporter(args.state)
        if args.action in ("deliver", "handoff"):
            results = [exporter.deliver(e, sink, auth_path=args.auth) for e in envelopes]
            if args.c1:
                assert selection is not None
                res = {"envelopes": [{
                    **result, "part": e.get("part"), "records": e["record_count"],
                    "bytes": len(encode_envelope(e))}
                    for e, result in zip(envelopes, results)]}
                res.update({k: selection[k] for k in ("dropped_types", "messages_dropped")})
                kept = sum(r["type"] == "message" for r in selection["records"])
                res["messages_kept"] = kept
                res["warnings"] = ["messages_kept_zero"] if kept == 0 else []
            else:
                res = results[0]
        elif args.action == "withdraw" and args.generation is not None:
            results = exporter.withdraw_generation(args.generation, sink, args.reason)
            res = {"results": results}
        elif args.action == "withdraw":
            res = exporter.withdraw(args.envelope_id, sink, args.reason)
            results = [res]
        elif args.receipts is not None:
            results = exporter.import_receipts(args.receipts, sink)
            res = {"results": results}
        elif args.all:
            results = [exporter.reconcile(path.stem, sink)
                       for path in sorted(_journal_paths(args.state))
                       if re.fullmatch(r"[0-9a-f]{24}\.json", path.name)]
            res = {"results": results}
        else:
            res = exporter.reconcile(args.envelope_id, sink)
            results = [res]
        print(json.dumps(res, ensure_ascii=False, allow_nan=False))
        statuses = {result["status"] for result in results}
        if not statuses or statuses - {
                "acked", "already_acked", "withdrawn", "held", "delete_held"}:
            return 1
        if statuses & {"held", "delete_held"}:
            return 0 if args.action in ("deliver", "handoff") and kind == "handoff" else \
                1 if legacy else 2
        return 0
    except (ContractError, OSError, ValueError, TypeError, RecursionError) as e:
        reason = str(e) if isinstance(e, ContractError) else type(e).__name__
        print(json.dumps({"status": "refused", "reason": reason},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
