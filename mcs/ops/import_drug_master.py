"""Convert an explicitly pinned official medicine master offline into a private dictionary."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import date
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import TypeAlias
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402,F401
import drug_map  # noqa: E402

JSON: TypeAlias = None | bool | int | float | str | list["JSON"] | dict[str, "JSON"]
MENU_URL = "https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/"
SOURCE_URL = MENU_URL + "yFile"
SPEC_URL = "https://shinryohoshu.mhlw.go.jp/shinryohoshu/file/spec/R08rec3.pdf"
PIN_SCHEMA = "mcs-official-drug-master-pin/1"
LAYOUT = "R08rec3-medicine-42"
EDITION = "20260930"
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_CSV_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100000
# Official pp222/223: character/byte bounds and numeric leading-zero omission.
WIDTHS = (1, 1, 9, 2, 64, 2, 20, 3, 1, 12, 1, 13, 2, 1, 1, 1, 1,
          1, 1, 1, 5, 1, 9, 1, 13, 1, 1, 1, 49, 8, 8, 12, 9, 8, 200,
          8, 12, 200, 1, 1, 9, 1)
TEXT_COLUMNS = {1, 4, 6, 9, 28, 31, 34, 36, 37, 38, 39}


class MasterError(ValueError):
    """Refusal code without a row, alias, private path or upstream contents."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Conversion:
    report: dict[str, JSON]
    payload: bytes | None


