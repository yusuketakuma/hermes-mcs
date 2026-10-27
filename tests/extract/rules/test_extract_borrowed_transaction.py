"""Rule extraction must not finish a transaction owned by its caller."""

import sqlite3

import pytest

import extract
from extract_testkit import _ledger, _message


@pytest.mark.parametrize("entry", ["pending", "stale", "rows"])
def test_rule_extraction_rejects_borrowed_transaction_without_committing(tmp_path, entry):
    store = _ledger(tmp_path)
    try:
        store.save_messages([_message(body="完全合成の連絡です。")])
        path = store.db.execute("PRAGMA database_list").fetchone()[2]
        before = store.db.execute("SELECT is_unread FROM messages WHERE message_id=1").fetchone()[0]
        after = 0 if before else 1
        store.db.execute("BEGIN")
        store.db.execute("UPDATE messages SET is_unread=? WHERE message_id=1", (after,))
        with pytest.raises(ValueError, match="extract_transaction_owned"):
            if entry == "pending":
                extract.run_pending(store)
            elif entry == "stale":
                extract._delete_stale(store)
            else:
                extract._extract_rows(store, [])
        assert store.db.in_transaction
        assert store.db.execute("SELECT is_unread FROM messages WHERE message_id=1").fetchone()[0] == after
        with sqlite3.connect(path) as reader:
            assert reader.execute("SELECT is_unread FROM messages WHERE message_id=1").fetchone()[0] == before
        store.db.rollback()
        assert store.db.execute("SELECT is_unread FROM messages WHERE message_id=1").fetchone()[0] == before
    finally:
        store.close()
