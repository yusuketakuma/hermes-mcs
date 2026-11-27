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


@pytest.fixture(autouse=True)
def _open_send_routes(monkeypatch):
    """Exercise reservation-guard mechanics on every configured route.

    Individual cloud routes accept no new sends in production; the guard's
    per-route record interpretation stays as accounting safety code. Closed
    send policy itself is covered by test_cloud_context_only.py."""
    monkeypatch.setattr(cloud_policy, 'send_route', lambda route: True)


def _db(tmp_path):
    ledger = _ledger(tmp_path)
    _patient(ledger)
    ledger.save_messages([_message()])
    return ledger


def _reserve(ledger, prompt, provider='anthropic', *, max_tokens=100, cache_prefix=None, unit='synthetic-unit', owner=None, message_id=1, route='semantic_backlog', selection=None):
    cfg = {'cloud_llm': {'mode': 'on', 'project_ids': [1], 'provider': provider}}
    client_type = anthropic_client.AnthropicClient if provider == 'anthropic' else openai_client.OpenAIClient
    client = client_type('synthetic', **({'model': selection['model'], 'effort': selection['effort']}
                                         if selection is not None else {}))
    body = client.body(prompt, max_tokens, cache_prefix)
    return cloud_policy.usage_reserver(ledger, cfg, route=route, project_id=1,
                                      message_id=message_id, unit_key=unit, owner=owner, selection=selection)(body, 2)


def _result(reservation, status, error):
    return {'model': reservation['model'], 'request_fp': reservation['request_fp'],
            'usage': None, 'http_class': status, 'error_kind': error}


@pytest.mark.parametrize('provider', ['anthropic', 'openai'])
def test_transport_failure_retries_same_logical_prompt_even_after_provider_switch(tmp_path, monkeypatch, provider):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        cloud_policy.record_result(ledger, reservation, _result(reservation, 'timeout', 'timeout'))
        _reserve(ledger, 'synthetic prompt', provider)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2
        assert cloud_policy.usage_report(ledger)['unresolved_requests'] == 2


@pytest.mark.parametrize('legacy', [False, True])
def test_patient_transport_failure_retries_only_when_the_logical_prompt_is_verified(tmp_path, monkeypatch, legacy):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt', route='patient_context', unit='old-unit',
                               owner={'workday_id': 'old', 'source_sig': 'old'}, message_id=1)
        cloud_policy.record_result(ledger, reservation, _result(reservation, 'timeout', 'timeout'))
        if legacy:
            with ledger.db:
                ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp') "
                                  "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')")
        def retry():
            return _reserve(ledger, 'synthetic prompt', 'openai', route='patient_context', unit='new-unit',
                            owner={'workday_id': 'new', 'source_sig': 'new'}, message_id=1)
        if legacy:
            with pytest.raises(anthropic_client.AnthropicError) as caught:
                retry()
            assert caught.value.detail == 'cloud_unit_unresolved'
            assert len(ledger.artifacts('cloud_usage_reserve')) == 1
        else:
            retry()
            assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_patient_unknown_does_not_block_distinct_message(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        ledger.save_messages([_message(mid=2)])
        reservation = _reserve(ledger, 'synthetic prompt', route='patient_context', unit='old-unit',
                               owner={'workday_id': 'old', 'source_sig': 'old'}, message_id=1)
        cloud_policy.record_result(ledger, reservation, _result(reservation, 'timeout', 'timeout'))
        _reserve(ledger, 'synthetic prompt', 'openai', route='patient_context', unit='new-unit',
                 owner={'workday_id': 'new', 'source_sig': 'new'}, message_id=2)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_patient_settled_success_allows_distinct_model_and_different_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        previous = _reserve(ledger, 'synthetic prompt', route='patient_context', unit='old',
                            owner={'workday_id': 'old', 'source_sig': 'old'})
        cloud_policy.record_result(ledger, previous, {
            **_result(previous, 'http_200', None), 'usage': {'input_tokens': 10, 'output_tokens': 2}})
        _reserve(ledger, 'synthetic prompt', 'openai', route='patient_context', unit='new',
                 owner={'workday_id': 'new', 'source_sig': 'new'})
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
                                          ('http_402', 'payment_required')])
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


