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
    "output,counts",
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
