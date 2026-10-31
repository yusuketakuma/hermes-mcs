"""Synthetic, provider-independent duplicate request reservation boundaries."""
from contextlib import closing
import json
import uuid

import pytest

import anthropic_client
import cloud_policy
import mcs_requests
import mcs_util
import openai_client
from semantic_testkit import _ledger, _message, _patient


def _db(tmp_path):
    ledger = _ledger(tmp_path)
    _patient(ledger)
    ledger.save_messages([_message()])
    return ledger


def _reserve(ledger, prompt, provider='anthropic', *, max_tokens=100, cache_prefix=None, unit='synthetic-unit', owner=None, message_id=1):
    cfg = {'cloud_llm': {'mode': 'on', 'project_ids': [1], 'provider': provider}}
    client = (anthropic_client.AnthropicClient('synthetic') if provider == 'anthropic'
              else openai_client.OpenAIClient('synthetic'))
    body = client.body(prompt, max_tokens, cache_prefix)
    return cloud_policy.usage_reserver(ledger, cfg, route='semantic_backlog', project_id=1,
                                      message_id=message_id, unit_key=unit, owner=owner)(body, 2)


def _result(reservation, status, error):
    return {'model': reservation['model'], 'request_fp': reservation['request_fp'],
            'usage': None, 'http_class': status, 'error_kind': error}


@pytest.mark.parametrize('provider', ['anthropic', 'openai'])
def test_unknown_attempt_blocks_same_prompt_even_after_provider_switch(tmp_path, monkeypatch, provider):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        cloud_policy.record_result(ledger, reservation, _result(reservation, 'timeout', 'timeout'))
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt', provider)
        assert caught.value.detail == 'cloud_unit_unresolved'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1
        assert cloud_policy.usage_report(ledger)['unresolved_requests'] == 1


@pytest.mark.parametrize('status,error', [('not_sent', None), ('http_429', 'rate_limited')])
def test_definitive_refusal_allows_retry_without_erasing_accounting(tmp_path, monkeypatch, status, error):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        cloud_policy.record_result(ledger, reservation, _result(reservation, status, error))
        _reserve(ledger, 'synthetic prompt')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2
        assert len(ledger.artifacts('cloud_usage_result')) == 1


def test_openai_verified_quota_retries_only_after_receipted_human_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    monkeypatch.setattr(cloud_policy, 'selected_key', lambda *args, **kwargs: 'synthetic')
    monkeypatch.setattr(mcs_util, 'HOME', str(tmp_path))
    (tmp_path / 'config.json').write_text(json.dumps({
        'mcs_login_id': 'synthetic', 'notify_target': 'synthetic',
        'cloud_llm': {'mode': 'on', 'project_ids': [1], 'provider': 'openai'},
    }))
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt', 'openai')
        result = {**_result(reservation, 'http_429', 'payment_required'),
                  'openai_error_code': 'project_spend_limit_exceeded'}
        cloud_policy.record_result(ledger, reservation, result)
        usage = cloud_policy.usage_report(ledger)
        assert usage['unresolved_requests'] == 1
        assert usage['max_reserved_cost_usd'] == reservation['max_cost_usd']
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt', 'openai')
        req = {'version': 1, 'cmd': 'ops.cloud_resume', 'command_id': str(uuid.uuid4()),
               'actor': 'synthetic human', 'human_confirmed': False,
               'project_id': None, 'reason': 'synthetic approval'}
        denied = mcs_requests.apply_command(ledger, req)
        assert denied['outcome'] == 'rejected' and denied['error'] == 'human_confirmation_required'
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt', 'openai')
        req.update(command_id=str(uuid.uuid4()), human_confirmed=True)
        receipt = mcs_requests.apply_command(ledger, req)
        assert receipt['outcome'] == 'applied'
        _reserve(ledger, 'synthetic prompt', 'openai')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2
        assert cloud_policy.usage_report(ledger)['unresolved_requests'] == 2


@pytest.mark.parametrize('status,error', [('http_429', 'payment_required'),
                                          ('http_402', 'payment_required'),
                                          ('transport', 'transport')])
