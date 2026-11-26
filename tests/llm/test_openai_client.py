"""Synthetic OpenAI Responses transport, usage and guarded billing regressions."""
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import time

import pytest

from mcs_util import payload_hash

import anthropic_client
import cloud_client
import cloud_cli
import cloud_policy as policy
from anthropic_client import AnthropicError
from ledger import Ledger
import openai_client as client


def usage(n=100, out=10, cached=20, written=30):
    return {"input_tokens": n, "output_tokens": out, "total_tokens": n + out,
            "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written},
            "output_tokens_details": {"reasoning_tokens": 2}}


def response(**kw):
    return {"model": client.MODEL, "service_tier": "default", "status": "completed", "usage": usage(),
            "output": [{"type": "reasoning"}, {"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": '{"synthetic":true}'}]}], **kw}


def test_cache_write_details_survive_success_accounting_and_log(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "pricing_valid", lambda *args, **kwargs: True)
    tokens = usage()
    writes = {"ttl_30m_tokens": 30, "ttl_12h_tokens": 0}
    tokens["input_tokens_details"]["cache_write_tokens_details"] = writes
    body = json.dumps(response(usage=tokens)).encode()
    calls = []
    provider = client.OpenAIClient("synthetic", post_fn=lambda *args: (calls.append(True) or 200, {}, body))
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "openai"}}
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        reserve = policy.usage_reserver(ledger, cfg, route="patient_context", project_id=1, unit_key="synthetic")
        result = provider.complete("synthetic" * 100, deadline=time.monotonic() + 10, max_tokens=100,
                                   reserve_fn=reserve,
                                   after_result=lambda reservation, meta, **_answer: policy.record_result(ledger, reservation, meta))
        assert calls == [True] and result["usage"] == tokens
        row = ledger.db.execute("SELECT kind,created_at,model,meta FROM artifacts WHERE kind=?", (policy.KINDS[1],)).fetchone()
        stored = json.loads(row["meta"])
        assert stored["usage"] == tokens and cloud_cli._safe_log(row, stored)["usage"] == tokens
        report = policy.usage_report(ledger)
        assert not report["pricing_unverified"] and report["unresolved_requests"] == 0
        assert Decimal(report["monthly_cost_usd"]) == policy.usage_cost(tokens, model=client.MODEL)
        assert policy.usage_cost(tokens, model=client.MODEL) == policy.usage_cost(usage(), model=client.MODEL)


@pytest.mark.parametrize('model,effort', [
    ('gpt-6-luna', 'medium'), ('gpt-6.1-sol', 'medium'), ('gpt-6.1-sol', 'high'),
])
def test_profile_client_binds_model_effort_and_output_limit(model, effort):
    instance = client.OpenAIClient('synthetic', model=model, effort=effort)
    body = instance.body('synthetic prompt', 8192, None)
    assert body['model'] == instance.model == model
    assert body['reasoning'] == {'effort': effort}
    assert body['max_output_tokens'] == 8192
    assert body['store'] is False and body['service_tier'] == 'default'


def test_sol_client_answer_uses_selected_model_and_rejects_other_model():
    instance = client.OpenAIClient('synthetic', model=client.SOL_MODEL, effort='medium')
    meta = {'usage': None}
    assert instance.answer(response(model=client.SOL_MODEL), meta)['model'] == client.SOL_MODEL
    with pytest.raises(AnthropicError, match='model_mismatch'):
        instance.answer(response(model=client.MODEL), {'usage': None})


@pytest.mark.parametrize('model,effort', [('gpt-6.1-sol', 'low'), ('unknown', 'high'),
                                         ('gpt-6-luna', 'high'), ([], 'low')])
def test_profile_client_rejects_unverified_model_effort(model, effort):
    with pytest.raises(ValueError, match='openai_profile_invalid'):
        client.OpenAIClient('synthetic', model=model, effort=effort)


def test_sol_price_has_separate_snapshot_and_reserves_long_cache_write():
    sol = client.SOL_MODEL
    bound, tokens = 300000, 1000
    assert policy.max_cost(tokens, input_bound=bound, model=sol, now=datetime(2026, 11, 1).timestamp()) == Decimal('1.515')
    usage = {'input_tokens': 272001, 'output_tokens': 100, 'total_tokens': 272101,
             'input_tokens_details': {'cached_tokens': 1, 'cache_write_tokens': 1}}
    expected = (Decimal(271999) * 2 + Decimal('0.1') + Decimal('2.5')) * 2 + Decimal(100) * 15
    assert policy.usage_cost(usage, model=sol) == expected / Decimal(1000000)
    assert policy._price_definition(client.MODEL)['price_version'] == client.PRICE_VERSION
    assert policy._price_definition(sol)['price_version'] == client.SOL_PRICE_VERSION


def test_success_answer_hash_is_bound_to_settled_result_without_text(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, 'pricing_valid', lambda *args, **kwargs: True)
    cfg = {'cloud_llm': {'mode': 'on', 'provider': 'openai', 'project_ids': [1]}}
    answer = '合成回答 synthetic-canary'
    raw = json.dumps(response(output=[{'type': 'message', 'role': 'assistant', 'content': [
        {'type': 'output_text', 'text': answer}]}]), ensure_ascii=False).encode('utf-8')
    instance = client.OpenAIClient('synthetic', post_fn=lambda *args: (200, {}, raw))
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reserve = policy.usage_reserver(ledger, cfg, route='patient_context', project_id=1, unit_key='synthetic')
        result = instance.complete('synthetic prompt', deadline=time.monotonic() + 10, max_tokens=100,
                                   reserve_fn=reserve,
                                   after_result=lambda reservation, meta, **_answer: policy.record_result(ledger, reservation, meta))
        assert result['text'] == answer
        row = ledger.artifacts('cloud_usage_result')[0]
        metadata = json.loads(row['meta'])
        digest = hashlib.sha256(answer.encode('utf-8')).hexdigest()
        assert metadata['answer_text_sha256_v1'] == digest
        assert answer not in row['meta'] and 'synthetic-canary' not in row['meta']
        reservation_id = ledger.artifacts('cloud_usage_reserve')[0]['artifact_id']
        evidence = policy.reservation_evidence(ledger.db, reservation_id)
        assert evidence['result']['answer_text_sha256_v1'] == digest
        assert evidence['result']['answer_finish_reason_v1'] == 'stop'
        assert 'answer_text_sha256_v1' not in evidence['reservation']


