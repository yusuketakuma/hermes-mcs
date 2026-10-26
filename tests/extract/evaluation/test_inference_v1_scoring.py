"""Pinned clinical attributes must affect the existing offline evaluation oracle."""
import pytest

import extract_bench as bench


@pytest.mark.parametrize("attribute,want,wrong", [
    ("route", "oral", "injection"), ("freq", "1日2回", "1日1回"),
    ("prn", False, True), ("prn", True, False),
])
def test_medication_attributes_are_in_the_denominator_even_on_failure(attribute, want, wrong):
    case = {"id": "synthetic-med-attribute", "expect": {"meds": [{"name": "架空薬A", attribute: want}]}}
    score = bench._score_case(case, {"meds": [{"name": "架空薬A", attribute: wrong}]})
    assert score["fields"]["meds"] == {"tp": 0, "fp": 1, "fn": 1}
    assert score["fields"][f"med_{attribute}"] == {"tp": 0, "fp": 0, "fn": 1}
    failure = bench._score_case(case, None)
    assert failure["fields"][f"med_{attribute}"] == {"tp": 0, "fp": 0, "fn": 1}
    matched = bench._score_case(case, {"meds": [{"name": "架空薬A", attribute: want}]})
    assert matched["fields"]["meds"] == {"tp": 1, "fp": 0, "fn": 0}


@pytest.mark.parametrize("attribute,want,wrong", [
    ("severity", "mild", "severe"), ("onset", "昨日", "今日"),
    ("duration", "3日", "3か月"),
])
def test_symptom_timing_and_severity_cannot_be_ignored(attribute, want, wrong):
    case = {"id": "synthetic-symptom-attribute", "expect": {
        "symptoms": [{"text": "咳", "negated": False, attribute: want}]}}
    score = bench._score_case(case, {"symptoms": [{"text": "咳", "negated": False, attribute: wrong}]})
    assert score["fields"]["symptoms"] == {"tp": 0, "fp": 1, "fn": 1}
    assert bench._score_case(case, None)["fields"]["symptoms"]["fn"] == 1


@pytest.mark.parametrize("attribute,want,wrong", [
    ("subject", "family", "patient"), ("status", "past", "current"),
    ("condition", "空腹時", "食後"), ("evidence", "合成検査値5", "別の合成検査値5"),
])
def test_equal_lab_numbers_do_not_hide_wrong_subject_time_or_source(attribute, want, wrong):
    case = {"id": "synthetic-lab-attribute", "expect": {
        "labs": [{"name": "合成検査", "value": 5, attribute: want}]}}
    score = bench._score_case(case, {"labs": [{"name": "合成検査", "value": 5, attribute: wrong}]})
    assert score["fields"]["labs"] == {"tp": 0, "fp": 1, "fn": 1}
    assert bench._score_case(case, None)["fields"]["labs"]["fn"] == 1


@pytest.mark.parametrize("field,item", [
    ("meds", {"name": "架空薬A", "dose": None}),
    ("symptoms", {"text": "咳", "negated": False, "onset": None}),
])
def test_a_missing_attribute_label_cannot_accept_an_invented_value(field, item):
    key = "dose" if field == "meds" else "onset"
    case = {"id": "synthetic-null", "expect": {field: [item]}}
    assert bench._score_case(case, {field: [item]})["fields"][field]["tp"] == 1
    wrong = bench._score_case(case, {field: [{**item, key: "原文にない値"}]})
    assert wrong["fields"][field] == {"tp": 0, "fp": 1, "fn": 1}
    if field == "meds":
        assert bench._score_case(case, None)["fields"]["med_dose"]["fn"] == 1


@pytest.mark.parametrize("field,item", [
    ("meds", {"name": "架空薬A", "dose": "5mg"}),
    ("symptoms", {"text": "咳", "negated": False}),
    ("event_records", {"kind": "admission", "subject": "patient"}),
])
def test_held_assertions_are_recall_misses_even_if_the_attributes_match(field, item):
    case = {"id": "synthetic-held", "expect": {field: [item]}}
    held = bench._score_case(case, {field: [{**item, "unverified": True}]})
    assert held["fields"][field] == {"tp": 0, "fp": 1, "fn": 1}


@pytest.mark.parametrize("key,value", [("subject", "family"), ("status", "planned"),
    ("negated", True), ("evidence", "別の合成記録"), ("span", {"start": 1, "end": 9})])
def test_event_kind_alone_cannot_satisfy_source_bound_event_axes(key, value):
    record = {"kind": "admission", "subject": "patient", "status": "current", "negated": False,
              "evidence": "本人は入院しました。", "span": {"start": 0, "end": 10}}
    case = {"id": "synthetic-event", "expect": {"event_records": [record]}}
    assert bench._score_case(case, {"event_records": [record]})["fields"]["event_records"]["tp"] == 1
    wrong = bench._score_case(case, {"event_records": [{**record, key: value}]})
    assert wrong["fields"]["event_records"] == {"tp": 0, "fp": 1, "fn": 1}
    assert bench._score_case(case, None)["fields"]["event_records"]["fn"] == 1
