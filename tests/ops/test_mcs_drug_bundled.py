"""Bundled-master install/status contracts over a fictional bundle; no network or live DB."""
from datetime import date, timedelta
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

import drug_map
import import_drug_master as importer
import mcs_drug
from test_import_drug_master import _csv, _row

ROOT = Path(__file__).resolve().parents[2]
NAMES = ["--terms-checked-on", "2026-10-04", "--confirmed-by", "fictional-operator"]


def _bundle(tmp_path, rows=None, *, manifest_csv_sha=None):
    data = _csv(rows or [_row(), _row("900000002", "架空ミドリ錠５ｍｇ", kana="ｶｸｳﾐﾄﾞﾘ")])
    folder = tmp_path / "bundle" / importer.EDITION
    folder.mkdir(parents=True)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
        stream.writestr(f"y_{importer.EDITION}.csv", data)
    (folder / f"y_{importer.EDITION}.zip").write_bytes(archive.getvalue())
    (folder / "manifest.json").write_text(json.dumps({
        "edition": importer.EDITION, "layout": importer.LAYOUT,
        "archive": f"y_{importer.EDITION}.zip", "member": f"y_{importer.EDITION}.csv",
        "archive_sha256": hashlib.sha256(archive.getvalue()).hexdigest(),
        "csv_sha256": manifest_csv_sha or hashlib.sha256(data).hexdigest(),
        "rows": len(rows or [0, 0]),
        "terms": {"url": "https://example.invalid/terms"}}))
    return tmp_path / "bundle"


def _main(tmp_path, monkeypatch, bundle, argv, capsys, config=None):
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mcs_drug, "BUNDLE", bundle)
    monkeypatch.setattr(mcs_drug, "HOME", str(home))
    monkeypatch.setattr(mcs_drug, "load_config", lambda: config or {})
    code = mcs_drug.main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out), home


def test_install_writes_private_dictionary_without_touching_config(tmp_path, monkeypatch, capsys):
    bundle = _bundle(tmp_path)
    code, report, home = _main(tmp_path, monkeypatch, bundle,
                               ["install-bundled", *NAMES, "--approved-by", "fictional-operator"],
                               capsys)
    assert code == 0 and report["state"] == "written" and report["approved"]
    path = Path(report["path"])
    assert path.parent == (home / "data" / "drug-map").resolve()
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    dictionary = drug_map.load(path, expected_sha256=report["sha256"])
    assert dictionary.approved and report["entries"] == 1   # both rows share a general code
    assert not (home / "config.json").exists()
    # A rerun with the same operator inputs is idempotent, not an overwrite.
    code, again, _ = _main(tmp_path, monkeypatch, bundle,
                           ["install-bundled", *NAMES, "--approved-by", "fictional-operator"], capsys)
    assert code == 0 and again["state"] == "present" and again["sha256"] == report["sha256"]


def test_without_approval_the_dictionary_is_reference_only(tmp_path, monkeypatch, capsys):
    code, report, _ = _main(tmp_path, monkeypatch, _bundle(tmp_path),
                            ["install-bundled", *NAMES], capsys)
    dictionary = drug_map.load(report["path"], expected_sha256=report["sha256"])
    assert code == 0 and not report["approved"] and not dictionary.approved


def test_policy_keeps_deleted_rows_out_and_accepts_documented_sentinels(tmp_path, monkeypatch, capsys):
    deleted = _row("900000002", "架空アカ錠５ｍｇ", "GEN000000002", "【般】架空アカ５ｍｇ錠", "ｶｸｳｱｶ")
    deleted[0] = "1"
    kept = _row("900000003", "架空キ錠５ｍｇ", "GEN000000003", "【般】架空キ５ｍｇ錠", "ｶｸｳｷ")
    kept[30] = "99999999"
    code, report, _ = _main(tmp_path, monkeypatch, _bundle(tmp_path, [_row(), deleted, kept]),
                            ["install-bundled", *NAMES], capsys)
    assert code == 0 and report["entries"] == 2
    assert report["reasons"]["unconfirmed_change_value"] == 1


@pytest.mark.parametrize("missing", ["--terms-checked-on", "--confirmed-by"])
def test_operator_inputs_are_required(tmp_path, monkeypatch, missing):
    argv = list(NAMES)
    del argv[argv.index(missing):argv.index(missing) + 2]
    monkeypatch.setattr(mcs_drug, "BUNDLE", _bundle(tmp_path))
    with pytest.raises(SystemExit):
        mcs_drug.main(["install-bundled", *argv])


