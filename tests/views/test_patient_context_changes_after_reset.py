"""Synthetic: a rebuild after a patient-context reset does not re-announce old findings."""
import json
import sqlite3

import patient_context_view as view


def _db(reset_at=None):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE messages(message_id INTEGER, project_id INTEGER, first_seen REAL, posted_at_ts REAL)")
    db.execute("CREATE TABLE message_revisions(message_id INTEGER, seq INTEGER, body_state TEXT, observed_at REAL, "
               "content_hash TEXT, prev_content_hash TEXT)")
    db.execute("CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, kind TEXT, project_id INTEGER, "
               "content TEXT, created_at REAL)")
    db.executemany("INSERT INTO messages VALUES(?,?,?,?)", [(1, 7, 100.0, 90.0), (2, 7, 600.0, 95.0)])
    if reset_at is not None:
        db.execute("INSERT INTO artifacts(kind,created_at) VALUES(?,?)", (view.RESET_MARK_KIND, reset_at))
    return db


def _found(message_id, category="病状"):
    return {"source": "chat", "message_id": message_id, "content_hash": "h%d" % message_id,
            "category": category, "discovery": "backfill_found", "found_at": 700.0}


def _changes(db, items=None):
    ev = {"project_id": 7, "items": items or [_found(1), _found(2)], "as_of": 800.0, "coverage": {}}
    return view.changes(db, ev, since_ts=200.0)["by_discovery"]


def test_without_a_reset_every_finding_since_the_last_view_is_shown():
    groups = _changes(_db())
    assert [item["message_id"] for item in groups["backfill_found"]] == [1]
    assert [item["message_id"] for item in groups["retrospective"]] == [2]


def test_findings_in_records_seen_before_the_reset_are_not_new():
    groups = _changes(_db(reset_at=500.0))
    assert groups["backfill_found"] == []
    assert [item["message_id"] for item in groups["retrospective"]] == [2], "a record first seen later still counts"


def _prior(db, *facts):
    db.execute("INSERT INTO artifacts(kind,project_id,content,created_at) VALUES(?,?,?,?)",
               (view.RESET_PRIOR_KIND, 7, json.dumps([view.reset_fact_key(f) for f in facts]), 500.0))


def test_a_fact_first_found_by_the_rebuild_in_an_old_record_is_shown():
    db = _db(reset_at=500.0)
    _prior(db, _found(1))
    allergy = _found(1, category="アレルギー")
    groups = _changes(db, [_found(1), allergy, _found(2)])
    assert groups["backfill_found"] == [allergy], "only the fact the erased context already held is hidden"
    assert [item["message_id"] for item in groups["retrospective"]] == [2]


def test_the_identity_ignores_the_rewritten_wording():
    worded = {**_found(1), "text": "rebuilt wording", "quote": "other quote"}
    assert view.reset_fact_key(worded) == view.reset_fact_key(_found(1))
    assert view.reset_fact_key({**_found(1), "content_hash": "edited"}) != view.reset_fact_key(_found(1))


def test_a_chat_fact_keeps_its_identity_when_the_view_drops_its_source_key():
    stored = {**_found(1), "source_key": 1}
    assert view.reset_fact_key(stored) == view.reset_fact_key({k: v for k, v in _found(1).items() if k != "source"})
    ocr = {**_found(1), "source": "attachment_ocr", "source_key": "attachment_ocr:a"}
    assert view.reset_fact_key(ocr) != view.reset_fact_key({**ocr, "source_key": "attachment_ocr:b"})


def test_an_empty_prior_shows_every_fact_first_found_after_the_reset():
    db = _db(reset_at=500.0)
    _prior(db)   # the patient had records but no published context when the reset ran
    groups = _changes(db)
    assert [item["message_id"] for item in groups["backfill_found"]] == [1]
