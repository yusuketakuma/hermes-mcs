"""semantic-facts/v2 contract tests: enums, stable IDs, validators,
importance policy, and the committed synthetic completeness corpus."""
import json
from pathlib import Path

import pytest

import semantic_evaluation as evaluation
import semantic_facts as sf


CORPUS = Path(__file__).resolve().parents[2] / "evaluation" / \
    "semantic_completeness_cases.json"


def _binding(**overrides):
    binding = {
        "message_id": "m1", "revision": "r1", "content_hash": "h1",
        "body_codepoints": 40, "content_quality": "full",
        "attachments_complete": True,
        "source_fingerprint": sf.source_fingerprint(
            "m1", "r1", "h1", 40, "full", True),
    }
    binding.update(overrides)
    return binding


def _doc(facts=None, relations=None, obligations=None, coverage=None):
    atoms = [{"atom_id": "atom_a1", "kind": "clause", "start": 0,
              "end": 20, "text_hash": "th1"}]
    chunks = [{"chunk_id": "chk_c1", "core_atom_ids": ["atom_a1"],
               "status": "done"}]
    return {
        "version": sf.CONTRACT_VERSION,
        "source": _binding(),
        "atoms": atoms, "chunks": chunks,
        "obligations": obligations if obligations is not None else [],
        "evidence": [], "facts": facts if facts is not None else [],
        "relations": relations if relations is not None else [],
        "coverage": coverage if coverage is not None else {
            "category_counts": {}, "open_obligation_ids": [],
            "limitations": [], "status": "complete"},
    }


def _fact(**overrides):
    fact = {
        "fact_id": "fact_x", "kind": "medication_event",
        "subject": "patient:proj-a", "actor": "sender:s1",
        "statement": "アムロジピン開始", "polarity": "affirmed",
        "epistemic": "asserted", "workflow_status": "ordered",
        "event_time": "本日", "valid_time": "unknown",
        "evidence_ids": [], "obligation_ids": [],
        "importance": "T1", "provenance": "local_llm",
        "validation_status": "unverified",
        "action": "start",
    }
    fact.update(overrides)
    return fact


def test_ids_are_deterministic_across_retries_and_chunk_layout():
    args = ("proj-a", "m1", "r1", "medication_event", "patient:proj-a",
            "アムロジピン開始", ["ev_a", "ev_b"])
    first = sf.fact_id(*args)
    assert first == sf.fact_id(*args)
    assert first == sf.fact_id("proj-a", "m1", "r1", "medication_event",
                             "patient:proj-a", "アムロジピン開始",
                             ["ev_b", "ev_a"])
    # Chunk identity is not an input: layout changes cannot shift the ID.
    assert sf.chunk_id("sf_x", 0) != sf.chunk_id("sf_x", 1)
    assert sf.atom_id("sf_x", 0) != sf.chunk_id("sf_x", 0)


def test_same_evidence_different_statement_or_subject_yields_distinct_ids():
    base = ("proj-a", "m1", "r1", "medication_event", "patient:proj-a",
            "アムロジピン開始", ["ev_a"])
    fid = sf.fact_id(*base)
    assert fid != sf.fact_id(*base[:5], "アムロジピン中止", base[6])
    assert fid != sf.fact_id(*base[:4], "person:x", base[5], base[6])
    assert fid != sf.fact_id("proj-b", *base[1:])


def test_subject_identity_never_invents_patient_or_links_generics():
    assert sf.subject_identity("p9", role="patient") == "patient:p9"
    assert sf.subject_identity("p9", sender_id="u1") == "sender:u1"
    named = sf.subject_identity("p9", role="family", name="母")
    assert named.startswith("person:")
    assert named != sf.subject_identity("p8", role="family", name="母")
    assert sf.subject_identity("p9", role="family") == "role:family"
    assert sf.subject_identity("p9") == "unknown"


def test_enum_validation_rejects_invalid_values_with_stable_reasons():
    with pytest.raises(sf.ContractError, match="contract:fact_polarity"):
        sf.validate_fact(_fact(polarity="positive"))
    with pytest.raises(sf.ContractError, match="contract:fact_kind"):
        sf.validate_fact(_fact(kind="diagnosis"))
    with pytest.raises(sf.ContractError, match="contract:relation_type"):
        sf.validate_relation({"relation_id": "rel_x",
                              "left_fact_id": "fact_a",
                              "right_fact_id": "fact_b",
                              "type": "REPLACES"})
    with pytest.raises(sf.ContractError, match="contract:obligation_category"):
        sf.validate_obligation({"obligation_id": "obl_x",
                                "owner_id": "chk_c1",
                                "category": "billing",
                                "source": "deterministic",
                                "status": "open"})


