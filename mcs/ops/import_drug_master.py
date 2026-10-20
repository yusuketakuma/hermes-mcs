"""Convert an explicitly pinned official medicine master offline into a private dictionary."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
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
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402,F401
import drug_map  # noqa: E402
from mcs_util import loads_dict  # noqa: E402
from safe_io import fsync_dir, read_regular_nofollow, fdopen_owned  # noqa: E402

JSON: TypeAlias = None | bool | int | float | str | list["JSON"] | dict[str, "JSON"]
MENU_URL = "https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/"
SOURCE_URL = MENU_URL + "yFile"
SPEC_URL = "https://shinryohoshu.mhlw.go.jp/shinryohoshu/file/spec/R08rec3.pdf"
PIN_SCHEMA = "mcs-official-drug-master-pin/1"
LAYOUT = "R08rec3-medicine-42"
EDITION = "20261013"
EDITION_DATE = date(int(EDITION[:4]), int(EDITION[4:6]), int(EDITION[6:]))  # pin as_of floor
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_CSV_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100000
BUNDLE = Path(__file__).resolve().parents[2] / "resources" / "drug-master"
# Official pp222/223: character/byte bounds and numeric leading-zero omission.
WIDTHS = (1, 1, 9, 2, 64, 2, 20, 3, 1, 12, 1, 13, 2, 1, 1, 1, 1,
          1, 1, 1, 5, 1, 9, 1, 13, 1, 1, 1, 49, 8, 8, 12, 9, 8, 200,
          8, 12, 200, 1, 1, 9, 1)
TEXT_COLUMNS = {1, 4, 6, 9, 28, 31, 34, 36, 37, 38, 39}


class MasterError(ValueError):
    """Refusal code without a row, alias, private path or upstream contents."""

    def __init__(self, code: str, *, written: bool = False):
        self.code = code
        self.written = written
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Conversion:
    report: dict[str, JSON]
    payload: bytes | None


def read_master_input(path: Path, bound: int) -> bytes:
    if not path.is_absolute():
        raise MasterError("absolute_input_required")
    raw = read_regular_nofollow(path, bound, lambda: MasterError("input_file_or_size"))
    if len(raw) > bound:
        raise MasterError("input_size")
    return raw


def load_bundled_master(directory: Path = BUNDLE):
    """Read the bundled manifest of the converter's pinned edition and its archive path."""
    folder = (directory / EDITION).resolve()
    manifest = loads_dict(read_master_input(folder / "manifest.json", 65536))
    if (manifest is None or manifest.get("edition") != EDITION
            or manifest.get("layout") != LAYOUT
            or manifest.get("member") != f"y_{EDITION}.csv"
            or manifest.get("archive") != f"y_{EDITION}.zip"
            or any(not isinstance(manifest.get(key), str)
                   or not re.fullmatch(r"[0-9a-f]{64}", manifest[key])
                   for key in ("archive_sha256", "csv_sha256"))
            or type(manifest.get("rows")) is not int
            or not 0 < manifest["rows"] <= MAX_ROWS
            or not isinstance(manifest.get("terms"), dict)
            or not drug_map.valid_text(manifest["terms"].get("url"))):
        raise ValueError("bundle_manifest")
    return manifest, folder / manifest["archive"]


def _iso(value: JSON) -> date:
    if not isinstance(value, str):
        raise MasterError("pin_date_invalid")
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise MasterError("pin_date_invalid") from None


def _check_pin(pin: dict[str, JSON]):
    """Validate the operator pin and decode its declared policy values.

    Returns (expected_sha256, as_of, status_policy, change_values,
    absent_date_values, selected_codes_or_None)."""
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
    if as_of < EDITION_DATE or pin.get("member") != f"y_{EDITION}.csv":
        raise MasterError("pin_edition_or_member")
    policy = pin.get("status_policy", {})
    if not isinstance(policy, dict) or set(policy) - {
            "candidate_change_values", "absent_date_values", "confirmed_by"}:
        raise MasterError("status_policy_invalid")
    changes = policy.get("candidate_change_values", [])
    absent = policy.get("absent_date_values", [])
    if (not isinstance(changes, list) or not isinstance(absent, list)
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9]", v) for v in changes)
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9]{1,8}", v) for v in absent)
            or ((changes or absent) and not drug_map.valid_text(policy.get("confirmed_by")))):
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
    return expected, as_of, policy, changes, absent, selected


