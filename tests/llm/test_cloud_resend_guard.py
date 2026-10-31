"""Synthetic, provider-independent duplicate request reservation boundaries."""
from contextlib import closing
import json
import time
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


def _reserve(ledger, prompt, provider='anthropic', *, max_tokens=100, cache_prefix=None, unit='synthetic-unit', owner=None, message_id=1, route='semantic_backlog', selection=None):
    cfg = {'cloud_llm': {'mode': 'on', 'project_ids': [1], 'provider': provider}}
    client = (anthropic_client.AnthropicClient('synthetic') if provider == 'anthropic'
              else openai_client.OpenAIClient('synthetic', **({'model': selection['model'], 'effort': selection['effort']}
                                                             if selection is not None else {})))
    body = client.body(prompt, max_tokens, cache_prefix)
    return cloud_policy.usage_reserver(ledger, cfg, route=route, project_id=1,
                                      message_id=message_id, unit_key=unit, owner=owner, selection=selection)(body, 2)


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


@pytest.mark.parametrize('legacy', [False, True])
def test_patient_unknown_remains_blocked_across_source_and_night_changes(tmp_path, monkeypatch, legacy):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt', route='patient_context', unit='old-unit',
                               owner={'night_id': 'old', 'source_sig': 'old'}, message_id=1)
        cloud_policy.record_result(ledger, reservation, _result(reservation, 'timeout', 'timeout'))
        if legacy:
            with ledger.db:
                ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp') "
                                  "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')")
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt', 'openai', route='patient_context', unit='new-unit',
                     owner={'night_id': 'new', 'source_sig': 'new'}, message_id=1)
        assert caught.value.detail == 'cloud_unit_unresolved'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def test_patient_unknown_does_not_block_distinct_message(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        ledger.save_messages([_message(mid=2)])
        reservation = _reserve(ledger, 'synthetic prompt', route='patient_context', unit='old-unit',
                               owner={'night_id': 'old', 'source_sig': 'old'}, message_id=1)
        cloud_policy.record_result(ledger, reservation, _result(reservation, 'timeout', 'timeout'))
        _reserve(ledger, 'synthetic prompt', 'openai', route='patient_context', unit='new-unit',
                 owner={'night_id': 'new', 'source_sig': 'new'}, message_id=2)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_patient_settled_success_allows_distinct_model_and_different_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        previous = _reserve(ledger, 'synthetic prompt', route='patient_context', unit='old',
                            owner={'night_id': 'old', 'source_sig': 'old'})
        cloud_policy.record_result(ledger, previous, {
            **_result(previous, 'http_200', None), 'usage': {'input_tokens': 10, 'output_tokens': 2}})
        _reserve(ledger, 'synthetic prompt', 'openai', route='patient_context', unit='new',
                 owner={'night_id': 'new', 'source_sig': 'new'})
        _reserve(ledger, 'another synthetic prompt', 'openai', route='patient_context', unit='third')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 3


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
            _reserve(ledger, 'synthetic prompt')
        assert caught.value.detail == 'cloud_unit_unresolved'
        # A verified success may be deliberately reprocessed by another model.
        _reserve(ledger, 'synthetic prompt', 'openai')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


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


@pytest.mark.parametrize('change', ['source', 'phase', 'model'])
def test_distinct_semantic_unit_is_not_a_duplicate(tmp_path, monkeypatch, change):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        owner = {'semantic_target_id': 1, 'semantic_source': 'a' * 64,
                 'semantic_generation': 'generation', 'semantic_phase': 'facts'}
        previous = _reserve(ledger, 'synthetic prompt', unit='older-job', owner=owner)
        changed = dict(owner)
        provider = 'anthropic'
        if change == 'model':
            cloud_policy.record_result(ledger, previous, {**_result(previous, 'http_200', None),
                                       'usage': {'input_tokens': 10, 'output_tokens': 2}})
            provider = 'openai'
        else:
            changed['semantic_' + change] = 'b' * 64 if change == 'source' else 'summary'
        _reserve(ledger, 'synthetic prompt', provider, unit='newer-job', owner=changed)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def _medium_selection(route):
    purpose = 'patient_context' if route == 'patient_context' else 'backlog'
    cfg = {'cloud_llm': {'mode': 'on', 'project_ids': [1], 'provider': 'openai',
                         'profiles': {purpose: 'luna-medium'}}}
    selected, errors = cloud_policy.resolve_profile(cfg, route=route, purpose=purpose)
    assert not errors
    return selected


def _owner(generation='generation', target=1, source='a' * 64, phase='facts'):
    return {'semantic_target_id': target, 'semantic_thread_root': 1,
            'semantic_source': source, 'semantic_generation': generation, 'semantic_phase': phase}


def _openai_usage():
    return {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12,
            'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}


@pytest.mark.parametrize('route', ['reextract', 'semantic_backlog', 'patient_context'])
@pytest.mark.parametrize('legacy', [False, True])
def test_unknown_survives_generation_unit_and_effort_changes(tmp_path, monkeypatch, route, legacy):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        owner = _owner() if route == 'semantic_backlog' else {'night_id': 'old', 'source_sig': 'old'}
        old = _reserve(ledger, 'synthetic prompt', 'openai', route=route, unit='old', owner=owner)
        cloud_policy.record_result(ledger, old, _result(old, 'timeout', 'timeout'))
        if legacy:
            with ledger.db:
                ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp',"
                                  "'$.logical_unit_fp','$.logical_target_id') "
                                  "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')")
        selected = _medium_selection(route)
        owner = _owner('new-generation') if route == 'semantic_backlog' else {
            'night_id': 'new', 'source_sig': 'new'}
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt', 'openai', route=route, unit='new', owner=owner,
                     max_tokens=selected['max_tokens'], selection=selected)
        assert caught.value.detail == 'cloud_unit_unresolved'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


