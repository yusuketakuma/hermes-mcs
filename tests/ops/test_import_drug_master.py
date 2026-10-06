"""Wholly fictional cp932 42-column masters; no real master rows or services."""
from copy import deepcopy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

import pytest

import drug_map
import import_drug_master as importer


def _row(code="900000001", name="架空アオ錠５ｍｇ", general="GEN000000001",
         general_text="【般】架空アオ５ｍｇ錠", kana="ｶｸｳｱｵ"):
    row = ["0"] * 42
    for column in importer.TEXT_COLUMNS:
        row[column] = ""
    row[1], row[2], row[4], row[6] = "Y", code, name, kana
    row[3], row[5] = str(len(name)), str(len(kana))
    row[7], row[8], row[9], row[11] = "1", "1", "錠", "12.00"
    row[29], row[30], row[31], row[33] = "20260901", "0", "DRG000000001", "0"
    row[34], row[35], row[36], row[37] = name, "20260401", general, general_text
    return row


def _csv(rows):
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\r\n").writerows(rows)
    return stream.getvalue().encode("cp932")


def _input(tmp_path, rows=None, *, zipped=True, raw=None, members=None):
    data = raw if raw is not None else _csv(rows or [_row()])
    if zipped:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
            for name, body in members or [(f"y_{importer.EDITION}.csv", data)]:
                stream.writestr(name, body)
        data = archive.getvalue()
    path = tmp_path / ("synthetic.zip" if zipped else f"y_{importer.EDITION}.csv")
    path.write_bytes(data)
    pin = {"schema": importer.PIN_SCHEMA, "layout": importer.LAYOUT,
           "edition": importer.EDITION, "member": f"y_{importer.EDITION}.csv",
           "as_of": "2026-10-04", "sha256": hashlib.sha256(data).hexdigest(),
           "terms_checked_on": "2026-10-04", "terms_record": "fictional-test-receipt",
           "status_policy": {"candidate_change_values": ["0", "1", "2"],
                             "absent_date_values": ["0", "99999999"],
                             "confirmed_by": "fictional-test-only"}}
    return path, pin