def _master_csv(path: Path, member: str, raw: bytes) -> bytes:
    """Return the pinned member's CSV bytes from a local csv or single-member zip."""
    csv_bytes = raw
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = archive.infolist()
                if (len(members) != 1 or members[0].filename != member
                        or members[0].is_dir() or members[0].flag_bits & 1
                        or stat.S_ISLNK(members[0].external_attr >> 16)
                        or members[0].file_size > MAX_CSV_BYTES
                        or members[0].compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise MasterError("zip_members_or_size")
                with archive.open(members[0]) as stream:
                    csv_bytes = stream.read(MAX_CSV_BYTES + 1)
        except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError):
            raise MasterError("zip_invalid") from None
    elif path.suffix.lower() != ".csv" or path.name != member:
        raise MasterError("csv_member_invalid")
    if len(csv_bytes) > MAX_CSV_BYTES:
        raise MasterError("csv_size")
    return csv_bytes


def _check_master_row(row: list[str]) -> None:
    """Layout, field-bound and value checks of one master CSV record."""
    if len(row) != 42 or row[1] != "Y":
        raise MasterError("csv_layout_invalid")
    for i, value in enumerate(row):
        if len(value.encode("cp932")) > WIDTHS[i] or any(ord(c) < 32 for c in value):
            raise MasterError("csv_field_bound")
        if i not in TEXT_COLUMNS and i not in (11, 24) and not re.fullmatch(r"[0-9]+", value):
            raise MasterError("csv_numeric_invalid")
    # Field 25 is an unused 13-byte numeric reserve, not a price.
    # The published master uses decimal text there; keep it opaque.
    if (not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", row[24])
            or not re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,2})?", row[11])
            or any(not re.fullmatch(r"[A-Za-z0-9]*", row[i]) for i in (31, 36, 38, 39))
            or any(not 32 <= ord(c) < 127 for c in row[28])
            or any(not (32 <= ord(c) < 127 or 0xFF61 <= ord(c) <= 0xFF9F) for c in row[6])
            or any(int(row[n]) != len(row[s]) for n, s in ((3, 4), (5, 6), (8, 9)))
            or int(row[3]) > 32 or int(row[5]) > 20 or int(row[8]) > 6
            or len(row[34]) > 100 or len(row[37]) > 100):
        raise MasterError("csv_value_invalid")


