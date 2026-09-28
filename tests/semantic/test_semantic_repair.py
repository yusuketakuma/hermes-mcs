"""Targeted fact repair tests (T10).

A post-audit finding rejects specific fact_ids; only the owning chunks
are re-prompted, repaired items merge through stable fact_id identity,
and obligations re-link conservatively (covered -> open, never silent).
"""
import json

import semantic_extraction as extraction
import semantic_facts as sf


NO_FACTS = {c: "none" for c in sf.MANDATORY_CATEGORIES}


def _member(body, **kw):
    member = {"project_id": 1, "message_id": "m1", "revision": "r1",
              "body_original": body, "body_state": "full",
              "posted_at": "2026-09-20",
              "sender": {"id": "s1", "type": "staff", "profession": "dr"}}
    member.update(kw)
    return member


def _llm(facts, presence=None):
    def call(_prompt):
        return json.dumps(
            {"facts": facts,
             "category_presence": dict(NO_FACTS, **(presence or {}))},
            ensure_ascii=False)
    return call


def _doc():
    body = "アムロジピン5mgを中止します。"
    llm = _llm(
        [{"statement": "アムロジピン5mg中止", "kind": "medication_event",
          "action": "stop", "subject_role": "patient",
          "polarity": "affirmed", "workflow_status": "performed",
          "importance": "T1",
          "evidence_quote": "アムロジピン5mgを中止"}],
        presence={"medication": "one"})
    result = extraction.extract_facts_v2(llm, _member(body))
    sf.validate_facts_doc(result["doc"])
    return result["doc"], body


def test_repair_replaces_rejected_fact_from_owning_chunk():
    doc, body = _doc()
    rejected_fact = next(f for f in doc["facts"]
                         if f["provenance"] == "local_llm")
    seen = {}

    def repair_llm(prompt):
        seen["prompt"] = prompt
        return json.dumps(
            {"facts": [{"statement": "アムロジピン5mg中止指示",
                        "kind": "medication_event", "action": "stop",
                        "subject_role": "patient", "polarity": "affirmed",
                        "workflow_status": "ordered", "importance": "T1",
                        "evidence_quote": "アムロジピン5mgを中止"}],
             "category_presence": dict(NO_FACTS, medication="one")},
            ensure_ascii=False)

    out = extraction.repair_facts_v2(
        repair_llm, _member(body), doc,
        {rejected_fact["fact_id"]: "audit: wrong workflow_status"})
    assert out["repaired"] and out["owner_chunk_ids"]
    sf.validate_facts_doc(out["doc"])
    remaining = {f["fact_id"] for f in out["doc"]["facts"]}
    assert rejected_fact["fact_id"] not in remaining
    assert out["repaired_fact_ids"]
    new_fact = next(f for f in out["doc"]["facts"]
                    if f["fact_id"] in out["repaired_fact_ids"])
    assert new_fact["workflow_status"] == "ordered"
    assert new_fact["validation_status"] == "verified"
    # The repair prompt carried the rejection feedback.
    assert "wrong workflow_status" in seen["prompt"]
    # Obligations re-link to the repaired fact.
    med = next(o for o in out["doc"]["obligations"]
               if o["category"] == "medication"
               and o["source"] == "deterministic")
    assert new_fact["fact_id"] in med["fact_ids"]
    assert med["status"] == "covered"


def test_repair_without_replacement_reopens_obligation():
    doc, body = _doc()
    rejected_fact = next(f for f in doc["facts"]
                         if f["provenance"] == "local_llm")
    # Repair model returns no facts and presence none -> the medication
    # obligation loses its link and falls back, never to silent
    # explicit_no_fact.
    out = extraction.repair_facts_v2(
        _llm([], presence={"medication": "none"}), _member(body), doc,
        {rejected_fact["fact_id"]: "audit: unsupported"})
    assert out["repaired"]
    med = next(o for o in out["doc"]["obligations"]
               if o["category"] == "medication"
               and o["source"] == "deterministic")
    # The deterministic extract_v1 hint fact may still cover the
    # obligation — either way the rejected link is gone and the status
    # stays consistent with remaining links (never explicit_no_fact
    # fabricated by repair).
    assert rejected_fact["fact_id"] not in med["fact_ids"]
    assert med["status"] != "explicit_no_fact"
    assert (med["status"] == "covered") == bool(med["fact_ids"])
    assert all(f["fact_id"] != rejected_fact["fact_id"]
               for f in out["doc"]["facts"])
    sf.validate_facts_doc(out["doc"])


def test_repair_keeps_untouched_facts_and_chunks():
    body = "アムロジピン5mgを中止します。血圧120/80です。"
    llm = _llm(
        [{"statement": "アムロジピン5mg中止", "kind": "medication_event",
          "action": "stop", "subject_role": "patient",
          "polarity": "affirmed", "workflow_status": "performed",
          "importance": "T1",
          "evidence_quote": "アムロジピン5mgを中止"},
         {"statement": "血圧120/80", "kind": "vital_lab",
          "subject_role": "patient", "polarity": "affirmed",
          "workflow_status": "reported", "importance": "T2",
          "evidence_quote": "血圧120/80"}],
        presence={"medication": "one", "vital_lab": "one"})
    result = extraction.extract_facts_v2(llm, _member(body))
    doc = result["doc"]
    vital = next(f for f in doc["facts"] if f["kind"] == "vital_lab"
                 and f["provenance"] == "local_llm")
    med = next(f for f in doc["facts"] if f["kind"] == "medication_event"
               and f["provenance"] == "local_llm")
    out = extraction.repair_facts_v2(
        _llm([], presence={"medication": "none"}), _member(body), doc,
        {med["fact_id"]: "audit: unsupported"})
    kept = {f["fact_id"] for f in out["doc"]["facts"]}
    assert vital["fact_id"] in kept


