"""A repair that adds a fact to a category the model adjudicated absent
leaves that obligation ambiguous (the extraction rule), never covered.
Synthetic bodies, stub model only."""
import json

import semantic_extraction as extraction
import semantic_facts as sf

NO = {c: "none" for c in sf.MANDATORY_CATEGORIES}
BODY = "アムロジピン5mgを継続。頭痛あり。"
MED = {"statement": "アムロジピン5mg継続", "kind": "medication_event", "action": "continue",
       "subject_role": "patient", "polarity": "affirmed", "workflow_status": "performed",
       "evidence_quote": "アムロジピン5mgを継続"}
SYMPTOM = {"statement": "頭痛あり", "kind": "symptom_state", "subject_role": "patient",
           "polarity": "affirmed", "workflow_status": "reported", "evidence_quote": "頭痛あり"}


def _member():
    return {"project_id": 1, "message_id": "m1", "revision": "r1", "body_original": BODY,
            "body_state": "full", "posted_at": "2026-09-20",
            "sender": {"id": "s1", "type": "staff", "profession": "dr"}}


def _model(facts, presence=None):
    return lambda prompt: json.dumps(
        {"facts": facts, "category_presence": dict(NO, **(presence or {}))},
        ensure_ascii=False)


def _status(doc, category):
    return [o["status"] for o in doc["obligations"]
            if o["category"] == category and o["source"] == "deterministic"]


def _repair(doc, facts):
    med = next(f["fact_id"] for f in doc["facts"] if f["kind"] == "medication_event")
    return extraction.repair_facts_v2(_model(facts), _member(), doc, {med: "synthetic"})


def test_repair_never_turns_an_adjudicated_absence_into_coverage():
    doc = extraction.extract_facts_v2(_model([MED], {"medication": "one"}), _member())["doc"]
    assert _status(doc, "symptom_state") == ["explicit_no_fact"]
    out = _repair(doc, [MED, SYMPTOM])
    assert out["repaired"] is True
    new = out["doc"]
    sf.validate_facts_doc(new)
    assert _status(new, "symptom_state") == ["ambiguous"]
    assert new["coverage"]["status"] == "incomplete"
    assert any(o["obligation_id"] in new["coverage"]["open_obligation_ids"]
               for o in new["obligations"]
               if o["category"] == "symptom_state" and o["source"] == "deterministic")
    # the same verdict and fact at extraction reach the same state
    fresh = extraction.extract_facts_v2(
        _model([MED, SYMPTOM], {"medication": "one"}), _member())["doc"]
    assert _status(fresh, "symptom_state") == _status(new, "symptom_state")
    assert fresh["coverage"]["status"] == new["coverage"]["status"]


def test_repair_keeps_an_untouched_absence_and_covers_its_own_category():
    doc = extraction.extract_facts_v2(_model([MED], {"medication": "one"}), _member())["doc"]
    new = _repair(doc, [MED])["doc"]
    assert _status(new, "symptom_state") == ["explicit_no_fact"]
    assert _status(new, "medication") == ["covered"]
    assert new["coverage"]["status"] == "complete"
