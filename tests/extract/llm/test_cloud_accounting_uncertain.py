"""Completely synthetic owner/transport accounting boundaries."""
import json
import time

import pytest

import cloud_policy
import extract_llm as worker
from anthropic_client import AnthropicClient, MODEL
from test_anthropic_runtime import old
from test_anthropic_runtime import routed as routed

_REAL_RESERVE = cloud_policy.usage_reserver
_REAL_RESULT = cloud_policy.record_result
_REAL_DISPOSITION = cloud_policy.record_disposition_tx


def test_result_does_not_finish_a_borrowed_transaction(routed):
    led, *_ = routed
    # No metadata is consumed before this guard; the owning caller retains it.
    led.db.execute('CREATE TABLE synthetic_marker(value TEXT)')
    led.db.commit()
    led.db.execute("INSERT INTO synthetic_marker VALUES('pending')")
    with pytest.raises(RuntimeError, match='cloud_result_owner_transaction_active'):
        _REAL_RESULT(led, {}, {})
    assert led.db.in_transaction
    assert led.db.execute('SELECT count(*) FROM synthetic_marker').fetchone()[0] == 1
    led.db.rollback()
    assert led.db.execute('SELECT count(*) FROM synthetic_marker').fetchone()[0] == 0


def test_unsent_receipt_is_not_an_attempt(routed):
    led, cfg, *_ = routed
    provider = worker._CloudProvider(led, cfg, route='reextract', project_id=1,
        unit_key='synthetic', deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    provider.result({'id': 1}, {'http_class': 'not_sent'})
    assert len(provider.completed_reservations) == 1
    assert not provider.possibly_sent


@pytest.mark.parametrize('max_attempts, expected', [(None, 3), (1, 1)])
def test_unit_can_forbid_transport_retry_without_releasing_unknown_cost(
        routed, monkeypatch, max_attempts, expected):
    led, cfg, *_ = routed
    sent = []
    monkeypatch.setattr(cloud_policy, 'usage_reserver', _REAL_RESERVE)
    monkeypatch.setattr(cloud_policy, 'record_result', _REAL_RESULT)
    monkeypatch.setattr(worker.anthropic_client.time, 'sleep', lambda _: None)

    def post(body, timeout):
        sent.append(True)
        raise TimeoutError('synthetic timeout')

    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient',
                        lambda key, **kw: AnthropicClient(key, post_fn=post, **kw))
    provider = worker._CloudProvider(led, cfg, route='patient_context', project_id=1,
        unit_key='synthetic-summary-repair', deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kwargs: fn(), max_attempts=max_attempts)
    with pytest.raises(worker.CloudFallback, match='cloud_unit_failed'):
        provider('synthetic prompt', deadline=time.monotonic() + 180, max_tokens=100)
    report = cloud_policy.usage_report(led)
    assert len(sent) == expected
    assert report['daily_requests'] == report['unresolved_requests'] == expected
    assert provider.possibly_sent


def test_post_reservation_guard_refusal_never_sends_or_burns_retry(routed, monkeypatch):
    led, cfg, *_ = routed
    old(led)
    sent = []
    def reserve_factory(*args, **kwargs):
        reserve = _REAL_RESERVE(*args, **kwargs)
        def reserve_then_tighten(body, timeout):
            reservation = reserve(body, timeout)
            cfg['cloud_llm']['daily_request_budget'] = 0
            return reservation
        return reserve_then_tighten
    monkeypatch.setattr(cloud_policy, 'usage_reserver', reserve_factory)
    monkeypatch.setattr(cloud_policy, 'record_result', _REAL_RESULT)
    monkeypatch.setattr(cloud_policy, 'record_disposition_tx', _REAL_DISPOSITION)
    def post(body, timeout):
        sent.append(True)
        pytest.fail('guard refused before HTTP')
    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient',
                        lambda key: AnthropicClient(key, post_fn=post, max_attempts=1))
    monkeypatch.setattr(worker, 'circuit_open_s', lambda db: 60)
    def infer(body, **kwargs):
        if kwargs.get('provider') is not None:
            kwargs['provider']('synthetic prompt', deadline=time.monotonic() + 180, max_tokens=100)
        pytest.fail('local breaker is open')
    monkeypatch.setattr(worker, 'llm_extract', infer)
    report = worker.run_pending(led, budget_s=180)
    assert report['done'] == 0 and report['deferred'] == 1 and not sent
    results = [json.loads(row['meta']) for row in led.artifacts('cloud_usage_result')]
    assert len(results) == 1 and results[0]['http_class'] == 'not_sent'
    assert not [row for row in led.artifacts(worker.KIND) if json.loads(row['meta']).get('error')]
    assert cloud_policy.usage_report(led)['unresolved_requests'] == 0


