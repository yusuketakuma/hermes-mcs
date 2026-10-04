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
  field whitelist — raw patient content keys are rejected at both the
  producer and the sink;
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
import time
from contextlib import contextmanager
from pathlib import Path

from export_schema import RECORD_TYPES, finite_number, validate_record
from mcs_util import atomic_write

AUTH_CONTRACT = "mcs-ext-auth/1"
EXT_CONTRACT = "mcs-ext-export/1"

# Explicit diagnostics for familiar raw-content keys. The exhaustive
# nested allowlist in export_schema is enforced after this quick check.
FORBIDDEN_KEYS = frozenset({
    "body", "body_text", "text", "statement", "evidence_quote",
    "sender", "sender_name", "patient_name", "note",
    "disease", "station_name",
})
# per-type additions — 'name' is a legitimate stat key but on an
# attachment record it is a user-authored file name
TYPE_FORBIDDEN = {"attachment": frozenset({"name"})}

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


def load_authorization(path, now: float | None = None) -> dict:
    """Load and validate an ``mcs-ext-auth/1`` authorization file.

    Refuses: unknown fields (typo-tolerance is how authorizations
    silently widen), missing required fields, non-aggregate scope,
    missing human confirmation, revocation and expiry."""
    now = time.time() if now is None else now
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as e:
        raise ContractError(f"auth_unreadable:{type(e).__name__}") from e
    return _validate_authorization(raw, now)


def _validate_authorization(raw, now: float) -> dict:
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
        bad = [f for f in fields if f not in RECORD_TYPES]
        if bad:
            raise ContractError(f"auth_field_not_exportable:{bad[0]}")
    patients = raw.get("patients", "all")
    if patients != "all" and not (
            isinstance(patients, list)
            and all(type(p) is int and p > 0 for p in patients)):
        raise ContractError("auth_patients_invalid")
    return dict(raw)


def _check_record_keys(rec: dict) -> None:
    """Nested scan — a smuggled fact dict carrying 'statement' inside a
    'facts' list must trip the same wall as a top-level key."""
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
    # Reusing an auth_id cannot change a previously dispatched intent.
    return _sha(_canonical({key: envelope.get(key) for key in (
        "auth_id", "destination", "purpose", "scope", "retention_days",
        "snapshot_generation_id", "snapshot_generated_at", "records_sha256")}))


def _check_id(envelope_id) -> str:
    if not isinstance(envelope_id, str) or not re.fullmatch(r"[0-9a-f]{24}", envelope_id):
        raise ContractError("envelope_id_invalid")
    return envelope_id


