"""Synthetic check: the shared semantic_policy reader returns the newest content or None."""
import sqlite3

import request_loops


def test_latest_policy_reads_the_newest_artifact_or_none():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE artifacts (artifact_id INTEGER PRIMARY KEY,"
               " kind TEXT, content TEXT)")
    assert request_loops.latest_policy(db) is None
    db.executemany("INSERT INTO artifacts (kind, content) VALUES (?, ?)",
                   [("semantic_policy", "a" * 64), ("semantic_policy", "b" * 64),
                    ("semantic_coverage", "c" * 64)])
    assert request_loops.latest_policy(db) == "b" * 64
