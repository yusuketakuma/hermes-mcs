"""Synthetic regressions for unreadable and unsupported semantic jobs."""
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from inference_version import VERSION_KEY
import semantic_drain
import semantic_notice
import semantic_runtime
import ledger
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


def _arrival(db, *, body='合成の新着返信です。'):
    db.save_messages([_message(2, parent=1, body=body)],
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
def test_arrival_is_requeued_with_its_origin_after_job_repair(store, damage):
    original = _job(store)['payload']
    future = json.loads(original)
    future[VERSION_KEY] = '2.0'
    raw = '{' if damage == 'broken' else json.dumps(future)
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?', (raw, _job(store)['job_id']))
    event = _arrival(store)
    held = tuple(_job(store))
    semantic_drain.reprocess_step(store, _cfg(loop_mode='off'))
    assert tuple(_job(store)) == held
    assert store.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_deferred_intake'").fetchone()[0] == 1

    # Simulate an independent repair or a newer implementation adopting the
    # held evidence; this test never silently rewrites the corrupt row itself.
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?', (original, held[0]))
    for _ in range(3):
        semantic_drain.reprocess_step(store, _cfg(loop_mode='off'))
        if not store.db.execute("SELECT 1 FROM artifacts WHERE kind='semantic_deferred_intake'").fetchone():
            break
    assert not store.db.execute("SELECT 1 FROM artifacts WHERE kind='semantic_deferred_intake'").fetchone()
    payload = json.loads(_job(store)['payload'])
    assert 2 in payload['targets']
    assert payload['origin']['event_id'] == event['event_id']
    assert payload['eligible'] is True and not payload.get('notification_free')
    assert store.db.execute('SELECT state FROM notify_outbox WHERE event_id=?',
                            (event['event_id'],)).fetchone()[0] == 'pending'


def test_deferred_intake_and_job_update_roll_back_together(store, monkeypatch):
    original = _job(store)['payload']
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload='{' WHERE job_id=?", (_job(store)['job_id'],))
    _arrival(store)
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?', (original, _job(store)['job_id']))
    before = tuple(_job(store))
    real_seed = store.semantic_seed_tx

    def interrupted(*args, **kwargs):
        real_seed(*args, **kwargs)
        raise RuntimeError('synthetic interruption')

    monkeypatch.setattr(store, 'semantic_seed_tx', interrupted)
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        semantic_drain.reprocess_step(store, _cfg(loop_mode='off'))
    assert tuple(_job(store)) == before
    assert store.db.execute("SELECT 1 FROM artifacts WHERE kind='semantic_deferred_intake'").fetchone()
    assert not store.db.in_transaction


@pytest.mark.parametrize('live_first', [False, True])
def test_deferred_live_and_quiet_origins_keep_their_notification_eligibility(store, live_first):
    from semantic_testkit import BODY
    original = _job(store)['payload']
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload='{' WHERE job_id=?", (_job(store)['job_id'],))
    store.save_messages([_message(3, parent=1)], project_id=1)
    old_event = store.outbox_add('new_messages', 1, {'source': 'unread', 'message_ids': [3]})

    def quiet():
        store.semantic_seed(1, [3], {'source': 'history_import', 'notification_free': True})

    if live_first:
        event = _arrival(store, body=BODY)
        quiet()
    else:
        quiet()
        event = _arrival(store, body=BODY)
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?', (original, _job(store)['job_id']))
    for _ in range(10):
        semantic_drain.reprocess_step(store, _cfg(loop_mode='off'), limit=1)
        if not store.db.execute("SELECT 1 FROM artifacts WHERE kind='semantic_deferred_intake'").fetchone():
            break
    assert not store.db.execute("SELECT 1 FROM artifacts WHERE kind='semantic_deferred_intake'").fetchone()
    payload = json.loads(_job(store)['payload'])
    assert {2, 3} <= set(payload['targets'])
    assert not payload.get('notification_free')
    assert semantic_notice.origin_event_id(store.db, 1, [2]) == event['event_id']
    assert semantic_notice.origin_event_id(store.db, 1, [3]) == old_event
    assert 3 in payload['deferred_quiet_targets']
    semantic_drain.run_due(store, _cfg('enforce', loop_mode='off'), {'errors': []},
                           time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)
    notices = [json.loads(r['payload']) for r in store.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind='semantic_notice'")]
    assert any(notice['target_message_id'] == 2 and notice['src_event_id'] == event['event_id']
               for notice in notices)
    assert all(notice['target_message_id'] != 3 for notice in notices)


@pytest.mark.parametrize('old_state', ['done', 'failed'])
def test_quiet_intake_does_not_inherit_finished_or_failed_live_origin(store, old_state):
    old_event = store.outbox_add('new_messages', 1, {'source': 'unread', 'message_ids': [1]})
    store.semantic_seed(1, [1], {'source': 'unread', 'event_id': old_event})
    original = _job(store)['payload']
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=?,state=? WHERE job_id=?',
                         ('{', old_state, _job(store)['job_id']))
    store.save_messages([_message(3, parent=1)], project_id=1)
    store.semantic_seed(1, [3], {'source': 'history_import', 'notification_free': True})
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?', (original, _job(store)['job_id']))
    semantic_drain.reprocess_step(store, _cfg(loop_mode='off'))
    payload = json.loads(_job(store)['payload'])
    assert 3 in payload['targets']
    assert payload['notification_free'] is True
    assert semantic_notice.origin_event_id(store.db, 1, [3]) is None


def test_new_live_arrival_clears_only_its_target_suppression(store):
    store.save_messages([_message(2, parent=1), _message(3, parent=1)], project_id=1)
    store.semantic_seed(1, [2, 3], {'source': 'history_import', 'notification_free': True})
    payload = json.loads(_job(store)['payload'])
    payload['deferred_quiet_targets'] = [2, 3]
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?',
                         (json.dumps(payload), _job(store)['job_id']))
    event = store.outbox_add('new_messages', 1, {'source': 'unread', 'message_ids': [2]})
    store.semantic_seed(1, [2], {'source': 'unread', 'event_id': event})
    payload = json.loads(_job(store)['payload'])
    assert payload['deferred_quiet_targets'] == [3]
    assert payload['eligible'] is True and not payload.get('notification_free')