def test_prior_success_without_hash_keeps_accounting_and_has_no_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, 'pricing_valid', lambda *args, **kwargs: True)
    cfg = {'cloud_llm': {'mode': 'on', 'provider': 'openai', 'project_ids': [1]}}
    instance = client.OpenAIClient('synthetic')
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservation = policy.usage_reserver(ledger, cfg, route='patient_context', project_id=1, unit_key='synthetic')(
            instance.body('synthetic prompt', 100, None), 2)
        policy.record_result(ledger, reservation, {'model': client.MODEL, 'request_fp': reservation['request_fp'],
            'usage': usage(), 'http_class': 'http_200', 'error_kind': None})
        reservation_id = ledger.artifacts('cloud_usage_reserve')[0]['artifact_id']
        evidence = policy.reservation_evidence(ledger.db, reservation_id)
        assert 'answer_text_sha256_v1' not in evidence['result']
        assert policy.usage_report(ledger)['pricing_unverified'] is False


@pytest.mark.parametrize('bad', [None, True, 7, 'A' * 64, 'z' * 64, 'f' * 63])
def test_invalid_stored_answer_hash_blocks_receipt_pricing_and_new_reservations(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(policy, 'pricing_valid', lambda *args, **kwargs: True)
    cfg = {'cloud_llm': {'mode': 'on', 'provider': 'openai', 'project_ids': [1]}}
    instance = client.OpenAIClient('synthetic')
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservation = policy.usage_reserver(ledger, cfg, route='patient_context', project_id=1, unit_key='synthetic')(
            instance.body('synthetic prompt', 100, None), 2)
        policy.record_result(ledger, reservation, {'model': client.MODEL, 'request_fp': reservation['request_fp'],
            'usage': usage(), 'http_class': 'http_200', 'error_kind': None})
        row = ledger.artifacts('cloud_usage_result')[0]
        metadata = json.loads(row['meta'])
        metadata['answer_text_sha256_v1'] = bad
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                              (json.dumps(metadata), row['artifact_id']))
        reservation_id = ledger.artifacts('cloud_usage_reserve')[0]['artifact_id']
        with pytest.raises(ValueError, match='cloud_receipt_invalid'):
            policy.reservation_evidence(ledger.db, reservation_id)
        report = policy.usage_report(ledger)
        assert report['pricing_unverified'] is True and report['unresolved_requests'] == 1
        maximum = Decimal(reservation['max_cost_usd'])
        assert Decimal(report['monthly_cost_usd']) == Decimal(report['max_reserved_cost_usd']) == maximum
        assert Decimal(report['settled_cost_usd']) == 0
        with pytest.raises(AnthropicError, match='pricing_unverified'):
            policy.usage_reserver(ledger, cfg, route='patient_context', project_id=1, unit_key='another')(
                instance.body('synthetic prompt', 100, None), 2)
        assert len(ledger.artifacts('cloud_usage_reserve')) == len(ledger.artifacts('cloud_usage_result')) == 1


@pytest.mark.parametrize('bad', [None, True, 'A' * 64, 'not-a-hash'])
def test_result_writer_rejects_invalid_new_answer_hash(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(policy, 'pricing_valid', lambda *args, **kwargs: True)
    cfg = {'cloud_llm': {'mode': 'on', 'provider': 'openai', 'project_ids': [1]}}
    instance = client.OpenAIClient('synthetic')
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservation = policy.usage_reserver(ledger, cfg, route='patient_context', project_id=1, unit_key='synthetic')(
            instance.body('synthetic prompt', 100, None), 2)
        with pytest.raises(AnthropicError, match='pricing_unverified'):
            policy.record_result(ledger, reservation, {'model': client.MODEL, 'request_fp': reservation['request_fp'],
                'usage': usage(), 'http_class': 'http_200', 'error_kind': None, 'answer_text_sha256_v1': bad})
        assert not ledger.artifacts('cloud_usage_result')
        assert Decimal(policy.usage_report(ledger)['max_reserved_cost_usd']) == Decimal(reservation['max_cost_usd'])


def test_discarded_success_hash_never_becomes_reservation_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, 'pricing_valid', lambda *args, **kwargs: True)
    cfg = {'cloud_llm': {'mode': 'on', 'provider': 'openai', 'project_ids': [1]}}
    instance = client.OpenAIClient('synthetic')
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reservation = policy.usage_reserver(ledger, cfg, route='patient_context', project_id=1, unit_key='synthetic')(
            instance.body('synthetic prompt', 100, None), 2)
        policy.record_result(ledger, reservation, {'model': client.MODEL, 'request_fp': reservation['request_fp'],
            'usage': usage(), 'http_class': 'http_200', 'error_kind': None,
            'answer_text_sha256_v1': hashlib.sha256(b'synthetic').hexdigest()})
        with ledger.db:
            ledger.db.execute('BEGIN')
            policy.record_disposition_tx(ledger, reservation, 'discarded_scope')
        reservation_id = ledger.artifacts('cloud_usage_reserve')[0]['artifact_id']
        with pytest.raises(ValueError, match='cloud_receipt_invalid'):
            policy.reservation_evidence(ledger.db, reservation_id)
        assert policy.usage_report(ledger)['pricing_unverified'] is False


