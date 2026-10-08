"""Bundle integrity uses fictional ZIP rows only; never opens the public master."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sqlite3
import zipfile

import pytest

from test_import_drug_master import _row, _csv

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("drug_bundle_check", ROOT / "scripts/development/check_drug_master_bundle.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def _bundle(tmp_path):
    data = _csv([_row()])
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("y_20260930.csv", data)
    raw = archive.getvalue()
    (tmp_path / "y_20260930.zip").write_bytes(raw)
    manifest = {
        "schema": checker.SCHEMA, "edition": "20260930", "layout": checker.importer.LAYOUT,
        "archive": "y_20260930.zip", "member": "y_20260930.csv",
        "archive_bytes": len(raw), "archive_sha256": hashlib.sha256(raw).hexdigest(),
        "csv_bytes": len(data), "csv_sha256": hashlib.sha256(data).hexdigest(), "rows": 1,
        "source": {"name": "fictional public-source fixture", "url": checker.importer.SOURCE_URL,
            "menu": checker.importer.MENU_URL, "spec": checker.importer.SPEC_URL,
            "status_spec": checker.importer.SPEC_URL.replace("R08rec3.pdf", "R08rec1.pdf")},
        "attribution": "fictional attribution", "packaging": {"original_bytes": True,
            "notice": "fictional unchanged ZIP", "introduced_version": "1.0.16"},
        "terms": {"url": checker.TERMS_URL, "reference_checked_on": "2026-10-06",
            "license_reference": "fictional reference only", "operator_review_required": True},
        "activation": False, "operator_approval_required": True}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path, manifest


def test_bundle_verifies_all_rows_but_keeps_converter_output_unapproved(tmp_path, monkeypatch):
    path, _ = _bundle(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("no bundle network"))
    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: pytest.fail("no bundle DB"))
    report = checker.check(path)
    assert report["status"] == "ok" and report["rows"] == 1
    assert report["activation"] is False and report["operator_approval_required"] is True
    assert report["conversion_held"] == "terms_unconfirmed"
    assert "架空アオ" not in json.dumps(report)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("damage", ["archive_hash", "csv_hash", "archive_size", "csv_size", "rows",
                                    "edition", "layout", "member", "traversal", "source", "approval"])
def test_bundle_damage_unknown_edition_and_unsafe_paths_are_refused(tmp_path, damage):
    path, manifest = _bundle(tmp_path)
    damaged = deepcopy(manifest)
    if damage in ("archive_hash", "csv_hash"):
        damaged[damage.replace("hash", "sha256")] = "0" * 64
    elif damage in ("archive_size", "csv_size"):
        damaged[damage.replace("size", "bytes")] += 1
    elif damage == "rows":
        damaged["rows"] = True
    elif damage == "edition":
        damaged["edition"] = "20261001"
    elif damage == "layout":
        damaged["layout"] = "future-43"
    elif damage == "member":
        damaged["member"] = "../y_20260930.csv"
    elif damage == "traversal":
        damaged["archive"] = "../y_20260930.zip"
    elif damage == "source":
        damaged["source"]["url"] = "https://example.invalid/master"
    else:
        damaged["terms"]["approved_by"] = "must-not-be-an-operator-approval"
    path.write_text(json.dumps(damaged))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError):
        checker.check(path)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("target", ["manifest", "archive"])
def test_bundle_symlinks_never_followed(tmp_path, target):
    path, _ = _bundle(tmp_path)
    source = path if target == "manifest" else tmp_path / "y_20260930.zip"
    renamed = source.with_name(source.name + ".saved")
    source.rename(renamed)
    source.symlink_to(renamed)
    with pytest.raises(ValueError):
        checker.check(path)
    assert source.is_symlink()


def test_bundle_cli_failure_reports_no_paths_or_contents(tmp_path, capsys):
    path, manifest = _bundle(tmp_path)
    manifest["archive_sha256"] = "0" * 64
    path.write_text(json.dumps(manifest))
    assert checker.main(["--manifest", str(path)]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["reason"] == "bundle_invalid"
    assert str(tmp_path) not in output and "架空アオ" not in output