def _write_result(tmp_path, result):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "dictionary.json"
    importer.write_private(result, path)
    dictionary = drug_map.load(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert dictionary is not None
    return path, dictionary


def test_explicit_general_identity_groups_provenance_not_numeric_prefixes(tmp_path):
    # Given
    first = _row()
    second = _row("900000002", "架空ミドリ錠５ｍｇ", kana="ｶｸｳﾐﾄﾞﾘ")
    product = _row("900000003", "架空ムラサキ錠５ｍｇ", general="", general_text="", kana="ｶｸｳﾑﾗｻｷ")
    path, pin = _input(tmp_path, [first, second, product])
    # When
    result = importer.convert(path, pin)
    target, dictionary = _write_result(tmp_path, result)
    # Then
    document = json.loads(target.read_text())
    assert document["schema"] == drug_map.SOURCE_SCHEMA
    assert result.report["entries"] == 2 and result.report["reasons"]["candidate_rows"] == 3
    general = next(e for e in document["entries"] if e["kind"] == "general_name")
    assert general["codes"] == {"general_name": "GEN000000001"}
    assert general["source_codes"] == [
        {"medicine": "900000001", "drug_price": "DRG000000001", "general_name": "GEN000000001"},
        {"medicine": "900000002", "drug_price": "DRG000000001", "general_name": "GEN000000001"}]
    assert dictionary.resolve("架空ミドリ錠５ｍｇ")["cands"][0]["kind"] == "general_name"
    assert dictionary.resolve("架空ムラサキ錠５ｍｇ")["cands"][0]["kind"] == "product"
    assert not dictionary.approved
    assert not any(e["kind"] == "ingredient" for e in document["entries"])
    assert document["source"]["master_sha256"] == pin["sha256"]


def test_unicode_exact_aliases_collisions_and_max_kana_do_not_create_stems(tmp_path):
    # Given
    a = _row(name="共通架空５ｍｇ", kana="ｶｸｳ" * 5 + "ｶｸｳｱｵ")
    assert len(a[6]) == 20
    b = _row("900000002", "共通架空５ｍｇ", "GEN000000002", "【般】別架空５ｍｇ", "ﾍﾞﾂｶｸｳ")
    path, pin = _input(tmp_path, [a, b])
    # When
    result = importer.convert(path, pin)
    _, dictionary = _write_result(tmp_path, result)
    # Then
    assert dictionary.resolve("共通架空5mg")["status"] == "ambiguous"
    assert len(dictionary.resolve("共通架空5mg")["cands"]) == 2
    assert dictionary.resolve(a[6])["status"] == "unresolved"
    assert dictionary.resolve("べつかくう")["cands"][0]["kind"] == "general_name"
    assert dictionary.resolve("共通架空")["status"] == "unresolved"
    assert result.report["reasons"]["max_length_kana_excluded"] == 1


@pytest.mark.parametrize("damage", [
    "hash", "edition_old", "edition_future", "layout", "pin_schema", "member",
    "extra_column", "header", "type", "price", "declared_length", "width",
    "fullwidth_kana", "duplicate", "encoding", "selection", "approval", "terms",
])
def test_pin_layout_types_and_metadata_fail_before_any_output(tmp_path, damage):
    # Given
    row = _row()
    rows, raw = [row], None
    if damage == "extra_column":
        row.append("0")
    elif damage == "header":
        row[0] = "変更区分"
    elif damage == "type":
        row[7] = "not_numeric"
    elif damage == "price":
        row[11] = "-1.0"
    elif damage == "declared_length":
        row[3] = "1"
    elif damage == "width":
        row[34] = "架" * 101
    elif damage == "fullwidth_kana":
        row[6], row[5] = "カクウ", "3"
    elif damage == "duplicate":
        rows.append(deepcopy(row))
    elif damage == "encoding":
        raw = b"\x81"
    path, pin = _input(tmp_path, rows, raw=raw)
    if damage == "hash":
        pin["sha256"] = "0" * 64
    elif damage == "edition_old":
        pin["edition"] = "20250331"
    elif damage == "edition_future":
        pin["edition"] = "20261001"
    elif damage == "layout":
        pin["layout"] = "future-43"
    elif damage == "pin_schema":
        pin["schema"] = "future/2"
    elif damage == "member":
        pin["member"] = "../y_20260930.csv"
    elif damage == "selection":
        pin["medicine_codes"] = ["900000099"]
    elif damage == "approval":
        pin["approved_by"] = "fictional"
        del pin["terms_checked_on"]
    elif damage == "terms":
        pin["terms_checked_on"] = "2026-10-05"
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    # When / Then
    with pytest.raises(importer.MasterError):
        importer.convert(path, pin)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("members", [
    [("../y_20260930.csv", b"synthetic")],
    [("y_20260930.csv", b"synthetic"), ("extra.txt", b"synthetic")],
])
def test_zip_members_are_bounded_and_never_extracted(tmp_path, members):
    # Given / When / Then
    path, pin = _input(tmp_path, members=members)
    with pytest.raises(importer.MasterError, match="zip_members"):
        importer.convert(path, pin)
    assert set(p.name for p in tmp_path.iterdir()) == {"synthetic.zip"}


@pytest.mark.parametrize("bound", ["input", "csv", "rows", "dictionary"])
def test_byte_row_and_loader_bounds_are_not_silently_truncated(tmp_path, monkeypatch, bound):
    # Given
    path, pin = _input(tmp_path)
    if bound == "input":
        monkeypatch.setattr(importer, "MAX_INPUT_BYTES", 1)
    elif bound == "csv":
        monkeypatch.setattr(importer, "MAX_CSV_BYTES", 1)
    elif bound == "rows":
        monkeypatch.setattr(importer, "MAX_ROWS", 0)
    else:
        monkeypatch.setattr(drug_map, "MAX_BYTES", 1)
    # When / Then
    if bound == "dictionary":
        result = importer.convert(path, pin)
        assert result.payload is None and result.report["held"] == "dictionary_byte_bound"
    else:
        with pytest.raises(importer.MasterError):
            importer.convert(path, pin)


def test_unknown_policy_dates_and_ended_records_are_honestly_held(tmp_path):
    # Given
    ended = _row("900000002")
    ended[30] = "20260929"
    unknown = _row("900000003")
    unknown[33] = "99999998"
    zero = _row("0")
    path, pin = _input(tmp_path, [_row(), ended, unknown, zero])
    # When
    result = importer.convert(path, pin)
    # Then
    assert result.report["reasons"] == {
        "candidate_rows": 1, "ended_or_end_boundary": 1,
        "unconfirmed_date_or_sentinel": 1, "unknown_medicine_identity": 1}
    unconfirmed = deepcopy(pin)
    del unconfirmed["status_policy"]
    report = importer.convert(path, unconfirmed)
    assert report.payload is None and report.report["held"] == "no_candidate_entries"


def test_unknown_general_code_falls_back_to_product_and_explicit_selection(tmp_path):
    # Given
    product = _row("900000001", general="000000000000")
    path, pin = _input(tmp_path, [product, _row("900000002")], zipped=False)
    pin["medicine_codes"] = ["900000001"]
    # When
    result = importer.convert(path, pin)
    _, dictionary = _write_result(tmp_path, result)
    # Then
    assert result.report["reasons"]["outside_explicit_selection"] == 1
    assert dictionary.resolve(product[4])["cands"][0]["code"] == "mhlw:medicine:900000001"
    assert dictionary.resolve(product[4])["cands"][0]["kind"] == "product"


def test_private_output_is_owned_0600_new_and_preserves_existing_or_symlink(tmp_path):
    # Given
    path, pin = _input(tmp_path)
    result = importer.convert(path, pin)
    # When
    output, _ = _write_result(tmp_path, result)
    # Then
    info = output.stat()
    assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.getuid()
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        importer.write_private(result, output)
    link = output.parent / "linked.json"
    link.symlink_to(output)
    with pytest.raises(FileExistsError):
        importer.write_private(result, link)
    assert output.read_bytes() == before and link.is_symlink()
    assert sorted(p.name for p in output.parent.iterdir()) == ["dictionary.json", "linked.json"]


def test_shared_directory_and_unconfirmed_terms_never_receive_output(tmp_path):
    # Given
    path, pin = _input(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    result = importer.convert(path, pin)
    # When / Then
    with pytest.raises(importer.MasterError, match="private_output_directory"):
        importer.write_private(result, shared / "dictionary.json")
    del pin["terms_checked_on"]
    held = importer.convert(path, pin)
    assert held.payload is None and held.report["held"] == "terms_unconfirmed"
    with pytest.raises(importer.MasterError, match="output_held"):
        importer.write_private(held, shared / "dictionary.json")
    assert list(shared.iterdir()) == []


@pytest.mark.parametrize("mode", ["default", "dry_with_output", "write", "refused"])
def test_real_cli_report_only_and_explicit_private_output(tmp_path, mode):
    # Given
    path, pin = _input(tmp_path)
    if mode == "refused":
        pin["sha256"] = "0" * 64
    pin_path = tmp_path / "pin.json"
    pin_path.write_text(json.dumps(pin))
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "dictionary.json"
    assert importer.__file__ is not None
    command = [sys.executable, str(Path(importer.__file__)), "--source", str(path), "--pin", str(pin_path)]
    if mode != "default":
        command += ["--output", str(output)]
    if mode == "dry_with_output":
        command.append("--dry-run")
    source_before = path.read_bytes()
    # When
    completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
    # Then
    report = json.loads(completed.stdout)
    assert completed.returncode == (1 if mode == "refused" else 0), completed.stderr
    assert report["written"] is (mode == "write")
    assert output.exists() is (mode == "write")
    assert "架空アオ" not in completed.stdout and "GEN000000001" not in completed.stdout
    assert path.read_bytes() == source_before
    if output.exists():
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        assert hashlib.sha256(output.read_bytes()).hexdigest() == report["output_sha256"]


def test_output_owner_mismatch_and_symlink_parent_do_not_mutate(tmp_path, monkeypatch):
    # Given
    path, pin = _input(tmp_path)
    result = importer.convert(path, pin)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(private, target_is_directory=True)
    # When / Then
    with pytest.raises(importer.MasterError, match="private_output_path"):
        importer.write_private(result, linked / "dictionary.json")
    uid = os.getuid()
    monkeypatch.setattr(importer.os, "getuid", lambda: uid + 1)
    with pytest.raises(importer.MasterError, match="private_output_directory"):
        importer.write_private(result, private / "dictionary.json")
    assert list(private.iterdir()) == []


def test_source_symlink_is_not_followed(tmp_path):
    # Given
    path, pin = _input(tmp_path)
    link = tmp_path / "linked.zip"
    link.symlink_to(path)
    before = path.read_bytes()
    # When / Then
    with pytest.raises(OSError):
        importer.convert(link, pin)
    assert path.read_bytes() == before


def test_private_output_mode_is_exact_under_restrictive_umask(tmp_path):
    # Given
    path, pin = _input(tmp_path)
    result = importer.convert(path, pin)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "dictionary.json"
    previous = os.umask(0o777)
    try:
        # When
        importer.write_private(result, output)
    finally:
        os.umask(previous)
    # Then
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_same_general_code_with_conflicting_text_is_refused_not_guessed(tmp_path):
    # Given
    path, pin = _input(tmp_path, [_row(), _row(
        "900000002", general_text="【般】別物の架空処方１０ｍｇ")])
    # When / Then
    with pytest.raises(importer.MasterError, match="source_identity_text_conflict"):
        importer.convert(path, pin)


def test_blank_general_and_basic_names_do_not_become_false_identities(tmp_path):
    # Given
    row = _row(general_text="　")
    row[34] = "　"
    path, pin = _input(tmp_path, [row])
    # When
    result = importer.convert(path, pin)
    _, dictionary = _write_result(tmp_path, result)
    # Then
    assert dictionary.resolve(row[4])["cands"][0]["kind"] == "product"
    assert dictionary.resolve("　")["status"] == "unresolved"


def test_whole_synthetic_master_exceeds_old_limits_without_truncation(tmp_path):
    rows = [_row(str(900000001 + i), name=f"架空製品{i:05d}錠５ｍｇ",
                 general="", general_text="", kana="") for i in range(19272)]
    path, pin = _input(tmp_path, rows)
    result = importer.convert(path, pin)
    assert result.report["rows"] == result.report["entries"] == 19272
    assert result.report["selection"] == "whole_master"
    assert result.payload is not None
    assert 4 * 1024 * 1024 < len(result.payload) <= drug_map.MAX_BYTES
    _, dictionary = _write_result(tmp_path, result)
    assert dictionary.lookup("架空製品19271錠5mg")["cands"][0]["kind"] == "product"
    assert dictionary.lookup("架空製品19271")["status"] == "unresolved"
    assert dictionary.approved is False
    assert result.report["activation"] is False


def test_whole_master_entry_cap_holds_instead_of_truncating(tmp_path, monkeypatch):
    path, pin = _input(tmp_path, [_row("900000001", general="", general_text=""),
                                  _row("900000002", general="", general_text="")])
    monkeypatch.setattr(drug_map, "MAX_ENTRIES", 1)
    result = importer.convert(path, pin)
    assert result.payload is None
    assert result.report["entries"] == 2
    assert result.report["held"] == "dictionary_entry_or_alias_bound"


def test_numeric_reserved_column_accepts_public_format_decimal_without_price_semantics(tmp_path):
    rows = []
    for i in range(99):
        row = _row(str(900000001 + i))
        row[24] = "12.75"  # wholly fictional unused reserve; no monetary interpretation
        rows.append(row)
    path, pin = _input(tmp_path, rows)
    result = importer.convert(path, pin)
    assert result.report["rows"] == 99 and result.report["reasons"]["candidate_rows"] == 99
    assert result.payload is not None
    assert "12.75" not in result.payload.decode()


@pytest.mark.parametrize("value", ["-1.25", "NaN", "1e2", "+12", "1.", "", "12345678901234"])
def test_numeric_reserve_rejects_non_numeric_and_overwide_values(tmp_path, value):
    row = _row()
    row[24] = value
    path, pin = _input(tmp_path, [row])
    with pytest.raises(importer.MasterError):
        importer.convert(path, pin)


def test_decimal_is_still_rejected_in_nonreserve_integer_columns(tmp_path):
    row = _row()
    row[20] = "1.25"
    path, pin = _input(tmp_path, [row])
    with pytest.raises(importer.MasterError, match="csv_numeric_invalid"):
        importer.convert(path, pin)