def test_reasoning_only_length_has_hash_for_returned_empty_text():
    raw = json.dumps(response(status='incomplete', incomplete_details={'reason': 'max_output_tokens'},
                              output=[{'type': 'reasoning'}])).encode()
    recorded = []
    instance = client.OpenAIClient('synthetic', post_fn=lambda *args: (200, {}, raw))
    result = instance.complete('synthetic prompt', deadline=time.monotonic() + 10, max_tokens=100,
                               after_result=lambda _reservation, meta, **_answer: recorded.append(meta), reserve_fn=lambda *args: None)
    assert result['text'] == '' and result['finish_reason'] == 'length'
    assert recorded[0]['answer_text_sha256_v1'] == hashlib.sha256(b'').hexdigest()
    assert recorded[0]['answer_finish_reason_v1'] == 'length'


def test_non_utf8_encodable_answer_is_charged_without_proof():
    result = response(output=[{'type': 'message', 'role': 'assistant', 'content': [
        {'type': 'output_text', 'text': '\ud800'}]}])
    recorded = []
    instance = client.OpenAIClient('synthetic', post_fn=lambda *args: (200, {}, json.dumps(result).encode()))
    with pytest.raises(AnthropicError, match='contract_error'):
        instance.complete('synthetic prompt', deadline=time.monotonic() + 10, max_tokens=100,
                          after_result=lambda _reservation, meta, **_answer: recorded.append(meta), reserve_fn=lambda *args: None)
    assert len(recorded) == 1 and recorded[0]['usage'] == usage()
    assert recorded[0]['error_kind'] == 'contract_error' and 'answer_text_sha256_v1' not in recorded[0]


def test_sol_selection_binds_reservation_result_and_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, 'pricing_valid', lambda *args, **kwargs: True)
    cfg = {'cloud_llm': {'mode': 'on', 'provider': 'openai', 'project_ids': [1]}}
    selection, errors = policy.resolve_profile(cfg, route='patient_context', purpose='patient_context')
    assert not errors
    instance = client.OpenAIClient('synthetic', model=selection['model'], effort=selection['effort'])
    body = instance.body('synthetic prompt', selection['max_tokens'], None)
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reserve = policy.usage_reserver(ledger, cfg, route='patient_context', project_id=1,
                                        unit_key='synthetic', selection=selection)
        invalid = {**body, 'reasoning': {'effort': 'high'}}
        with pytest.raises(AnthropicError, match='contract_error'):
            reserve(invalid, 2)
        assert not ledger.artifacts('cloud_usage_reserve')
        reservation = reserve(body, 2)
        assert (reservation['model'], reservation['effort'], reservation['profile']) == (
            client.SOL_MODEL, 'medium', 'sol-medium')
        assert reservation['price_version'] == client.SOL_PRICE_VERSION
        result = {'model': client.SOL_MODEL, 'request_fp': reservation['request_fp'],
                  'usage': usage(), 'http_class': 'http_200', 'error_kind': None}
        policy.record_result(ledger, reservation, result)
        settled = json.loads(ledger.artifacts('cloud_usage_result')[0]['meta'])
        assert (settled['model'], settled['effort'], settled['profile']) == (
            client.SOL_MODEL, 'medium', 'sol-medium')
        assert Decimal(settled['cost_usd']) == policy.usage_cost(usage(), model=client.SOL_MODEL)
        assert Decimal(policy.usage_report(ledger)['providers']['openai']['cost_usd']) == policy.usage_cost(usage(), model=client.SOL_MODEL)


def test_openai_models_share_provider_budget_and_keep_price_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, 'pricing_valid', lambda *args, **kwargs: True)
    cfg = {'cloud_llm': {'mode': 'on', 'provider': 'openai', 'project_ids': [1],
                         'profiles': {'patient_context': 'luna-medium',
                                      'quality_repair': 'sol-medium'}}}
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        costs = []
        for purpose in ('patient_context', 'quality_repair'):
            selection, errors = policy.resolve_profile(cfg, route='patient_context', purpose=purpose)
            assert not errors
            instance = client.OpenAIClient('synthetic', model=selection['model'], effort=selection['effort'])
            body = instance.body('synthetic ' + purpose, selection['max_tokens'], None)
            reservation = policy.usage_reserver(ledger, cfg, route='patient_context', project_id=1,
                                                unit_key=purpose, selection=selection)(body, 2)
            policy.record_result(ledger, reservation, {
                'model': selection['model'], 'request_fp': reservation['request_fp'],
                'usage': usage(), 'http_class': 'http_200', 'error_kind': None})
            costs.append(policy.usage_cost(usage(), model=selection['model'],
                                           price_version=reservation['price_version']))
        reservations = ledger.artifacts('cloud_usage_reserve')
        assert len(reservations) == 2
        assert {row['model'] for row in reservations} == {client.MODEL, client.SOL_MODEL}
        for row in reservations:
            meta = json.loads(row['meta'])
            assert meta['route'] == 'patient_context'
            assert meta['price_version'] == client.CURRENT_PRICE_VERSIONS[row['model']]
        report = policy.usage_report(ledger)
        assert not report['pricing_unverified']
        assert Decimal(report['providers']['openai']['cost_usd']) == sum(costs)
        assert Decimal(report['monthly_cost_usd']) == sum(costs)


def test_sol_selection_zero_budget_blocks_before_reservation(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, 'pricing_valid', lambda *args, **kwargs: True)
    cfg = {'cloud_llm': {'mode': 'on', 'provider': 'openai', 'project_ids': [1],
                         'daily_cost_limit_usd': 0}}
    selection, errors = policy.resolve_profile(cfg, route='patient_context', purpose='patient_context')
    assert not errors
    body = client.OpenAIClient('synthetic', model=selection['model'], effort=selection['effort']).body(
        'synthetic prompt', selection['max_tokens'], None)
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        reserve = policy.usage_reserver(ledger, cfg, route='patient_context', project_id=1,
                                        unit_key='synthetic', selection=selection)
        with pytest.raises(AnthropicError):
            reserve(body, 2)
        assert not ledger.artifacts('cloud_usage_reserve')


