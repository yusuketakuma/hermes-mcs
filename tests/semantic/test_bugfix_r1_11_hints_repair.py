"""Regression: unverifiable extract_v1 hints and repair status upgrades."""
import json

import semantic_extraction as extraction
import semantic_facts as sf

NO = {c: "none" for c in sf.MANDATORY_CATEGORIES}


def _member(body):
    return {"project_id": 1, "message_id": "m1", "revision": "r1",
            "body_original": body, "body_state": "full",
            "posted_at": "2026-09-20",
            "sender": {"id": "s1", "type": "staff", "profession": "dr"}}


def _llm(facts, presence):
    return lambda _p: json.dumps(
        {"facts": facts, "category_presence": dict(NO, **presence)},
        ensure_ascii=False)


def _fact(statement, kind, quote, **kw):
    return dict({"statement": statement, "kind": kind,
                 "subject_role": "patient", "polarity": "affirmed",
                 "evidence_quote": quote}, **kw)


def _assert_complete(result):
    doc = result["doc"]
    assert doc["coverage"]["status"] == "complete", doc["coverage"]
    assert all(f["validation_status"] == "verified" for f in doc["facts"])
    ids = {f["fact_id"] for f in doc["facts"]}
    for ob in doc["obligations"]:
        assert set(ob["fact_ids"]) <= ids


def test_repeated_symptom_hint_does_not_block_coverage():
    body = "昨日から発熱あり。本日も発熱が続いている。"
    llm = _llm([_fact("発熱が続いている", "symptom_state", "本日も発熱が続いている")],
               {"symptom_state": "one"})
    _assert_complete(extraction.extract_facts_v2(llm, _member(body)))


def test_fullwidth_vital_hint_does_not_block_coverage():
    body = "血圧１３０／８０"
    llm = _llm([_fact("血圧130/80", "vital_lab", body)], {"vital_lab": "one"})
    _assert_complete(extraction.extract_facts_v2(llm, _member(body)))


def test_repair_keeps_ambiguous_obligation_open():
    body = "アムロジピン5mg内服中。血圧130/80。"
    med = _fact("アムロジピン5mg内服中", "medication_event", "アムロジピン5mg内服中",
                action="continue", workflow_status="performed", importance="T1")
    vit = _fact("血圧130/80", "vital_lab", "血圧130/80",
                workflow_status="performed", importance="T2")
    llm = _llm([med, vit], {"medication": "none", "vital_lab": "one"})
    doc = extraction.extract_facts_v2(llm, _member(body))["doc"]
    assert doc["coverage"]["status"] == "incomplete"
    vit_id = next(f["fact_id"] for f in doc["facts"] if "血圧" in f["statement"])
    out = extraction.repair_facts_v2(llm, _member(body), doc, {vit_id: "x"})
    assert out["repaired"] is True
    new = out["doc"]
    assert new["coverage"]["status"] == "incomplete"
    med_obs = [o for o in new["obligations"]
               if o["category"] == "medication" and o["fact_ids"]]
    assert med_obs and all(o["status"] == "ambiguous" for o in med_obs)
