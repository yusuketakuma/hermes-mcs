"""Stored source-bound assertions survive stricter candidate validation."""
import copy

import pytest

import extract_quality as quality


BODY = "家族には発熱があります。本人に息切れはありません。"
FAMILY = {"text": "発熱", "subject": "family", "negated": False,
          "evidence": "家族には発熱があります。"}
NEGATIVE = {"text": "息切れ", "subject": "patient", "negated": True,
            "evidence": "本人に息切れはありません。"}


def test_stricter_validator_cannot_remove_the_stored_protection_baseline(monkeypatch):
    prior = {"symptoms": [FAMILY, NEGATIVE]}
    candidate = {"symptoms": [copy.deepcopy(FAMILY), copy.deepcopy(NEGATIVE)]}
    original = copy.deepcopy(prior)
    calls = []

    def stricter(document, body):
        assert document is candidate and body == BODY
        calls.append(document)
        return {"symptoms": [copy.deepcopy(NEGATIVE)]}

    monkeypatch.setattr(quality.extract_validation, "validate", stricter)
    assert quality.protected_lost(prior, candidate, BODY) == {"symptoms": 1}
    assert len(calls) == 1 and prior == original


@pytest.mark.parametrize("items", [[FAMILY], [NEGATIVE], [FAMILY, NEGATIVE]])
def test_retained_assertions_and_added_attributes_do_not_report_loss(monkeypatch, items):
    prior = {"symptoms": copy.deepcopy(items)}
    candidate = {"symptoms": [{**item, "severity": "mild"} for item in items]}
    monkeypatch.setattr(quality.extract_validation, "validate", lambda document, body: document)
    assert quality.protected_lost(prior, candidate, BODY) == {}


@pytest.mark.parametrize("item", [FAMILY, NEGATIVE, {**FAMILY, "subject": "other"}])
def test_dropped_negation_or_nonpatient_assertion_is_still_protected(monkeypatch, item):
    monkeypatch.setattr(quality.extract_validation, "validate", lambda document, body: {})
    assert quality.protected_lost({"symptoms": [item]}, {}, BODY) == {"symptoms": 1}


@pytest.mark.parametrize("prior", [None, [], "invalid", 1,
    {"symptoms": True}, {"symptoms": "invalid"}, {"symptoms": FAMILY},
    {"symptoms": [None, [], 7, "invalid", {}]},
    {"symptoms": [{**FAMILY, "evidence": None}]},
    {"symptoms": [{**FAMILY, "evidence": 3}]},
    {"symptoms": [{**FAMILY, "evidence": []}]},
    {"symptoms": [{**FAMILY, "evidence": "別の原文にしかない引用"}]},
    {"symptoms": [{"text": "発熱", "subject": "patient", "negated": False,
                   "evidence": FAMILY["evidence"]}]},
    {"symptoms": [{"text": "発熱", "subject": [], "negated": 1,
                   "evidence": FAMILY["evidence"]}]},
])
def test_malformed_or_unlocated_or_nonprotected_prior_does_not_freeze_candidate(monkeypatch, prior):
    monkeypatch.setattr(quality.extract_validation, "validate", lambda document, body: {})
    assert quality.protected_lost(prior, {}, BODY) == {}


def test_unrelated_and_ambiguous_source_quotes_supply_no_protection(monkeypatch):
    monkeypatch.setattr(quality.extract_validation, "validate", lambda document, body: {})
    prior = {"symptoms": [FAMILY]}
    assert quality.protected_lost(prior, {}, "本人は落ち着いています。") == {}
    assert quality.protected_lost(prior, {}, FAMILY["evidence"] * 2) == {}


def test_malformed_nested_assertion_keys_do_not_crash_comparison(monkeypatch):
    prior = {"symptoms": [{**FAMILY, "extra": {1: "invalid"}}]}
    monkeypatch.setattr(quality.extract_validation, "validate", lambda document, body: {"symptoms": [FAMILY]})
    # A corrupt extra attribute is never silently declared retained.
    assert quality.protected_lost(prior, {}, BODY) == {"symptoms": 1}


@pytest.mark.parametrize("body", [None, [], 3])
def test_missing_source_body_never_supplies_a_protection_baseline(body):
    assert quality.protected_lost({"symptoms": [FAMILY]}, {}, body) == {}


def test_same_source_reanchoring_can_restore_protected_axes_without_normal_repair_trigger():
    import extract_validation
    from extract_llm_prompts import _response_has_required_fields
    from extract_llm_source_contract import _rule_hints

    body = "父は昨日、咳がありました。父は現在も同じ咳が続いています。"
    symptom = {"text": "咳", "subject": "family", "negated": False}
    prior = {"symptoms": [{**symptom, "status": "ongoing", "evidence": "父は昨日、咳がありました。"}]}
    first = {"symptoms": [{**symptom, "evidence": "父は現在も同じ咳が続いています。"}]}
    repaired = {"symptoms": [{**first["symptoms"][0], "status": "ongoing"}]}
    assert _response_has_required_fields(first) and _response_has_required_fields(repaired)
    drops = {}
    initial = extract_validation.validate(first, body, drops=drops)
    current = extract_validation.validate(repaired, body)
    assert quality.protected_lost(prior, prior, body) == {"symptoms": 1}
    assert initial is not None and not drops
    assert not quality.is_thin(initial, body)
    assert not quality.completeness_issues(initial, _rule_hints(body), body)
    assert quality.protected_lost(prior, initial, body) == {"symptoms": 1}
    assert current is not None and not current.get('_items_dropped') and not current.get('_evidence_dropped')
    assert not any(item.get('unverified') for item in current['symptoms'])
    assert quality.protected_lost(prior, current, body) == {}
    assert quality.improves(current, initial)