def test_post_result_failure_records_backoff_if_owner_is_available(routed, monkeypatch):
    led, _, _, _, _ = routed
    old(led)
    sent = []
    monkeypatch.setattr(cloud_policy, 'usage_reserver', _REAL_RESERVE)
    monkeypatch.setattr(cloud_policy, 'record_result',
                        lambda *args: (_ for _ in ()).throw(RuntimeError('synthetic failure')))
    def post(body, timeout):
        sent.append(True)
        return 200, {}, json.dumps({'model': MODEL, 'usage': {'input_tokens': 10, 'output_tokens': 2},
            'content': [{'type': 'text', 'text': '{}'}], 'stop_reason': 'end_turn'}).encode()
    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient',
                        lambda key: AnthropicClient(key, post_fn=post, max_attempts=1))
    monkeypatch.setattr(worker, 'circuit_open_s', lambda db: 60)
    def infer(body, **kwargs):
        if kwargs.get('provider') is not None:
            kwargs['provider']('synthetic prompt', deadline=time.monotonic() + 180, max_tokens=100)
        pytest.fail('local breaker is open')
    monkeypatch.setattr(worker, 'llm_extract', infer)
    report = worker.run_pending(led, budget_s=180)
    assert sent == [True] and report['deferred'] == 1
    errors = [json.loads(row['meta']) for row in led.artifacts(worker.KIND)
              if json.loads(row['meta']).get('error')]
    assert len(errors) == 1 and errors[0]['attempts'] == 1 and errors[0]['next_try'] > time.time()
    accounting = cloud_policy.usage_report(led)
    assert accounting['unresolved_requests'] == 1 and accounting['daily_requests'] == 1
    worker.run_pending(led, budget_s=180)
    assert sent == [True]


@pytest.mark.parametrize('accounting_fails', [False, True])
def test_scope_denial_after_response_is_sticky_even_if_scope_restores(routed, monkeypatch, accounting_fails):
    led, cfg, *_ = routed
    old(led)
    sent, returned_cloud, local = [], [], []
    monkeypatch.setattr(cloud_policy, 'usage_reserver', _REAL_RESERVE)
    def post(body, timeout):
        sent.append(True)
        cfg['cloud_llm']['exclude_project_ids'] = [1]
        return 200, {}, json.dumps({'model': MODEL, 'usage': {'input_tokens': 10, 'output_tokens': 2},
            'content': [{'type': 'text', 'text': '{}'}], 'stop_reason': 'end_turn'}).encode()
    def finish(ledger, reservation, metadata):
        assert metadata['disposition'] == 'discarded_scope'
        cfg['cloud_llm'].pop('exclude_project_ids')
        if accounting_fails:
            raise RuntimeError('synthetic accounting failure')
        return _REAL_RESULT(ledger, reservation, metadata)
    monkeypatch.setattr(cloud_policy, 'record_result', finish)
    monkeypatch.setattr(cloud_policy, 'record_disposition_tx', _REAL_DISPOSITION)
    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient',
                        lambda key: AnthropicClient(key, post_fn=post, max_attempts=1))
    monkeypatch.setattr(worker, 'circuit_open_s', lambda db: 60 if accounting_fails else 0)
    def infer(body, **kwargs):
        if kwargs.get('provider') is not None:
            answer = kwargs['provider']('synthetic prompt', deadline=time.monotonic() + 180, max_tokens=100)
            returned_cloud.append(answer)
            return {}
        local.append(True)
        return {}
    monkeypatch.setattr(worker, 'llm_extract', infer)
    report = worker.run_pending(led, budget_s=180)
    assert sent == [True] and not returned_cloud
    errors = [json.loads(row['meta']) for row in led.artifacts(worker.KIND)
              if json.loads(row['meta']).get('error')]
    accounting = cloud_policy.usage_report(led)
    if accounting_fails:
        assert not local and report['deferred'] == 1
        assert len(errors) == 1 and errors[0]['attempts'] == 1 and errors[0]['next_try'] > time.time()
        assert accounting['unresolved_requests'] == 1
        worker.run_pending(led, budget_s=180)
        assert sent == [True]
    else:
        assert local == [True] and report['done'] == 1 and not errors
        assert accounting['unresolved_requests'] == 0
        [settled] = [json.loads(row['meta']) for row in led.artifacts('cloud_usage_result')]
        assert settled['disposition'] == 'discarded_scope'


