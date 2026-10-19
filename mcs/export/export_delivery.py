"""外部搬出のローカルjournal・送受領の証跡・撤回とbounded healthを管理する。"""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

import export_contract
from c1_contract import C1_FIELDS, C1ContractError
from c1_envelopes import (CONTRACT as C1_CONTRACT, RECEIPT_CONTRACT,
                          WITHDRAW_REASONS, encode_envelope, encode_withdrawal,
                          parse_envelope)
from stats_math import finite_number
from safe_io import fdopen_owned, atomic_write, fsync_dir, read_regular_nofollow


def _write_json(path: Path, obj) -> None:
    """Atomic write — a crash must not leave a torn journal/ack."""
    atomic_write(str(path), lambda fh: fh.write(export_contract.canonical_text(obj)), tmp_prefix=".t-")


def load_authorization(path, now: float | None = None, *, c1: bool = False) -> dict:
    """Load and validate an ``mcs-ext-auth/1`` authorization file.

    Refuses: unknown fields (typo-tolerance is how authorizations
    silently widen), missing required fields, non-aggregate scope,
    missing human confirmation, revocation and expiry."""
    now = time.time() if now is None else now
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError, RecursionError) as e:
        raise export_contract.ContractError(f"auth_unreadable:{type(e).__name__}") from e
    return export_contract.validate_authorization(raw, now, c1=c1)


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
        export_contract.validate_export_envelope(envelope)
        # a new sink root is private; an existing one is never chmodded
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        eid = envelope["envelope_id"]
        dest = self.root / "envelopes" / f"{eid}.json"
        if dest.exists():
            stored = json.loads(dest.read_text(encoding="utf-8"))
            export_contract.validate_export_envelope(stored)
            if export_contract.export_intent_hash(stored) != export_contract.export_intent_hash(envelope):
                raise export_contract.ContractError("sink_identity_conflict")
        if not dest.exists():                      # idempotent store
            if envelope["contract"] == C1_CONTRACT:
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
        try:
            envelope = parse_envelope(raw)
        except C1ContractError as e:
            raise export_contract.ContractError(e.code) from e
        return self.receive(envelope)

    def delete(self, envelope_id: str) -> None:
        export_contract.validate_envelope_id(envelope_id)
        path = self.root / "envelopes" / f"{envelope_id}.json"
        path.unlink(missing_ok=True)
        if path.parent.exists():
            fsync_dir(path.parent, require_dir=False)
        if not self.drop_delete_ack:
            _write_json(
                self.root / "deletions" / f"{envelope_id}.json",
                {"contract": RECEIPT_CONTRACT, "kind": "delete",
                 "deleted": True, "envelope_id": envelope_id,
                 "deleted_at": time.time()})

    def has(self, envelope_id: str) -> bool:
        export_contract.validate_envelope_id(envelope_id)
        return (self.root / "envelopes" / f"{envelope_id}.json").exists()

    def ack(self, envelope_id: str):
        export_contract.validate_envelope_id(envelope_id)
        p = self.root / "acks" / f"{envelope_id}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return None

    def matches(self, envelope_id: str, records_sha256: str, intent_sha256: str) -> bool:
        export_contract.validate_envelope_id(envelope_id)
        try:
            stored = json.loads((self.root / "envelopes" / f"{envelope_id}.json")
                                .read_text(encoding="utf-8"))
            export_contract.validate_export_envelope(stored)
            return stored["envelope_id"] == envelope_id \
                and stored["records_sha256"] == records_sha256 \
                and export_contract.export_intent_hash(stored) == intent_sha256
        except (OSError, ValueError, TypeError, export_contract.ContractError, RecursionError):
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
        # Stop handing over the copy first, even if the directive cannot be encoded.
        self.delete(envelope_id)
        try:
            raw = encode_withdrawal(envelope_id, auth_id, reason).decode("utf-8")
        except C1ContractError as e:
            raise export_contract.ContractError(e.code) from e
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
        # mkdir's mode applies only to the leaf: create the state dir itself
        # private first, or the next C1 run refuses it as shared
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.dir / "journal").mkdir(mode=0o700, exist_ok=True)

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
        with fdopen_owned(fd, "w", encoding="utf-8") as fh:
            fh.write(export_contract.canonical_text(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _journal(self, envelope_id: str) -> dict | None:
        export_contract.validate_envelope_id(envelope_id)
        p = self.dir / "journal" / f"{envelope_id}.json"
        if not p.exists():
            return None
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as e:
            raise export_contract.ContractError("journal_unreadable") from e
        if not isinstance(entry, dict) or entry.get("envelope_id") != envelope_id \
                or entry.get("status") not in (
                    "sent", "held", "acked", "refused", "withdrawn", "delete_held",
                    "rejected") \
                or not isinstance(entry.get("attempts"), list):
            raise export_contract.ContractError("journal_invalid")
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
            envelope = json.loads(export_contract.canonical_text(envelope))
            export_contract.validate_export_envelope(envelope)
            eid = envelope["envelope_id"]
            if auth_path is None:
                raise export_contract.ContractError("auth_required")
            c1 = envelope["contract"] == C1_CONTRACT
            now = time.time()
            auth = load_authorization(auth_path, now, c1=c1)
            if c1:
                if any(envelope[key] != auth[key] for key in (
                        "auth_id", "destination", "purpose", "scope", "retention_days")):
                    raise export_contract.ContractError("authorization_envelope_mismatch")
                if envelope["created_at"] > now \
                        or envelope["snapshot_generated_at"] > now:
                    raise export_contract.ContractError("snapshot_time_invalid")
                if now - envelope["snapshot_generated_at"] > auth["max_snapshot_age_s"]:
                    raise export_contract.ContractError("snapshot_stale")
            else:
                expected = export_contract.build_envelope(envelope["records"], auth,
                                          envelope["snapshot_generated_at"], now=now)
                if any(envelope[key] != expected[key] for key in expected if key != "created_at"):
                    raise export_contract.ContractError("authorization_envelope_mismatch")
            prior = self._journal(eid)
            sink_root = str(sink.root.resolve())
            if prior and prior["status"] != "refused" and prior.get("sink_root") != sink_root:
                raise export_contract.ContractError("sink_binding_mismatch")
            if prior and prior["status"] != "refused" \
                    and prior.get("intent_sha256") != export_contract.export_intent_hash(envelope):
                raise export_contract.ContractError("envelope_identity_conflict")
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
                              intent_sha256=export_contract.export_intent_hash(envelope),
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
                    or not sink.matches(eid, envelope["records_sha256"], export_contract.export_intent_hash(envelope)):
                self._audit("deliver", "sent_ack_unknown",
                            envelope_id=eid, auth_id=auth["auth_id"])
                return {"status": "held",
                        "reason": "ack_unknown", "envelope_id": eid}
            self._set_journal(eid, "acked", acked_at=ack.get("acked_at"))
            self._audit("deliver", "acked", envelope_id=eid, auth_id=auth["auth_id"])
            return {"status": "acked", "envelope_id": eid}
        except (export_contract.ContractError, ValueError, TypeError, RecursionError) as e:
            # A later authorization refusal must not erase an earlier send
            # or withdrawal; that would turn an unknown outcome into a retry.
            if eid is not None and not (self.dir / "journal" / f"{eid}.json").exists():
                self._set_journal(eid, "refused")
            reason = str(e) if isinstance(e, export_contract.ContractError) else "envelope_invalid"
            self._audit("deliver", "refused", envelope_id=eid,
                        auth_id=auth.get("auth_id") if auth else None, reason=reason)
            return {"status": "refused", "reason": reason,
                    "envelope_id": eid}

    @staticmethod
    def _valid_ack(ack, entry):
        try:
            ack = export_contract.parse_receipt(ack)
        except export_contract.ContractError:
            return False
        if entry is not None and entry.get("export_contract") == C1_CONTRACT \
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
            receipt = export_contract.parse_receipt(receipt)
        except export_contract.ContractError:
            return False
        return entry is not None and receipt.get("kind") == "receive" \
            and receipt.get("status") == "rejected" \
            and receipt["envelope_id"] == entry["envelope_id"] \
            and ("records_sha256" not in receipt
                 or receipt["records_sha256"] == entry.get("records_sha256"))

    def import_receipts(self, path, sink: LocalSink):
        """Reconcile manually supplied JSON/NDJSON receipts; never send."""
        source = Path(path)
        files = sorted(p for p in source.iterdir()
                       if p.is_file() and not p.is_symlink() and not p.name.startswith(".")) \
            if source.is_dir() else [source]
        receipts = []
        for file in files:
            raw = file.read_bytes()
            try:
                receipts.append(export_contract.parse_receipt(raw))
            except export_contract.ContractError:
                # NDJSON is only a bundle, each line keeps the /1 contract.
                lines = [line for line in raw.splitlines() if line.strip()]
                if not lines:
                    raise export_contract.ContractError("receipt_not_object") from None
                receipts.extend(export_contract.parse_receipt(line) for line in lines)
        results = []
        sink_root = str(sink.root.resolve())
        with self._locked():
            # Bind every receipt before applying any: a bundle is all or nothing.
            for receipt in receipts:
                prior = self._journal(receipt["envelope_id"])
                if prior is None or prior.get("sink_root") != sink_root:
                    raise export_contract.ContractError("receipt_journal_mismatch")
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
            except export_contract.ContractError as exc:
                self._audit("reconcile", "refused", auth_id=None, reason=str(exc))
                raise

    def _reconcile(self, envelope_id, sink):
        prior = self._journal(envelope_id)
        if prior is None:
            self._audit("reconcile", "no_journal", envelope_id=envelope_id)
            return {"status": "no_journal", "envelope_id": envelope_id}
        if prior["status"] == "refused":
            # Never sent, so the journal has no sink binding to check.
            self._audit("reconcile", "refused", envelope_id=envelope_id)
            return {"status": "refused", "envelope_id": envelope_id}
        if prior.get("sink_root") != str(sink.root.resolve()):
            raise export_contract.ContractError("sink_binding_mismatch")
        if prior["status"] in ("acked", "withdrawn", "rejected"):
            if isinstance(sink, HandoffSink):
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
            except export_contract.ContractError as exc:
                self._audit("withdraw", "refused", auth_id=None, reason=str(exc))
                raise

    def withdraw_generation(self, generation_id: str, sink: LocalSink,
                            reason: str = "operator_request") -> list:
        """Expand one generation from the durable journal, never the outbox."""
        with self._locked():
            try:
                targets = []
                for path in sorted(journal_paths(self.dir)):
                    if re.fullmatch(r"[0-9a-f]{24}\.json", path.name) is None:
                        continue
                    entry = self._journal(path.stem)
                    if entry.get("snapshot_generation_id") == generation_id \
                            and entry["status"] != "refused":
                        if entry.get("sink_root") != str(sink.root.resolve()):
                            raise export_contract.ContractError("sink_binding_mismatch")
                        targets.append(entry["envelope_id"])
                if not targets:
                    raise export_contract.ContractError("withdraw_generation_unknown")
                return [self._withdraw(eid, sink, reason) for eid in targets]
            except export_contract.ContractError as exc:
                self._audit("withdraw", "refused", auth_id=None, reason=str(exc))
                raise

    def _withdraw(self, envelope_id, sink, reason="operator_request"):
        if reason not in WITHDRAW_REASONS:
            raise export_contract.ContractError("withdraw_reason_invalid")
        prior = self._journal(envelope_id)
        if prior is None:
            raise export_contract.ContractError("withdraw_no_delivery")
        if prior.get("sink_root") != str(sink.root.resolve()):
            raise export_contract.ContractError("sink_binding_mismatch")
        if prior["status"] in ("withdrawn", "delete_held"):
            return self._reconcile(envelope_id, sink)
        if isinstance(sink, HandoffSink) and not prior.get("auth_id"):
            raise export_contract.ContractError("withdraw_auth_unknown")
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
        export_contract.validate_envelope_id(envelope_id)
        p = sink.root / "deletions" / f"{envelope_id}.json"
        if not p.exists():
            return None
        try:
            ack = export_contract.parse_receipt(p.read_bytes())
        except (OSError, export_contract.ContractError):
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
    raw = read_regular_nofollow(
        path, export_contract.RECEIPT_MAX_BYTES, lambda: export_contract.ContractError("health_file_size_or_type"))
    if len(raw) > export_contract.RECEIPT_MAX_BYTES:
        raise export_contract.ContractError("health_file_size_or_type")
    return json.loads(raw)


def journal_paths(state, max_entries=None):
    """Scan the local journal; only health bounds it (invalid names count too).

    Withdraw and reconcile stay uncapped: the journal is never pruned, so a
    cap would block deletion propagation once enough parts were handed off.
    """
    directory = Path(state) / "journal"
    if directory.is_symlink():
        raise export_contract.ContractError("journal_directory_invalid")
    with os.scandir(directory) as entries:
        for count, entry in enumerate(entries, 1):
            if max_entries is not None and count > max_entries:
                raise export_contract.ContractError("journal_entry_limit")
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
        raise export_contract.ContractError("health_entry_limit_invalid")
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
        for path in journal_paths(state, max_entries):
            try:
                eid = export_contract.validate_envelope_id(path.stem)
                if path.suffix != ".json":
                    raise export_contract.ContractError("journal_invalid")
                entry = _read_health_json(path)
                if (not isinstance(entry, dict) or entry.get("envelope_id") != eid
                        or entry.get("status") not in (
                            "sent", "held", "delete_held", "acked", "withdrawn",
                            "refused", "rejected")
                        or not isinstance(entry.get("attempts"), list)):
                    raise export_contract.ContractError("journal_invalid")
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
            except (export_contract.ContractError, OSError, ValueError, TypeError, RecursionError):
                counts["corrupt"] += 1
                reasons.add("journal_invalid_or_unreadable")
                report["complete"] = False
    except export_contract.ContractError as exc:
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
            fields = raw.get("fields") if isinstance(raw, dict) else None
            export_contract.validate_authorization(raw, now, c1=isinstance(fields, list)
                                    and set(fields) == set(C1_FIELDS))
            report["authorization"] = {
                "state": "valid", "seconds_left": raw["expires_at"] - now}
        except export_contract.ContractError as exc:
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
