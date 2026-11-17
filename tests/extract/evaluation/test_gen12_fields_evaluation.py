"""Fully synthetic generation-12 field corpus and the bench's matching of its new labels."""
import copy
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

import extract_bench
import extract_llm

ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "evaluation" / "extract_cases_gen12_fields.json"
CASES = json.loads(CORPUS.read_bytes())["cases"]
BY_ID = {case["id"]: case for case in CASES}
CATEGORIES = {"剤形・飲む時点・処方期間", "labs形式のvitals", "date_text・actor",
              "日付の推測", "栄養・排泄（A12）", "A15略称の衝突"}


def _output(case, labels):
    """A validated-shaped output asserting exactly what each label names."""
    body = case["body"]
    out = {}
    if "meds" in labels:
        out["meds"] = copy.deepcopy(labels["meds"])
    if "vitals" in labels:
        out["vitals"] = dict(labels["vitals"])
    if "labs" in labels:
        out["labs"] = [{**lab, "evidence": body} for lab in labels["labs"]]
    if "requests" in labels:
        out["requests"] = [{**request, "evidence": body} for request in labels["requests"]]
    if "event_records" in labels:
        out["event_records"] = [{"subject": "patient", "status": "current", "negated": False,
                                 "evidence": body, **record} for record in labels["event_records"]]
    if "patient_context" in labels:
        out["patient_context"] = [
            {"category": label["category"], "text": label["value_contains"], "evidence": body,
             "subject": label.get("subject", "patient")} for label in labels["patient_context"]]
    return out


def test_corpus_is_distinct_synthetic_and_covers_every_planned_category():
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids)) and all(case_id.startswith("g12-") for case_id in ids)
    assert {case["category"] for case in CASES} == CATEGORIES
    assert "完全創作" in json.loads(CORPUS.read_bytes())["note"]
    approved = extract_bench._synthetic_cases()
    assert all(case in approved for case in CASES)
    assert len({case["id"] for case in approved}) == len(approved)
    for case in CASES:
        assert "expect" in case or "forbid" in case, case["id"]
        for key in ("expect", "forbid"):
            assert extract_bench._section_valid(case.get(key, {}), forbid=key == "forbid"), (case["id"], key)


def test_corpus_names_every_planned_forbid_example():
    def forbidden(field):
        return [label for case in CASES for label in case.get("forbid", {}).get(field, [])]

    lab_names = {(label["name"], label.get("value")) for label in forbidden("labs")}
    assert {("Mg", 330), ("マグミット", 330), ("Ca", 6.8), ("TP", 9.2), ("PT", 2), ("PT", 1.8)} <= lab_names
    assert {"PT", "PT-INR", "T-Bil", "NH3", "TSH"} <= {
        lab["name"] for case in CASES for lab in case.get("expect", {}).get("labs", [])}
    events = forbidden("event_records")
    assert any(label.get("status") == "past" and label["kind"] == "visit" for label in events)
    undated = [case for case in CASES if case["category"] == "日付の推測" and "posted_at" not in case]
    assert len(undated) == 2
    for case in undated:
        assert any(re.fullmatch(r"\d{4}-\d\d-\d\d", label.get("date_text") or "")
                   for label in case["forbid"]["event_records"]), case["id"]
    assert any(label.get("timing") for label in forbidden("meds") if label.get("subject") == "patient")
    assert forbidden("vitals")


def test_case_bodies_do_not_leak_from_few_shot_examples():
    def norm(text):
        return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text))

    examples = norm(extract_llm._PROMPT_EXAMPLES)
    for case in CASES:
        text = norm(case["body"] + case.get("context", ""))
        assert not {text[i:i + 12] for i in range(max(len(text) - 11, 1))
                    if text[i:i + 12] in examples}, case["id"]


def test_output_matching_every_expectation_misses_nothing_and_asserts_nothing_forbidden():
    scores = [extract_bench._score_case(case, _output(case, case.get("expect", {}))) for case in CASES]
    fields = extract_bench._aggregate(scores)
    assert {name: (row["fp"], row["fn"]) for name, row in fields.items()
            if name in ("meds", "labs", "vitals", "requests", "event_records", "patient_context")
            or name.startswith("med_")} == {name: (0, 0) for name in (
                "meds", "labs", "vitals", "requests", "event_records", "patient_context",
                "med_form", "med_timing", "med_days")}
    assert extract_bench._false_assertions(scores) == {"total": 0, "by_field": {}, "by_detail": {}}


@pytest.mark.parametrize("case", [case for case in CASES if "forbid" in case], ids=lambda case: case["id"])
def test_every_forbid_label_is_violated_by_the_output_it_names(case):
    score = extract_bench._score_case(case, _output(case, case["forbid"]))
    assert len(score["forbid_violations"]) == sum(len(labels) for labels in case["forbid"].values())


