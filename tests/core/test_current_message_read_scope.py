"""Current-document sharing stays within one borrowed synthetic read view."""
from collections import Counter
from contextlib import closing
import asyncio
import json
import sqlite3

import pytest

import mcs_queries as queries
from test_inference_currentness import _add, db

__all__ = ["db"]


def test_read_scope_remains_part_of_the_loaded_candidate_contract(monkeypatch):
    import semantic_candidate
    monkeypatch.setattr(semantic_candidate, '_LOADED_SOURCES', None)
    contract = semantic_candidate.loaded_source_contract()
    assert contract == semantic_candidate.loaded_source_contract()


def _count_reads(monkeypatch):
    counts = Counter()
    read = queries._current_message_content

    def counted(db, params, *, facts):
        counts[(facts, params)] += 1
        return read(db, params, facts=facts)

    monkeypatch.setattr(queries, "_current_message_content", counted)
    return counts


def _seed(db, content=None):
    artifact = _add(db, "extract_v1", content=content if content is not None else {"value": "A"})
    db.commit()
    return artifact


def test_repeated_documents_are_fresh_nested_objects_and_scope_ends(db, monkeypatch):
    _seed(db, {"nested": {"items": [{"value": 1}]}, "__mcs_internal_proof_v1": {"private": True}})
    counts = _count_reads(monkeypatch)
    with queries.current_message_read_scope(db):
        scope = queries._message_reads.get()
        first = queries.current_message_extract(db, "extract_v1", 1)
        first["nested"]["items"][0]["value"] = 99
        first["extra"] = True
        second = queries.current_message_extract(db, "extract_v1", 1)
        assert second == {"nested": {"items": [{"value": 1}]}}
        assert sum(counts.values()) == 1
    assert queries._message_reads.get() is None
    assert scope.db is None and scope.cache == {}
    assert not db.in_transaction
    assert queries.current_message_extract(db, "extract_v1", 1) == second
    assert sum(counts.values()) == 2


def test_none_is_cached_but_a_caller_write_disables_the_whole_scope(db, monkeypatch):
    counts = _count_reads(monkeypatch)
    with queries.current_message_read_scope(db):
        assert queries.current_message_extract(db, "extract_v1", 1) is None
        assert queries.current_message_extract(db, "extract_v1", 1) is None
        assert sum(counts.values()) == 1
        _add(db, "extract_v1", content={"value": "new"})
        assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "new"}
        assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "new"}
        assert sum(counts.values()) == 3
    assert db.in_transaction  # the scope did not commit the caller's insert


def test_empty_document_is_not_confused_with_a_missing_document(db, monkeypatch):
    _seed(db, {})
    counts = _count_reads(monkeypatch)
    with queries.current_message_read_scope(db):
        first = queries.current_message_extract(db, "extract_v1", 1)
        assert first == {} and first is not None
        first["synthetic"] = []
        assert queries.current_message_extract(db, "extract_v1", 1) == {}
        assert queries.current_message_extract(db, "missing", 1) is None
        assert queries.current_message_extract(db, "missing", 1) is None
        assert sum(counts.values()) == 2


def test_rule_legacy_and_canonical_fact_selectors_keep_their_own_results(db, monkeypatch):
    _seed(db)
    _add(db, "extract_llm", content={"legacy": True})
    _add(db, "canonical_projection", content={"canonical": True})
    db.commit()
    counts = _count_reads(monkeypatch)
    with queries.current_message_read_scope(db):
        for _ in range(2):
            assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
            assert queries.current_message_extract(db, "extract_llm", 1) == {"legacy": True}
            assert queries.current_message_facts(db, 1) == {"canonical": True}
        assert sum(counts.values()) == 3


def test_sqlite_parameter_types_cannot_alias_a_memo_key(db):
    _add(db, "1", content={"kind": "integer text"})
    _add(db, "1.0", content={"kind": "float text"})
    db.commit()
    with queries.current_message_read_scope(db):
        assert queries.current_message_extract(db, 1, 1) == {"kind": "integer text"}
        assert queries.current_message_extract(db, 1.0, 1) == {"kind": "float text"}
        assert queries.current_message_extract(db, "1", 1) == {"kind": "integer text"}
        assert queries.current_message_extract(db, "1.0", 1) == {"kind": "float text"}


