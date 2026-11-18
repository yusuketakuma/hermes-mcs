"""Synthetic allergy retention and thinness checks using the shared clinical quality paths."""
import copy
import json

import pytest

import extract_llm as worker
import extract_quality as quality
import extract_validation as validation
from test_cloud_repair_final_proof import _repair_run, routed  # noqa: F401


def claim(body, **changes):
    return {"allergen": "合成薬A", "kind": "allergy", "reaction": None,
            "subject": "patient", "status": "current", "negated": False,
            "certainty": "confirmed", "evidence": body, **changes}


def test_two_allergy_candidates_make_a_long_post_non_thin_without_confirming_them():
    first = "合成薬Aにアレルギーがあります。"
    second = "合成薬Bで副作用がありました。"
    body = "完全合成の経過連絡です。" * 40 + first + second
    checked = validation.validate({"allergies": [claim(first),
        claim(second, allergen="合成薬B", kind="adverse_reaction", status="past")]}, body)
    assert all(item["unverified"] is True for item in checked["allergies"])
    assert len(quality.facts(checked)) == quality.thin_fact_count(checked) == 2
    assert not quality.is_thin(checked, body)
    assert quality.is_thin({"allergies": checked["allergies"][:1]}, body)


@pytest.mark.parametrize("axes", [{"negated": True}, {"subject": "family"}, {"subject": "other"}])
def test_omitting_a_grounded_protected_allergy_is_not_an_improvement(axes):
    body = "合成薬Aのアレルギーについて確認しました。"
    prior = validation.validate({"allergies": [claim(body, **axes)]}, body)
    before = copy.deepcopy(prior)
    candidate = {"allergies": [], "summary": "合成要約", "points": ["合成の要点"]}
    assert quality.protected_assertions(prior, body) == prior
    assert quality.protected_lost(prior, candidate, body) == {"allergies": 1}
    assert not quality.improves(candidate, prior)
    assert prior == before


def test_a_reaction_can_fill_an_unknown_attribute_without_losing_negation():
    body = "合成薬Aで発疹はありません。"
    prior = validation.validate({"allergies": [claim(body, negated=True)]}, body)
    candidate = validation.validate({"allergies": [claim(body, negated=True, reaction="発疹")]}, body)
    assert quality.protected_lost(prior, candidate, body) == {}
    assert quality.improves(candidate, prior)
    assert not quality.quality_improves(candidate, prior, body)


def test_added_medication_timing_does_not_lose_a_generation11_negation():
    body = "夕食後の合成薬Aは使用していません。"
    prior = {"meds": [{"name": "合成薬A", "negated": True, "evidence": body}]}
    candidate = {"meds": [{**prior["meds"][0], "timing": "夕食後"}], "allergies": []}
    before = copy.deepcopy(prior)
    assert quality.protected_lost(prior, candidate, body) == {}
    assert prior == before


def test_an_unlocated_allergy_does_not_freeze_an_unrelated_source():
    item = {**claim("合成薬Aにアレルギーはありません。", negated=True), "unverified": True}
    assert quality.protected_assertions({"allergies": [item]}, "別の合成本文。") == {}
    assert quality.protected_lost({"allergies": [item]}, {}, "別の合成本文。") == {}


@pytest.mark.parametrize("include_allergies", [False, True])
def test_source_grounded_vitals_gain_is_independent_of_new_allergy_candidates(include_allergies):
    quote = "合成薬Aにアレルギーがあります。"
    body = "本人の血圧120/80です。" + quote
    prior = validation.validate({"summary": "合成要約"}, body)
    raw = {"vitals": {"sbp": 120, "dbp": 80}}
    if include_allergies:
        raw["allergies"] = [claim(quote)]
    candidate = validation.validate(raw, body)
    before = copy.deepcopy((prior, candidate))
    assert candidate["vitals"] == {"sbp": 120.0, "dbp": 80.0}
    assert quality.quality_improves(candidate, prior, body)
    assert (prior, candidate) == before
    if include_allergies:
        assert candidate["allergies"][0]["unverified"] is True
    else:
        assert "allergies" not in candidate


def test_an_allergy_candidate_alone_is_not_a_verified_quality_gain():
    body = "合成薬Aにアレルギーがあります。"
    prior = validation.validate({"summary": "合成要約"}, body)
    candidate = validation.validate({**prior, "allergies": [claim(body)]}, body)
    assert candidate["allergies"][0]["unverified"] is True
    assert quality.improves(candidate, prior)
    assert not quality.quality_improves(candidate, prior, body)