def test_semantic_retry_across_job_units_uses_the_same_logical_failure_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        owner = {'semantic_target_id': 1, 'semantic_source': 'source',
                 'semantic_generation': 'generation', 'semantic_phase': 'facts'}
        reservation = _reserve(ledger, 'synthetic prompt', unit='older-job', owner=owner)
        cloud_policy.record_result(ledger, reservation, _result(reservation, 'timeout', 'timeout'))
        _reserve(ledger, 'synthetic prompt', unit='newer-job', owner=owner)
        _reserve(ledger, 'a different synthetic chunk', unit='newer-job', owner=owner)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 3


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
def test_transport_failure_survives_generation_unit_and_effort_changes_with_prompt_proof(
        tmp_path, monkeypatch, route, legacy):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        owner = _owner() if route == 'semantic_backlog' else {'workday_id': 'old', 'source_sig': 'old'}
        old = _reserve(ledger, 'synthetic prompt', 'openai', route=route, unit='old', owner=owner)
        cloud_policy.record_result(ledger, old, _result(old, 'timeout', 'timeout'))
        if legacy:
            with ledger.db:
                ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp',"
                                  "'$.logical_unit_fp','$.logical_target_id') "
                                  "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')")
        selected = _medium_selection(route)
        owner = _owner('new-generation') if route == 'semantic_backlog' else {
            'workday_id': 'new', 'source_sig': 'new'}
        _reserve(ledger, 'synthetic prompt', 'openai', route=route, unit='new', owner=owner,
                 max_tokens=selected['max_tokens'], selection=selected)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


