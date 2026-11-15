"""Synthetic: a rebuild after a patient-context reset does not re-announce old findings."""
import sqlite3

import patient_context_view as view


def _db(reset_at=None):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE messages(message_id INTEGER, project_id INTEGER, first_seen REAL, posted_at_ts REAL)")
    db.execute("CREATE TABLE message_revisions(message_id INTEGER, seq INTEGER, body_state TEXT, observed_at REAL, "
               "content_hash TEXT, prev_content_hash TEXT)")
    db.execute("CREATE TABLE artifacts(kind TEXT, created_at REAL)")
    db.executemany("INSERT INTO messages VALUES(?,?,?,?)", [(1, 7, 100.0, 90.0), (2, 7, 600.0, 95.0)])
    if reset_at is not None:
        db.execute("INSERT INTO artifacts VALUES(?,?)", (view.RESET_MARK_KIND, reset_at))
    return db


def _found(message_id):
    return {"message_id": message_id, "discovery": "backfill_found", "found_at": 700.0}


def _changes(db):
    ev = {"project_id": 7, "items": [_found(1), _found(2)], "as_of": 800.0, "coverage": {}}
    return view.changes(db, ev, since_ts=200.0)["by_discovery"]


def test_without_a_reset_every_finding_since_the_last_view_is_shown():
    groups = _changes(_db())
    assert [item["message_id"] for item in groups["backfill_found"]] == [1]
    assert [item["message_id"] for item in groups["retrospective"]] == [2]


def test_findings_in_records_seen_before_the_reset_are_not_new():
    groups = _changes(_db(reset_at=500.0))
    assert groups["backfill_found"] == []
    assert [item["message_id"] for item in groups["retrospective"]] == [2], "a record first seen later still counts"