def test_old_unconfirmed_receipt_still_blocks_after_resume(tmp_path, monkeypatch, status, error):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt', 'openai')
        cloud_policy.record_result(ledger, reservation, _result(reservation, status, error))
        cloud_policy.resume(ledger)
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt', 'openai')
        assert caught.value.detail == 'cloud_unit_unresolved'


@pytest.mark.parametrize('change', ['marker_false', 'marker_integer', 'marker_string',
                                    'wrong_status', 'wrong_kind', 'wrong_provider', 'usage_missing'])
def test_corrupt_quota_marker_cannot_prove_retry(tmp_path, monkeypatch, change):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt', 'openai')
        cloud_policy.record_result(ledger, reservation, {
            **_result(reservation, 'http_429', 'payment_required'),
            'openai_error_code': 'project_spend_limit_exceeded'})
        expressions = {'marker_false': "'$.openai_error_code',json('false')",
                       'marker_integer': "'$.openai_error_code',1",
                       'marker_string': "'$.openai_error_code','synthetic_unknown'",
                       'wrong_status': "'$.http_class','http_402'",
                       'wrong_kind': "'$.error_kind','rate_limited'",
                       'wrong_provider': "'$.model','claude-haiku-5-5'"}
        with ledger.db:
            if change == 'usage_missing':
                ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.usage') "
                                  "WHERE kind='cloud_usage_result'")
            else:
                ledger.db.execute("UPDATE artifacts SET meta=json_set(meta," + expressions[change] + ") "
                                  "WHERE kind='cloud_usage_result'")
        assert cloud_policy.usage_report(ledger)['pricing_unverified'] is True
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt', 'openai')


def test_unknown_attempt_does_not_block_another_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        _reserve(ledger, 'synthetic first chunk')
        _reserve(ledger, 'synthetic different chunk')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_accounted_success_does_not_resend_an_identical_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        result = {**_result(reservation, 'http_200', None),
                  'usage': {'input_tokens': 10, 'output_tokens': 2}}
        cloud_policy.record_result(ledger, reservation, result)
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt', 'openai')
        assert caught.value.detail == 'cloud_unit_unresolved'


def test_corrupt_reservation_cannot_hide_its_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        _reserve(ledger, 'synthetic prompt')
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=? WHERE kind='cloud_usage_reserve'",
                              ('{"unit_key":"other","unit_key":"synthetic-unit"}',))
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt')
        assert caught.value.detail == 'cloud_unit_unresolved'


@pytest.mark.parametrize('change', ['tokens', 'cache', 'provider'])
def test_legacy_unknown_envelope_never_proves_a_distinct_prompt(tmp_path, monkeypatch, change):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        _reserve(ledger, 'synthetic prompt')
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp') "
                              "WHERE kind='cloud_usage_reserve'")
        options = ({'max_tokens': 101} if change == 'tokens' else
                   {'cache_prefix': 'synthetic'} if change == 'cache' else {'provider': 'openai'})
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt', **options)
        assert caught.value.detail == 'cloud_unit_unresolved'