@pytest.mark.parametrize("writes", [
    None, [], {}, {"ttl_30m_tokens": 30}, {"ttl_12h_tokens": 0},
    {"ttl_30m_tokens": 30, "ttl_12h_tokens": 0, "unknown": 0},
    {"ttl_30m_tokens": True, "ttl_12h_tokens": 0},
    {"ttl_30m_tokens": 30, "ttl_12h_tokens": False},
    {"ttl_30m_tokens": -1, "ttl_12h_tokens": 0},
    {"ttl_30m_tokens": 30, "ttl_12h_tokens": -1},
    {"ttl_30m_tokens": 30.0, "ttl_12h_tokens": 0},
    {"ttl_30m_tokens": 2**53 + 1, "ttl_12h_tokens": 0},
    {"ttl_30m_tokens": 29, "ttl_12h_tokens": 0},
    {"ttl_30m_tokens": 29, "ttl_12h_tokens": 1},
])
def test_invalid_cache_write_details_fail_closed(writes):
    tokens = usage()
    tokens["input_tokens_details"]["cache_write_tokens_details"] = writes
    with pytest.raises(AnthropicError, match="pricing_unverified"):
        client.validate_usage(tokens)


def test_12h_cache_write_keeps_attempt_reserved_and_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "pricing_valid", lambda *args, **kwargs: True)
    tokens = usage()
    tokens["input_tokens_details"]["cache_write_tokens_details"] = {"ttl_30m_tokens": 29, "ttl_12h_tokens": 1}
    raw = json.dumps(response(usage=tokens)).encode()
    calls = []
    provider = client.OpenAIClient("synthetic", post_fn=lambda *args: (calls.append(True) or 200, {}, raw))
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "openai"}}
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        reserve = policy.usage_reserver(ledger, cfg, route="patient_context", project_id=1, unit_key="synthetic")
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            provider.complete("synthetic" * 100, deadline=time.monotonic() + 10, max_tokens=100,
                              reserve_fn=reserve,
                              after_result=lambda reservation, meta, **_answer: policy.record_result(ledger, reservation, meta))
        report = policy.usage_report(ledger)
        assert calls == [True] and report["unresolved_requests"] == 1
        assert Decimal(report["max_reserved_cost_usd"]) > 0


def test_openai_uses_the_neutral_base_and_shared_error_identity():
    assert client.OpenAIClient.__bases__ == (cloud_client.CloudClient,)
    assert anthropic_client.AnthropicError is client.AnthropicError is cloud_client.CloudError


@pytest.mark.parametrize("module,key", [
    (anthropic_client, ("claude-haiku-5-5", "anthropic-haiku-5-5-2026-10-26")),
    (client, ("gpt-6-luna", "openai-gpt-6-luna-2026-10-29")),
])
def test_previous_price_snapshot_is_immutable_when_current_alias_changes(module, key, monkeypatch):
    snapshot = module.PRICE_SNAPSHOTS[key]
    validator = snapshot["validate_usage"]
    with pytest.raises(TypeError):
        snapshot["prices"]["input"] = "999"
    monkeypatch.setattr(module, "MODEL", "synthetic-future")
    monkeypatch.setattr(module, "PRICE_VERSION", "synthetic-future-version")
    monkeypatch.setattr(module, "PRICES", {"input": "999"})
    monkeypatch.setattr(module, "validate_usage", lambda value: {})
    assert snapshot["model"] == key[0] and snapshot["price_version"] == key[1]
    assert snapshot["prices"]["input"] == "0.1" and snapshot["validate_usage"] is validator


@pytest.mark.parametrize("kind", ["duplicate", "nan", "infinity"])
def test_success_with_non_strict_json_keeps_usage_unknown(kind):
    raw = json.dumps(response()).encode()
    if kind == "duplicate":
        raw = raw.replace(b'"input_tokens": 100', b'"input_tokens": 100, "input_tokens": 100', 1)
    else:
        value = b"NaN" if kind == "nan" else b"1e9999"
        raw = raw.replace(b'{"model":', b'{"synthetic_invalid":' + value + b',"model":', 1)
    calls, recorded = [], []
    provider = client.OpenAIClient("synthetic", post_fn=lambda *args: (calls.append(True) or 200, {}, raw))
    with pytest.raises(AnthropicError, match="protocol_error"):
        provider.complete("synthetic", deadline=time.monotonic() + 10,
                          after_result=lambda _reservation, meta, **_answer: recorded.append(meta), reserve_fn=lambda *args: None)
    assert len(calls) == len(recorded) == 1
    assert recorded[0]["http_class"] == "http_200" and recorded[0]["usage"] is None


def test_reasoning_only_output_limit_returns_length_for_split():
    doc = response(status="incomplete", incomplete_details={"reason": "max_output_tokens"},
                   output=[{"type": "reasoning"}])
    provider = client.OpenAIClient("synthetic", post_fn=lambda *args: (200, {}, json.dumps(doc).encode()))
    out = provider.complete("synthetic", deadline=time.monotonic() + 10, reserve_fn=lambda *args: None)
    assert out["text"] == "" and out["finish_reason"] == "length" and out["usage"] == usage()


@pytest.mark.parametrize("doc", [
    response(output=[]),
    response(output=[{"type": "reasoning"}]),
    response(output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ""}]}]),
    response(status="incomplete", incomplete_details={"reason": "other"}, output=[{"type": "reasoning"}]),
    response(status="incomplete", incomplete_details={"reason": "max_output_tokens"}, output=[]),
])
def test_other_empty_outputs_are_not_accepted(doc):
    provider = client.OpenAIClient("synthetic", post_fn=lambda *args: (200, {}, json.dumps(doc).encode()))
    with pytest.raises(AnthropicError, match="contract_error"):
        provider.complete("synthetic", deadline=time.monotonic() + 10, reserve_fn=lambda *args: None)


