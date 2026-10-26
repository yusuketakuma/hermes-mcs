"""Jev request-count reductions keep every answer and finding (synthetic only)."""
import json
import time

import pytest

import semantic_audit
import semantic_drain
import semantic_jev as jev
from semantic_testkit import _FakeJev, _llm, _seeded
from test_mcs_semantic import _cfg
from semantic_testkit import v2_doc
from test_semantic_facts_audit import EV, _fact, _Jev


def _prefilter(db, mid):
    content_hash = db.db.execute("SELECT content_hash FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
    db.artifact_add("extract_llm", "{}", project_id=1, message_id=mid,
                    meta={"hash": content_hash, "prefilter": "no_signal"})


def test_a_prefiltered_post_is_not_assessed_by_jev(tmp_path):
    db = _seeded(tmp_path)
    try:
        assert not semantic_drain._prefiltered(db, 1)
        _prefilter(db, 1)
        _prefilter(db, 2)
        assert semantic_drain._prefiltered(db, 1)
        client = _FakeJev()
        semantic_drain.run_due(db, _cfg("shadow"), {"errors": []}, time.monotonic() + 300,
                               jev_client=client, llm_fn=_llm)
        # proposition rows (medication-detail rows share the kind)
        rows = [json.loads(a["meta"]) for a in db.artifacts("semantic_assess")
                if "verdicts" in json.loads(a["content"])]
        assert rows and all(m.get("skipped") == "prefilter_no_signal" and m["jev_requests"] == 0 for m in rows)
        assert not any(set(call) & set(jev.PROPOSITIONS) for call in client.calls)
    finally:
        db.close()


def test_a_changed_body_is_assessed_again(tmp_path):
    db = _seeded(tmp_path)
    try:
        _prefilter(db, 1)
        db.db.execute("UPDATE messages SET content_hash='synthetic-new' WHERE message_id=1")
        assert not semantic_drain._prefiltered(db, 1)
    finally:
        db.close()


def test_fact_audit_batches_facts_and_sends_the_source_once():
    facts = [_fact(f"fact_{i}") for i in range(14)]
    doc = v2_doc(facts, [EV])
    calls = []

    class Capture(_Jev):
        def evaluate(self, state, questions, deadline):
            calls.append((state, list(questions)))
            return super().evaluate(state, questions, deadline)

    result = semantic_audit.audit_facts_v2(Capture(), doc, "完全合成の原文。", deadline=10 ** 9, per_request=12)
    audits = [(s, q) for s, q in calls if s["target"]["id"] == "facts"]
    assert [len(q) for _s, q in audits] == [12, 2]
    for state, _q in audits:
        assert sum(item["id"] == "source" for item in state["context"]) == 1
    assert set(result["fact_verdicts"]) == {f["fact_id"] for f in facts}


@pytest.mark.parametrize("size", [0, -1, 49, True, 1.0])
def test_an_invalid_fact_batch_size_never_skips_the_audit(size):
    calls = []

    class Capture(_Jev):
        def evaluate(self, state, questions, deadline):
            calls.append(questions)
            return super().evaluate(state, questions, deadline)

    result = semantic_audit.audit_facts_v2(Capture(), v2_doc([_fact("fact_a")], [EV]), "原文。",
                                           deadline=10 ** 9, per_request=size)
    assert not result["evaluated"] and result["status"] == "INCOMPLETE" and not calls
    assert {"code": "fact_audit_unevaluated"} in result["findings"]


def test_an_evidence_id_named_source_keeps_the_full_source_context():
    evidence = {**EV, "evidence_id": "source"}
    doc = v2_doc([_fact("fact_a", evidence_ids=["source"])], [evidence])
    seen = []

    class Capture(_Jev):
        def evaluate(self, state, questions, deadline):
            seen.append(state)
            return super().evaluate(state, questions, deadline)

    source_text = "完全合成の原文全体。" + EV["quote"] + "続きの文。"
    semantic_audit.audit_facts_v2(Capture(), doc, source_text, deadline=10 ** 9)
    [state] = [s for s in seen if s["target"]["id"] == "facts"]
    by_id = {c["id"]: c for c in state["context"]}
    assert by_id["source"]["text"] == source_text and by_id["source"]["role"] == "evidence_context"
    assert [c["role"] for c in state["context"]].count("evidence_quote") == 1
