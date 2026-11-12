"""Stored protected assertions are carried into a lost-shape candidate, never merged over a disagreement."""
import copy

import pytest

import extract_llm
import extract_llm_engine
import extract_quality as quality
import extract_validation
from extract_llm_prompts import _hint_coverage
from extract_llm_proofs import _quality_record_consistent

BODY = "本日、発熱はありません。"
QUOTE = "発熱はありません"
PRIOR_FEVER = {"text": "発熱", "negated": True, "status": "ongoing", "subject": "patient", "evidence": QUOTE}
FAMILY_BODY = "母は架空薬B 10mgを服用中です。"
FAMILY_MED = {"name": "架空薬B", "dose": "10mg", "action": "none", "negated": False, "status": "current",
              "subject": "family", "evidence": FAMILY_BODY}


def carry(prior, raw, body, piece=None):
    piece = body if piece is None else piece
    candidate = extract_validation.validate_piece(copy.deepcopy(raw), piece, body)
    return candidate, quality.carry_protected(
        prior, candidate, piece, body, raw=raw,
        validate_item=lambda field, item: extract_validation.validate_piece({field: [item]}, piece, body))


def test_shorter_form_of_a_protected_symptom_is_carried_and_no_longer_lost():
    prior = {"symptoms": [PRIOR_FEVER]}
    candidate, (merged, counts) = carry(prior, {"symptoms": [{"text": "発熱", "negated": True, "evidence": QUOTE}]}, BODY)
    assert quality.protected_lost(prior, candidate, BODY) == {"symptoms": 1}
    assert counts == {"carried": 1}
    assert merged["symptoms"] == [PRIOR_FEVER]
    assert quality.protected_lost(prior, merged, BODY) == {}


@pytest.mark.parametrize(("prior", "raw", "body"), [
    ({"symptoms": [PRIOR_FEVER]}, {"symptoms": [{**PRIOR_FEVER, "negated": False}]}, BODY),
    ({"symptoms": [PRIOR_FEVER]}, {"symptoms": [{**PRIOR_FEVER, "status": "resolved"}]}, BODY),
    ({"meds": [FAMILY_MED]}, {"meds": [{**FAMILY_MED, "dose": "20mg"}]}, FAMILY_BODY),
    ({"symptoms": [PRIOR_FEVER]}, {"symptoms": [{"text": "発熱", "negated": True, "severity": "mild",
                                                  "evidence": QUOTE}]}, BODY),
])
def test_any_disagreeing_attribute_keeps_the_candidate_held(prior, raw, body):
    # The validator may strip the disagreeing value; the provider's own value still disagrees.
    candidate, (merged, counts) = carry(prior, raw, body)
    assert merged is None and counts == {"conflict": 1}
    assert quality.protected_lost(prior, candidate, body)


def test_a_prior_the_current_validator_cannot_hold_is_never_added():
    prior = {"symptoms": [{**PRIOR_FEVER, "status": "resolved"}]}
    _, (merged, counts) = carry(prior, {"symptoms": []}, BODY)
    assert merged is None and counts == {"prior_unverifiable": 1}


def test_an_adjacent_label_sharing_the_quote_stays_beside_the_carried_item():
    body = "本日、発熱・咳なし。"
    fever = {**PRIOR_FEVER, "evidence": "発熱・咳なし"}
    cough = {"text": "咳", "negated": True, "status": "ongoing", "subject": "patient", "evidence": "発熱・咳なし"}
    _, (merged, counts) = carry({"symptoms": [fever]}, {"symptoms": [cough]}, body)
    assert counts == {"carried": 1}
    assert merged["symptoms"] == [cough, fever]