def _read(path: Path, bound: int) -> bytes:
    if not path.is_absolute():
        raise MasterError("absolute_input_required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > bound:
            raise MasterError("input_file_or_size")
        raw = stream.read(bound + 1)
    if len(raw) > bound:
        raise MasterError("input_size")
    return raw


def _iso(value: JSON) -> date:
    if not isinstance(value, str):
        raise MasterError("pin_date_invalid")
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise MasterError("pin_date_invalid") from None


def convert(path: Path, pin: dict[str, JSON]) -> Conversion:
    """Read only local pinned bytes; unknown terms/status policy holds output.

    The PDF defines layout, not change/zero/sentinel semantics. status_policy
    is therefore an explicit operator declaration, never an invented official
    interpretation. A new supported edition requires review, not auto-detection.
    """
    if (pin.get("schema") != PIN_SCHEMA or pin.get("layout") != LAYOUT
            or pin.get("edition") != EDITION
            or set(pin) - {"schema", "layout", "edition", "sha256", "member", "as_of",
                          "terms_checked_on", "terms_record", "approved_by",
                          "status_policy", "medicine_codes"}):
        raise MasterError("pin_schema_layout_or_edition")
    expected = pin.get("sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise MasterError("pin_hash_invalid")
    as_of = _iso(pin.get("as_of"))
    if as_of < date(2026, 9, 30) or pin.get("member") != f"y_{EDITION}.csv":
        raise MasterError("pin_edition_or_member")
    terms = pin.get("terms_checked_on")
    terms_record = pin.get("terms_record")
    if terms is not None and (_iso(terms) > as_of or not drug_map._text(terms_record)):
        raise MasterError("terms_metadata_invalid")
    approval = pin.get("approved_by")
    if approval is not None and (not drug_map._text(approval) or terms is None):
        raise MasterError("approval_metadata_invalid")
    policy = pin.get("status_policy", {})
    if not isinstance(policy, dict) or set(policy) - {
            "candidate_change_values", "absent_date_values", "confirmed_by"}:
        raise MasterError("status_policy_invalid")
    changes = policy.get("candidate_change_values", [])
    absent = policy.get("absent_date_values", [])
    if (not isinstance(changes, list) or not isinstance(absent, list)
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9]", v) for v in changes)
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9]{1,8}", v) for v in absent)
            or ((changes or absent) and not drug_map._text(policy.get("confirmed_by")))):
        raise MasterError("status_policy_invalid")
    selected = pin.get("medicine_codes")
    if selected is not None and (
            not isinstance(selected, list) or len(selected) > drug_map.MAX_ENTRIES
            or not selected
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9]{1,9}", v)
                   or int(v) == 0 for v in selected)
            or len(set(selected)) != len(selected)):
        raise MasterError("selection_invalid")
    if selected is not None:
        selected = frozenset(selected)   # O(1) membership per master row
    raw = _read(path, MAX_INPUT_BYTES)
    source_hash = hashlib.sha256(raw).hexdigest()
    if source_hash != expected:
        raise MasterError("source_hash_mismatch")
    csv_bytes = raw
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = archive.infolist()
                if (len(members) != 1 or members[0].filename != pin["member"]
                        or members[0].is_dir() or members[0].flag_bits & 1
                        or stat.S_ISLNK(members[0].external_attr >> 16)
                        or members[0].file_size > MAX_CSV_BYTES
                        or members[0].compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise MasterError("zip_members_or_size")
                with archive.open(members[0]) as stream:
                    csv_bytes = stream.read(MAX_CSV_BYTES + 1)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
            raise MasterError("zip_invalid") from None
    elif path.suffix.lower() != ".csv" or path.name != pin["member"]:
        raise MasterError("csv_member_invalid")
    if len(csv_bytes) > MAX_CSV_BYTES:
        raise MasterError("csv_size")
    try:
        text = csv_bytes.decode("cp932", errors="strict")
    except UnicodeDecodeError:
        raise MasterError("csv_encoding_invalid") from None
    reasons, groups, seen, count = Counter(), {}, set(), 0
    try:
        for row in csv.reader(io.StringIO(text, newline=""), strict=True):
            count += 1
            if count > MAX_ROWS:
                raise MasterError("row_bound")
            if len(row) != 42 or row[1] != "Y":
                raise MasterError("csv_layout_invalid")
            for i, value in enumerate(row):
                if len(value.encode("cp932")) > WIDTHS[i] or any(ord(c) < 32 for c in value):
                    raise MasterError("csv_field_bound")
                if i not in TEXT_COLUMNS and i != 11 and not re.fullmatch(r"[0-9]+", value):
                    raise MasterError("csv_numeric_invalid")
            if (not re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,2})?", row[11])
                    or any(not re.fullmatch(r"[A-Za-z0-9]*", row[i]) for i in (31, 36, 38, 39))
                    or any(not 32 <= ord(c) < 127 for c in row[28])
                    or any(not (32 <= ord(c) < 127 or 0xFF61 <= ord(c) <= 0xFF9F) for c in row[6])
                    or any(int(row[n]) != len(row[s]) for n, s in ((3, 4), (5, 6), (8, 9)))
                    or int(row[3]) > 32 or int(row[5]) > 20 or int(row[8]) > 6
                    or len(row[34]) > 100 or len(row[37]) > 100):
                raise MasterError("csv_value_invalid")
            medicine = row[2]
            if medicine in seen:
                raise MasterError("duplicate_medicine_code")
            seen.add(medicine)
            dates, unknown_date = {}, False
            for column in (29, 30, 33, 35):
                value = row[column]
                if value in absent:
                    dates[column] = None
                else:
                    try:
                        if len(value) != 8:
                            raise ValueError
                        dates[column] = date(int(value[:4]), int(value[4:6]), int(value[6:]))
                    except ValueError:
                        unknown_date = True
            if int(medicine) == 0:
                reasons["unknown_medicine_identity"] += 1
            elif selected is not None and medicine not in selected:
                reasons["outside_explicit_selection"] += 1
            elif row[0] not in changes:
                reasons["unconfirmed_change_value"] += 1
            elif unknown_date:
                reasons["unconfirmed_date_or_sentinel"] += 1
            elif any(dates[c] is not None and dates[c] <= as_of for c in (30, 33)):
                reasons["ended_or_end_boundary"] += 1
            elif any(dates[c] is not None and dates[c] > as_of for c in (29, 35)):
                reasons["future_date"] += 1
            elif not row[4] or not drug_map.fold(row[4]):
                reasons["missing_product_name"] += 1
            else:
                general = (len(row[36]) == 12 and any(c != "0" for c in row[36])
                           and bool(drug_map.fold(row[37])))
                code = row[36] if general else medicine
                kind = "general_name" if general else "product"
                identity = f"mhlw:{'general' if general else 'medicine'}:{code}"
                display = row[37] if general else row[34] if drug_map.fold(row[34]) else row[4]
                entry = groups.setdefault(identity, {
                    "id": identity, "kind": kind, "display": display, "aliases": [],
                    "codes": {"general_name" if general else "medicine": code},
                    "source_codes": [], "forms": []})
                if entry["display"] != display:
                    raise MasterError("source_identity_text_conflict")
                aliases = [row[4], row[34]]
                if row[6] and int(row[5]) < 20:
                    aliases.append(row[6])
                elif row[6]:
                    reasons["max_length_kana_excluded"] += 1
                if not general:
                    reasons["ingredient_identity_unresolved"] += 1
                entry["aliases"].extend(a for a in aliases if drug_map.fold(a))
                entry["source_codes"].append({
                    "medicine": medicine, "drug_price": row[31] or None,
                    "general_name": row[36] or None})
                reasons["candidate_rows"] += 1
    except csv.Error:
        raise MasterError("csv_parse_invalid") from None
    if not count:
        raise MasterError("empty_master")
    if selected is not None and set(selected) - seen:
        raise MasterError("selection_code_not_found")
    entries = [groups[key] for key in sorted(groups)]
    for entry in entries:
        entry["aliases"] = sorted(set(entry["aliases"]))
    report: dict[str, JSON] = {"schema": "mcs-official-drug-master-report/1", "edition": EDITION,
              "layout": LAYOUT, "source_sha256": source_hash,
              "csv_sha256": hashlib.sha256(csv_bytes).hexdigest(), "rows": count,
              "entries": len(entries), "reasons": dict(sorted(reasons.items())),
              "source": {"url": SOURCE_URL, "spec": SPEC_URL,
                         "terms_checked_on": terms, "approval_recorded": approval is not None,
                         "status_policy_confirmed": bool(changes and absent)},
              "activation": False, "output_sha256": None,
              "dictionary_limits": {"entries": drug_map.MAX_ENTRIES,
                                    "bytes": drug_map.MAX_BYTES,
                                    "aliases_per_entry": drug_map.MAX_ALIASES},
              "selection": "explicit_subset" if selected is not None else "whole_master",
              "payload_bytes": None}
    payload = None
    if terms is None:
        report["held"] = "terms_unconfirmed"
    elif not entries:
        report["held"] = "no_candidate_entries"
    elif len(entries) > drug_map.MAX_ENTRIES or any(
            len(e["aliases"]) > drug_map.MAX_ALIASES
            or len(e["source_codes"]) > drug_map.MAX_ENTRIES for e in entries):
        report["held"] = "dictionary_entry_or_alias_bound"
    else:
        source = {"name": "厚生労働省 医薬品マスター " + EDITION, "url": SOURCE_URL,
                  "terms_checked_on": terms, "terms_record": terms_record,
                  "master_sha256": source_hash, "csv_sha256": report["csv_sha256"],
                  "spec_url": SPEC_URL, "layout": LAYOUT, "status_policy": policy}
        if approval is not None:
            source["approved_by"] = approval
        document = {"schema": drug_map.SOURCE_SCHEMA, "dict_id": "mhlw-y-" + EDITION + "-" + source_hash[:12],
                    "source": source, "entries": entries}
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        report["payload_bytes"] = len(payload)
        if len(payload) > drug_map.MAX_BYTES:
            payload = None
            report["held"] = "dictionary_byte_bound"
        else:
            report["output_sha256"] = hashlib.sha256(payload).hexdigest()
    return Conversion(report, payload)