@pytest.mark.parametrize('route', ['reextract', 'semantic_backlog', 'patient_context'])
def test_validated_legacy_low_success_allows_same_model_medium(tmp_path, monkeypatch, route):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        owner = _owner() if route == 'semantic_backlog' else {'workday_id': 'old', 'source_sig': 'old'}
        old = _reserve(ledger, 'synthetic prompt', 'openai', route=route, unit='old', owner=owner)
        cloud_policy.record_result(ledger, old, {**_result(old, 'http_200', None), 'usage': _openai_usage()})
        selected = _medium_selection(route)
        owner = _owner('new-generation') if route == 'semantic_backlog' else {
            'workday_id': 'new', 'source_sig': 'new'}
        _reserve(ledger, 'synthetic prompt', 'openai', route=route, unit='new', owner=owner,
                 max_tokens=selected['max_tokens'], selection=selected)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_semantic_generation_change_transport_failure_retries_with_a_new_reservation(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    selected = _medium_selection('semantic_backlog')
    cfg = {'cloud_llm': {'mode': 'on', 'project_ids': [1], 'provider': 'openai'}}
    posts = []

    def post(*args):
        posts.append(True)
        raise TimeoutError('synthetic')

    with closing(_db(tmp_path)) as ledger:
        old = _reserve(ledger, 'synthetic prompt', 'openai', unit='old', owner=_owner())
        cloud_policy.record_result(ledger, old, _result(old, 'timeout', 'timeout'))
        reserve = cloud_policy.usage_reserver(ledger, cfg, route='semantic_backlog', project_id=1,
            message_id=1, unit_key='new', owner=_owner('new-generation'), selection=selected)
        instance = openai_client.OpenAIClient('synthetic', model=selected['model'], effort=selected['effort'],
            max_attempts=1, post_fn=post)
        with pytest.raises(anthropic_client.AnthropicError):
            instance.complete('synthetic prompt', deadline=time.monotonic() + 10,
                              max_tokens=selected['max_tokens'], reserve_fn=reserve)
        assert posts == [True] and len(ledger.artifacts('cloud_usage_reserve')) == 2


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
            _reserve(ledger, 'synthetic prompt', 'openai', unit='new', owner=_owner(),
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
            _reserve(ledger, 'synthetic prompt', 'openai', unit='new', owner=_owner(),
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
            _reserve(ledger, 'synthetic prompt', 'openai', unit='new', owner=_owner(),
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
            _reserve(ledger, 'synthetic prompt', unit='new', owner=_owner())
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


def _refusal_of(ledger, prompt):
    with pytest.raises(anthropic_client.AnthropicError) as caught:
        _reserve(ledger, prompt)
    assert (caught.value.kind, caught.value.detail) == ('budget_exceeded', 'cloud_unit_unresolved')
    return getattr(caught.value, 'history_refusal', None)


@pytest.mark.parametrize('status,error,usage,expected,failed_sends', [
    ('timeout', 'timeout', None, 'repeat_unsettled', 6),
    ('http_200', None, {'input_tokens': 10, 'output_tokens': 2}, 'repeat_success', 1),
])
def test_repeat_refusal_carries_its_fixed_class_without_changing_the_refusal(
        tmp_path, monkeypatch, status, error, usage, expected, failed_sends):
    """The guard's refusal keeps its kind and detail; the class only tells which
    validated branch refused. Retryable transport loss refuses after the shared cap."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        for _ in range(failed_sends):
            reservation = _reserve(ledger, 'synthetic prompt')
            cloud_policy.record_result(ledger, reservation, {**_result(reservation, status, error), 'usage': usage})
        assert _refusal_of(ledger, 'synthetic prompt') == expected
        assert len(ledger.artifacts('cloud_usage_reserve')) == failed_sends


def test_a_settled_paid_failure_allows_a_new_billed_resend(tmp_path, monkeypatch):
    """A settled failure left the unit unanswered: resending is a separate billed
    attempt, not a duplicate. A success still refuses another send."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        cloud_policy.record_result(ledger, reservation, {**_result(reservation, 'http_200', 'contract_error'),
                                                         'usage': {'input_tokens': 10, 'output_tokens': 2}})
        _reserve(ledger, 'synthetic prompt')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def _settled_failure(ledger, prompt='synthetic prompt'):
    reservation = _reserve(ledger, prompt)
    cloud_policy.record_result(ledger, reservation, {
        **_result(reservation, 'http_200', 'contract_error'),
        'usage': {'input_tokens': 10, 'output_tokens': 2}})
    return reservation


def test_settled_failure_resends_stop_after_five_retries(tmp_path, monkeypatch):
    """The same logical request bills the first send plus at most five settled-failure
    retries; the seventh verified repeat refuses."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        for _ in range(5):
            _settled_failure(ledger)
        sixth = _reserve(ledger, 'synthetic prompt')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 6
        cloud_policy.record_result(ledger, sixth, {
            **_result(sixth, 'http_200', 'contract_error'),
            'usage': {'input_tokens': 10, 'output_tokens': 2}})
        assert _refusal_of(ledger, 'synthetic prompt') == 'repeat_failed'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 6


@pytest.mark.parametrize('status,error', [
    ('transport', 'transport'), ('timeout', 'timeout'),
    ('http_529', 'transport'), ('http_429', 'rate_limited')])
def test_cloud_send_failures_share_the_first_plus_five_retry_cap(tmp_path, monkeypatch, status, error):
    """Transport loss, provider 5xx and rate limiting all consume the same bounded sends."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        for _ in range(5):
            reservation = _reserve(ledger, 'synthetic prompt')
            cloud_policy.record_result(ledger, reservation, _result(reservation, status, error))
        sixth = _reserve(ledger, 'synthetic prompt')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 6
        cloud_policy.record_result(ledger, sixth, _result(sixth, status, error))
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 6


def test_guarded_transport_retries_reserve_and_record_every_send(tmp_path, monkeypatch):
    """The real client/reserver path reaches first send plus five retries without hiding cost."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    posts = []

    def post(*args):
        posts.append(True)
        raise TimeoutError('synthetic')

    with closing(_db(tmp_path)) as ledger:
        cfg = {'cloud_llm': {'mode': 'on', 'project_ids': [1], 'provider': 'anthropic'}}
        reserve = cloud_policy.usage_reserver(ledger, cfg, route='semantic_backlog', project_id=1,
                                              message_id=1, unit_key='synthetic-unit')
        client = anthropic_client.AnthropicClient('synthetic', post_fn=post, wait_fn=lambda delay: None)
        with pytest.raises(anthropic_client.AnthropicError, match='timeout'):
            client.complete('synthetic prompt', deadline=time.monotonic() + 60, reserve_fn=reserve,
                            after_result=lambda reservation, meta, **_answer: cloud_policy.record_result(
                                ledger, reservation, meta))
        assert len(posts) == 6
        assert len(ledger.artifacts('cloud_usage_reserve')) == 6
        assert len(ledger.artifacts('cloud_usage_result')) == 6


def test_a_success_in_the_failure_history_still_refuses(tmp_path, monkeypatch):
    """Even below the retry cap, one verified successful answer remains reusable evidence."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        _settled_failure(ledger)
        other = _reserve(ledger, 'synthetic prompt')
        cloud_policy.record_result(ledger, other, {
            **_result(other, 'http_200', None), 'usage': {'input_tokens': 10, 'output_tokens': 2}})
        assert _refusal_of(ledger, 'synthetic prompt') == 'repeat_success'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_mixed_failure_kinds_share_one_retry_cap(tmp_path, monkeypatch):
    """Paid contract failures and transport loss consume the same logical request budget."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        _settled_failure(ledger)
        second = _reserve(ledger, 'synthetic prompt')
        cloud_policy.record_result(ledger, second, _result(second, 'timeout', 'timeout'))
        for _ in range(4):
            reservation = _reserve(ledger, 'synthetic prompt')
            cloud_policy.record_result(ledger, reservation, {
                **_result(reservation, 'http_200', 'contract_error'),
                'usage': {'input_tokens': 10, 'output_tokens': 2}})
        assert _refusal_of(ledger, 'synthetic prompt') == 'repeat_failed'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 6


def test_a_settled_refusal_is_refused_and_never_counted(tmp_path, monkeypatch):
    """A provider-proven refusal (contract_error_detail) is declined content:
    the resend allowance excludes it — repeat_failed, no new reservation —
    and the detail stays on the stored result row for the guard to read."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        cloud_policy.record_result(ledger, reservation, {
            **_result(reservation, 'http_200', 'contract_error'),
            'usage': {'input_tokens': 10, 'output_tokens': 2},
            'contract_error_detail': 'refusal'})
        assert _refusal_of(ledger, 'synthetic prompt') == 'repeat_failed'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1
        stored = json.loads(ledger.artifacts('cloud_usage_result')[0]['meta'])
        assert stored['contract_error_detail'] == 'refusal'


