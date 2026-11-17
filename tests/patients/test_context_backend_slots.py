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
                'contract': {'synthetic': 'scan-contract'}}
        legacy = ledger.artifact_add(refresh.SCAN, json.dumps({'complete': False, 'failure': 'synthetic'}),
                                     project_id=1, message_id=1, meta={**base, 'source_key': 1,
                                     refresh.VERSION_KEY: refresh.CURRENT_VERSION})
        provider = {'backend': 'synthetic-cloud', 'model': 'synthetic-cloud-model'}
        cloud = {**base, 'provider': provider, 'model': provider['model'],
                 'provenance': {'backend': 'synthetic-cloud', 'model': provider['model'], 'profile': None}}
        local = {**base, 'route_provider': provider, 'local_fallback': True, 'model': 'synthetic-local',
                 'provenance': {'backend': 'local', 'model': 'synthetic-local', 'profile': None}}
        other_model = {**local, 'model': 'synthetic-other-local',
                       'provenance': {**local['provenance'], 'model': 'synthetic-other-local'}}
        with ledger.db:
            cloud_id = refresh._replace(ledger, refresh.SCAN, 1, {'complete': False, 'failure': 'synthetic'}, cloud, 1)
            refresh._replace(ledger, refresh.SCAN, 1, {'complete': False, 'failure': 'synthetic'}, local, 1)
            local_id = refresh._replace(ledger, refresh.SCAN, 1, {'complete': False}, local, 1)
            # A local model change is a separate producer, not a replacement of the old row.
            other_id = refresh._replace(ledger, refresh.SCAN, 1, {'complete': False}, other_model, 1)
        rows = ledger.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                                 (refresh.SCAN,)).fetchall()
        assert [row['artifact_id'] for row in rows] == [legacy, cloud_id, local_id, other_id]
        slots = [json.loads(row['meta'])['backend_slot'] for row in rows[1:]]
        assert len(set(slots)) == 3
        assert refresh.lane_metrics(ledger.db)['lanes']['B']['other_failure'] == 0
    finally:
        ledger.close()


def test_newer_incompatible_slot_cannot_hide_current_source(runtime):
    ledger, _, _ = runtime
    seed(ledger, body='完全合成の居住記録。')
    snapshot = refresh._snapshot(ledger.db, 1, CFG)
    source = snapshot['sources'][0]
    route = snapshot['route']
    local = refresh._local_provenance(CFG)
    meta = {'source_sig': refresh.context_plan.source_signature(source), 'contract': snapshot['contract'],
            'model': local['model'], 'provenance': local, 'source_binding': 'synthetic-occurrence'}
    with ledger.db:
        expected = refresh._replace(ledger, refresh.SCAN, 1, {'cursor': 1, 'complete': False}, meta, 1)
        refresh._replace(ledger, refresh.SCAN, 1, {'cursor': 7, 'complete': False},
                         {**meta, 'model': 'other-model', 'provenance': {**local, 'model': 'other-model'}}, 1)
    chosen = refresh._current_scans(ledger.db, 1, [source], route, check_quality=False)[1]
    assert chosen['cursor'] == 1
    assert chosen['_source_meta']['backend_slot'] == json.loads(ledger.db.execute(
        'SELECT meta FROM artifacts WHERE artifact_id=?', (expected,)).fetchone()[0])['backend_slot']
