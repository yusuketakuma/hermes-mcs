"""Synthetic quarantine tests; unknown formats remain untouched."""
import json
import hashlib
import time

import pytest
import semantic_drain as drain
import semantic_quarantine as quarantine

from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _FakeJev, _cfg, _llm, _seeded


def _job(db):
    return db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic' AND message_id=1").fetchone()


def _scan(db):
    return quarantine.scan(db, time.monotonic() + 5, drain._reservation_sched, drain._job_lock)


@pytest.mark.parametrize('targets', [None, []])
@pytest.mark.parametrize('version', [None, CURRENT_VERSION])
def test_quarantine_preserves_payload_attempts_and_previous_metadata(tmp_path, targets, version):
    db = _seeded(tmp_path)
    try:
        payload = {'targets': targets}
        if version is not None:
            payload[VERSION_KEY] = version
        raw = json.dumps(payload)
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4,next_try=123,"
                          "reason_code='synthetic_previous',updated_at=456 WHERE kind='semantic'", (raw,))
        old = dict(_job(db))
        drain._sched_write(db, {'job_state_epoch': 7,
                                'inference_reprocess': {VERSION_KEY: CURRENT_VERSION, 'phase': 'waiting'}})
        assert _scan(db) == 1
        after = dict(_job(db))
        assert after['state'] == 'failed' and after['reason_code'] == 'invalid_payload'
        assert after['payload'] == old['payload'] and after['attempts'] == old['attempts']
        assert after['next_try'] == old['next_try']
        evidence = db.db.execute('SELECT content FROM artifacts WHERE kind=? AND project_id=1 AND model=?',
                                 (quarantine.EVIDENCE_KIND, str(old['job_id']))).fetchone()
        assert json.loads(evidence['content']) == {
            'job_id': old['job_id'], 'root_id': 1,
            'payload_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
            'attempts': 4, 'old_next_try': 123.0,
            'old_reason': 'synthetic_previous', 'old_updated_at': 456.0,
        }
        assert drain._sched_state(db)['job_state_epoch'] == 8
    finally:
        db.close()


@pytest.mark.parametrize('raw', [
    '{', '[]', 'null',
    '{"inference_engine_version":"2.0","targets":[]}',
    '{"inference_engine_version":"1.0","inference_engine_\\u0076ersion":"2.0","targets":[]}',
    '{"inference_engine_version":"2.0","inference_engine_\\u0076ersion":"1.0","targets":[]}',
    '{"targets":[1],"targets":[]}',
    '{"targets":[],"targets":[1]}',
])
def test_unknown_version_or_format_is_never_changed(tmp_path, raw):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4,reason_code='old' WHERE kind='semantic'",
                          (raw,))
        before = dict(_job(db))
        assert _scan(db) == 0
        assert dict(_job(db)) == before
        assert not db.db.execute('SELECT 1 FROM artifacts WHERE kind=?', (quarantine.EVIDENCE_KIND,)).fetchone()
    finally:
        db.close()


def test_long_defer_evidence_prevents_quarantine(tmp_path):
    db = _seeded(tmp_path)
    try:
        job_id = _job(db)['job_id']
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE job_id=?",
                          ('{"targets":[]}', job_id))
        sched = {'long_defers': {str(job_id): {'g': 'synthetic', 'n': 5}}}
        drain._sched_write(db, sched)
        before = dict(_job(db))
        assert _scan(db) == 0
        assert dict(_job(db)) == before
        assert drain._sched_state(db)['long_defers'] == sched['long_defers']
    finally:
        db.close()


@pytest.mark.parametrize('raw', [
    '{"targets":[],"oversized":"' + 'x' * (1024 * 1024) + '"}',
    '{"targets":[],"nested":' + '[' * 1500 + '0' + ']' * 1500 + '}',
    '{"targets":[],"number":NaN}',
])
def test_unbounded_or_unparseable_evidence_remains_untouched(tmp_path, raw):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE kind='semantic'", (raw,))
        before = dict(_job(db))
        assert _scan(db) == 0
        assert dict(_job(db)) == before
    finally:
        db.close()


