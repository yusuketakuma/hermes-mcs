"""Synthetic regression cases for patient-context scheduling and source completeness."""
import json

import pytest

import extract_llm as worker
import initial_import_state
import patient_context_refresh as refresh
from test_patient_context_refresh import CFG, document, finish, runtime, seed, step

__all__ = ['runtime']


@pytest.fixture
def cloud_schedule(runtime, monkeypatch):
    led, _, _ = runtime
    cfg = {**CFG, 'cloud_llm': {'mode': 'on', 'project_ids': [1, 2]}}
    monkeypatch.setattr(worker, '_cloud_mode', lambda *_: 'on')
    class Cloud:
        def __init__(self, ledger, config, **kwargs):
            assert ledger is led
            self.contract = worker._cloud_contract()
            self.model = self.contract['model']
            self.possibly_sent = False
        def __call__(self, prompt, **kwargs):
            assert not led.db.in_transaction
            payload = json.loads(prompt)
            if 'reports' in payload:
                result = {'fact_ids': [item['id'] for item in payload['reports'][:12]]}
            else:
                source = payload['source']
                result = {'patient_context': [{'category': 'living', 'text': source,
                    'evidence': source, 'subject': 'unspecified', 'details': []}]}
            return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps(result)}
        def commit_guard(self):
            return None
        def discard(self):
            return None
    monkeypatch.setattr(worker, '_CloudProvider', Cloud)
    return runtime, cfg


def test_cloud_draft_waits_for_two_source_scans_and_audits_before_quality(cloud_schedule, monkeypatch):
    rt, cfg = cloud_schedule
    led, _, _ = rt
    seed(led, mid=1, body='生活環境: 合成の記録一。')
    seed(led, mid=2, body='生活環境: 合成の記録二。')
    led.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 WHERE project_id=1")
    led.db.commit()
    quality_inputs = []
    def quality(_ledger, pid, _cfg, night_id, doc, deadline, admit, **kwargs):
        snapshot = refresh._snapshot(led.db, pid, cfg)
        scans = refresh._current_scans(led.db, pid, snapshot['sources'], snapshot['contract'])
        quality_inputs.append({mid: (scans.get(mid, {}).get('complete'), scans.get(mid, {}).get('audited'))
                               for mid in (1, 2)})
        return 'pending', None
    monkeypatch.setattr(refresh, '_quality_step', quality)
    lanes = []
    for _ in range(40):
        result = step(rt, cfg=cfg)
        if result.get('lane'):
            lanes.append(result['lane'])
        if quality_inputs:
            break
    assert quality_inputs, 'finite source work must reach quality evaluation'
    assert quality_inputs[0] == {1: (True, True), 2: (True, True)}
    assert lanes.count('B') >= 2 and lanes.count('C') >= 2
    assert not led.artifacts(refresh.QUALITY_PASS, project_id=1)


@pytest.mark.parametrize('fact_source,canonical,allowed', [
    ('legacy', False, True), ('shadow', False, True),
    ('legacy', True, False), ('shadow', True, False),
    ('canonical', True, True), ('canonical', False, False),
])
def test_quality_scope_tracks_actual_fact_provenance_without_notification_enforce(runtime, fact_source, canonical, allowed):
    led, _, _ = runtime
    seed(led)
    policy = {'mode': 'assist', 'project_ids': [1], 'fact_source': fact_source}
    source = {'source': 'chat', 'body_state': 'full', 'body_text': 'synthetic',
              'canonical_context': canonical}
    assert refresh._quality_scope_allowed(led.db, 1, policy, [source]) is allowed
    assert not refresh._quality_scope_allowed(led.db, 2, policy, [source])
    with led.db:
        led.artifact_add_tx('semantic_control', 'paused', project_id=1)
    assert not refresh._quality_scope_allowed(led.db, 1, policy, [source])


def test_unapproved_canonical_quality_never_spends_or_falls_back(runtime, monkeypatch):
    import semantic_jev
    led, clock, _ = runtime
    seed(led)
    cfg = {**CFG, 'semantic': {'mode': 'assist', 'fact_source': 'canonical',
                              'project_ids': [1], 'daily_request_budget': 3000}}
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *_: 'synthetic-jev-key')
    monkeypatch.setattr(worker.cloud_policy, 'api_key', lambda *_: 'synthetic-anthropic-key')
    monkeypatch.setattr(semantic_jev, 'JevClient', lambda **_: pytest.fail('unapproved source made a request'))
    assert refresh._quality_step(led, 1, cfg, '2026-10-17', {}, clock.mono + 600,
                                 lambda: True) == ('deferred', None)
    assert not led.artifacts('semantic_usage')


def test_quality_budget_wait_does_not_prevent_other_patient_progress(cloud_schedule, monkeypatch):
    rt, cfg = cloud_schedule
    led, _, _ = rt
    seed(led, mid=1, pid=1)
    led.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1")
    led.db.commit()
    waits = []
    def wait(_ledger, pid, *_args, **_kwargs):
        waits.append(pid)
        return 'deferred', None
    monkeypatch.setattr(refresh, '_quality_step', wait)
    for _ in range(30):
        step(rt, cfg=cfg)
        if waits:
            break
    assert waits == [1], 'first patient must actually reach the budget wait'
    seed(led, mid=2, pid=2)
    led.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1")
    led.db.commit()
    assert not led.artifacts(refresh.SCAN, project_id=2)
    for _ in range(30):
        step(rt, cfg=cfg)
        if led.artifacts(refresh.SCAN, project_id=2):
            break
    assert led.artifacts(refresh.SCAN, project_id=2), 'other source work must progress after the wait'
    assert not led.artifacts(refresh.QUALITY_PASS, project_id=1)


@pytest.mark.parametrize('invalid', ['fetch_pending', 'fetch_unknown', 'capture_pending',
                                    'capture_invalid', 'capture_complete_without_proof'])
def test_incomplete_acquisition_cannot_be_accepted_as_whole_history(runtime, invalid):
    led, clock, _ = runtime
    seed(led, body='生活環境: 完全合成の記録。')
    led.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 WHERE project_id=1")
    led.db.commit()
    finish(runtime)
    doc = document(led)
    assert doc and doc['summary']
    if invalid.startswith('fetch_'):
        state = 'pending' if invalid == 'fetch_pending' else None
        led.db.execute('UPDATE patients SET fetch_state=? WHERE project_id=1', (state,))
    elif invalid == 'capture_invalid':
        led.db.execute("UPDATE patients SET initial_import='{' WHERE project_id=1")
    else:
        with led.db:
            initial_import_state.start_tx(led, 1, int(clock.wall), clock.wall - 1, boundary_source='server')
        state = initial_import_state.load(led.db, 1)
        if invalid == 'capture_complete_without_proof':
            state['capture_phase'] = 'complete'
        led.db.execute('UPDATE patients SET initial_import=? WHERE project_id=1', (json.dumps(state),))
    led.db.commit()
    led.db.execute('BEGIN')
    try:
        with pytest.raises(ValueError):
            refresh._quality_capture(led.db, 1, CFG, '2026-10-17', doc['summary'],
                                     {'model': 'synthetic'}, doc['temporal_view'])
    finally:
        led.db.rollback()
    assert not led.artifacts(refresh.QUALITY_PASS, project_id=1)