def test_a_refusal_in_the_history_blocks_later_resends(tmp_path, monkeypatch):
    """A resendable failure followed by a provider refusal still refuses: the
    refusal row is excluded from the allowance and never spends the count, so
    another attempt is denied even though fewer than five failures exist."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        _settled_failure(ledger)
        second = _reserve(ledger, 'synthetic prompt')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2
        cloud_policy.record_result(ledger, second, {
            **_result(second, 'http_200', 'contract_error'),
            'usage': {'input_tokens': 10, 'output_tokens': 2},
            'contract_error_detail': 'refusal'})
        assert _refusal_of(ledger, 'synthetic prompt') == 'repeat_failed'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_reservation_without_result_is_result_missing_not_a_broken_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        _reserve(ledger, 'synthetic prompt')
        assert _refusal_of(ledger, 'synthetic prompt') == 'result_missing'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def test_disposition_is_classified_apart_from_a_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        ledger.db.execute('BEGIN IMMEDIATE')
        cloud_policy.record_disposition_tx(ledger, reservation, 'discarded_scope')
        ledger.db.commit()
        assert _refusal_of(ledger, 'synthetic prompt') == 'disposition'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def test_inconsistent_receipt_is_classified_apart_from_a_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        _reserve(ledger, 'synthetic prompt')
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=? WHERE kind='cloud_usage_reserve'",
                              ('{"unit_key":"other","unit_key":"synthetic-unit"}',))
        assert _refusal_of(ledger, 'synthetic prompt') == 'receipt_inconsistent'


@pytest.mark.parametrize('status,marker', [
    ('http_400', {'anthropic_400_diagnostic': 'usage_limit'}),
    ('http_429', {'anthropic_error_code': 'enforced_spend_limit_reached'})])
def test_anthropic_verified_payment_retries_only_after_receipted_human_resume(tmp_path, monkeypatch, status, marker):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    monkeypatch.setattr(cloud_policy, 'selected_key', lambda *args, **kwargs: 'synthetic')
    monkeypatch.setattr(mcs_util, 'HOME', str(tmp_path))
    (tmp_path / 'config.json').write_text(json.dumps({
        'mcs_login_id': 'synthetic', 'notify_target': 'synthetic',
        'cloud_llm': {'mode': 'on', 'project_ids': [1], 'provider': 'anthropic'},
    }))
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        cloud_policy.record_result(ledger, reservation, {
            **_result(reservation, status, 'payment_required'), **marker})
        usage = cloud_policy.usage_report(ledger)
        assert usage['unresolved_requests'] == 1
        assert usage['max_reserved_cost_usd'] == reservation['max_cost_usd']
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt')
        req = {'version': 1, 'cmd': 'ops.cloud_resume', 'command_id': str(uuid.uuid4()),
               'actor': 'synthetic human', 'human_confirmed': False,
               'project_id': None, 'reason': 'synthetic approval'}
        denied = mcs_requests.apply_command(ledger, req)
        assert denied['outcome'] == 'rejected' and denied['error'] == 'human_confirmation_required'
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt')
        req.update(command_id=str(uuid.uuid4()), human_confirmed=True)
        receipt = mcs_requests.apply_command(ledger, req)
        assert receipt['outcome'] == 'applied'
        _reserve(ledger, 'synthetic prompt')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2
        assert cloud_policy.usage_report(ledger)['unresolved_requests'] == 2


@pytest.mark.parametrize('status,error', [('http_400', 'payment_required'),
                                          ('http_429', 'payment_required')])
def test_an_old_unmarked_anthropic_payment_receipt_still_blocks_after_resume(tmp_path, monkeypatch, status, error):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        cloud_policy.record_result(ledger, reservation, _result(reservation, status, error))
        cloud_policy.resume(ledger)
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt')
        assert caught.value.detail == 'cloud_unit_unresolved'


@pytest.mark.parametrize('change', ['marker_false', 'marker_integer', 'marker_string',
                                    'wrong_status', 'wrong_kind', 'wrong_provider', 'usage_missing',
                                    'openai_marker_mixed'])
def test_corrupt_anthropic_payment_marker_cannot_prove_retry(tmp_path, monkeypatch, change):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        cloud_policy.record_result(ledger, reservation, {
            **_result(reservation, 'http_429', 'payment_required'),
            'anthropic_error_code': 'enforced_spend_limit_reached'})
        expressions = {'marker_false': "'$.anthropic_error_code',json('false')",
                       'marker_integer': "'$.anthropic_error_code',1",
                       'marker_string': "'$.anthropic_error_code','synthetic_unknown'",
                       'wrong_status': "'$.http_class','http_400'",
                       'wrong_kind': "'$.error_kind','rate_limited'",
                       'wrong_provider': "'$.model','gpt-6-luna'",
                       'openai_marker_mixed': "'$.openai_error_code','insufficient_quota'"}
        with ledger.db:
            if change == 'usage_missing':
                ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.usage') "
                                  "WHERE kind='cloud_usage_result'")
            else:
                ledger.db.execute("UPDATE artifacts SET meta=json_set(meta," + expressions[change] + ") "
                                  "WHERE kind='cloud_usage_result'")
        assert cloud_policy.usage_report(ledger)['pricing_unverified'] is True
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt')


@pytest.mark.parametrize('evidence', [
    {'answer_text_sha256_v1': 'a' * 64},
    {'answer_finish_reason_v1': 'stop'},
    {'contract_error_detail': 'refusal'}])
def test_stored_payment_marker_mixed_with_200_evidence_stays_held(tmp_path, monkeypatch, evidence):
    """record_result can never write a payment marker beside 200-only evidence;
    a stored row that mixes them is corrupt, so it must hold as unresolved —
    never prove a resendable rejection, even after a receipted resume."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        cloud_policy.record_result(ledger, reservation, {
            **_result(reservation, 'http_429', 'payment_required'),
            'anthropic_error_code': 'enforced_spend_limit_reached'})
        field, value = next(iter(evidence.items()))
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,?,?) "
                              "WHERE kind='cloud_usage_result'", ('$.' + field, value))
        stored = ledger.db.execute("SELECT meta FROM artifacts WHERE kind='cloud_usage_result'").fetchone()[0]
        assert cloud_policy.usage_report(ledger)['pricing_unverified'] is True
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt')
        cloud_policy.resume(ledger)
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1
        assert ledger.db.execute("SELECT meta FROM artifacts WHERE kind='cloud_usage_result'").fetchone()[0] == stored


