"""Synthetic recovery of complete intake snapshots without losing authority."""
import hashlib
import json
import os
import sqlite3
import sys
import time

import pytest

import ledger
import semantic
import semantic_drain as drain
import semantic_intake_hold as hold
import semantic_runtime as runtime
from semantic_testkit import _cfg, _message, _seeded, _FakeJev, _llm
from writer_lock import acquire_run_lock


@pytest.fixture
def store(tmp_path):
    db = _seeded(tmp_path)
    try:
        yield db
    finally:
        db.close()


def _job(db):
    return db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic' AND project_id=1 AND message_id=1").fetchone()


def _hold(db, mid=1, *, event_id=None):
    origin = {'source': 'history_import', 'notification_free': True}
    if event_id is not None:
        origin = {'source': 'unread', 'event_id': event_id}
    with db.db:
        hold.hold_tx(db, 1, 1, [mid], origin,
                     source_generation=db._semantic_source_generation(1, 1))


def _release(db, plan=None, *, cfg=None):
    plan = plan or hold.prepare_release(db, 1, 1)
    path = db.db.execute('PRAGMA database_list').fetchone()[2]
    fd = acquire_run_lock(os.path.join(os.path.dirname(path), 'run.lock'))
    assert fd is not None
    try:
        return hold.release_root(db, 1, 1, expected_job_sha256=plan['job_sha256'],
                                 expected_hold_epoch=plan['hold_epoch'],
                                 expected_snapshot_sha256=plan['snapshot_sha256'],
                                 cfg=cfg or _cfg(), run_lock_fd=fd)
    finally:
        os.close(fd)


def _state(db):
    return {table: [tuple(row) for row in db.db.execute(f'SELECT * FROM {table} ORDER BY 1')]
            for table in ('fetch_jobs', 'artifacts', 'notify_outbox')}


def test_prepare_is_read_only_including_cli(store, tmp_path, monkeypatch, capsys):
    _hold(store)
    before = _state(store)
    plan = hold.prepare_release(store, 1, 1)
    reader = ledger.LedgerReader(str(tmp_path / 'ledger.db'))
    try:
        assert hold.prepare_release(reader, 1, 1) == plan
        assert reader.db.total_changes == 0
    finally:
        reader.close()
    monkeypatch.setattr(semantic, 'DB', str(tmp_path / 'ledger.db'))
    monkeypatch.setattr(sys, 'argv', ['semantic', '--offline', '--intake-prepare', '1', '1'])
    assert semantic.main() == 0
    assert json.loads(capsys.readouterr().out) == plan
    assert _state(store) == before


def test_release_keeps_evidence_and_fences_old_cas_with_idempotent_receipt(store, tmp_path):
    original = dict(_job(store))
    token = runtime.JobToken.from_row(original)
    _hold(store)
    intent = dict(store.db.execute('SELECT * FROM artifacts WHERE kind=?', (hold.HOLD_KIND,)).fetchone())
    plan = hold.prepare_release(store, 1, 1)
    outbox = _state(store)['notify_outbox']
    old = sqlite3.connect(str(tmp_path / 'ledger.db'))
    result = _release(store, plan)
    current = dict(_job(store))
    assert result['released'] == 1 and current['job_id'] != original['job_id']
    assert current['attempts'] == original['attempts']
    assert json.loads(current['payload'])['generation'] != token.generation
    archived = dict(store.db.execute('SELECT * FROM fetch_jobs WHERE job_id=?', (original['job_id'],)).fetchone())
    assert archived == {**original, 'kind': hold.ARCHIVE_KIND, 'message_id': original['job_id']}
    evidence = dict(store.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (intent['artifact_id'],)).fetchone())
    assert evidence == {**intent, 'kind': hold.RELEASED_KIND}
    assert hold.hold_epoch(store.db, 1) > plan['hold_epoch']
    assert not hold.has_held_root(store.db, 1, 1)
    assert not runtime.job_matches(store, token)
    # Actual pre-intake-guard SQL, on a connection opened before recovery.
    try:
        for action in ('done', 'failed', 'pending'):
            with old:
                changed = old.execute(
                    'UPDATE fetch_jobs SET state=? WHERE job_id=? AND kind=? AND project_id=? '
                    'AND message_id=? AND state=? AND attempts=? AND payload=?',
                    (action, token.job_id, token.kind, token.project_id, token.message_id,
                     token.state, token.attempts, token.payload_raw))
                assert changed.rowcount == 0
        with pytest.raises(RuntimeError, match='stale'):
            with old:
                old.execute("INSERT INTO artifacts(kind,content) VALUES('synthetic_old_result','{}')")
                changed = old.execute("UPDATE fetch_jobs SET state='done' WHERE job_id=? AND kind='semantic'",
                                      (token.job_id,))
                if changed.rowcount == 0:
                    raise RuntimeError('stale')
        assert old.execute("SELECT COUNT(*) FROM artifacts WHERE kind='synthetic_old_result'").fetchone()[0] == 0
    finally:
        old.close()
    after = _state(store)
    assert _release(store, plan) == result
    assert _state(store) == after
    assert _state(store)['notify_outbox'] == outbox