@pytest.mark.parametrize('raw', [
    '{', '[]', 'null', '{"targets":[]}', '{"targets":null}',
    '{"targets":[true]}', '{"targets":[1],"targets":[1]}',
    '{"inference_engine_version":"2.0","targets":[1]}',
    '{"inference_engine_version":"1.0","targets":[1],"x":NaN}',
    '{"inference_engine_version":"1.0","targets":[1],"x":1e400}',
    '{"inference_engine_version":"1.0","targets":[1],"x":' + '[' * 129 + '0' + ']' * 129 + '}',
    '{"inference_engine_version":"1.0","targets":[1],"x":"' + 'a' * (1024 * 1024) + '"}',
])
def test_corrupt_or_future_jobs_are_not_revived_or_dispatched(store, raw):
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload=?,state='failed',attempts=4,updated_at=1 "
                         "WHERE kind='semantic'", (raw,))
    before = tuple(_job(store))
    assert semantic_drain.revive_failed(store, now=time.time() + 100000)['revived'] == 0
    assert tuple(_job(store)) == before
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET state='pending',next_try=0 WHERE kind='semantic'")
    before = tuple(_job(store))
    client = _FakeJev()
    semantic_drain.run_due(store, _cfg(loop_mode='off'), {'errors': []}, time.monotonic() + 300,
                           jev_client=client, llm_fn=_llm, max_jobs=1)
    assert not client.calls
    after = tuple(_job(store))
    assert after[:3] == before[:3]
    assert after[3] == ('pending' if '"2.0"' in raw or len(raw) > 1024 * 1024 else 'failed')


def test_deferred_intake_respects_deadline_and_keeps_other_roots_moving(store):
    store.save_messages([_message(3)], project_id=1)
    store.semantic_seed(1, [3], {'source': 'history_import', 'notification_free': True})
    original = _job(store)['payload']
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload='{' WHERE kind='semantic' AND message_id=1")
    _arrival(store)
    queued = store.db.execute("SELECT artifact_id FROM artifacts WHERE kind='semantic_deferred_intake'").fetchone()[0]
    semantic_drain.reprocess_step(store, _cfg(loop_mode='off'), limit=1, deadline=time.monotonic() - 1)
    assert store.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (queued,)).fetchone()
    selected = semantic_drain._select_due(store, None, ('semantic',), False, 1)[0]
    assert selected and selected[0]['message_id'] == 3
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=? WHERE kind=? AND message_id=?', (original, 'semantic', 1))
    for _ in range(3):
        semantic_drain.reprocess_step(store, _cfg(loop_mode='off'), limit=1)
        if not store.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (queued,)).fetchone():
            break
    assert not store.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (queued,)).fetchone()


