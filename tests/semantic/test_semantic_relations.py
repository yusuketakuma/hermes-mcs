"""Candidate relation reconciliation tests (T6).

Relations are candidates only — reconciliation never mutates the active
set, never drops a fact, and never resolves a conflict by latest-wins.
Contradictions, temporal order, and repetition are all preserved as
typed relations between surviving facts.
"""
import copy

import pytest

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


def test_exact_duplicate_same_statement():
    left = _fact("fact_aaaa", "アムロジピン5mg継続")
    right = _fact("fact_bbbb", "アムロジピン5mg継続")
    rel = sr.classify_pair(left, right)
    assert rel["type"] == "EXACT_DUPLICATE"
    sf.validate_relation(rel)


@pytest.mark.parametrize("changed", [
    {"subject": "person:family01"}, {"actor": "sender:s2"},
    {"polarity": "negated"}, {"epistemic": "suspected"},
    {"workflow_status": "planned"}, {"event_time": "2026-09-02"},
    {"valid_time": "2026-09-02"}, {"quantity": "10mg"},
    {"action": "stop"},
])
def test_same_statement_preserves_distinct_clinical_attributes(changed):
    left = _fact("fact_aaaa", "アムロジピン服用", action="start")
    right = {**_fact("fact_bbbb", "アムロジピン服用", action="start"), **changed}
    relation = sr.classify_pair(left, right)
    assert relation is not None
    assert relation["type"] != "EXACT_DUPLICATE"


def test_ordered_action_pair_supersedes():
    left = _fact("fact_aaaa", "アムロジピン開始", action="start",
                 event_time="2026-09-01")
    right = _fact("fact_bbbb", "アムロジピン中止", action="stop",
                  event_time="2026-09-10")
    rel = sr.classify_pair(left, right)
    assert rel["type"] == "EXPLICIT_SUPERSESSION"
    assert rel["left_fact_id"] == "fact_aaaa"
    assert rel["right_fact_id"] == "fact_bbbb"


@pytest.mark.parametrize("axis", ["event_time", "valid_time"])
@pytest.mark.parametrize("first,second,earlier", [
    ("2026-09-01T00:30:00+09:00", "2026-08-31T16:00:00+00:00", "fact_aaaa"),
    ("2026-08-31T16:00:00+00:00", "2026-09-01T00:30:00+09:00", "fact_bbbb"),
    ("2026-09-01T00:00:00+09:00", "2026-08-31T15:00:00Z", None),
    ("2026-09-01-invalid", "2026-09-02", None),
    ("2026-02-30", "2026-09-02", None),
    ("2026-09-01T09:00:00", "2026-09-02T09:00:00+09:00", None),
    ("2026-09-01", "2026-09-01T09:00:00+09:00", None),
])
def test_temporal_relations_require_valid_comparable_instants(axis, first, second, earlier):
    left = _fact("fact_aaaa", "合成薬剤A開始", action="start")
    right = _fact("fact_bbbb", "合成薬剤A中止", action="stop")
    left[axis], right[axis] = first, second
    rel = sr.classify_pair(left, right)
    if earlier is None:
        assert rel["type"] == "CONTRADICTION"
    else:
        assert rel["type"] == "EXPLICIT_SUPERSESSION"
        assert rel["left_fact_id"] == earlier


def test_reversed_temporal_order_swaps_relation_direction():
    left = _fact("fact_aaaa", "アムロジピン中止", action="stop",
                 event_time="2026-09-10")
    right = _fact("fact_bbbb", "アムロジピン開始", action="start",
                  event_time="2026-09-01")
    rel = sr.classify_pair(left, right)
    assert rel["type"] == "EXPLICIT_SUPERSESSION"
    # The temporally earlier fact is always the superseding source.
    assert rel["left_fact_id"] == "fact_bbbb"
    assert rel["right_fact_id"] == "fact_aaaa"


