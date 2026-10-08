#!/usr/bin/env python3
"""同梱した公式医薬品マスターの出典・容量・SHA・既知形式を無通信で検証する。"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcs"))
import _mcs_path  # noqa: E402,F401
import import_drug_master as importer  # noqa: E402
from mcs_requests import parse_command  # noqa: E402

SCHEMA = "mcs-official-drug-bundle/1"
TERMS_URL = "https://www.mhlw.go.jp/chosakuken/index.html"
DEFAULT = ROOT / "resources/drug-master" / importer.EDITION / "manifest.json"


def check(manifest_path: Path = DEFAULT) -> dict:
    """Inspect public bundle bytes; never create a dictionary, pin or approval."""
    if not manifest_path.is_absolute() or manifest_path.resolve() != manifest_path:
        raise ValueError("bundle_manifest_path")
    manifest = parse_command(importer._read(manifest_path, 65536))
    if (not isinstance(manifest, dict) or set(manifest) != {
            "schema", "edition", "layout", "archive", "member", "archive_bytes",
            "archive_sha256", "csv_bytes", "csv_sha256", "rows", "source",
            "attribution", "packaging", "terms", "activation", "operator_approval_required"}
            or manifest["schema"] != SCHEMA or manifest["edition"] != importer.EDITION
            or manifest["layout"] != importer.LAYOUT
            or manifest["archive"] != f"y_{importer.EDITION}.zip"
            or manifest["member"] != f"y_{importer.EDITION}.csv"
            or manifest["activation"] is not False
            or manifest["operator_approval_required"] is not True):
        raise ValueError("bundle_manifest_contract")
    for key, bound in (("archive_bytes", importer.MAX_INPUT_BYTES),
                       ("csv_bytes", importer.MAX_CSV_BYTES), ("rows", importer.MAX_ROWS)):
        if type(manifest[key]) is not int or not 0 < manifest[key] <= bound:
            raise ValueError("bundle_size_or_rows")
    if any(not isinstance(manifest[key], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest[key])
           for key in ("archive_sha256", "csv_sha256")):
        raise ValueError("bundle_hash_format")
    source, terms, packaging = manifest["source"], manifest["terms"], manifest["packaging"]
    if (not isinstance(source, dict) or set(source) != {"name", "url", "menu", "spec", "status_spec"}
            or not isinstance(source["name"], str) or not source["name"].strip()
            or source["url"] != importer.SOURCE_URL or source["menu"] != importer.MENU_URL
            or source["spec"] != importer.SPEC_URL
            or source["status_spec"] != importer.SPEC_URL.replace("R08rec3.pdf", "R08rec1.pdf")
            or not isinstance(manifest["attribution"], str) or not manifest["attribution"].strip()
            or not isinstance(terms, dict) or set(terms) != {
                "url", "reference_checked_on", "license_reference", "operator_review_required"}
            or terms["url"] != TERMS_URL or terms["operator_review_required"] is not True
            or not isinstance(terms["license_reference"], str) or not terms["license_reference"].strip()
            or not isinstance(packaging, dict) or set(packaging) != {
                "original_bytes", "notice", "introduced_version"}
            or packaging["original_bytes"] is not True
            or not isinstance(packaging["notice"], str) or not packaging["notice"].strip()
            or not isinstance(packaging["introduced_version"], str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", packaging["introduced_version"])):
        raise ValueError("bundle_provenance")
    checked_on = date.fromisoformat(terms["reference_checked_on"])
    archive = manifest_path.parent / manifest["archive"]
    if archive.resolve() != archive:
        raise ValueError("bundle_archive_path")
    raw = importer._read(archive, importer.MAX_INPUT_BYTES)
    if len(raw) != manifest["archive_bytes"] or hashlib.sha256(raw).hexdigest() != manifest["archive_sha256"]:
        raise ValueError("bundle_archive_integrity")
    with zipfile.ZipFile(io.BytesIO(raw)) as zipped:
        members = zipped.infolist()
        if (len(members) != 1 or members[0].filename != manifest["member"]
                or members[0].file_size != manifest["csv_bytes"]):
            raise ValueError("bundle_member_integrity")
    # Missing terms/status declarations deliberately keep converter output held.
    # This still runs the exact importer field/layout checks over every row.
    converted = importer.convert(archive, {
        "schema": importer.PIN_SCHEMA, "layout": importer.LAYOUT,
        "edition": importer.EDITION, "sha256": manifest["archive_sha256"],
        "member": manifest["member"], "as_of": checked_on.isoformat()})
    if (converted.report["rows"] != manifest["rows"]
            or converted.report["csv_sha256"] != manifest["csv_sha256"]
            or converted.payload is not None):
        raise ValueError("bundle_csv_integrity")
    return {"status": "ok", "edition": manifest["edition"], "layout": manifest["layout"],
            "rows": converted.report["rows"], "archive_sha256": manifest["archive_sha256"],
            "csv_sha256": manifest["csv_sha256"], "activation": False,
            "operator_approval_required": True, "conversion_held": converted.report.get("held")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT)
    args = parser.parse_args(argv)
    try:
        report = check(args.manifest)
    except (ValueError, TypeError, OSError, RecursionError, zipfile.BadZipFile):
        print(json.dumps({"status": "refused", "reason": "bundle_invalid", "activation": False}))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