def test_deferred_intake_survives_retention_and_sqlite_backup(store, tmp_path):
    original = _job(store)['payload']
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload='{' WHERE job_id=?", (_job(store)['job_id'],))
    event = _arrival(store)
    with store.db:
        maintenance.prune_history(store.db, now=time.time() + 10 * 365 * 86400)
    assert store.db.execute("SELECT 1 FROM artifacts WHERE kind='semantic_deferred_intake'").fetchone()
    backup = tmp_path / 'synthetic-backup.db'
    with sqlite3.connect(backup) as destination:
        store.db.backup(destination)
    restored = ledger.Ledger(str(backup))
    try:
        with restored.db:
            restored.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?', (original, _job(restored)['job_id']))
        semantic_drain.reprocess_step(restored, _cfg(loop_mode='off'))
        payload = json.loads(_job(restored)['payload'])
        assert payload['origin']['event_id'] == event['event_id']
        assert 2 in payload['targets'] and not payload.get('notification_free')
        assert not restored.db.execute("SELECT 1 FROM artifacts WHERE kind='semantic_deferred_intake'").fetchone()
    finally:
        restored.close()


def test_unrepaired_deferred_intake_does_not_starve_repaired_sibling(store):
    store.save_messages([_message(3)], project_id=1)
    store.semantic_seed(1, [3], {'source': 'history_import', 'notification_free': True})
    second = store.db.execute("SELECT job_id,payload FROM fetch_jobs "
                              "WHERE kind='semantic' AND message_id=3").fetchone()
    second_id, original = tuple(second)
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload='{' WHERE kind='semantic'")
    _arrival(store)
    store.save_messages([_message(4, parent=3)], project_id=1,
                        notify={'source': 'unread'}, semantic=True)
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?', (original, second_id))
    for _ in range(4):
        semantic_drain.reprocess_step(store, _cfg(loop_mode='off'), limit=1)
        second_payload = json.loads(store.db.execute(
            'SELECT payload FROM fetch_jobs WHERE job_id=?', (second_id,)).fetchone()[0])
        if 4 in second_payload['targets']:
            break
    assert 4 in second_payload['targets']
    assert _job(store)['payload'] == '{'
    assert store.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_deferred_intake'").fetchone()[0] == 1


def test_quarantine_preserves_existing_long_defer_evidence(store):
    job_id = _job(store)['job_id']
    original = _job(store)['payload']
    saved = {str(job_id): {'g': semantic_runtime.payload_generation(json.loads(original)), 'n': 4}}
    semantic_drain._sched_write(store, {'long_defers': saved})
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload='{' WHERE job_id=?", (job_id,))
    semantic_drain.run_due(store, _cfg(loop_mode='off'), {'errors': []}, time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm, max_jobs=1)
    sched = semantic_drain._sched_state(store)
    with store.db:
        semantic_drain._merge_long_defers(store, sched, {})
        semantic_drain._sched_write(store, sched, commit=False)
    assert not sched.get('long_defers')
    held = store.db.execute("SELECT content FROM artifacts WHERE kind='semantic_held_long_defer'").fetchone()
    assert json.loads(held[0]) == {'job_id': job_id, **saved[str(job_id)]}
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload=?,state='pending',next_try=0 WHERE job_id=?", (original, job_id))
    assert not semantic_drain._select_due(store, None, ('semantic',), False, 1)[0]
    semantic_drain._restore_held_long_defers(store, 1, time.monotonic() + 5)
    assert semantic_drain._long_defers(store) == saved
    assert not store.db.execute("SELECT 1 FROM artifacts WHERE kind='semantic_held_long_defer'").fetchone()
    assert store.db.execute("SELECT 1 FROM artifacts WHERE kind='semantic_held_long_defer_history'").fetchone()