def test_unordered_action_pair_is_contradiction_not_latest_wins():
    left = _fact("fact_aaaa", "アムロジピン開始", action="start")
    right = _fact("fact_bbbb", "アムロジピン中止", action="stop")
    rel = sr.classify_pair(left, right)
    assert rel["type"] == "CONTRADICTION"
    assert rel["reason"] == "unordered_action_pair"


def test_polarity_conflict_orders_or_contradicts():
    affirmed = _fact("fact_aaaa", "頭痛あり", kind="symptom_state",
                     polarity="affirmed", event_time="2026-09-01")
    negated = _fact("fact_bbbb", "頭痛なし", kind="symptom_state",
                    polarity="negated", event_time="2026-09-05")
    # Shared entity token 頭痛, ordered affirmed->negated.
    rel = sr.classify_pair(affirmed, negated)
    assert rel["type"] == "EXPLICIT_SUPERSESSION"
    unordered = _fact("fact_cccc", "頭痛あり", kind="symptom_state",
                      polarity="affirmed")
    rel = sr.classify_pair(unordered, negated)
    assert rel["type"] == "CONTRADICTION"


def test_workflow_progression_is_transition():
    planned = _fact("fact_aaaa", "CT検査予定", kind="care_event",
                    workflow="planned", event_time="2026-09-01")
    done = _fact("fact_bbbb", "CT検査実施", kind="care_event",
                 workflow="done", event_time="2026-09-03")
    rel = sr.classify_pair(planned, done)
    assert rel["type"] == "TRANSITION"


def test_complements_for_distinct_same_entity_facts():
    left = _fact("fact_aaaa", "アムロジピン5mg継続", action="continue")
    right = _fact("fact_bbbb", "アムロジピン朝食後服用", action="continue")
    rel = sr.classify_pair(left, right)
    assert rel["type"] == "COMPLEMENTS"


def test_unrelated_facts_have_no_relation():
    left = _fact("fact_aaaa", "アムロジピン継続")
    right = _fact("fact_bbbb", "散歩を実施", kind="care_event")
    assert sr.classify_pair(left, right) is None


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


def test_relation_evidence_unions_both_facts():
    left = _fact("fact_aaaa", "アムロジピン開始", action="start",
                 event_time="2026-09-01", evidence=["ev_1"])
    right = _fact("fact_bbbb", "アムロジピン中止", action="stop",
                  event_time="2026-09-10", evidence=["ev_2"])
    rel = sr.classify_pair(left, right)
    assert rel["evidence_ids"] == ["ev_1", "ev_2"]
    assert rel["status"] == "candidate"


def test_different_subjects_do_not_supersede():
    left = _fact("fact_aaaa", "アムロジピン開始", action="start",
                 subject="patient:1", event_time="2026-09-01")
    right = _fact("fact_bbbb", "アムロジピン中止", action="stop",
                  subject="person:family01", event_time="2026-09-10")
    rel = sr.classify_pair(left, right)
    assert rel["type"] == "COMPLEMENTS"


def test_malformed_inputs_are_safe():
    assert sr.classify_pair(None, {}) is None
    assert sr.classify_pair({"fact_id": "a"}, {"fact_id": "a"}) is None
    result = sr.reconcile_facts([{"bad": 1}], [{"fact_id": "fact_x"}])
    assert result["relations"] == []
    assert sr.relation_set_fingerprint(None).startswith("relset_")


def test_generic_action_word_is_not_a_shared_entity():
    """U07-F05: two different drugs sharing only a generic action word
    (投与) are unrelated — a stop of one must not supersede the other."""
    left = _fact("fact_aaaa", "アムロジピンを投与", action="start",
                 event_time="2026-09-01", evidence=("ev_a",))
    right = _fact("fact_bbbb", "ロキソニンの投与を中止", action="stop",
                  event_time="2026-09-02", evidence=("ev_b",))
    assert sr.classify_pair(left, right) is None
    # the same drug still links through its name
    same = _fact("fact_cccc", "アムロジピンの投与を中止", action="stop",
                 event_time="2026-09-02", evidence=("ev_c",))
    rel = sr.classify_pair(left, same)
    assert rel is not None and rel["type"] == "EXPLICIT_SUPERSESSION"