def test_unknown_attempt_then_retry_scope_denial_still_records_backoff(routed, monkeypatch):
    led, cfg, *_ = routed
    old(led)
    sent = []
    monkeypatch.setattr(cloud_policy, 'usage_reserver', _REAL_RESERVE)
    def post(body, timeout):
        sent.append(True)
        raise TimeoutError('synthetic timeout')
    def finish(ledger, reservation, metadata):
        _REAL_RESULT(ledger, reservation, metadata)
        cfg['cloud_llm']['exclude_project_ids'] = [1]
    def discard(ledger, reservation, reason):
        cfg['cloud_llm'].pop('exclude_project_ids')
        return _REAL_DISPOSITION(ledger, reservation, reason)
    monkeypatch.setattr(cloud_policy, 'record_result', finish)
    monkeypatch.setattr(cloud_policy, 'record_disposition_tx', discard)
    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient',
                        lambda key: AnthropicClient(key, post_fn=post, max_attempts=2))
    monkeypatch.setattr(worker.anthropic_client.time, 'sleep', lambda _: None)
    monkeypatch.setattr(worker, 'circuit_open_s', lambda db: 60)
    def infer(body, **kwargs):
        if kwargs.get('provider') is not None:
            kwargs['provider']('synthetic prompt', deadline=time.monotonic() + 180, max_tokens=100)
        pytest.fail('local breaker is open')
    monkeypatch.setattr(worker, 'llm_extract', infer)
    report = worker.run_pending(led, budget_s=180)
    assert report['deferred'] == 1 and sent == [True]
    errors = [json.loads(row['meta']) for row in led.artifacts(worker.KIND)
              if json.loads(row['meta']).get('error')]
    assert len(errors) == 1 and errors[0]['attempts'] == 1 and errors[0]['next_try'] > time.time()
    accounting = cloud_policy.usage_report(led)
    assert accounting['daily_requests'] == 1 and accounting['unresolved_requests'] == 1
    worker.run_pending(led, budget_s=180)
    assert sent == [True]


def test_changed_source_never_receives_a_previous_attempt_failure(routed, monkeypatch):
    led, cfg, *_ = routed
    old(led)
    sent = []
    monkeypatch.setattr(cloud_policy, 'usage_reserver', _REAL_RESERVE)
    def post(body, timeout):
        sent.append(True)
        raise TimeoutError('synthetic timeout')
    def finish(ledger, reservation, metadata):
        _REAL_RESULT(ledger, reservation, metadata)
        ledger.db.execute("UPDATE messages SET content_hash='synthetic-new-hash' WHERE message_id=1")
        ledger.db.commit()
    monkeypatch.setattr(cloud_policy, 'record_result', finish)
    monkeypatch.setattr(cloud_policy, 'record_disposition_tx', _REAL_DISPOSITION)
    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient',
                        lambda key: AnthropicClient(key, post_fn=post, max_attempts=2))
    monkeypatch.setattr(worker.anthropic_client.time, 'sleep', lambda _: None)
    monkeypatch.setattr(worker, 'circuit_open_s', lambda db: 60)
    def infer(body, **kwargs):
        if kwargs.get('provider') is not None:
            kwargs['provider']('synthetic prompt', deadline=time.monotonic() + 180, max_tokens=100)
        pytest.fail('local breaker is open')
    monkeypatch.setattr(worker, 'llm_extract', infer)
    with pytest.raises(worker.CloudFallback, match='cloud_local_context_unavailable'):
        worker.run_pending(led, budget_s=180)
    assert sent == [True]
    assert not [row for row in led.artifacts(worker.KIND) if json.loads(row['meta']).get('error')]
    assert cloud_policy.usage_report(led)['unresolved_requests'] == 1
