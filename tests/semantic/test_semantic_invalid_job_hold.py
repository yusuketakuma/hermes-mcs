"""Synthetic damaged jobs stay held without blocking unrelated work."""
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

import semantic_drain as drain
import semantic_runtime as runtime
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _cfg, _FakeJev, _ledger, _llm, _message, _patient


def _job(db, mid):
    return db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic' AND project_id=1 AND message_id=?",
                         (mid,)).fetchone()


@pytest.mark.parametrize('payload', [
    '{', '[]', 'null', '{"targets":[]}', '{"targets":null}',
    '{"targets":[true]}', '{"targets":[2]}',
    '{"targets":[1],"targets":[2]}',
    '{"inference_engine_version":"1.0","inference_engine_version":"2.0","targets":[1]}',
    '{"inference_engine_version":null,"targets":[1]}',
    '{"inference_engine_version":"2.0","targets":[1]}',
    '{"inference_engine_version":"1.0","targets":[1],"extra":1e400}',
    '{"inference_engine_version":"1.0","targets":[1],"extra":' + '[' * 130 + '0' + ']' * 130 + '}',
])
def test_bad_job_does_not_overwrite_or_block_other_root(tmp_path, payload):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1), _message(2), _message(3)], project_id=1)
        assert db.semantic_seed(1, [1], {'source': 'synthetic'}) is None
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=3,reason_code='network_error' "
                          "WHERE kind='semantic' AND message_id=1", (payload,))
        before = dict(_job(db, 1))
        first = drain.reprocess_step(db, _cfg(loop_mode='off'), limit=2)
        assert first['cursor'] == 2 and first['counts']['held'] == 1
        assert dict(_job(db, 1)) == before
        assert _job(db, 2) is not None
        selected = drain._select_due(db, None, ('semantic',), False, 1)[0]
        assert selected and selected[0]['message_id'] == 2
        assert db.semantic_seed(1, [1], {'source': 'synthetic'}) == {1}
        assert dict(_job(db, 1)) == before
        client = _FakeJev()
        result = drain.run_due(db, _cfg(loop_mode='off'), {'errors': []},
                               time.monotonic() + 300, jev_client=client, llm_fn=_llm, max_jobs=1)
        assert all(metric['job_id'] != before['job_id'] for metric in result['job_metrics'])
        assert any(metric['job_id'] == _job(db, 2)['job_id'] for metric in result['job_metrics'])
        assert dict(_job(db, 1)) == before
    finally:
        db.close()


def test_legacy_default_target_is_valid_and_duplicate_keys_are_not(tmp_path):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1)], project_id=1)
        db.semantic_seed(1, [1], {'source': 'synthetic'})
        def admissible():
            return db.db.execute('SELECT ' + runtime.admissible_job_sql() +
                                 " FROM fetch_jobs WHERE kind='semantic'").fetchone()[0]
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{}' WHERE kind='semantic'")
        assert admissible() == 1
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind='semantic'",
                          (json.dumps({VERSION_KEY: CURRENT_VERSION, 'targets': [1]}),))
        assert admissible() == 1
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind='semantic'",
                          ('{"targets":[1],"targets":[1]}',))
        assert admissible() == 0
    finally:
        db.close()


def test_legacy_default_target_survives_a_new_reply(tmp_path):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1), _message(2, parent=1)], project_id=1)
        db.semantic_seed(1, [1], {'source': 'synthetic'})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{}' WHERE kind='semantic'")
        assert not db.semantic_seed(1, [2], {'source': 'synthetic'})
        assert json.loads(_job(db, 1)['payload'])['targets'] == [1, 2]
    finally:
        db.close()


def test_held_job_keeps_its_long_defer_budget(tmp_path):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1)], project_id=1)
        db.semantic_seed(1, [1], {'source': 'synthetic'})
        key = str(_job(db, 1)['job_id'])
        saved = {key: {'g': 'synthetic-generation', 'n': 2}}
        drain._sched_write(db, {'long_defers': saved})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind='semantic'",
                          ('{"targets":[1],"extra":1e400}',))
        sched = drain._sched_state(db)
        drain._merge_long_defers(db, sched, {})
        assert sched['long_defers'] == saved
    finally:
        db.close()


