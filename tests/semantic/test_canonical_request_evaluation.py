"""Entirely fictional canonical-following evaluation, not model acceptance."""
import copy
import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "canonical_request_eval", ROOT / "evaluation" / "canonical_request_eval.py")
assert SPEC is not None and SPEC.loader is not None
support = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(support)


@pytest.fixture
def corpus():
    return json.loads((ROOT / "evaluation" / "canonical_request_cases.json").read_text())


@pytest.fixture
def criteria():
    return json.loads((ROOT / "evaluation" / "g6-criteria-v1.json").read_text())


def test_actual_old_new_canonical_projection_and_missing_metadata_are_reported(corpus, criteria):
    report = support.evaluate(corpus, criteria)
    assert report["cases"] == 12
    assert report["canonical_compatibility"]["correct"] == 12
    assert report["canonical_compatibility"]["denominator"] == 12
    assert report["canonical_compatibility"]["scope"] == "legacy_projection_shape_only"
    assert report["canonical_compatibility"]["false_done_nonregression"] is False
    assert report["canonical_metadata"]["request_to"] == {"supplied": 1, "retained": 1}
    assert report["canonical_metadata"]["request_from"] == {"supplied": 2, "retained": 2}
    assert report["quality"]["legacy"]["fields"]["kind"]["rate"] == 1
    assert report["quality"]["canonical_new"]["fields"]["kind"]["rate"] == 0
    assert report["quality"]["canonical_new"]["fields"]["from"]["rate"] == .5
    assert report["quality"]["canonical_new"]["unknown_preservation"]["rate"] == 1
    assert report["production_ready"] is False
    assert report["model_calls"] == 0
    serialized = json.dumps(report, ensure_ascii=False)
    assert corpus["cases"][0]["text"] not in serialized
    assert "架空担当A" not in serialized and "fictional-request-details" not in serialized


def test_duplicate_requests_false_done_and_request_reply_confusion_do_not_disappear(
        corpus, criteria):
    report = support.evaluate(corpus, criteria)
    legacy = report["quality"]["legacy"]
    assert legacy["request_precision"]["correct"] == 6
    assert legacy["request_precision"]["denominator"] == 7
    assert legacy["request_recall"]["rate"] == 1
    assert legacy["false_positive_requests"] == 1
    assert legacy["request_kinds"]["request"]["precision"]["correct"] == 4
    assert legacy["request_kinds"]["request"]["precision"]["denominator"] == 5
    assert legacy["false_done"] == {"errors": 2, "denominator": 12}
    assert legacy["reply_confusion"]["intent"]["done"] == 1
    assert legacy["reply_confusion"]["none"]["done"] == 1
    canonical = report["quality"]["canonical_new"]
    assert canonical["request_reply_confusion"]["reply"]["request"] == 1
    assert canonical["false_done"]["errors"] == 1
    assert report["quality"]["canonical_old"]["false_done"]["errors"] == 0


def test_missing_request_and_empty_predictions_keep_recall_denominators(corpus, criteria):
    corpus["cases"] = [corpus["cases"][0]]
    corpus["cases"][0]["legacy"] = {}
    corpus["cases"][0]["canonical_new"] = []
    report = support.evaluate(corpus, criteria)
    metric = report["quality"]["legacy"]
    assert metric["request_precision"]["rate"] is None
    assert metric["request_recall"]["rate"] == 0
    assert metric["missing_requests"] == 1
    assert report["canonical_compatibility"]["correct"] == 0


def test_more_than_200_synthetic_rows_cannot_satisfy_existing_g6(corpus, criteria):
    original = corpus["cases"][0]
    corpus["cases"] = []
    for index in range(201):
        row = copy.deepcopy(original)
        row["case_id"] = f"fictional-{index}"
        corpus["cases"].append(row)
    report = support.evaluate(corpus, criteria)
    assert report["g6"]["min_human_labels"] == 200
    assert report["g6"]["label_provenance"] == {"synthetic": 201, "human": 0, "missing": 0}
    assert report["g6"]["gate"]["pass"] is False
    assert report["g6"]["gate"]["g6_eligible"] is False
    assert "human_labels_insufficient" in report["g6"]["gate"]["reasons"]
    assert "human_labels_required" in report["g6"]["gate"]["reasons"]
    assert "telemetry_missing:latency" in report["g6"]["gate"]["reasons"]


@pytest.mark.parametrize("source", ["human", "production", None])
def test_support_never_relabels_cases_as_human(corpus, criteria, source):
    corpus["source"] = source
    with pytest.raises(ValueError, match="synthetic_corpus_required"):
        support.evaluate(corpus, criteria)


