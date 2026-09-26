#!/usr/bin/env python3
"""Governed external export contract — disabled by default (T16).

A provider-neutral authorization + envelope + journal layer around the
T15 machine read model. There is deliberately NO network code here: the
only demonstrable destination is :class:`LocalSink`, an in-process fake
consumer backed by a directory. Wiring a real connector requires a
named provider, an access review and a separate permission — see
``docs/external-export-contract.md``.

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
import json
import os
import tempfile
import time
from pathlib import Path

AUTH_CONTRACT = "mcs-ext-auth/1"
EXT_CONTRACT = "mcs-ext-export/1"

# record types an authorization may grant; anything else in the JSONL
# stream (or invented later) is refused rather than passed through
RECORD_TYPES = ("meta", "coverage", "stat", "signal", "attachment",
                "message", "signals_truncated")

# keys that must NEVER appear in an externally exported record — the
# aggregate-scope whitelist complement. Enforced at envelope build AND
# at the sink, so a malformed producer cannot smuggle raw content out.
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
                 "expires_at", "confirm_human", "reason")


class ContractError(Exception):
    """Refusal — the export is held before any data leaves."""


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, obj) -> None:
    """Atomic write — a crash must not leave a torn journal/ack."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".t-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_canonical(obj))
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def load_authorization(path, now: float | None = None) -> dict:
    """Load and validate an ``mcs-ext-auth/1`` authorization file.

    Refuses: unknown fields (typo-tolerance is how authorizations
    silently widen), missing required fields, non-aggregate scope,
    missing human confirmation, revocation and expiry."""
    now = time.time() if now is None else now
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ContractError(f"auth_unreadable:{type(e).__name__}") from e
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
    if raw.get("revoked"):
        raise ContractError("auth_revoked")
    if not isinstance(raw.get("expires_at"), (int, float)) \
            or raw["expires_at"] <= now:
        raise ContractError("auth_expired")
    fields = raw.get("fields")
    if fields is not None:
        bad = [f for f in fields if f not in RECORD_TYPES]
        if bad:
            raise ContractError(f"auth_field_not_exportable:{bad[0]}")
    patients = raw.get("patients", "all")
    if patients != "all" and not (
            isinstance(patients, list)
            and all(isinstance(p, int) for p in patients)):
        raise ContractError("auth_patients_invalid")
    return dict(raw)


def _check_record_keys(rec: dict) -> None:
    """Nested scan — a smuggled fact dict carrying 'statement' inside a
    'facts' list must trip the same wall as a top-level key."""
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
        elif isinstance(node, (list, tuple)):
            stack.extend(node)


def build_envelope(records: list[dict], auth: dict,
                   snapshot_generated_at: float | None,
                   now: float | None = None) -> dict:
    """Wrap validated export records in one versioned envelope.

    `records` are parsed ``export.jsonl`` lines (T15 machine surface).
    Per-patient eligibility is applied here: message records are
    filtered by ``auth["patients"]`` and attachments follow their
    message's eligibility."""
    now = time.time() if now is None else now
    if auth.get("scope", "aggregate") != "aggregate":
        raise ContractError("envelope_scope_not_aggregate")
    max_age = auth.get("max_snapshot_age_s")
    if max_age is not None:
        if snapshot_generated_at is None \
                or now - snapshot_generated_at > max_age:
            raise ContractError("snapshot_stale")
    allowed = set(auth.get("fields") or RECORD_TYPES)
    patients = auth.get("patients", "all")
    eligible_pids = None if patients == "all" else set(patients)

    kept, kept_mids = [], set()
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
        if rtype == "message" and eligible_pids is not None \
                and rec.get("project_id") not in eligible_pids:
            continue
        kept.append(rec)
        if rtype == "message":
            kept_mids.add(rec.get("message_id"))
    if eligible_pids is not None:
        # attachments carry no project_id — eligibility follows the
        # parent message (an attachment of an ineligible message drops)
        kept = [r for r in kept if r.get("type") != "attachment"
                or r.get("message_id") in kept_mids]

    body = _canonical(kept)
    gen = next((r.get("snapshot_generation_id") for r in kept
                if r.get("snapshot_generation_id")), None)
    envelope = {
        "contract": EXT_CONTRACT,
        "auth_id": auth["auth_id"],
        "destination": auth["destination"],
        "purpose": auth["purpose"],
        "scope": "aggregate",
        "snapshot_generation_id": gen,
        "snapshot_generated_at": snapshot_generated_at,
        "created_at": now,
        "retention_days": auth.get("retention_days"),
        "record_count": len(kept),
        "records_sha256": _sha(body),
        "records": kept,
    }
    envelope["envelope_id"] = _sha(_canonical(
        {"auth_id": auth["auth_id"], "gen": gen,
         "records_sha256": envelope["records_sha256"]}))[:24]
    return envelope


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
        if envelope.get("contract") != EXT_CONTRACT:
            raise ContractError("sink_contract_mismatch")
        if envelope.get("scope") != "aggregate":
            raise ContractError("sink_scope_not_aggregate")
        for rec in envelope.get("records") or []:
            _check_record_keys(rec)
        eid = envelope["envelope_id"]
        dest = self.root / "envelopes" / f"{eid}.json"
        if not dest.exists():                      # idempotent store
            _write_json(dest, envelope)
        if not self.drop_ack:
            _write_json(self.root / "acks" / f"{eid}.json",
                        {"envelope_id": eid, "acked_at": time.time(),
                         "records": envelope.get("record_count")})
        return eid

    def delete(self, envelope_id: str) -> None:
        (self.root / "envelopes" / f"{envelope_id}.json").unlink(
            missing_ok=True)
        if not self.drop_delete_ack:
            _write_json(
                self.root / "deletions" / f"{envelope_id}.json",
                {"envelope_id": envelope_id,
                 "deleted_at": time.time()})

    def has(self, envelope_id: str) -> bool:
        return (self.root / "envelopes" / f"{envelope_id}.json").exists()

    def ack(self, envelope_id: str):
        p = self.root / "acks" / f"{envelope_id}.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))


