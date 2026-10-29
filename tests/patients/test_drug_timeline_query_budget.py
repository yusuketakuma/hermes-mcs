"""Synthetic occurrence-fence query budgets and borrowed-snapshot equivalence."""
import sqlite3

import pytest

import drug_timeline
import extract_llm
import inference_dependency_loss as loss
from test_drug_timeline import corpus, current, view_of

__all__ = ["corpus"]


def _report(store):
    # The first registration's failed compatibility probe is not traced by
    # SQLite. Warm it before counting so both paths have the same connection.
    extract_llm._register_thin_fact_count(store.db)
    statements = []
    store.db.set_trace_callback(statements.append)
    try:
        report = drug_timeline.timeline(view_of(store), 1, limit=2)
    finally:
        store.db.set_trace_callback(None)
    singles = [sql for sql in statements if sql.startswith(
        "SELECT " + ",".join(loss._SOURCE_FIELDS) + " FROM messages WHERE project_id=")
        or sql.startswith("SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=")]
    return report, len(statements), len(singles)


def test_timeline_reuses_selected_occurrence_fence(corpus, monkeypatch):
    for mid in range(1, 33):
        current(corpus, mid, [{"name": f"キラナ{mid}", "subject": "patient"}])
    corpus.db.commit()
    actual, actual_queries, extra_reads = _report(corpus)
    assert extra_reads == 0, (actual_queries, extra_reads)

    # Restore the old binding lookup on the same fixture without duplicating
    # timeline logic. The selected message/revision still supplies the new path.
    with monkeypatch.context() as patch:
        patch.setattr(loss, "source_binding_from_row", lambda row:
                      loss.source_binding(corpus.db, row["project_id"], row["message_id"]))
        expected, old_queries, old_extra_reads = _report(corpus)
    assert actual == expected
    assert old_extra_reads == 64
    assert old_queries - actual_queries == 64
    assert actual["coverage"]["stored_posts"] == 32
    assert len(actual["items"]) == 2 and actual["next_cursor"] is not None


@pytest.mark.parametrize("binding", [None, "f" * 64])
def test_unknown_or_stale_binding_is_still_unextracted(corpus, binding):
    current(corpus, 1, [{"name": "キラナ", "subject": "patient"}])
    corpus.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.source_binding',?) "
                      "WHERE kind='extract_llm'", (binding,))
    report, _, _ = _report(corpus)
    assert report["items"] == []
    assert report["coverage"]["no_current_extraction"] == 1
    assert corpus.db.in_transaction


@pytest.mark.parametrize("change", ["source", "revision"])
def test_transaction_local_source_or_aba_revision_is_not_reused(corpus, change):
    current(corpus, 1, [{"name": "キラナ", "subject": "patient"}])
    corpus.db.commit()
    original = drug_timeline.timeline(view_of(corpus), 1)
    assert original["items"]
    if change == "source":
        corpus.db.execute("UPDATE messages SET sender_name='別の合成投稿者' WHERE message_id=1")
    else:
        corpus.db.execute("INSERT INTO message_revisions VALUES(1,2,?,?,'full',1)", ("a" * 64, "a" * 64))
    changed = drug_timeline.timeline(view_of(corpus), 1)
    assert changed["items"] == []
    assert changed["coverage"]["no_current_extraction"] == 1
    assert corpus.db.in_transaction
    corpus.db.rollback()
    assert drug_timeline.timeline(view_of(corpus), 1) == original


def test_borrowed_snapshot_keeps_original_occurrence_until_caller_releases(corpus):
    current(corpus, 1, [{"name": "キラナ", "subject": "patient"}])
    corpus.db.commit()
    corpus.db.execute("PRAGMA journal_mode=WAL")
    corpus.db.execute("BEGIN")
    original = drug_timeline.timeline(view_of(corpus), 1)
    writer = sqlite3.connect(corpus.db.execute("PRAGMA database_list").fetchone()[2])
    try:
        with writer:
            writer.execute("UPDATE messages SET sender_name='別の合成投稿者' WHERE message_id=1")
        assert drug_timeline.timeline(view_of(corpus), 1) == original
        assert corpus.db.in_transaction
        corpus.db.rollback()
        changed = drug_timeline.timeline(view_of(corpus), 1)
        assert changed["items"] == []
        assert changed["coverage"]["no_current_extraction"] == 1
    finally:
        writer.close()


def test_revision_read_failure_propagates_without_releasing_caller_transaction(corpus):
    current(corpus, 1, [{"name": "キラナ", "subject": "patient"}])
    corpus.db.execute("UPDATE messages SET sender_name=sender_name WHERE message_id=1")

    def deny_revision(action, table, *_):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == "message_revisions" else sqlite3.SQLITE_OK

    corpus.db.set_authorizer(deny_revision)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            drug_timeline.timeline(view_of(corpus), 1)
        assert corpus.db.in_transaction
    finally:
        corpus.db.set_authorizer(None)
    corpus.db.rollback()


@pytest.mark.parametrize("revision", [0.5, "legacy-revision"])
def test_noninteger_legacy_revision_keeps_the_single_reader_result(corpus, revision):
    current(corpus, 1, [{"name": "キラナ", "subject": "patient"}])
    corpus.db.execute("INSERT INTO message_revisions VALUES(1,?,?,NULL,'full',1)", (revision, "a" * 64))
    message = dict(corpus.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    binding = loss.source_binding(corpus.db, 1, 1)
    message["_source_binding"] = binding
    context = extract_llm._thread_context(corpus.db, message)
    context_binding = extract_llm._chunk_context(message, context)
    corpus.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.source_binding',?,'$.context_binding',?) "
                      "WHERE kind='extract_llm'", (binding, context_binding))
    report = drug_timeline.timeline(view_of(corpus), 1)
    assert len(report["items"]) == 1
    assert report["coverage"]["no_current_extraction"] == 0
    assert corpus.db.in_transaction
