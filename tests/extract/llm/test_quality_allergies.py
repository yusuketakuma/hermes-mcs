"""Generation-12 allergies in the quality decisions: thinness, protected assertions and improves."""
import copy

import extract_quality as quality


BODY = "ご本人は合成薬Eにアレルギーがあります。合成薬Fでも発疹が出たことがあります。"


def _claim(allergen, evidence, **changes):
    claim = {"allergen": allergen, "kind": "allergy", "reaction": None, "subject": "patient",
             "status": "current", "negated": False, "certainty": "suspected",
             "evidence": evidence, "unverified": True}
    claim.update(changes)
    return claim


ALLERGY_E = _claim("合成薬E", "合成薬Eにアレルギーがあります。")
ALLERGY_F = _claim("合成薬F", "合成薬Fでも発疹が出たことがあります。", kind="adverse_reaction")


def test_a_long_post_of_only_allergy_candidates_is_not_thin():
    body = BODY + "補足の記録です。" * 40
    document = {"allergies": [copy.deepcopy(ALLERGY_E), copy.deepcopy(ALLERGY_F)]}
    assert quality.thin_fact_count(document) == 2
    assert not quality.is_thin(document, body)


def test_an_added_attribute_is_not_a_protected_loss(monkeypatch):
    # A generation-11 stored artifact (negated drug) vs the generation-12
    # candidate that only adds the timing attribute.
    prior = {"meds": [{"name": "合成薬E", "action": "none", "subject": "patient",
                       "negated": True, "evidence": "合成薬Eにアレルギーがあります。"}]}
    candidate = {"meds": [{**prior["meds"][0], "timing": "朝"}]}
    monkeypatch.setattr(quality.extract_validation, "validate", lambda document, body: document)
    assert quality.protected_lost(prior, candidate, BODY) == {}


def test_a_dropped_negated_allergy_reports_protected_loss(monkeypatch):
    prior = {"allergies": [{**ALLERGY_E, "negated": True}]}
    monkeypatch.setattr(quality.extract_validation, "validate", lambda document, body: document)
    assert quality.protected_lost(prior, {"allergies": []}, BODY) == {"allergies": 1}
    assert not quality.improves({"allergies": []}, prior)


def test_a_kept_negated_allergy_reports_no_loss(monkeypatch):
    prior = {"allergies": [{**ALLERGY_E, "negated": True}]}
    candidate = {"allergies": [copy.deepcopy(prior["allergies"][0])]}
    monkeypatch.setattr(quality.extract_validation, "validate", lambda document, body: document)
    assert quality.protected_lost(prior, candidate, BODY) == {}
