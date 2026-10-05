"""Regression: repairing a fact must keep a covered jev_pre obligation covered."""

import json

import semantic_extraction as ex
from test_semantic_facts_v2 import NO_FACTS, _FakeJev, _member

BODY = "合成患者は個室を希望しています。"


def _llm(stmt):
    def call(_prompt):
        return json.dumps({
            "facts": [{"statement": stmt, "kind": "preference",
                       "subject_role": "patient", "polarity": "affirmed",
                       "epistemic": "reported",
                       "workflow_status": "reported",
                       "evidence_quote": "個室を希望"}],
            "category_presence": dict(NO_FACTS, preference="one"),
        }, ensure_ascii=False)
    return call


def test_repair_keeps_jev_pre_present_obligation_covered():
    r = ex.extract_facts_v2(_llm("個室希望"), _member(BODY),
                            jev_client=_FakeJev({"preference": "present"}))
    assert r["coverage"]["status"] == "complete"
    doc = r["doc"]
    fid = next(f["fact_id"] for f in doc["facts"]
               if f["kind"] == "preference")
    out = ex.repair_facts_v2(_llm("個室を希望"), _member(BODY), doc,
                             {fid: "wording"})
    assert out["repaired"]
    assert out["doc"]["coverage"]["status"] == "complete"
    jev = [ob for ob in out["doc"]["obligations"]
           if ob["source"] == "jev_pre" and ob["category"] == "preference"]
    assert jev and all(ob["status"] == "covered" and ob["fact_ids"]
                       for ob in jev)
