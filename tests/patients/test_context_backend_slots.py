"""Backend switches retain synthetic source checkpoints in separate slots."""
import json

import patient_context_refresh as refresh
from semantic_testkit import _ledger, _message, _patient
from test_patient_context_refresh import CFG, runtime as runtime, seed


def test_backend_slot_keeps_old_and_legacy_checkpoints_without_double_counting(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        _patient(ledger)
        ledger.save_messages([_message()])
        base = {'source_sig': 'synthetic-source', 'source_binding': 'synthetic-occurrence',
                'contract': {'model': 'synthetic-local'}}
        legacy = ledger.artifact_add(refresh.SCAN, json.dumps({'complete': False, 'failure': 'synthetic'}),
                                     project_id=1, message_id=1, meta={**base, 'source_key': 1,
                                     refresh.VERSION_KEY: refresh.CURRENT_VERSION})
        cloud = {**base, 'contract': {**base['contract'], 'provider': {'backend': 'synthetic-cloud'}},
                 'provider': {'backend': 'synthetic-cloud'}}
        local = {**base, 'route_contract': cloud['contract']}
        with ledger.db:
            cloud_id = refresh._replace(ledger, refresh.SCAN, 1, {'complete': False, 'failure': 'synthetic'}, cloud, 1)
            refresh._replace(ledger, refresh.SCAN, 1, {'complete': False, 'failure': 'synthetic'}, local, 1)
            local_id = refresh._replace(ledger, refresh.SCAN, 1, {'complete': False}, local, 1)
        rows = ledger.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                                 (refresh.SCAN,)).fetchall()
        assert [row['artifact_id'] for row in rows] == [legacy, cloud_id, local_id]
        assert json.loads(rows[1]['meta'])['backend_slot'] != json.loads(rows[2]['meta'])['backend_slot']
        assert refresh.lane_metrics(ledger.db)['lanes']['B']['other_failure'] == 0
    finally:
        ledger.close()


def test_newer_incompatible_slot_cannot_hide_current_source(runtime):
    ledger, _, _ = runtime
    seed(ledger, body='完全合成の居住記録。')
    snapshot = refresh._snapshot(ledger.db, 1, CFG)
    source = snapshot['sources'][0]
    contract = snapshot['contract']
    meta = {'source_sig': refresh.context_plan.source_signature(source), 'contract': contract,
            'model': contract['model'], 'source_binding': 'synthetic-occurrence'}
    with ledger.db:
        expected = refresh._replace(ledger, refresh.SCAN, 1, {'cursor': 1, 'complete': False}, meta, 1)
        refresh._replace(ledger, refresh.SCAN, 1, {'cursor': 7, 'complete': False},
                         {**meta, 'contract': {**contract, 'model': 'other-model'}}, 1)
    chosen = refresh._current_scans(ledger.db, 1, [source], contract, check_quality=False)[1]
    assert chosen['cursor'] == 1
    assert chosen['_source_meta']['backend_slot'] == json.loads(ledger.db.execute(
        'SELECT meta FROM artifacts WHERE artifact_id=?', (expected,)).fetchone()[0])['backend_slot']
