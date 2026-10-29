"""Synthetic intake evidence and guards against publishing through active holds."""
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
    '{"generation": 3}',
    '{"generation":"one", "generation":"two"}',
    '{"extra":{"same":1, "same":2}}',
    '{"extra":NaN}', '{"extra":1e400}',
    '{"extra":9223372036854775808}',
    '{"extra":' + '[' * 129 + '0' + ']' * 129 + '}',
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


def test_hold_prevents_old_token_result_and_both_reservations(tmp_path):
    import semantic_drain
    import semantic_runtime as runtime

    db = _seeded(tmp_path)
    try:
        row = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
        token = runtime.JobToken.from_row(row)
        original = tuple(row)
        assert runtime.job_matches(db, token)
        with db.db:
            hold.hold_tx(db, 1, 1, [1], {'source': 'history_import', 'notification_free': True},
                         source_generation=db._semantic_source_generation(1, 1))
        assert not runtime.job_matches(db, token)
        for action in ('done', 'retry', 'defer', 'failed'):
            assert not runtime.transition(db, token, action)
        with pytest.raises(ValueError, match='semantic_intake_held'):
            semantic_drain._reserve_long_defer(db, token)
        before_usage = db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_usage'").fetchone()[0]
        reserve = runtime.usage_reserver(db, token, kind='semantic_usage', model='synthetic',
                                         project_id=1, message_id=1, daily_request_budget=50)
        with pytest.raises(runtime.RuntimeStale):
            reserve({'synthetic': True}, 1.0)
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='semantic_usage'").fetchone()[0] == before_usage
        assert tuple(db.db.execute('SELECT * FROM fetch_jobs WHERE job_id=?', (token.job_id,)).fetchone()) == original
    finally:
        db.close()


@pytest.mark.parametrize('lane', [None, 'realtime', 'backlog'])
def test_hold_cursor_yields_other_roots_even_when_more_than_one_page(tmp_path, monkeypatch, lane):
    import semantic_drain
    import semantic_transport
    import time
    from inference_version import CURRENT_VERSION, VERSION_KEY
    from semantic_testkit import _FakeJev, _cfg, _ledger, _patient

    monkeypatch.setattr(semantic_transport, '_FMT_PATH', str(tmp_path / 'format.json'))
    monkeypatch.setattr(semantic_transport, '_LONG_PATH', str(tmp_path / 'long.json'))
    monkeypatch.setattr(semantic_transport, '_LONG_UNSAVED', {})
    monkeypatch.setattr(semantic_transport, '_LONG_MARK_FAILURES', {'busy': 0, 'error': 0})
    db = _ledger(tmp_path)
    try:
        _patient(db)
        now = time.time() - 10
        def add(mid, held):
            with db.db:
                db.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,attempts,"
                              "next_try,created_at,updated_at) VALUES('semantic',1,?,?,'pending',0,?,?,?)",
                              (mid, json.dumps({VERSION_KEY: CURRENT_VERSION, 'targets': [mid],
                                                'eligible': True, 'generation': f'g{mid}'}), now, now, now))
                if held:
                    db.db.execute('INSERT INTO artifacts(kind,project_id,model,meta,content,created_at) '
                                  'VALUES(?,?,?,?,?,?)',
                                  (hold.HOLD_KIND, 1, str(mid), f'{{"test":{mid}}}', '{}', now))
        if lane == 'realtime':
            add(999, False)
        for mid in range(100, 140):
            add(mid, True)
        if lane != 'realtime':
            add(999, False)
        before = [tuple(r) for r in db.db.execute(
            "SELECT job_id,payload,state,attempts,next_try FROM fetch_jobs WHERE message_id!=999 ORDER BY job_id")]
        served = []
        def process(ledger, scfg, job, jev_client, llm_fn, deadline, **kwargs):
            served.append(job['message_id'])
            kwargs['diagnostics']['not_sent'] = None
            return 'done'
        monkeypatch.setattr(semantic_drain, '_process_job', process)
        for _ in range(5):
            if served:
                break
            semantic_drain.run_due(db, _cfg('shadow'), {'errors': []}, time.monotonic() + 300,
                                   jev_client=_FakeJev(), llm_fn=None, lane=lane, max_jobs=1)
        assert served == [999]
        after = [tuple(r) for r in db.db.execute(
            "SELECT job_id,payload,state,attempts,next_try FROM fetch_jobs WHERE kind='semantic' "
            "AND message_id!=999 ORDER BY job_id")]
        assert after == before
    finally:
        db.close()