@pytest.mark.parametrize('value', ['a', None, {'a': True}])
@pytest.mark.parametrize('validator,document,field', [
    (sf.validate_atom, _doc()['atoms'][0], 'dependency_atom_ids'),
    (sf.validate_chunk, _doc()['chunks'][0], 'context_atom_ids'),
    (sf.validate_fact, _fact(), 'evidence_ids'),
    (sf.validate_obligation, {'obligation_id': 'o', 'owner_id': 'a',
        'category': 'medication', 'source': 'deterministic', 'status': 'open'}, 'fact_ids'),
    (sf.validate_relation, {'relation_id': 'r', 'left_fact_id': 'a',
        'right_fact_id': 'b', 'type': 'COMPLEMENTS'}, 'evidence_ids'),
    (sf.validate_coverage, _doc()['coverage'], 'open_obligation_ids'),
])
def test_reference_arrays_are_not_coerced_from_other_json_types(validator,
                                                               document, field, value):
    with pytest.raises(sf.ContractError, match='list_required'):
        validator({**document, field: value})


def test_evidence_validation_preserves_verbatim_whitespace():
    quote = '  合成の引用  '
    record = {'evidence_id': 'e', 'message_id': 'm1', 'revision': 'r1',
              'start': 0, 'end': len(quote), 'quote': quote, 'atom_id': 'a'}
    assert sf.validate_evidence(record)['quote'] == quote


def test_huge_confidence_has_contract_error():
    with pytest.raises(sf.ContractError, match='confidence_invalid'):
        sf.validate_relation({'relation_id': 'r', 'left_fact_id': 'a',
            'right_fact_id': 'b', 'type': 'COMPLEMENTS', 'confidence': 10**1000})


def test_importance_is_ordering_metadata_only():
    assert [sf.importance_rank(t) for t in ("T0", "T1", "T2", "T3")] \
        == [0, 1, 2, 3]
    assert sf.importance_rank("unknown") == 4
    with pytest.raises(sf.ContractError):
        sf.importance_rank("T4")


def test_facts_doc_cross_reference_validation():
    evidence = {"evidence_id": "ev_1", "message_id": "m1",
                "revision": "r1", "start": 0, "end": 8,
                "quote": "アムロジピン", "atom_id": "atom_a1"}
    fact = _fact(fact_id="fact_f1", evidence_ids=["ev_1"],
                 obligation_ids=["obl_o1"],
                 validation_status="verified")
    obligation = {"obligation_id": "obl_o1", "owner_id": "chk_c1",
                  "category": "medication", "source": "deterministic",
                  "status": "covered", "fact_ids": ["fact_f1"]}
    relation = {"relation_id": "rel_1", "left_fact_id": "fact_f1",
                "right_fact_id": "fact_f2", "type": "COMPLEMENTS"}
    doc = _doc(facts=[fact, _fact(fact_id="fact_f2")],
               obligations=[obligation], relations=[relation])
    doc["evidence"] = [evidence]
    validated = sf.validate_facts_doc(doc)
    assert validated["version"] == sf.CONTRACT_VERSION

    bad = _doc(facts=[_fact(evidence_ids=["ev_missing"])])
    with pytest.raises(sf.ContractError,
                       match="contract:doc_fact_evidence_unknown"):
        sf.validate_facts_doc(bad)
    bad = _doc(facts=[_fact(validation_status="verified")])
    with pytest.raises(sf.ContractError,
                       match="contract:doc_verified_without_evidence"):
        sf.validate_facts_doc(bad)
    bad = _doc()
    bad["atoms"].append({"atom_id": "atom_a2", "kind": "clause",
                         "start": 20, "end": 40, "text_hash": "th2"})
    with pytest.raises(sf.ContractError,
                       match="contract:doc_core_coverage_incomplete"):
        sf.validate_facts_doc(bad)
    bad = _doc(facts=[fact], obligations=[obligation],
               relations=[relation])
    bad["evidence"] = [evidence]
    with pytest.raises(sf.ContractError,
                       match="contract:doc_relation_fact_unknown"):
        sf.validate_facts_doc(bad)
    bad = _doc(obligations=[{"obligation_id": "obl_o2",
                             "owner_id": "chk_c1",
                             "category": "symptom_state",
                             "source": "jev_pre", "status": "open"}],
               coverage={"category_counts": {"symptom_state": 1},
                         "open_obligation_ids": ["obl_o2"],
                         "limitations": [], "status": "complete"})
    with pytest.raises(sf.ContractError,
                       match="contract:doc_coverage_complete_with_open"):
        sf.validate_facts_doc(bad)


