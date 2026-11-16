"""Synthetic scan/summary contract split and per-artifact provenance (P0-2a)."""
import hashlib
import json
from pathlib import Path

import pytest

import attachment_extraction
import extract_llm
import inference_dependency_loss as dependency_loss
import patient_context_artifacts as context_artifacts
import patient_context_plan as context_plan
import patient_context_projection as context_projection
import patient_context_refresh as context_refresh
import patient_context_refresh_readiness as readiness
import patient_context_refresh_sources as sources
import patient_context_refresh_state as state
import patient_context_scan
import patient_context_schedule
import patient_context_selection
from test_patient_context_refresh import CFG, document, finish, runtime as runtime, seed, step

__all__ = ['runtime']


def _code(*modules):
    return hashlib.sha256(b''.join(Path(module.__file__).read_bytes() for module in modules)).hexdigest()


def _rows(led, kind):
    return [json.loads(row[0]) for row in led.db.execute(
        'SELECT meta FROM artifacts WHERE kind=? AND project_id=1 ORDER BY artifact_id', (kind,))]


def test_contracts_hold_code_and_versions_but_no_backend_name():
    contract, summary = state._contract(), state._summary_contract()
    assert set(contract) == {'version', state.VERSION_KEY, 'chunks', 'facts', 'scan_code',
                             'attachment_items_code', 'rule', 'llm', 'context'}
    assert set(summary) == {'version', 'code'} and summary['version'] == 'patient-context-summary/1'
    assert contract['version'] == 'patient-context-nightly/1'
    assert not {'model', 'provider', 'context_checks', 'state_code', 'dependency_code'} & set(contract)
    route = state._route({'local_llm': {'model': 'synthetic-named-local'}})
    assert 'synthetic-named-local' not in json.dumps([contract, summary])
    assert route['local_model'] == 'synthetic-named-local' and route['contract'] == contract
    assert route['provider'] is None and route['local_model_pinned'] is True


def test_scheduling_code_is_in_neither_contract():
    assert state._SCAN_CODE == _code(context_plan, patient_context_scan)
    assert state._SUMMARY_CODE == _code(patient_context_selection, context_projection, dependency_loss)
    schedule = Path(patient_context_schedule.__file__).read_bytes()
    for modules in ((context_plan, patient_context_scan),
                    (patient_context_selection, context_projection, dependency_loss)):
        assert all(Path(module.__file__).read_bytes() != schedule for module in modules)
    assert state._checks() == state._digest([state._SCAN_CODE, state._SUMMARY_CODE])


