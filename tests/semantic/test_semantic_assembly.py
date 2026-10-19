"""Pure assembly preserves raw meaning and cached rebuild cannot dispatch or write."""
import copy
import sqlite3

import pytest

import semantic_assembly as assembly
import semantic_extraction as extraction
import semantic_facts as facts
from test_semantic_facts_v2 import FP, _FakeLedger, _llm, _member


def test_normalization_returns_owned_state_and_preserves_raw_subject_and_negation():
    member = _member("家族の合成観察は否定され、確認は予定です。")
    source = member["body_original"]
    manifest = assembly.build_manifest(source, FP)
    spec, = assembly.manifest_specs(manifest)
    owners = {atom: chunk["chunk_id"] for chunk in manifest["chunks"]
              for atom in chunk["core_atom_ids"]}
    obligations = assembly.v2_obligations(manifest)
    drops, omitted = {}, {}
    items = [{"kind": "other_observation", "statement": source,
              "evidence_quote": source, "subject_role": "family",
              "polarity": "negated", "workflow_status": "planned"}]
    before = copy.deepcopy((items, obligations, drops, omitted))
    clean, dropped, linked, reasons, omissions = assembly.normalise_facts_v2(
        items, member, source, spec=spec, manifest=manifest, atom_owner=owners,
        obligations=obligations, drop_reasons=drops, omitted=omitted)
    assert (items, obligations, drops, omitted) == before
    assert dropped == 0 and reasons == omissions == {}
    assert clean[0]["subject"] == facts.subject_identity("1", role="family")
    assert clean[0]["polarity"] == "negated" and clean[0]["workflow_status"] == "planned"
    assert any(clean[0]["fact_id"] in row["fact_ids"] for row in linked.values())


def test_skipped_comparison_cannot_make_a_document_complete():
    member = _member("完全合成の特記なし記録。")
    source = member["body_original"]
    manifest = assembly.build_manifest(source, FP)
    specs = assembly.manifest_specs(manifest)
    status = {row["chunk_id"]: "complete" for row in manifest["chunks"]}
    presence = {cid: {category: "none" for category in facts.MANDATORY_CATEGORIES} for cid in status}
    signals = {cid: set() for cid in status}
    assembled = assembly.assemble_facts(member, source, manifest, [], assembly.v2_obligations(manifest),
                                        presence, {}, status, [], signals, use_jev_preflight=False)
    complete = list(range(len(specs)))
    progress = assembly.chunk_progress(specs, complete, [], [], None, 0, FP)
    result = assembly.assemble_document(assembly.source_binding(member, FP), manifest, assembled,
                                        {"relations": [], "pairs_skipped": 1}, progress)
    assert result["doc"]["coverage"]["status"] == "incomplete"
    assert result["doc"]["coverage"]["limitations"] == ["relations_incomplete"]
    assert not result["extraction_complete"] and not result["complete"]
    assert result["failure_reason"] == "deadline" and progress["failure_reason"] is None


def test_cached_rebuild_reads_real_checkpoint_rows_without_execution_capabilities(request, monkeypatch):
    ledger = _FakeLedger(request)
    member = _member("完全合成の特記なし記録。")
    original = extraction.extract_facts_v2(_llm([]), member, ledger=ledger, source_fingerprint=FP)
    assert original["extraction_complete"]
    db = ledger.db
    before = db.total_changes
    db.execute("SAVEPOINT caller_owned")

    def forbidden(*args, **kwargs):
        raise AssertionError("cached rebuild must not dispatch")

    monkeypatch.setattr(extraction, "extract_facts_v2", forbidden)
    monkeypatch.setattr(extraction, "_preflight_verdict", forbidden)
    denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
              sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
    db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
    try:
        rebuilt, reason = extraction.rebuild_facts_v2_cached(member, db, FP, 1, original["doc"], {})
        assert reason is None and rebuilt == original["doc"]
        assert db.total_changes == before and db.in_transaction
    finally:
        db.set_authorizer(None)
        db.execute("ROLLBACK TO caller_owned")
        db.execute("RELEASE caller_owned")


def test_cached_rebuild_refuses_an_unfinished_relation_set(request, monkeypatch):
    import semantic_relations

    ledger = _FakeLedger(request)
    labels = ["Synthetic observation alpha.", "Synthetic observation beta."]
    member = _member("\n".join(labels))
    items = [{"kind": "other_observation", "statement": label,
              "evidence_quote": label, "subject_role": "patient",
              "polarity": "affirmed", "workflow_status": "reported"}
             for label in labels]
    original = extraction.extract_facts_v2(
        _llm(items, presence={"other_observation": "multiple"}), member,
        ledger=ledger, source_fingerprint=FP)
    assert original["extraction_complete"] and len(original["facts"]) == 2
    # First prove the complete manifest/checkpoints are actually reusable.
    rebuilt, reason = extraction.rebuild_facts_v2_cached(
        member, ledger.db, FP, 1, original["doc"], {})
    assert reason is None and rebuilt == original["doc"]
    before = copy.deepcopy(original["doc"])
    changes = ledger.db.total_changes
    comparisons = []

    def incomplete(active, facts, *, deadline=None):
        assert active == [] and len(facts) == 2 and deadline is None
        comparisons.append(True)
        return {"relations": [], "pairs_skipped": 1}

    monkeypatch.setattr(semantic_relations, "reconcile_facts", incomplete)
    rebuilt, reason = extraction.rebuild_facts_v2_cached(
        member, ledger.db, FP, 1, original["doc"], {})
    assert comparisons == [True]
    assert rebuilt is None and reason == "chunk_cache_miss"
    assert original["doc"] == before and ledger.db.total_changes == changes


@pytest.mark.parametrize("chunks", [4, 16])
def test_live_normalization_keeps_obligation_copy_work_linear(monkeypatch, chunks):
    real_copy = copy.deepcopy
    copied_rows = []

    def observe_copy(value, *args, **kwargs):
        if isinstance(value, dict) and value and all(
                isinstance(row, dict) and "owner_id" in row and "fact_ids" in row
                for row in value.values()):
            copied_rows.append(len(value))
        return real_copy(value, *args, **kwargs)

    monkeypatch.setattr(copy, "deepcopy", observe_copy)
    member = _member("合成" * (20 * chunks))
    result = extraction.extract_facts_v2(_llm([]), member, chunk_size=40)
    assert result["chunks_total"] == result["chunks_completed"] == chunks
    assert result["extraction_complete"] and result["coverage"]["status"] == "complete"
    assert result["doc"]["source"]["body_codepoints"] == 40 * chunks
    # Count actual copied obligation rows; do not depend on wall-clock speed.
    assert copied_rows and sum(copied_rows) <= 3 * chunks * len(facts.MANDATORY_CATEGORIES)
