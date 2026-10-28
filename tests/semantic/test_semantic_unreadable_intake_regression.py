"""Synthetic regressions for unreadable and unsupported semantic jobs."""
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from inference_version import VERSION_KEY
import semantic_drain
import semantic_notice
import semantic
import maintenance
from semantic_testkit import _FakeJev, _cfg, _ledger, _llm, _message, _patient


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


@pytest.mark.parametrize('damage', ['broken', 'future'])
def test_held_intake_stays_visible_and_does_not_auto_resume(store, damage):
    original = _job(store)['payload']
    future = json.loads(original)
    future[VERSION_KEY] = '2.0'
    raw = '{"synthetic":' if damage == 'broken' else json.dumps(future)
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=?,attempts=4 WHERE job_id=?',
                         (raw, _job(store)['job_id']))
    before = tuple(_job(store))
    event = _arrival(store)
    store.save_messages([_message(3, parent=1)], project_id=1)
    store.semantic_seed(1, [3], {'source': 'history_import', 'notification_free': True})
    assert tuple(_job(store)) == before
    evidence = store.db.execute("SELECT CAST(content AS BLOB),meta FROM artifacts "
                                "WHERE kind='semantic_hold_evidence'").fetchall()
    assert evidence and evidence[0][0] == raw.encode()
    assert json.loads(evidence[0][1])['attempts'] == 4
    intents = [json.loads(row[0]) for row in store.db.execute(
        "SELECT content FROM artifacts WHERE kind='semantic_hold_intent' ORDER BY artifact_id")]
    assert len(intents) == 2
    assert intents[0]['targets'] == [2] and intents[0]['origin']['event_id'] == event['event_id']
    assert intents[1]['targets'] == [3] and intents[1]['origin']['notification_free'] is True
    status = semantic.status_report(store, _cfg())
    assert status['held_intakes'] == 2 and status['held_threads'] == 1
    assert status['held_job_inspection'] == 'unverified'

    # Even after independent repair and cancellation, the held intents must
    # not be merged or turned into semantic notifications automatically.
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?', (original, before[0]))
        store.db.execute("UPDATE notify_outbox SET state='suppressed' WHERE event_id=?", (event['event_id'],))
    repaired = tuple(_job(store))
    client = _FakeJev()
    semantic_drain.run_due(store, _cfg('enforce', loop_mode='off'), {'errors': []},
                           time.monotonic() + 300, jev_client=client, llm_fn=_llm)
    assert tuple(_job(store)) == repaired and not client.calls
    assert store.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='semantic_notice'").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_hold_intent'").fetchone()[0] == 2


def test_held_thread_does_not_block_an_unrelated_runnable_thread(store):
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload='{' WHERE kind='semantic'")
    _arrival(store)
    store.save_messages([_message(4)], project_id=1)
    store.semantic_seed(1, [4], {'source': 'history_import'})
    selected = semantic_drain._select_due(store, None, ('semantic',), False, 1)[0]
    assert selected and selected[0]['message_id'] == 4


def test_oversized_evidence_stays_in_original_job_without_a_second_copy(store):
    raw = '{"inference_engine_version":"2.0","pad":"' + 'a' * (1024 * 1024) + '"}'
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=?,attempts=4 WHERE kind=?', (raw, 'semantic'))
    before = tuple(_job(store))
    event = _arrival(store)
    assert tuple(_job(store)) == before
    evidence = store.db.execute("SELECT content,meta FROM artifacts WHERE kind='semantic_hold_evidence'").fetchone()
    assert evidence['content'] == '' and json.loads(evidence['meta'])['copied'] is False
    assert store.db.execute('SELECT state FROM notify_outbox WHERE event_id=?', (event['event_id'],)).fetchone()[0] == 'pending'
    intent = json.loads(store.db.execute("SELECT content FROM artifacts WHERE kind='semantic_hold_intent'").fetchone()[0])
    assert intent['targets'] == [2] and intent['origin']['event_id'] == event['event_id']


