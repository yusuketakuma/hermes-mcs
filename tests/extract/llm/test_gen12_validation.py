"""Synthetic allergy-field integration without promoting a model claim to a confirmed fact."""
import copy

import pytest

import clinical_allergies
import extract_validation as validation
import inference_version


def claim(body, **changes):
    return {"allergen": "合成薬A", "kind": "allergy", "reaction": None,
            "subject": "patient", "status": "current", "negated": False,
            "certainty": "confirmed", "evidence": body, **changes}


@pytest.mark.parametrize("body,axes", [
    ("本人は合成薬Aにアレルギーがあります。", {}),
    ("家族は合成薬Aにアレルギーがあります。", {"subject": "family"}),
    ("本人は合成薬Aにアレルギーはありません。", {"negated": True}),
    ("明日、合成薬Aのアレルギーを確認します。", {"status": "planned"}),
    ("別の人は合成薬Aにアレルギーがあります。", {"subject": "other"}),
])
def test_allergy_claims_keep_axes_and_are_never_confirmed(body, axes):
    item = claim(body, **axes)
    document = {"allergies": [item]}
    before = copy.deepcopy(document)
    checked = validation.validate(document, body)
    assert checked == {"allergies": [{**item, "unverified": True}]}
    assert document == before
    generation = inference_version.LLM_EXTRACT_VERSION
    assert clinical_allergies.stored_assertions(
        {"extract_version": generation}, checked, first_generation=generation) == []


def test_absent_allergy_field_preserves_the_legacy_validation_shape():
    document = {"symptoms": []}
    assert validation.validate(document, "合成本文。") == {"symptoms": []}
    assert validation.merge([document, document]) == {"symptoms": []}
    assert validation.merge([{}, {}]) == {}
    assert validation.validate_piece(document, "合成本文。", "前置き。合成本文。") == {
        "symptoms": [], "_items_dropped": 0, "_evidence_dropped": 0}
    assert validation.validate({"allergies": []}, "合成本文。") == {"allergies": []}
    assert validation.merge([{}, {"allergies": []}]) == {"allergies": []}


@pytest.mark.parametrize("bad", [None, {}, "合成薬A", True])
def test_bad_allergy_field_uses_existing_drop_accounting(bad):
    drops = {}
    checked = validation.validate({"allergies": bad, "symptoms": []}, "合成本文。", drops)
    assert checked is None
    assert drops["items"] == ["allergies"]


def test_absent_allergen_is_dropped_without_erasing_valid_sibling():
    body = "本人は合成薬Aにアレルギーがあります。"
    drops = {}
    checked = validation.validate({"allergies": [claim(body), claim(body, allergen="合成薬B")]}, body, drops)
    assert checked["allergies"] == [{**claim(body), "unverified": True}]
    assert checked["_items_dropped"] == 1
    assert drops["items"] == ["allergies"]
    assert len(drops["reasons"]) == 1


def test_reference_context_never_supplies_an_allergen_absent_from_target():
    body = "本人は落ち着いています。"
    context = "合成薬Aにアレルギーがあります。"
    drops = {}
    assert validation.validate({"allergies": [claim(context)]}, body, drops, ctx=True) is None
    assert drops["items"] == ["allergies"]


def test_piece_validation_does_not_reintroduce_an_allergen_from_another_piece():
    piece = "本人は合成薬Aにアレルギーがあります。"
    other = "合成薬Bで副作用がありました。"
    checked = validation.validate_piece({"allergies": [claim(piece), claim(other, allergen="合成薬B")]},
                                        piece, piece + other, source_offset=0)
    assert checked["allergies"] == [{**claim(piece), "unverified": True}]
    assert checked["_items_dropped"] == 1


@pytest.mark.parametrize("reactions", [(None, "発疹", "咳", "発疹"), ("発疹", "咳", None, "発疹")])
def test_chunk_merge_uses_same_assertion_without_erasing_distinct_reactions(reactions):
    body = "合成薬Aのアレルギーについて発疹と咳を記録しました。"
    items = [{**claim(body, reaction=reaction), "unverified": True} for reaction in reactions]
    family = {**items[0], "subject": "family", "reaction": "発疹"}
    chunks = [{"allergies": [item]} for item in [*items, family]]
    before = copy.deepcopy(chunks)
    checked = validation.merge(chunks)
    assert len(checked["allergies"]) == 3
    assert {(item["subject"], item["reaction"]) for item in checked["allergies"]} == {
        ("patient", "発疹"), ("patient", "咳"), ("family", "発疹")}
    assert all(item["unverified"] is True for item in checked["allergies"])
    assert chunks == before