def test_corpus_is_synthetic_and_asserts_mandatory_coverage():
    corpus = json.loads(CORPUS.read_text())
    assert corpus["source"] == "synthetic"
    assert len(corpus["cases"]) >= 10
    bodies = "\n".join(
        m["body"] for c in corpus["cases"] for m in c["messages"])
    assert "SYNTHETIC" not in bodies  # real-looking fixture text only
    for case in corpus["cases"]:
        expect = case["expect"]
        message_bodies = {m["message_id"]: m["body"]
                          for m in case["messages"]}
        fact_ids = {f["id"] for f in expect["facts"]}
        mandatory = [f for f in expect["facts"] if f["mandatory"]]
        # Mandatory/rendered recall denominators are asserted.
        assert len(mandatory) == expect["mandatory_count"]
        assert expect["rendered_mandatory"] is True
        for fact in expect["facts"]:
            assert fact["message_id"] in message_bodies
            # Evidence closure: quote must be an exact source substring.
            assert fact["evidence_quote"] \
                in message_bodies[fact["message_id"]]
            assert fact["kind"] in sf.FACT_KINDS
            assert fact["polarity"] in sf.POLARITIES
            assert fact["workflow_status"] in sf.WORKFLOW_STATUSES
        # Relation closure: every endpoint names a declared fact.
        for rel in expect["relations"]:
            assert rel["left_fact_id"] in fact_ids
            assert rel["right_fact_id"] in fact_ids
            assert rel["type"] in sf.RELATION_TYPES


@pytest.mark.parametrize("changed", [
    {"message_id": "m2"}, {"revision": "r2"}, {"end": 41},
    {"start": 21, "end": 25},
])
def test_facts_doc_rejects_evidence_outside_its_source_or_atom(changed):
    evidence = {"evidence_id": "ev_1", "message_id": "m1",
                "revision": "r1", "start": 0, "end": 8,
                "quote": "synthetic", "atom_id": "atom_a1", **changed}
    doc = _doc(facts=[_fact(evidence_ids=["ev_1"], validation_status="verified")])
    doc["evidence"] = [evidence]
    with pytest.raises(sf.ContractError):
        sf.validate_facts_doc(doc)

def test_corpus_has_no_cross_case_id_collision():
    corpus = json.loads(CORPUS.read_text())
    ids = [c["id"] for c in corpus["cases"]]
    assert len(ids) == len(set(ids))
    fact_ids = [f["id"] for c in corpus["cases"]
                for f in c["expect"]["facts"]]
    assert len(fact_ids) == len(set(fact_ids))


def test_gate_denies_promotion_without_human_labels():
    manifest = {"version": "manifest-v1", "bundle_version": "bundle-v1",
                "candidate_version": "candidate-v1",
                "label_version": "label-v1"}
    criteria = {
        "version": "criteria-v1",
        "required_metrics": list(evaluation.METRICS),
        "min_human_labels": 1,
    }
    record = {
        "case_id": "case-1", "split": "test",
        "account_id": "a", "project_id": "p", "thread_id": "t",
        "bundle": {"version": "bundle-v1",
                   "messages": [{"message_id": "m1"}]},
        "candidate": {"version": "candidate-v1",
                      "facts": [{"fact_id": "f1", "important": True,
                                 "evidence_ids": ["e1"]}],
                      "rendered_fact_ids": ["f1"],
                      "status": "complete"},
        "label": {"version": "label-v1", "source": "synthetic",
                  "facts": [{"fact_id": "f1", "important": True,
                             "mandatory": True}]},
    }
    report = evaluation.evaluate_records([record], manifest, criteria)
    assert not report["gate"]["pass"]
    assert "human_labels_insufficient" in report["gate"]["reasons"]
    assert report["metrics"]["mandatory_fact_recall"]["denominator"] == 1
    assert report["metrics"]["rendered_fact_recall"]["correct"] == 1
    assert report["metrics"]["evidence_closure"]["rate"] == 1.0
    assert report["metrics"]["silent_drop"]["errors"] == 0


def test_missing_mandatory_fact_is_silent_drop_not_recall():
    manifest = {"version": "manifest-v1", "bundle_version": "bundle-v1",
                "candidate_version": "candidate-v1",
                "label_version": "label-v1"}
    criteria = {"version": "criteria-v1",
                "required_metrics": list(evaluation.METRICS),
                "min_human_labels": 1}
    record = {
        "case_id": "case-1", "split": "test",
        "account_id": "a", "project_id": "p", "thread_id": "t",
        "bundle": {"version": "bundle-v1",
                   "messages": [{"message_id": "m1"}]},
        "candidate": {"version": "candidate-v1", "facts": [],
                      "status": "complete"},
        "label": {"version": "label-v1", "source": "human",
                  "receipt": {"receipt_id": "rcpt-1",
                              "labelled_at": "2026-09-21",
                              "reviewer": "reviewer-1"},
                  "facts": [{"fact_id": "f1", "important": True,
                             "mandatory": True}]},
    }
    report = evaluation.evaluate_records([record], manifest, criteria)
    assert report["metrics"]["mandatory_fact_recall"]["correct"] == 0
    assert report["metrics"]["silent_drop"]["errors"] == 1
    # Recording the miss as unresolved avoids the silent-drop error.
    record["candidate"]["unresolved"] = [{"fact_ref": "f1"}]
    report = evaluation.evaluate_records([record], manifest, criteria)
    assert report["metrics"]["silent_drop"]["errors"] == 0
    assert report["metrics"]["mandatory_fact_recall"]["correct"] == 0