def convert(path: Path, pin: dict[str, JSON]) -> Conversion:
    """Read only local pinned bytes; unknown terms/status policy holds output.

    The PDF defines layout, not change/zero/sentinel semantics. status_policy
    is therefore an explicit operator declaration, never an invented official
    interpretation. A new supported edition requires review, not auto-detection.
    """
    expected, as_of, policy, changes, absent, selected = _check_pin(pin)
    terms = pin.get("terms_checked_on")
    terms_record = pin.get("terms_record")
    if terms is not None and (_iso(terms) > as_of or not drug_map.valid_text(terms_record)):
        raise MasterError("terms_metadata_invalid")
    approval = pin.get("approved_by")
    if approval is not None and (not drug_map.valid_text(approval) or terms is None):
        raise MasterError("approval_metadata_invalid")
    raw = read_master_input(path, MAX_INPUT_BYTES)
    source_hash = hashlib.sha256(raw).hexdigest()
    if source_hash != expected:
        raise MasterError("source_hash_mismatch")
    csv_bytes = _master_csv(path, pin["member"], raw)
    try:
        text = csv_bytes.decode("cp932", errors="strict")
    except UnicodeDecodeError:
        raise MasterError("csv_encoding_invalid") from None
    reasons, groups, seen, count = Counter(), {}, set(), 0
    future_starts, future_ends = Counter(), Counter()
    try:
        for row in csv.reader(io.StringIO(text, newline=""), strict=True):
            count += 1
            if count > MAX_ROWS:
                raise MasterError("row_bound")
            _check_master_row(row)
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
            # These are dates for this converter's candidate policy, not a
            # claim that a medication is clinically valid. Count each row's
            # effective start/end once; a later start or earlier end wins.
            if (int(medicine) != 0 and (selected is None or medicine in selected)
                    and row[0] in changes and not unknown_date
                    and row[4] and drug_map.fold(row[4])):
                start = max((dates[c] for c in (29, 35) if dates[c] is not None),
                            default=None)
                end = min((dates[c] for c in (30, 33) if dates[c] is not None),
                          default=None)
                if ((end is None or end >= as_of)
                        and (start is None or end is None or start <= end)):
                    if start is not None and start > as_of:
                        future_starts[start.isoformat()] += 1
                    # R08rec1 medicine fields 31/34 include the final day.
                    # date.max has no representable later day to report.
                    if end is not None and end < date.max:
                        future_ends[(end + timedelta(days=1)).isoformat()] += 1
            if int(medicine) == 0:
                reasons["unknown_medicine_identity"] += 1
            elif selected is not None and medicine not in selected:
                reasons["outside_explicit_selection"] += 1
            elif row[0] not in changes:
                reasons["unconfirmed_change_value"] += 1
            elif unknown_date:
                reasons["unconfirmed_date_or_sentinel"] += 1
            elif any(dates[c] is not None and dates[c] < as_of for c in (30, 33)):
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
                    "source_codes": [], "forms": [], "products": []})
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
                entry["products"].append({"medicine": medicine, "name": row[4],
                                          "unit": row[9], "form_code": row[27]})
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
    next_boundary = min(future_starts.keys() | future_ends.keys(), default=None)
    report: dict[str, JSON] = {"schema": "mcs-official-drug-master-report/1", "edition": EDITION,
              "layout": LAYOUT, "source_sha256": source_hash,
              "csv_sha256": hashlib.sha256(csv_bytes).hexdigest(), "rows": count,
              "entries": len(entries), "reasons": dict(sorted(reasons.items())),
              "as_of": as_of.isoformat(), "next_boundary_on": next_boundary,
              "future_starts": dict(sorted(future_starts.items())),
              "future_ends": dict(sorted(future_ends.items())),
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
                  "spec_url": SPEC_URL, "layout": LAYOUT, "status_policy": policy,
                  "as_of": as_of.isoformat(), "next_boundary_on": next_boundary}
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
    published = False
    try:
        with fdopen_owned(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(result.payload)
            stream.flush()
            os.fsync(stream.fileno())
        digest = hashlib.sha256(result.payload).hexdigest()
        if drug_map.load(temporary, expected_sha256=digest) is None:
            raise MasterError("dictionary_unavailable")
        os.link(temporary, destination, follow_symlinks=False)
        published = True
    finally:
        try:
            os.unlink(temporary)
            if published:
                fsync_dir(destination.parent)
        except OSError:
            if published:
                raise MasterError("output_published_sync_unconfirmed", written=True) from None
            raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--pin", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        pin = json.loads(read_master_input(args.pin, 65536))
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
        print(json.dumps({"status": "refused", "reason": code,
                          "written": isinstance(error, MasterError) and error.written}))
        return 1




def catalog(path: Path, pin: dict[str, JSON]) -> dict:
    """Read all pinned official rows, including inactive codes, without creating candidates."""
    edition = pin.get("edition")
    if (not isinstance(edition, str) or not re.fullmatch(r"[0-9]{8}", edition)
            or pin.get("member") != f"y_{edition}.csv"):
        raise MasterError("pin_edition_or_member")
    try:
        edition_day = date(int(edition[:4]), int(edition[4:6]), int(edition[6:]))
    except ValueError:
        raise MasterError("pin_edition_or_member") from None
    as_of = _iso(pin.get("as_of"))
    if as_of < edition_day:
        raise MasterError("pin_edition_or_member")
    # Catalogue comparisons accept an explicit older edition of this exact
    # reviewed layout; candidate conversion remains pinned to EDITION.
    expected, _, _, _, _, _ = _check_pin({**pin, "edition": EDITION,
        "member": f"y_{EDITION}.csv", "as_of": max(as_of, EDITION_DATE).isoformat()})
    raw = read_master_input(path, MAX_INPUT_BYTES)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise MasterError("source_hash_mismatch")
    data = _master_csv(path, pin["member"], raw)
    rows = {}
    try:
        for row in csv.reader(io.StringIO(data.decode("cp932"), newline=""), strict=True):
            if len(rows) >= MAX_ROWS:
                raise MasterError("row_bound")
            _check_master_row(row)
            code = row[2]
            if code in rows:
                raise MasterError("duplicate_medicine_code")
            rows[code] = {"medicine": code, "name": row[4], "unit": row[9],
                          "form_code": row[27], "change": row[0],
                          "abolished_on": row[30], "transition_until": row[33],
                          "general_code": row[36], "general_name": row[37]}
    except (csv.Error, UnicodeDecodeError):
        raise MasterError("csv_parse_invalid") from None
    if not rows:
        raise MasterError("empty_master")
    return {"sha256": expected, "edition": edition, "layout": LAYOUT, "rows": rows,
            "read_only": True, "candidate_only": True}


if __name__ == "__main__":
    raise SystemExit(main())
