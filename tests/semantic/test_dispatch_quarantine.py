"""Synthetic bounded quarantine of malformed semantic jobs."""
import json
import time

import pytest

import semantic_drain as drain
from inference_version import VERSION_KEY
from semantic_testkit import _FakeJev, _cfg, _ledger, _llm, _message, _patient


@pytest.mark.parametrize('raw', ['{', '[]', 'null', '{"targets":[]}', '{"targets":null}'])
def test_quarantine_preserves_payload_attempts_and_advances_epoch(tmp_path, raw):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1)], project_id=1)
        db.semantic_seed(1, [1], {'source': 'history_import'})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4,next_try=0,reason_code='network_error' "
                          "WHERE kind='semantic'", (raw,))
        drain._sched_write(db, {'inference_reprocess': {VERSION_KEY: '1.0', 'phase': 'waiting'},
                                'job_state_epoch': 7})
        before = dict(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone())
        drain._quarantine_invalid_jobs(db, 4, time.monotonic() + 10)
        after = dict(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone())
        assert after == {**before, 'state': 'failed'}
        assert drain._sched_state(db)['job_state_epoch'] == 8
    finally:
        db.close()


def test_future_and_deferred_jobs_are_left_unchanged(tmp_path):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1), _message(2)], project_id=1)
        db.semantic_seed(1, [1, 2], {'source': 'history_import'})
        rows = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic' ORDER BY message_id").fetchall()
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4,next_try=0 WHERE job_id=?",
                          (json.dumps({VERSION_KEY: '2.0', 'targets': [1], 'future': 'keep'}), rows[0]['job_id']))
            db.db.execute("UPDATE fetch_jobs SET payload='{',attempts=3,next_try=0 WHERE job_id=?", (rows[1]['job_id'],))
        saved = {str(rows[1]['job_id']): {'g': 'synthetic-old-generation', 'n': 4}}
        drain._sched_write(db, {'long_defers': saved})
        before = [tuple(row) for row in db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic' ORDER BY job_id")]
        drain._quarantine_invalid_jobs(db, 4, time.monotonic() + 10)
        assert [tuple(row) for row in db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic' ORDER BY job_id")] == before
        assert drain._sched_state(db)['long_defers'] == saved
    finally:
        db.close()


@pytest.mark.parametrize('raw', [
    '{"inference_engine_version":"1.0","inference_engine_version":"2.0","targets":[1]}',
    '{"inference_engine_version":"2.0","inference_engine_version":"1.0","targets":[1]}',
    '{"inference_engine_version":"1.0","inference_engine_\\u0076ersion":"2.0","targets":[1]}',
    '{"inference_engine_\\u0076ersion":"2.0","inference_engine_version":"1.0","targets":[1]}',
])
def test_ambiguous_version_marker_does_not_mutate_job(tmp_path, raw):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1)], project_id=1)
        db.semantic_seed(1, [1], {'source': 'history_import'})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4,next_try=0 WHERE kind='semantic'", (raw,))
        before = tuple(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone())
        drain._quarantine_invalid_jobs(db, 4, time.monotonic() + 10)
        assert tuple(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()) == before
    finally:
        db.close()


@pytest.mark.parametrize('version_fields', [
    '"inference_engine_version":"2.0"',
    '"inference_engine_version":"1.0","inference_engine_\\u0076ersion":"2.0"',
])
@pytest.mark.parametrize('hazard', ['deep', 'long_integer'])
def test_unreadable_future_or_ambiguous_payload_stays_untouched(tmp_path, version_fields, hazard):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1)], project_id=1)
        db.semantic_seed(1, [1], {'source': 'history_import'})
        value = '[' * 100000 + '0' + ']' * 100000 if hazard == 'deep' else '1' * 5000
        raw = '{' + version_fields + ',"value":' + value + '}'
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4,next_try=0 WHERE kind='semantic'", (raw,))
        before = dict(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone())
        drain._quarantine_invalid_jobs(db, 4, time.monotonic() + 10)
        after = dict(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone())
        assert after['payload'] == before['payload']
        assert after['attempts'] == before['attempts']
        assert after['state'] == before['state']
    finally:
        db.close()


