"""Guard evidence requires both named relations, never two duplicate rows."""
import sqlite3
from contextlib import closing

import pytest

import ledger_audit


@pytest.mark.parametrize("relations", [["artifacts", "artifacts"], ["attachments", "attachments"]])
def test_duplicate_guard_rows_do_not_prove_a_missing_relation(relations):
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE ledger_relation_guards(relation TEXT, mode TEXT, existing_count INTEGER, shadow_count INTEGER)")
        db.executemany("INSERT INTO ledger_relation_guards VALUES(?, 'enforce', 0, 0)",
                       [(relation,) for relation in relations])
        assert ledger_audit.guard_status(db) == {"state": "unknown", "relations": []}


def test_both_distinct_guard_relations_keep_the_existing_known_report():
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE ledger_relation_guards(relation TEXT, mode TEXT, existing_count INTEGER, shadow_count INTEGER)")
        db.executemany("INSERT INTO ledger_relation_guards VALUES(?, 'enforce', 0, 0)",
                       [(relation,) for relation in ["artifacts", "attachments"]])
        report = ledger_audit.guard_status(db)
        assert report["state"] == "known"
        assert {row["relation"] for row in report["relations"]} == {"artifacts", "attachments"}
