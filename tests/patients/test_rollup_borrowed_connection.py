"""Patient aggregation borrows the caller's connection and transaction."""
import rollup
from extract_testkit import _ledger, _message


def test_rollup_keeps_the_callers_open_transaction_and_connection(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        ledger.save_messages([_message(mid=1, body="完全合成の薬局状態記録です。")])
        db = ledger.db
        db.execute("BEGIN")
        db.execute("SAVEPOINT caller_owned")
        db.execute("UPDATE patients SET patient_name='合成人物' WHERE project_id=1")
        before = db.total_changes
        result = rollup.build_rollup(db, 1)
        assert result["project_id"] == 1
        assert db.in_transaction and db.total_changes == before
        # A helper commit/rollback would destroy this savepoint; close would make it unreadable.
        db.execute("ROLLBACK TO caller_owned")
        db.execute("RELEASE caller_owned")
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        db.rollback()
    finally:
        ledger.close()
