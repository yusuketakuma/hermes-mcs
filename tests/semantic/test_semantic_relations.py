"""Candidate relation reconciliation tests (T6).

Relations are candidates only — reconciliation never mutates the active
set, never drops a fact, and never resolves a conflict by latest-wins.
Contradictions, temporal order, and repetition are all preserved as
typed relations between surviving facts.
"""
import copy


import semantic_facts as sf
import semantic_relations as sr
from semantic_testkit import v2_fact


def _fact(fid, statement, *, kind="medication_event", subject="patient:1",
          action="unknown", polarity="affirmed", workflow="performed",
          event_time="unknown", evidence=()):
    fact = v2_fact(fid, kind=kind, subject=subject, statement=statement,
                   polarity=polarity, workflow_status=workflow,
                   event_time=event_time, evidence_ids=list(evidence),
                   importance="T2",
                   validation_status="verified" if evidence
                   else "unverified")
    if kind == "medication_event":
        fact["action"] = action
    return fact


def test_reconcile_preserves_everything_and_fingerprints():
    active = [
        _fact("fact_aaaa", "アムロジピン開始", action="start",
              event_time="2026-09-01", evidence=["ev_1"]),
        _fact("fact_bbbb", "頭痛あり", kind="symptom_state"),
    ]
    new = [
        _fact("fact_cccc", "アムロジピン中止", action="stop",
              event_time="2026-09-10", evidence=["ev_2"]),
        _fact("fact_dddd", "頭痛なし", kind="symptom_state",
              polarity="negated"),
    ]
    active_snapshot = copy.deepcopy(active)
    new_snapshot = copy.deepcopy(new)
    result = sr.reconcile_facts(active, new)
    # Inputs untouched — nothing deactivated or rewritten.
    assert active == active_snapshot and new == new_snapshot
    assert result["active_preserved"] == 2
    assert result["new_preserved"] == 2
    types = {r["type"] for r in result["relations"]}
    assert "EXPLICIT_SUPERSESSION" in types  # ordered start->stop
    assert "CONTRADICTION" in types          # 頭痛 polarity flip unordered
    for rel in result["relations"]:
        sf.validate_relation(rel)
    # Fingerprint is stable for identical sets and changes on mutation.
    fp = result["fingerprint"]
    assert fp == sr.relation_set_fingerprint(result["relations"])
    changed = result["relations"] + [
        _fact("fact_eeee", "x") and
        {"relation_id": "rel_x", "left_fact_id": "fact_aaaa",
         "right_fact_id": "fact_eeee", "type": "UNRESOLVED",
         "evidence_ids": [], "status": "candidate"}]
    assert sr.relation_set_fingerprint(changed) != fp


def test_intra_batch_contradiction_captured():
    new = [
        _fact("fact_aaaa", "アムロジピン開始", action="start"),
        _fact("fact_bbbb", "アムロジピン中止", action="stop"),
    ]
    result = sr.reconcile_facts([], new)
    assert result["relations"][0]["type"] == "CONTRADICTION"


