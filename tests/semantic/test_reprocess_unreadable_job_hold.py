"""Synthetic regressions for bounded replay of unreadable semantic jobs."""
import pytest

import semantic
import semantic_drain
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
