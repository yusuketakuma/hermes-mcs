"""Synthetic cache-history reads stream rows without changing generation selection."""
import json
import sqlite3

import pytest

import extract_llm
import semantic_assessment as assessment
import semantic_extraction as extraction


@pytest.fixture
def ledger():
    result = sqlite3.connect(":memory:")
    result.row_factory = sqlite3.Row
    result.execute("CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,"
                      "kind TEXT,project_id INTEGER,message_id INTEGER,content TEXT,meta TEXT)")
    yield result
    result.close()


def add(ledger, kind, content, meta):
    ledger.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
                      "VALUES(?,1,1,?,?)", (kind, json.dumps(content), json.dumps(meta)))


@pytest.mark.parametrize("project_id", [1, None])
def test_detail_uses_newest_valid_matching_generation(ledger, project_id):
    meta = {"schema": assessment.ASSESSMENT_SCHEMA_VERSION, "source_fingerprint": "fp",
            "policy_fingerprint": "policy", "fact_id": "fact1", "dimension": "dim",
            "technical_status": "complete"}
    for confidence in (0.5, 0.9, "invalid"):
        add(ledger, assessment.KIND_ASSESS, {"choice": "yes", "confidence": confidence}, meta)
    add(ledger, assessment.KIND_ASSESS, {"choice": "yes", "confidence": 0.7},
        {**meta, "source_fingerprint": "old"})
    before = ledger.total_changes
    assert assessment._cached_detail(ledger, project_id, 1, "fp", "policy", "fact1", "dim",
                                      {"yes": "SYNTH"}) == {"choice": "yes", "confidence": 0.9}
    assert ledger.total_changes == before


@pytest.mark.parametrize("project_id", [1, None])
def test_fact_chunk_uses_latest_valid_chunk_despite_newer_malformed_row(ledger, project_id):
    specs = [{"index": 0, "hash": "h", "start": 0, "end": 1, "text": "x"}]
    meta = {"chunk_index": 0, "schema": extraction.SCHEMA_VERSION, "generation": "gen",
            "status": "complete", "source_fingerprint": "fp", "source_hash": "body-hash",
            "revision": "r", "chunks_total": 1, "chunk_hash": "h",
            "start_codepoint": 0, "end_codepoint": 1}
    for dropped in (1, 2, -1):
        add(ledger, extraction.KIND_CHUNK, {"chunk_index": 0, "start_codepoint": 0,
                                          "end_codepoint": 1, "facts": [], "dropped": dropped}, meta)
    add(ledger, extraction.KIND_CHUNK, {}, {**meta, "generation": "old"})
    assert extraction._cached_chunks(ledger, project_id, 1, "fp", "body-hash", "r", specs, "x",
                                     generation="gen") == {0: {"facts": [], "dropped": 2, "extra": {}}}


def test_legacy_chunk_malformed_latest_owner_still_invalidates_old_checkpoint(ledger):
    row = {"message_id": 1, "project_id": 1, "body_text": "SYNTH", "content_hash": "hash",
           "posted_at": "2026-01-01T00:00:00+09:00"}
    meta = {"hash": "hash", "ver": extract_llm.EXTRACT_VERSION,
            "chunk_size": extract_llm._CHUNK_SIZE, "context": extract_llm._chunk_context(row, None, checkpoint=True),
            "chunk": 0}
    add(ledger, "extract_llm_chunk", {"summary": "SYNTH"}, meta)
    assert extract_llm._saved_chunk_state(ledger, row)[1] == {0: {"summary": "SYNTH"}}
    add(ledger, "extract_llm_chunk", [], meta)
    assert extract_llm._saved_chunk_state(ledger, row)[1] == {}
    add(ledger, "extract_llm_chunk", {"summary": "SYNTH-old"}, {**meta, "hash": "old"})
    assert extract_llm._saved_chunk_state(ledger, row)[1] == {}


def test_legacy_detail_protocol_and_borrowed_chunk_reads_preserve_selection(ledger):
    legacy = ledger
    assert not hasattr(legacy, "db") and not hasattr(legacy, "iter_artifacts")
    detail_meta = {"schema": assessment.ASSESSMENT_SCHEMA_VERSION, "source_fingerprint": "fp",
                   "policy_fingerprint": "policy", "fact_id": "fact1", "dimension": "dim",
                   "technical_status": "complete"}
    for confidence in (0.5, 0.9):
        add(ledger, assessment.KIND_ASSESS, {"choice": "yes", "confidence": confidence}, detail_meta)
    assert assessment._cached_detail(legacy, 1, 1, "fp", "policy", "fact1", "dim",
                                      {"yes": "SYNTH"}) == {"choice": "yes", "confidence": 0.9}
    assert extraction._cached_chunks(ledger, 1, 1, "fp", "hash", "r", [], "") == {}
    row = {"message_id": 1, "project_id": 1, "body_text": "SYNTH", "content_hash": "hash",
           "posted_at": "2026-01-01T00:00:00+09:00"}
    meta = {"hash": "hash", "ver": extract_llm.EXTRACT_VERSION,
            "chunk_size": extract_llm._CHUNK_SIZE, "context": extract_llm._chunk_context(row, None, checkpoint=True),
            "chunk": 0}
    add(ledger, "extract_llm_chunk", {"summary": "SYNTH"}, meta)
    assert extract_llm._saved_chunk_state(ledger, row)[1] == {0: {"summary": "SYNTH"}}


def test_no_ledger_detail_assessment_keeps_no_cache_mode():
    assert assessment._cached_detail(None, 1, 1, "fp", "policy", "fact1", "dim",
                                     {"yes": "SYNTH"}) is None