def _validate_envelope(envelope: dict) -> None:
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
            _write_json(dest, envelope)
        if not self.drop_ack:
            _write_json(self.root / "acks" / f"{eid}.json",
                        {"envelope_id": eid, "acked_at": time.time(),
                         "records": envelope["record_count"],
                         "records_sha256": envelope["records_sha256"]})
        return eid

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
                {"envelope_id": envelope_id,
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
        except (OSError, ValueError):
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
        except (OSError, ValueError, TypeError, ContractError):
            return False


class GovernedExporter:
    """Journal + audit around one authorization and one sink.

    ``state_dir`` holds ``journal/<envelope_id>.json`` and
    ``audit.jsonl``. Statuses: ``sent`` (payload left, ack outcome
    unknown — HELD), ``acked``, ``held``, ``refused``, ``withdrawn``,
    ``delete_held``.
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
        except (OSError, ValueError) as e:
            raise ContractError("journal_unreadable") from e
        if not isinstance(entry, dict) or entry.get("envelope_id") != envelope_id \
                or entry.get("status") not in (
                    "sent", "held", "acked", "refused", "withdrawn", "delete_held") \
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
            auth = load_authorization(auth_path)
            expected = build_envelope(envelope["records"], auth,
                                      envelope["snapshot_generated_at"])
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
                              record_count=envelope["record_count"])
            try:
                sink.receive(envelope)
                ack = sink.ack(eid)
            except Exception as exc:
                self._audit("deliver", "sent_ack_unknown", envelope_id=eid,
                            auth_id=auth["auth_id"], reason=type(exc).__name__)
                return {"status": "held", "reason": "ack_unknown", "envelope_id": eid}
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
        return isinstance(ack, dict) and entry is not None \
            and ack.get("envelope_id") == entry["envelope_id"] \
            and finite_number(ack.get("acked_at")) \
            and type(ack.get("records")) is int \
            and isinstance(entry.get("records_sha256"), str) \
            and ack.get("records_sha256") == entry.get("records_sha256") \
            and ack.get("records") == entry.get("record_count")

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
        if prior["status"] in ("acked", "withdrawn", "refused"):
            self._audit("reconcile", prior["status"], envelope_id=envelope_id)
            return {"status": prior["status"], "envelope_id": envelope_id}
        if prior["status"] == "delete_held":
            ack = self._delete_ack(sink, envelope_id)
            if ack is not None and not sink.has(envelope_id):
                self._set_journal(envelope_id, "withdrawn")
                self._audit("reconcile", "withdrawn", envelope_id=envelope_id)
                return {"status": "withdrawn", "envelope_id": envelope_id}
            self._audit("reconcile", "delete_held", envelope_id=envelope_id)
            return {"status": "delete_held", "envelope_id": envelope_id}
        if sink.matches(envelope_id, prior.get("records_sha256"), prior.get("intent_sha256")) \
                and self._valid_ack(sink.ack(envelope_id), prior):
            self._set_journal(envelope_id, "acked")
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

    def withdraw(self, envelope_id: str, sink: LocalSink) -> dict:
        """Propagate a deletion directive; its acknowledgement is
        journaled independently — an unacknowledged delete is held."""
        with self._locked():
            try:
                return self._withdraw(envelope_id, sink)
            except ContractError as exc:
                self._audit("withdraw", "refused", auth_id=None, reason=str(exc))
                raise

    def _withdraw(self, envelope_id, sink):
        prior = self._journal(envelope_id)
        if prior is None:
            raise ContractError("withdraw_no_delivery")
        if prior.get("sink_root") != str(sink.root.resolve()):
            raise ContractError("sink_binding_mismatch")
        if prior["status"] in ("withdrawn", "delete_held"):
            return self._reconcile(envelope_id, sink)
        self._set_journal(envelope_id, "delete_held")
        try:
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
            ack = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(ack, dict) and ack.get("envelope_id") == envelope_id \
                and finite_number(ack.get("deleted_at")):
            return ack
        return None

    def audit_entries(self) -> list:
        p = self.dir / "audit.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in
                p.read_text(encoding="utf-8").splitlines() if line]


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--auth", help="mcs-ext-auth/1 authorization file")
    ap.add_argument("--records", help="export.jsonl to wrap")
    ap.add_argument("--state", help="exporter journal/audit dir")
    ap.add_argument("--sink", help="LocalSink directory (fake consumer)")
    args = ap.parse_args(argv)
    if not all([args.auth, args.records, args.state, args.sink]):
        ap.error("--auth --records --state --sink are all required; "
                 "governed export is disabled without them")
    try:
        auth = load_authorization(args.auth)
        records = [json.loads(line) for line in
                   Path(args.records).read_text(
                       encoding="utf-8").splitlines() if line.strip()]
        gen_at = next((r.get("snapshot", {}).get("generated_at")
                       for r in records if r.get("type") == "meta"), None)
        env = build_envelope(records, auth, gen_at)
        res = GovernedExporter(args.state).deliver(
            env, LocalSink(args.sink), auth_path=args.auth)
        print(json.dumps(res, ensure_ascii=False, allow_nan=False))
        return 0 if res["status"] in ("acked", "already_acked") else 1
    except (ContractError, OSError, ValueError, TypeError, RecursionError) as e:
        reason = str(e) if isinstance(e, ContractError) else type(e).__name__
        print(json.dumps({"status": "refused", "reason": reason},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