def test_held_defer_evidence_does_not_consume_scheduler_capacity(store, monkeypatch):
    monkeypatch.setattr(semantic_drain, '_LONG_DEFERS_MAX', 2)
    store.save_messages([_message(3), _message(4)], project_id=1)
    store.semantic_seed(1, [3, 4], {'source': 'history_import', 'notification_free': True})
    jobs = store.db.execute("SELECT job_id,message_id,payload FROM fetch_jobs WHERE kind='semantic' ORDER BY job_id").fetchall()
    prior = {str(row['job_id']): {'g': semantic_runtime.payload_generation(json.loads(row['payload'])), 'n': 3}
             for row in jobs[:2]}
    semantic_drain._sched_write(store, {'long_defers': prior})
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload='{' WHERE kind='semantic' AND message_id IN (1,3)")
    sched = semantic_drain._sched_state(store)
    active = {str(jobs[2]['job_id']): {'g': semantic_runtime.payload_generation(json.loads(jobs[2]['payload'])), 'n': 1}}
    with store.db:
        semantic_drain._merge_long_defers(store, sched, active)
        semantic_drain._sched_write(store, sched, commit=False)
    assert sched['long_defers'] == active
    assert store.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_held_long_defer'").fetchone()[0] == 2


@pytest.mark.parametrize('raw', [
    '{"inference_engine_version":"1.0","inference_engine_version":"2.0","targets":[1]}',
    '{"inference_engine_version":"2.0","inference_engine_version":"1.0","targets":[1]}',
    '{"inference_engine_version":"1.0","inference_engine_\\u0076ersion":"2.0","targets":[1]}',
    '{"inference_engine_\\u0076ersion":"2.0","inference_engine_version":"1.0","targets":[1]}',
])
def test_ambiguous_version_keys_are_quarantined_in_either_order(store, raw):
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE kind='semantic'", (raw,))
    before = tuple(_job(store))
    semantic_drain.run_due(store, _cfg(loop_mode='off'), {'errors': []}, time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm, max_jobs=1)
    after = tuple(_job(store))
    assert after[:3] == before[:3]
    assert after[3] == 'failed'


@pytest.mark.parametrize('targets', [[], [3]])
def test_manual_retry_rejects_invalid_target_without_rewriting_payload(store, targets):
    from adapters.common import human_commands
    import mcs_operations
    import mcs_requests

    store.save_messages([_message(3)], project_id=1)
    raw = json.dumps({'inference_engine_version': '1.0', 'targets': targets})
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload=?,state='failed',attempts=4 WHERE kind='semantic'", (raw,))
    before = tuple(_job(store))
    _, error, _ = human_commands._semantic_job(SimpleNamespace(db=store.db), mcs_requests, 1, before[0])
    assert error == 'invalid_payload'
    req = {'job_id': before[0], 'project_id': 1, 'command_id': 'synthetic-retry',
           'expected_payload_hash': mcs_requests.payload_hash(json.loads(raw))}
    with store.db:
        error, _ = mcs_operations._apply_retry_tx(store.db, req, time.time())
    assert error == 'invalid_payload'
    assert tuple(_job(store)) == before


def test_quarantine_reads_only_a_bounded_candidate_page(store, monkeypatch):
    messages = [_message(mid) for mid in range(10, 90)]
    store.save_messages(messages, project_id=1)
    store.semantic_seed(1, [m.message_id for m in messages], {'source': 'history_import'})
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload=json_set(payload,'$.synthetic_padding',?) "
                         "WHERE kind='semantic'", ('a' * 65536,))
    original = ledger.semantic_job_payload
    calls = []

    def observed(raw, **kwargs):
        calls.append(len(raw.encode('utf-8')))
        return original(raw, **kwargs)

    monkeypatch.setattr(ledger, 'semantic_job_payload', observed)
    started = time.monotonic()
    semantic_drain._quarantine_invalid_jobs(store, 4, started + 10)
    elapsed = time.monotonic() - started
    print(f'synthetic_quarantine elapsed_s={elapsed:.3f} candidates={len(calls)} bytes={sum(calls)}')
    assert len(calls) <= 4 and sum(calls) <= 4 * ledger.SEMANTIC_JSON_MAX_BYTES
    assert store.db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='semantic' AND state='failed'").fetchone()[0] == 0