def test_held_defers_do_not_consume_live_defer_capacity(tmp_path):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(mid) for mid in range(1, 502)], project_id=1)
        db.semantic_seed(1, list(range(1, 502)), {'source': 'synthetic', 'event_id': 1})
        jobs = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic' ORDER BY message_id").fetchall()
        held = {str(row['job_id']): {'g': 'synthetic-held', 'n': 7} for row in jobs[:500]}
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{' WHERE kind='semantic' AND message_id<=500")
        assert drain._select_due(db, 'realtime', ('semantic',), False, 1, {})[0][0]['job_id'] == jobs[500]['job_id']
        drain._sched_write(db, {'long_defers': held})
        valid = jobs[500]
        key = str(valid['job_id'])
        entry = {'g': runtime.payload_generation(runtime.parse_payload(valid)), 'n': 1}
        sched = drain._sched_state(db)
        drain._merge_long_defers(db, sched, {key: entry})
        assert all(sched['long_defers'][k] == value for k, value in held.items())
        assert sched['long_defers'][key] == entry
        drain._sched_write(db, sched)
        reloaded = drain._long_defers(db)
        assert reloaded[key] == entry
        assert not drain._select_due(db, 'realtime', ('semantic',), False, 1, reloaded)[0]
    finally:
        db.close()


def test_new_arrival_and_retry_leave_bad_payload_and_history_unchanged(tmp_path):
    from adapters.common import human_commands
    import mcs_operations
    import mcs_requests
    db = _ledger(tmp_path)
    try:
        patient = _patient(db)
        patient.messages = [_message(1)]
        db.save_patient(patient, notify={'source': 'unread'}, semantic=True)
        event_id = db.db.execute("SELECT event_id FROM notify_outbox WHERE kind='new_messages'").fetchone()[0]
        notice_id = db.outbox_add('semantic_notice', 1, {'src_event_id': event_id, 'target_message_id': 1})
        with db.db:
            db.db.execute("UPDATE notify_outbox SET state='accepted',attempts=1 WHERE event_id=?", (notice_id,))
        projection = db.artifact_add('canonical_projection', '{}', project_id=1, message_id=1,
                                     meta={VERSION_KEY: CURRENT_VERSION})
        rollup = db.artifact_add('patient_rollup', '{}', project_id=1,
                                 meta={VERSION_KEY: CURRENT_VERSION})
        bad = '{"targets":null}'
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=2,state='failed' "
                          "WHERE kind='semantic'", (bad,))
        before = dict(_job(db, 1))
        prior_outbox = [tuple(row) for row in db.db.execute('SELECT * FROM notify_outbox ORDER BY event_id')]
        patient.messages = [_message(2, parent=1)]
        db.save_patient(patient, notify={'source': 'unread'}, semantic=True)
        assert dict(_job(db, 1)) == before
        assert db.db.execute('SELECT 1 FROM messages WHERE message_id=2').fetchone()
        meta = db.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (projection,)).fetchone()[0]
        assert json.loads(meta)['invalidated'] is True
        assert db.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (rollup,)).fetchone() is None
        assert [tuple(row) for row in db.db.execute('SELECT * FROM notify_outbox ORDER BY event_id')][:len(prior_outbox)] == prior_outbox
        assert len(db.db.execute('SELECT * FROM notify_outbox').fetchall()) > len(prior_outbox)
        assert drain.revive_failed(db, now=time.time() + 100000)['revived'] == 0
        _, preview_error, _ = human_commands._semantic_job(SimpleNamespace(db=db.db), mcs_requests, 1,
                                                           before['job_id'])
        assert preview_error == 'invalid_payload'
        with db.db:
            error, _ = mcs_operations._apply_retry_tx(db, {'job_id': before['job_id'], 'project_id': 1}, time.time())
        assert error == 'invalid_payload'
        assert dict(_job(db, 1)) == before
    finally:
        db.close()


def test_reprocess_does_not_overwrite_a_concurrent_job_change(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1)], project_id=1)
        db.semantic_seed(1, [1], {'source': 'synthetic'})
        original = drain._reprocess_target_state
        changed = '{"targets":null}'
        path = db.db.execute('PRAGMA database_list').fetchone()[2]

        def race(*args):
            result = original(*args)
            with sqlite3.connect(path) as other:
                other.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE kind='semantic'",
                              (changed,))
            return result

        monkeypatch.setattr(drain, '_reprocess_target_state', race)
        result = drain.reprocess_step(db, _cfg(loop_mode='off'))
        assert result['status'] == 'source_changed' and result['processed'] == 0
        assert _job(db, 1)['payload'] == changed and _job(db, 1)['attempts'] == 4
        assert 'inference_reprocess' not in drain._sched_state(db)
    finally:
        db.close()


def test_worker_rejects_duplicate_key_after_job_selection(tmp_path):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1)], project_id=1)
        db.semantic_seed(1, [1], {'source': 'synthetic'})
        token = runtime.JobToken.from_row(_job(db, 1))
        payload = token.payload_raw[:-1] + ', "targets": [1]}'
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?", (payload, token.job_id))
        assert not runtime.job_matches(db, token)
    finally:
        db.close()


def test_seed_holds_writer_reservation_before_reading_existing_job(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1)], project_id=1)
        db.semantic_seed(1, [1], {'source': 'synthetic'})
        source_generation = db._semantic_source_generation
        path = db.db.execute('PRAGMA database_list').fetchone()[2]
        locked = []

        def concurrent_write(*args):
            assert db.db.in_transaction
            with sqlite3.connect(path, timeout=0) as other:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    other.execute("UPDATE fetch_jobs SET payload='{' WHERE kind='semantic'")
            locked.append(True)
            return source_generation(*args)

        monkeypatch.setattr(db, '_semantic_source_generation', concurrent_write)
        db.semantic_seed(1, [1], {'source': 'synthetic'})
        assert locked
    finally:
        db.close()


def test_large_invalid_prefix_cannot_hide_eligible_job(tmp_path):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(mid) for mid in range(1, 35)], project_id=1)
        db.semantic_seed(1, list(range(1, 35)), {'source': 'synthetic'})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{' WHERE kind='semantic' AND message_id<34")
        selected = drain._select_due(db, None, ('semantic',), False, 1)[0]
        assert [job['message_id'] for job in selected] == [34]
    finally:
        db.close()
