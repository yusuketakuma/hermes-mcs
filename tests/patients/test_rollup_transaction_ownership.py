"""Rollup rebuild must not commit a caller's pending changes."""

import sqlite3

import pytest

from ledger import Ledger
import rollup


def test_rebuild_rejects_a_borrowed_transaction_without_finishing_it(tmp_path):
    path = tmp_path / "synthetic-rollup.db"
    ledger = Ledger(str(path))
    try:
        ledger.ensure_patient(1)
        ledger.db.execute("BEGIN")
        ledger.db.execute("UPDATE patients SET patient_name='合成名' WHERE project_id=1")
        ledger.db.execute("SAVEPOINT caller_state")
        with pytest.raises(sqlite3.OperationalError, match="inference_rollup_transaction_owned"):
            rollup.rebuild(ledger, 1)
        assert ledger.db.in_transaction
        assert ledger.db.execute("SELECT patient_name FROM patients WHERE project_id=1").fetchone()[0] == "合成名"
        with sqlite3.connect(path) as other:
            assert other.execute("SELECT patient_name FROM patients WHERE project_id=1").fetchone()[0] != "合成名"
        ledger.db.execute("ROLLBACK TO caller_state")
        ledger.db.execute("RELEASE caller_state")
        ledger.db.rollback()
        assert ledger.db.execute("SELECT patient_name FROM patients WHERE project_id=1").fetchone()[0] != "合成名"
        assert rollup.rebuild(ledger, 1) > 0
    finally:
        ledger.close()
