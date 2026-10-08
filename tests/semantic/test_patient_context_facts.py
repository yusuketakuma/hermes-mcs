"""Semantic detail attributes remain quote-bound and subject-bound behind existing fences."""
import copy
import json

import pytest

import semantic_extraction as extraction
import semantic_audit
import semantic_facts as sf
import semantic_llm
import semantic_projection as projection
from semantic_store import bundle_fingerprint
from test_semantic_facts_v2 import FP, _FakeLedger, _llm, _member


BODY = "家族は架空サプリを昨夜1袋摂取したと報告しています。"
CONTEXT = {"category": "medication_management", "text": "架空サプリを昨夜1袋摂取した",
           "details": [{"key": "actual_dose", "value": "1袋", "evidence": "昨夜1袋摂取"},
                       {"key": "actual_frequency", "value": "昨夜", "evidence": "昨夜1袋摂取"}]}


def _fact(**changes):
    fact = {"statement": BODY, "kind": "adherence_administration", "subject_role": "family",
            "polarity": "affirmed", "epistemic": "reported", "workflow_status": "reported",
            "evidence_quote": BODY, "patient_context": copy.deepcopy(CONTEXT)}
    fact.update(changes)
    return fact


def _extract(fact=None, **options):
    return extraction.extract_facts_v2(
        _llm([fact or _fact()], presence={"adherence_administration": "one"}),
        _member(BODY), **options)


def test_semantic_detail_preserves_attributes_without_asserting_patient_use():
    result = _extract()
    doc = sf.validate_facts_doc(result["doc"])
    fact = next(f for f in doc["facts"] if f["provenance"] == "local_llm")
    assert fact["subject"] == "role:family"
    assert fact["patient_context"]["subject"] == "family"
    assert fact["patient_context"]["evidence"] == BODY
    assert fact["patient_context"]["details"] == CONTEXT["details"]
    out = projection.project_v2_doc_legacy(doc)
    assert out["patient_context"][0]["subject"] == "family"
    assert out["patient_context"][0]["details"] == CONTEXT["details"]
    assert "meds" not in out  # Actual use is an observation, not a prescription.


@pytest.mark.parametrize("bad", [
    {"category": "unknown"}, {"text": "根拠に存在しない架空属性"},
    {"details": [{"key": "actual_dose", "value": "99袋", "evidence": "昨夜1袋摂取"}]},
    {"details": [{"key": "unknown", "value": "1袋", "evidence": "昨夜1袋摂取"}]},
    {"extra": "unsupported"},
])
def test_ungrounded_or_unknown_optional_attributes_keep_chunk_nonpass(bad):
    result = _extract(_fact(patient_context={**CONTEXT, **bad}))
    assert not result["extraction_complete"] and not result["complete"]
    assert result["failed_chunks"] and result["dropped_in_failed_chunk"]


def test_fact_and_document_revalidate_attribute_subject_and_own_evidence():
    doc = _extract()["doc"]
    fact = next(f for f in doc["facts"] if f["provenance"] == "local_llm")
    wrong_subject = copy.deepcopy(fact)
    wrong_subject["patient_context"]["subject"] = "patient"
    with pytest.raises(sf.ContractError, match="patient_context_invalid"):
        sf.validate_fact(wrong_subject)
    foreign = copy.deepcopy(doc)
    foreign["facts"][0]["patient_context"].update(text="架空別の引用", evidence="架空別の引用", details=[])
    assert sf.validate_fact(foreign["facts"][0])["patient_context"]["text"] == "架空別の引用"
    with pytest.raises(sf.ContractError, match="patient_context_ungrounded"):
        sf.validate_facts_doc(foreign)


def test_projection_keeps_legacy_quote_but_never_unverified_context():
    result = _extract()
    old = copy.deepcopy(result["doc"])
    for fact in old["facts"]:
        fact.pop("patient_context", None)
    out = projection.project_v2_doc_legacy(old)
    assert out["patient_context"][0]["text"] == BODY
    assert out["patient_context"][0]["category"] == "medication_management"
    for fact in old["facts"]:
        fact["validation_status"] = "unverified"
    assert "patient_context" not in projection.project_v2_doc_legacy(old)


