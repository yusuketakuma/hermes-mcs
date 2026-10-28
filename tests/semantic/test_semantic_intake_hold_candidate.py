"""Synthetic intake evidence; recovery and old-reader fencing remain unfinished."""
import json
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
