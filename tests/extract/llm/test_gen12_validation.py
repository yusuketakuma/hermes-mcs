"""Generation-12 allergies through validate(): kept claims stay unverified candidates, nothing is ever confirmed."""
import pytest

import extract_validation

BODY = "ご本人は合成薬Eにアレルギーがあります。内服は継続します。"


def _claim(**changes):
    claim = {"allergen": "合成薬E", "kind": "allergy", "reaction": None, "subject": "patient",
             "status": "current", "negated": False, "certainty": "suspected", "evidence": BODY}
    claim.update(changes)
    return claim


def test_a_located_claim_passes_validate_as_an_unverified_candidate():
    drops = {}
    out = extract_validation.validate({"allergies": [_claim()]}, BODY, drops=drops)
    assert out["allergies"] == [{**_claim(), "unverified": True}]
    assert not drops and not out.get("_items_dropped")


@pytest.mark.parametrize("changes", [
    {"subject": "family"},
    {"subject": "other"},
    {"negated": True},
    {"status": "planned"},
])
def test_a_forbidden_claim_is_kept_only_as_an_unverified_candidate(changes):
    out = extract_validation.validate({"allergies": [_claim(**changes)]}, BODY)
    assert all(item.get("unverified") is True for item in out["allergies"])


def test_a_claim_whose_allergen_is_not_in_the_body_is_dropped():
    drops = {}
    out = extract_validation.validate(
        {"allergies": [_claim(), _claim(allergen="合成薬Z")]}, BODY, drops=drops)
    assert out["allergies"] == [{**_claim(), "unverified": True}] and out["_items_dropped"] == 1
    assert drops["items"] == ["allergies"]
    assert extract_validation.validate({"allergies": [_claim(allergen="合成薬Z")]}, BODY) is None


def test_a_non_list_allergies_field_is_dropped():
    out = extract_validation.validate({"allergies": "あり"}, BODY)
    assert out is None


def test_validate_piece_carries_allergies_through():
    piece = BODY + " 続きの文です。"
    out = extract_validation.validate_piece({"allergies": [_claim()]}, piece, BODY)
    assert out["allergies"] == [{**_claim(), "unverified": True}]


def test_merge_dedupes_allergy_candidates_across_chunks():
    first = extract_validation.validate({"allergies": [_claim()]}, BODY)
    second = extract_validation.validate({"allergies": [_claim(), _claim(reaction="発疹")]}, BODY)
    merged = extract_validation.merge([first, second])
    assert merged["allergies"] == [{**_claim(), "unverified": True},
                                   {**_claim(reaction="発疹"), "unverified": True}]
