"""OCR clinical results and unfinished paid owners retain exact financial evidence."""
import json
import time

import pytest

import maintenance
from test_maintenance_retention_cleanup import _ledger, _repair_accounting, _expire_repair_accounting

from test_attachment_inference import page_store, run
from test_cloud_after_local import local_then_cloud
from test_anthropic_runtime import routed
from test_cloud_response_recovery import legacy_cloud_routes

__all__ = ['page_store', 'local_then_cloud', 'routed']


@pytest.mark.parametrize('adopted', [False, True])
def test_ocr_owner_or_adopted_reference_pins_reserve_result_and_disposition(tmp_path, adopted):
    ledger = _ledger(tmp_path)
    try:
        now = time.time()
        protected = _repair_accounting(ledger, 1, 'a', header=False, slot=False)
        unrelated = _repair_accounting(ledger, 2, 'b', header=False, slot=False)
        if adopted:
            ledger.artifact_add('attachment_extract_v1', json.dumps({'stage': 'cloud',
                'reservation_artifact_ids': [protected['reserve']]}), project_id=1)
        else:
            row = ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (protected['reserve'],)).fetchone()
            meta = json.loads(row[0])
            meta['owner'] = {'source_type': 'attachment_ocr', 'source_key': 'attachment_ocr:1:1'}
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), protected['reserve']))
        _expire_repair_accounting(ledger, now)
        maintenance._prune_cloud_usage(ledger.db, now - maintenance.CLOUD_USAGE_KEEP_S)
        remaining = {row[0] for row in ledger.db.execute('SELECT artifact_id FROM artifacts')}
        assert {protected[k] for k in ('reserve', 'result', 'disposition')} <= remaining
        assert not set(unrelated.values()).intersection(remaining)
        assert not {protected[k] for k in ('dryrun', 'compare')}.intersection(remaining)
    finally:
        ledger.close()


def test_conflicting_scope_uuid_evidence_is_retained(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        now = time.time()
        protected = _repair_accounting(ledger, 1, 'a', header=False, slot=False)
        foreign = _repair_accounting(ledger, 2, 'a', header=False, slot=False)
        ledger.artifact_add('attachment_extract_v1', json.dumps({'stage': 'cloud',
            'reservation_artifact_ids': [protected['reserve']]}), project_id=1)
        _expire_repair_accounting(ledger, now)
        maintenance._prune_cloud_usage(ledger.db, now - maintenance.CLOUD_USAGE_KEEP_S)
        remaining = {row[0] for row in ledger.db.execute('SELECT artifact_id FROM artifacts')}
        assert {rows[k] for rows in (protected, foreign) for k in ('reserve', 'result', 'disposition')} <= remaining
    finally:
        ledger.close()


def test_real_ocr_cloud_result_remains_current_after_financial_retention(page_store):
    import attachment_inference as worker
    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    with legacy_cloud_routes():
        assert run(ledger, cfg, 'cloud')['done'] == 1
    before = worker.current(ledger.db, 1, key, cfg=cfg)
    assert before['ready'] and before['stage'] == 'cloud'
    cutoff = time.time() - maintenance.CLOUD_USAGE_KEEP_S
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET created_at=? WHERE kind IN "
            "('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition')", (cutoff - 1,))
    maintenance._prune_cloud_usage(ledger.db, cutoff)
    after = worker.current(ledger.db, 1, key, cfg=cfg)
    assert after['ready'] and after['artifact_id'] == before['artifact_id']
    assert len(cloud_calls) == 1
