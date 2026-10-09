"""Synthetic regressions for extraction snapshots, evidence and input budgets."""

import json
from datetime import datetime
import os
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import extract
import extract_bench
import extract_llm
import rollup
import structured_view
from semantic_projection import PROJECTION_VERSION
from extract_testkit import _hash, _ledger, _message


@pytest.fixture
def db(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.ensure_patient(1)
    yield ledger
    ledger.close()


@pytest.mark.parametrize("change", ["revived", "succeeded", "deleted"])
def test_revive_counts_only_unchanged_error_snapshots(db, change):
    now = time.time()
    db.save_messages([_message(mid=1), _message(mid=2)])
    for mid in (1, 2):
        extract_llm._fail(db, {"project_id": 1, "message_id": mid,
                              "content_hash": _hash(db, mid)}, 4)
    with db.db:
        db.db.execute("UPDATE artifacts SET created_at=? WHERE kind='extract_llm'",
                      (now - extract_llm.REVIVE_COOLDOWN_S - 1,))
    aid, original = db.db.execute(
        "SELECT artifact_id,meta FROM artifacts WHERE message_id=1").fetchone()
    replacement = json.loads(original)
    replacement.update(attempts=4, auto_retry=2, next_try=now + 100)
    if change == "succeeded":
        replacement.update(error=False, attempts=0)
    replacement_json = json.dumps(replacement)
    crossed = []
    synthetic_path = db.db.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(synthetic_path) as other:
        def cross_snapshot(sql):
            if not crossed and sql.startswith("UPDATE artifacts SET meta="):
                crossed.append(True)
                if change == "deleted":
                    other.execute("DELETE FROM artifacts WHERE artifact_id=?", (aid,))
                else:
                    other.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                                  (replacement_json, aid))
                other.commit()

        db.db.set_trace_callback(cross_snapshot)
        try:
            result = extract_llm.revive_failed(db, now)
        finally:
            db.db.set_trace_callback(None)
    assert crossed == [True]
    assert result == {"revived": 1, "skipped_cap": 0}
    row = db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()
    assert row is None if change == "deleted" else row[0] == replacement_json
    second = json.loads(db.db.execute(
        "SELECT meta FROM artifacts WHERE message_id=2").fetchone()[0])
    assert second["attempts"] == 4 and second["auto_retry"] == 1


@pytest.mark.parametrize("updated,posted,expected", [
    ("2026-10-04T10:00:00.900+09:00", "2026-10-04T10:00:00.100+09:00", False),
    ("2026-10-04T01:00:00.900Z", "2026-10-04T10:00:00.100+09:00", False),
    ("2026-10-04T01:00:00.100Z", "2026-10-04T10:00:00.100+09:00", True),
    ("2026-10-04T10:00:00.100+09:00", "2026-10-04T10:00:00.900+09:00", True),
    ("2026-10-04T01:00:00", "2026-10-04T10:00:00+09:00", False),
    ("2026-10-04T00:00:00+09:00", "2026-10-04T01:00:00", False),
    ("2026-10-04", "2026-10-05T00:00:00+09:00", False),
    ("2026-10-04T00:00:00+09:00", "2026-10-05", False),
])
def test_karte_context_uses_explicit_instants_without_rounding(db, updated, posted, expected):
    db.karte_summary_store(1, 1, {"comment": "架空の参考情報", "updated_at": updated})
    block = extract_llm._karte_block(db, 1, posted)
    assert bool(block) is expected
    if expected:
        assert "架空の参考情報" in block