def test_existing_semantic_notice_waits_while_raw_arrival_keeps_its_body(tmp_path, monkeypatch):
    import notify_flush
    import semantic_send_gate
    from semantic_testkit import _cfg, _delivery_db, _semantic_event

    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg('enforce')
        monkeypatch.setattr(notify_flush, '_config', lambda: cfg)
        event = _semantic_event(db)
        payload = json.loads(event['payload'])
        semantic_send_gate.semantic_gate(db.db, event, payload, cfg)
        with db.db:
            hold.hold_tx(db, 1, 1, [1], {'source': 'history_import', 'notification_free': True},
                         source_generation=db._semantic_source_generation(1, 1))
        with pytest.raises(semantic_send_gate.DeferredSend, match='semantic_intake_held'):
            semantic_send_gate.semantic_gate(db.db, event, payload, cfg)
        with pytest.raises(semantic_send_gate.FreezeSend, match='semantic_intake_held'):
            semantic_send_gate.semantic_gate(db.db, event, payload, cfg, in_progress=True)
        raw = db.db.execute("SELECT * FROM notify_outbox WHERE kind='new_messages' ORDER BY event_id LIMIT 1").fetchone()
        text = notify_flush._format_event(db, raw)[0]
        assert 'カロナール' in text and '要約（自動検査済）' not in text
        assert semantic_send_gate.semantic_render_state(db.db, raw, cfg) == ()
        assert db.db.execute('SELECT state FROM notify_outbox WHERE event_id=?',
                             (event['event_id'],)).fetchone()[0] == event['state']
    finally:
        db.close()


def test_held_root_does_not_create_degraded_semantic_notice(tmp_path):
    import semantic_notice
    import semantic_policy
    from semantic_testkit import _cfg

    db = _seeded(tmp_path)
    try:
        _future(db)
        db.save_messages([_message(3, parent=1)], project_id=1,
                         notify={'source': 'unread'}, semantic=True)
        with db.db:
            db.db.execute("UPDATE notify_outbox SET created_at=0 WHERE kind='new_messages'")
        scfg, _ = semantic_policy.semantic_config(_cfg('enforce'))
        assert semantic_notice.emit_degraded(db, scfg) == 0
        assert db.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='semantic_notice'").fetchone()[0] == 0
        assert db.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='new_messages' AND state='pending'").fetchone()[0] >= 1
    finally:
        db.close()


def test_hold_inserted_between_format_and_snapshot_never_sends_old_summary(tmp_path, monkeypatch):
    import ledger
    import notify_flush
    import notify_sender
    from semantic_testkit import _cfg, _delivery_db, _semantic_event

    db = _delivery_db(tmp_path, [_message()])
    other = None
    try:
        cfg = _cfg('enforce')
        monkeypatch.setattr(notify_flush, '_config', lambda: cfg)
        _semantic_event(db)
        event_id = db.outbox_add('new_messages', 1, {'message_ids': [1], 'source': 'unread'})
        event = db.db.execute('SELECT * FROM notify_outbox WHERE event_id=?', (event_id,)).fetchone()
        other = ledger.Ledger(str(tmp_path / 'ledger.db'))
        original_format = notify_flush._format_event
        def format_then_hold(*args):
            rendered = original_format(*args)
            assert '要約（自動検査済）' in rendered[0]
            with other.db:
                other.db.execute('INSERT INTO artifacts(kind,project_id,model,meta,content,created_at) '
                                 'VALUES(?,?,?,?,?,0)', (hold.HOLD_KIND, 1, '1', '{}', '{}'))
            return rendered
        monkeypatch.setattr(notify_flush, '_format_event', format_then_hold)
        monkeypatch.setattr(notify_sender, 'hermes_free', lambda *_args: False)
        sent = []
        monkeypatch.setattr(notify_flush, '_send_marked', lambda *args, **kwargs: sent.append(args))
        result = {'sent': 0, 'failed': 0}
        notify_flush._send_text(db, event, cfg, [], 'synthetic', result, None)
        assert sent == [] and result.get('rerender') == 1
        assert db.db.execute('SELECT state FROM notify_outbox WHERE event_id=?', (event_id,)).fetchone()[0] == 'pending'
    finally:
        if other is not None:
            other.close()
        db.close()