def test_duplicate_identity_unknown_fields_and_weakened_human_gate_are_refused(corpus, criteria):
    corpus["cases"].append(copy.deepcopy(corpus["cases"][0]))
    with pytest.raises(ValueError, match="case_fields_invalid"):
        support.evaluate(corpus, criteria)
    corpus["cases"].pop()
    corpus["cases"][0]["legacy"]["future_field"] = "fictional"
    with pytest.raises(ValueError, match="prediction_fields_invalid"):
        support.evaluate(corpus, criteria)
    corpus["cases"][0]["legacy"].pop("future_field")
    criteria["min_human_labels"] = 199
    with pytest.raises(ValueError, match="human_gate_must_remain_at_least_200"):
        support.evaluate(corpus, criteria)


def _days():
    return [{"day": str(date(2026, 9, 1) + timedelta(days=index)),
             "llm_s": 23_400, "job_llm_s": [900] * 26,
             "slot_busy": int(index in (0, 7))} for index in range(14)]


def test_synthetic_capacity_boundaries_and_missing_observations_are_not_acceptance():
    days = _days()
    report = support.capacity_report(days)
    assert report["daily_mean_llm_s"] == 23_400
    assert report["job_llm_s_p90"] == 900
    assert report["slot_busy_per_week"] == [1, 1]
    assert report["thresholds_met"] is True and report["eligible"] is False
    days[1]["slot_busy"] = 1
    assert support.capacity_report(days)["thresholds_met"] is False
    assert support.capacity_report(days[:-1])["thresholds_met"] is None
    for day in days:
        day["job_llm_s"] = []
    assert support.capacity_report(days)["job_llm_s_p90"] is None
    assert support.capacity_report(days)["thresholds_met"] is None
    assert support.capacity_report([])["daily_mean_llm_s"] is None


@pytest.mark.parametrize("invalid", [float("inf"), float("nan"), True, -1])
def test_capacity_invalid_measurements_cannot_become_zero(invalid):
    days = _days()
    days[0]["llm_s"] = invalid
    with pytest.raises(support.evaluation.EvaluationError):
        support.capacity_report(days)


def test_capacity_p90_reuses_linear_interpolation_and_checks_day_identity():
    days = _days()
    for day in days:
        day["job_llm_s"] = []
        day["llm_s"] = 0
    days[0]["job_llm_s"] = [100, 1000]
    days[0]["llm_s"] = 1100
    assert support.capacity_report(days)["job_llm_s_p90"] == 910
    days[-1]["day"] = days[0]["day"]
    with pytest.raises(ValueError, match="capacity_dates_invalid"):
        support.capacity_report(days)


def test_real_offline_cli_reports_only_local_fixture_runtime(capsys):
    assert support.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    runtime = report["local_fixture_runtime"]
    assert runtime["elapsed_ms"] >= 0
    assert runtime["scope"] == "offline_validation_projection_scoring_only"
    assert runtime["llm_capacity_measurement"] is False
    assert report["g6"]["gate"]["g6_eligible"] is False
    assert report["capacity"]["eligible"] is False


def test_cli_rejects_deep_synthetic_json_without_traceback(tmp_path, capsys):
    source = tmp_path / 'deep.json'
    source.write_text('[' * 20000 + '0' + ']' * 20000)
    assert support.main(['--input', str(source)]) == 1
    assert json.loads(capsys.readouterr().out) == {'error': 'RecursionError'}


@pytest.mark.parametrize('bad_field,bad_value', [
    ('statement', []), ('statement', {}), ('quote', ['bad1', 'bad2', 'bad3', 'bad4']),
    ('quote', {'bad1': 1, 'bad2': 2, 'bad3': 3, 'bad4': 4}),
])
def test_shadow_corrupt_evidence_does_not_block_other_candidate(bad_field, bad_value):
    import sqlite3
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE messages(message_id INTEGER,project_id INTEGER,'
                   'content_hash TEXT,body_state TEXT)')
        db.execute('CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,kind TEXT,'
                   'project_id INTEGER,message_id INTEGER,content TEXT,meta TEXT)')
        for mid in (1, 2):
            text = '架空資料を確認してください'
            fact = {'_v2_kind': 'request_pending', 'statement': text, '_evidence': {'quote': text}}
            if mid == 1:
                if bad_field == 'quote':
                    fact['_evidence']['quote'] = bad_value
                else:
                    fact['statement'] = bad_value
            candidate = {'source': {'revision': 'synthetic'},
                         'projection': {'requests': [{'action': text}]}, 'loop_facts': [fact]}
            db.execute('INSERT INTO messages VALUES(?,?,?,?)', (mid, 1, 'synthetic', 'full'))
            db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,meta) VALUES(?,?,?,?,?)',
                       ('v4_stage', 1, mid, json.dumps({'candidate': candidate}),
                        json.dumps({'stage': 'request_following_candidate'})))
            db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,meta) VALUES(?,?,?,?,?)',
                       ('extract_llm', 1, mid, json.dumps({'requests': [{'action': text, 'evidence': text}]}),
                        json.dumps({'hash': 'synthetic'})))
        before = db.total_changes
        result = support.shadow_compare(db)
        assert db.total_changes == before
        assert result['candidates'] == 2 and result['stale_or_invalid'] == 1
        assert result['compared'] == result['matched'] == 1
    finally:
        db.close()