@pytest.mark.parametrize("mutation", ["content", "source", "project", "meta"])
def test_own_write_rollback_begin_cannot_revive_a_cached_document(db, monkeypatch, mutation):
    artifact = _seed(db)
    counts = _count_reads(monkeypatch)
    db.execute("BEGIN")
    with queries.current_message_read_scope(db):
        assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
        if mutation == "content":
            db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", ('{"value":"B"}', artifact))
            expected = {"value": "B"}
        elif mutation == "source":
            db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
            expected = None
        elif mutation == "project":
            db.execute("UPDATE messages SET project_id=20 WHERE message_id=1")
            expected = None
        else:
            db.execute("UPDATE artifacts SET meta='{}'")
            expected = None
        assert queries.current_message_extract(db, "extract_v1", 1) == expected
        db.rollback()
        db.execute("BEGIN")
        assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
        assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
        assert sum(counts.values()) == 4
    assert db.in_transaction


def test_schema_change_disables_memo_even_without_row_changes(db, monkeypatch):
    _seed(db)
    counts = _count_reads(monkeypatch)
    changes = db.total_changes
    with queries.current_message_read_scope(db):
        assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
        db.execute("CREATE TABLE synthetic_schema_marker(value)")
        assert db.total_changes == changes
        assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
        assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
        assert sum(counts.values()) == 3


def test_external_commit_stays_in_one_wal_view_and_next_scope_refreshes(db, tmp_path):
    _seed(db)
    path = tmp_path / "synthetic.db"
    with closing(sqlite3.connect(path)) as reader:
        reader.row_factory = sqlite3.Row
        db.backup(reader)
        reader.execute("PRAGMA journal_mode=WAL")
        with closing(sqlite3.connect(path)) as writer:
            with queries.current_message_read_scope(reader):
                assert queries.current_message_extract(reader, "extract_v1", 1) == {"value": "A"}
                writer.execute("UPDATE artifacts SET content=?", ('{"value":"B"}',))
                writer.commit()
                assert queries.current_message_extract(reader, "extract_v1", 1) == {"value": "A"}
            with queries.current_message_read_scope(reader):
                assert queries.current_message_extract(reader, "extract_v1", 1) == {"value": "B"}


def test_static_readonly_connection_is_unchanged(db, tmp_path):
    _seed(db)
    path = tmp_path / "synthetic ?# read.db"
    with closing(sqlite3.connect(path)) as destination:
        db.backup(destination)
    before = path.read_bytes()
    names = sorted(p.name for p in tmp_path.iterdir())
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        with queries.current_message_read_scope(reader):
            assert queries.current_message_extract(reader, "extract_v1", 1) == {"value": "A"}
            assert queries.current_message_extract(reader, "extract_v1", 1) == {"value": "A"}
        assert not reader.in_transaction
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == names


def test_rollback_begin_external_change_matches_a_fresh_same_connection_read(db, tmp_path):
    _seed(db)
    path = tmp_path / "synthetic.db"
    with closing(sqlite3.connect(path)) as reader:
        reader.row_factory = sqlite3.Row
        db.backup(reader)
        reader.execute("PRAGMA journal_mode=WAL")
        with closing(sqlite3.connect(path)) as writer:
            reader.execute("BEGIN")
            with queries.current_message_read_scope(reader):
                assert queries.current_message_extract(reader, "extract_v1", 1) == {"value": "A"}
                reader.rollback()
                writer.execute("UPDATE artifacts SET content=?", ('{"value":"B"}',))
                writer.commit()
                reader.execute("BEGIN")
                raw = queries._current_message_content(reader, ("extract_v1", 1), facts=False)
                assert queries.current_message_extract(reader, "extract_v1", 1) == json.loads(raw)
            assert reader.in_transaction
            reader.rollback()
            assert queries.current_message_extract(reader, "extract_v1", 1) == {"value": "B"}


