"""Synthetic allergy retention and thinness checks using the shared clinical quality paths."""
import copy

import pytest

import extract_quality as quality
import extract_validation as validation


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
