"""Synthetic: a no-op source write must not leave a rollup dirty forever."""
import json

import pytest

from extract_testkit import _ledger, _message, _hash
import rollup


@pytest.fixture
def db(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.ensure_patient(1)
    yield ledger
    ledger.close()


def _setup(db, now):
    db.save_messages([_message(mid=1, body="合成の投稿",
                               posted_at="2026-09-20T10:00:00+09:00")])
    db.artifact_add("extract_v1", json.dumps({"symptoms": []}),
                    project_id=1, message_id=1, meta={"hash": _hash(db, 1)})
    aid = rollup.rebuild(db, 1)
    assert rollup.dirty_projects(db) == []
    now[0] += 60
    return aid


def _content(db):
    return db.db.execute("SELECT content FROM artifacts WHERE kind=?",
                         (rollup.KIND,)).fetchone()[0]


def test_llm_error_row_then_rebuild_is_clean(db, monkeypatch):
    now = [1_790_000_000.0]
    monkeypatch.setattr(rollup.time, "time", lambda: now[0])
    aid = _setup(db, now)
    content = _content(db)
    db.artifact_add("extract_llm", json.dumps({"error": "timeout"}),
                    project_id=1)
    assert rollup.dirty_projects(db) == [1]
    now[0] += 60
    assert rollup.rebuild(db, 1) == aid
    assert _content(db) == content
    assert rollup.dirty_projects(db) == []


def test_unchanged_resave_then_rebuild_is_clean(db, monkeypatch):
    now = [1_790_000_000.0]
    monkeypatch.setattr(rollup.time, "time", lambda: now[0])
    aid = _setup(db, now)
    db.save_messages([_message(mid=1, body="合成の投稿",   # unchanged
                               posted_at="2026-09-20T10:00:00+09:00")])
    assert rollup.dirty_projects(db) == [1]
    now[0] += 60
    assert rollup.rebuild(db, 1) == aid
    assert rollup.dirty_projects(db) == []