def test_repair_unknown_fact_id_is_noop():
    doc, body = _doc()
    out = extraction.repair_facts_v2(
        _llm([]), _member(body), doc, {"fact_missing": "x"})
    assert not out["repaired"] and out["doc"] is doc


def test_repair_llm_failure_returns_not_repaired():
    doc, body = _doc()
    rejected_fact = next(f for f in doc["facts"]
                         if f["provenance"] == "local_llm")

    def dead_llm(_prompt):
        raise ConnectionError("local llm down")

    out = extraction.repair_facts_v2(
        dead_llm, _member(body), doc,
        {rejected_fact["fact_id"]: "audit: unsupported"})
    assert not out["repaired"] and out["doc"] is doc


def test_repair_unparseable_output_keeps_doc():
    doc, body = _doc()
    rejected_fact = next(f for f in doc["facts"]
                         if f["provenance"] == "local_llm")
    out = extraction.repair_facts_v2(
        lambda _p: "not json", _member(body), doc,
        {rejected_fact["fact_id"]: "audit: unsupported"})
    # A dispatched-but-empty repair still produced no merge — the doc
    # stays as the caller's held artifact.
    assert not out["repaired"]


def test_repair_chunk_with_dropped_items_contributes_nothing():
    # FIX-SE4: extraction fails a whole chunk when any item is dropped;
    # repair must apply the same rule — a partially-malformed repair
    # response merges no facts at all.
    doc, body = _doc()
    rejected_fact = next(f for f in doc["facts"]
                         if f["provenance"] == "local_llm")

    def sloppy_llm(prompt):
        return json.dumps(
            {"facts": [{"statement": "アムロジピン5mg中止",
                        "kind": "medication_event", "action": "stop",
                        "subject_role": "patient", "polarity": "affirmed",
                        "workflow_status": "performed",
                        "evidence_quote": "アムロジピン5mgを中止"},
                       {"statement": "壊れた行", "kind": "bogus_kind"}],
             "category_presence": dict(NO_FACTS, medication="one")},
            ensure_ascii=False)

    out = extraction.repair_facts_v2(
        sloppy_llm, _member(body), doc,
        {rejected_fact["fact_id"]: "audit: unsupported"})
    assert out["repaired"]            # dispatched, but contributed 0 facts
    assert not out["repaired_fact_ids"]
    kept = {f["fact_id"] for f in out["doc"]["facts"]}
    assert rejected_fact["fact_id"] not in kept
    sf.validate_facts_doc(out["doc"])
    # The rejected link reopened the obligation — nothing fabricated.
    med = next(o for o in out["doc"]["obligations"]
               if o["category"] == "medication"
               and o["source"] == "deterministic")
    assert med["status"] != "explicit_no_fact"
    assert (med["status"] == "covered") == bool(med["fact_ids"])


def test_repair_preserves_failed_chunk_obligation_status():
    # FIX-SE5: a hint fact can link an obligation owned by a chunk that
    # later failed — repair re-derivation must not upgrade it to
    # covered; the failed chunk's adjudication never completed.
    body = ("血圧130を記録。" + "あ" * 80 + "\n"
            + "メトホルミン500mg を中止した。")
    calls = []

    def llm(prompt):
        calls.append(prompt)
        if len(calls) > 1:
            raise RuntimeError("model down")
        return json.dumps(
            {"facts": [{"statement": "血圧130", "kind": "vital_lab",
                        "subject_role": "patient", "polarity": "affirmed",
                        "evidence_quote": "血圧130"}],
             "category_presence": dict(NO_FACTS, vital_lab="one")},
            ensure_ascii=False)

    result = extraction.extract_facts_v2(
        llm, _member(body), chunk_size=100)
    doc = result["doc"]
    assert not result["extraction_complete"]
    sf.validate_facts_doc(doc)
    failed_med = [o for o in doc["obligations"]
                  if o["category"] == "medication"
                  and o["status"] == "failed" and o["fact_ids"]]
    assert failed_med  # the hint covered a fact in the failed chunk

    vital = next(f for f in doc["facts"]
                 if f["kind"] == "vital_lab" and f["provenance"] == "local_llm")
    out = extraction.repair_facts_v2(
        _llm([], presence={"vital_lab": "none"}), _member(body), doc,
        {vital["fact_id"]: "audit: unsupported"}, chunk_size=100)
    sf.validate_facts_doc(out["doc"])
    for ob in out["doc"]["obligations"]:
        if ob["obligation_id"] == failed_med[0]["obligation_id"]:
            assert ob["status"] == "failed"
    assert "failed" in {o["status"] for o in out["doc"]["obligations"]}


def test_repair_refuses_different_source_before_dispatch():
    doc, body = _doc()
    calls = []
    rejected = {doc["facts"][0]["fact_id"]: "synthetic"}
    result = extraction.repair_facts_v2(
        lambda prompt: calls.append(prompt), _member(body.replace("5mg", "9mg")),
        doc, rejected)
    assert not result["repaired"]
    assert result["doc"] is doc
    assert calls == []


def test_repair_discards_answer_returned_after_deadline(monkeypatch):
    doc, body = _doc()
    now = [10.0]
    monkeypatch.setattr(extraction.time, "monotonic", lambda: now[0])

    def late(_prompt):
        now[0] = 12.0
        return _llm([])(_prompt)

    result = extraction.repair_facts_v2(late, _member(body), doc,
        {doc["facts"][0]["fact_id"]: "synthetic"}, deadline=11.0)
    assert not result["repaired"]
    assert result["doc"] is doc
