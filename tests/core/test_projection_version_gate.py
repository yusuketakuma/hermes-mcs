"""Synthetic reader predicates reject obsolete projections without weakening scope."""
import json
import sqlite3

import pytest

from mcs_queries import (FACT_KINDS_SQL, current_fact_pred,
                         current_projection_id, current_v4_id)
from semantic_projection import PROJECTION_VERSION


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
      CREATE TABLE messages(message_id INTEGER PRIMARY KEY, project_id INTEGER,
                            content_hash TEXT, body_state TEXT);
      CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, kind TEXT,
                             message_id INTEGER, project_id INTEGER,
                             content TEXT, meta TEXT);
      INSERT INTO messages VALUES(1,10,'synthetic-source-hash','full');
    """)
    try:
        yield connection
    finally:
        connection.close()


def _add(db, kind, *, version=PROJECTION_VERSION, content=None, meta=None,
         project_id=10):
    binding = {"hash": "synthetic-source-hash", "engine_version": 4}
    if version is not None:
        binding["projection_version"] = version
    binding.update(meta or {})
    return db.execute(
        "INSERT INTO artifacts(kind,message_id,project_id,content,meta) VALUES(?,1,?,?,?)",
        (kind, project_id, json.dumps({} if content is None else content),
         json.dumps(binding))).lastrowid


def _selected(db, kind, *, require_version=True):
    predicate = current_projection_id if kind == "canonical_projection" else current_v4_id
    return db.execute(
        f"SELECT {predicate(require_version=require_version)} FROM messages m WHERE message_id=1"
    ).fetchone()[0]


@pytest.mark.parametrize("kind", ["canonical_projection", "semantic_facts_v4"])
@pytest.mark.parametrize("version", [None, PROJECTION_VERSION - 1,
                                      PROJECTION_VERSION + 1, str(PROJECTION_VERSION)])
def test_normal_read_rejects_noncurrent_version_without_deleting_history(db, kind, version):
    row = _add(db, kind, version=version, content={"meds": [{"dose": "50mg"}]})
    assert _selected(db, kind) is None
    assert _selected(db, kind, require_version=False) == row
    assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1


@pytest.mark.parametrize("kind", ["canonical_projection", "semantic_facts_v4"])
def test_newest_current_empty_projection_supersedes_older_content(db, kind):
    _add(db, kind, content={"meds": [{"dose": "5mg"}]})
    empty = _add(db, kind, content={})
    _add(db, kind, version=PROJECTION_VERSION - 1, content={"meds": [{"dose": "50mg"}]})
    assert _selected(db, kind) == empty


@pytest.mark.parametrize("kind", ["canonical_projection", "semantic_facts_v4"])
@pytest.mark.parametrize("fault", ["invalid_json", "invalid_meta", "array", "nonobject_meta",
                                   "payload_error", "meta_error", "old_hash", "wrong_project",
                                   "invalidated", "invalidated_number", "invalidated_string"])
def test_maintenance_version_exception_preserves_every_source_and_payload_fence(db, kind, fault):
    expected = _add(db, kind, version=PROJECTION_VERSION - 1)
    content = [] if fault == "array" else {"_error": True} if fault == "payload_error" else {}
    overrides = {
        "meta_error": {"error": True}, "old_hash": {"hash": "different-source"},
        "invalidated": {"invalidated": True}, "invalidated_number": {"invalidated": 2},
        "invalidated_string": {"invalidated": "yes"},
    }.get(fault, {})
    broken = _add(db, kind, version=PROJECTION_VERSION - 1, content=content,
                  meta=overrides, project_id=20 if fault == "wrong_project" else 10)
    if fault == "invalid_json":
        db.execute("UPDATE artifacts SET content='{' WHERE artifact_id=?", (broken,))
    elif fault == "invalid_meta":
        db.execute("UPDATE artifacts SET meta='{' WHERE artifact_id=?", (broken,))
    elif fault == "nonobject_meta":
        db.execute("UPDATE artifacts SET meta='[]' WHERE artifact_id=?", (broken,))
    assert _selected(db, kind) is None
    assert _selected(db, kind, require_version=False) == expected


def test_maintenance_v4_candidate_keeps_the_engine_pin(db):
    expected = _add(db, "semantic_facts_v4", version=PROJECTION_VERSION - 1)
    _add(db, "semantic_facts_v4", version=PROJECTION_VERSION - 1, meta={"engine_version": 3})
    assert _selected(db, "semantic_facts_v4", require_version=False) == expected


def test_current_fact_read_uses_legacy_fallback_until_a_current_projection_is_published(db):
    legacy = _add(db, "extract_llm", content={"meds": [{"dose": "5mg"}]})
    _add(db, "canonical_projection", version=PROJECTION_VERSION - 1,
         content={"meds": [{"dose": "50mg"}]})
    _add(db, "semantic_facts_v4", version=PROJECTION_VERSION - 1,
         content={"meds": [{"dose": "50mg"}]})
    sql = ("SELECT a.artifact_id FROM artifacts a JOIN messages m "
           "ON m.message_id=a.message_id "
           f"WHERE a.kind IN ({FACT_KINDS_SQL}) {current_fact_pred()}")
    assert db.execute(sql).fetchall() == [(legacy,)]
    current = _add(db, "canonical_projection", content={})
    assert db.execute(sql).fetchall() == [(current,)]
    v4 = _add(db, "semantic_facts_v4", content={})
    assert db.execute(sql).fetchall() == [(v4,)]
    db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
    assert db.execute(sql).fetchall() == []
