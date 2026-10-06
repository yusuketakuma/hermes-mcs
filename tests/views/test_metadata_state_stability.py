"""Persisted contradictory metadata cannot become current or historical evidence."""
import json
import sqlite3

import pytest

from project_metadata import ARTIFACT_KIND
from project_metadata_view import get_project_metadata


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE patients(project_id INTEGER PRIMARY KEY,project_type TEXT,karte_id INTEGER);
        INSERT INTO patients VALUES(1,'medical',10);
        CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,kind TEXT,project_id INTEGER,content TEXT);
    """)
    yield conn
    conn.close()


def _capture(db, **changes):
    payload = {"contract": "project-metadata/1", "dataset": "care_team",
               "scope": "project", "entity_id": 1, "item_id": None,
               "complete": True, "reason": None, "http_status": None,
               "attempted_at": 100, "rows": []}
    payload.update(changes)
    db.execute("INSERT INTO artifacts(kind,project_id,content) VALUES(?,1,?)",
               (ARTIFACT_KIND, json.dumps(payload)))


@pytest.mark.parametrize("corruption", [
    {"reason": "network_error"}, {"http_status": 503}, {"attempted_at": -1},
    {"scope": "group"}, {"complete": False, "reason": None},
], ids=["failed_complete", "http_failed_complete", "negative_time", "wrong_scope", "unexplained_failure"])
def test_contradictory_latest_metadata_stays_unknown(db, corruption):
    _capture(db, **corruption)
    result = get_project_metadata(db, 1, "care_team", now=110, max_age_s=1000)
    assert result["state"] == "unknown" and result["reason"] == "artifact_invalid"
    assert not result["current_known"] and not result["historical"]


@pytest.mark.parametrize("corruption", [
    {"complete": 1}, {"complete": 1.0}, {"reason": "network_error"},
    {"http_status": 503}, {"attempted_at": -1}, {"scope": "group"},
])
def test_invalid_old_complete_cannot_be_used_as_historical_fallback(db, corruption):
    _capture(db, **corruption)
    _capture(db, complete=False, reason="network_error", attempted_at=110)
    result = get_project_metadata(db, 1, "care_team", now=120, max_age_s=1000)
    assert result["state"] == "unknown" and result["reason"] == "artifact_invalid"
    assert not result["historical"] and result["last_complete_at"] is None