def test_new_hold_wakes_waiting_reprocess_without_rewriting_scheduler(tmp_path):
    import semantic_drain
    from semantic_testkit import _cfg

    db = _seeded(tmp_path)
    try:
        cfg = _cfg(loop_mode='off')
        for _ in range(12):
            result = semantic_drain.reprocess_step(db, cfg)
            if result['status'] == 'waiting':
                break
        assert result['status'] == 'waiting'
        epoch_before = hold.hold_epoch(db.db)
        scheduler_before = db.db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic_sched'").fetchone()[0]
        _future(db)
        db.semantic_seed(1, [1], {'source': 'history_import', 'notification_free': True})
        assert hold.hold_epoch(db.db) > epoch_before
        assert db.db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic_sched'").fetchone()[0] == scheduler_before
        for _ in range(8):
            result = semantic_drain.reprocess_step(db, cfg)
            coverage = result.get('coverage') or {}
            if coverage.get('counts', {}).get('held'):
                break
        assert result.get('coverage', {}).get('counts', {}).get('held', 0) >= 1
        assert result['complete'] is False
    finally:
        db.close()


def test_reprocess_does_not_infer_no_holds_when_hold_epoch_index_missing(tmp_path):
    import semantic_drain
    from semantic_testkit import _cfg

    db = _seeded(tmp_path)
    try:
        db.db.execute('DROP INDEX idx_semantic_intake_hold_epoch')
        assert semantic_drain.reprocess_step(db, _cfg(loop_mode='off')) == {
            'status': 'intake_hold_unverified', 'complete': False}
    finally:
        db.close()