def test_stored_rate_limited_mixed_with_200_evidence_stays_held(tmp_path, monkeypatch):
    """The generic http_429 rate_limited rejection carries no provider marker;
    a stored row mixing it with 200-only evidence is just as impossible and
    must hold instead of authorizing another attempt."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        reservation = _reserve(ledger, 'synthetic prompt')
        cloud_policy.record_result(ledger, reservation, _result(reservation, 'http_429', 'rate_limited'))
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.answer_text_sha256_v1',?) "
                              "WHERE kind='cloud_usage_result'", ('a' * 64,))
        assert cloud_policy.usage_report(ledger)['pricing_unverified'] is True
        with pytest.raises(anthropic_client.AnthropicError):
            _reserve(ledger, 'synthetic prompt')
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def _anthropic_profile(route, purpose):
    selected, errors = cloud_policy.resolve_profile(
        {'cloud_llm': {'mode': 'on', 'project_ids': [1], 'provider': 'anthropic'}},
        route=route, purpose=purpose)
    assert not errors
    return selected


def test_c1_pending_unit_is_not_resent_under_the_new_profile(tmp_path, monkeypatch):
    """A paid attempt without a result stays unresolved across the C1 profile change."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    selected = _anthropic_profile('patient_context', 'patient_context')
    with closing(_db(tmp_path)) as ledger:
        _reserve(ledger, 'synthetic prompt', 'anthropic', route='patient_context', unit='old',
                 owner={'workday_id': 'old', 'source_sig': 'old'})
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt', 'anthropic', route='patient_context', unit='new',
                     owner={'workday_id': 'new', 'source_sig': 'new'},
                     max_tokens=selected['max_tokens'], selection=selected)
        assert caught.value.detail == 'cloud_unit_unresolved'
        assert caught.value.history_refusal == 'result_missing'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def test_c1_failed_unit_is_resent_as_a_new_billed_attempt(tmp_path, monkeypatch):
    """A settled paid failure stays settled; the resend guard now allows the unit
    to be sent again as a separate billed attempt (rule change 2026-11-20)."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    selected = _anthropic_profile('patient_context', 'patient_context')
    with closing(_db(tmp_path)) as ledger:
        previous = _reserve(ledger, 'synthetic prompt', 'anthropic', route='patient_context', unit='old',
                            owner={'workday_id': 'old', 'source_sig': 'old'})
        cloud_policy.record_result(ledger, previous, {
            **_result(previous, 'http_200', 'contract_error'),
            'usage': {'input_tokens': 10, 'output_tokens': 2}})
        _reserve(ledger, 'synthetic prompt', 'anthropic', route='patient_context', unit='new',
                 owner={'workday_id': 'new', 'source_sig': 'new'},
                 max_tokens=selected['max_tokens'], selection=selected)
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_c1_completed_backlog_is_not_rebuilt_under_haiku_medium(tmp_path, monkeypatch):
    """Completed backlog work keeps its Haiku result: same model with effort
    None to medium is not a proven selection change, so nothing is resent."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    selected = _anthropic_profile('semantic_backlog', 'backlog')
    assert (selected['model'], selected['effort'], selected['profile']) == (
        anthropic_client.MODEL, 'medium', 'haiku-medium')
    with closing(_db(tmp_path)) as ledger:
        previous = _reserve(ledger, 'synthetic prompt', 'anthropic', route='semantic_backlog',
                            unit='old', owner=_owner())
        cloud_policy.record_result(ledger, previous, {
            **_result(previous, 'http_200', None),
            'usage': {'input_tokens': 10, 'output_tokens': 2}})
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt', 'anthropic', route='semantic_backlog',
                     unit='new', owner=_owner(),
                     max_tokens=selected['max_tokens'], selection=selected)
        assert caught.value.detail == 'cloud_unit_unresolved'
        assert caught.value.history_refusal == 'repeat_success'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


