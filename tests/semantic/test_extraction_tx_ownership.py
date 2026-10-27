"""Semantic extraction cannot commit a transaction borrowed from its caller."""
import sqlite3

import pytest

import semantic_extraction as extraction
from semantic_testkit import BODY, _ledger, _llm, _message, _patient


@pytest.fixture
def store(tmp_path, monkeypatch):
    import extraction_refresh

    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body=BODY)], project_id=1)
    monkeypatch.setattr(extraction_refresh, "refresh_extraction_cards", lambda *args: None)
    yield db
    db.close()


def _member(store):
    row = store.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()
    return {"project_id": 1, "message_id": 1, "revision": row[0], "body_original": BODY}


@pytest.mark.parametrize("entry", [extraction.extract_facts_resumable, extraction.extract_facts_v2])
def test_borrowed_transaction_rejected_before_writes_or_model_call(store, entry):
    observer = sqlite3.connect(store.db.execute("PRAGMA database_list").fetchone()[2])
    member = _member(store)
    calls = []
    try:
        store.db.execute("UPDATE patients SET disease='synthetic-pending' WHERE project_id=1")
        with pytest.raises(RuntimeError, match="semantic_extraction_requires_idle_connection"):
            entry(lambda prompt: calls.append(prompt), member, ledger=store, source_fingerprint="synthetic")
        assert store.db.in_transaction and not calls
        assert store.db.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0] == "synthetic-pending"
        assert observer.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0] == ""
        assert observer.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        store.db.rollback()
        assert store.db.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0] == ""
    finally:
        observer.close()


def test_transaction_opened_during_model_call_is_not_committed_by_chunk_writer(store):
    observer = sqlite3.connect(store.db.execute("PRAGMA database_list").fetchone()[2])
    member = _member(store)

    def synthetic_model(prompt):
        store.db.execute("UPDATE patients SET disease='synthetic-pending' WHERE project_id=1")
        return _llm(prompt)

    try:
        with pytest.raises(RuntimeError, match="semantic_extraction_requires_idle_connection"):
            extraction.extract_facts_resumable(synthetic_model, member, ledger=store,
                                                source_fingerprint="synthetic")
        assert store.db.in_transaction
        assert observer.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0] == ""
        assert observer.execute("SELECT count(*) FROM artifacts WHERE kind=?", (extraction.KIND_CHUNK,)).fetchone()[0] == 0
        store.db.rollback()
        assert store.db.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0] == ""
    finally:
        observer.close()
