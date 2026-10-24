"""command_receipts replay: absent, exact replay and conflict, read inside the
caller's BEGIN IMMEDIATE (synthetic ledger only)."""
import mcs_requests as requests
from ops_testkit import _create, _source

_RECEIPT_SELECT = "SELECT payload_hash,receipt_json FROM command_receipts"


def _store(db, command_id, digest, receipt_json):
    with db.db:
        db.db.execute("INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
                      (command_id, digest, None, None, "applied", receipt_json, 0.0))


def test_absent_exact_and_conflicting_receipts(tmp_path):
    db = _source(tmp_path)
    try:
        assert requests.replay_receipt(db.db, "cid-absent", "a" * 64) == (False, None)
        _store(db, "cid-1", "a" * 64, '{"outcome":"applied","n":1}')
        assert requests.replay_receipt(db.db, "cid-1", "a" * 64) == (True, {"outcome": "applied", "n": 1})
        assert requests.replay_receipt(db.db, "cid-1", "b" * 64) == (
            True, {"outcome": "rejected", "error": "command_id_conflict"})
        # a stored JSON null is still a found receipt, never an absent one
        _store(db, "cid-null", "c" * 64, "null")
        assert requests.replay_receipt(db.db, "cid-null", "c" * 64) == (True, None)
    finally:
        db.close()


def test_apply_command_replays_and_binds_the_payload_inside_its_transaction(tmp_path):
    db = _source(tmp_path)
    try:
        create = _create(db)
        statements = []
        db.db.set_trace_callback(statements.append)
        receipt = requests.apply_command(db, create)
        db.db.set_trace_callback(None)
        assert receipt["outcome"] == "applied"
        begin = next(i for i, sql in enumerate(statements) if sql.startswith("BEGIN IMMEDIATE"))
        reads = [i for i, sql in enumerate(statements) if sql.startswith(_RECEIPT_SELECT)]
        assert reads and all(i > begin for i in reads)      # the replay check runs in the write TX
        assert requests.apply_command(db, create) == receipt
        assert requests.apply_command(db, {**create, "title": "other"}) == {
            "outcome": "rejected", "error": "command_id_conflict"}
        assert db.db.execute("SELECT count(*) FROM command_receipts WHERE command_id=?",
                             (create["command_id"],)).fetchone()[0] == 1
        assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    finally:
        db.close()
