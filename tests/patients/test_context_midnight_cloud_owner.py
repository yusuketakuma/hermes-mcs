"""Synthetic midnight accounting through the real context cloud owner and client."""
import json
from types import SimpleNamespace

import pytest

import anthropic_client
import cloud_policy
import extract_llm as worker
import ledger as ledger_module
import semantic_runtime
from test_patient_context_refresh import CFG, document, runtime, seed, stamp, step

__all__ = ['runtime']


@pytest.mark.parametrize(('start', 'finish', 'unknown'), [
    ('2026-10-26T23:59:30', '2026-10-27T00:00:01', False),
    ('2026-10-31T23:59:30', '2026-11-01T00:00:01', False),
    ('2026-10-26T23:59:30', '2026-10-27T00:00:01', True),
])
def test_real_owner_reservation_and_response_across_midnight(runtime, monkeypatch, start, finish, unknown):
    led, clock, _ = runtime
    seed(led)
    clock.wall = stamp(start)
    settings = cloud_policy.cloud_config({'cloud_llm': {'mode': 'on', 'project_ids': [1],
                                                        'daily_request_budget': 1}})[0]
    fake_time = SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono,
                                perf_counter=lambda: clock.mono,
                                sleep=lambda seconds: setattr(clock, 'mono', clock.mono + seconds))
    for module in (worker, cloud_policy, anthropic_client, ledger_module, semantic_runtime):
        monkeypatch.setattr(module, 'time', fake_time)
    monkeypatch.setattr(worker, '_cloud_settings', lambda _cfg: settings)
    monkeypatch.setattr(worker, '_cloud_mode', lambda *_args: 'on')
    monkeypatch.setattr(worker, 'load_config', lambda: {'cloud_llm': settings})
    monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(cloud_policy, 'api_key', lambda *_args: 'synthetic-key')
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
    result = step(runtime, cfg=CFG, local_admit=lambda: False)
    assert len(calls) == 1
    reserves, results = led.artifacts('cloud_usage_reserve'), led.artifacts('cloud_usage_result')
    assert len(reserves) == len(results) == 1
    assert reserves[0]['created_at'] < stamp(finish) <= results[0]['created_at']
    saved = json.loads(results[0]['meta'])
    assert saved['reservation_id'] == json.loads(reserves[0]['meta'])['reservation_id']
    assert saved.get('disposition') != 'discarded_workday'
    if unknown:
        assert result['status'] == 'deferred' and document(led) is None
        assert saved['usage'] is None and saved['error_kind'] == 'pricing_unverified'
    else:
        assert result['status'] == 'progressed' and document(led)
        assert saved['usage']['input_tokens'] == 10 and saved['usage']['output_tokens'] == 2
        assert cloud_policy.usage_report(led)['daily_requests'] == 0
