"""Assemble proposed C1 records and paired selections from one read transaction."""
from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Sequence
from copy import deepcopy
import hashlib
import math
import sqlite3
from typing import TypeAlias, TypedDict

import mcs_signals
import read_model
from c1_contract import JSONValue, MAX_BODY_BYTES, validate_record
from export_schema import finite_number, project_record

JSON: TypeAlias = JSONValue
Record: TypeAlias = dict[str, JSON]


class SnapshotRecords(TypedDict):
    records: list[Record]
    scope: dict[str, str]


class Selection(TypedDict):
    records: list[Record]
    dropped_types: dict[str, int]
    messages_dropped: int
    messages_unknown_time: int
    window_since: int | float | None
    window_until: int | float | None
    absence: str


class SnapshotError(ValueError):
    """Machine-readable local snapshot/selection error, not a wire error."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _timestamp(value: JSON) -> int | float | None:
    match value:
        case bool():
            return None
        case int() | float():
            return value if finite_number(value) and value >= 0 else None
        case None | str() | list() | dict():
            return None
        case _:
            raise SnapshotError("timestamp_type_invalid")


_PROFESSION_KINDS = {"医師": "physician", "看護師": "nurse",
                     "ケアマネ": "care_manager", "ケアマネジャー": "care_manager",
                     "介護支援専門員": "care_manager"}


class SenderPolicy(TypedDict):
    self_sender_id: int | None
    self_organizations: list[str]


def classify_sender(sender_id: object, profession: object, organization: object,
                    policy: SenderPolicy) -> str:
    """Map one sender to a sender_kind without trusting a profession alone.

    self_org needs the resolved self sender ID or an explicitly configured
    self organization. Any other mixed/unlisted profession, including our
    own profession at another organization, is other_professional. No
    recorded profession is unknown; patient_family has no grounded source.
    """
    sid = policy["self_sender_id"]
    if type(sid) is int and sid > 0 and sender_id == sid:
        return "self_org"
    if isinstance(organization, str) and organization in policy["self_organizations"]:
        return "self_org"
    names = [n.strip() for n in profession.split(",") if n.strip()] \
        if isinstance(profession, str) else []
    if not names:
        return "unknown"
    kinds = {_PROFESSION_KINDS.get(name, "other_professional") for name in names}
    return kinds.pop() if len(kinds) == 1 else "other_professional"


def assemble_records(db: sqlite3.Connection, *, signal_limit: int = 200,
                     sender_policy: SenderPolicy | None = None) -> SnapshotRecords:
    """Read a caller-validated View connection without opening/ending its transaction.

    Returns local proposed records, NOT an authorized envelope. Current
    aggregate patient scope is preserved, including archived and zero-message
    patients; membership in today's upstream fetch filter is not knowable here.
    sender_kind stays unknown unless the caller passes an explicit policy;
    no private config or live path is read here. Sender/patient name fields
    never enter the output.
    """
    if not db.in_transaction:
        raise SnapshotError("caller_read_transaction_required")
    if type(signal_limit) is not int or signal_limit < 1:
        raise SnapshotError("bad_signal_limit")
    model = read_model.read_model(db, scope="aggregate")
    snapshot = model["snapshot"]
    if not snapshot["published"] or not snapshot["generation_id"]:
        raise SnapshotError("published_snapshot_required")
    base = {"contract": read_model.CONTRACT,
            "snapshot_generation_id": snapshot["generation_id"]}
    records = [project_record({**base, "type": "meta", "snapshot": {
        "generation_id": snapshot["generation_id"], "generated_at": snapshot["generated_at"]}})]
    # Use precisely coverage_ts()/history_floor()'s source columns, not
    # last_complete_fetch, last_seen, or a newer stored-message watermark.
    columns = {row[1] for row in db.execute("PRAGMA table_info(patients)")}
    sql_columns = [name if name in columns else f"NULL AS {name}"
                   for name in ("fetch_state", "coverage_ts", "history_floor")]
    patients = db.execute("SELECT project_id," + ",".join(sql_columns)
                          + " FROM patients ORDER BY project_id").fetchall()
    coverage = project_record({**base, "type": "coverage", "coverage": model["coverage"]})
    coverage["coverage"]["collection"]["patients_incomplete"] = sum(
        patient["fetch_state"] != "complete" for patient in patients)
    records.append(coverage)
    for patient in patients:
        floor = patient["history_floor"]
        floor = 0 if floor == -1 else floor if floor is not None and floor > 0 else None
        patient_record = {**base, "type": "patient_coverage",
                          "project_id": patient["project_id"],
                          "fetch_state": patient["fetch_state"],
                          "coverage_ts": patient["coverage_ts"] or None,
                          "history_floor": floor}
        # Unrepresentable legacy fetch states fail closed, never shrink the
        # patient scope or silently coerce unknown into pending/complete.
        validate_record(patient_record)
        records.append(patient_record)
    signals = mcs_signals.current_open(db, limit=signal_limit)
    signal_items = signals["items"]
    assert isinstance(signal_items, list)  # current_open's fixed read-side result
    for signal in signal_items:
        records.append(project_record({
            **base, "type": "signal", "signal_type": signal["type"],
            "project_id": signal["project_id"], "detected_at": signal["detected_at"],
            "evidence": signal["evidence"]}))
    if signals["truncated"]:
        records.append(project_record({**base, "type": "signals_truncated",
                                       "total": signals["total"]}))
    bodies, senders = {}, {}
    for row in db.execute(
            "SELECT message_id,body_text,sender_id,profession,organization FROM messages "
            "WHERE body_state='full' AND body_text IS NOT NULL ORDER BY message_id"):
        bodies[row["message_id"]] = row["body_text"]
        senders[row["message_id"]] = "unknown" if sender_policy is None else classify_sender(
            row["sender_id"], row["profession"], row["organization"], sender_policy)
    for message in model["records"]:
        record = project_record({**base, "type": "message", **message})
        if record["body_state"] is None:
            record["body_state"] = "unknown"
        validate_record(record)
        records.append(record)
        mid = message["message_id"]
        if mid in bodies:
            original = bodies[mid].encode("utf-8")
            text = original[:MAX_BODY_BYTES].decode("utf-8", errors="ignore")
            body = {**base, "type": "message_body", "project_id": message["project_id"],
                    "message_id": mid, "body_text": text, "body_format": "text",
                    "body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "body_truncated": len(original) > MAX_BODY_BYTES,
                    "sender_kind": senders[mid]}
            validate_record(body, message=record)
            records.append(body)
    for record in records:
        if record["type"] not in ("message", "message_body", "patient_coverage"):
            validate_record(record)
    return {"records": records, "scope": {
        "patients": "all_source_patients", "archived": "included",
        "current_fetch_target_filter": "unknown",
        "sender_classification": "pending" if sender_policy is None else "explicit_policy",
        "source": "aggregate_read_model"}}


def select_records(records: Sequence[Record], allowed: Collection[str], *,
                   only_with_facts: bool = False, since_days: int | None = None) -> Selection:
    """Shrink proposed records, keeping bodies only with their same-generation message.

    Window bounds are snapshot-relative, inclusive, and never derived from
    wall time. Unknown message times are excluded (counted), not dated by
    guesswork. A missing record/filtered reply never proves clinical absence.
    Input records and legacy coverage are never mutated. Exact wire validation
    and authorization belong to the parent's separate contract integration.
    """
    if type(only_with_facts) is not bool:
        raise SnapshotError("bad_only_with_facts")
    if since_days is not None and (type(since_days) is not int or since_days < 1):
        raise SnapshotError("bad_since_days")
    since, until = None, None
    if since_days is not None:
        metas = [r for r in records if r.get("type") == "meta"]
        if len(metas) != 1:
            raise SnapshotError("snapshot_time_required")
        snapshot = metas[0].get("snapshot")
        if not isinstance(snapshot, dict):
            raise SnapshotError("snapshot_time_required")
        until = _timestamp(snapshot.get("generated_at"))
        if until is None:
            raise SnapshotError("snapshot_time_required")
        since = max(0, until - since_days * 86400)
    # Identity includes generation: no body from an older snapshot can hold
    # a new message. Duplicate identities are left to the exact validator.
    messages = {(r.get("snapshot_generation_id"), r.get("project_id"), r.get("message_id")): r
                for r in records if r.get("type") == "message"}
    body_keys = {(r.get("snapshot_generation_id"), r.get("project_id"), r.get("message_id"))
                 for r in records if r.get("type") == "message_body"
                 and "message_body" in allowed
                 and messages.get((r.get("snapshot_generation_id"), r.get("project_id"),
                                   r.get("message_id")), {}).get("body_state") == "full"}
    kept, unknown_time = set(), 0
    for key, message in messages.items():
        if "message" not in allowed:
            continue
        if since is not None and until is not None:
            stamp = _timestamp(message.get("posted_at_ts"))
            if stamp is None:
                unknown_time += 1
                continue
            if not since <= stamp <= until:
                continue
        if only_with_facts and not (
                message.get("facts") or message.get("body_state") == "deleted" or key in body_keys):
            continue
        kept.add(key)
    selected, dropped = [], Counter()
    for record in records:
        kind = record.get("type")
        if kind not in allowed:
            dropped[kind] += 1
            continue
        key = (record.get("snapshot_generation_id"), record.get("project_id"), record.get("message_id"))
        if kind == "message" and key not in kept:
            continue
        if kind == "message_body" and (key not in kept or key not in body_keys):
            continue
        copied = deepcopy(record)
        if kind == "patient_coverage" and since is not None:
            floor = copied.get("history_floor")
            if type(floor) is int and floor >= 0:
                # Floors are integer epochs; ceil is the first integer in
                # the exact (possibly fractional) snapshot-relative window.
                copied["history_floor"] = max(floor, math.ceil(since))
        validate_record(copied, message=messages.get(key) if kind == "message_body" else None)
        selected.append(copied)
    return {"records": selected, "dropped_types": dict(dropped),
            "messages_dropped": len(messages) - len(kept),
            "messages_unknown_time": unknown_time, "window_since": since,
            "window_until": until, "absence": "unknown_not_absence"}
