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
    assert rollup.dirty_projects(db) == []
    now[0] += 60
    assert rollup.rebuild(db, 1) == aid
    assert rollup.dirty_projects(db) == []


@pytest.mark.parametrize('field,value', [
    ('sender_name', '別の合成名'), ('sender_id', 2), ('sender_type', 'synthetic'),
    ('posted_at', '2026-09-21T10:00:00+09:00'), ('parent_id', 2),
])
def test_metadata_changes_are_immediately_dirty(db, monkeypatch, field, value):
    now = [1_790_000_000.0]
    monkeypatch.setattr(rollup.time, 'time', lambda: now[0])
    _setup(db, now)
    before = _hash(db, 1)
    message = _message(mid=1, body='合成の投稿', posted_at='2026-09-20T10:00:00+09:00')
    setattr(message, field, value)
    db.save_messages([message])
    assert _hash(db, 1) == before
    assert rollup.dirty_projects(db) == [1]


def test_deletion_possibility_changes_are_immediate(db, monkeypatch):
    now = [1_790_000_000.0]
    monkeypatch.setattr(rollup.time, 'time', lambda: now[0])
    _setup(db, now)
    older = _message(mid=2, body='古い合成投稿')
    db.save_messages([older])
    with db.db:
        db.db.execute('UPDATE messages SET updated_seen=? WHERE message_id=2',
                      (now[0] - rollup.STALE_DAYS * 86400 + 5,))
    rollup.rebuild(db, 1)
    assert json.loads(_content(db))['possibly_deleted'] == []
    now[0] += 10
    db.save_messages([_message(mid=1, body='合成の投稿', posted_at='2026-09-20T10:00:00+09:00')])
    assert rollup.dirty_projects(db) == [1]
    rollup.rebuild(db, 1)
    assert json.loads(_content(db))['possibly_deleted'] == [2]
    now[0] += 10
    db.save_messages([older])
    assert rollup.dirty_projects(db) == [1]
    rollup.rebuild(db, 1)
    assert json.loads(_content(db))['possibly_deleted'] == []


def test_registered_data_state_change_is_immediately_dirty(db, monkeypatch):
    import read_model
    now = [1_790_000_000.0]
    monkeypatch.setattr(rollup.time, 'time', lambda: now[0])
    registered = {'total': 1, 'records': [{'state': 'complete'}]}
    monkeypatch.setattr(read_model, '_registered_data', lambda *a: registered)
    _setup(db, now)
    registered['records'][0]['state'] = 'stale'
    assert rollup.dirty_projects(db) == [1]


def test_patient_scope_change_is_immediately_dirty(db, monkeypatch):
    now = [1_790_000_000.0]
    monkeypatch.setattr(rollup.time, 'time', lambda: now[0])
    _setup(db, now)
    with db.db:
        db.db.execute('UPDATE patients SET patient_name=? WHERE project_id=1', ('合成対象',))
    assert rollup.dirty_projects(db) == [1]


@pytest.mark.parametrize('change', ['new', 'edit', 'delete'])
def test_message_changes_are_immediately_dirty(db, monkeypatch, change):
    now = [1_790_000_000.0]
    monkeypatch.setattr(rollup.time, 'time', lambda: now[0])
    _setup(db, now)
    message = _message(mid=2 if change == 'new' else 1,
                       body='' if change == 'delete' else '変更後の合成投稿',
                       state='deleted' if change == 'delete' else 'full')
    db.save_messages([message])
    assert rollup.dirty_projects(db) == [1]


def test_karte_observation_refresh_is_immediately_dirty(db, monkeypatch):
    now = [1_790_000_000.0]
    monkeypatch.setattr(rollup.time, 'time', lambda: now[0])
    _setup(db, now)
    with db.db:
        db.db.execute('UPDATE patients SET karte_id=11 WHERE project_id=1')
    db.karte_summary_store(1, 11, None)
    rollup.rebuild(db, 1)
    now[0] += 10
    db.karte_summary_store(1, 11, None)
    assert rollup.dirty_projects(db) == [1]


def test_reobservation_does_not_call_the_heavy_builder(db, monkeypatch):
    now = [1_790_000_000.0]
    monkeypatch.setattr(rollup.time, 'time', lambda: now[0])
    _setup(db, now)
    before = _content(db)
    calls = []
    original = rollup.build_rollup

    def build(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(rollup, 'build_rollup', build)
    db.save_messages([_message(mid=1, body='合成の投稿', posted_at='2026-09-20T10:00:00+09:00')])
    rollup.rebuild_many(db, rollup.dirty_projects(db))
    assert calls == []
    assert _content(db) == before