@pytest.mark.parametrize('route', ['reextract', 'semantic_backlog', 'patient_context'])
def test_validated_legacy_low_success_allows_same_model_medium(tmp_path, monkeypatch, route):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        owner = _owner() if route == 'semantic_backlog' else {'night_id': 'old', 'source_sig': 'old'}
        old = _reserve(ledger, 'synthetic prompt', 'openai', route=route, unit='old', owner=owner)
        cloud_policy.record_result(ledger, old, {**_result(old, 'http_200', None), 'usage': _openai_usage()})
        selected = _medium_selection(route)
        owner = _owner('new-generation') if route == 'semantic_backlog' else {
            'night_id': 'new', 'source_sig': 'new'}
        _reserve(ledger, 'synthetic prompt', 'openai', route=route, unit='new', owner=owner,
                 max_tokens=selected['max_tokens'], selection=selected)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_semantic_generation_change_unknown_never_posts_or_reserves_again(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    selected = _medium_selection('semantic_backlog')
    cfg = {'cloud_llm': {'mode': 'on', 'project_ids': [1], 'provider': 'openai'}}
    posts = []
    with closing(_db(tmp_path)) as ledger:
        old = _reserve(ledger, 'synthetic prompt', 'openai', unit='old', owner=_owner())
        cloud_policy.record_result(ledger, old, _result(old, 'timeout', 'timeout'))
        reserve = cloud_policy.usage_reserver(ledger, cfg, route='semantic_backlog', project_id=1,
            message_id=1, unit_key='new', owner=_owner('new-generation'), selection=selected)
        instance = openai_client.OpenAIClient('synthetic', model=selected['model'], effort=selected['effort'],
            post_fn=lambda *args: posts.append(True))
        with pytest.raises(anthropic_client.AnthropicError):
            instance.complete('synthetic prompt', deadline=time.monotonic() + 10,
                              max_tokens=selected['max_tokens'], reserve_fn=reserve)
        assert posts == [] and len(ledger.artifacts('cloud_usage_reserve')) == 1


def test_legacy_success_without_prompt_proof_cannot_authorize_model_change(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        old = _reserve(ledger, 'synthetic prompt', unit='old', owner=_owner())
        cloud_policy.record_result(ledger, old, {**_result(old, 'http_200', None),
                                                 'usage': {'input_tokens': 10, 'output_tokens': 2}})
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp') "
                              "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')")
        selected = _medium_selection('semantic_backlog')
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt', 'openai', unit='new', owner=_owner('new-generation'),
                     max_tokens=selected['max_tokens'], selection=selected)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def test_legacy_luna_success_exact_envelope_proves_prompt_for_sol_change(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    cfg = {'cloud_llm': {'mode': 'on', 'project_ids': [1], 'provider': 'openai',
                         'profiles': {'backlog': 'sol-medium'}}}
    selected, errors = cloud_policy.resolve_profile(cfg, route='semantic_backlog', purpose='backlog')
    assert not errors
    with closing(_db(tmp_path)) as ledger:
        old = _reserve(ledger, 'synthetic prompt', 'openai', unit='old', owner=_owner())
        cloud_policy.record_result(ledger, old, {**_result(old, 'http_200', None), 'usage': _openai_usage()})
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp') "
                              "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')")
        _reserve(ledger, 'synthetic prompt', 'openai', unit='new', owner=_owner('new-generation'),
                 max_tokens=selected['max_tokens'], selection=selected)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_unproved_legacy_low_cannot_authorize_effort_change(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        old = _reserve(ledger, 'synthetic prompt', 'openai', unit='old', owner=_owner())
        cloud_policy.record_result(ledger, old, {**_result(old, 'http_200', None), 'usage': _openai_usage()})
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.request_fp',?) "
                              "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')", ('0' * 64,))
        selected = _medium_selection('semantic_backlog')
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt', 'openai', unit='new', owner=_owner('new-generation'),
                     max_tokens=selected['max_tokens'], selection=selected)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def test_same_model_settled_medium_to_high_is_intentional_reprocessing(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    cfg = {'cloud_llm': {'mode': 'on', 'project_ids': [1], 'provider': 'openai'}}
    medium, errors = cloud_policy.resolve_profile(cfg, route='patient_context', purpose='patient_context')
    assert not errors
    high, errors = cloud_policy.resolve_profile(cfg, route='patient_context', purpose='quality_repair')
    assert not errors and medium['model'] == high['model']
    with closing(_db(tmp_path)) as ledger:
        old = _reserve(ledger, 'synthetic prompt', 'openai', route='patient_context', unit='old',
                       selection=medium, max_tokens=medium['max_tokens'])
        cloud_policy.record_result(ledger, old, {**_result(old, 'http_200', None), 'usage': _openai_usage()})
        _reserve(ledger, 'synthetic prompt', 'openai', route='patient_context', unit='new',
                 selection=high, max_tokens=high['max_tokens'])
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_token_limit_change_alone_cannot_authorize_resend(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    cfg = {'cloud_llm': {'mode': 'on', 'project_ids': [1], 'provider': 'openai', 'model': openai_client.MODEL}}
    selected, errors = cloud_policy.resolve_profile(cfg, route='reextract', purpose='backlog')
    assert not errors and selected['effort'] == 'low'
    with closing(_db(tmp_path)) as ledger:
        old = _reserve(ledger, 'synthetic prompt', 'openai', route='reextract', unit='old')
        cloud_policy.record_result(ledger, old, {**_result(old, 'http_200', None), 'usage': _openai_usage()})
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt', 'openai', route='reextract', unit='new',
                     selection=selected, max_tokens=selected['max_tokens'])
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def test_discarded_settled_result_cannot_authorize_effort_change(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        old = _reserve(ledger, 'synthetic prompt', 'openai', unit='old', owner=_owner())
        cloud_policy.record_result(ledger, old, {**_result(old, 'http_200', None), 'usage': _openai_usage()})
        with ledger.db:
            ledger.db.execute('BEGIN')
            cloud_policy.record_disposition_tx(ledger, old, 'discarded_scope')
        selected = _medium_selection('semantic_backlog')
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt', 'openai', unit='new', owner=_owner('new-generation'),
                     selection=selected, max_tokens=selected['max_tokens'])
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def test_semantic_owner_without_matching_unit_fingerprint_cannot_bypass_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        _reserve(ledger, 'synthetic prompt', unit='old', owner=_owner())
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.owner.semantic_source',?) "
                              "WHERE kind='cloud_usage_reserve'", ('b' * 64,))
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt', unit='new', owner=_owner('new-generation'))
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def test_semantic_distinct_bound_target_without_generation_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        ledger.save_messages([_message(mid=2, parent=1)])
        _reserve(ledger, 'synthetic prompt', unit='old', message_id=1,
                 owner={'semantic_target_id': 1, 'semantic_thread_root': 1})
        _reserve(ledger, 'synthetic prompt', unit='new', message_id=2,
                 owner={'semantic_target_id': 2, 'semantic_thread_root': 1})
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


