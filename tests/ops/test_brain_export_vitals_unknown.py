"""Synthetic snapshot coverage for unavailable vital verification settings."""
import json
import sqlite3

import pytest

import brain_export
import inference_version
import rollup
from test_brain_export import SECRET_BODY, _rollup, _snapshot


@pytest.mark.parametrize("current", [False, True])
def test_export_discloses_unknown_without_restoring_cached_vitals(tmp_path, current, monkeypatch, capsys):
    snapshot = _snapshot(tmp_path)
    _rollup(snapshot)
    with sqlite3.connect(snapshot) as db:
        content = json.loads(db.execute(
            "SELECT content FROM artifacts WHERE kind='patient_rollup'").fetchone()[0])
        content["latest_vitals"] = {"at": "2026-09-19", "bp": "137/79"}
        meta = {}
        if current:
            content[inference_version.VERSION_KEY] = inference_version.CURRENT_VERSION
            meta = {inference_version.VERSION_KEY: inference_version.CURRENT_VERSION,
                    "period_check_version": rollup.PERIOD_CHECK_VERSION,
                    "projection_version": rollup.PROJECTION_VERSION}
        db.execute("UPDATE artifacts SET content=?,meta=? WHERE kind='patient_rollup'",
                   (json.dumps(content), json.dumps(meta)))
    before = snapshot.read_bytes()
    out = tmp_path / "export"
    baseline = tmp_path / "baseline"
    render = brain_export._patient_md
    with monkeypatch.context() as patch:
        patch.setattr(brain_export, "_patient_md", lambda pid, name, info, roll, **_kw:
                      render(pid, name, info, roll))
        assert brain_export.run(baseline, snapshot)["ok"]
    assert brain_export.main(["--out", str(out), "--snapshot", str(snapshot)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    page = (out / "patients" / "p1.md").read_text()
    if current:
        assert "## 患者集約: 未確認" in page
        assert "記録の現行性を確認できません。" in page
    else:
        assert "## バイタル: 未確認" in page
        assert "設定と記録の現行性を確認できません。" in page
    assert "137/79" not in page and SECRET_BODY not in page
    assert snapshot.read_bytes() == before
    records = (out / "export.jsonl").read_bytes()
    assert records == (baseline / "export.jsonl").read_bytes()
    parsed = [json.loads(line) for line in records.splitlines()]
    assert parsed and all(record["contract"] == "mcs-read-model/1"
                          and record["snapshot_generation_id"] == "gen-1" for record in parsed)
    assert SECRET_BODY.encode() not in records
    assert brain_export.run(out, snapshot)["ok"]
    assert (out / "patients" / "p1.md").read_text() == page
    assert (out / "export.jsonl").read_bytes() == records
    assert snapshot.read_bytes() == before


@pytest.mark.parametrize(("value", "line"), [
    ({"bp": "137/79"}, "- bp: 137/79"),
    ({"temperature": 0}, "- temperature: 0"),
])
def test_confirmed_vitals_keep_the_existing_rendering(value, line):
    page = brain_export._patient_md(1, "合成患者", {}, {
        "latest_vitals": {"at": "2026-09-19", **value}},
        measurement_config_unavailable=True)
    assert "## latest vitals (2026-09-19)" in page
    assert line in page
    assert "バイタル: 未確認" not in page


def test_unknown_notice_is_scoped_to_patients_with_rollups(tmp_path):
    snapshot = _snapshot(tmp_path)
    _rollup(snapshot)
    with sqlite3.connect(snapshot) as db:
        db.execute("INSERT INTO patients SELECT 2,project_type,?,disease,station_name,url,"
                   "fetch_state,fetch_reason,last_complete_fetch,last_seen,is_archived,created_at "
                   "FROM patients WHERE project_id=1", ("未集約の合成患者",))
    before = snapshot.read_bytes()
    out = tmp_path / "export"
    result = brain_export.run(out, snapshot)
    pages = sorted((out / "patients").glob("*.md"))
    assert result["patients"] == 1 and [page.name for page in pages] == ["p1.md"]
    text = pages[0].read_text()
    assert "## 患者集約: 未確認" in text
    assert "記録の現行性を確認できません。" in text
    assert "未集約の合成患者" not in text
    assert not (out / "patients" / "p2.md").exists()
    assert snapshot.read_bytes() == before