def test_new_prompt_changes_bundle_identity_and_never_reuses_old_chunks(monkeypatch):
    prompt = semantic_llm._FACT_V2_PROMPT
    ledger = _FakeLedger()
    member = _member(BODY)
    new_fingerprint = bundle_fingerprint([member], local_model="synthetic-model")
    monkeypatch.setattr(semantic_llm, "_FACT_V2_PROMPT", "synthetic legacy fact prompt: %s")
    old_fingerprint = bundle_fingerprint([member], local_model="synthetic-model")
    # A genuine old producer omitted the optional key, rather than emitting null.
    fact = _fact()
    fact.pop("patient_context")
    old = _extract(fact, ledger=ledger, source_fingerprint=FP)
    assert old["extraction_complete"]
    monkeypatch.setattr(semantic_llm, "_FACT_V2_PROMPT", prompt)
    new = _extract(ledger=ledger, source_fingerprint=FP)
    assert new_fingerprint != old_fingerprint
    assert new["extraction_complete"] and not new["reused_chunks"]
    assert projection.PROJECTION_VERSION > 2


def test_duplicate_fact_contexts_union_grounded_attributes():
    result = _extract()
    fact = next(f for f in result["doc"]["facts"] if f["provenance"] == "local_llm")
    left, right = copy.deepcopy(fact), copy.deepcopy(fact)
    left["patient_context"]["details"] = CONTEXT["details"][:1]
    right["patient_context"]["details"] = CONTEXT["details"][1:]
    merged = extraction._merge_dupe_facts([left, right])
    assert len(merged) == 1 and merged[0]["patient_context"]["details"] == CONTEXT["details"]


def test_detail_values_enter_the_same_audit_target_and_document_hash():
    from semantic_v4 import _doc_hash
    from test_semantic_facts_audit import _Jev

    doc = _extract()["doc"]
    calls = []

    class Capture(_Jev):
        def evaluate(self, state, questions, deadline):
            calls.append(state)
            return super().evaluate(state, questions, deadline)

    semantic_audit.audit_facts_v2(Capture(), doc, BODY, deadline=10 ** 9)
    targets = [state["target"]["text"] for state in calls if state["target"]["id"] != "source"]
    assert any('"actual_dose"' in target and '"1袋"' in target for target in targets)
    assert len(calls) == sum(f["validation_status"] == "verified" for f in doc["facts"]) + 1
    changed = copy.deepcopy(doc)
    changed["facts"][0]["patient_context"]["details"] = CONTEXT["details"][:1]
    assert _doc_hash(changed) != _doc_hash(doc)
    raw = copy.deepcopy(doc)
    raw["facts"][0]["patient_context"]["subject"] = "patient"
    rejected = semantic_audit.audit_facts_v2(Capture(), raw, BODY, deadline=10 ** 9)
    assert rejected["status"] != "PASS"
    assert any(item["code"] == "fact_patient_context_invalid" for item in rejected["findings"])


def test_active_v4_pass_publishes_new_context_through_existing_stage_fences(tmp_path):
    import semantic_v4
    from semantic_testkit import _PassJev, _drain, _llm_v2, _seeded_two

    db = _seeded_two(tmp_path)

    def llm(prompt):
        response = json.loads(_llm_v2(prompt))
        for fact in response.get("facts", []):
            fact["patient_context"] = {"category": "medication_management",
                                       "text": fact["evidence_quote"], "details": []}
        return json.dumps(response, ensure_ascii=False)

    try:
        result = _drain(db, llm=llm, jev=_PassJev())
        assert result["done"] == 1 and not result["failed"]
        for mid in (1, 2):
            row = db.db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND message_id=? "
                                "ORDER BY artifact_id DESC LIMIT 1", (semantic_v4.KIND_V4, mid)).fetchone()
            content, meta = json.loads(row["content"]), json.loads(row["meta"])
            assert content["patient_context"] and meta["projection_version"] == projection.PROJECTION_VERSION
            assert any(context["subject"] == "patient" for context in content["patient_context"])
            assert all(context["subject"] in ("patient", "unspecified") for context in content["patient_context"])
            stages = semantic_v4.stage_ledger(db, mid, meta["fingerprint"])
            assert any(stage["stage"] == "s8_publish" and stage["status"] == "PASS" for stage in stages)
    finally:
        db.close()