@pytest.mark.parametrize("updated,posted", [
    ("2026-10-04T01:00:00.100Z", "2026-10-04T10:00:00.100+09:00"),
    ("2026-10-04T10:00:00.100+09:00", "2026-10-04T01:00:00.100Z"),
    ("2026-10-04T01:00:00.100Z", "2026-10-04T01:00:00.100Z"),
])
def test_karte_utc_suffix_keeps_python310_compatibility(db, monkeypatch, updated, posted):
    class Python310Datetime:
        @staticmethod
        def fromisoformat(value):
            if isinstance(value, str) and value.endswith("Z"):
                raise ValueError("Python 3.10 does not parse a Z suffix")
            return datetime.fromisoformat(value)

    monkeypatch.setattr(extract_llm, "datetime", Python310Datetime)
    db.karte_summary_store(1, 1, {"comment": "架空のUTC参考情報", "updated_at": updated})
    assert "架空のUTC参考情報" in extract_llm._karte_block(db, 1, posted)


@pytest.mark.parametrize("kind", ["extract_llm", "canonical_projection", "semantic_facts_v4"])
def test_empty_current_fact_source_never_revives_rule_vitals(db, kind):
    db.save_messages([_message(body="酸素10L投与中")])
    db.artifact_add("extract_v1", json.dumps({"vitals": {"spo2": 10}}),
                    project_id=1, message_id=1,
                    meta={"hash": _hash(db), "rule_version": extract.RULE_VERSION})
    db.artifact_add(kind, "{}", project_id=1, message_id=1,
                    meta={"hash": _hash(db), "extract_version": extract_llm.EXTRACT_VERSION,
                          "engine_version": 4, "projection_version": PROJECTION_VERSION})
    assert rollup.build_rollup(db, 1).get("latest_vitals") is None


def test_rule_vitals_remain_available_without_a_current_fact_source(db):
    db.save_messages([_message(body="SpO2 96%")])
    db.artifact_add("extract_v1", '{"vitals":{"spo2":96}}',
                    project_id=1, message_id=1, meta={"hash": _hash(db)})
    db.artifact_add("extract_llm", "{}", project_id=1, message_id=1,
                    meta={"hash": "stale-body"})
    assert rollup.build_rollup(db, 1)["latest_vitals"]["spo2"] == 96


@pytest.mark.parametrize("suffix", ["ではありません", "疑い", "かもしれません", "extra"])
def test_qualitative_lab_prefix_does_not_confirm_a_different_result(db, suffix):
    body = "合成検査Q 陰性" + suffix
    raw = {"name": "合成検査Q", "value": "陰性", "evidence": body}
    lab = extract_llm._validate({"labs": [raw]}, body)["labs"][0]
    assert lab["value"] == "陰性"
    assert lab["unverified"] is True
    assert lab["normalized"]["confirmation"] == "unverified"
    db.save_messages([_message(body=body)])
    # An old artifact may claim confirmation: the shared view rechecks the quote.
    db.artifact_add("extract_llm", json.dumps({"labs": [{**raw, "normalized": {
        "confirmation": "quote_supported"}}]}), project_id=1, message_id=1,
        meta={"hash": _hash(db), "extract_version": extract_llm.EXTRACT_VERSION})
    lines = structured_view.structured_lines(db.db, 1)
    qualifier = "(条件・可能性の記載)" if suffix == "かもしれません" else ""
    assert "検査候補（未確認）: 合成検査Q 陰性" + qualifier in lines
    assert not any(line.startswith("検査:") for line in lines)


@pytest.mark.parametrize("ending", ["", "。", "です。", "でした。", "、再確認します"])
def test_complete_qualitative_lab_statement_stays_quote_supported(ending):
    body = "合成検査Q 陰性" + ending
    lab = extract_llm._validate({"labs": [{
        "name": "合成検査Q", "value": "陰性", "evidence": body}]}, body)["labs"][0]
    assert lab["normalized"]["confirmation"] == "quote_supported"


def test_complete_negated_lab_result_is_preserved_without_reinterpretation():
    body = "合成検査Q 陰性ではありません"
    raw = {"name": "合成検査Q", "value": "陰性ではありません", "evidence": body}
    lab = extract_llm._validate({"labs": [raw]}, body)["labs"][0]
    assert lab["value"] == raw["value"]
    assert lab["normalized"]["confirmation"] == "quote_supported"