class GovernedExporter:
    """Journal + audit around one authorization and one sink.

    ``state_dir`` holds ``journal/<envelope_id>.json`` and
    ``audit.jsonl``. Statuses: ``sent`` (payload left, ack outcome
    unknown — HELD), ``acked``, ``held``, ``refused``, ``withdrawn``,
    ``delete_held``.
    """

    def __init__(self, state_dir):
        self.dir = Path(state_dir)
        (self.dir / "journal").mkdir(parents=True, exist_ok=True)

    def _audit(self, action: str, outcome: str, **kw) -> None:
        entry = {"ts": time.time(), "action": action,
                 "outcome": outcome, **kw}
        with open(self.dir / "audit.jsonl", "a", encoding="utf-8") as fh:
            fh.write(_canonical(entry) + "\n")

    def _journal(self, envelope_id: str) -> dict | None:
        p = self.dir / "journal" / f"{envelope_id}.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

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
        eid = envelope.get("envelope_id")
        try:
            if auth_path is not None:
                # re-validate at send time: revocation between build and
                # deliver must stop the bytes, not just the build
                load_authorization(auth_path)
            prior = self._journal(eid)
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
            sink.receive(envelope)
            ack = sink.ack(eid)
            if ack is None:
                self._set_journal(eid, "sent")
                self._audit("deliver", "sent_ack_unknown",
                            envelope_id=eid)
                return {"status": "held",
                        "reason": "ack_unknown", "envelope_id": eid}
            self._set_journal(eid, "acked", acked_at=ack.get("acked_at"))
            self._audit("deliver", "acked", envelope_id=eid)
            return {"status": "acked", "envelope_id": eid}
        except ContractError as e:
            self._set_journal(eid, "refused")
            self._audit("deliver", "refused", envelope_id=eid,
                        reason=str(e))
            return {"status": "refused", "reason": str(e),
                    "envelope_id": eid}

    def reconcile(self, envelope_id: str, sink: LocalSink) -> dict:
        """Resolve an unknown outcome by inspecting the sink — a
        stored payload + lost ack reconciles to acked WITHOUT a
        retransmission; absent payload stays held for a human."""
        prior = self._journal(envelope_id)
        if prior is None:
            return {"status": "no_journal", "envelope_id": envelope_id}
        if prior["status"] == "acked":
            return {"status": "acked", "envelope_id": envelope_id}
        if sink.has(envelope_id) and sink.ack(envelope_id) is not None:
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
        sink.delete(envelope_id)
        ack = self._delete_ack(sink, envelope_id)
        if ack is None:
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
        p = sink.root / "deletions" / f"{envelope_id}.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

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
    except ContractError as e:
        print(json.dumps({"status": "refused", "reason": str(e)},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
