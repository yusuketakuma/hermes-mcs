"""Synthetic regressions for unreadable and unsupported semantic jobs."""
import json

import pytest

from inference_version import VERSION_KEY
import semantic_drain
from semantic_testkit import _cfg, _ledger, _message, _patient


@pytest.fixture
def store(tmp_path):
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body='合成の親投稿です。')], project_id=1)
    db.semantic_seed(1, [1], {'source': 'history_import', 'notification_free': True})
    try:
        yield db
    finally:
        db.close()


def _job(db):
    return db.db.execute(
        "SELECT job_id,payload,attempts,state FROM fetch_jobs "
        "WHERE kind='semantic' AND project_id=1 AND message_id=1"
    ).fetchone()


def _arrival(db):
    db.save_messages([_message(2, parent=1, body='合成の新着返信です。')],
                     project_id=1, notify={'source': 'unread'}, semantic=True)
    event = db.db.execute(
        "SELECT event_id,payload,state,attempts FROM notify_outbox "
        "WHERE kind='new_messages' AND project_id=1 ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    assert event is not None and event['state'] == 'pending' and event['attempts'] == 0
    assert 2 in json.loads(event['payload'])['message_ids']
    assert db.db.execute('SELECT 1 FROM messages WHERE project_id=1 AND message_id=2').fetchone()
    return event


@pytest.mark.parametrize('entry', ['reprocess', 'arrival'])
def test_future_pending_job_is_not_downgraded_or_given_a_new_budget(store, entry):
    payload = json.loads(_job(store)['payload'])
    payload[VERSION_KEY] = '2.0'
    payload['future_only'] = {'synthetic': 'keep'}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE kind='semantic' AND message_id=1",
                         (raw,))
    before = tuple(_job(store))
    if entry == 'reprocess':
        semantic_drain.reprocess_step(store, _cfg(loop_mode='off'))
    else:
        _arrival(store)
    assert tuple(_job(store)) == before


def test_unreadable_job_keeps_bytes_and_arrival_outbox_intent(store):
    raw = '{"合成の破損":'
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE kind='semantic' AND message_id=1",
                         (raw,))
    before = tuple(_job(store))
    original_bytes = store.db.execute(
        "SELECT CAST(payload AS BLOB) FROM fetch_jobs WHERE job_id=?", (before[0],)
    ).fetchone()[0]
    event = _arrival(store)
    assert store.db.execute('SELECT state FROM notify_outbox WHERE event_id=?',
                            (event['event_id'],)).fetchone()[0] == 'pending'
    assert tuple(_job(store)) == before
    assert store.db.execute(
        "SELECT CAST(payload AS BLOB) FROM fetch_jobs WHERE job_id=?", (before[0],)
    ).fetchone()[0] == original_bytes
