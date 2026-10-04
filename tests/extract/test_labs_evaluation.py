"""Frozen, fully synthetic #15 labs corpus: P/R through _validate and forbid=0."""

import json
from pathlib import Path

import extract_bench
import extract_llm

CORPUS = Path(__file__).resolve().parents[2] / "evaluation" / "extract_cases_labs.json"


def _claims(case):
    # Simulated model output: every labelled assertion quoting the whole body.
    # measured_on is derived by the validator, never claimed by the model.
    labels = case["expect"]["labs"] + [
        lab for lab in case["forbid"]["labs"] if "measured_on" not in lab]
    return [{k: v for k, v in lab.items() if k not in ("unverified", "measured_on")}
            | {"evidence": case["body"]} for lab in labels]


def test_labs_corpus_precision_recall_and_no_confirmed_forbidden_value():
    # Given: the frozen synthetic corpus covering the planned categories.
    raw = CORPUS.read_bytes()
    cases = json.loads(raw)["cases"]
    assert {c["category"] for c in cases} == {
        "基準内外", "単位表記ゆれ", "全角", "前回→今回", "家族の値",
        "予定/依頼だけ", "値なし", "別検査名の混同", "複数日付"}
    assert all(extract_bench._section_valid(c["expect"])
               and extract_bench._section_valid(c["forbid"]) for c in cases)
    # A typo'd bench-only lab key must not let a forbid label pass vacuously.
    for bad in ({"unverified": "false"}, {"measured_on": "2026/10/01"}):
        assert not extract_bench._section_valid(
            {"labs": [{"name": "Cr", "value": 1.2} | bad]})
    # When: the production validator and bench scorer run on each case.
    scores = [extract_bench._score_case(
        c, extract_llm._validate({"labs": _claims(c)}, c["body"])) for c in cases]
    by_category = {}
    for case, score in zip(cases, scores):
        tp, fp, fn = by_category.get(case["category"], (0, 0, 0))
        f = score["fields"]["labs"]
        by_category[case["category"]] = (tp + f["tp"], fp + f["fp"], fn + f["fn"])
    labs = extract_bench._aggregate(scores)["labs"]
    # Then: no forbidden value is shown as confirmed (forbid=0).
    assert [s["forbid_violations"] for s in scores if s["forbid_violations"]] == []
    # Fixed (tp, fp, fn). The only miss is conservative: a quote mentioning
    # 基準 stays an unconfirmed candidate. Owner P/R targets are external.
    assert by_category == {
        "基準内外": (3, 0, 1), "単位表記ゆれ": (3, 0, 0), "全角": (2, 0, 0),
        "前回→今回": (3, 0, 0), "家族の値": (0, 0, 0), "予定/依頼だけ": (0, 0, 0),
        "値なし": (1, 0, 0), "別検査名の混同": (5, 0, 0), "複数日付": (3, 0, 0)}
    assert (labs["precision"], labs["recall"]) == (1.0, 0.952)


def test_labs_scoring_when_extraction_failed_or_confirmed_forbidden():
    # Given: a corpus case and two outputs (failure, wrongly confirmed value).
    case = {"id": "x", "body": "CRP 1.2mg/dL", "expect": {"labs": [
        {"name": "CRP", "value": 1.2, "unit": "mg/dL"}]},
        "forbid": {"labs": [{"name": "Cr", "value": 1.2, "unverified": False}]}}
    # When/Then: a failure stays in recall; a confirmed confusion is a violation.
    assert extract_bench._score_case(case, None)["fields"]["labs"] == {
        "tp": 0, "fp": 0, "fn": 1}
    score = extract_bench._score_case(case, {"labs": [
        {"name": "Cr", "value": 1.2, "unit": "mg/dL"}]})
    assert score["fields"]["labs"] == {"tp": 0, "fp": 1, "fn": 1}
    assert score["forbid_violations"] == ["labs:Cr"]