@pytest.mark.parametrize("budget", [float("nan"), float("inf"), -float("inf"), True, "180"])
def test_invalid_budget_is_rejected_before_any_database_or_inference(budget):
    with pytest.raises(ValueError, match="extract_budget_invalid"):
        extract_llm.run_pending(object(), budget_s=budget)


@pytest.mark.parametrize("args", [
    ["--budget", "nan"], ["--budget", "inf"], ["--budget=-inf"],
    ["--all", "--stop-after", "nan"], ["--all", "--stop-after", "inf"],
    ["--all", "--stop-after=-1"],
])
def test_nonfinite_cli_budget_cannot_open_a_database_or_start_a_resident(monkeypatch, capsys, args):
    monkeypatch.setattr(sys, "argv", ["extract_llm.py", *args])
    monkeypatch.setattr(extract_llm, "Ledger", lambda *a: pytest.fail("opened database"))
    assert extract_llm.main() == 2
    assert json.loads(capsys.readouterr().out)["error"] == "bad_budget"


@pytest.mark.parametrize("changed", ["body", "context", "posted_at", "expect", "unknown"])
def test_bench_cannot_send_unapproved_case_content_to_a_model(tmp_path, monkeypatch, changed):
    case = dict(extract_bench._load_cases(extract_bench.DEFAULT_CASES)[0])
    if changed == "unknown":
        case = {"id": "unknown-fictional-case", "body": "完全合成の未登録本文", "expect": {}}
    else:
        case[changed] = {"events": ["visit"]} if changed == "expect" else "完全合成の差替え"
    path, report = tmp_path / "cases.json", tmp_path / "report.json"
    path.write_text(json.dumps({"source": "fully_synthetic", "cases": [case]}))
    calls = []
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **kw: calls.append(body) or {})
    assert extract_bench.cmd_run(SimpleNamespace(
        cases=str(path), out=str(report), tag="synthetic", mock_ok=False)) == 2
    assert calls == [] and not report.exists()


@pytest.mark.parametrize("filename", ["extract_cases.json", "extract_cases_labs.json"])
def test_bench_known_synthetic_case_copies_keep_the_report_contract(tmp_path, monkeypatch, filename):
    approved = extract_bench._load_cases(str(extract_bench.Path(extract_bench.DEFAULT_CASES)
                                           .with_name(filename)))[0]
    path, report = tmp_path / "copied.json", tmp_path / "report.json"
    path.write_text(json.dumps({"cases": [approved]}, ensure_ascii=False, indent=2))
    calls = []
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **kw: calls.append(body) or {})
    assert extract_bench.cmd_run(SimpleNamespace(
        cases=str(path), out=str(report), tag="synthetic", mock_ok=False)) == 0
    assert calls == [approved["body"]]
    result = json.loads(report.read_text())
    assert result["n_cases"] == 1 and result["model"] == extract_llm.MODEL
    assert len(result["corpus_sha256"]) == 64


def test_bench_offline_validation_keeps_custom_fully_synthetic_cases(tmp_path):
    path = tmp_path / "custom.json"
    path.write_text(json.dumps({"cases": [
        {"id": "fictional-custom", "body": "架空の検証本文", "expect": {}}]}))
    assert extract_bench.cmd_run(SimpleNamespace(cases=str(path), mock_ok=True)) == 0


@pytest.mark.parametrize("script,args", [
    ("benchmark", ["run", "--mock-ok"]),
    ("review_queue", ["validate"]),
])
def test_offline_entrypoints_bootstrap_outside_the_repository(tmp_path, script, args):
    root = extract_bench.Path(extract_bench.DEFAULT_CASES).parent.parent
    target = (extract_bench.Path(extract_bench.__file__) if script == "benchmark"
              else root / "evaluation" / "request_following_review.py")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    result = subprocess.run([sys.executable, str(target), *args], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    if script == "review_queue":
        report = json.loads(result.stdout)
        assert report["human_verified_labels"] == 0 and report["promotion_eligible"] is False
