"""extract_bench scoring contract tests — pure function, no LLM."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "mcs"))

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


def test_case_file_validates_offline():
    import extract_llm
    cases = extract_bench._load_cases(extract_bench.DEFAULT_CASES)
    assert len(cases) >= 10
    for c in cases:
        exp = c.get("expect", {})
        if exp:
            assert isinstance(extract_llm._validate(dict(exp)), dict), \
                f"{c['id']}: expectation fails _validate"