@pytest.mark.parametrize('status,error', [('not_sent', None), ('http_429', 'rate_limited')])
def test_old_definitive_refusal_remains_retryable_without_losing_accounting(tmp_path, monkeypatch, status, error):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        cloud_policy.record_result(ledger, reservation, _result(reservation, status, error))
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET created_at=created_at-? "
                              "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')", (70 * 86400,))
        _reserve(ledger, 'synthetic prompt')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_actual_reservation_lookup_with_many_other_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        ledger.save_messages([_message(2, parent=1, body='synthetic reply')])
        owner = {'semantic_target_id': 2, 'semantic_thread_root': 1, 'semantic_source': 'source',
                 'semantic_generation': 'generation', 'semantic_phase': 'facts'}
        _reserve(ledger, 'other synthetic prompt', unit='other', owner=owner, message_id=2)
        template = ledger.db.execute("SELECT content,model,meta FROM artifacts "
                                     "WHERE kind='cloud_usage_reserve' AND message_id=2").fetchone()
        with ledger.db:
            ledger.db.executemany(
                "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                "VALUES('cloud_usage_reserve',1,2,?,?,json_set(?,'$.reservation_id',printf('%032x',?)),0)",
                [(template['content'], template['model'], template['meta'], n + 100) for n in range(2000)])
        statements, ticks = [], [0]
        ledger.db.set_trace_callback(statements.append)
        ledger.db.set_progress_handler(lambda: ticks.__setitem__(0, ticks[0] + 1) or 0, 1000)
        try:
            assert _reserve(ledger, 'synthetic prompt')
        finally:
            ledger.db.set_trace_callback(None)
            ledger.db.set_progress_handler(None, 0)
        actual = [sql for sql in statements if sql.startswith(
            'SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts')
            and 'json_tree(meta)' in sql]
        assert len(actual) == 1
        plan = ledger.db.execute('EXPLAIN QUERY PLAN ' + actual[0]).fetchall()
        assert any('idx_artifacts_lookup' in row[3] for row in plan)
        # A bounded fixture checks the real path, including its current
        # project-wide fallback, for accidental superlinear work.
        assert ticks[0] < 2000


def test_semantic_retry_across_job_units_does_not_repeat_paid_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        owner = {'semantic_target_id': 1, 'semantic_source': 'source',
                 'semantic_generation': 'generation', 'semantic_phase': 'facts'}
        reservation = _reserve(ledger, 'synthetic prompt', unit='older-job', owner=owner)
        cloud_policy.record_result(ledger, reservation, _result(reservation, 'timeout', 'timeout'))
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt', unit='newer-job', owner=owner)
        assert caught.value.detail == 'cloud_unit_unresolved'
        _reserve(ledger, 'a different synthetic chunk', unit='newer-job', owner=owner)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


@pytest.mark.parametrize('change', ['source', 'generation', 'phase', 'model'])
def test_distinct_semantic_unit_is_not_a_duplicate(tmp_path, monkeypatch, change):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        owner = {'semantic_target_id': 1, 'semantic_source': 'source',
                 'semantic_generation': 'generation', 'semantic_phase': 'facts'}
        previous = _reserve(ledger, 'synthetic prompt', unit='older-job', owner=owner)
        changed = dict(owner)
        provider = 'anthropic'
        if change == 'model':
            cloud_policy.record_result(ledger, previous, {**_result(previous, 'http_200', None),
                                       'usage': {'input_tokens': 10, 'output_tokens': 2}})
            provider = 'openai'
        else:
            changed['semantic_' + change] += '-new'
        _reserve(ledger, 'synthetic prompt', provider, unit='newer-job', owner=changed)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_unknown_semantic_unit_blocks_provider_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        owner = {'semantic_target_id': 1, 'semantic_source': 'source',
                 'semantic_generation': 'generation', 'semantic_phase': 'facts'}
        _reserve(ledger, 'synthetic prompt', unit='old', owner=owner)
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt', 'openai', unit='new', owner=owner)


def test_legacy_exact_envelope_remains_held_across_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        _reserve(ledger, 'synthetic prompt', unit='older-job')
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp',"
                              "'$.logical_unit_fp','$.logical_target_id') WHERE kind='cloud_usage_reserve'")
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt', unit='newer-job')
        _reserve(ledger, 'a different prompt', unit='newer-job')


def test_legacy_thread_root_reservation_blocks_same_reply_envelope(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        ledger.save_messages([_message(2, parent=1, body='synthetic reply')])
        _reserve(ledger, 'synthetic prompt', unit='older-job')
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp',"
                              "'$.logical_unit_fp','$.logical_target_id') WHERE kind='cloud_usage_reserve'")
        owner = {'semantic_target_id': 2, 'semantic_thread_root': 1, 'semantic_source': 'source',
                 'semantic_generation': 'generation', 'semantic_phase': 'facts'}
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt', unit='newer-job', owner=owner, message_id=2)
