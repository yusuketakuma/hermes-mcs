"""Synthetic cloud provenance: a local model change never invalidates cloud work (P0-2a)."""
import pytest

import inference_dependency_loss as dependency_loss
import patient_context_artifacts as context_artifacts
import patient_context_refresh as context_refresh
import patient_context_refresh_readiness as readiness
import patient_context_refresh_sources as sources
import patient_context_refresh_state as state
import semantic_policy
from test_patient_context_refresh import seed
from anthropic_context_testkit import CFG, cloud, drain, runtime

__all__ = ['runtime', 'cloud']

BODY = '見出しのない完全合成の居住記録。'


def test_local_model_change_keeps_cloud_scans_and_publication(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body=BODY)
    drain(runtime)
    view = readiness.context_view_input(led.db, 1, cfg=CFG)
    snapshot = sources._snapshot(led.db, 1, CFG)
    scans = sources._current_scans(led.db, 1, snapshot['sources'], snapshot['route'], cfg=CFG)
    assert view is not None and scans[1]['_provider_contract'] == snapshot['route']['provider']
    _, meta = state._latest(led.db, state.KIND, 1)
    assert meta['provenance']['backend'] == meta['provider']['backend']
    monkeypatch.setattr(context_refresh.local_llm, 'resolve', lambda cfg: ('http://invalid.test', 'synthetic-other'))
    changed = sources._snapshot(led.db, 1, CFG)
    assert changed['route']['local_model'] == 'synthetic-other'
    assert changed['source_sig'] == snapshot['source_sig']
    assert sources._current_scans(led.db, 1, changed['sources'], changed['route'], cfg=CFG) == scans
    assert readiness._publication_route(CFG, meta) is not None
    assert readiness.context_view_input(led.db, 1, cfg=CFG) == view


def test_source_quality_identity_is_the_same_on_capture_and_dependency_paths(runtime, cloud):
    led, _, _ = runtime
    seed(led, body=BODY)
    drain(runtime)
    snapshot = sources._snapshot(led.db, 1, CFG)
    scan = sources._current_scans(led.db, 1, snapshot['sources'], snapshot['route'], cfg=CFG)[1]
    assert scan['audited'] and scan['source_quality_audit_key']
    meta = scan['_source_meta']
    assert context_artifacts.quality_binding(meta) == context_artifacts.quality_binding(snapshot['route'])
    policy, errors = semantic_policy.semantic_config(CFG, key_present=True)
    assert not errors
    led.db.execute('BEGIN')
    try:
        captured = sources._source_quality_capture(led.db, 1, CFG, 1, state._quality_assessment(policy),
                                                    route=snapshot['route'])
        stored = context_artifacts.selected_scan(led.db, 1, 1)
        # Source QA reads the stored facts exactly; no read-time interpretation enters its identity.
        assert all('temporal' not in fact for fact in captured['facts'])
        assert captured['identity']['audit_key'] == scan['source_quality_audit_key']
        assert dependency_loss._verified_cloud_context(led.db, 1, 1, stored)
    finally:
        led.db.rollback()


@pytest.mark.parametrize('mutation', ['provider', 'contract'])
def test_cloud_scan_is_bound_to_its_provider_and_scan_contract(runtime, cloud, mutation):
    led, _, _ = runtime
    seed(led, body=BODY)
    drain(runtime)
    snapshot = sources._snapshot(led.db, 1, CFG)
    route = dict(snapshot['route'])
    if mutation == 'provider':
        route['provider'] = {**route['provider'], 'prompt': '0' * 16}
    else:
        route['contract'] = {**route['contract'], 'facts': 3}
    assert sources._current_scans(led.db, 1, snapshot['sources'], route, cfg=CFG) == {}