def test_context_quote_and_event_span_differences_keep_the_located_prior():
    body = "家族は物忘れが増えたと話す。本日転倒はなかった。"
    context = {"category": "cognition", "text": "物忘れが増えた", "subject": "family",
               "evidence": "家族は物忘れが増えたと話す。"}
    raw = {"patient_context": [{**context, "evidence": "家族は物忘れが増えたと話す"}]}
    candidate, (merged, _) = carry({"patient_context": [context]}, raw, body)
    assert quality.protected_lost({"patient_context": [context]}, merged, body) == {}
    [record] = extract_validation.validate({"event_records": [
        {"kind": "fall", "evidence": "本日転倒はなかった。"}]}, body)["event_records"]
    prior = {"event_records": [record]}
    candidate, (merged, counts) = carry(prior, {"event_records": [{"kind": "fall", "evidence": "転倒はなかった"}]}, body)
    assert quality.protected_lost(prior, candidate, body) == {"event_records": 1}
    assert counts == {"carried": 1} and merged["event_records"] == [record]
    assert quality.protected_lost(prior, merged, body) == {}


def test_a_quote_outside_the_piece_is_left_to_the_commit_hold():
    body = "経過は安定。本日、発熱はありません。"
    _, (merged, counts) = carry({"symptoms": [PRIOR_FEVER]}, {"summary": "合成"}, body, piece="経過は安定。")
    assert counts == {} and "symptoms" not in merged


def _model(document, calls):
    def model(prompt, **kwargs):
        calls.append(kwargs.get("provider"))
        return copy.deepcopy(document)
    return model


def test_local_extraction_carries_and_its_completeness_claim_matches_the_result(monkeypatch):
    calls = []
    monkeypatch.setattr(extract_llm_engine, "_llm_call", _model(
        {"summary": "合成", "symptoms": [{"text": "発熱", "negated": True, "evidence": QUOTE}]}, calls))
    meta = {}
    result = extract_llm.llm_extract(BODY, meta_out=meta, protected_prior={"symptoms": [PRIOR_FEVER]})
    assert result["symptoms"] == [PRIOR_FEVER] and meta["protected_carried"] >= 1
    assert "protected_conflict" not in meta
    claim = meta["completeness"]["0"]
    assert _quality_record_consistent(
        {key: claim[key] for key in ("state", "remaining_hint_candidates")},
        _hint_coverage(result, extract_llm._rule_hints({"body_text": BODY, "posted_at": None}), BODY))


def test_local_conflict_leaves_the_candidate_unchanged_and_is_counted(monkeypatch):
    raw = {"summary": "合成", "symptoms": [{**PRIOR_FEVER, "negated": False}]}
    monkeypatch.setattr(extract_llm_engine, "_llm_call", _model(raw, []))
    meta = {}
    result = extract_llm.llm_extract(BODY, meta_out=meta, protected_prior={"symptoms": [PRIOR_FEVER]})
    assert meta["protected_conflict"] >= 1 and "protected_carried" not in meta
    assert quality.protected_lost({"symptoms": [PRIOR_FEVER]}, result, BODY) == {"symptoms": 1}


def test_cloud_results_are_never_carried(monkeypatch):
    calls = []
    monkeypatch.setattr(extract_llm_engine, "_llm_call", _model(
        {"summary": "合成", "symptoms": [{"text": "発熱", "negated": True, "evidence": QUOTE}]}, calls))
    meta = {}
    provider = object()
    result = extract_llm.llm_extract(BODY, meta_out=meta, provider=provider,
                                     protected_prior={"symptoms": [PRIOR_FEVER]})
    assert calls and all(call is provider for call in calls)
    assert "protected_carried" not in meta and "protected_conflict" not in meta
    assert quality.protected_lost({"symptoms": [PRIOR_FEVER]}, result, BODY) == {"symptoms": 1}


def test_a_quote_across_a_chunk_boundary_is_not_carried(monkeypatch):
    body = "経過は安定。本日、発熱はありません。"
    monkeypatch.setattr(extract_llm_engine, "_llm_call", _model({"symptoms": []}, []))
    meta = {}
    result = extract_llm.llm_extract(body, meta_out=meta, chunk_size=9,
                                     protected_prior={"symptoms": [PRIOR_FEVER]})
    assert meta["completeness_chunks"] == 3 and "protected_carried" not in meta
    assert quality.protected_lost({"symptoms": [PRIOR_FEVER]}, result, body) == {"symptoms": 1}