@pytest.mark.parametrize("damage", ["archive", "csv_record", "terms_date"])
def test_tampered_bundle_or_bad_input_writes_nothing(tmp_path, monkeypatch, capsys, damage):
    bundle = _bundle(tmp_path, manifest_csv_sha="0" * 64 if damage == "csv_record" else None)
    if damage == "archive":
        zipped = bundle / importer.EDITION / f"y_{importer.EDITION}.zip"
        zipped.write_bytes(zipped.read_bytes() + b"\0")
    argv = ["install-bundled", "--terms-checked-on",
            "not-a-date" if damage == "terms_date" else "2026-10-04",
            "--confirmed-by", "fictional-operator"]
    code, report, home = _main(tmp_path, monkeypatch, bundle, argv, capsys)
    assert code == 1 and report["status"] == "refused" and not report["written"]
    assert not (home / "data" / "drug-map").exists()


def test_status_compares_config_with_the_bundle(tmp_path, monkeypatch, capsys):
    bundle = _bundle(tmp_path)
    code, unset, _ = _main(tmp_path, monkeypatch, bundle, ["status"], capsys)
    assert code == 0 and unset["state"] == "unconfigured" and unset["configured"] is None
    _, made, _ = _main(tmp_path, monkeypatch, bundle, ["install-bundled", *NAMES], capsys)
    config = {"drug_map": {"path": made["path"], "sha256": made["sha256"]}}
    _, current, _ = _main(tmp_path, monkeypatch, bundle, ["status"], capsys, config)
    assert current["state"] == "bundled_reference_only"
    assert current["configured"]["id"] == current["bundled"]["dict_id"]
    other = _bundle(tmp_path / "newer", [_row("900000009", "架空シロ錠５ｍｇ", kana="ｶｸｳｼﾛ")])
    _, stale, _ = _main(tmp_path, monkeypatch, other, ["status"], capsys, config)
    assert stale["state"] == "differs_from_bundle"
    gone = {"drug_map": {"path": str(tmp_path / "absent.json"), "sha256": "a" * 64}}
    assert _main(tmp_path, monkeypatch, bundle, ["status"], capsys, gone)[1]["state"] == "missing"


def test_shipped_bundle_converts_every_row(tmp_path, monkeypatch, capsys):
    """The released public master itself goes through the runtime converter."""
    manifest = json.loads((ROOT / "resources/drug-master" / importer.EDITION
                           / "manifest.json").read_text())
    code, report, _ = _main(tmp_path, monkeypatch, ROOT / "resources/drug-master",
                            ["install-bundled", *NAMES], capsys)
    assert code == 0 and report["state"] == "written"
    assert report["rows"] == manifest["rows"] and report["entries"] > 0
    assert drug_map.load(report["path"], expected_sha256=report["sha256"]) is not None


@pytest.mark.parametrize("edit", [
    lambda m: m.pop("archive_sha256"),
    lambda m: m.update(csv_sha256="Z" * 64),
    lambda m: m.update(rows=True),
    lambda m: m.update(rows=0),
    lambda m: m.update(rows="2"),
    lambda m: m.update(terms={"url": 1}),
    "deep",
], ids=["missing_sha", "bad_csv_sha", "bool_rows", "zero_rows", "text_rows", "bad_terms", "deep_json"])
def test_malformed_manifest_is_refused_and_status_unavailable(tmp_path, monkeypatch, capsys, edit):
    bundle = _bundle(tmp_path)
    path = bundle / importer.EDITION / "manifest.json"
    if edit == "deep":
        path.write_text("[" * 5000 + "]" * 5000)
    else:
        manifest = json.loads(path.read_text())
        edit(manifest)
        path.write_text(json.dumps(manifest))
    code, report, home = _main(tmp_path, monkeypatch, bundle, ["install-bundled", *NAMES], capsys)
    assert code == 1 and report == {"status": "refused", "reason": "bundle_manifest", "written": False}
    assert not (home / "data" / "drug-map").exists()
    assert _main(tmp_path, monkeypatch, bundle, ["status"], capsys)[1]["state"] == "bundle_unavailable"


def test_printed_setup_command_is_one_shell_argument(capsys):
    import shlex
    path = "/tmp/fictional dir/it's $(touch x) `y`/mhlw.json"
    mcs_drug._bundled_display("install-bundled", {
        "edition": importer.EDITION, "rows": 1, "entries": 1, "approved": True,
        "held": None, "state": "written", "path": path, "sha256": "a" * 64})
    line = [row for row in capsys.readouterr().out.splitlines() if "mcs_setup.py" in row][0]
    argv = shlex.split(line)
    assert argv[:4] == ["python3", "mcs/ops/mcs_setup.py", "init", "--set"] and len(argv) == 5
    key, value = argv[4].split("=", 1)
    assert key == "drug_map" and json.loads(value) == {"path": path, "sha256": "a" * 64}


