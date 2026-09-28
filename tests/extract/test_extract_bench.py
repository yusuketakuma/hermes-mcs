"""extract_bench scoring contract tests — pure function, no LLM."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import extract_bench


def test_med_match_action_any_when_unspecified():
    got = [{"name": "プレドニン", "action": "stop"}]
    assert extract_bench._match_med({"name": "プレドニン"}, got)
    assert extract_bench._match_med(
        {"name": "プレドニン", "action": "stop"}, got)
    assert not extract_bench._match_med(
        {"name": "プレドニン", "action": "start"}, got)


def test_symptom_match_negated_aware():
    got = [{"text": "発熱", "negated": True}]
    assert extract_bench._match_symptom(
        {"text": "発熱", "negated": True}, got)
    assert not extract_bench._match_symptom(
        {"text": "発熱", "negated": False}, got)
    # containment: longer extraction text still matches a short label
    got2 = [{"text": "左側頭部皮下血腫", "negated": False}]
    assert extract_bench._match_symptom({"text": "皮下血腫"}, got2)


def test_events_loose_fp_only_forbid():
    # open-vocabulary field: unlisted 'care' is not an FP, but a
    # forbid-matching event is
    case = {"id": "x", "expect": {"events": ["visit"]},
            "forbid": {"events": ["fall"]}}
    out = {"events": ["visit", "care", "fall"]}
    s = extract_bench._score_case(case, out)
    assert s["fields"]["events"] == {"tp": 1, "fp": 1, "fn": 0}
    assert "events:fall" in s["forbid_violations"]


def test_meds_strict_fp_counts_unlisted():
    case = {"id": "x", "expect": {"meds": [{"name": "A"}]}}
    out = {"meds": [{"name": "A"}, {"name": "B"}]}
    s = extract_bench._score_case(case, out)
    assert s["fields"]["meds"] == {"tp": 1, "fp": 1, "fn": 0}


def test_one_output_cannot_satisfy_two_expected_symptoms():
    case = {"id": "synthetic", "expect": {"symptoms": [
        {"text": "頭痛"}, {"text": "腹痛"}]}}
    output = {"symptoms": [{"text": "頭痛と腹痛", "negated": False}]}

    assert extract_bench._score_case(case, output)["fields"]["symptoms"] == {
        "tp": 1, "fp": 0, "fn": 1}


def test_one_to_one_matching_prefers_the_specific_medication():
    case = {"id": "synthetic", "expect": {"meds": [
        {"name": "合成薬"}, {"name": "合成薬", "action": "stop"}]}}
    output = {"meds": [{"name": "合成薬", "action": "stop"},
                       {"name": "合成薬", "action": "start"}]}

    assert extract_bench._score_case(case, output)["fields"]["meds"] == {
        "tp": 2, "fp": 0, "fn": 0}


def test_one_medication_cannot_satisfy_two_safety_attributes():
    case = {"id": "synthetic", "expect": {"meds": [
        {"name": "合成薬", "status": "past"},
        {"name": "合成薬", "status": "past"}]}}
    output = {"meds": [{"name": "合成薬", "status": "past"}]}

    fields = extract_bench._score_case(case, output)["fields"]
    assert fields["meds"] == {"tp": 1, "fp": 0, "fn": 1}
    assert fields["med_status"] == {"tp": 1, "fp": 0, "fn": 1}


def test_extract_failed_marks_error():
    s = extract_bench._score_case({"id": "x", "expect": {}}, None)
    assert s["error"] == "extract_failed"


def test_aggregate_f1():
    scores = [{"id": "a", "fields": {
        "meds": {"tp": 1, "fp": 1, "fn": 1}}}]
    agg = extract_bench._aggregate(scores)
    assert agg["meds"] == {"tp": 1, "fp": 1, "fn": 1,
                           "precision": 0.5, "recall": 0.5,
                           "f1": 0.5}


@pytest.mark.parametrize(
    ("output", "counts"),
    [(None, {"tp": 0, "fp": 0, "fn": 1}),
     ({"requests": []}, {"tp": 0, "fp": 0, "fn": 1}),
     ({"requests": [{"action": "確認", "to": "家族"}]},
      {"tp": 0, "fp": 1, "fn": 1}),
     ({"requests": [{"action": "確認してください", "to": "医師"}]},
      {"tp": 1, "fp": 0, "fn": 0})])
def test_request_expectations_contribute_to_recall(output, counts):
    case = {"id": "request", "expect": {
        "requests": [{"action": "確認", "to": "医師"}]}}
    score = extract_bench._score_case(case, output)
    assert score["fields"]["requests"] == counts


def test_forbidden_request_is_reported_with_extracted_items():
    request = {"action": "中止", "to": "患者"}
    case = {"id": "request", "forbid": {"requests": [request]}}
    score = extract_bench._score_case(case, {"requests": [request]})
    assert score["forbid_violations"] == ["requests:中止"]
    assert score["raw"]["requests"] == [request]


def test_case_file_validates_offline():
    import extract_llm
    cases = extract_bench._load_cases(extract_bench.DEFAULT_CASES)
    assert len(cases) >= 10
    for c in cases:
        exp = c.get("expect", {})
        if exp:
            assert isinstance(extract_llm._validate(dict(exp)), dict), \
                f"{c['id']}: expectation fails _validate"


def test_bench_scores_foreign_and_mixed_vitals():
    case = {'id': 'synthetic-vital', 'expect': {'vitals': {'sbp': 90}}}
    score = extract_bench._score_case(case, {'vitals': {'sbp': 90, 'dbp': 80}})
    assert score['fields']['vitals'] == {'tp': 1, 'fp': 1, 'fn': 0}
    assert extract_bench._score_case(case, None)['fields']['vitals']['fn'] == 1


def test_bench_symptom_subject_and_time_are_scored():
    assert not extract_bench._match_symptom(
        {'text': '発熱', 'subject': 'patient', 'status': 'ongoing'},
        [{'text': '発熱', 'subject': 'family', 'status': 'past', 'negated': False}])


def test_benchmark_records_cost_and_paired_corpus(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    import extract_llm
    cases = tmp_path / 'synthetic.json'
    cases.write_text(json.dumps({'cases': [{'id': 's', 'body': '合成', 'expect': {}}]}))
    def fake(body, **kwargs):
        kwargs['meta_out'].update(calls=2, repairs=1, usage={'total_tokens': 12})
        return {}
    monkeypatch.setattr(extract_llm, 'llm_extract', fake)
    outputs = []
    for tag in ('before', 'after'):
        out = tmp_path / (tag + '.json')
        assert extract_bench.cmd_run(SimpleNamespace(
            cases=str(cases), out=str(out), tag=tag, mock_ok=False)) == 0
        outputs.append(str(out))
    report = json.loads(Path(outputs[0]).read_text())
    assert report['cases'][0]['performance']['calls'] == 2
    assert report['cases'][0]['performance']['usage']['total_tokens'] == 12
    assert report['performance']['p95_s'] is not None
    assert extract_bench.cmd_report(SimpleNamespace(files=outputs)) == 0
    report['corpus_sha256'] = 'different'
    Path(outputs[0]).write_text(json.dumps(report))
    assert extract_bench.cmd_report(SimpleNamespace(files=outputs)) == 2


@pytest.mark.parametrize(("durations", "expected"), [
    ([], {"p50_s": None, "p95_s": None}),
    ([12.5], {"p50_s": 12.5, "p95_s": 12.5}),
    ([9, 1, 4, 2], {"p50_s": 2, "p95_s": 9}),
    (list(range(20, 0, -1)), {"p50_s": 10, "p95_s": 19}),
])
def test_benchmark_latency_percentiles(durations, expected):
    assert extract_bench._duration_percentiles(durations) == expected
