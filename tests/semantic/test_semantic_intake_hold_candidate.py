"""Synthetic intake evidence; recovery and old-reader fencing remain unfinished."""
import json

import pytest
from datetime import datetime, timezone

import semantic_intake_hold as hold
from semantic_testkit import _message, _seeded


def _held(db, mid):
    rows = db.db.execute(
        'SELECT * FROM artifacts WHERE kind=? AND project_id=1 AND model=? ORDER BY artifact_id',
        (hold.HOLD_KIND, '1')).fetchall()
    return [(row, json.loads(row['content'])) for row in rows
            if json.loads(row['content'])['target_id'] == mid]


def _future(db):
    row = db.db.execute("SELECT job_id,payload FROM fetch_jobs WHERE kind='semantic'").fetchone()
    payload = json.loads(row['payload'])
    payload['inference_engine_version'] = '2.0'
    with db.db:
        db.db.execute('UPDATE fetch_jobs SET payload=?,attempts=4 WHERE job_id=?',
                      (json.dumps(payload), row['job_id']))


def test_held_intent_retains_live_on_same_revision_quiet_seed(tmp_path):
    db = _seeded(tmp_path)
    try:
        _future(db)
        db.save_messages([_message(3, parent=1)], project_id=1,
                         notify={'source': 'unread'}, semantic=True)
        event = db.db.execute(
            "SELECT MAX(event_id) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
        row, live = _held(db, 3)[0]
        assert live['event_id'] == event
        db.semantic_seed(1, [3], {'source': 'history_import', 'notification_free': True})
        held = _held(db, 3)
        assert len(held) == 2 and held[0][0]['artifact_id'] == row['artifact_id']
        assert held[0][1] == live
        assert held[1][1]['fingerprint'] == live['fingerprint'] and held[1][1]['event_id'] is None
        db.save_messages([_message(3, parent=1, body='別の合成本文')],
                         project_id=1, semantic=True)
        held = _held(db, 3)
        assert len(held) == 3 and held[0][1] == live
        _, changed = held[-1]
        assert changed['fingerprint'] != live['fingerprint']
        assert changed['event_id'] is None
        assert db.db.execute('SELECT 1 FROM notify_outbox WHERE event_id=?', (event,)).fetchone()
    finally:
        db.close()


def test_held_intents_bind_only_targets_covered_by_arrival(tmp_path):
    db = _seeded(tmp_path)
    try:
        _future(db)
        stale = _message(3, parent=1, body='古い合成返信')
        live = _message(4, parent=1, body='新しい合成返信')
        live.posted_at = datetime.now(timezone.utc).isoformat()
        db.save_messages([stale, live], project_id=1, notify={'source': 'unread'},
                         semantic=True, notify_max_age_s=3600)
        event = db.db.execute("SELECT * FROM notify_outbox WHERE kind='new_messages' "
                              "ORDER BY event_id DESC LIMIT 1").fetchone()
        assert json.loads(event['payload'])['message_ids'] == [4]
        assert _held(db, 3)[0][1]['event_id'] is None
        assert _held(db, 4)[0][1]['event_id'] == event['event_id']
    finally:
        db.close()


def test_held_evidence_keeps_old_thread_generation_and_deduplicates_seed(tmp_path):
    db = _seeded(tmp_path)
    try:
        _future(db)
        db.save_messages([_message(3, parent=1)], project_id=1,
                         notify={'source': 'unread'}, semantic=True)
        event = db.db.execute("SELECT MAX(event_id) FROM notify_outbox "
                              "WHERE kind='new_messages'").fetchone()[0]
        original = _held(db, 3)[0][1]
        db.semantic_seed(1, [3], {'source': 'unread', 'event_id': event})
        assert len(_held(db, 3)) == 1
        with db.db:
            db.db.execute("UPDATE notify_outbox SET state='suppressed' WHERE event_id=?", (event,))
        db.save_messages([_message(4, parent=1)], project_id=1, semantic=True)
        second = _held(db, 4)[0][1]
        assert _held(db, 3)[0][1] == original
        assert original['event_id'] == event and second['event_id'] is None
        assert original['source_generation'] != second['source_generation']
    finally:
        db.close()


@pytest.mark.parametrize('raw', [
    None, '', b'\xff', '{', 'null', '[]',
    '{"inference_engine_version":"2.0", "future_only":"keep"}',
    '{"inference_engine_version":"1.0", "inference_engine_version":"2.0"}',
    '{"inference_engine_version":"2.0", "inference_engine_version":"1.0"}',
    '{"inference_engine_version":"1.0", "inference_engine_vers\\u0069on":"2.0"}',
    '{"targets": [1], "targets": [2]}',
    '{"targets": []}', '{"targets": null}',
])
def test_message_and_event_commit_without_changing_unknown_job(tmp_path, raw):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?, attempts=4 WHERE kind='semantic'", (raw,))
        before = tuple(db.db.execute(
            "SELECT payload,state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone())
        db.save_messages([_message(3, parent=1, body='合成の新着')], project_id=1,
                         notify={'source': 'unread'}, semantic=True)
        assert tuple(db.db.execute(
            "SELECT payload,state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()) == before
        assert db.db.execute('SELECT body_text FROM messages WHERE message_id=3').fetchone()[0] == '合成の新着'
        event = db.db.execute("SELECT event_id,payload FROM notify_outbox WHERE kind='new_messages' "
                              "ORDER BY event_id DESC LIMIT 1").fetchone()
        assert 3 in json.loads(event['payload'])['message_ids']
        assert _held(db, 3)[0][1]['event_id'] == event['event_id']
    finally:
        db.close()


def test_valid_legacy_target_not_yet_stored_keeps_existing_merge_contract(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind='semantic'",
                          (json.dumps({'targets': [1, 99]}),))
        db.save_messages([_message(3, parent=1)], project_id=1, semantic=True)
        payload = json.loads(db.db.execute(
            "SELECT payload FROM fetch_jobs WHERE kind='semantic'").fetchone()[0])
        assert payload['targets'] == [1, 3, 99]
        assert not _held(db, 3)
    finally:
        db.close()


def test_oversized_original_is_not_copied_and_new_message_is_saved(tmp_path):
    db = _seeded(tmp_path)
    try:
        raw = '{"synthetic":"' + 'x' * (1024 * 1024) + '"}'
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE kind='semantic'", (raw,))
        db.save_messages([_message(3, parent=1, body='合成の新着')], project_id=1,
                         notify={'source': 'unread'}, semantic=True)
        job = db.db.execute("SELECT length(CAST(payload AS BLOB)),payload,attempts FROM fetch_jobs "
                            "WHERE kind='semantic'").fetchone()
        assert job[0] == len(raw.encode()) and job[1] == raw and job[2] == 4
        assert _held(db, 3)[0][1]['target_id'] == 3
        assert db.db.execute('SELECT body_text FROM messages WHERE message_id=3').fetchone()[0] == '合成の新着'
    finally:
        db.close()


def test_hold_write_failure_rolls_back_message_and_event_together(tmp_path):
    import sqlite3
    db = _seeded(tmp_path)
    try:
        _future(db)
        db.db.execute("CREATE TRIGGER synthetic_hold_full BEFORE INSERT ON artifacts "
                      "WHEN NEW.kind='semantic_intake_hold_v1' BEGIN SELECT RAISE(ABORT,'full'); END")
        before = db.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            db.save_messages([_message(3, parent=1)], project_id=1,
                             notify={'source': 'unread'}, semantic=True)
        assert db.db.execute('SELECT 1 FROM messages WHERE message_id=3').fetchone() is None
        assert db.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0] == before
        assert not _held(db, 3) and not db.db.in_transaction
    finally:
        db.close()


def test_status_indicates_hold_and_missing_index_is_unverified(tmp_path):
    import semantic
    from semantic_testkit import _cfg

    db = _seeded(tmp_path)
    try:
        assert semantic.status_report(db, _cfg())['intake_hold'] == 'none_observed'
        _future(db)
        db.save_messages([_message(3, parent=1)], project_id=1, semantic=True)
        assert semantic.status_report(db, _cfg())['intake_hold'] == 'present'
        db.db.execute('DROP INDEX idx_semantic_intake_hold_root')
        assert semantic.status_report(db, _cfg())['intake_hold'] == 'unverified'
    finally:
        db.close()


def test_other_root_seeds_in_same_intake_transaction(tmp_path):
    db = _seeded(tmp_path)
    try:
        _future(db)
        before = tuple(db.db.execute(
            "SELECT payload,state,attempts FROM fetch_jobs WHERE kind='semantic' AND message_id=1").fetchone())
        db.save_messages([_message(3, parent=1), _message(20)], project_id=1,
                         notify={'source': 'unread'}, semantic=True)
        after = tuple(db.db.execute(
            "SELECT payload,state,attempts FROM fetch_jobs WHERE kind='semantic' AND message_id=1").fetchone())
        normal = db.db.execute("SELECT payload,state FROM fetch_jobs WHERE kind='semantic' "
                               "AND message_id=20").fetchone()
        assert after == before
        assert _held(db, 3) and normal is not None and normal['state'] == 'pending'
        assert json.loads(normal['payload'])['targets'] == [20]
        assert db.db.execute('SELECT COUNT(*) FROM messages WHERE message_id IN (3,20)').fetchone()[0] == 2
    finally:
        db.close()
