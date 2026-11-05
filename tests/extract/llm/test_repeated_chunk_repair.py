"""Repeated source chunks cannot yield a globally unique evidence quote."""
import extract_llm_engine
import extract_llm_runner
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
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
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
    out, calls = run_chunks(monkeypatch, {"summary": "経過の連絡", "symptoms": [], field: item})
    assert len(calls) == 2
    assert not out.get(field)
    assert out["_items_dropped"] == 2


@pytest.mark.parametrize("extra", [
    {"symptoms": [symptom("本文にない引用")]},
    {"symptoms": "invalid"},
    {"symptoms": [], "patient_context": [{"category": "invalid", "subject": "patient",
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
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    out = extract_llm.llm_extract("".join(pieces), chunk_size=len(pieces[0]))
    assert len(calls) == 4
    symptoms = out["symptoms"]
    assert [item["evidence"] for item in symptoms] == pieces
    for piece, item in zip(pieces, symptoms, strict=True):
        quote = item["evidence"]
        assert quote == piece and "".join(pieces).count(quote) == 1
        assert not item.get("unverified")


def test_completeness_repair_is_preserved_for_repeated_chunks(monkeypatch):
    monkeypatch.setattr(extract_quality, 'completeness_issues', lambda *a: ["必要項目が未収載"])
    _, calls = run_chunks(monkeypatch, {"summary": "経過の連絡", "symptoms": [symptom()]})
    assert len(calls) == 4


def test_globally_empty_validation_still_repairs_and_fails(monkeypatch):
    validations = []
    validate = extract_validation.validate

    def observe_validation(document, source, *args, **kwargs):
        result = validate(document, source, *args, **kwargs)
        validations.append((source, copy.deepcopy(result)))
        return result

    monkeypatch.setattr(extract_validation, "validate", observe_validation)
    document = {"symptoms": [], "patient_context": [
        {"category": "observations", "subject": "patient", "text": "痛みがあります",
         "evidence": PIECE}]}
    out, calls = run_chunks(monkeypatch, document)
    assert out is None
    assert len(calls) == 2
    assert [source for source, _ in validations] == [PIECE, PIECE * 2] * 2
    for source, result in validations:
        if source == PIECE:
            assert result["patient_context"] == document["patient_context"]
            assert result["symptoms"] == []
        else:
            assert result is None


def test_mixed_local_and_global_evidence_loss_still_repairs(monkeypatch):
    _, calls = run_chunks(monkeypatch, {"summary": "経過の連絡",
                                       "symptoms": [symptom(), symptom("本文にない引用")]})
    assert len(calls) == 4


def test_single_body_thin_repair_remains(monkeypatch):
    document = {"summary": "経過の連絡", "symptoms": []}
    body = PIECE * 2 + "合成背景の文章です。" * 30
    meta = {}
    out, calls = run_chunks(monkeypatch, document, [body], meta_out=meta)
    assert out == document
    assert len(calls) == 2
    assert meta["repair_issues"]["thin"] == 1
    assert meta["repair_issues"]["structure"] == 0
    assert meta["thin"] is True
    assert "本文の情報量に対し抽出項目が少なすぎます" in calls[1]


def test_ambiguity_inside_one_core_still_requests_a_repair(monkeypatch):
    out, calls = run_chunks(monkeypatch, {"summary": "経過の連絡", "symptoms": [symptom()]},
                            [PIECE * 2])
    assert len(calls) == 2
    assert out["symptoms"][0]["unverified"] is True


def test_ambiguous_urgency_is_still_unclear(monkeypatch):
    out, calls = run_chunks(monkeypatch, {"summary": "経過の連絡", "symptoms": [], "urgency": "high",
                                        "urgency_evidence": [PIECE]})
    assert len(calls) == 2
    assert out["urgency"] == "unclear"
    assert not out.get("urgency_evidence")
    assert out["_evidence_dropped"] == 2