def test_same_input_keeps_attempt_limit_and_long_defer_count(store):
    payload = json.loads(_job(store)['payload'])
    payload['manual_attempt_limit'] = 7
    retry_at = time.time() + 3600
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET state='failed',attempts=7,payload=?,next_try=? WHERE kind='semantic'",
                         (json.dumps(payload), retry_at))
        drain._sched_write(store, {'long_defers': {str(_job(store)['job_id']): {'g': payload['generation'], 'n': 9}},
                                  'unrelated': {'keep': True}}, commit=False)
    _hold(store)
    result = _release(store)
    current = _job(store)
    assert (current['state'], current['attempts']) == ('failed', 7)
    assert current['next_try'] == retry_at
    assert runtime.attempt_limit(json.loads(current['payload'])) == 7
    expected = {str(result['job_id']): {'g': json.loads(current['payload'])['generation'], 'n': 9}}
    assert drain._long_defers(store) == expected
    assert drain._sched_state(store)['unrelated'] == {'keep': True}


@pytest.mark.parametrize('raw', ['{', '[]', '{"long_defers":null}', '{"long_defers":{"1":{"g":"x","n":-1}}}'])
def test_broken_scheduler_rejects_release_without_changes(store, raw):
    _hold(store)
    with store.db:
        drain._sched_write(store, {}, commit=False)
        store.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind='semantic_sched'", (raw,))
    before = _state(store)
    with pytest.raises(ValueError, match='semantic_scheduler_invalid'):
        hold.prepare_release(store, 1, 1)
    assert _state(store) == before


@pytest.mark.parametrize('mutation', ['source', 'event', 'attempts', 'scheduler', 'hold'])
def test_snapshot_change_refuses_whole_release(store, mutation):
    _hold(store)
    plan = hold.prepare_release(store, 1, 1)
    with store.db:
        if mutation == 'source':
            store.db.execute("UPDATE messages SET body_text='別の合成本文' WHERE message_id=2")
        elif mutation == 'event':
            store.db.execute("UPDATE notify_outbox SET state='suppressed' WHERE kind='new_messages'")
        elif mutation == 'attempts':
            store.db.execute("UPDATE fetch_jobs SET attempts=attempts+1 WHERE kind='semantic'")
        elif mutation == 'scheduler':
            drain._sched_write(store, {'long_defers': {}}, commit=False)
        else:
            hold.hold_tx(store, 1, 1, [2], {'source': 'history_import', 'notification_free': True},
                         source_generation=store._semantic_source_generation(1, 1))
    before = _state(store)
    with pytest.raises(ValueError, match='semantic_release_stale'):
        _release(store, plan)
    assert _state(store) == before


