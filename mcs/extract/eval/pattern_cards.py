"""Lint aggregate pattern cards against fixed vocabulary and local name denylisting."""
from __future__ import annotations

import argparse
from contextlib import closing
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import NoReturn, TypeAlias, TypeGuard
import unicodedata


JSON: TypeAlias = dict[str, "JSON"] | list["JSON"] | str | int | float | bool | None

# Conservative identity keys cover future tables/JSON without enumerating storage paths.
NAME_KEY_PATTERNS = (
    "name", "*_name", "sender*", "organization*", "org", "staff*", "author*",
    "display*", "nickname", "patient*", "family*", "kana", "furigana",
)
_HARVEST_MAX_BYTES = 1024 * 1024
_HARVEST_MAX_DEPTH = 16
_HARVEST_MAX_NODES = 10000

# Named identities, including historical copies and optional request/notification schemas.
# Non-JSON rows read as NULL (no names); the generic harvest still scans their text.
NAME_SOURCES = {
    "patients.patient_name": (
        "patients", "SELECT patient_name FROM patients", "Patient identity."),
    "patients.station_name": (
        "patients", "SELECT station_name FROM patients", "Patient's facility."),
    "messages.sender_name": (
        "messages", "SELECT sender_name FROM messages", "Posting person's name."),
    "messages.organization": (
        "messages", "SELECT organization FROM messages", "Posting person's organizations."),
    "messages_fts.sender_name": (
        "messages_fts", "SELECT sender_name FROM messages_fts", "Stored sender-name copy."),
    "message_reaction_actors.actor_name": (
        "message_reaction_actors", "SELECT actor_name FROM message_reaction_actors",
        "Reaction people, including removed historical actors."),
    "message_reaction_actors.organization": (
        "message_reaction_actors", "SELECT organization FROM message_reaction_actors",
        "Reaction actors' organizations, including historical actors."),
    "station_staff_v1.staff.name": (
        "artifacts", "SELECT json_extract(CASE WHEN json_valid(s.value) THEN s.value END,'$.name') FROM artifacts a, "
        "json_each(CASE WHEN json_valid(a.content) THEN a.content END,'$.staff') s WHERE a.kind='station_staff_v1'",
        "Every historical staff roster's person names."),
    "station_staff_v1.staff.station": (
        "artifacts", "SELECT json_extract(CASE WHEN json_valid(s.value) THEN s.value END,'$.station') FROM artifacts a, "
        "json_each(CASE WHEN json_valid(a.content) THEN a.content END,'$.staff') s WHERE a.kind='station_staff_v1'",
        "Every historical staff roster's facility names."),
    "karte_summary.updater.name": (
        "artifacts", "SELECT json_extract(CASE WHEN json_valid(content) THEN content END,'$.updater.name') FROM artifacts "
        "WHERE kind='karte_summary'", "Summary updater's person name."),
    "self_profile_v1.name": (
        "artifacts", "SELECT json_extract(CASE WHEN json_valid(content) THEN content END,'$.name') FROM artifacts "
        "WHERE kind='self_profile_v1'", "Logged-in person's name, including old profiles."),
    "self_profile_v1.organizations": (
        "artifacts", "SELECT o.value FROM artifacts a, "
        "json_each(CASE WHEN json_valid(a.content) THEN a.content END,'$.organizations') o WHERE a.kind='self_profile_v1'",
        "Logged-in person's organization names, including old profiles."),
    "patient_rollup.top_senders": (
        "artifacts", "SELECT json_extract(CASE WHEN json_valid(s.value) THEN s.value END,'$[0]') FROM artifacts a, "
        "json_each(CASE WHEN json_valid(a.content) THEN a.content END,'$.top_senders') s WHERE a.kind='patient_rollup'",
        "Stored historical sender names in patient aggregates."),
    "requests.assignee": (
        "requests", "SELECT assignee FROM requests", "Free-text name or name(facility) label."),
    "command_receipts.actor": (
        "command_receipts", "SELECT json_extract(CASE WHEN json_valid(receipt_json) THEN receipt_json END,'$.actor') FROM command_receipts",
        "Human approval identity, which may be a free-text name."),
    "command_receipts.before.assignee": (
        "command_receipts", "SELECT json_extract(CASE WHEN json_valid(receipt_json) THEN receipt_json END,'$.before.assignee') "
        "FROM command_receipts", "Historical pre-update assignee name/facility label."),
    "command_receipts.after.assignee": (
        "command_receipts", "SELECT json_extract(CASE WHEN json_valid(receipt_json) THEN receipt_json END,'$.after.assignee') "
        "FROM command_receipts", "Historical post-update assignee name/facility label."),
    "command_receipts.owner": (
        "command_receipts", "SELECT json_extract(CASE WHEN json_valid(receipt_json) THEN receipt_json END,'$.owner') FROM command_receipts",
        "Historical notification ownership identity."),
    "notification_acknowledgements.actor": (
        "notification_acknowledgements", "SELECT actor FROM notification_acknowledgements",
        "Acknowledging person's identity; do not assume every transport supplies IDs."),
    "notification_triage.owner": (
        "notification_triage", "SELECT owner FROM notification_triage",
        "Assigned person's identity."),
    "notification_triage.last_actor": (
        "notification_triage", "SELECT last_actor FROM notification_triage",
        "Last acting person's identity."),
    "artifacts.content.actor": (
        "artifacts", "SELECT json_extract(CASE WHEN json_valid(content) THEN content END,'$.actor') FROM artifacts",
        "All historical operation actors, including link, feedback, policy and approval rows."),
    "artifacts.meta.actor": (
        "artifacts", "SELECT json_extract(CASE WHEN json_valid(meta) THEN meta END,'$.actor') FROM artifacts",
        "All historical operation actors stored in metadata, without a kind allowlist."),
    "artifacts.content.dismissed_by": (
        "artifacts", "SELECT json_extract(CASE WHEN json_valid(content) THEN content END,'$.dismissed_by') FROM artifacts",
        "Person who dismissed a signal, retained in its historical transition."),
}