def test_proven_unsent_timeout_releases_only_that_synthetic_reservation(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "pricing_valid", lambda *args, **kwargs: True)
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "openai"}}
    calls = []
    def post(*args):
        calls.append(True)
        raise client.bounded_http.NotSentTimeout("synthetic")
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        provider = client.OpenAIClient("synthetic", post_fn=post)
        reserve = policy.usage_reserver(ledger, cfg, route="patient_context", project_id=1, unit_key="synthetic")
        with pytest.raises(AnthropicError, match="budget_exceeded"):
            provider.complete("synthetic", deadline=time.monotonic() + 10, max_tokens=100,
                              reserve_fn=reserve,
                              after_result=lambda reservation, meta, **_answer: policy.record_result(ledger, reservation, meta))
        report = policy.usage_report(ledger)
        assert len(calls) == 1 and report["unresolved_requests"] == 0
        assert Decimal(report["monthly_cost_usd"]) == 0


@pytest.mark.parametrize("provider_name", ["anthropic", "openai"])
def test_duplicate_success_json_keeps_the_paid_attempt_reserved(tmp_path, monkeypatch, provider_name):
    monkeypatch.setattr(policy, "pricing_valid", lambda *args, **kwargs: True)
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": provider_name}}
    provider_type = anthropic_client.AnthropicClient if provider_name == "anthropic" else client.OpenAIClient
    calls = []
    def post(*args):
        calls.append(True)
        return 200, {}, b'{"usage":{},"usage":{}}'
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        provider = provider_type("synthetic", post_fn=post)
        reserve = policy.usage_reserver(ledger, cfg, route="patient_context", project_id=1, unit_key="synthetic")
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            provider.complete("synthetic", deadline=time.monotonic() + 10, max_tokens=100,
                              reserve_fn=reserve,
                              after_result=lambda reservation, meta, **_answer: policy.record_result(ledger, reservation, meta))
        report = policy.usage_report(ledger)
        assert len(calls) == 1 and report["unresolved_requests"] == 1
        assert Decimal(report["max_reserved_cost_usd"]) > 0
        with pytest.raises(AnthropicError):
            reserve(provider.body("synthetic", 100, None), 2)


def test_responses_uses_fixed_model_no_storage_and_exact_attempt_hooks():
    seen, sent = [], []

    def post(body, timeout):
        assert body["model"] == client.MODEL and body["max_output_tokens"] == 100
        assert body["reasoning"] == {"effort": "low"}
        assert body["service_tier"] == "default" and body["store"] is False and body["tools"] == []
        # no fixed head: explicit mode with no breakpoint writes nothing to the cache
        assert body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
        assert body["input"] == "synthetic JSON request"
        sent.append(body)
        return 200, {"x-request-id": "req_synthetic"}, json.dumps(response()).encode()

    provider = client.OpenAIClient("synthetic-private", post_fn=post)
    result = provider.complete("synthetic JSON request", deadline=time.monotonic() + 10,
        max_tokens=100, before_attempt=lambda: seen.append("guard"),
        reserve_fn=lambda body, timeout: seen.append("reserve") or 123,
        after_result=lambda reserved, meta, **_answer: seen.append((reserved, meta)))
    assert len(sent) == 1 and result["model"] == client.MODEL and result["finish_reason"] == "stop"
    assert seen[:3] == ["guard", "reserve", "guard"]
    assert seen[3][0] == 123 and seen[3][1]["request_id"] == "req_synthetic"
    assert seen[3][1]["usage"] == usage()


@pytest.mark.parametrize("doc,kind", [
    (response(model="unexpected"), "model_mismatch"),
    (response(service_tier="priority"), "pricing_unverified"),
    (response(status="failed"), "contract_error"),
    (response(output=[{"type": "function_call"}]), "contract_error"),
    (response(usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}), "pricing_unverified")])
def test_unknown_model_price_or_output_never_becomes_success(doc, kind):
    records = []
    provider = client.OpenAIClient("synthetic-private", post_fn=lambda *a: (200, {}, json.dumps(doc).encode()))
    with pytest.raises(AnthropicError) as error:
        provider.complete("synthetic JSON", max_tokens=100, deadline=time.monotonic() + 10,
            reserve_fn=lambda *a: 123, after_result=lambda *a, **_answer: records.append(a))
    assert error.value.kind == kind and len(records) == 1
    assert records[0][1]["error_kind"] == kind


def test_uncertain_send_is_recorded_on_each_bounded_retry():
    sent, records = [], []

    def post(*a):
        sent.append(True)
        raise TimeoutError("synthetic")

    provider = client.OpenAIClient("synthetic-private", post_fn=post, wait_fn=lambda delay: None)
    with pytest.raises(AnthropicError):
        provider.complete("synthetic JSON", deadline=time.monotonic() + 60,
            after_result=lambda *a, **_answer: records.append(a), reserve_fn=lambda *args: None)
    assert len(sent) == len(records) == 6
    assert all(record[1]["usage"] is None for record in records)