def test_nested_scope_and_another_connection_do_not_share_memos(db, monkeypatch):
    _seed(db)
    counts = _count_reads(monkeypatch)
    with closing(sqlite3.connect(":memory:")) as other:
        other.row_factory = sqlite3.Row
        db.backup(other)
        other.execute("UPDATE artifacts SET content=?", ('{"value":"other"}',))
        other.commit()
        with queries.current_message_read_scope(db):
            assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
            assert queries.current_message_extract(other, "extract_v1", 1) == {"value": "other"}
            with queries.current_message_read_scope(db):
                assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
            assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
        assert sum(counts.values()) == 3


def test_proof_recursion_bypasses_but_does_not_poison_the_outer_memo(db, monkeypatch):
    _seed(db)
    counts = _count_reads(monkeypatch)
    proof = queries.selected_artifact_proof_valid

    def checked(db, artifact_id):
        return False if queries._proof_read_active.get() else proof(db, artifact_id)

    monkeypatch.setattr(queries, "selected_artifact_proof_valid", checked)
    with queries.current_message_read_scope(db):
        assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
        token = queries._proof_read_active.set(True)
        try:
            assert queries.current_message_extract(db, "extract_v1", 1) is None
        finally:
            queries._proof_read_active.reset(token)
        assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
        assert sum(counts.values()) == 2


def test_read_exception_is_not_memoized_and_retry_stays_uncached(db, monkeypatch):
    _seed(db)
    proof = queries.selected_artifact_proof_valid
    calls = []

    def interrupted(db, artifact_id):
        calls.append(artifact_id)
        if len(calls) == 1:
            raise sqlite3.OperationalError("synthetic interrupted read")
        return proof(db, artifact_id)

    monkeypatch.setattr(queries, "selected_artifact_proof_valid", interrupted)
    with queries.current_message_read_scope(db):
        with pytest.raises(sqlite3.OperationalError, match="synthetic interrupted"):
            queries.current_message_extract(db, "extract_v1", 1)
        assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
        assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
    assert len(calls) == 3


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_cleanup_preserves_borrowed_transaction_and_original_failure(db, failure):
    _seed(db)
    db.execute("UPDATE messages SET body_text='synthetic uncommitted'")
    changes = db.total_changes
    calls = []
    try:
        with pytest.raises(failure, match="synthetic body failure"):
            with queries.current_message_read_scope(db):
                assert queries.current_message_extract(db, "extract_v1", 1) == {"value": "A"}
                db.set_progress_handler(lambda: calls.append(1) or 1, 1)
                raise failure("synthetic body failure")
        assert queries._message_reads.get() is None and calls == []
        assert db.in_transaction and db.total_changes == changes
    finally:
        db.set_progress_handler(None, 0)
    assert db.execute("SELECT body_text FROM messages").fetchone()[0] == "synthetic uncommitted"
    db.rollback()
    assert db.execute("SELECT body_text FROM messages").fetchone()[0] == "synthetic source"


def test_oversized_document_is_returned_fresh_without_retaining_it(db, monkeypatch):
    value = {"text": "x" * (queries._MESSAGE_CACHE_BYTES // 4 + 1)}
    _seed(db, value)
    counts = _count_reads(monkeypatch)
    with queries.current_message_read_scope(db):
        assert queries.current_message_extract(db, "extract_v1", 1) == value
        assert queries.current_message_extract(db, "extract_v1", 1) == value
        assert queries._message_reads.get().cache == {}
    assert sum(counts.values()) == 2


def test_entry_bound_does_not_change_missing_document_results(db, monkeypatch):
    monkeypatch.setattr(queries, "_MESSAGE_CACHE_ENTRIES", 1)
    counts = _count_reads(monkeypatch)
    with queries.current_message_read_scope(db):
        for kind in ("first", "second", "first", "second"):
            assert queries.current_message_extract(db, kind, 1) is None
        assert len(queries._message_reads.get().cache) == 1
    assert sum(counts.values()) == 3


def test_unproven_connection_wrapper_keeps_original_uncached_reads(db, monkeypatch):
    _seed(db)
    counts = _count_reads(monkeypatch)

    class Borrowed:
        def execute(self, *args):
            return db.execute(*args)

    borrowed = Borrowed()
    with queries.current_message_read_scope(borrowed):
        assert queries.current_message_extract(borrowed, "extract_v1", 1) == {"value": "A"}
        assert queries.current_message_extract(borrowed, "extract_v1", 1) == {"value": "A"}
    assert sum(counts.values()) == 2
