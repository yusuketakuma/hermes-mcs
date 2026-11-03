"""Completely synthetic owner/transport accounting boundaries."""
import json
import time

import pytest

import cloud_policy
import extract_llm as worker
import cloud_inference
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
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route='reextract', project_id=1,
        unit_key='synthetic', deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    provider.result({'id': 1}, {'http_class': 'not_sent'})
    assert len(provider.completed_reservations) == 1
    assert not provider.possibly_sent


@pytest.mark.parametrize('max_attempts, expected', [(None, 1), (1, 1)])
def test_unit_can_forbid_transport_retry_without_releasing_unknown_cost(
        routed, monkeypatch, max_attempts, expected):
    led, cfg, *_ = routed
    sent = []
    monkeypatch.setattr(cloud_policy, 'usage_reserver', _REAL_RESERVE)
    monkeypatch.setattr(cloud_policy, 'record_result', _REAL_RESULT)
    monkeypatch.setattr(worker.anthropic_client.time, 'sleep', lambda _: None)

    def post(body, timeout):
        sent.append(True)
        raise ConnectionRefusedError('synthetic refusal')   # never proves "not sent": no retry

    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient',
                        lambda key, **kw: AnthropicClient(key, post_fn=post, **kw))
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route='patient_context', project_id=1,
        unit_key='synthetic-summary-repair', deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kwargs: fn(), max_attempts=max_attempts)
    with pytest.raises(cloud_inference.CloudFallback, match='cloud_unit_failed'):
        provider('synthetic prompt', deadline=time.monotonic() + 180, max_tokens=100)
    report = cloud_policy.usage_report(led)
    assert len(sent) == expected
    assert report['daily_requests'] == report['unresolved_requests'] == expected
    assert provider.possibly_sent


def test_first_attempt_reserve_refusal_is_the_only_typed_not_sent(routed, monkeypatch):
    """A pre-send refusal on the first attempt is the only definite not-sent:
    no POST leaves, no attempt is charged, nothing becomes a local unit."""
    led, cfg, *_ = routed
    sent = []
    monkeypatch.setattr(cloud_policy, 'record_result', _REAL_RESULT)

    def refuse(*args, **kwargs):
        def reservation(body, timeout):
            raise worker.anthropic_client.AnthropicError('budget_exceeded', 'synthetic refusal')
        return reservation
    monkeypatch.setattr(cloud_policy, 'usage_reserver', refuse)

    def post(body, timeout):
        sent.append(True)
        pytest.fail('a refused reservation still sent')
    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient',
                        lambda key, **kw: AnthropicClient(key, post_fn=post, max_attempts=3, **kw))
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route='patient_context', project_id=1,
        unit_key='synthetic-first-refusal', deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    with pytest.raises(worker.cloud_client.CloudNotSent, match='budget_exceeded'):
        provider('synthetic prompt', deadline=time.monotonic() + 180, max_tokens=100)
    assert not sent and not provider.completed_reservations
    assert not provider.possibly_sent
    assert cloud_policy.usage_report(led)['daily_requests'] == 0


def test_sent_attempt_failure_survives_a_later_reserve_refusal(routed, monkeypatch):
    """A 429 settles its reservation; a later pre-send refusal does not turn
    the whole call into a free defer — the sent failure carries the outcome."""
    led, cfg, *_ = routed
    sent, reserves = [], []
    monkeypatch.setattr(cloud_policy, 'record_result', _REAL_RESULT)

    def reserve_factory(*args, **kwargs):
        reserve = _REAL_RESERVE(*args, **kwargs)
        def maybe_refuse(body, timeout):
            if reserves:
                raise worker.anthropic_client.AnthropicError('budget_exceeded', 'synthetic refusal')
            reservation = reserve(body, timeout)
            reserves.append(reservation)
            return reservation
        return maybe_refuse
    monkeypatch.setattr(cloud_policy, 'usage_reserver', reserve_factory)
    monkeypatch.setattr(worker.anthropic_client.time, 'sleep', lambda _: None)

    def post(body, timeout):
        sent.append(True)
        return 429, {}, b'{}'
    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient',
                        lambda key, **kw: AnthropicClient(key, post_fn=post, max_attempts=2, **kw))
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route='patient_context', project_id=1,
        unit_key='synthetic-retry-refusal', deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kwargs: fn())
    with pytest.raises(cloud_inference.CloudFallback, match='cloud_unit_failed'):
        provider('synthetic prompt', deadline=time.monotonic() + 180, max_tokens=100)
    assert sent == [True] and len(reserves) == 1
    # the sent attempt keeps its settled reservation; the refusal created none
    assert len(provider.completed_reservations) == 1 and provider.possibly_sent
    assert cloud_policy.usage_report(led)['daily_requests'] == 1


