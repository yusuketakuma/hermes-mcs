"""Synthetic overlapping cloud accounting and circuit recovery."""
from contextlib import closing
import time

import pytest

import cloud_policy
import openai_client
import semantic_runtime as runtime
from anthropic_client import AnthropicClient, AnthropicError, MODEL
from ledger import Ledger


def _reserve(ledger, unit_key='synthetic'):
    cfg = {'cloud_llm': {'mode': 'on', 'project_ids': [1]}}
    return cloud_policy.usage_reserver(ledger, cfg, route='reextract', project_id=1, unit_key=unit_key)(
        AnthropicClient('synthetic').body(unit_key, 100, None), 2)


def _open(ledger, now):
    error = AnthropicError('rate_limited', retryable=True, status=429)
    for _ in range(3):
        runtime.record_circuit_result(ledger, error, now=now, kind='cloud_circuit')


def test_late_success_is_saved_without_reopening_cloud_sends(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(runtime, '_circuit_now', lambda value: clock[0] if value is None else value)
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(Ledger(str(tmp_path / 'ledger.db'))) as owner, closing(Ledger(str(tmp_path / 'ledger.db'))) as other:
        reservation = _reserve(owner)
        _open(owner, clock[0])
        cloud_policy.record_result(other, reservation, {
            'model': MODEL, 'request_fp': reservation['request_fp'],
            'usage': {'input_tokens': 10, 'output_tokens': 2},
            'http_class': 'http_200', 'request_id': None, 'error_kind': None})
        assert len(owner.artifacts('cloud_usage_result')) == 1
        assert runtime.circuit_open(owner, now=clock[0], kind='cloud_circuit')
        for model in (MODEL, openai_client.MODEL):
            assert cloud_policy.global_block_reason(owner, model=model) == 'http_429'
        with pytest.raises(AnthropicError, match='budget_exceeded'):
            _reserve(owner, 'synthetic-while-open')
        assert len(owner.artifacts('cloud_usage_reserve')) == 1
        clock[0] += runtime._CIRCUIT_COOLDOWN_SECONDS
        assert cloud_policy.global_block_reason(owner, model=MODEL) is None
        with pytest.raises(AnthropicError, match='budget_exceeded'):
            _reserve(owner)
        assert _reserve(owner, 'synthetic-after-cooldown')


def test_cloud_explicit_reset_and_expired_success_still_close_circuit(tmp_path):
    with closing(Ledger(str(tmp_path / 'ledger.db'))) as ledger:
        _open(ledger, 1000)
        assert not runtime.record_circuit_result(ledger, None, now=1001, kind='cloud_circuit', reset=True)
        assert not runtime.circuit_open(ledger, now=1001, kind='cloud_circuit')
        _open(ledger, 2000)
        assert not runtime.record_circuit_result(ledger, None, now=2300, kind='cloud_circuit')
        assert not runtime.circuit_open(ledger, now=2300, kind='cloud_circuit')


def test_jev_success_keeps_existing_reset_behavior(tmp_path):
    with closing(Ledger(str(tmp_path / 'ledger.db'))) as ledger:
        error = AnthropicError('rate_limited', retryable=True, status=429)
        for _ in range(3):
            runtime.record_circuit_result(ledger, error, now=1000)
        assert not runtime.record_circuit_result(ledger, None, now=1001)
        assert not runtime.circuit_open(ledger, now=1001)


@pytest.mark.parametrize('content', [
    '{"consecutive_failures":0,"open_until":0,"failure_class":null,"failure_class":"auth_error"}',
    '{"consecutive_failures":0,"open_until":0,"failure_class":null,"other":NaN}',
    '{"consecutive_failures":3,"open_until":0,"failure_class":"auth_error"}',
    '{"consecutive_failures":3,"open_until":500,"failure_class":"payment_required"}',
])
def test_malformed_cloud_record_stays_held_until_explicit_reset(tmp_path, content):
    with closing(Ledger(str(tmp_path / 'ledger.db'))) as ledger:
        ledger.artifact_add('cloud_circuit', content)
        assert runtime._circuit_state(ledger, now=1000, kind='cloud_circuit')['failure_class'] == 'pricing_unverified'
        assert runtime.record_circuit_result(ledger, None, now=1000, kind='cloud_circuit')
        assert runtime.circuit_open(ledger, now=1000, kind='cloud_circuit')
        assert not runtime.record_circuit_result(ledger, None, now=1000, kind='cloud_circuit', reset=True)


def test_jev_duplicate_keys_fail_closed_for_the_bounded_cooldown(tmp_path):
    with closing(Ledger(str(tmp_path / 'ledger.db'))) as ledger:
        ledger.artifact_add(runtime._CIRCUIT_ARTIFACT_KIND,
                            '{"consecutive_failures":1,"consecutive_failures":0,"open_until":0,"failure_class":null}')
        state = runtime._circuit_state(ledger, now=1000)
        assert state['failure_class'] == 'malformed'
        assert runtime.circuit_open(ledger, now=state['open_until'] - 1)
        assert not runtime.circuit_open(ledger, now=state['open_until'] + 1)
