"""Synthetic midnight accounting through the real context cloud owner and client."""
import json
from types import SimpleNamespace

import pytest

import anthropic_client
import cloud_policy
import cloud_client
import extract_llm as worker
import cloud_inference
import ledger as ledger_module
import semantic_runtime
import patient_context_refresh as context_refresh
from test_anthropic_context_refresh import cloud
from test_patient_context_refresh import CFG, document, runtime, seed, stamp, step

__all__ = ['runtime', 'cloud']

_REAL_PROVIDER = cloud_inference.CloudInferenceUnit
_REAL_WRITER_CURRENT = worker._writer_current


@pytest.mark.parametrize(('start', 'finish', 'unknown'), [
    ('2026-10-26T23:59:30', '2026-10-27T00:00:01', False),
    ('2026-10-31T23:59:30', '2026-11-01T00:00:01', False),
    ('2026-10-26T23:59:30', '2026-10-27T00:00:01', True),
])
def test_real_owner_reservation_and_response_across_midnight(runtime, cloud, monkeypatch, start, finish, unknown):
    led, clock, _ = runtime
    settings = cloud_policy.cloud_config({'cloud_llm': {'mode': 'on', 'provider': 'anthropic', 'project_ids': [1],
                                                        'daily_request_budget': 1, 'routes': ['patient_context']}})[0]
    cfg = {**CFG, 'cloud_llm': settings,
           'semantic': {**CFG['semantic'], 'project_ids': [1], 'daily_request_budget': 3000}}
    monkeypatch.setattr(worker, '_writer_current', _REAL_WRITER_CURRENT)
    monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: worker._WRITER_STATE)
    monkeypatch.setattr(worker, 'load_config', lambda: cfg)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings', lambda _cfg: settings)
    seed(led, body='見出しのない完全合成の居住記録。')
    # Prepare the exact cloud source proof via the real scan and Jev audit
    # using only synthetic provider responses; no cloud accounting is spent.
    for _ in range(10):
        prepared = step(runtime, cfg=cfg)
        assert prepared['status'] == 'progressed' or (
            prepared['status'] == 'deferred' and prepared.get('reason') == 'source_quality_pending'
        ), prepared
        scan = document(led, context_refresh.SCAN)
        if scan and scan.get('complete') and scan.get('audited'):
            break
    else:
        pytest.fail('synthetic source scan and audit did not complete')
    assert not document(led)
    assert not led.artifacts('cloud_usage_reserve')
    assert context_refresh._pending(led.db, 1, cfg)[2]['A']
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', _REAL_PROVIDER)
    clock.wall = stamp(start)
    clock.mono = 1000
    fake_time = SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono,
                                perf_counter=lambda: clock.mono,
                                sleep=lambda seconds: setattr(clock, 'mono', clock.mono + seconds))
    for module in (worker, cloud_policy, cloud_client, anthropic_client, ledger_module, semantic_runtime):
        monkeypatch.setattr(module, 'time', fake_time)
        monkeypatch.setattr(cloud_inference, 'time', module.time)
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings', lambda _cfg: settings)
    monkeypatch.setattr(worker, '_cloud_mode', lambda *_args: 'on')
    monkeypatch.setattr(cloud_policy, 'api_key', lambda *_args: 'synthetic-key')
    # Fixed calendar boundaries exercise accounting, not the live price-table date.
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    calls = []

    def request(_client, method, body, timeout, *, deadline):
        assert method == 'POST' and timeout > 0 and deadline > clock.mono
        reserved = led.artifacts('cloud_usage_reserve')
        assert len(reserved) == 1 and reserved[0]['created_at'] == clock.wall
        calls.append(1)
        prompt = json.loads(body['messages'][0]['content'])
        assert 'reports' in prompt
        answer = {'fact_ids': [item['id'] for item in prompt['reports'][:12]]}
        clock.wall = stamp(finish)
        clock.mono += 31
        response = {'model': anthropic_client.MODEL, 'stop_reason': 'end_turn',
                    'content': [{'type': 'text', 'text': json.dumps(answer)}],
                    'usage': {'input_tokens': 10, 'output_tokens': 2}}
        if unknown:
            response['usage']['synthetic_unpriced_field'] = 1
        return 200, {'Request-Id': 'synthetic'}, json.dumps(response).encode()

    monkeypatch.setattr(anthropic_client.AnthropicClient, 'request', request)
    result = step(runtime, cfg=cfg, local_admit=lambda: False)
    assert len(calls) == 1 and result.get('lane') == 'A', result
    reserves, results = led.artifacts('cloud_usage_reserve'), led.artifacts('cloud_usage_result')
    assert len(reserves) == len(results) == 1
    assert reserves[0]['created_at'] < stamp(finish) <= results[0]['created_at']
    saved = json.loads(results[0]['meta'])
    assert saved['reservation_id'] == json.loads(reserves[0]['meta'])['reservation_id']
    assert saved.get('disposition') != 'discarded_workday'
    if unknown:
        assert result['status'] == 'failed' and result['reason'] == 'nightly_cloud_failed'
        assert document(led) is None
        assert saved['usage'] is None and saved['error_kind'] == 'pricing_unverified'
        assert saved['cost_usd'] == json.loads(reserves[0]['meta'])['max_cost_usd']
        assert cloud_policy.global_block_reason(led) == 'pricing_unverified'
        accounting = cloud_policy.usage_report(led, now=stamp(start))
        assert accounting['unresolved_requests'] == 1
        assert accounting['max_reserved_cost_usd'] == saved['cost_usd']
        # Even after the persisted retry delay, the terminal pricing guard
        # prevents a second send or a new reservation for this input.
        clock.wall += 901
        clock.mono += 901
        step(runtime, cfg=cfg, local_admit=lambda: False)
        assert len(calls) == 1
        assert len(led.artifacts('cloud_usage_reserve')) == 1
        assert len(led.artifacts('cloud_usage_result')) == 1
        assert document(led) is None
    else:
        assert result['status'] == 'progressed' and document(led)
        assert saved['usage']['input_tokens'] == 10 and saved['usage']['output_tokens'] == 2
        assert cloud_policy.usage_report(led)['daily_requests'] == 0