def test_unverified_hold_index_stops_dispatch_and_reports_reason(tmp_path, monkeypatch):
    import semantic_drain
    import semantic_transport
    import time
    from semantic_testkit import _FakeJev, _cfg

    monkeypatch.setattr(semantic_transport, '_FMT_PATH', str(tmp_path / 'format.json'))
    monkeypatch.setattr(semantic_transport, '_LONG_PATH', str(tmp_path / 'long.json'))
    monkeypatch.setattr(semantic_transport, '_LONG_UNSAVED', {})
    monkeypatch.setattr(semantic_transport, '_LONG_MARK_FAILURES', {'busy': 0, 'error': 0})
    db = _seeded(tmp_path)
    try:
        before = tuple(db.db.execute("SELECT payload,state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone())
        db.db.execute('DROP INDEX idx_semantic_intake_hold_root')
        calls = []
        monkeypatch.setattr(semantic_drain, '_process_job', lambda *a, **kw: calls.append(1))
        result = {'errors': []}
        out = semantic_drain.run_due(db, _cfg('shadow'), result, time.monotonic() + 300,
                                     jev_client=_FakeJev(), llm_fn=None)
        assert not calls and out['intake_hold_unverified'] is True
        assert 'semantic: intake_hold_unverified' in result['errors']
        assert tuple(db.db.execute("SELECT payload,state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()) == before
    finally:
        db.close()


@pytest.mark.parametrize('previous', [None, 2])
@pytest.mark.parametrize('unverified', [False, True])
def test_hold_or_unverified_state_keeps_long_defer_reservation(tmp_path, previous, unverified):
    import semantic_drain
    import semantic_runtime as runtime

    db = _seeded(tmp_path)
    try:
        row = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
        token = runtime.JobToken.from_row(row)
        if previous is not None:
            with db.db:
                semantic_drain._sched_write(db, {
                    'long_defers': {str(token.job_id): {'g': token.generation, 'n': previous}}}, commit=False)
        reservation = semantic_drain._reserve_long_defer(db, token)
        assert reservation is not None
        if unverified:
            db.db.execute('DROP INDEX idx_semantic_intake_hold_root')
        else:
            with db.db:
                hold.hold_tx(db, 1, 1, [1], {'source': 'history_import', 'notification_free': True},
                             source_generation=db._semantic_source_generation(1, 1))
        if unverified:
            with pytest.raises(ValueError, match='semantic_intake_hold_unverified'):
                semantic_drain._refund_long_defer(db, token, reservation)
        else:
            semantic_drain._refund_long_defer(db, token, reservation)
        assert semantic_drain._long_defers(db)[str(token.job_id)] == {
            'g': token.generation, 'n': (previous or 0) + 1}
    finally:
        db.close()


@pytest.mark.parametrize('previous', [None, 2])
def test_stale_after_reservation_keeps_long_defer_when_hold_arrives(tmp_path, monkeypatch, previous):
    import semantic_drain
    import semantic_runtime as runtime
    import semantic_transport
    import time
    from semantic_testkit import _FakeJev, _cfg

    monkeypatch.setattr(semantic_transport, '_FMT_PATH', str(tmp_path / 'format.json'))
    monkeypatch.setattr(semantic_transport, '_LONG_PATH', str(tmp_path / 'long.json'))
    monkeypatch.setattr(semantic_transport, '_LONG_UNSAVED', {})
    monkeypatch.setattr(semantic_transport, '_LONG_MARK_FAILURES', {'busy': 0, 'error': 0})
    db = _seeded(tmp_path)
    try:
        row = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
        token = runtime.JobToken.from_row(row)
        if previous is not None:
            with db.db:
                semantic_drain._sched_write(db, {
                    'long_defers': {str(token.job_id): {'g': token.generation, 'n': previous}}}, commit=False)
        def stale_after_hold(ledger, scfg, job, jev_client, llm_fn, deadline, **kwargs):
            # Models the status after long-output refusal is followed by a
            # stale partial-publication guard; exercise real settlement logic.
            with ledger.db:
                hold.hold_tx(ledger, 1, 1, [1], {'source': 'history_import', 'notification_free': True},
                             source_generation=ledger._semantic_source_generation(1, 1))
            return 'stale'
        monkeypatch.setattr(semantic_drain, '_process_job', stale_after_hold)
        semantic_drain.run_due(db, _cfg('shadow'), {'errors': []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=None, max_jobs=1)
        assert semantic_drain._long_defers(db)[str(token.job_id)] == {
            'g': token.generation, 'n': (previous or 0) + 1}
    finally:
        db.close()


@pytest.mark.parametrize('previous', [None, 2])
def test_long_output_then_hold_at_real_partial_guard_keeps_reservation(tmp_path, monkeypatch, previous):
    import semantic_assessment
    import semantic_audit
    import semantic_drain
    import semantic_runtime as runtime
    import semantic_store
    import semantic_transport
    import time
    from semantic_testkit import _FakeJev, _cfg

    monkeypatch.setattr(semantic_transport, '_FMT_PATH', str(tmp_path / 'format.json'))
    monkeypatch.setattr(semantic_transport, '_LONG_PATH', str(tmp_path / 'long.json'))
    monkeypatch.setattr(semantic_transport, '_LONG_UNSAVED', {})
    monkeypatch.setattr(semantic_transport, '_LONG_MARK_FAILURES', {'busy': 0, 'error': 0})
    db = _seeded(tmp_path)
    try:
        job = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
        token = runtime.JobToken.from_row(job)
        if previous is not None:
            with db.db:
                semantic_drain._sched_write(db, {
                    'long_defers': {str(token.job_id): {'g': token.generation, 'n': previous}}}, commit=False)
        monkeypatch.setattr(semantic_drain, '_assess_target', lambda *a, **kw: ({}, None))
        monkeypatch.setattr(semantic_drain, '_fact_stage', lambda *a, **kw: {
            'outcome': None, 'facts': [], 'v2_doc': None})
        monkeypatch.setattr(semantic_assessment, 'evaluate_medication_events', lambda *a, **kw: {
            'findings': [], 'complete': True, 'failure_reason': None})
        monkeypatch.setattr(semantic_audit, 'evaluate_source_fact_coverage', lambda *a, **kw: {
            'findings': [], 'evaluated': True})
        monkeypatch.setattr(semantic_store, 'refresh_cards_allowed', lambda *a: False)
        original_artifact = semantic_drain.current_artifact
        def artifact(ledger, kind, mid, *args):
            if kind == semantic_drain.KIND_SUMMARY and mid == 1:
                return {'content': {'claims': []}, 'meta': {'publication_mode': 'shadow'}}
            if kind == semantic_drain.KIND_AUDIT and mid == 1:
                return {'content': {'status': 'PASS', 'findings': []}, 'meta': {'audit_status': 'PASS'}}
            return original_artifact(ledger, kind, mid, *args)
        monkeypatch.setattr(semantic_drain, 'current_artifact', artifact)
        def reject_after_hold(ledger, scfg, bundle, llm_fn, guard, pid, mid, fp, policy,
                              facts, verdicts, existing, deadline, *, diagnostics):
            assert mid == 2
            with ledger.db:
                hold.hold_tx(ledger, 1, 1, [2], {'source': 'history_import', 'notification_free': True},
                             source_generation=ledger._semantic_source_generation(1, 1))
            return None, 'budget', {}, runtime.LLMNotSent('long_output_needs_budget')
        monkeypatch.setattr(semantic_drain, '_obtain_summary', reject_after_hold)
        result = {'errors': []}
        semantic_drain.run_due(db, _cfg('shadow', loop_mode='off'), result, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=None, max_jobs=1)
        assert _held(db, 2)
        assert semantic_drain._long_defers(db)[str(token.job_id)] == {
            'g': token.generation, 'n': (previous or 0) + 1}
        assert db.db.execute('SELECT state,attempts FROM fetch_jobs WHERE job_id=?',
                             (token.job_id,)).fetchone()[:] == ('pending', token.attempts)
    finally:
        db.close()


@pytest.mark.parametrize('event_kind', ['new_messages', 'semantic_notice'])
def test_hold_after_send_gate_before_marker_prevents_send(tmp_path, monkeypatch, event_kind):
    import notify_flush
    import notify_sender
    import semantic_send_gate
    from semantic_testkit import _cfg, _delivery_db, _semantic_event

    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg('enforce')
        monkeypatch.setattr(notify_flush, '_config', lambda: cfg)
        sem = _semantic_event(db)
        event_id = (sem['event_id'] if event_kind == 'semantic_notice' else
                    db.outbox_add('new_messages', 1, {'message_ids': [1], 'source': 'unread'}))
        event = db.db.execute('SELECT * FROM notify_outbox WHERE event_id=?', (event_id,)).fetchone()
        original = notify_flush._send_marked
        def hold_then_marker(*args, **kwargs):
            with db.db:
                hold.hold_tx(db, 1, 1, [1], {'source': 'history_import', 'notification_free': True},
                             source_generation=db._semantic_source_generation(1, 1))
            return original(*args, **kwargs)
        monkeypatch.setattr(notify_flush, '_send_marked', hold_then_marker)
        monkeypatch.setattr(notify_sender, 'hermes_free', lambda *a: False)
        sent = []
        monkeypatch.setattr(notify_sender, 'send', lambda *a, **kw: sent.append(1))
        result = {'sent': 0, 'failed': 0}
        if event_kind == 'semantic_notice':
            with pytest.raises(semantic_send_gate.DeferredSend, match='semantic_intake_held'):
                notify_flush._send_text(db, event, cfg, [], 'synthetic', result, None)
        else:
            notify_flush._send_text(db, event, cfg, [], 'synthetic', result, None)
            assert result['rerender'] == 1
        assert not sent
        row = db.db.execute('SELECT state,progress FROM notify_outbox WHERE event_id=?', (event_id,)).fetchone()
        assert row['state'] == 'pending'
        assert not json.loads(row['progress']).get('sending')
    finally:
        db.close()


def test_hold_before_second_marker_freezes_only_unfinished_chunks(tmp_path, monkeypatch):
    import notify_flush
    import notify_sender
    import semantic_send_gate
    from semantic_testkit import _cfg, _delivery_db

    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg('enforce')
        monkeypatch.setattr(notify_flush, '_config', lambda: cfg)
        monkeypatch.setattr(notify_flush, '_format_event', lambda *args: ('a' * 2200, []))
        monkeypatch.setattr(semantic_send_gate, 'semantic_render_state', lambda *args: ((1, 1, 'synthetic'),))
        monkeypatch.setattr(notify_sender, 'hermes_free', lambda *args: False)
        event_id = db.outbox_add('new_messages', 1, {'message_ids': [1], 'source': 'unread'})
        event = db.db.execute('SELECT * FROM notify_outbox WHERE event_id=?', (event_id,)).fetchone()
        sent = []
        monkeypatch.setattr(notify_sender, 'send', lambda *a, **kw: sent.append(a[1]))
        original = notify_flush._send_marked
        def hold_on_second(*args, **kwargs):
            if args[2] == 1:
                with db.db:
                    hold.hold_tx(db, 1, 1, [1], {'source': 'history_import', 'notification_free': True},
                                 source_generation=db._semantic_source_generation(1, 1))
            return original(*args, **kwargs)
        monkeypatch.setattr(notify_flush, '_send_marked', hold_on_second)
        result = {'sent': 0, 'failed': 0}
        with pytest.raises(semantic_send_gate.FreezeSend, match='semantic_intake_changed'):
            notify_flush._send_text(db, event, cfg, [], 'synthetic', result, None)
        progress = json.loads(db.db.execute('SELECT progress FROM notify_outbox WHERE event_id=?',
                                            (event_id,)).fetchone()[0])
        assert len(sent) == 1 and progress['sent'] == ['1'] and progress['next'] == 1
        assert progress['sending'] is None
    finally:
        db.close()


def test_hold_in_other_project_does_not_block_valid_notification(tmp_path, monkeypatch):
    import notify_flush
    import notify_sender
    from semantic_testkit import _cfg, _delivery_db, _semantic_event

    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg('enforce')
        monkeypatch.setattr(notify_flush, '_config', lambda: cfg)
        _semantic_event(db)
        event_id = db.outbox_add('new_messages', 1, {'message_ids': [1], 'source': 'unread'})
        event = db.db.execute('SELECT * FROM notify_outbox WHERE event_id=?', (event_id,)).fetchone()
        original = notify_flush._send_marked
        def unrelated_then_marker(*args, **kwargs):
            with db.db:
                db.db.execute('INSERT INTO artifacts(kind,project_id,model,meta,content,created_at) '
                              'VALUES(?,?,?,?,?,0)', (hold.HOLD_KIND, 2, '8', '{}', '{}'))
            return original(*args, **kwargs)
        monkeypatch.setattr(notify_flush, '_send_marked', unrelated_then_marker)
        monkeypatch.setattr(notify_sender, 'hermes_free', lambda *a: False)
        sent = []
        monkeypatch.setattr(notify_sender, 'send', lambda *a, **kw: sent.append(1))
        result = {'sent': 0, 'failed': 0}
        notify_flush._send_text(db, event, cfg, [], 'synthetic', result, None)
        assert sent == [1] and result['sent'] == 1
        assert db.db.execute('SELECT state FROM notify_outbox WHERE event_id=?', (event_id,)).fetchone()[0] == 'accepted'
    finally:
        db.close()


def test_hold_in_other_root_same_project_does_not_block_valid_notification(tmp_path, monkeypatch):
    import notify_flush
    import notify_sender
    from semantic_testkit import _cfg, _delivery_db, _semantic_event

    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg('enforce')
        monkeypatch.setattr(notify_flush, '_config', lambda: cfg)
        _semantic_event(db)
        event_id = db.outbox_add('new_messages', 1, {'message_ids': [1], 'source': 'unread'})
        event = db.db.execute('SELECT * FROM notify_outbox WHERE event_id=?', (event_id,)).fetchone()
        original = notify_flush._send_marked
        def unrelated_then_marker(*args, **kwargs):
            with db.db:
                db.db.execute('INSERT INTO artifacts(kind,project_id,model,meta,content,created_at) '
                              'VALUES(?,?,?,?,?,0)', (hold.HOLD_KIND, 1, '20', '{}', '{}'))
            return original(*args, **kwargs)
        monkeypatch.setattr(notify_flush, '_send_marked', unrelated_then_marker)
        monkeypatch.setattr(notify_sender, 'hermes_free', lambda *a: False)
        sent = []
        monkeypatch.setattr(notify_sender, 'send', lambda *a, **kw: sent.append(1))
        result = {'sent': 0, 'failed': 0}
        notify_flush._send_text(db, event, cfg, [], 'synthetic', result, None)
        assert sent == [1] and result['sent'] == 1
        assert db.db.execute('SELECT state FROM notify_outbox WHERE event_id=?', (event_id,)).fetchone()[0] == 'accepted'
    finally:
        db.close()


@pytest.mark.parametrize('count', [64, 65])
def test_summary_root_boundary_uses_bounded_project_epoch(tmp_path, monkeypatch, count):
    import notify_flush
    import notify_sender
    import semantic_send_gate
    from semantic_testkit import _cfg, _delivery_db

    db = _delivery_db(tmp_path, [_message(mid) for mid in range(1, count + 1)])
    try:
        cfg = _cfg('enforce')
        monkeypatch.setattr(notify_flush, '_config', lambda: cfg)
        monkeypatch.setattr(notify_flush, '_format_event', lambda *args: ('synthetic', []))
        monkeypatch.setattr(semantic_send_gate, 'semantic_render_state',
                            lambda *args: tuple((mid, mid, 'synthetic') for mid in range(1, count + 1)))
        monkeypatch.setattr(notify_sender, 'hermes_free', lambda *args: False)
        event_id = db.outbox_add('new_messages', 1, {'message_ids': [1], 'source': 'unread'})
        event = db.db.execute('SELECT * FROM notify_outbox WHERE event_id=?', (event_id,)).fetchone()
        original = notify_flush._send_marked
        def unrelated_then_marker(*args, **kwargs):
            with db.db:
                db.db.execute('INSERT INTO artifacts(kind,project_id,model,meta,content,created_at) '
                              'VALUES(?,?,?,?,?,0)', (hold.HOLD_KIND, 1, '999', '{}', '{}'))
            return original(*args, **kwargs)
        monkeypatch.setattr(notify_flush, '_send_marked', unrelated_then_marker)
        sent = []
        monkeypatch.setattr(notify_sender, 'send', lambda *a, **kw: sent.append(1))
        result = {'sent': 0, 'failed': 0}
        notify_flush._send_text(db, event, cfg, [], 'synthetic', result, None)
        if count <= 64:
            assert sent == [1] and result['sent'] == 1
        else:
            assert not sent and result['rerender'] == 1
    finally:
        db.close()


@pytest.mark.parametrize('change', ['suppress', 'target_removed', 'oversized', 'duplicate_targets'])
def test_origin_change_after_gate_prevents_semantic_send_before_marker(tmp_path, monkeypatch, change):
    import notify_flush
    import notify_sender
    import semantic_send_gate
    from semantic_testkit import _cfg, _delivery_db, _semantic_event

    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg('enforce')
        monkeypatch.setattr(notify_flush, '_config', lambda: cfg)
        event = _semantic_event(db)
        payload = json.loads(event['payload'])
        source_id = payload['src_event_id']
        original = notify_flush._send_marked
        def change_source_then_marker(*args, **kwargs):
            with db.db:
                if change == 'suppress':
                    db.db.execute("UPDATE notify_outbox SET state='suppressed' WHERE event_id=?", (source_id,))
                else:
                    if change == 'duplicate_targets':
                        raw = '{"message_ids":[1],"message_ids":[2]}'
                    else:
                        changed = ({'message_ids': [2]} if change == 'target_removed' else
                                   {'message_ids': [1], 'padding': 'x' * (1024 * 1024)})
                        raw = json.dumps(changed)
                    db.db.execute("UPDATE notify_outbox SET payload=? WHERE event_id=?", (raw, source_id))
            return original(*args, **kwargs)
        monkeypatch.setattr(notify_flush, '_send_marked', change_source_then_marker)
        monkeypatch.setattr(notify_sender, 'hermes_free', lambda *a: False)
        sent = []
        monkeypatch.setattr(notify_sender, 'send', lambda *a, **kw: sent.append(1))
        error = semantic_send_gate.DeferredSend if change in ('oversized', 'duplicate_targets') else semantic_send_gate.StaleSend
        reason = 'src_event_unverified' if change in ('oversized', 'duplicate_targets') else 'src_event_ineligible'
        with pytest.raises(error, match=reason):
            notify_flush._send_text(db, event, cfg, [], 'synthetic', {'sent': 0, 'failed': 0}, None)
        assert not sent
        row = db.db.execute('SELECT state,progress FROM notify_outbox WHERE event_id=?',
                            (event['event_id'],)).fetchone()
        assert row['state'] == 'pending' and not json.loads(row['progress']).get('sending')
        with db.db:
            db.db.execute('BEGIN IMMEDIATE')
            with pytest.raises(semantic_send_gate.FreezeSend, match=reason):
                semantic_send_gate.marker_source_gate(db.db, 1, payload, in_progress=True)
    finally:
        db.close()


@pytest.mark.parametrize('column,oversized', [('payload', 'x' * (1024 * 1024)),
                                               ('progress', 'x' * 65536)])
def test_oversized_origin_is_deferred_before_reading_full_value(tmp_path, column, oversized):
    import semantic_notice
    import semantic_policy
    import semantic_send_gate
    from semantic_testkit import _cfg, _delivery_db

    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg('enforce')
        src = db.outbox_add('new_messages', 1, {'message_ids': [1]})
        with db.db:
            db.db.execute('UPDATE notify_outbox SET created_at=0 WHERE event_id=?', (src,))
        assert semantic_notice.emit_degraded(db, semantic_policy.semantic_config(cfg)[0]) == 1
        payload = json.loads(db.db.execute("SELECT payload FROM notify_outbox WHERE kind='semantic_notice'").fetchone()[0])
        value = (json.dumps({'message_ids': [1], 'padding': oversized}) if column == 'payload'
                 else json.dumps({'sent': [], 'padding': oversized}))
        with db.db:
            db.db.execute(f'UPDATE notify_outbox SET {column}=? WHERE event_id=?', (value, src))
        before = tuple(db.db.execute('SELECT * FROM notify_outbox WHERE event_id=?', (src,)).fetchone())
        event = {'project_id': 1}
        with pytest.raises(semantic_send_gate.DeferredSend, match='src_event_unverified'):
            semantic_send_gate.semantic_gate(db.db, event, payload, cfg)
        with pytest.raises(semantic_send_gate.FreezeSend, match='src_event_unverified'):
            semantic_send_gate.semantic_gate(db.db, event, payload, cfg, in_progress=True)
        assert tuple(db.db.execute('SELECT * FROM notify_outbox WHERE event_id=?', (src,)).fetchone()) == before
    finally:
        db.close()


def test_source_reader_keeps_json_blob_compatibility(tmp_path):
    import semantic_notice
    import semantic_policy
    import semantic_send_gate
    from semantic_testkit import _cfg, _delivery_db

    db = _delivery_db(tmp_path, [_message()])
    try:
        src = db.outbox_add('new_messages', 1, {'message_ids': [1]})
        with db.db:
            db.db.execute('UPDATE notify_outbox SET payload=?,progress=?,created_at=0 WHERE event_id=?',
                          (b'{"message_ids":[1]}', b'{"sent":[]}', src))
        assert semantic_notice.emit_degraded(db, semantic_policy.semantic_config(_cfg('enforce'))[0]) == 1
        degraded = json.loads(db.db.execute("SELECT payload FROM notify_outbox WHERE kind='semantic_notice'").fetchone()[0])
        row, payload = semantic_send_gate._source_event(db.db, 1, src)
        assert payload == {'message_ids': [1]} and json.loads(row['progress']) == {'sent': []}
        with db.db:
            db.db.execute('BEGIN IMMEDIATE')
            semantic_send_gate.marker_source_gate(db.db, 1,
                {'src_event_id': src, 'target_message_id': 1})
            semantic_send_gate.marker_source_gate(db.db, 1,
                degraded, cfg=_cfg('enforce'))
    finally:
        db.close()


@pytest.mark.parametrize('encoding', ['UTF-16le', 'UTF-16be'])
def test_source_reader_decodes_utf16_database_text(tmp_path, encoding):
    import sqlite3
    import ledger
    import semantic_send_gate

    path = tmp_path / 'synthetic-utf16.db'
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA encoding='{encoding}'")
        conn.execute('CREATE TABLE synthetic_encoding_anchor(x INTEGER)')
        conn.commit()
    finally:
        conn.close()
    db = ledger.Ledger(str(path))
    try:
        assert db.db.execute('PRAGMA encoding').fetchone()[0].lower() == encoding.lower()
        src = db.outbox_add('new_messages', 1, {'message_ids': [1], 'source': '合成'})
        row, payload = semantic_send_gate._source_event(db.db, 1, src)
        assert row['state'] == 'pending' and payload['source'] == '合成'
    finally:
        db.close()