def test_held_intake_survives_retention_and_sqlite_backup(store, tmp_path):
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload='{' WHERE kind='semantic'")
    before = tuple(_job(store))
    event = _arrival(store)
    with store.db:
        maintenance.prune_history(store.db, now=time.time() + 10 * 365 * 86400)
    backup = tmp_path / 'synthetic-backup.db'
    with sqlite3.connect(backup) as destination:
        store.db.backup(destination)
    with sqlite3.connect(backup) as restored:
        restored.row_factory = sqlite3.Row
        assert tuple(restored.execute("SELECT job_id,payload,attempts,state FROM fetch_jobs "
                                      "WHERE kind='semantic' AND message_id=1").fetchone()) == before
        assert restored.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_hold_intent'").fetchone()[0] == 1
        assert restored.execute('SELECT state FROM notify_outbox WHERE event_id=?', (event['event_id'],)).fetchone()[0] == 'pending'


def test_control_and_manual_retry_keep_held_job_evidence(store):
    from adapters.common import human_commands
    import mcs_operations
    import mcs_requests
    payload = json.loads(_job(store)['payload'])
    payload[VERSION_KEY] = '2.0'
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=?,attempts=4 WHERE job_id=?',
                         (json.dumps(payload), _job(store)['job_id']))
    before = tuple(_job(store))
    _arrival(store)
    for command in ('ops.pause', 'ops.resume'):
        req = {'cmd': command, 'project_id': 1, 'command_id': 'synthetic-' + command,
               'actor': 'synthetic'}
        with store.db:
            error, result = mcs_operations._apply_control_tx(store.db, req, time.time())
        assert error is None and result['skipped_jobs'] == 1
        assert tuple(_job(store)) == before
    _, error, _ = human_commands._semantic_job(SimpleNamespace(db=store.db), mcs_requests, 1, before[0])
    assert error == 'semantic_intake_held'
    with store.db:
        error, _ = mcs_operations._apply_retry_tx(store.db, {'job_id': before[0], 'project_id': 1}, time.time())
    assert error == 'semantic_intake_held'
    assert tuple(_job(store)) == before


@pytest.mark.parametrize('payload', [
    {'inference_engine_version': '1.0', 'targets': []},
    {'inference_engine_version': '1.0', 'targets': [3]},
    {'inference_engine_version': '1.0', 'targets': [1], 'extra': 'a' * (1024 * 1024)},
])
def test_manual_retry_rejects_invalid_targets_or_oversize_without_a_hold(store, payload):
    from adapters.common import human_commands
    import mcs_operations
    import mcs_requests
    store.save_messages([_message(3)], project_id=1)
    raw = json.dumps(payload)
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload=?,state='failed',attempts=4 WHERE kind='semantic'", (raw,))
    before = tuple(_job(store))
    _, error, _ = human_commands._semantic_job(SimpleNamespace(db=store.db), mcs_requests, 1, before[0])
    assert error == 'invalid_payload'
    req = {'job_id': before[0], 'project_id': 1, 'command_id': 'synthetic-retry',
           'expected_payload_hash': mcs_requests.payload_hash(payload)}
    with store.db:
        error, _ = mcs_operations._apply_retry_tx(store.db, req, time.time())
    assert error == 'invalid_payload'
    assert tuple(_job(store)) == before


def test_held_thread_blocks_degraded_and_previously_queued_semantic_notices(store):
    import semantic_policy
    import semantic_send_gate
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload='{' WHERE kind='semantic'")
    event = _arrival(store)
    with store.db:
        store.db.execute('UPDATE notify_outbox SET created_at=1 WHERE event_id=?', (event['event_id'],))
    cfg = _cfg('enforce', loop_mode='off')
    scfg, errors = semantic_policy.runtime_config(cfg)
    assert not errors
    assert semantic_notice.emit_degraded(store, scfg) == 0
    assert not store.db.execute("SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'").fetchone()
    payload = {'root_id': 1, 'src_event_id': event['event_id'], 'fingerprint': 'synthetic',
               'policy_version': semantic_policy.POLICY_VERSION,
               'policy_fingerprint': semantic_policy.policy_fingerprint(scfg)}
    with pytest.raises(semantic_send_gate.DeferredSend, match='semantic_intake_held'):
        semantic_send_gate.semantic_gate(store.db, {'project_id': 1}, payload, cfg)
    with pytest.raises(semantic_send_gate.FreezeSend, match='semantic_intake_held'):
        semantic_send_gate.semantic_gate(store.db, {'project_id': 1}, payload, cfg, in_progress=True)