@pytest.mark.parametrize('purpose', ['patient_context', 'quality_repair'])
def test_c1_completed_patient_work_is_rebuilt_once_under_sonnet(tmp_path, monkeypatch, purpose):
    """Completed patient-context work may be rebuilt exactly once under Sonnet;
    the rebuilt unit then blocks any further resend as a repeat success."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    selected = _anthropic_profile('patient_context', purpose)
    assert (selected['model'], selected['effort'], selected['profile']) == (
        anthropic_client.SONNET_MODEL, 'high', 'sonnet-high')
    with closing(_db(tmp_path)) as ledger:
        previous = _reserve(ledger, 'synthetic prompt', 'anthropic', route='patient_context',
                            unit='old', owner={'workday_id': 'old', 'source_sig': 'old'})
        cloud_policy.record_result(ledger, previous, {
            **_result(previous, 'http_200', None),
            'usage': {'input_tokens': 10, 'output_tokens': 2}})
        rebuilt = _reserve(ledger, 'synthetic prompt', 'anthropic', route='patient_context',
                           unit='new', owner={'workday_id': 'new', 'source_sig': 'new'},
                           max_tokens=selected['max_tokens'], selection=selected)
        assert rebuilt['model'] == anthropic_client.SONNET_MODEL
        cloud_policy.record_result(ledger, rebuilt, {
            **_result(rebuilt, 'http_200', None),
            'usage': {'input_tokens': 10, 'output_tokens': 2}})
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt', 'anthropic', route='patient_context',
                     unit='third', owner={'workday_id': 'third', 'source_sig': 'third'},
                     max_tokens=selected['max_tokens'], selection=selected)
        assert caught.value.detail == 'cloud_unit_unresolved'
        assert caught.value.history_refusal == 'repeat_success'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_c1_completed_semantic_quality_repair_is_rebuilt_once_under_sonnet(tmp_path, monkeypatch):
    """The same one-time Sonnet rebuild applies to quality repair on the
    semantic backlog route."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    selected = _anthropic_profile('semantic_backlog', 'quality_repair')
    assert (selected['model'], selected['effort'], selected['profile']) == (
        anthropic_client.SONNET_MODEL, 'high', 'sonnet-high')
    with closing(_db(tmp_path)) as ledger:
        previous = _reserve(ledger, 'synthetic prompt', 'anthropic', route='semantic_backlog',
                            unit='old', owner=_owner())
        cloud_policy.record_result(ledger, previous, {
            **_result(previous, 'http_200', None),
            'usage': {'input_tokens': 10, 'output_tokens': 2}})
        rebuilt = _reserve(ledger, 'synthetic prompt', 'anthropic', route='semantic_backlog',
                           unit='new', owner=_owner(),
                           max_tokens=selected['max_tokens'], selection=selected)
        assert rebuilt['model'] == anthropic_client.SONNET_MODEL
        cloud_policy.record_result(ledger, rebuilt, {
            **_result(rebuilt, 'http_200', None),
            'usage': {'input_tokens': 10, 'output_tokens': 2}})
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt', 'anthropic', route='semantic_backlog',
                     unit='third', owner=_owner(),
                     max_tokens=selected['max_tokens'], selection=selected)
        assert caught.value.detail == 'cloud_unit_unresolved'
        assert caught.value.history_refusal == 'repeat_success'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def _semantic_owner(generation, *, phase='facts', source=None):
    return {'semantic_target_id': 1, 'semantic_thread_root': 1,
            'semantic_source': source or 'a' * 64,
            'semantic_generation': generation, 'semantic_phase': phase}