def test_summary_code_change_hides_publication_but_keeps_every_scan(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    finish(runtime)
    before = sources._snapshot(led.db, 1, CFG)
    scans = sources._current_scans(led.db, 1, before['sources'], before['route'], cfg=CFG)
    assert scans and readiness.context_view_input(led.db, 1, cfg=CFG) is not None
    checks = state._checks()
    monkeypatch.setattr(state, '_SUMMARY_CODE', 'f' * 64)
    after = sources._snapshot(led.db, 1, CFG)
    assert after['source_sig'] != before['source_sig']
    assert after['scan_source_sig'] == before['scan_source_sig'] and after['contract'] == before['contract']
    assert sources._current_scans(led.db, 1, after['sources'], after['route'], cfg=CFG) == scans
    # The view never renders the stale read-time interpretation.
    assert readiness.context_view_input(led.db, 1, cfg=CFG) is None
    assert state._checks() != checks
    pending = readiness._pending(led.db, 1, CFG, now=clock.wall)[2]
    assert pending['A'] and not pending['B'] and not pending['C']


def test_scan_code_change_reopens_the_scans(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    finish(runtime)
    monkeypatch.setattr(state, '_SCAN_CODE', 'e' * 64)
    after = sources._snapshot(led.db, 1, CFG)
    assert sources._current_scans(led.db, 1, after['sources'], after['route'], cfg=CFG) == {}


def test_attachment_item_interpretation_stays_a_scan_dependency_until_inputs_are_bound(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    finish(runtime)
    monkeypatch.setattr(state, '_ATTACHMENT_ITEMS_CODE', 'd' * 64)
    after = sources._snapshot(led.db, 1, CFG)
    assert sources._current_scans(led.db, 1, after['sources'], after['route'], cfg=CFG) == {}


def test_stored_scan_facts_carry_no_temporal_and_reads_compute_it(runtime):
    led, _, _ = runtime
    seed(led)
    finish(runtime)
    chunks = [json.loads(row[0]) for row in led.db.execute(
        'SELECT content FROM artifacts WHERE kind=?', (state.CHUNK,))]
    stored = [fact for chunk in chunks for fact in chunk['facts']]
    assert stored and all('temporal' not in fact for fact in stored)
    snapshot = sources._snapshot(led.db, 1, CFG)
    source = snapshot['sources'][0]
    scan = sources._current_scans(led.db, 1, snapshot['sources'], snapshot['route'], cfg=CFG)[1]
    read = list(sources._scan_facts(led.db, 1, source, snapshot['route'], scan))
    assert read and all(fact['temporal'] == context_projection.annotate(fact, source['body_text']) for fact in read)
    assert all(fact['temporal'] for fact in document(led)['facts'])


def test_every_artifact_stores_its_local_provenance(runtime):
    led, _, _ = runtime
    seed(led)
    finish(runtime)
    expected = {'backend': 'local', 'model': 'synthetic', 'profile': None}
    for kind in (state.SCAN, state.CHUNK, state.KIND):
        metas = _rows(led, kind)
        assert metas and all(meta['provenance'] == expected and meta['model'] == 'synthetic'
                             and context_artifacts.provenance(meta) == expected for meta in metas)
        assert all('route_contract' not in meta and 'model' not in meta['contract'] for meta in metas)


def test_local_model_change_rebuilds_local_scans_without_reusing_a_unit_key(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    finish(runtime)
    before = sources._snapshot(led.db, 1, CFG)
    monkeypatch.setattr(context_refresh.local_llm, 'resolve', lambda cfg: ('http://invalid.test', 'synthetic-other'))
    changed = sources._snapshot(led.db, 1, CFG)
    # The local model is not a signed input: only the local rows stop matching.
    assert changed['source_sig'] == before['source_sig']
    assert sources._current_scans(led.db, 1, changed['sources'], changed['route'], cfg=CFG) == {}
    assert readiness.context_view_input(led.db, 1, cfg=CFG) is None
    # The old unit is a different producer: rescanning the same span is not a replay.
    results = [step(runtime) for _ in range(6)]
    assert all(result.get('reason') != 'patient_context_original_unit_unverified' for result in results)
    assert any(result.get('lane') == 'B' and result['status'] == 'progressed' for result in results)
    rescanned = sources._current_scans(led.db, 1, changed['sources'], changed['route'], cfg=CFG)
    assert rescanned and rescanned[1]['_source_meta']['provenance']['model'] == 'synthetic-other'


def _as_cloud_route_fallback(led, provider):
    """Rewrite the finished local rows as the fallback a cloud route would have stored."""
    for kind in (state.SCAN, state.CHUNK):
        for artifact_id, content, raw in led.db.execute(
                'SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=1', (kind,)).fetchall():
            meta = {**json.loads(raw), 'route_provider': provider, 'local_fallback': True}
            if kind == state.CHUNK:
                doc = json.loads(content)
                meta['unit_key'] = state._stored_unit_key(meta, doc['chunk_span'])
                meta['chunk_key'] = state._digest([meta['unit_key'], doc['part']])
            led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), artifact_id))
    led.db.commit()


def test_local_route_rescans_a_cloud_route_fallback_without_a_replay_hold(runtime):
    led, _, _ = runtime
    seed(led)
    finish(runtime)
    _as_cloud_route_fallback(led, {'backend': 'synthetic-cloud', 'model': 'synthetic-cloud-model'})
    snapshot = sources._snapshot(led.db, 1, CFG)
    assert snapshot['route']['provider'] is None
    assert sources._current_scans(led.db, 1, snapshot['sources'], snapshot['route'], cfg=CFG) == {}
    # The fallback unit keeps its cloud provider, so the plain local unit is new.
    results = [step(runtime) for _ in range(6)]
    assert all(result.get('reason') != 'patient_context_original_unit_unverified' for result in results)
    assert any(result.get('lane') == 'B' and result['status'] == 'progressed' for result in results)
    rescanned = sources._current_scans(led.db, 1, snapshot['sources'], snapshot['route'], cfg=CFG)
    assert rescanned and 'local_fallback' not in rescanned[1]['_source_meta']
    assert not any(json.loads(row[0]).get('held') for row in led.db.execute(
        'SELECT content FROM artifacts WHERE kind=? AND project_id=1', (state.SCAN,)))


def test_live_route_without_a_local_model_value_never_accepts_a_local_row():
    contract = state._contract()
    meta = {'contract': contract, 'model': 'synthetic',
            'provenance': {'backend': 'local', 'model': 'synthetic', 'profile': None}}
    pinned = {'contract': contract, 'provider': None, 'local_model': None, 'local_model_pinned': True}
    assert not context_artifacts.route_accepts(pinned, meta)
    assert context_artifacts.route_accepts({**pinned, 'local_model': 'synthetic'}, meta)
    # Only the explicit snapshot-reader flag accepts the stored model.
    assert context_artifacts.route_accepts({**pinned, 'local_model_pinned': False}, meta)


def test_local_fallback_and_pure_local_rows_are_distinct_producers():
    contract = state._contract()
    provider = {'backend': 'synthetic-cloud', 'model': 'synthetic-cloud-model'}
    local = {'contract': contract, 'model': 'synthetic',
             'provenance': {'backend': 'local', 'model': 'synthetic', 'profile': None}}
    fallback = {**local, 'route_provider': provider, 'local_fallback': True}
    cloud = {'contract': contract, 'provider': provider, 'model': provider['model'],
             'provenance': {'backend': 'synthetic-cloud', 'model': provider['model'], 'profile': None}}
    local_route = {'contract': contract, 'provider': None, 'local_model': 'synthetic', 'local_model_pinned': True}
    cloud_route = {**local_route, 'provider': provider}
    assert context_artifacts.route_accepts(local_route, local) and not context_artifacts.route_accepts(local_route, fallback)
    assert context_artifacts.route_accepts(cloud_route, fallback) and not context_artifacts.route_accepts(cloud_route, local)
    assert context_artifacts.route_accepts(cloud_route, cloud) and not context_artifacts.route_accepts(local_route, cloud)
    # A cloud artifact never depends on the local model.
    assert context_artifacts.route_accepts({**cloud_route, 'local_model': 'synthetic-other'}, cloud)
    assert not context_artifacts.route_accepts({**cloud_route, 'local_model': 'synthetic-other'}, fallback)
    assert {key: fallback.get(key) for key in context_artifacts.BINDING_KEYS} != {
        key: local.get(key) for key in context_artifacts.BINDING_KEYS}
    # Source QA keeps the provider of the route but never a local model.
    assert context_artifacts.quality_binding(fallback) == context_artifacts.quality_binding(cloud_route)
    assert context_artifacts.quality_binding(local) == {'contract': contract, 'provider': None}
    for broken in ({**fallback, 'local_fallback': False}, {**cloud, 'route_provider': provider},
                   {**local, 'provenance': {**local['provenance'], 'backend': 'synthetic-cloud'}},
                   {**cloud, 'provenance': {**cloud['provenance'], 'backend': 'local'}},
                   {**local, 'model': 'synthetic-other'}):
        assert context_artifacts.provenance(broken) is None
        assert not any(context_artifacts.route_accepts(route, broken) for route in (local_route, cloud_route))


@pytest.mark.parametrize('name', ['patient_context_plan', 'patient_context_state', 'patient_context_refresh',
                                  'patient_context_scan', 'patient_context_selection', 'patient_context_schedule',
                                  'patient_context_artifacts', 'patient_context_refresh_readiness'])
def test_attachment_extraction_contract_ignores_patient_context_modules(monkeypatch, name):
    cfg = {'local_llm': {'url': 'http://127.0.0.1:8012', 'model': 'synthetic-local'}}
    base = attachment_extraction.contract(cfg, code_digest='a' * 64)
    assert name in extract_llm._LOADED_SOURCE_DIGESTS
    monkeypatch.setitem(extract_llm._LOADED_SOURCE_DIGESTS, name, '0' * 64)
    assert attachment_extraction.contract(cfg, code_digest='a' * 64) == base


@pytest.mark.parametrize('name', ['patient_context', 'patient_state_context', 'extract_llm'])
def test_attachment_extraction_contract_keeps_its_own_inputs(monkeypatch, name):
    cfg = {'local_llm': {'url': 'http://127.0.0.1:8012', 'model': 'synthetic-local'}}
    base = attachment_extraction.contract(cfg, code_digest='a' * 64)
    monkeypatch.setitem(extract_llm._LOADED_SOURCE_DIGESTS, name, '0' * 64)
    assert attachment_extraction.contract(cfg, code_digest='a' * 64) != base


def test_context_only_exclusions_name_loaded_patient_context_modules():
    loaded = set(extract_llm._LOADED_SOURCE_DIGESTS)
    assert attachment_extraction._CONTEXT_ONLY_SOURCES <= loaded
    assert {'patient_context', 'patient_state_context'} & attachment_extraction._CONTEXT_ONLY_SOURCES == set()