def _future_rows():
    # a row whose listing starts after today and a current row that ends later
    today = date.today()
    start = (today + timedelta(days=30)).strftime("%Y%m%d")
    end = (today + timedelta(days=60)).strftime("%Y%m%d")
    upcoming = _row("900000004", "架空ムラサキ錠５ｍｇ", "GEN000000004", "【般】架空ムラサキ５ｍｇ錠", "ｶｸｳﾑﾗｻｷ")
    upcoming[29] = start
    ending = _row("900000005", "架空チャ錠５ｍｇ", "GEN000000005", "【般】架空チャ５ｍｇ錠", "ｶｸｳﾁｬ")
    ending[30] = end
    return [_row(), upcoming, ending], date.fromisoformat(f"{start[:4]}-{start[4:6]}-{start[6:]}")


def test_install_reports_later_starts_and_when_to_rebuild(tmp_path, monkeypatch, capsys):
    rows, boundary = _future_rows()
    bundle = _bundle(tmp_path, rows)
    code, report, _ = _main(tmp_path, monkeypatch, bundle, ["install-bundled", *NAMES], capsys)
    assert code == 0 and report["entries"] == 2       # the later-listed row is not a candidate yet
    assert report["as_of"] == date.today().isoformat()
    assert report["next_boundary_on"] == boundary.isoformat()
    assert report["future_starts"] == {boundary.isoformat(): 1}
    assert sum(report["future_ends"].values()) == 1
    monkeypatch.setattr(mcs_drug, "load_config", lambda: {})
    assert mcs_drug.main(["install-bundled", *NAMES]) == 0
    text = capsys.readouterr().out
    assert f"1行は {boundary.isoformat()} から候補になります" in text
    assert f"次の見直し日 {boundary.isoformat()} 以降に install-bundled で作り直してください" in text


def test_status_asks_for_a_rebuild_once_a_boundary_has_passed(tmp_path, monkeypatch, capsys):
    rows, boundary = _future_rows()
    bundle = _bundle(tmp_path, rows)
    _, made, _ = _main(tmp_path, monkeypatch, bundle, ["install-bundled", *NAMES], capsys)
    config = {"drug_map": {"path": made["path"], "sha256": made["sha256"]}}
    before = mcs_drug.bundled_status(config, directory=bundle)
    assert before["state"] == "bundled_reference_only"
    assert before["configured"]["next_boundary_on"] == boundary.isoformat()
    after = mcs_drug.bundled_status(config, directory=bundle, today=boundary)
    assert after["state"] == "rebuild_due" and "install-bundled" in after["explanation"]


def _rewrite(path, home, edit):
    document = json.loads(Path(path).read_text())
    edit(document["source"])
    raw = json.dumps(document, ensure_ascii=False).encode()
    out = home / "data" / "drug-map" / "edited.json"
    out.write_bytes(raw)
    out.chmod(0o600)
    return {"drug_map": {"path": str(out), "sha256": hashlib.sha256(raw).hexdigest()}}


@pytest.mark.parametrize("edit", [
    lambda s: (s.pop("as_of"), s.pop("next_boundary_on")),   # dictionary built before 1.0.21
    lambda s: s.update(as_of=(date.today() + timedelta(days=3)).isoformat(),
                       next_boundary_on=None),                 # dated after today
], ids=["no_build_date", "future_build_date"])
def test_unknown_or_future_build_dates_are_never_reported_as_current(tmp_path, monkeypatch, capsys, edit):
    bundle = _bundle(tmp_path)
    _, made, home = _main(tmp_path, monkeypatch, bundle, ["install-bundled", *NAMES], capsys)
    config = _rewrite(made["path"], home, edit)
    report = mcs_drug.bundled_status(config, directory=bundle)
    assert report["state"] == "build_date_unknown", report
    assert report["configured"]["id"] == report["bundled"]["dict_id"]


# Expected lines are built at run time: a collection-time date breaks
# when the suite crosses midnight before this test runs.
@pytest.mark.parametrize("edit, line", [
    (lambda s: (s.pop("as_of"), s.pop("next_boundary_on")),
     lambda: "作成基準日: 記録なし / 次の見直し日: 未確認"),
    (lambda s: s.update(as_of=(date.today() + timedelta(days=3)).isoformat(), next_boundary_on=None),
     lambda: f"作成基準日: {(date.today() + timedelta(days=3)).isoformat()} / 次の見直し日: 未確認"),
    (lambda s: s.update(next_boundary_on=None),
     lambda: f"作成基準日: {date.today().isoformat()} / 次の見直し日: なし"),
], ids=["legacy", "future_build_date", "verified_no_boundary"])
def test_status_text_separates_an_unknown_boundary_from_none(tmp_path, monkeypatch, capsys, edit, line):
    bundle = _bundle(tmp_path)
    _, made, home = _main(tmp_path, monkeypatch, bundle, ["install-bundled", *NAMES], capsys)
    config = _rewrite(made["path"], home, edit)
    monkeypatch.setattr(mcs_drug, "load_config", lambda: config)
    assert mcs_drug.main(["status"]) == 0
    assert line() in capsys.readouterr().out
