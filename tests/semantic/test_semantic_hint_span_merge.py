"""An extract_v1 hint merges into a model fact only when the hint's
evidence span lies inside that fact's evidence span. The same wording at
another place (same chunk or another chunk) stays a separate fact and
never closes another chunk's obligation. Fully synthetic bodies; stub
model only."""
import json

import semantic_extraction as extraction
import semantic_facts as sf

NO = {c: "none" for c in sf.MANDATORY_CATEGORIES}
STATEMENT = "アムロジピン 5mg"


def _member(body):
    return {"project_id": 1, "message_id": "m1", "revision": "r1",
            "body_original": body, "body_state": "full",
            "posted_at": "2026-09-20",
            "sender": {"id": "s1", "type": "staff", "profession": "dr"}}


def _fact(quote):
    return {"statement": STATEMENT, "kind": "medication_exposure",
            "subject_role": "patient", "polarity": "affirmed",
            "evidence_quote": quote}


def _run(body, quote, only_in=None, **kw):
    """The model reports the fact only in the chunk whose prompt holds
    ``only_in`` (every chunk when None)."""
    def llm(prompt):
        facts = [_fact(quote)] if only_in is None or only_in in prompt else []
        return json.dumps({"facts": facts,
                           "category_presence": dict(NO, medication="one")},
                          ensure_ascii=False)
    doc = extraction.extract_facts_v2(llm, _member(body), **kw)["doc"]
    sf.validate_facts_doc(doc)
    spans = {e["evidence_id"]: (e["start"], e["end"]) for e in doc["evidence"]}
    facts = {f["fact_id"]: f for f in doc["facts"]}
    for ob in doc["obligations"]:
        assert set(ob["fact_ids"]) <= set(facts)          # no dangling link
    return doc, facts, spans


def _named(facts, spans):
    return sorted((f["provenance"], [spans[e] for e in f["evidence_ids"]])
                  for f in facts.values() if f["statement"] == STATEMENT)


def test_same_chunk_disjoint_evidence_is_not_merged():
    body = "アムロジピン5mg内服。本日から5mgは中止。"
    _, facts, spans = _run(body, "本日から5mgは中止")
    assert _named(facts, spans) == [("extract_v1", [(0, 6)]),
                                    ("local_llm", [(12, 22)])]


def test_cross_chunk_hint_never_closes_the_other_chunks_obligation():
    body = "アムロジピン5mg内服。\n" + "経過観察。" * 4 + "\n本日から5mgは中止。"
    doc, facts, spans = _run(body, "本日から5mgは中止", only_in="本日から5mgは中止。",
                             chunk_size=20)
    assert len(doc["chunks"]) > 1
    model = next(f for f in facts.values() if f["provenance"] == "local_llm")
    hint = next(f for f in facts.values() if f["provenance"] == "extract_v1")
    assert spans[model["evidence_ids"][0]][0] > spans[hint["evidence_ids"][0]][1]
    assert not set(model["obligation_ids"]) & set(hint["obligation_ids"])
    for ob in doc["obligations"]:
        if ob["obligation_id"] in hint["obligation_ids"]:
            assert model["fact_id"] not in ob["fact_ids"]


def test_hint_inside_the_model_evidence_merges_and_retargets_links():
    body = "アムロジピン5mgを内服中。"
    doc, facts, spans = _run(body, "アムロジピン5mgを内服中")
    assert _named(facts, spans) == [("local_llm+extract_v1", [(0, 13)])]
    survivor = next(iter(facts.values()))
    linked = [ob for ob in doc["obligations"] if survivor["fact_id"] in ob["fact_ids"]]
    assert linked and all(ob["obligation_id"] in survivor["obligation_ids"]
                          for ob in linked)


def test_unlocatable_hint_never_corroborates_a_model_fact():
    body = "アムロジピン5mg開始。アムロジピン5mgは来週中止予定。"
    _, facts, spans = _run(body, "アムロジピン5mgは来週中止予定")
    assert _named(facts, spans) == [("local_llm", [(12, 28)])]
