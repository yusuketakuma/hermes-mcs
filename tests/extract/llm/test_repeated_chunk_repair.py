"""Repeated source chunks cannot yield a globally unique evidence quote."""
import extract_quality
import extract_validation
import copy

import pytest

import extract_llm


PIECE = "本人に痛みがあります。"


def symptom(quote=PIECE):
    return {"text": "痛み", "status": "ongoing", "subject": "patient",
            "negated": False, "evidence": quote}


def run_chunks(monkeypatch, document, pieces=None, **kwargs):
    pieces = pieces or [PIECE, PIECE]
    body = "".join(pieces)
    calls = []
    kwargs.setdefault("chunk_size", max(map(len, pieces)))
    assert extract_llm.plan_chunks(body, kwargs["chunk_size"]) == pieces
    monkeypatch.setattr(extract_llm, "_llm_call",
                        lambda *a, **k: calls.append(a[0]) or copy.deepcopy(document))
    out = extract_llm.llm_extract(body, **kwargs)
    return out, calls


def test_repeated_chunk_skips_only_impossible_evidence_repair(monkeypatch):
    document = {"summary": "経過の連絡", "symptoms": [symptom()]}
    assert extract_validation.validate(document, PIECE)["symptoms"][0]["evidence"] == PIECE
    out, calls = run_chunks(monkeypatch, document)
    assert len(calls) == 2
    assert out["_evidence_dropped"] == 2
    assert out["symptoms"][0]["unverified"] is True
    assert "evidence" not in out["symptoms"][0]


@pytest.mark.parametrize("field,item", [
    ("patient_context", [{"category": "observations", "subject": "patient",
                           "text": "痛みがあります", "evidence": PIECE}]),
    ("reply", {"kind": "ack", "evidence": PIECE}),
])
def test_ambiguous_context_and_reply_remain_rejected_without_repair(monkeypatch, field, item):
    out, calls = run_chunks(monkeypatch, {"summary": "経過の連絡", field: item})
    assert len(calls) == 2
    assert not out.get(field)
    assert out["_items_dropped"] == 2


@pytest.mark.parametrize("extra", [
    {"symptoms": [symptom("本文にない引用")]},
    {"symptoms": "invalid"},
    {"patient_context": [{"category": "invalid", "subject": "patient",
                           "text": "痛み", "evidence": PIECE}]},
])
def test_local_missing_evidence_and_schema_repairs_are_preserved(monkeypatch, extra):
    _, calls = run_chunks(monkeypatch, {"summary": "経過の連絡", **extra})
    assert len(calls) == 4


def test_a_unique_chunk_can_repair_with_a_longer_quote(monkeypatch):
    pieces = ["午前：" + PIECE, "午後：" + PIECE]
    calls = []

    def answer(prompt, **kwargs):
        index = len(calls)
        calls.append(prompt)
        quote = pieces[index // 2] if index % 2 else PIECE
        return {"summary": "経過の連絡", "symptoms": [symptom(quote)]}

    monkeypatch.setattr(extract_llm, "_llm_call", answer)
    out = extract_llm.llm_extract("".join(pieces), chunk_size=len(pieces[0]))
    assert len(calls) == 4
    assert out["symptoms"][0]["evidence"] == pieces[-1]
    assert not out["symptoms"][0].get("unverified")


def test_completeness_repair_is_preserved_for_repeated_chunks(monkeypatch):
    monkeypatch.setattr(extract_quality, 'completeness_issues', lambda *a: ["必要項目が未収載"])
    _, calls = run_chunks(monkeypatch, {"summary": "経過の連絡", "symptoms": [symptom()]})
    assert len(calls) == 4


def test_globally_empty_validation_still_repairs_and_fails(monkeypatch):
    out, calls = run_chunks(monkeypatch, {"patient_context": [
        {"category": "observations", "subject": "patient", "text": "痛みがあります",
         "evidence": PIECE}]})
    assert out is None
    assert len(calls) == 2


def test_mixed_local_and_global_evidence_loss_still_repairs(monkeypatch):
    _, calls = run_chunks(monkeypatch, {"summary": "経過の連絡",
                                       "symptoms": [symptom(), symptom("本文にない引用")]})
    assert len(calls) == 4


def test_single_body_thin_repair_remains(monkeypatch):
    monkeypatch.setattr(extract_quality, 'is_thin', lambda *a: True)
    _, calls = run_chunks(monkeypatch, {"summary": "経過の連絡"}, [PIECE * 2])
    assert len(calls) == 2


def test_ambiguity_inside_one_core_still_requests_a_repair(monkeypatch):
    out, calls = run_chunks(monkeypatch, {"summary": "経過の連絡", "symptoms": [symptom()]},
                            [PIECE * 2])
    assert len(calls) == 2
    assert out["symptoms"][0]["unverified"] is True


def test_ambiguous_urgency_is_still_unclear(monkeypatch):
    out, calls = run_chunks(monkeypatch, {"summary": "経過の連絡", "urgency": "high",
                                        "urgency_evidence": [PIECE]})
    assert len(calls) == 2
    assert out["urgency"] == "unclear"
    assert not out.get("urgency_evidence")
    assert out["_evidence_dropped"] == 2
