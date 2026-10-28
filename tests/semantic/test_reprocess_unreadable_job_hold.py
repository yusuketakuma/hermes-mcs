"""Synthetic regressions for bounded replay of unreadable semantic jobs."""
import json

import pytest

import semantic
import semantic_drain
import semantic_policy
import semantic_projection
import semantic_store
from semantic_testkit import _cfg, _ledger, _message, _patient


@pytest.mark.parametrize('raw', [
    '{', '[]', 'null', '{"targets":[]}', '{"targets":null}', '{"targets":[true]}',
    '{"targets":[2]}',
    '{"inference_engine_version":"2.0","targets":[1],"synthetic_future":true}',
    '{"inference_engine_version":"1.0","inference_engine_version":"2.0","targets":[1]}',
    '{"inference_engine_\\u0076ersion":"2.0","inference_engine_version":"1.0","targets":[1]}',
    '{"inference_engine_version":"1.0","targets":[1],"unused":NaN}',
    '{"inference_engine_version":"1.0","targets":[1],"unused":1e400}',
    '{"inference_engine_version":"1.0","targets":[1],"unused":' + '[' * 129 + '0' + ']' * 129 + '}',
    '{"inference_engine_version":"1.0","targets":[1],"unused":"' + 'a' * (1024 * 1024) + '"}',
])
def test_reprocess_holds_original_and_advances_to_other_thread(tmp_path, raw):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1), _message(2)], project_id=1)
        db.semantic_seed(1, [1], {'source': 'history_import', 'notification_free': True})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE kind='semantic' AND message_id=1", (raw,))
        before = tuple(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic' AND message_id=1").fetchone())
        raw_bytes = db.db.execute("SELECT CAST(payload AS BLOB) FROM fetch_jobs "
                                  "WHERE kind='semantic' AND message_id=1").fetchone()[0]
        result = semantic_drain.reprocess_step(db, _cfg(loop_mode='off'), limit=2)
        assert result['phase'] == 'verify' and result['complete'] is False
        assert tuple(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic' AND message_id=1").fetchone()) == before
        assert db.db.execute("SELECT CAST(payload AS BLOB) FROM fetch_jobs "
                             "WHERE kind='semantic' AND message_id=1").fetchone()[0] == raw_bytes
        assert db.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='semantic' AND message_id=2").fetchone()
        result = semantic_drain.reprocess_step(db, _cfg(loop_mode='off'), limit=2)
        assert result['coverage']['counts']['held'] == 1
        held = semantic.status_report(db, _cfg())['reprocess_held']
        assert held['status'] == 'previous_scan' and held['count'] == 1
        assert type(held['observed_at']) in (int, float)
        assert tuple(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic' AND message_id=1").fetchone()) == before
        assert db.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind='semantic_notice'").fetchone()[0] == 0
    finally:
        db.close()


def test_status_without_completed_scan_does_not_report_zero(tmp_path):
    db = _ledger(tmp_path)
    try:
        assert semantic.status_report(db, _cfg())['reprocess_held'] == {
            'status': 'unverified', 'count': None, 'observed_at': None}
    finally:
        db.close()


@pytest.mark.parametrize('raw', ['{', '{"inference_engine_version":"2.0","targets":[1]}'])
def test_current_artifacts_do_not_hide_an_unreadable_job(tmp_path, raw):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.save_messages([_message(1)], project_id=1)
        cfg = _cfg(loop_mode='off')
        scfg, errors = semantic_policy.runtime_config(cfg)
        assert not errors
        fp = semantic_store.thread_bundle(db.db, 1, 1)['source_fingerprint']
        source = db.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
        binding = {'inference_engine_version': '1.0', 'fingerprint': fp,
                   'policy_fingerprint': semantic_policy.policy_fingerprint(scfg),
                   'hash': source, 'engine_version': 4,
                   'projection_version': semantic_projection.PROJECTION_VERSION}
        db.artifact_add('semantic_summary', json.dumps({'claims': []}), project_id=1, message_id=1, meta=binding)
        db.artifact_add('semantic_audit', json.dumps({'status': 'PASS'}), project_id=1, message_id=1, meta=binding)
        db.artifact_add('semantic_facts', json.dumps({'facts': []}), project_id=1, message_id=1, meta=binding)
        db.semantic_seed(1, [1], {'source': 'history_import', 'notification_free': True})
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=?,attempts=4 WHERE kind='semantic'", (raw,))
        before = tuple(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone())
        row = db.db.execute('SELECT m.message_id,m.project_id,m.parent_id,m.body_state,m.body_text, '
                            'p.project_id AS patient_id,p.fetch_reason FROM messages m '
                            'LEFT JOIN patients p ON p.project_id=m.project_id WHERE m.message_id=1').fetchone()
        assert semantic_drain._reprocess_target_state(db, row, scfg) == 'current'
        semantic_drain.reprocess_step(db, cfg)
        report = semantic_drain.reprocess_step(db, cfg)
        assert report['coverage']['counts'] == {'held': 1}
        assert report['complete'] is False
        report = semantic_drain.reprocess_step(db, cfg)
        assert report['waiting_reasons']['messages']['held'] == 1
        assert report['complete'] is False
        assert semantic.status_report(db, cfg)['reprocess_held']['count'] == 1
        assert tuple(db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()) == before
    finally:
        db.close()