CLASSES = frozenset({
    "one_dose_packaging", "prescription_inquiry", "storage", "leftover",
    "swallowing", "next_visit", "adherence", "rx_change", "interaction_check",
    "sleep", "excretion", "renal_hepatic", "pain_opioid", "weight", "fall", "adverse",
})
ATTRIBUTES = {
    "negation": frozenset({"affirmed", "negated", "unknown"}),
    "subject": frozenset({"patient", "family", "other", "unknown"}),
    "tense": frozenset({"past", "present", "future", "unknown"}),
    "condition": frozenset({"conditional", "unconditional", "unknown"}),
}
ENUMS = frozenset({
    "missing", "unknown", "none", "current", "past", "planned", "new", "ongoing",
    "resolved", "patient", "family", "other", "start", "stop", "change", "decrease",
    "increase", "request", "question", "self_plan", "high", "routine", "unclear",
    "visit", "exam", "admission", "discharge", "transfer", "fall", "eol", "care",
    "family_contact", "prescription_inquiry", "interaction_check", "true", "false",
    "oral", "topical", "injection", "infusion", "inhalation", "tube",
    "mild", "moderate", "severe", "low",
})
_CATEGORIES = (
    "demographics", "diagnoses", "history", "course", "adl", "living", "care_level",
    "care_services", "care_team", "contacts", "allergies", "devices", "preferences",
    "medication_management", "nutrition", "cognition", "adverse_events",
    "observations", "followup",
)
_DETAILS = (
    "drug_name", "medication_kind", "prescriber", "actual_dose", "actual_frequency",
    "residual_quantity", "missed_doses", "symptom", "onset", "response", "outcome",
    "certainty", "observation_name", "value", "unit", "measured_on", "condition",
    "swallowing", "self_management", "supporter", "support_method", "followup",
    "assignee", "due_text", "observed_on", "last_confirmed_on", "confirmed_by",
    # planned pharmacy details (plan T10); not yet runtime DETAIL_KEYS
    "storage_status", "storage_place", "medication_manager", "inquiry_result",
    "interaction_check",
)
_FIELDS = {
    "meds": ("name", "dose", "action", "status", "subject", "negated", "route",
             "freq", "prn", "evidence"),
    "symptoms": ("text", "negated", "status", "subject", "severity", "onset",
                 "duration", "evidence"),
    "requests": ("to", "from", "kind", "action", "condition", "due", "due_text",
                 "evidence"),
    "labs": ("name", "value", "unit", "flag", "evidence", "subject", "status",
             "measured_on", "condition"),
    "patient_context": ("category", "text", "evidence", "subject", "details"),
    "vitals": ("bt", "hr", "rr", "sbp", "dbp", "bs"),
    "events": (), "summary": (), "points": (), "urgency": (), "urgency_evidence": (),
}
FIELD_PATHS = frozenset(
    set(_FIELDS)
    | {f"{section}.{key}" for section, keys in _FIELDS.items() for key in keys}
    | {f"patient_context.{category}" for category in _CATEGORIES}
    | {f"patient_context.{category}.{detail}"
       for category in _CATEGORIES for detail in _DETAILS}
    | {f"events:{event}" for event in ENUMS if event in {
        "visit", "exam", "admission", "discharge", "transfer", "fall", "eol",
        "care", "family_contact", "other", "prescription_inquiry", "interaction_check"}}
)
_KEYS = frozenset({
    "class", "field_paths", "attributes", "counts", "current_shape", "expected_shape",
    *ATTRIBUTES, "posts", "patients", "professions",
})
_ID = re.compile(
    r"(?:message|project|msg|mid|pid)(?:[_:/=-]|$)|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _normalise(value: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", value).casefold()
                   if unicodedata.category(char) != "Cf")