def test_reserve_refusal_defers_without_send_or_local_fallback(routed, monkeypatch):
    led, cfg, *_ = routed
    old(led)
    sent = []
    monkeypatch.setattr(cloud_policy, 'record_result', _REAL_RESULT)
    monkeypatch.setattr(cloud_policy, 'record_disposition_tx', _REAL_DISPOSITION)

    def refuse(*args, **kwargs):
        def reservation(body, timeout):
            raise worker.anthropic_client.AnthropicError('budget_exceeded', 'synthetic refusal')
        return reservation
    monkeypatch.setattr(cloud_policy, 'usage_reserver', refuse)

    def post(body, timeout):
        sent.append(True)
        pytest.fail('a refused reservation still sent')
    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient',
                        lambda key: AnthropicClient(key, post_fn=post, max_attempts=1))
    monkeypatch.setattr(worker, 'circuit_open_s', lambda db: 60)

    def infer(body, **kwargs):
        if kwargs.get('provider') is not None:
            kwargs['provider']('synthetic prompt', deadline=time.monotonic() + 180, max_tokens=100)
        pytest.fail('a refused unit fell back to local')
    monkeypatch.setattr(worker, 'llm_extract', infer)
    report = worker.run_pending(led, budget_s=180)
    assert report['deferred'] == 1 and not sent
    assert not [row for row in led.artifacts('cloud_usage_reserve')]
    assert not [row for row in led.artifacts(worker.KIND) if json.loads(row['meta']).get('error')]
    assert cloud_policy.usage_report(led)['unresolved_requests'] == 0
    worker.run_pending(led, budget_s=180)
    assert not sent


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
    def finish(ledger, reservation, metadata, **kwargs):
        assert metadata['disposition'] == 'discarded_scope'
        cfg['cloud_llm'].pop('exclude_project_ids')
        if accounting_fails:
            raise RuntimeError('synthetic accounting failure')
        return _REAL_RESULT(ledger, reservation, metadata, **kwargs)
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
    report = worker.run_pending(led, budget_s=180)
    assert report['deferred'] == 1
    assert sent == [True]
    assert not [row for row in led.artifacts(worker.KIND) if json.loads(row['meta']).get('error')]
    assert cloud_policy.usage_report(led)['unresolved_requests'] == 1


def test_provider_reservation_dispatch_releases_owner_lock_before_http(routed, monkeypatch):
    led, cfg, *_ = routed
    monkeypatch.setattr(cloud_policy, 'usage_reserver', _REAL_RESERVE)
    monkeypatch.setattr(cloud_policy, 'record_result', _REAL_RESULT)
    led.db.execute('CREATE TABLE synthetic_slot(reservation_id TEXT)')
    led.db.commit()
    lock = {'held': False}
    deltas = []
    calls = []
    def dispatch(fn, **kwargs):
        assert not lock['held']
        return fn()
    def reserve_dispatch(fn):
        assert not lock['held'] and not led.db.in_transaction
        lock['held'] = True
        before = led.db.total_changes
        try:
            result = fn()
            assert not led.db.in_transaction
            deltas.append(led.db.total_changes - before)
            return result
        finally:
            lock['held'] = False
    def on_reserved(ledger, meta, artifact_id):
        assert lock['held'] and ledger.db.in_transaction
        ledger.db.execute('INSERT INTO synthetic_slot VALUES(?)', (meta['reservation_id'],))
        calls.append(artifact_id)
        return 1
    def post(body, timeout):
        assert not lock['held'] and not led.db.in_transaction
        return 200, {}, json.dumps({'model': MODEL, 'usage': {'input_tokens': 10, 'output_tokens': 2},
            'content': [{'type': 'text', 'text': '{}'}], 'stop_reason': 'end_turn'}).encode()
    monkeypatch.setattr(worker.anthropic_client, 'AnthropicClient',
                        lambda key, **kw: AnthropicClient(key, post_fn=post, **kw))
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route='patient_context', project_id=1,
        unit_key='synthetic-repair', deadline=time.monotonic() + 180, current=lambda: True,
        dispatch=dispatch, reserve_dispatch=reserve_dispatch, on_reserved_tx=on_reserved, max_attempts=1)
    assert provider('synthetic', deadline=time.monotonic() + 180, max_tokens=100)['status'] == 200
    assert len(calls) == 1 and deltas == [2]
    assert led.db.execute('SELECT count(*) FROM synthetic_slot').fetchone()[0] == 1
    assert cloud_policy.usage_report(led)['daily_requests'] == 1


@pytest.mark.parametrize("detail,expected", [
    ("cloud_unit_unresolved", "cloud_unit_unresolved"),
    ("owner_transaction_active", "cloud_owner_transaction"),
    ("deadline", "cloud_deadline"),
    ("daily_cap", "cloud_budget_blocked"),
    ("provider_monthly_cap", "cloud_budget_blocked"),
    ("pricing_unverified", "cloud_pricing_unverified"),
    ("synthetic-private-text", "not_sent"),
    ([], "not_sent"),
    (None, "not_sent"),
])
def test_not_sent_reason_uses_only_fixed_categories(detail, expected):
    error = worker.cloud_client.CloudNotSent("budget_exceeded", detail)
    assert worker.cloud_client.not_sent_reason(error) == expected