@pytest.mark.parametrize("fields,expected", [
    ({"code": "insufficient_quota"}, "insufficient_quota"),
    ({"code": "insufficient_quota", "type": "insufficient_quota"}, "insufficient_quota"),
    ({"code": "credit_balance_exhausted"}, "credit_balance_exhausted"),
    ({"code": "organization_spend_limit_exceeded"}, "organization_spend_limit_exceeded"),
    ({"code": "project_spend_limit_exceeded", "type": "insufficient_quota"}, "project_spend_limit_exceeded"),
    ({"code": "organization_usage_limit_exceeded"}, "organization_usage_limit_exceeded"),
    ({"type": "insufficient_quota", "code": None}, "insufficient_quota"),
])
def test_quota_429_is_one_attempt_and_only_fixed_metadata(fields, expected):
    calls, records = [], []
    body = json.dumps({"error": {**fields, "message": "synthetic-secret-canary"}}).encode()
    provider = client.OpenAIClient("synthetic-private", post_fn=lambda *args: (
        calls.append(True) or 429, {"unknown": "synthetic-secret-canary"}, body))
    with pytest.raises(AnthropicError) as caught:
        provider.complete("synthetic", deadline=time.monotonic() + 10,
                          after_result=lambda *args, **_answer: records.append(args), reserve_fn=lambda *args: None)
    assert caught.value.kind == "payment_required" and not caught.value.retryable
    assert len(calls) == len(records) == 1
    metadata = records[0][1]
    assert metadata["http_class"] == "http_429" and metadata["usage"] is None
    assert metadata["error_kind"] == "payment_required" and metadata["openai_error_code"] == expected
    assert "synthetic-secret-canary" not in repr(records) + repr(caught.value)


@pytest.mark.parametrize("body", [
    b'{"error":{"code":"future_synthetic_secret"}}',
    b'{"error":{"code":"future_synthetic_secret","type":"insufficient_quota"}}',
    b'{"error":{"code":"","type":"insufficient_quota"}}',
    b'{"error":{"code":true,"type":"insufficient_quota"}}',
    b'{"error":{"code":"rate_limit_exceeded","type":"rate_limit_error"}}',
    b'{"error":{"code":"insufficient_quota","type":"rate_limit_error"}}',
    b'{"error":{"code":"insufficient_quota","type":"future_type"}}',
    b'{"error":{"code":[],"type":{}}}',
    b'{"error":{"code":"insufficient_quota","code":"other"}}',
    b'{"error":', b'\xff', b'[]', b'x' * (client.bounded_http.MAX_RESPONSE_BYTES + 1), None,
])
def test_unknown_429_keeps_legacy_status_class_without_leaking_body(body):
    calls, records, delays = [], [], []
    provider = client.OpenAIClient("synthetic-private", post_fn=lambda *args: (
        calls.append(True) or 429, {"Retry-After": "0.25", "unknown": "synthetic-secret-canary"}, body),
        wait_fn=delays.append)
    with pytest.raises(AnthropicError) as caught:
        provider.complete("synthetic", deadline=time.monotonic() + 10,
                          after_result=lambda *args, **_answer: records.append(args), reserve_fn=lambda *args: None)
    assert caught.value.kind == "rate_limited" and caught.value.retryable
    assert len(calls) == len(records) == 6 and delays == [0.25] * 5
    assert all("openai_error_code" not in meta for _, meta in records)
    assert "synthetic-secret-canary" not in repr(records) + repr(caught.value)
    assert "future_synthetic_secret" not in repr(records) + repr(caught.value)


@pytest.mark.parametrize("body,expected", [
    (b'{"error":{"code":"rate_limit_exceeded","type":"rate_limit_error",'
     b'"message":"synthetic-secret-canary"}}', "rate_limit_exceeded"),
    (b'{"error":{"code":"slow_down"}}', "slow_down"),
    (b'{"error":{"type":"rate_limit_error","code":null}}', "rate_limit_error"),
    (b'{"error":{}}', "missing_code"),
    (b'{"error":{"code":"synthetic-unknown-secret"}}', "unknown_code"),
    (b'{"error":{"type":"synthetic-unknown-secret"}}', "unknown_type"),
    (b'{"error":{"code":"insufficient_quota","type":"rate_limit_error"}}', "conflicting_fields"),
    (b'{"error":{"code":"synthetic-unknown-secret","type":"insufficient_quota"}}', "conflicting_fields"),
    (b'{"error":{"code":true}}', "invalid_field_type"),
    (None, "body_not_bytes"),
    (b'x' * (client.bounded_http.MAX_RESPONSE_BYTES + 1), "body_too_large"),
    (b'\xff', "body_invalid_utf8"),
    (b'{"error":', "body_invalid_json"),
    (b'{"error":{"code":"a","code":"b"}}', "body_duplicate_keys"),
    (b'{"error":{"code":NaN}}', "body_nonfinite"),
    (b'[]', "body_not_object"),
    (b'{}', "error_missing"),
    (b'{"error":[]}', "error_not_object"),
])
def test_openai_429_diagnostic_is_bounded_and_contains_only_fixed_labels(body, expected):
    sent, recorded = [], []
    provider = client.OpenAIClient("synthetic", max_attempts=1, post_fn=lambda *args: (
        sent.append(True) or 429, {"unknown": "synthetic-secret-canary"}, body))
    with pytest.raises(AnthropicError, match="rate_limited"):
        provider.complete("synthetic", deadline=time.monotonic() + 10,
                          after_result=lambda _reservation, meta, **_answer: recorded.append(meta), reserve_fn=lambda *args: None)
    assert len(sent) == len(recorded) == 1
    assert recorded[0]["openai_429_diagnostic"] == expected
    assert "openai_error_code" not in recorded[0]
    assert "synthetic-secret-canary" not in repr(recorded)
    assert "synthetic-unknown-secret" not in repr(recorded)


def test_openai_429_decoder_recursion_is_bounded(recursion_limited_json):
    sent, recorded = [], []
    body = b'[' * 2000 + b']' * 2000
    provider = client.OpenAIClient("synthetic", max_attempts=1, post_fn=lambda *args: (
        sent.append(True) or 429, {}, body))
    with pytest.raises(AnthropicError, match="rate_limited"):
        provider.complete("synthetic", deadline=time.monotonic() + 10,
                          after_result=lambda _reservation, meta, **_answer: recorded.append(meta), reserve_fn=lambda *args: None)
    assert recursion_limited_json and len(sent) == len(recorded) == 1
    assert recorded[0]["openai_429_diagnostic"] == "body_too_deep"
    assert "openai_error_code" not in recorded[0]