@pytest.mark.parametrize('raw', ['{', '[]', 'null', '{"targets":[]}',
                                '{"inference_engine_version":"2.0","targets":[1]}'])
def test_unknown_original_rights_are_never_replaced(store, raw):
    _hold(store)
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind='semantic'", (raw,))
    before = _state(store)
    with pytest.raises(ValueError, match='semantic_release_job_unverified'):
        hold.prepare_release(store, 1, 1)
    assert _state(store) == before


def test_worker_lock_and_unstored_markers_block_release(store):
    _hold(store)
    plan = hold.prepare_release(store, 1, 1)
    with drain._job_lock(store, _job(store)['job_id']) as locked:
        assert locked
        before = _state(store)
        with pytest.raises(ValueError, match='semantic_release_worker_active'):
            _release(store, plan)
        assert _state(store) == before
    hold.record_unstored(store, 1, roots=[1])
    before = _state(store)
    with pytest.raises(ValueError, match='semantic_release_unstored'):
        _release(store, plan)
    assert _state(store) == before


@pytest.mark.parametrize('fault', ['seed', 'receipt'])
def test_release_failure_rolls_back_archive_and_all_intent(store, monkeypatch, fault):
    _hold(store)
    plan = hold.prepare_release(store, 1, 1)
    before = _state(store)
    if fault == 'seed':
        def fail(*args, **kwargs):
            raise ValueError('synthetic_seed_failure')
        monkeypatch.setattr(store, 'semantic_seed_tx', fail)
    else:
        original = hold.canonical
        def fail(value):
            if isinstance(value, dict) and 'previous_long_defer' in value:
                raise ValueError('synthetic_receipt_failure')
            return original(value)
        monkeypatch.setattr(hold, 'canonical', fail)
    with pytest.raises(ValueError, match='synthetic_'):
        _release(store, plan)
    assert _state(store) == before and not store.db.in_transaction


@pytest.mark.parametrize('mutation', ['contract', 'holds', 'missing', 'job_id', 'reservation', 'reservation_negative'])
def test_invalid_receipt_never_returns_success(store, mutation):
    _hold(store)
    plan = hold.prepare_release(store, 1, 1)
    _release(store, plan)
    row = store.db.execute('SELECT artifact_id,content FROM artifacts WHERE kind=?', (hold.RELEASE_RECEIPT_KIND,)).fetchone()
    data = json.loads(row['content'])
    if mutation == 'contract':
        data['contract'] = 2
    elif mutation == 'holds':
        data['holds'] = 'abc'
    elif mutation == 'missing':
        del data['holds']
    elif mutation == 'job_id':
        data['new_job_id'] = True
    elif mutation == 'reservation':
        data['previous_long_defer'] = 'unknown'
    else:
        data['previous_long_defer'] = {'g': 'synthetic', 'n': -1}
    raw = json.dumps(data)
    with store.db:
        store.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                         (raw, json.dumps({'receipt_sha256': hashlib.sha256(raw.encode()).hexdigest()}), row['artifact_id']))
    before = _state(store)
    with pytest.raises(ValueError, match='semantic_release_receipt_unverified'):
        _release(store, plan)
    assert _state(store) == before


def test_same_intent_after_release_forms_new_episode(store):
    _hold(store)
    first = _release(store)
    _hold(store)
    assert hold.has_held_root(store.db, 1, 1)
    second = _release(store)
    assert second['job_id'] != first['job_id']
    assert store.db.execute('SELECT COUNT(*) FROM fetch_jobs WHERE kind=?', (hold.ARCHIVE_KIND,)).fetchone()[0] == 2
    assert store.db.execute('SELECT COUNT(*) FROM artifacts WHERE kind=?', (hold.RELEASED_KIND,)).fetchone()[0] == 2


