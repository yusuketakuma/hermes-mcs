"""Artifact readers borrow the caller's transaction and connection."""

import json
import sqlite3
from contextlib import closing

import pytest

from mcs_queries import iter_artifacts, karte_summary_block, karte_summary_current


@pytest.fixture
def connection():
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, "
                   "kind TEXT,project_id INTEGER,message_id INTEGER,content TEXT,meta TEXT)")
        db.commit()
        yield db


def test_reads_keep_the_borrowed_transaction_and_scope(connection):
    db = connection
    db.executemany("INSERT INTO artifacts VALUES(?,?,?,?,?,?)", [
        (1, "karte_summary", 1, None, '{"comment":"合成の旧記録"}', '{}'),
        (2, "karte_summary", 2, None, '{"comment":"合成の別患者"}', '{}'),
    ])
    db.commit()
    db.execute("BEGIN")
    db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?)", (
        3, "karte_summary", 1, 10, '{"comment":"合成の更新"}', '{"fetched_at":123}'))
    assert karte_summary_current(db, 1) == {"comment": "合成の更新", "fetched_at": 123}
    assert karte_summary_current(db, 2)["comment"] == "合成の別患者"
    assert karte_summary_current(db, 3) is None
    assert [r["artifact_id"] for r in iter_artifacts(db, "karte_summary", 1)] == [1, 3]
    assert [r["artifact_id"] for r in iter_artifacts(
        db, "karte_summary", 1, descending=True)] == [3, 1]
    assert [r["artifact_id"] for r in iter_artifacts(db, "karte_summary", message_id=10)] == [3]
    assert list(iter_artifacts(db, "absent")) == []
    assert db.in_transaction
    db.rollback()
    assert karte_summary_current(db, 1) == {"comment": "合成の旧記録", "fetched_at": None}
    assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 2


@pytest.mark.parametrize(("content", "meta", "expected"), [
    ("{", "{}", None),
    ("[]", "{}", None),
    ('{"empty":true}', "{", None),
    ('{"empty":true}', "[]", {"empty": True, "fetched_at": None}),
])
def test_summary_preserves_unreadable_and_missing_metadata(connection, content, meta, expected):
    connection.execute("INSERT INTO artifacts VALUES(1,'karte_summary',1,NULL,?,?)", (content, meta))
    assert karte_summary_current(connection, 1) == expected
    assert connection.in_transaction


def test_summary_projection_preserves_source_and_empty_state():
    content = {"comment": None, "updated_at": "", "updater": {"profession": "合成職種"},
               "empty": True, "fetched_at": 123}
    before = json.dumps(content, ensure_ascii=False, sort_keys=True)
    assert karte_summary_block(content) == {
        "comment": None, "updated_at": "", "updater_profession": "合成職種", "empty": True}
    assert json.dumps(content, ensure_ascii=False, sort_keys=True) == before