@pytest.mark.parametrize('corrupt_target', [True, 2, None])
def test_semantic_unverified_prior_target_cannot_bypass_unknown(tmp_path, monkeypatch, corrupt_target):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        ledger.save_messages([_message(mid=2, parent=1)])
        _reserve(ledger, 'synthetic prompt', unit='old', message_id=1,
                 owner={'semantic_target_id': 1, 'semantic_thread_root': 1})
        with ledger.db:
            row = ledger.db.execute("SELECT artifact_id, meta FROM artifacts WHERE kind='cloud_usage_reserve'").fetchone()
            meta = json.loads(row['meta'])
            meta['owner']['semantic_target_id'] = corrupt_target
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), row['artifact_id']))
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt', unit='new', message_id=2,
                     owner={'semantic_target_id': 2, 'semantic_thread_root': 1})
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def test_semantic_distinct_verified_target_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        ledger.save_messages([_message(mid=2, parent=1)])
        _reserve(ledger, 'synthetic prompt', unit='old', owner=_owner())
        _reserve(ledger, 'synthetic prompt', unit='new', owner=_owner(target=2), message_id=2)
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
        # Without a logical prompt binding, envelope inequality cannot prove
        # that only the prompt changed rather than model or request settings.
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'a different prompt', unit='newer-job')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


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