def write_private(result: Conversion, destination: Path) -> None:
    """Validate a private staging file, then atomically link a NEW destination."""
    if result.payload is None:
        raise MasterError("output_held")
    if not destination.is_absolute() or destination.parent.resolve() != destination.parent:
        raise MasterError("private_output_path")
    parent = destination.parent.stat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077):
        raise MasterError("private_output_directory")
    fd, temporary = tempfile.mkstemp(prefix=".drug-master-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(result.payload)
            stream.flush()
            os.fsync(stream.fileno())
        digest = hashlib.sha256(result.payload).hexdigest()
        if drug_map.load(temporary, expected_sha256=digest) is None:
            raise MasterError("dictionary_unavailable")
        os.link(temporary, destination, follow_symlinks=False)
    finally:
        os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--pin", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        pin = json.loads(_read(args.pin, 65536))
        if not isinstance(pin, dict):
            raise MasterError("pin_object_required")
        result = convert(args.source, pin)
        if args.output is not None and not args.dry_run:
            write_private(result, args.output)
        print(json.dumps({**result.report,
                         "written": args.output is not None and not args.dry_run}, ensure_ascii=False))
        return 0
    except (MasterError, ValueError, OSError, TypeError, RecursionError) as error:
        code = error.code if isinstance(error, MasterError) else (
            "output_exists" if isinstance(error, FileExistsError) else "input_or_dictionary_invalid")
        print(json.dumps({"status": "refused", "reason": code, "written": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