def test_unreadable_scheduler_blocks_quarantine_without_losing_job(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{\"targets\":[]}',attempts=4 WHERE kind='semantic'")
            db.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state) "
                          "VALUES('semantic_sched',0,0,'{','done')")
        before = dict(_job(db))
        with pytest.raises(ValueError, match='semantic_scheduler_invalid'):
            _scan(db)
        assert dict(_job(db)) == before
        assert db.db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic_sched'").fetchone()[0] == '{'
        assert not db.db.execute('SELECT 1 FROM artifacts WHERE kind=?', (quarantine.EVIDENCE_KIND,)).fetchone()
    finally:
        db.close()


def test_evidence_capacity_holds_the_job_and_reports_observed_limit(tmp_path):
    db = _seeded(tmp_path)
    try:
        job_id = _job(db)['job_id']
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{\"targets\":[]}',attempts=4 WHERE job_id=?",
                          (job_id,))
            db.db.executemany('INSERT INTO artifacts(kind,project_id,content,model) VALUES(?,1,?,?)',
                              [(quarantine.EVIDENCE_KIND, '{}', str(job_id))
                               for _ in range(quarantine.MAX_EVIDENCE_PER_JOB)])
        before = dict(_job(db))
        diagnostic = {}
        assert quarantine.scan(db, time.monotonic() + 5, drain._reservation_sched,
                               drain._job_lock, diagnostics=diagnostic) == 0
        assert diagnostic == {'evidence_full_observed': 1}
        assert dict(_job(db)) == before
        assert db.db.execute('SELECT COUNT(*) FROM artifacts WHERE kind=?',
                             (quarantine.EVIDENCE_KIND,)).fetchone()[0] == quarantine.MAX_EVIDENCE_PER_JOB
    finally:
        db.close()


@pytest.mark.parametrize('targets', [None, []])
def test_actual_dispatch_quarantines_supported_bad_targets(tmp_path, targets):
    db = _seeded(tmp_path)
    try:
        raw = json.dumps({'targets': targets})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE kind='semantic'", (raw,))
        client = _FakeJev()
        result = {'errors': []}
        drain.run_due(db, _cfg(), result, time.monotonic() + 300,
                      jev_client=client, llm_fn=_llm)
        assert not client.calls
        assert _job(db)['state'] == 'failed'
        assert _job(db)['payload'] == raw and _job(db)['attempts'] == 4
    finally:
        db.close()


def test_page_cursor_reaches_later_candidates_without_rewriting_unknown_jobs(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{',attempts=4 WHERE kind='semantic'")
            db.db.executemany(
                "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,attempts,next_try) "
                "VALUES('semantic',1,?,?,'pending',4,0)",
                [(mid, '{') for mid in range(3, quarantine.PAGE_SIZE + 2)])
            db.db.execute(
                "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,attempts,next_try) "
                "VALUES('semantic',1,999,'{\"targets\":[]}','pending',4,0)")
        before = dict(_job(db))
        assert _scan(db) == 0
        assert db.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic' AND message_id=999").fetchone()[0] == 'pending'
        assert _scan(db) == 1
        assert db.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic' AND message_id=999").fetchone()[0] == 'failed'
        assert dict(_job(db)) == before
    finally:
        db.close()


def test_page_cycle_revisits_older_repairs_while_new_jobs_keep_arriving(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{' WHERE kind='semantic'")
            db.db.executemany(
                "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,attempts,next_try) "
                "VALUES('semantic',1,?,'{','pending',0,0)",
                [(mid,) for mid in range(3, quarantine.PAGE_SIZE + 3)])
        assert _scan(db) == 0
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{\"targets\":[]}' "
                          "WHERE kind='semantic' AND message_id=1")
            db.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,next_try) "
                          "VALUES('semantic',1,999,'{','pending',0)")
        assert _scan(db) == 0
        with db.db:
            db.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,next_try) "
                          "VALUES('semantic',1,1000,'{','pending',0)")
        assert _scan(db) == 0
        assert _scan(db) == 1
        assert _job(db)['state'] == 'failed'
    finally:
        db.close()


def test_expired_scan_leaves_jobs_and_cursor_untouched(tmp_path):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload='{\"targets\":[]}' WHERE kind='semantic'")
        before = dict(_job(db))
        assert quarantine.scan(db, time.monotonic() - 1,
                               drain._reservation_sched, drain._job_lock) == 0
        assert dict(_job(db)) == before
        assert not db.db.execute('SELECT 1 FROM fetch_jobs WHERE kind=?', (quarantine.CURSOR_KIND,)).fetchone()
    finally:
        db.close()


@pytest.mark.parametrize('name', ['fetch_jobs', 'artifacts'])
def test_temp_shadow_keeps_the_main_job_unchanged(tmp_path, name):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE main.fetch_jobs SET payload='{\"targets\":[]}' "
                          "WHERE kind='semantic'")
        before = tuple(db.db.execute(
            "SELECT payload,state,attempts FROM main.fetch_jobs WHERE kind='semantic'").fetchone())
        db.db.execute(f'CREATE TEMP TABLE {name}(dummy INTEGER)')
        assert _scan(db) == 0
        assert tuple(db.db.execute(
            "SELECT payload,state,attempts FROM main.fetch_jobs WHERE kind='semantic'").fetchone()) == before
    finally:
        db.close()