@pytest.mark.parametrize("case", [case for case in CASES if "expect" in case], ids=lambda case: case["id"])
def test_a_failed_extraction_is_a_miss_for_every_labelled_field(case):
    score = extract_bench._score_case(case, None)
    assert score["error"] == "extract_failed"
    assert all(row["tp"] == 0 and row["fp"] == 0 for row in score["fields"].values())
    for key in ("form", "timing", "days"):
        pinned = sum(key in med for med in case["expect"].get("meds", []))
        assert score["fields"].get(f"med_{key}", {"fn": 0})["fn"] == pinned


def test_a_stored_output_scores_through_the_offline_command(tmp_path):
    outputs = {case["id"]: _output(case, case.get("expect", {})) for case in CASES}
    stored = {"validated": True, "generations": {"llm_extract": 12},
              "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(), "outputs": outputs}
    (tmp_path / "o.json").write_text(json.dumps(stored, ensure_ascii=False))
    code = extract_bench.cmd_score(SimpleNamespace(
        cases=str(CORPUS), outputs=str(tmp_path / "o.json"), tag="t", out=str(tmp_path / "r.json")))
    report = json.loads((tmp_path / "r.json").read_text())
    assert code == 0 and report["false_assertion"]["total"] == 0


@pytest.mark.parametrize(("key", "wrong"), [("form", "散"), ("timing", "就寝前"), ("days", "7日分"),
                                           ("period", "1週間")])
def test_a_pinned_medication_attribute_must_agree(key, wrong):
    label = {"name": "合成薬ZZ", key: "原文"}
    assert extract_bench._match_med(label, [{"name": "合成薬ZZ", key: "原文"}])
    assert not extract_bench._match_med(label, [{"name": "合成薬ZZ", key: wrong}])
    assert not extract_bench._match_med(label, [{"name": "合成薬ZZ"}])
    assert extract_bench._match_med({"name": "合成薬ZZ", key: None}, [{"name": "合成薬ZZ"}])
    assert not extract_bench._match_med({"name": "合成薬ZZ", key: None}, [{"name": "合成薬ZZ", key: "原文"}])
    score = extract_bench._score_case({"id": "x", "expect": {"meds": [label]}}, {"meds": [{"name": "合成薬ZZ"}]})
    assert score["fields"][f"med_{key}"] == {"tp": 0, "fp": 0, "fn": 1}


@pytest.mark.parametrize("key", ["date_text", "actor"])
def test_a_pinned_event_text_must_agree(key):
    label = {"kind": "care", key: "原文"}
    assert extract_bench._match_event(label, [{"kind": "care", key: "原文"}])
    assert not extract_bench._match_event(label, [{"kind": "care", key: "別"}])
    assert not extract_bench._match_event(label, [{"kind": "care"}])
    assert extract_bench._match_event({"kind": "care", key: None}, [{"kind": "care", key: None}])
    assert not extract_bench._match_event({"kind": "care", key: None}, [{"kind": "care", key: "原文"}])


def test_forbidden_vitals_name_a_leaked_reading_and_expected_ones_still_score():
    case = {"id": "x", "body": "", "forbid": {"vitals": {"sbp": 170, "dbp": 100}}}
    assert extract_bench._score_case(case, {"vitals": {"sbp": 170}})["forbid_violations"] == ["vitals:sbp"]
    assert extract_bench._score_case(case, {"vitals": {"hr": 70}})["forbid_violations"] == []
    assert extract_bench._score_case(case, {})["forbid_violations"] == []
    assert extract_bench._false_assertions([extract_bench._score_case(case, {"vitals": {"sbp": 170, "dbp": 100}})]
                                           )["by_field"] == {"vitals": 2}


@pytest.mark.parametrize(("section", "forbid"), [
    ({"meds": [{"name": "合成薬ZZ", "form": 5}]}, False),
    ({"meds": [{"name": "合成薬ZZ", "timing": ""}]}, False),
    ({"meds": [{"name": "合成薬ZZ", "days": 14}]}, False),
    ({"meds": [{"name": "合成薬ZZ", "period": ["2週間"]}]}, True),
    ({"event_records": [{"kind": "care", "date_text": ""}]}, False),
    ({"event_records": [{"kind": "care", "actor": 3}]}, True),
    ({"vitals": {"spo": 96}}, False),
    ({"vitals": {"sbp": "128"}}, False),
    ({"vitals": {}}, True),
    ({"vitals": [128]}, True),
])
def test_misspelled_or_empty_new_labels_are_rejected(section, forbid):
    assert not extract_bench._section_valid(section, forbid=forbid)


@pytest.mark.parametrize("section", [
    {"meds": [{"name": "合成薬ZZ", "form": "錠", "timing": "朝食後", "days": "14日分", "period": None}]},
    {"event_records": [{"kind": "care", "date_text": "10月28日", "actor": "ヘルパー"}]},
    {"event_records": [{"kind": "fall", "date_text": None, "actor": None}]},
    {"vitals": {"sbp": 128, "dbp": 76}},
])
def test_well_formed_new_labels_are_accepted(section):
    assert extract_bench._section_valid(section)
    assert extract_bench._section_valid(section, forbid=True)
