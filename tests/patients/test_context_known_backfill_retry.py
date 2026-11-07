"""Known cloud backfill preserves an overview's same-input retry boundary."""
import json

import pytest

import anthropic_client
import cloud_inference
import cloud_policy
import extract_llm as worker
import extract_llm_prepare
import extract_llm_provider
import mcs_queries
import patient_context_plan as plan
import patient_context_refresh as refresh
import patient_context_refresh_readiness as readiness
import patient_context_refresh_sources as sources
import patient_context_refresh_state as state
from test_context_cloud_update_identity import cloud as cloud, real_cloud as real_cloud, runtime as runtime
from test_patient_context_refresh import _prepare_summary, document, seed, step


_REAL_MODE = extract_llm_provider._cloud_mode
_REAL_SETTINGS = cloud_inference.resolve_cloud_settings


@pytest.mark.parametrize('attempts', [1, 5])
def test_known_cloud_backfill_does_not_rearm_same_scope_overview_retry(
        runtime, real_cloud, monkeypatch, attempts):
    led, clock, local_calls = runtime
    cfg, posts = real_cloud
    assert cfg['cloud_llm']['mode'] == 'on'
    assert cfg['cloud_llm']['provider'] == 'anthropic'
    assert cfg['cloud_llm']['project_ids'] == [1]
    configured = json.dumps(cfg, sort_keys=True)
    key = [None]
    monkeypatch.setattr(cloud_policy, 'api_key', lambda *_args: key[0])
    # The wire fixture pins mode to on. Restore real availability selection so
    # only the credential changes; the configured source contract stays fixed.
    monkeypatch.setattr(worker, '_cloud_mode', _REAL_MODE)
    monkeypatch.setattr(extract_llm_prepare, '_cloud_mode', _REAL_MODE)
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings', _REAL_SETTINGS)
    seed(led, body='生活環境: 合成の居住記録。')
    snapshot = sources._snapshot(led.db, 1, cfg, source_keys=[1])
    source, = snapshot['sources']
    known = plan.source_facts(source, source['items'], night_id='synthetic',
                              discovery='new_report', found_at=clock.wall)
    known_ids = {fact['id'] for fact in known}
    assert known_ids and snapshot['contract'].get('provider')
    assert worker._cloud_mode(led, cfg, 'patient_context', 1) == 'off'

    def known_answer(payload):
        current = next(mcs_queries.patient_context_sources(
            led.db, 1, mid=payload['source_identity']['key'], cfg=cfg))
        assert payload['source_identity']['signature'] == plan.source_signature(current)
        assert payload['source'] == current['body_text']
        items = [{name: item[name] for name in ('category', 'text', 'evidence', 'subject', 'details')
                  if name in item} for item in current['items']]
        assert items
        return {'patient_context': items}

    def local(_owner, prompt, **kwargs):
        assert not led.db.in_transaction
        payload = json.loads(prompt)
        local_calls.append(payload)
        if 'reports' in payload:
            assert payload['reports']
            return {'status': 200, 'finish_reason': 'length', 'text': '{}'}
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps(known_answer(payload))}

    wire = anthropic_client.AnthropicClient.request

    def cloud_wire(client, method, body, timeout, *, deadline):
        payload = json.loads(body['messages'][0]['content'])
        assert 'source' in payload and 'reports' not in payload
        status, headers, raw = wire(client, method, body, timeout, deadline=deadline)
        response = json.loads(raw)
        response['content'] = [{'type': 'text', 'text': json.dumps(known_answer(payload))}]
        return status, headers, json.dumps(response).encode()

    monkeypatch.setattr(refresh.local_llm, 'admitted_chat', local)
    monkeypatch.setattr(anthropic_client.AnthropicClient, 'request', cloud_wire)
    _prepare_summary(runtime, cfg=cfg)
    scans = sources._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=cfg)
    assert sources._scan_ready(scans[1]) and not scans[1].get('_provider_contract')
    assert led.synthetic_jev_requests and not posts
    local_chunks = led.artifacts(state.CHUNK, project_id=1)
    assert local_chunks
    for row in local_chunks:
        meta = json.loads(row['meta'])
        assert 'provider' not in meta and 'provider' not in meta['contract']
        assert meta['route_contract'] == snapshot['contract']

    for index in range(attempts):
        if index:
            clock.wall = document(led, state.PROGRESS)['next_try'] + 1
        result = step(runtime, cfg=cfg)
        assert (result['status'], result['lane'], result['reason']) == ('failed', 'A', 'nightly_length_stop')
        assert document(led, state.PROGRESS)['attempts'] == index + 1
    before = document(led, state.PROGRESS)
    assert before['source_scope'] == [1] and before['source_sig'] == snapshot['source_sig']
    assert before['next_try'] > clock.wall and before['failure'] == 'nightly_length_stop'
    assert sum('reports' in payload for payload in local_calls) == attempts
    assert not posts and document(led) is None

    key[0] = 'synthetic-key'
    assert worker._cloud_mode(led, cfg, 'patient_context', 1) == 'on'
    selected, progress, pending = readiness._pending(
        led.db, 1, cfg, now=clock.wall, cloud_scan=True, source_keys=[1])
    assert selected['source_sig'] == snapshot['source_sig'] and selected['contract'] == snapshot['contract']
    assert not pending['A'] and pending['B'] and progress['attempts'] == attempts
    result = step(runtime, cfg=cfg, local_admit=lambda: False)
    assert result['status'] == 'progressed' and result['lane'] == 'B'
    assert len(posts) == len(led.artifacts('cloud_usage_reserve')) == len(led.artifacts('cloud_usage_result')) == 1
    scan = document(led, state.SCAN)
    assert scan['complete'] and scan['facts']
    assert {fact['id'] for fact in scan['facts']} == known_ids
    assert all(fact['discovery'] == 'new_report' for fact in scan['facts'])
    after = document(led, state.PROGRESS)
    retry_fields = ('attempts', 'next_try', 'failure', 'checks', 'source_scope', 'source_sig')
    assert {name: after[name] for name in retry_fields} == {name: before[name] for name in retry_fields}
    assert sources._snapshot(led.db, 1, cfg, source_keys=[1])['source_sig'] == snapshot['source_sig']
    assert json.dumps(cfg, sort_keys=True) == configured and not led.db.in_transaction