def _unique_object(pairs: list[tuple[str, JSON]]) -> dict[str, JSON]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def lint(card: dict[str, JSON], denylist: set[str]) -> list[str]:
    """Return rule codes with trusted key paths, never input values or unknown keys."""
    errors = []
    names = {_normalise(name) for name in denylist} - {""}
    fragments: set[str] = set()

    def text(value: str, path: str) -> None:
        if any(name in _normalise(value) for name in names):
            errors.append(f"denylist:{path}")
        if any(char.isdigit() for char in value):
            errors.append(f"digit:{path}")
        if _ID.search(value):
            errors.append(f"identifier:{path}")
        if not value.isascii():
            errors.append(f"non_ascii:{path}")

    # Inspect rejected branches too, but never echo their untrusted key names.
    stack: list[tuple[JSON, str, int]] = [(card, "$", 0)]
    while stack:
        value, path, depth = stack.pop()
        if depth > 16:
            return ["depth:$"]
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if isinstance(key, str) and key in _KEYS else f"{path}.*"
                if isinstance(key, str):
                    text(key, child_path)
                stack.append((child, child_path, depth + 1))
        elif isinstance(value, list):
            stack.extend((child, f"{path}[]", depth + 1) for child in value)
        elif isinstance(value, str):
            text(value, path)
            if _normalise(value):
                fragments.add(_normalise(value))

    # Reject identities reconstructed from allowed fragments, regardless of field order.
    for name in names:
        reachable = {0}
        for offset in range(len(name)):
            if offset in reachable:
                reachable.update(offset + len(part) for part in fragments
                                 if name.startswith(part, offset))
        if len(name) in reachable:
            errors.append("denylist:$")

    def keys(value: JSON, required: set[str], path: str) -> TypeGuard[dict[str, JSON]]:
        if not isinstance(value, dict):
            errors.append(f"object:{path}")
            return False
        if set(value) - required:
            errors.append(f"unknown_key:{path}.*")
        for key in sorted(required - set(value)):
            errors.append(f"missing_key:{path}.{key}")
        return True

    def tokens(value: JSON, allowed: frozenset[str], path: str, *, scalar=False) -> None:
        if scalar and isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            errors.append(f"list:{path}")
        elif any(not isinstance(item, str) or item not in allowed for item in value):
            errors.append(f"vocabulary:{path}")

    required = {"class", "field_paths", "attributes", "counts", "current_shape", "expected_shape"}
    if keys(card, required, "$"):
        if not isinstance(card.get("class"), str) or card["class"] not in CLASSES:
            errors.append("vocabulary:$.class")
        tokens(card.get("field_paths"), FIELD_PATHS, "$.field_paths")
        attrs = card.get("attributes")
        if keys(attrs, set(ATTRIBUTES), "$.attributes"):
            for key, allowed in ATTRIBUTES.items():
                value = attrs.get(key)
                if key == "negation" and type(value) is bool:
                    continue
                if not isinstance(value, str) or value not in allowed:
                    errors.append(f"vocabulary:$.attributes.{key}")
        counts = card.get("counts")
        if keys(counts, {"posts", "patients", "professions"}, "$.counts"):
            # Aggregate counts are the only permitted numeric values.
            for key in ("posts", "patients", "professions"):
                count = counts.get(key)
                if type(count) is not int or count < 0:
                    errors.append(f"count:$.counts.{key}")
        for key in ("current_shape", "expected_shape"):
            tokens(card.get(key), FIELD_PATHS | ENUMS, f"$.{key}", scalar=True)
    # Even fixed schema paths can collide with a stored person's name.
    safe_errors = set()
    for error in errors:
        rule, _, path = error.partition(":")
        if any(name in _normalise(path) for name in names):
            path = "$"
        safe_errors.add(f"{rule}:{path}")
    return sorted(safe_errors)


