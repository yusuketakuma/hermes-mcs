"""Stored medication projections recheck dose uncertainty without rewriting reported fields."""
import copy

import pytest

import clinical_scope
import clinical_values
from patient_context_projection import clinical_state_fact
import structured_view


@pytest.mark.parametrize("body,evidence", [
    ("本人の架空薬A5mgは継続です。服用量は記載されていません。", "本人の架空薬A5mgは継続です。"),
    ("本人の架空薬A5mgは継続です、用量は不明です。", "本人の架空薬A5mgは継続です"),
    ("本人の架空薬A5mgの用量は不明です。", "本人の架空薬A5mgの用量は不明です。"),
    ("本人の架空薬A5mgは継続です。服用量は記載されていません。架空薬Aの残薬を確認します。",
     "本人の架空薬A5mgは継続です。"),
])
def test_old_strength_is_not_a_confirmed_dose_when_source_says_unrecorded(body, evidence):
    message = {"project_id": 1, "message_id": 1, "content_hash": "synthetic",
               "body_text": body, "body_state": "full"}
    item = {"name": "架空薬A", "dose": "5mg", "subject": "patient", "status": "current",
            "action": "continue", "negated": False, "evidence": evidence}
    original = copy.deepcopy(item)
    fact = clinical_state_fact(message, item, "medications")
    assert not any(detail["key"] == "actual_dose" for detail in fact["details"])
    assert fact["temporal"]["unknown"] is True
    assert fact["reported"] == original == item


@pytest.mark.parametrize("tail", [
    "架空薬Bの用量は不明です。", "別架空薬Aの用量は不明です。",
    "用量は不明ではありません。", "",
])
def test_other_drug_or_negated_uncertainty_does_not_erase_old_supported_dose(tail):
    evidence = "本人は架空薬Aを1錠服用しています。"
    message = {"project_id": 1, "message_id": 1, "content_hash": "synthetic",
               "body_text": evidence + tail, "body_state": "full"}
    item = {"name": "架空薬A", "dose": "1錠", "subject": "patient", "status": "current",
            "action": "continue", "negated": False, "evidence": evidence}
    fact = clinical_state_fact(message, item, "medications")
    assert any(detail["key"] == "actual_dose" and detail["value"] == "1錠" for detail in fact["details"])
    assert fact["reported"] == item


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("tail", ["", "架空薬Aの残薬を確認します。"])
def test_saved_unrecorded_dose_is_labelled_without_changing_the_item(legacy, tail):
    evidence = "本人の架空薬A5mgは継続です。"
    body = evidence + "服用量は記載されていません。" + tail
    item = {"name": "架空薬A", "dose": "5mg", "subject": "patient", "status": "current",
            "action": "continue", "negated": False, "evidence": evidence}
    original = copy.deepcopy(item)
    llm, v1 = ({}, {"medications": [item]}) if legacy else ({"meds": [item]}, {})
    confirmed, unverified = structured_view._med_entries(llm, v1, context=(body, None, "patient"))
    rows = unverified if legacy else confirmed
    assert len(rows) == 1 and "用量要確認: 5mg" in rows[0][0]
    assert item == original


@pytest.mark.parametrize("span_key", ["evidence_span", "span"])
def test_explicit_source_span_selects_its_own_repeated_quote(span_key):
    evidence = "本人の架空薬A5mgは継続です。"
    body = evidence + "用量は不明です。" + evidence
    item = {"name": "架空薬A", "dose": "5mg", "subject": "patient", "status": "current",
            "action": "continue", "negated": False, "evidence": evidence}
    bounds = [0, len(evidence)]
    item[span_key] = bounds if span_key == "evidence_span" else {"start": bounds[0], "end": bounds[1]}
    message = {"project_id": 1, "message_id": 1, "content_hash": "synthetic",
               "body_text": body, "body_state": "full"}
    fact = clinical_state_fact(message, item, "medications")
    assert not any(detail["key"] == "actual_dose" for detail in fact["details"])
    assert fact["temporal"]["unknown"] is True
    assert "用量要確認" in structured_view._med_display_name(item, body=body)
    later = [len(body) - len(evidence), len(body)]
    item[span_key] = later if span_key == "evidence_span" else {"start": later[0], "end": later[1]}
    assert "用量要確認" not in structured_view._med_display_name(item, body=body)


@pytest.mark.parametrize("anchor", [[], [0], [-1, 3], [True, 3], [0, 999], [3, 0], [0, 2], "0:3"])
def test_invalid_drug_anchor_cannot_bind_an_unrelated_source(anchor):
    assert clinical_scope.source_assertion_span("架空薬Aの用量は不明です。", "架空薬A", anchor=anchor) is None


def test_source_span_must_match_the_supplied_literal_evidence():
    assert not clinical_values.medication_source_dose_unrecorded(
        "架空薬Aの用量は不明です。", "架空薬A", evidence="別の引用", evidence_span=[0, 4])