def test_known_openai_rate_limit_keeps_bounded_retry_after():
    calls, delays, recorded = [], [], []
    raw = b'{"error":{"code":"rate_limit_exceeded","type":"rate_limit_error"}}'
    provider = client.OpenAIClient("synthetic", post_fn=lambda *args: (
        calls.append(True) or 429, {"Retry-After": "0.25"}, raw), wait_fn=delays.append)
    with pytest.raises(AnthropicError, match="rate_limited"):
        provider.complete("synthetic", deadline=time.monotonic() + 10,
                          after_result=lambda _reservation, meta, **_answer: recorded.append(meta), reserve_fn=lambda *args: None)
    assert len(calls) == len(recorded) == 6 and delays == [0.25] * 5
    assert all(item["openai_429_diagnostic"] == "rate_limit_exceeded" for item in recorded)


@pytest.mark.parametrize("change", ["false", "integer", "string", "status", "kind", "usage", "usage_missing", "provider"])
def test_quota_marker_writer_rejects_inconsistent_evidence(tmp_path, monkeypatch, change):
    monkeypatch.setattr(policy, "pricing_valid", lambda *args, **kwargs: True)
    provider = "anthropic" if change == "provider" else "openai"
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": provider}}
    provider_client = (anthropic_client.AnthropicClient("synthetic") if provider == "anthropic"
                       else client.OpenAIClient("synthetic"))
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        reservation = policy.usage_reserver(ledger, cfg, route="patient_context", project_id=1,
            unit_key="synthetic")(provider_client.body("synthetic", 100, None), 2)
        result = {"model": reservation["model"], "request_fp": reservation["request_fp"],
                  "usage": None, "http_class": "http_429", "error_kind": "payment_required",
                  "openai_error_code": "project_spend_limit_exceeded"}
        changes = {"false": {"openai_error_code": False},
                   "integer": {"openai_error_code": 1},
                   "string": {"openai_error_code": "synthetic_unknown"},
                   "status": {"http_class": "http_402"},
                   "kind": {"error_kind": "rate_limited"}, "usage": {"usage": usage()}}
        result.update(changes.get(change, {}))
        if change == "usage_missing":
            del result["usage"]
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            policy.record_result(ledger, reservation, result)
        assert not ledger.artifacts("cloud_usage_result")
        assert policy.usage_report(ledger)["unresolved_requests"] == 1


def test_transport_pins_endpoint_and_bearer_auth(monkeypatch):
    seen = []
    monkeypatch.setattr(client.bounded_http, "bounded_http_request", lambda *a, **kw: seen.append((a, kw)) or (200, {}, b"{}"))
    provider = client.OpenAIClient("synthetic-private")
    assert provider.request("POST", {}, 1, deadline=123)[0] == 200
    args, kwargs = seen[0]
    assert args == ("https://api.openai.com/v1/responses", "POST", {}, 1)
    assert kwargs == {"api_key": "synthetic-private", "deadline": 123,
                      "allowed_endpoints": (client.ENDPOINT,), "auth": "bearer",
                      "max_bytes": client.bounded_http.MAX_RESPONSE_BYTES}


@pytest.mark.parametrize("n", [100, 272000, 272001, policy.INPUT_RESERVATION_BOUND])
def test_prices_include_cached_reads_writes_and_long_context(n):
    u = usage(n=n)
    factor = 2 if n > 272000 else 1
    expected = ((Decimal(n - 50) * Decimal("0.1") + Decimal(20) * Decimal("0.01")
                 + Decimal(30) * Decimal("0.125")) * factor
                + Decimal(10) * (Decimal("0.75") if factor == 2 else Decimal("0.5"))) / 1000000
    assert policy.usage_cost(u, model=client.MODEL) == expected
    assert expected <= policy._reservation_cost(n, 10, model=client.MODEL)


def test_mixed_provider_budgets_keep_legacy_reserves_and_original_receipts(tmp_path, monkeypatch):
    now = datetime(2026, 10, 29, 12, tzinfo=policy.JST).timestamp()
    monkeypatch.setattr(policy.time, "time", lambda: now)
    budgets = {"anthropic": {"daily_cost_limit_usd": 20, "monthly_cost_limit_usd": 60},
               "openai": {"daily_cost_limit_usd": 5, "monthly_cost_limit_usd": 15}}
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "openai", "provider_budgets": budgets}}
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        reserve = policy.usage_reserver(ledger, cfg, route="patient_context", project_id=1, unit_key="synthetic")(
            client.OpenAIClient("synthetic").body("x" * 500, 100, None), 2)
        reserve_id = ledger.db.execute("SELECT max(artifact_id) FROM artifacts WHERE kind='cloud_usage_reserve'").fetchone()[0]
        meta = {"model": client.MODEL, "request_fp": reserve["request_fp"], "usage": usage(),
                "http_class": "http_200", "error_kind": None}
        policy.record_result(ledger, reserve, meta)
        report = policy.usage_report(ledger)
        assert not report["pricing_unverified"] and report["unresolved_requests"] == 0
        assert Decimal(report["monthly_cost_usd"]) == policy.usage_cost(usage(), model=client.MODEL)
        assert policy.reservation_evidence(ledger.db, reserve_id)["reservation"]["model"] == client.MODEL
        capped = {"cloud_llm": {**cfg["cloud_llm"], "provider_budgets": {**budgets, "openai": {
            "daily_cost_limit_usd": 0, "monthly_cost_limit_usd": 15}}}}
        assert policy.budget_block_reason(ledger, capped) == "provider_daily_cap"
        legacy = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
        policy.usage_reserver(ledger, legacy, route="patient_context", project_id=1, unit_key="legacy")(
            {"model": policy.MODEL, "max_tokens": 100, "system": "synthetic system",
             "messages": [{"role": "user", "content": "another synthetic prompt"}]}, 2)
        tally = policy._tally(ledger)
        assert tally.provider_totals["anthropic"] > 0 and tally.provider_totals["openai"] > 0
        assert policy.usage_report(ledger)["unresolved_requests"] == 1
        blocked = {"cloud_llm": {**legacy["cloud_llm"], "daily_cost_limit_usd": 0}}
        assert policy.budget_block_reason(ledger, blocked) == "daily_cost_cap"