@pytest.mark.parametrize('diagnostic', [
    'rate_limit_exceeded', 'slow_down', 'rate_limit_error', 'missing_code', 'unknown_code',
    'unknown_type', 'conflicting_fields', 'invalid_field_type', 'body_not_bytes',
    'body_too_large', 'body_invalid_utf8', 'body_invalid_json', 'body_duplicate_keys',
    'body_nonfinite', 'body_too_deep', 'body_not_object', 'error_missing', 'error_not_object',
])
def test_openai_rate_diagnostic_keeps_reserved_maximum_and_existing_retry(tmp_path, monkeypatch, diagnostic):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt', 'openai')
        result = {**_result(reservation, 'http_429', 'rate_limited'),
                  'openai_429_diagnostic': diagnostic, 'body': 'synthetic-secret-canary'}
        cloud_policy.record_result(ledger, reservation, result)
        stored = ledger.artifacts('cloud_usage_result')[0]
        meta = json.loads(stored['meta'])
        assert meta['openai_429_diagnostic'] == diagnostic
        assert meta['cost_usd'] == reservation['max_cost_usd']
        assert 'synthetic-secret-canary' not in json.dumps(dict(stored))
        report = cloud_policy.usage_report(ledger)
        assert report['unresolved_requests'] == 1 and not report['pricing_unverified']
        _reserve(ledger, 'synthetic prompt', 'openai')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


@pytest.mark.parametrize('changes,remove', [
    ({'openai_429_diagnostic': 'synthetic-secret-canary'}, ()),
    ({'openai_429_diagnostic': ['slow_down']}, ()),
    ({'openai_429_diagnostic': None}, ()),
    ({'http_class': 'http_500'}, ()),
    ({'error_kind': 'timeout'}, ()),
    ({'model': anthropic_client.MODEL}, ()),
    ({'usage': {'input_tokens': 1}}, ()),
    ({}, ('usage',)),
    ({'openai_error_code': 'insufficient_quota'}, ()),
])
def test_invalid_openai_rate_diagnostic_is_not_saved(tmp_path, monkeypatch, changes, remove):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt', 'openai')
        result = {**_result(reservation, 'http_429', 'rate_limited'), 'openai_429_diagnostic': 'slow_down'}
        result.update(changes)
        for key in remove:
            result.pop(key)
        with pytest.raises(cloud_policy.AnthropicError):
            cloud_policy.record_result(ledger, reservation, result)
        assert not ledger.artifacts('cloud_usage_result')


def test_anthropic_result_cannot_carry_openai_rate_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt', 'anthropic')
        result = {**_result(reservation, 'http_429', 'rate_limited'), 'openai_429_diagnostic': 'slow_down'}
        with pytest.raises(cloud_policy.AnthropicError):
            cloud_policy.record_result(ledger, reservation, result)
        assert not ledger.artifacts('cloud_usage_result')