def test_bounded_scan_advances_past_invalid_prefix_to_an_unrelated_root(tmp_path):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(mid) for mid in range(1, 42)], project_id=1)
        db.semantic_seed(1, list(range(1, 42)), {'source': 'history_import'})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,next_try=0 WHERE kind='semantic' AND message_id<=40",
                          ('{"inference_engine_version":"1.0","targets":[]}',))
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic' AND message_id=41")
        for _ in range(10):
            drain._quarantine_invalid_jobs(db, 4, time.monotonic() + 10)
        assert db.db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='semantic' AND state='failed'").fetchone()[0] == 40
        selected = drain._select_due(db, None, ('semantic',), False, 1)[0]
        assert selected and selected[0]['message_id'] == 41
    finally:
        db.close()


def test_automatic_revive_does_not_rewrite_quarantined_payload(tmp_path):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1)], project_id=1)
        db.semantic_seed(1, [1], {'source': 'history_import'})
        raw = '{"inference_engine_version":"1.0","targets":[]}'
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4,state='failed',updated_at=1 WHERE kind='semantic'", (raw,))
        before = tuple(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone())
        assert drain.revive_failed(db, now=time.time() + 100000)['revived'] == 0
        assert tuple(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()) == before
    finally:
        db.close()


def test_quarantine_and_cursor_roll_back_together_on_scheduler_failure(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1)], project_id=1)
        db.semantic_seed(1, [1], {'source': 'history_import'})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{',attempts=4,next_try=0 WHERE kind='semantic'")
        before = tuple(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone())
        monkeypatch.setattr(drain, '_sched_write', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('synthetic')))
        with pytest.raises(RuntimeError, match='synthetic'):
            drain._quarantine_invalid_jobs(db, 1, time.monotonic() + 10)
        assert tuple(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()) == before
        assert not db.db.in_transaction
    finally:
        db.close()


def test_unrelated_drain_does_not_erase_invalid_job_long_defer_evidence(tmp_path):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1), _message(2)], project_id=1)
        db.semantic_seed(1, [1, 2], {'source': 'history_import'})
        job = db.db.execute("SELECT job_id,payload FROM fetch_jobs WHERE kind='semantic' AND message_id=1").fetchone()
        saved = {str(job['job_id']): {'g': json.loads(job['payload'])['generation'], 'n': 4}}
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{',next_try=0 WHERE kind='semantic' AND message_id=1")
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic' AND message_id=2")
        drain._sched_write(db, {'long_defers': saved})
        result = drain.run_due(db, _cfg(loop_mode='off'), {'errors': []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=_llm, max_jobs=1)
        assert result['done'] == 1
        assert drain._sched_state(db)['long_defers'] == saved
    finally:
        db.close()


def test_many_invalid_deferred_jobs_do_not_block_an_unrelated_root(tmp_path):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(mid) for mid in range(1, 22)], project_id=1)
        db.semantic_seed(1, list(range(1, 22)), {'source': 'history_import'})
        rows = db.db.execute("SELECT job_id,payload FROM fetch_jobs WHERE kind='semantic' AND message_id<=20 ORDER BY job_id").fetchall()
        saved = {str(row['job_id']): {'g': json.loads(row['payload'])['generation'], 'n': 4} for row in rows}
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,next_try=0 WHERE kind='semantic' AND message_id<=20",
                          ('{"inference_engine_version":"1.0","targets":[]}',))
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic' AND message_id=21")
        drain._sched_write(db, {'long_defers': saved})
        result = drain.run_due(db, _cfg(loop_mode='off'), {'errors': []}, time.monotonic() + 300,
                               jev_client=_FakeJev(), llm_fn=_llm, max_jobs=1)
        assert result['done'] == 1
        assert drain._sched_state(db)['long_defers'] == saved
    finally:
        db.close()