@pytest.mark.parametrize("axes", [{}, {"negated": True}, {"subject": "family"}, {"subject": "other"}])
def test_another_field_gain_cannot_discard_an_existing_allergy(axes):
    quote = "合成薬Aのアレルギーについて確認しました。"
    body = quote + "本人の血圧120/80です。"
    prior = validation.validate({"allergies": [claim(quote, **axes)]}, body)
    candidate = validation.validate({"vitals": {"sbp": 120, "dbp": 80}}, body)
    assert quality.facts(candidate) - quality.facts(prior)
    assert not quality.quality_improves(candidate, prior, body)
    if axes:
        assert quality.protected_lost(prior, candidate, body) == {"allergies": 1}


@pytest.mark.parametrize("field,item", [
    ("labs", {"name": "CRP", "value": 1, "unit": "mg/dL", "unverified": True,
              "evidence": "本人のCRPは1mg/dLです。"}),
    ("meds", {"name": "合成薬B", "dose": "10mg", "status": "current", "subject": "patient",
              "negated": False, "evidence": "本人は合成薬Bを5mg内服しています。"}),
    ("events", "admission"),
])
def test_allergy_neutrality_does_not_relax_other_unverified_item_guards(field, item):
    quote = "合成薬Aにアレルギーがあります。"
    body = ("本人の血圧120/80です。本人のCRPは1mg/dLです。"
            "本人は合成薬Bを5mg内服しています。" + quote)
    prior = validation.validate({"summary": "合成要約"}, body)
    candidate = validation.validate({"vitals": {"sbp": 120, "dbp": 80},
        "allergies": [claim(quote)], field: [item]}, body)
    prior["_items_dropped"] = 1
    assert quality.improves(candidate, prior)
    checked_field = "event_records" if field == "events" else field
    assert candidate[checked_field][0]["unverified"] is True
    assert not quality.quality_improves(candidate, prior, body)


def test_allergy_comparison_preserves_failed_prior_salvage_and_none_refusal():
    body = "本人の血圧120/80です。合成薬Aにアレルギーがあります。"
    candidate = validation.validate({"vitals": {"sbp": 120, "dbp": 80},
        "allergies": [claim("合成薬Aにアレルギーがあります。")]}, body)
    assert quality.quality_improves(candidate, None, body)
    assert not quality.quality_improves(None, candidate, body)
    assert not quality.quality_improves(None, None, body)
    prior = validation.validate({"summary": "合成要約"}, body)
    assert quality.quality_improves(prior, {**prior, "_items_dropped": 1}, body)


def test_cloud_repair_with_allergy_candidates_replays_the_same_gain(routed, monkeypatch, tmp_path):  # noqa: F811
    quote = "合成薬Aにアレルギーがあります。"
    body = "本人に発熱と咳があります。本人の血圧120/80です。" + quote

    def add_candidate(request, answer):
        if request["reasoning"]["effort"] == "high":
            answer["allergies"] = [claim(quote)]
        return answer

    led, row, context, result, meta, qc_id, prior, chunks, sends = _repair_run(
        routed, monkeypatch, tmp_path, {"sbp": 120, "dbp": 80}, body=body, answer_hook=add_candidate)
    assert result["vitals"] == {"sbp": 120.0, "dbp": 80.0}
    assert result["allergies"][0]["unverified"] is True
    outcome = worker._commit_extraction_atomic(led, row, json.dumps(result, ensure_ascii=False), context,
        meta, {"qc": qc_id, "applied": True}, None, tuple(chunks), check_current=False,
        expected_prior=prior)
    assert outcome == "written"
    assert not led.artifacts("extract_llm_chunk", message_id=1)
    assert len(sends) == 2


def test_a_grounded_gain_keeps_a_prior_protected_allergy_candidate_unverified():
    quote = "合成薬Aで発疹はありません。"
    body = quote + "本人の血圧120/80です。"
    prior = validation.validate({"allergies": [claim(quote, negated=True)]}, body)
    candidate = validation.validate({**prior, "vitals": {"sbp": 120, "dbp": 80}}, body)
    assert quality.quality_improves(candidate, prior, body)
    assert quality.protected_lost(prior, candidate, body) == {}
    assert candidate["allergies"][0]["unverified"] is True