@pytest.mark.parametrize('bad', ['synthetic-secret-canary', None, ['slow_down']])
def test_corrupt_stored_openai_rate_diagnostic_cannot_authorize_retry(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt', 'openai')
        cloud_policy.record_result(ledger, reservation, {
            **_result(reservation, 'http_429', 'rate_limited'), 'openai_429_diagnostic': 'slow_down'})
        row = ledger.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='cloud_usage_result'").fetchone()
        meta = json.loads(row['meta'])
        meta['openai_429_diagnostic'] = bad
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), row['artifact_id']))
        assert cloud_policy.usage_report(ledger)['pricing_unverified'] is True
        with pytest.raises(cloud_policy.AnthropicError):
            _reserve(ledger, 'synthetic prompt', 'openai')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def test_conflicting_result_records_pricing_stop_and_keeps_maximum(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        first = {**_result(reservation, 'http_200', None), 'usage': {'input_tokens': 10, 'output_tokens': 2}}
        cloud_policy.record_result(ledger, reservation, first)
        conflict = {**first, 'usage': {'input_tokens': 11, 'output_tokens': 2}}
        with pytest.raises(cloud_policy.AnthropicError) as error:
            cloud_policy.record_result(ledger, reservation, conflict)
        assert error.value.kind == 'pricing_unverified'
        assert len(ledger.artifacts('cloud_usage_result')) == 2
        report = cloud_policy.usage_report(ledger)
        assert report['pricing_unverified'] and report['unresolved_requests'] == 1
        from decimal import Decimal
        assert Decimal(report['monthly_cost_usd']) == Decimal(reservation['max_cost_usd'])
        assert cloud_policy.global_block_reason(ledger) == 'pricing_unverified'
        with pytest.raises(cloud_policy.AnthropicError):
            _reserve(ledger, 'synthetic prompt', 'openai')


def test_success_after_terminal_result_does_not_clear_circuit(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        terminal = _reserve(ledger, 'synthetic first prompt', unit='first')
        paid = _reserve(ledger, 'synthetic second prompt', unit='second')
        cloud_policy.record_result(ledger, terminal, _result(terminal, 'http_401', 'auth_error'))
        before = [dict(row) for row in ledger.artifacts('cloud_circuit')]
        assert cloud_policy.global_block_reason(ledger) == 'auth_error'
        success = {**_result(paid, 'http_200', None), 'usage': {'input_tokens': 10, 'output_tokens': 2}}
        cloud_policy.record_result(ledger, paid, success)
        assert len(ledger.artifacts('cloud_usage_result')) == 2
        assert [dict(row) for row in ledger.artifacts('cloud_circuit')] == before
        assert cloud_policy.global_block_reason(ledger) == 'auth_error'
        with pytest.raises(cloud_policy.AnthropicError):
            _reserve(ledger, 'synthetic third prompt', unit='third')


@pytest.mark.parametrize('corrupt_kind', ['cloud_usage_reserve', 'cloud_usage_result'])
def test_corrupt_stored_accounting_forces_pricing_stop_without_overwrite(tmp_path, monkeypatch, corrupt_kind):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        metadata = {**_result(reservation, 'http_200', None), 'usage': {'input_tokens': 10, 'output_tokens': 2}}
        if corrupt_kind == 'cloud_usage_result':
            cloud_policy.record_result(ledger, reservation, metadata)
        row = ledger.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=?', (corrupt_kind,)).fetchone()
        corrupt = '{"reservation_id":"synthetic-duplicate",' + row['meta'][1:]
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (corrupt, row['artifact_id']))
        expected_results = 1 if corrupt_kind == 'cloud_usage_result' else 0
        with pytest.raises(cloud_policy.AnthropicError) as error:
            cloud_policy.record_result(ledger, reservation, metadata)
        assert error.value.kind == 'pricing_unverified'
        assert ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (row['artifact_id'],)).fetchone()[0] == corrupt
        assert len(ledger.artifacts('cloud_usage_result')) == expected_results
        assert cloud_policy.global_block_reason(ledger) == 'pricing_unverified'
        assert cloud_policy.usage_report(ledger)['pricing_unverified'] is True
        with pytest.raises(cloud_policy.AnthropicError):
            _reserve(ledger, 'synthetic next prompt', unit='next')
