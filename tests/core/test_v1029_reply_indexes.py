"""Per-tick reply scans and delivery lookups use partial indexes on a synthetic ledger."""
from contextlib import closing
import sqlite3

import pytest

from common_testkit import make_ledger

NEW_INDEXES = {"idx_messages_reply_first_seen", "idx_messages_reply_open",
               "idx_nrenders_state_updated", "idx_nparts_pending"}


def _indexes(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _plan(store, call):
    sql = []
    store.db.set_trace_callback(sql.append)
    try:
        call()
    finally:
        store.db.set_trace_callback(None)
    query = next(s for s in sql if s.lstrip().upper().startswith("SELECT"))
    return " | ".join(r[-1] for r in store.db.execute("EXPLAIN QUERY PLAN " + query))


@pytest.fixture
def store(tmp_path):
    store = make_ledger(tmp_path)
    rows = [(i, i % 50, None if i % 10 == 0 else i - i % 10, "full" if i % 3 else None,
             1800000000.0 + i) for i in range(1, 3001)]
    with store.db:
        store.db.executemany(
            "INSERT INTO messages(message_id,project_id,parent_id,body_state,first_seen) "
            "VALUES(?,?,?,?,?)", rows)
    store.db.execute("ANALYZE")
    yield store
    store.close()


def test_thread_read_candidates_uses_reply_first_seen_index(store):
    plan = _plan(store, lambda: store.thread_read_candidates(1800002990.0, 5))
    print("thread_read_candidates:", plan)
    assert "idx_messages_reply_first_seen" in plan


def test_replies_without_job_uses_open_reply_index(store):
    plan = _plan(store, lambda: store.replies_without_job())
    print("replies_without_job:", plan)
    assert "idx_messages_reply_open" in plan


def test_indexes_are_created_on_open_of_an_existing_db(tmp_path):
    make_ledger(tmp_path).close()
    with closing(sqlite3.connect(tmp_path / "ledger.db")) as db, db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        for name in NEW_INDEXES:
            db.execute(f"DROP INDEX {name}")
        assert not NEW_INDEXES & _indexes(db)
    store = make_ledger(tmp_path)
    try:
        assert NEW_INDEXES <= _indexes(store.db)
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == version
    finally:
        store.close()