def _settled_success(ledger, reservation):
    cloud_policy.record_result(ledger, reservation, {
        **_result(reservation, 'http_200', None),
        'usage': {'input_tokens': 10, 'output_tokens': 2}})


def test_semantic_previous_generation_success_does_not_block_reprocess_send(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        previous = _reserve(ledger, 'synthetic prompt', unit='semantic:1:old',
                            owner=_semantic_owner('old-generation'))
        _settled_success(ledger, previous)
        current = _reserve(ledger, 'synthetic prompt', unit='semantic:2:new',
                           owner=_semantic_owner('new-generation'))
        assert current['owner']['semantic_generation'] == 'new-generation'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_semantic_same_attempt_checks_legacy_prompt_after_model_change(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        previous = _reserve(ledger, 'synthetic prompt', unit='semantic:1:same',
                            owner=_semantic_owner('old-generation'))
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp') "
                              "WHERE kind='cloud_usage_reserve'")
        with pytest.raises(anthropic_client.AnthropicError) as error:
            _reserve(ledger, 'synthetic prompt', unit=previous['unit_key'],
                     owner=_semantic_owner('new-generation'))
        assert error.value.detail == 'cloud_unit_unresolved'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


@pytest.mark.parametrize('prompt,source,phase', [
    ('synthetic updated', 'a' * 64, 'facts'),
    ('synthetic prompt', 'b' * 64, 'facts'),
    ('synthetic prompt', 'a' * 64, 'summary'),
])
def test_semantic_same_attempt_allows_a_distinct_request(tmp_path, monkeypatch, prompt, source, phase):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        previous = _reserve(ledger, 'synthetic prompt', unit='semantic:1:same',
                            owner=_semantic_owner('old-generation'))
        _settled_success(ledger, previous)
        _reserve(ledger, prompt, unit=previous['unit_key'],
                 owner=_semantic_owner('new-generation', source=source, phase=phase))
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_semantic_same_generation_success_still_blocks_duplicate_send(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        previous = _reserve(ledger, 'synthetic prompt', unit='semantic:1:old',
                            owner=_semantic_owner('same-generation'))
        _settled_success(ledger, previous)
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt', unit='semantic:2:new',
                     owner=_semantic_owner('same-generation'))
        assert caught.value.detail == 'cloud_unit_unresolved'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


def _make_prebinding_legacy(ledger, reservation):
    with ledger.db:
        for kind in ('cloud_usage_reserve', 'cloud_usage_result'):
            ledger.db.execute(
                "UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp','$.input_binding') "
                "WHERE kind=? AND json_extract(meta,'$.reservation_id')=?",
                (kind, reservation['reservation_id']))
        ledger.db.execute(
            "UPDATE artifacts SET created_at=? WHERE kind='cloud_usage_reserve' "
            "AND json_extract(meta,'$.reservation_id')=?",
            (cloud_policy.SEMANTIC_MEMBER_BINDING_START_EPOCH - 1, reservation['reservation_id']))


def test_prebinding_semantic_success_is_migration_evidence_not_duplicate_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        previous = _reserve(ledger, 'synthetic prompt', unit='semantic:1:abc123', owner={})
        _settled_success(ledger, previous)
        _make_prebinding_legacy(ledger, previous)
        current = _reserve(ledger, 'synthetic prompt', unit='semantic:2:current',
                           owner=_semantic_owner('new-generation'))
        assert current['owner']['semantic_generation'] == 'new-generation'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 2


def test_postbinding_unbound_semantic_success_still_blocks_duplicate_send(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with closing(_db(tmp_path)) as ledger:
        previous = _reserve(ledger, 'synthetic prompt', unit='semantic:1:abc123', owner={})
        _settled_success(ledger, previous)
        with ledger.db:
            for kind in ('cloud_usage_reserve', 'cloud_usage_result'):
                ledger.db.execute(
                    "UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp','$.input_binding') "
                    "WHERE kind=? AND json_extract(meta,'$.reservation_id')=?",
                    (kind, previous['reservation_id']))
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            _reserve(ledger, 'synthetic prompt', unit='semantic:2:current',
                     owner=_semantic_owner('new-generation'))
        assert caught.value.detail == 'cloud_unit_unresolved'
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1