@pytest.mark.parametrize('live_first', [True, False])
def test_live_and_quiet_holds_preserve_per_target_rights(store, live_first):
    store.save_messages([_message(3, parent=1), _message(4, parent=1)], project_id=1,
                        notify={'source': 'unread'})
    event = store.db.execute("SELECT MAX(event_id) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
    mids = [3, 4] if live_first else [4, 3]
    for mid in mids:
        _hold(store, mid, event_id=event if mid == 3 else None)
    # A quiet hold after the live hold must not erase its authority.
    _hold(store, 3)
    before_outbox = _state(store)['notify_outbox']
    _release(store)
    payload = json.loads(_job(store)['payload'])
    assert hold.notification_allowed(store, payload, 1, 3)
    assert not hold.notification_allowed(store, payload, 1, 4)
    assert not payload.get('notification_free')
    store.semantic_seed(1, [3], {'source': 'history_import', 'notification_free': True})
    payload = json.loads(_job(store)['payload'])
    assert hold.notification_allowed(store, payload, 1, 3) and not payload.get('notification_free')
    assert _state(store)['notify_outbox'] == before_outbox
    store.save_messages([_message(3, parent=1, body='編集後の合成投稿')], project_id=1)
    assert not hold.notification_allowed(store, payload, 1, 3)


@pytest.mark.parametrize('problem', ['shadow', 'trigger', 'event_missing', 'event_in_flight', 'source_changed'])
def test_unverified_dependencies_refuse_prepare(store, problem):
    event = store.db.execute("SELECT MAX(event_id) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
    _hold(store, event_id=event)
    with store.db:
        if problem == 'shadow':
            store.db.execute('CREATE TEMP TABLE messages(message_id INTEGER)')
        elif problem == 'trigger':
            store.db.execute("CREATE TRIGGER synthetic_modify AFTER UPDATE ON fetch_jobs BEGIN SELECT 1; END")
        elif problem == 'event_missing':
            store.db.execute('DELETE FROM notify_outbox WHERE event_id=?', (event,))
        elif problem == 'event_in_flight':
            store.db.execute("UPDATE notify_outbox SET state='in_flight' WHERE event_id=?", (event,))
        else:
            store.db.execute("UPDATE messages SET body_text='合成の変更' WHERE message_id=1")
    before = _state(store)
    with pytest.raises((ValueError, sqlite3.Error)):
        hold.prepare_release(store, 1, 1)
    assert _state(store) == before


def test_recovery_notice_dispatch_uses_per_target_authority(store):
    store.save_messages([_message(3, parent=1), _message(4, parent=1)], project_id=1,
                        notify={'source': 'unread'})
    event = store.db.execute("SELECT MAX(event_id) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
    _hold(store, 3, event_id=event)
    _hold(store, 4)
    _release(store)
    result = {'errors': []}
    out = drain.run_due(store, _cfg('enforce', budget=500), result, time.monotonic() + 300,
                        jev_client=_FakeJev(), llm_fn=_llm)
    assert out['done'] == 1, result
    notices = [json.loads(row[0]) for row in store.db.execute("SELECT payload FROM notify_outbox WHERE kind='semantic_notice'")]
    assert {row['target_message_id'] for row in notices} == {1, 2, 3}
    assert next(row for row in notices if row['target_message_id'] == 3)['src_event_id'] == event


@pytest.mark.parametrize('limit', ['holds', 'thread', 'events', 'source_bytes'])
def test_capacity_limit_never_partially_releases(store, limit):
    if limit == 'holds':
        with store.db:
            for number in range(129):
                hold.hold_tx(store, 1, 1, [1], {'source': f'synthetic-{number}', 'notification_free': True},
                             source_generation=store._semantic_source_generation(1, 1))
    elif limit == 'thread':
        store.save_messages([_message(mid, parent=1) for mid in range(3, 131)], project_id=1)
        _hold(store)
    elif limit == 'events':
        _hold(store)
        with store.db:
            for _ in range(129):
                store.db.execute("INSERT INTO notify_outbox(kind,project_id,payload,state,created_at) "
                                 "VALUES('new_messages',1,'{\"message_ids\":[1]}','pending',1)")
    else:
        store.save_messages([_message(mid, parent=1, body='x' * 900000) for mid in range(3, 8)], project_id=1)
        _hold(store)
    before = _state(store)
    with pytest.raises(ValueError, match='semantic_release_capacity'):
        hold.prepare_release(store, 1, 1)
    assert _state(store) == before


@pytest.mark.parametrize('missing', ['--expected-job-sha256', '--expected-snapshot-sha256', '--expected-hold-epoch'])
def test_release_cli_requires_plan_before_opening_writer(monkeypatch, missing):
    def forbid(*args, **kwargs):
        pytest.fail('writer must not open for missing release identity')
    args = {'--expected-job-sha256': 'a' * 64, '--expected-snapshot-sha256': 'b' * 64, '--expected-hold-epoch': '1'}
    args.pop(missing)
    monkeypatch.setattr(semantic, 'Ledger', forbid)
    monkeypatch.setattr(sys, 'argv', ['semantic', '--intake-release', '1', '1',
                                    *[item for pair in args.items() for item in pair]])
    with pytest.raises(SystemExit) as error:
        semantic.main()
    assert error.value.code == 2


def test_clock_reversal_keeps_unstored_observation_valid(store, monkeypatch):
    monkeypatch.setattr(hold.time, 'time', lambda: 2000.0)
    hold.record_unstored(store, 1)
    monkeypatch.setattr(hold.time, 'time', lambda: 1000.0)
    hold.record_unstored(store, 1)
    report = hold.preview_root(store.db, 1, 1)
    assert report['intake_unstored'] == 'present'
    assert report['intake_unstored_observation'] == {'failures': 2, 'first_at': 2000.0, 'last_at': 2000.0}


def test_original_arrival_is_required_even_when_every_hold_is_quiet(store):
    _hold(store)
    event_id = json.loads(_job(store)['payload'])['origin']['event_id']
    with store.db:
        store.db.execute('DELETE FROM notify_outbox WHERE event_id=?', (event_id,))
    before = _state(store)
    with pytest.raises(ValueError, match='semantic_hold_origin_unverified'):
        hold.prepare_release(store, 1, 1)
    assert _state(store) == before


def test_original_latest_arrival_need_not_cover_every_merged_target(store):
    store.save_messages([_message(3, parent=1)], project_id=1, notify={'source': 'unread'}, semantic=True)
    payload = json.loads(_job(store)['payload'])
    assert payload['targets'] == [1, 2, 3]
    assert hold._arrival_targets(store.db, 1, payload['origin']['event_id']) == {3}
    _hold(store, 3)
    _release(store)
    replay = json.loads(_job(store)['payload'])
    assert all(hold.notification_allowed(store, replay, 1, mid) for mid in (1, 2, 3))


def test_saved_origin_does_not_override_quiet_hold_authority(store):
    store.save_messages([_message(3, parent=1)], project_id=1, notify={'source': 'unread'})
    event_id = store.db.execute("SELECT MAX(event_id) FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
    store.save_messages([_message(4, parent=1)], project_id=1)
    _hold(store, 4, event_id=event_id)
    intent = json.loads(store.db.execute('SELECT content FROM artifacts WHERE kind=?', (hold.HOLD_KIND,)).fetchone()[0])
    assert intent['event_id'] is None and intent['origin']['event_id'] == event_id
    _release(store)
    assert not hold.notification_allowed(store, json.loads(_job(store)['payload']), 1, 4)


@pytest.mark.parametrize('column,value', [('next_try', None), ('created_at', -1), ('updated_at', 'unknown')])
def test_unknown_job_timing_never_creates_an_unclaimable_replay(store, column, value):
    _hold(store)
    with store.db:
        store.db.execute(f"UPDATE fetch_jobs SET {column}=? WHERE kind='semantic'", (value,))
    before = _state(store)
    with pytest.raises(ValueError, match='semantic_release_job_unverified'):
        hold.prepare_release(store, 1, 1)
    assert _state(store) == before
