"""Persist a bounded synthetic /2 reference lifecycle in private local files.

One source namespace has one atomic state file: transport metadata, pending
records, first-complete evidence and item revision hashes commit together.
This fake keeps parsed synthetic records privately, not original wire bytes;
it is not encrypted production staging or a counterpart acceptance proof.
Tombstones/receipt metadata are not evicted to make capacity for new sends.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import TypedDict

from c1_contract import C1ContractError, C1_FIELDS, canonical_json
from c1_envelopes import (
    CONTRACT, Record, encode_envelope, intent_hash, parse_envelope, parse_withdrawal,
    validate_collection, validate_envelope,
)
from mcs_util import atomic_write

_ENTRY_FIELDS = {
    "status", "header", "records", "received_at", "expires_at", "wire_sha256",
    "intent_sha256", "counts", "items", "journal"}
_RECEIPT = "mcs-ext-receipt/1"


class Part(TypedDict):
    index: int
    count: int
    set: str


class _PartHeader(TypedDict, total=False):
    part: Part


class Header(_PartHeader):
    contract: str
    auth_id: str
    destination: str
    purpose: str
    scope: str
    snapshot_generation_id: str
    snapshot_generated_at: int | float
    created_at: int | float
    retention_days: int
    record_count: int
    records_sha256: str
    envelope_id: str


class Envelope(Header):
    records: list[Record]


class Extraction(TypedDict, total=False):
    state: str | None


class _MessageValues(TypedDict, total=False):
    content_hash: str | None
    posted_at_ts: int | float | None
    facts: list[Record]
    relations: list[Record]
    extraction: dict[str, Extraction | None]
    state: str


class Message(_MessageValues):
    type: str
    contract: str
    snapshot_generation_id: str
    project_id: int
    message_id: int
    body_state: str


class Body(TypedDict):
    type: str
    contract: str
    snapshot_generation_id: str
    project_id: int
    message_id: int
    body_text: str
    body_sha256: str
    body_format: str
    body_truncated: bool
    sender_kind: str


class Coverage(TypedDict):
    project_id: int
    fetch_state: str
    history_floor: int | None
    coverage_ts: int | float | None


class ItemMeta(TypedDict):
    item_id: str
    revision_id: str
    payload_sha256: str


class Event(TypedDict):
    status: str
    at: int | float


class Entry(TypedDict):
    status: str
    header: Header | None
    records: list[Record] | None
    received_at: int | float | None
    expires_at: int | float | None
    wire_sha256: str | None
    intent_sha256: str | None
    counts: dict[str, int]
    items: list[ItemMeta]
    journal: list[Event]


class Evidence(TypedDict):
    contract: str
    auth_id: str
    snapshot_generation_id: str
    count: int
    set: str


class Selection(TypedDict):
    evidence: Evidence
    members: list[str]
    snapshot_generated_at: int | float
    selected_at: int | float


class State(TypedDict):
    version: int
    namespace: str
    envelopes: dict[str, Entry]
    selected: dict[str, Selection]


class CollectionView(TypedDict):
    auth_id: str
    snapshot_generation_id: str
    set: str
    expected_parts: int
    received_parts: int
    active_parts: int
    evidence: Evidence | None
    status: str


class Revision(ItemMeta):
    snapshot_generation_id: str
    snapshot_generated_at: int | float
    links: list[str]
    available: bool
    superseded_by: str | None


class Bundle(TypedDict):
    message: Message
    body: Body | None


class ItemView(TypedDict):
    item_id: str
    source_label: str
    project_id: int
    message_id: int
    state: str
    reason: str
    current: Bundle | None
    last_known: Bundle | None
    revisions: list[Revision]


class SignalView(TypedDict):
    signal_id: str
    count: int | None
    state: str
    records: list[Record]
    last_known: list[Record]


class ReceiverView(TypedDict):
    source_label: str
    collection_state: str
    current_generation: str | None
    latest_complete_generation: str | None
    evidence: Evidence | None
    items: list[ItemView]
    signals: list[SignalView]


class Diagnostics(TypedDict):
    status: str
    reasons: list[str]
    counts: dict[str, int]


def _hash(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _now(value) -> int | float:
    value = time.time() if value is None else value
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        raise C1ContractError("receiver_time_invalid")
    canonical_json(value)
    return value


def _id(value) -> str:
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{24}", value) is None:
        raise C1ContractError("envelope_id_invalid")
    return value


def _normalize(value):
    if isinstance(value, list):
        return sorted((_normalize(item) for item in value), key=canonical_json)
    if isinstance(value, dict):
        return {key: _normalize(child) for key, child in value.items()}
    return value


def _payload_hash(message: Message, body: Body | None) -> str:
    payload = {key: message.get(key) for key in (
        "content_hash", "facts", "relations", "state", "body_state", "posted_at_ts")}
    payload.update({key: body.get(key) if body is not None else None for key in (
        "body_sha256", "body_format", "body_truncated", "sender_kind")})
    payload["extraction"] = {
        kind: row.get("state") if row is not None else None
        for kind, row in (message.get("extraction") or {}).items()}
    return _hash(_normalize(payload))


def _cohort(header: Header) -> str:
    return _hash([header["auth_id"], header["snapshot_generation_id"]])


def _set_id(header: Header) -> str:
    return header["part"]["set"] if "part" in header else _hash([header["records_sha256"]])


def _active(entry: Entry, now: int | float) -> bool:
    expires = entry["expires_at"]
    return entry["status"] == "received" and expires is not None and expires > now


def _envelope(entry: Entry) -> Record:
    assert entry["header"] is not None
    # A JSON-owned boundary value, independent of the persisted typed capsule.
    return json.loads(json.dumps({**entry["header"], "records": entry["records"]}))


def _bundles(records: list[Record]) -> list[tuple[Message, Body | None]]:
    # Records have passed the shared validator before this typed projection.
    bodies = {}
    messages = []
    for record in records:
        if record["type"] == "message":
            message: Message = json.loads(canonical_json(record))
            messages.append(message)
        elif record["type"] == "message_body":
            body: Body = json.loads(canonical_json(record))
            bodies[(record["project_id"], record["message_id"])] = body
    return [(m, bodies.get((m["project_id"], m["message_id"]))) for m in messages]


class ReferenceReceiver:
    """A local fake receiver; source_label is stable across authorization rotation.

    root must be a dedicated private directory. Limits include expired and
    withdrawn metadata; exhaustion refuses new input instead of losing replay
    protection. Use one instance/root per tenant; source labels separate feeds.
    """

    def __init__(self, root, *, source_label: str, max_envelopes: int = 256,
                 max_state_bytes: int = 16_777_216):
        if (not isinstance(source_label, str) or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", source_label) is None):
            raise C1ContractError("receiver_source_invalid")
        if (type(max_envelopes) is not int or not 1 <= max_envelopes <= 1024
                or type(max_state_bytes) is not int
                or not 1024 <= max_state_bytes <= 67_108_864):
            raise C1ContractError("receiver_limits_invalid")
        self.source_label = source_label
        self.namespace = _hash(source_label)
        self.max_envelopes = max_envelopes
        self.max_state_bytes = max_state_bytes
        base = Path(root).absolute()
        if base.is_symlink():
            raise C1ContractError("receiver_directory_unsafe")
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.base = base.resolve()
        self.root = self.base / ("source-" + self.namespace)
        self.root.mkdir(mode=0o700, exist_ok=True)
        self._directories()

    def _directories(self) -> None:
        for path in (self.base, self.root):
            mode = path.lstat().st_mode
            if not stat.S_ISDIR(mode) or mode & 0o077:
                raise C1ContractError("receiver_directory_unsafe")

    @contextmanager
    def _locked(self):
        self._directories()
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
                     | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
                raise C1ContractError("receiver_lock_unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self) -> State:
        path = self.root / "state.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {"version": 1, "namespace": self.namespace, "envelopes": {}, "selected": {}}
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or info.st_nlink != 1 or info.st_size > self.max_state_bytes):
                raise C1ContractError("receiver_state_unsafe")
            raw = stream.read(self.max_state_bytes + 1)
        if len(raw) > self.max_state_bytes:
            raise C1ContractError("receiver_capacity_reached")
        try:
            state: State = json.loads(raw)
            self._validate_state(state)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise C1ContractError("receiver_state_invalid") from None
        return state

    def _validate_state(self, state: State) -> None:
        if (not isinstance(state, dict) or set(state) != {
                "version", "namespace", "envelopes", "selected"}
                or type(state["version"]) is not int or state["version"] != 1
                or state["namespace"] != self.namespace
                or not isinstance(state["envelopes"], dict)
                or not isinstance(state["selected"], dict)
                or len(state["envelopes"]) > self.max_envelopes
                or len(state["selected"]) > self.max_envelopes):
            raise C1ContractError("receiver_state_invalid")
        for eid, entry in state["envelopes"].items():
            _id(eid)
            if (not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS
                    or entry["status"] not in ("received", "withdrawn", "expired")
                    or not isinstance(entry["journal"], list)
                    or not 1 <= len(entry["journal"]) <= 3):
                raise C1ContractError("receiver_state_invalid")
            for event in entry["journal"]:
                if set(event) != {"status", "at"} or event["status"] not in (
                        "received", "withdrawn", "expired") or event["at"] is None:
                    raise C1ContractError("receiver_state_invalid")
                _now(event["at"])
            if entry["journal"][-1]["status"] != entry["status"]:
                raise C1ContractError("receiver_state_invalid")
            if entry["header"] is None:
                if (entry["status"] != "withdrawn" or entry["records"] is not None
                        or entry["received_at"] is not None or entry["expires_at"] is not None
                        or entry["counts"] or entry["items"]):
                    raise C1ContractError("receiver_state_invalid")
                continue
            header = entry["header"]
            assert header is not None
            if (header["envelope_id"] != eid or header["contract"] != CONTRACT
                    or entry["received_at"] is None
                    or entry["expires_at"] != _now(entry["received_at"])
                    + header["retention_days"] * 86400
                    or set(entry["counts"]) - set(C1_FIELDS)
                    or any(type(n) is not int or n < 0 for n in entry["counts"].values())
                    or sum(entry["counts"].values()) != header["record_count"]
                    or not isinstance(entry["items"], list)
                    or any(not isinstance(value, str)
                           or re.fullmatch("[0-9a-f]{64}", value) is None
                           for value in (entry["wire_sha256"], entry["intent_sha256"]))):
                raise C1ContractError("receiver_state_invalid")
            for item in entry["items"]:
                if set(item) != {"item_id", "revision_id", "payload_sha256"} or any(
                        not isinstance(v, str) or re.fullmatch("[0-9a-f]{64}", v) is None
                        for v in item.values()):
                    raise C1ContractError("receiver_state_invalid")
            if entry["status"] == "received":
                envelope = _envelope(entry)
                validate_envelope(envelope)
                if (intent_hash(envelope) != entry["intent_sha256"]
                        or self._items(json.loads(canonical_json(envelope))) != entry["items"]
                        or Counter(r["type"] for r in (entry["records"] or [])) != entry["counts"]):
                    raise C1ContractError("receiver_state_invalid")
            elif entry["records"] is not None:
                raise C1ContractError("receiver_state_invalid")
        for key, selected in state["selected"].items():
            if set(selected) != {"evidence", "members", "snapshot_generated_at", "selected_at"}:
                raise C1ContractError("receiver_state_invalid")
            evidence = selected["evidence"]
            validate_collection([], current_set=json.loads(json.dumps(evidence)))
            members = selected["members"]
            if (not isinstance(members, list) or len(members) != evidence["count"]
                    or len(set(members)) != len(members)
                    or selected["selected_at"] is None
                    or selected["snapshot_generated_at"] is None
                    or key != _hash([evidence["auth_id"], evidence["snapshot_generation_id"]])):
                raise C1ContractError("receiver_state_invalid")
            _now(selected["selected_at"])
            _now(selected["snapshot_generated_at"])
            for index, eid in enumerate(members, 1):
                header = state["envelopes"][eid]["header"]
                if (header is None or header["auth_id"] != evidence["auth_id"]
                        or header["snapshot_generation_id"] != evidence["snapshot_generation_id"]
                        or header["snapshot_generated_at"] != selected["snapshot_generated_at"]
                        or header.get("part", {}).get("index", 1) != index
                        or _set_id(header) != evidence["set"]):
                    raise C1ContractError("receiver_state_invalid")
            headers = [state["envelopes"][eid]["header"] for eid in members]
            if _hash([h["records_sha256"] for h in headers if h is not None]) != evidence["set"]:
                raise C1ContractError("receiver_state_invalid")
        groups = {}
        for entry in state["envelopes"].values():
            header = entry["header"]
            if header is not None and entry["status"] == "received":
                groups.setdefault((_cohort(header), _set_id(header)), []).append(_envelope(entry))
        for (cohort, _), envelopes in groups.items():
            result = validate_collection(envelopes)
            if result["status"] == "complete":
                chosen = state["selected"].get(cohort)
                if chosen is None or chosen["evidence"] != result["evidence"]:
                    raise C1ContractError("receiver_state_invalid")

    def _save(self, state: State) -> None:
        text = json.dumps(state, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
        if len(text.encode("utf-8")) > self.max_state_bytes:
            raise C1ContractError("receiver_capacity_reached")
        atomic_write(str(self.root / "state.json"), lambda stream: stream.write(text),
                     mode=0o600, tmp_prefix=".c1-state-")

    def _items(self, envelope: Envelope) -> list[ItemMeta]:
        items: list[ItemMeta] = []
        for message, body in _bundles(envelope["records"]):
            item_id = _hash([self.source_label, message["project_id"], message["message_id"]])
            payload = _payload_hash(message, body)
            items.append({
                "item_id": item_id, "payload_sha256": payload,
                "revision_id": _hash([item_id, envelope["snapshot_generation_id"],
                                      envelope["snapshot_generated_at"], payload])})
        return items

    def withdraw_wire(self, raw: bytes, *, now=None) -> Record:
        """Apply one bounded mcs-ext-withdraw/1 directive; return its delete receipt."""
        directive = parse_withdrawal(raw)
        return self.withdraw(directive["envelope_id"], auth_id=directive["auth_id"], now=now)

    def receive(self, envelope: Record, *, now=None) -> str:
        """Accept a validated object through the same bounded wire entry point."""
        return self.receive_wire(encode_envelope(envelope), now=now)

    def receive_wire(self, raw: bytes, *, now=None) -> str:
        """Persist transport acceptance atomically; acceptance alone is not current."""
        wire_envelope = parse_envelope(raw)
        envelope: Envelope = json.loads(canonical_json(wire_envelope))
        intent = intent_hash(wire_envelope)
        now = _now(now)
        eid = envelope["envelope_id"]
        with self._locked():
            state = self._load()
            entries = state["envelopes"]
            if eid in entries:
                prior = entries[eid]
                if prior["status"] == "withdrawn":
                    raise C1ContractError("receiver_envelope_withdrawn")
                if not _active(prior, now):
                    raise C1ContractError("receiver_envelope_expired")
                if prior["intent_sha256"] != intent:
                    raise C1ContractError("receiver_identity_conflict")
                return eid  # No receipt clock/retention extension or duplicate revision.
            if len(entries) >= self.max_envelopes:
                raise C1ContractError("receiver_capacity_reached")
            cohort = _cohort(envelope)
            selected = state["selected"].get(cohort)
            same_set = [e for e in entries.values() if e["header"] is not None
                        and _cohort(e["header"]) == cohort
                        and _set_id(e["header"]) == _set_id(envelope)]
            if any(not _active(e, now) for e in same_set):
                raise C1ContractError("receiver_collection_unavailable")
            candidates = [_envelope(e) for e in same_set] + [wire_envelope]
            result = validate_collection(
                candidates, current_set=json.loads(json.dumps(selected["evidence"])) if selected else None)
            header: Header = json.loads(json.dumps({k: v for k, v in envelope.items() if k != "records"}))
            counts: dict[str, int] = {}
            for record in envelope["records"]:
                kind = record["type"]
                assert isinstance(kind, str)
                counts[kind] = counts.get(kind, 0) + 1
            entry: Entry = {
                "status": "received", "header": header,
                "records": envelope["records"], "received_at": now,
                "expires_at": now + envelope["retention_days"] * 86400,
                "wire_sha256": hashlib.sha256(raw).hexdigest(),
                "intent_sha256": intent,
                "counts": counts,
                "items": self._items(envelope), "journal": [{"status": "received", "at": now}]}
            entries[eid] = entry
            if result["status"] == "complete" and selected is None:
                headers = []
                for staged in same_set + [entry]:
                    member = staged["header"]
                    assert member is not None
                    headers.append(member)
                new_items = {i["item_id"]: i["payload_sha256"]
                             for staged in same_set + [entry] for i in staged["items"]}
                for known in state["selected"].values():
                    if known["evidence"]["snapshot_generation_id"] != envelope["snapshot_generation_id"]:
                        continue
                    previous = {i["item_id"]: i["payload_sha256"]
                                for old_id in known["members"] for i in entries[old_id]["items"]}
                    if (known["snapshot_generated_at"] != envelope["snapshot_generated_at"]
                            or any(previous[key] != new_items[key] for key in previous.keys() & new_items.keys())):
                        raise C1ContractError("receiver_generation_conflict")
                state["selected"][cohort] = {
                    "evidence": json.loads(json.dumps(result["evidence"])),
                    "members": [h["envelope_id"] for h in sorted(
                        headers, key=lambda h: h["part"]["index"] if "part" in h else 1)],
                    "snapshot_generated_at": envelope["snapshot_generated_at"], "selected_at": now}
            self._save(state)
        return eid

    def ack(self, envelope_id: str) -> Record | None:
        """Return historical transport receipt, not collection/item completeness."""
        eid = _id(envelope_id)
        with self._locked():
            entry = self._load()["envelopes"].get(eid)
            if entry is None or entry["header"] is None:
                return None
            return json.loads(json.dumps({"contract": _RECEIPT, "kind": "receive", "status": "accepted",
                    "envelope_id": eid, "acked_at": entry["received_at"],
                    "records": entry["header"]["record_count"],
                    "records_sha256": entry["header"]["records_sha256"],
                    "accepted": entry["counts"]}))

    def has(self, envelope_id: str, *, now=None) -> bool:
        eid, now = _id(envelope_id), _now(now)
        with self._locked():
            entry = self._load()["envelopes"].get(eid)
            return entry is not None and _active(entry, now)

    def matches(self, envelope_id: str, records_sha256: str, intent_sha256: str,
                *, now=None) -> bool:
        eid, now = _id(envelope_id), _now(now)
        with self._locked():
            entry = self._load()["envelopes"].get(eid)
            return (entry is not None and _active(entry, now)
                    and entry["header"] is not None
                    and entry["header"]["records_sha256"] == records_sha256
                    and entry["intent_sha256"] == intent_sha256)

    def withdraw(self, envelope_id: str, *, auth_id: str | None = None, now=None) -> Record:
        """Create a tombstone, even before arrival; an arrived auth must match."""
        eid, now = _id(envelope_id), _now(now)
        with self._locked():
            state = self._load()
            entry = state["envelopes"].get(eid)
            if (auth_id is not None and entry is not None and entry["header"] is not None
                    and entry["header"]["auth_id"] != auth_id):
                raise C1ContractError("withdraw_auth_mismatch")
            if entry is None:
                if len(state["envelopes"]) >= self.max_envelopes:
                    raise C1ContractError("receiver_capacity_reached")
                new_entry: Entry = {
                    "status": "withdrawn", "header": None, "records": None,
                    "received_at": None, "expires_at": None, "wire_sha256": None,
                    "intent_sha256": None, "counts": {}, "items": [], "journal": []}
                entry = new_entry
                state["envelopes"][eid] = entry
            if not entry["journal"] or entry["status"] != "withdrawn":
                entry.update(status="withdrawn", records=None)
                entry["journal"].append({"status": "withdrawn", "at": now})
                self._save(state)
            return {"contract": _RECEIPT, "kind": "delete", "envelope_id": eid,
                    "deleted": True, "deleted_at": entry["journal"][-1]["at"]}

    def expire(self, *, now=None) -> dict[str, int]:
        """Remove expired parsed payloads; retain bounded replay/index/receipt metadata."""
        now = _now(now)
        with self._locked():
            state = self._load()
            expired = 0
            for entry in state["envelopes"].values():
                if (entry["status"] == "received" and entry["expires_at"] is not None
                        and entry["expires_at"] <= now):
                    entry.update(status="expired", records=None)
                    entry["journal"].append({"status": "expired", "at": now})
                    expired += 1
            if expired:
                self._save(state)
            return {"expired": expired, "payloads": sum(
                e["records"] is not None for e in state["envelopes"].values())}

    def _collections(self, state: State, now: int | float) -> list[CollectionView]:
        groups: dict[tuple[str, str], CollectionView] = {}
        for eid, entry in state["envelopes"].items():
            header = entry["header"]
            if header is None:
                continue
            key = (_cohort(header), _set_id(header))
            row = groups.setdefault(key, {
                "auth_id": header["auth_id"], "snapshot_generation_id": header["snapshot_generation_id"],
                "set": key[1], "expected_parts": header.get("part", {}).get("count", 1),
                "received_parts": 0, "active_parts": 0, "evidence": None, "status": "incomplete"})
            row["received_parts"] += 1
            row["active_parts"] += _active(entry, now)
        for (cohort, set_id), row in groups.items():
            selected = state["selected"].get(cohort)
            if selected and selected["evidence"]["set"] == set_id:
                if row["active_parts"] == row["expected_parts"]:
                    row.update(status="complete", evidence=deepcopy(selected["evidence"]))
                else:
                    row["status"] = "partial"
            elif selected:
                row["status"] = "unselected"
        return list(groups.values())

    def collections(self, *, now=None) -> list[CollectionView]:
        """Read collection state; incomplete/withdrawn/expired sets have no live evidence."""
        now = _now(now)
        with self._locked():
            return self._collections(self._load(), now)

    def _view(self, state: State, now: int | float) -> ReceiverView:
        entries, selected = state["envelopes"], state["selected"]
        result: ReceiverView = {"source_label": self.source_label, "collection_state": "incomplete",
                  "current_generation": None, "latest_complete_generation": None,
                  "evidence": None, "items": [], "signals": []}
        if not selected:
            return result
        latest_time = max(s["snapshot_generated_at"] for s in selected.values())
        latest = [s for s in selected.values() if s["snapshot_generated_at"] == latest_time]
        ambiguous = len({s["evidence"]["snapshot_generation_id"] for s in latest}) > 1
        current = min(latest, key=lambda s: (
            not all(_active(entries[eid], now) for eid in s["members"]),
            s["selected_at"], _hash(s["evidence"])))
        full = all(_active(entries[eid], now) for eid in current["members"])
        newer = any(_active(e, now) and (
                        e["header"]["snapshot_generated_at"] > latest_time
                        or (e["header"]["snapshot_generated_at"] == latest_time
                            and e["header"]["snapshot_generation_id"]
                            != current["evidence"]["snapshot_generation_id"]))
                    and _cohort(e["header"]) not in selected for e in entries.values()
                    if e["header"] is not None)
        certain = full and not newer and not ambiguous
        result["latest_complete_generation"] = current["evidence"]["snapshot_generation_id"]
        result["collection_state"] = ("ambiguous" if ambiguous else "partial" if not full
                                      else "newer_incomplete" if newer else "complete")
        if certain:
            result.update(current_generation=result["latest_complete_generation"],
                          evidence=deepcopy(current["evidence"]))
        current_records = [r for eid in current["members"] if _active(entries[eid], now)
                           for r in (entries[eid]["records"] or [])]
        current_messages = {(m["project_id"], m["message_id"]): (m, b)
                            for m, b in _bundles(current_records)}
        coverage: dict[int, Coverage] = {}
        for record in current_records:
            if record["type"] == "patient_coverage":
                patient: Coverage = json.loads(canonical_json(record))
                coverage[patient["project_id"]] = patient
        histories: dict[str, dict[str, Revision]] = {}
        available: dict[str, Bundle] = {}
        identities: dict[str, tuple[int, int]] = {}
        for selection in selected.values():
            for eid in selection["members"]:
                entry = entries[eid]
                header = entry["header"]
                assert header is not None
                for item in entry["items"]:
                    versions = histories.setdefault(item["item_id"], {})
                    revision = versions.setdefault(item["revision_id"], {
                        **item, "snapshot_generation_id": header["snapshot_generation_id"],
                        "snapshot_generated_at": header["snapshot_generated_at"], "links": [],
                        "available": False, "superseded_by": None})
                    revision["links"].append(eid)
                    revision["available"] |= _active(entry, now)
                if _active(entry, now):
                    for (message, body), item in zip(_bundles(entry["records"] or []), entry["items"]):
                        identities[item["item_id"]] = (message["project_id"], message["message_id"])
                        available[item["revision_id"]] = {"message": message, "body": body}
        for item_id, identity in sorted(identities.items()):
            revisions = sorted(histories[item_id].values(), key=lambda r: (
                r["snapshot_generated_at"], r["snapshot_generation_id"], r["revision_id"]))
            for index, revision in enumerate(revisions):
                revision["superseded_by"] = (
                    revisions[index + 1]["revision_id"] if index + 1 < len(revisions) else None)
            last = available.get(revisions[-1]["revision_id"])
            row: ItemView = {"item_id": item_id, "source_label": self.source_label,
                   "project_id": identity[0], "message_id": identity[1], "state": "unknown",
                   "reason": "collection_unknown", "current": None,
                   "last_known": last, "revisions": revisions}
            if certain and identity in current_messages:
                message, body = current_messages[identity]
                row.update(current={"message": message, "body": body},
                           state="deleted" if message["body_state"] == "deleted"
                           else "body_unavailable" if body is None else "present",
                           reason="current_record")
            elif certain:
                cov = coverage.get(identity[0])
                posted = last["message"].get("posted_at_ts") if last else None
                floor = cov["history_floor"] if cov else None
                upper = cov["coverage_ts"] if cov else None
                verified = (cov is not None and cov["fetch_state"] == "complete" and floor is not None
                            and upper is not None and posted is not None and floor <= posted <= upper)
                row.update(state="not_reposted" if verified else "unknown",
                           reason="not_reposted_in_verified_window" if verified
                           else "coverage_unknown_or_outside")
            result["items"].append(row)
        # Keep each generation's multiplicity. Reauthorization of that same
        # generation does not double its observations or manufacture a new item.
        signal_history: dict[str, list[Record]] = {}
        seen_generations = set()
        for selection in sorted(selected.values(), key=lambda s: (
                s["snapshot_generated_at"], s["evidence"]["snapshot_generation_id"],
                not all(_active(entries[eid], now) for eid in s["members"]), s["selected_at"])):
            generation = selection["evidence"]["snapshot_generation_id"]
            if generation in seen_generations:
                continue
            seen_generations.add(generation)
            grouped: dict[str, list[Record]] = {}
            for eid in selection["members"]:
                if not _active(entries[eid], now):
                    continue
                for record in entries[eid]["records"] or []:
                    if record["type"] == "signal":
                        key = _hash([self.source_label, record["project_id"], record["signal_type"],
                                     _normalize(record.get("evidence"))])
                        grouped.setdefault(key, []).append(record)
            signal_history.update(grouped)
        current_signals: dict[str, list[Record]] = {}
        for record in current_records:
            if record["type"] == "signal":
                key = _hash([self.source_label, record["project_id"], record["signal_type"],
                             _normalize(record.get("evidence"))])
                current_signals.setdefault(key, []).append(record)
        truncated = any(r["type"] == "signals_truncated" for r in current_records)
        for key, records in sorted(signal_history.items()):
            current_rows = current_signals.get(key, [])
            result["signals"].append({
                "signal_id": key, "count": len(current_rows) if certain else None,
                "state": "detected" if certain and current_rows else
                "not_detected" if certain and not truncated else "unknown",
                "records": current_rows if certain and current_rows else [],
                "last_known": records})
        return result

    def view(self, *, now=None) -> ReceiverView:
        """Return synthetic item data explicitly; use diagnostics() for shareable counts."""
        now = _now(now)
        with self._locked():
            return self._view(self._load(), now)

    def diagnostics(self, *, now=None) -> Diagnostics:
        """Return fixed reasons and counts only, never source/actor/item IDs or text."""
        now = _now(now)
        try:
            with self._locked():
                state = self._load()
                view = self._view(state, now)
                entries = list(state["envelopes"].values())
                pending_expiry = sum(e["status"] == "received" and e["expires_at"] is not None
                                     and e["expires_at"] <= now
                                     for e in entries)
                reasons = []
                if view["collection_state"] != "complete":
                    reasons.append("collection_" + view["collection_state"])
                if pending_expiry:
                    reasons.append("retention_cleanup_pending")
                return {"status": "unknown" if reasons else "ok", "reasons": reasons,
                        "counts": {"envelopes": len(entries),
                                   "payloads": sum(_active(e, now) for e in entries),
                                   "withdrawn": sum(e["status"] == "withdrawn" for e in entries),
                                   "expired": sum(e["status"] == "expired" for e in entries),
                                   "expiry_pending": pending_expiry, "items": len(view["items"]),
                                   "signals": len(view["signals"])}}
        except (C1ContractError, OSError):
            return {"status": "unknown", "reasons": ["receiver_state_unavailable"], "counts": {}}