def test_priority_uses_only_mcs_private_key_and_validates_split(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-process-not-allowed")
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "auto"}}
    assert policy.selected_key(tmp_path, cfg) is None
    path = tmp_path / ".env"
    path.write_text("MCS_OPENAI_API_KEY=synthetic-openai\n")
    path.chmod(0o600)
    settings, errors = policy.resolve_settings(cfg, tmp_path)
    assert not errors and settings["provider"] == "openai" and settings["model"] == client.MODEL
    path.write_text(path.read_text() + "MCS_ANTHROPIC_API_KEY=synthetic-anthropic\n")
    assert policy.resolve_settings(cfg, tmp_path)[0]["provider"] == "anthropic"
    cfg["cloud_llm"]["provider_order"] = ["openai", "anthropic"]
    assert policy.resolve_settings(cfg, tmp_path)[0]["provider"] == "openai"
    cfg["cloud_llm"]["provider_budgets"] = {"openai": {"daily_cost_limit_usd": 81, "monthly_cost_limit_usd": 15}}
    settings, errors = policy.resolve_settings(cfg, tmp_path)
    assert errors and settings["mode"] == "off"


def test_fixed_head_is_posted_as_its_own_cache_block_without_changing_request_identity():
    """The wire body marks the head; reservation and settlement keep the plain body."""
    head, tail = "synthetic fixed instructions\n", "synthetic varying input"
    posted, reserved, settled = [], [], []

    def post(body, timeout):
        posted.append(body)
        return 200, {"x-request-id": "req_synthetic"}, json.dumps(response()).encode()

    provider = client.OpenAIClient("synthetic-private", post_fn=post)
    plain = provider.body(head + tail, 100, head)
    provider.complete(head + tail, deadline=time.monotonic() + 10, max_tokens=100, cache_prefix=head,
        reserve_fn=lambda body, timeout: reserved.append(body) or {"id": 1},
        after_result=lambda reservation, meta, **kw: settled.append(meta))
    assert reserved == [plain] and plain["input"] == head + tail
    assert settled[0]["request_fp"] == payload_hash(plain)
    assert posted[0]["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert posted[0]["input"] == [{"role": "user", "content": [
        {"type": "input_text", "text": head, "prompt_cache_breakpoint": {"mode": "explicit"}},
        {"type": "input_text", "text": tail}]}]
    assert {key: value for key, value in posted[0].items() if key not in ("input", "prompt_cache_options")} == {
        key: value for key, value in plain.items() if key not in ("input", "prompt_cache_options")}
    # a prompt that is only the head carries no empty second block
    assert provider.wire(provider.body(head, 100, head), head)["input"][0]["content"] == [
        {"type": "input_text", "text": head, "prompt_cache_breakpoint": {"mode": "explicit"}}]


def _refusal_part_response():
    return response(output=[{"type": "message", "role": "assistant",
                             "content": [{"type": "refusal", "refusal": "synthetic refusal"}]}])


def test_refusal_content_part_is_a_provider_proven_refusal():
    instance = client.OpenAIClient('synthetic', model=client.MODEL, effort='low')
    with pytest.raises(AnthropicError) as caught:
        instance.answer(_refusal_part_response(), {'usage': None})
    assert (caught.value.kind, caught.value.detail) == ("contract_error", "refusal")
    # The resend stop reads exactly this detail: a refusal is never resent or counted.
    assert caught.value.detail in cloud_client.CONTRACT_ERROR_DETAILS


def test_incomplete_content_filter_is_a_provider_proven_refusal():
    instance = client.OpenAIClient('synthetic', model=client.MODEL, effort='low')
    filtered = response(status="incomplete", incomplete_details={"reason": "content_filter"})
    with pytest.raises(AnthropicError) as caught:
        instance.answer(filtered, {'usage': None})
    assert (caught.value.kind, caught.value.detail) == ("contract_error", "refusal")


@pytest.mark.parametrize("part", [
    {"type": "output_text"},                        # missing text: still unknown output
    {"type": "refusalx", "text": "synthetic"},      # look-alike type is not a proven refusal
    {"type": "image", "text": "synthetic"},         # unknown part stays resendable-unknown
    "not-a-dict",
])
def test_unproven_output_parts_stay_unexpected_output(part):
    instance = client.OpenAIClient('synthetic', model=client.MODEL, effort='low')
    unknown = response(output=[{"type": "message", "role": "assistant", "content": [part]}])
    with pytest.raises(AnthropicError) as caught:
        instance.answer(unknown, {'usage': None})
    assert (caught.value.kind, caught.value.detail) == ("contract_error", "unexpected_output")
    assert caught.value.detail not in cloud_client.CONTRACT_ERROR_DETAILS


@pytest.mark.parametrize("details", [
    {"reason": "max_output_tokens"},     # bounded length handling, never a refusal
    {"reason": "other_reason"},
    None,
])
def test_incomplete_without_content_filter_is_never_a_refusal(details):
    instance = client.OpenAIClient('synthetic', model=client.MODEL, effort='low')
    incomplete = response(status="incomplete", incomplete_details=details,
                          output=[{"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": ""}]}])
    try:
        instance.answer(incomplete, {'usage': None})
    except AnthropicError as error:
        assert error.detail != "refusal"