def _name_key(key: str) -> bool:
    return any(fnmatchcase(_normalise(key), pattern) for pattern in NAME_KEY_PATTERNS)


def _generic_names(db: sqlite3.Connection, tables: set[str]) -> set[str]:
    """Harvest matching TEXT columns and nested JSON leaves, failing closed on bounds."""
    names = set()
    for table in sorted(tables):
        quoted_table = '"' + table.replace('"', '""') + '"'
        for info in db.execute(f"PRAGMA table_xinfo({quoted_table})"):
            column = info[1]
            quoted_column = '"' + column.replace('"', '""') + '"'
            column_is_name = _name_key(column)
            # SQLite affinity does not constrain stored types; include actual TEXT values.
            for (text,) in db.execute(
                f"SELECT {quoted_column} FROM {quoted_table} "
                f"WHERE typeof({quoted_column})='text'"
            ):
                if len(text.encode("utf-8")) > _HARVEST_MAX_BYTES:
                    raise ValueError("identity_text_bounds")
                if column_is_name and text.strip():
                    names.add(text.strip())
                try:
                    document = json.loads(text, object_pairs_hook=_unique_object)
                except json.JSONDecodeError:
                    continue  # ordinary non-JSON TEXT, not a structured name container
                stack: list[tuple[JSON, str | None, int]] = [(document, None, 0)]
                visited = 0
                while stack:
                    value, key, depth = stack.pop()
                    visited += 1
                    if depth > _HARVEST_MAX_DEPTH or visited > _HARVEST_MAX_NODES:
                        raise ValueError("identity_json_bounds")
                    if isinstance(value, str):
                        if value.strip() and (column_is_name or (key is not None and _name_key(key))):
                            names.add(value.strip())
                    elif isinstance(value, dict):
                        stack.extend((child, child_key, depth + 1)
                                     for child_key, child in value.items())
                    elif isinstance(value, list):
                        stack.extend((child, key, depth + 1) for child in value)
    return names


def _denylist(db: sqlite3.Connection) -> set[str]:
    names = set()
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    if not {"patients", "messages", "artifacts"} <= tables:
        raise ValueError("snapshot_schema")
    for table, query, _why in NAME_SOURCES.values():
        if table not in tables:
            continue  # request/notification schemas need not exist in every snapshot
        for (value,) in db.execute(query):
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
    names.update(_generic_names(db, tables))
    # MCS joins organizations with commas and resolves staff as name(facility).
    for value in names.copy():
        names.update(part.strip() for part in re.split(r"[,()]", _normalise(value))
                     if part.strip())
    return names


def _snapshot_denylist(source: Path) -> set[str]:
    """Read committed DB/WAL identities without creating sidecars beside the source."""
    private = Path(tempfile.mkdtemp(prefix="mcs-pattern-cards-"))
    try:
        source = source.resolve()
        if Path(str(source) + "-journal").exists():
            raise ValueError("snapshot_hot_journal")
        copy = private / "snapshot.db"
        for suffix in ("", "-wal", "-shm"):
            original = Path(str(source) + suffix)
            if suffix and not original.exists():
                continue
            target = Path(str(copy) + suffix)
            shutil.copyfile(original, target)
            os.chmod(target, 0o600)
        with closing(sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)) as db:
            return _denylist(db)
    finally:
        shutil.rmtree(private)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError("arguments")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("command", choices=["lint"])
    parser.add_argument("card", type=Path)
    parser.add_argument("--db", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        names = _snapshot_denylist(args.db)
        card = json.loads(args.card.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        errors = lint(card, names)
    except (OSError, UnicodeError, ValueError, RecursionError, sqlite3.Error):
        # An incomplete denylist cannot make even a fixed diagnostic word safe.
        return 1
    for error in errors:
        # A name can even equal a rule code; silence that diagnostic, not the failure status.
        if not any(_normalise(name) in _normalise(error) for name in names if _normalise(name)):
            print(error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
