"""Fixed repeat observations never change paid-history or recovery decisions."""
from contextlib import closing
import json
import time

import pytest

import cloud_client
import cloud_inference
import cloud_policy
import openai_client
from ledger import Ledger
from test_cloud_response_recovery import legacy_cloud_routes, paid as paid
from test_cloud_result_recovery_reasons import _damage
from test_cloud_resend_guard import _db, _reserve, _result


def _ask(provider, tokens=100):
    return provider('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=tokens)


def _events(capsys):
    output = capsys.readouterr()
    assert not output.out
    assert 'synthetic-private' not in output.err
    events = [json.loads(line) for line in output.err.splitlines()]
    for event in events:
        event.pop('pid')
        event.pop('ts')
    return events


@pytest.mark.parametrize('field,stored,current,expected', [
    ('effort', {'effort': None}, {'effort': None}, 'same'),
    ('profile', {'profile': None}, {'profile': 'synthetic'}, 'different'),
    ('profile', {}, {'profile': None}, 'stored_absent'),
    ('profile', {'profile': None}, {}, 'current_absent'),
    ('profile', {}, {}, 'unknown'),
    ('max_tokens', {'max_tokens': 1}, {'max_tokens': True}, 'unknown'),
    ('unit_key', {'unit_key': 'synthetic'}, {'unit_key': 'other'}, 'different'),
    ('request_fp', {'request_fp': ['synthetic-private']}, {'request_fp': 'synthetic'}, 'unknown'),
])
def test_comparisons_keep_presence_nullable_values_and_types(field, stored, current, expected):
    assert cloud_policy._repeat_comparison(stored, current, field) == expected


@pytest.mark.parametrize('removed,expected', [
    ((), 'recovery_not_requested'), (('input_binding',), 'unit_key_mismatch'),
    (('input_binding', 'unit_key'), 'stored_binding_missing'),
])
def test_primary_reason_uses_fixed_precedence(removed, expected):
    previous = {'unit_key': 'synthetic-old', 'request_fp': 'a' * 64}
    candidate = {'unit_key': 'synthetic-new', 'request_fp': 'b' * 64}
    if 'input_binding' in removed:
        candidate['input_binding'] = 'c' * 64
    if 'unit_key' in removed:
        candidate['unit_key'] = previous['unit_key']
    assert cloud_policy._repeat_observation(previous, candidate, 'legacy_unproven')['primary_reason'] == expected


@pytest.mark.parametrize('change,path,reason,comparison', [
    ('tokens', 'no_exact_rows', 'request_envelope_mismatch', 'max_tokens_cmp'),
    ('binding', 'no_exact_rows', 'input_binding_mismatch', 'binding_cmp'),
    ('absent', 'skipped_binding', 'recovery_not_requested', 'binding_cmp'),
    ('unit', 'no_exact_rows', 'unit_key_mismatch', 'unit_key_cmp'),
    ('stored_absent', 'no_exact_rows', 'stored_binding_missing', 'binding_cmp'),
])
def test_actual_refusal_observes_recovery_without_new_send(paid, capsys, change, path, reason, comparison):
    dbpath, _cfg, calls, locks, _state, unit, _restart = paid
    with closing(Ledger(dbpath)) as ledger:
        with legacy_cloud_routes():
            _ask(unit(ledger))
        provider = unit(ledger, binding='synthetic-changed' if change == 'binding' else 'original-input')
        if change == 'absent':
            provider.input_binding = None
        elif change == 'unit':
            provider.unit_key = 'synthetic-other-unit'
        elif change == 'stored_absent':
            with ledger.db:
                ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.input_binding') "
                                  "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')")
        before = [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
        with pytest.raises(cloud_client.CloudNotSent) as caught:
            _ask(provider, 101 if change == 'tokens' else 100)
        assert (caught.value.kind, caught.value.detail) == ('budget_exceeded', 'cloud_unit_unresolved')
        assert caught.value.__cause__ is None and caught.value.__suppress_context__
        assert provider.possibly_sent and provider.unresolved
        assert len(calls) == len(locks) == 1
        assert [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    event, = _events(capsys)
    assert event['phase'] == 'request_send_block_reason' and event['history_refusal'] == 'repeat_success'
    assert event['recovery_path'] == path and event['primary_reason'] == reason
    assert event['guard_match'] == 'logical_equal'
    assert event[comparison] == {'absent': 'current_absent', 'stored_absent': 'stored_absent'}.get(change, 'different')
    assert event['effort_cmp'] == event['profile_cmp'] == 'same'


@pytest.mark.parametrize('kind', ['logical_equal', 'legacy_unproven', 'legacy_low_verified'])
def test_guard_reports_the_comparison_it_used_without_inferred_recovery(tmp_path, monkeypatch, capsys, kind):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *a, **kw: True)
    provider = 'openai' if kind == 'legacy_low_verified' else 'anthropic'
    with closing(_db(tmp_path)) as ledger:
        with legacy_cloud_routes():
            reservation = _reserve(ledger, 'synthetic prompt', provider, route='reextract')
        cloud_policy.record_result(ledger, reservation, _result(reservation, 'timeout', 'timeout'))
        if kind != 'logical_equal':
            with ledger.db:
                ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp') "
                                  "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')")
        body = openai_client.OpenAIClient('synthetic').body('synthetic prompt', 100, None) if provider == 'openai' else None
        with pytest.raises(cloud_client.CloudError) as caught:
            cloud_policy._guard_repeated_request(ledger, reservation, current_body=body)
        assert caught.value.history_observation['guard_match'] == kind
        cloud_inference._history_read_error('request_send_block_reason', caught.value)
    event, = _events(capsys)
    assert event['recovery_path'] == 'unknown'
    assert event['guard_match'] == kind


@pytest.mark.parametrize('route,mid', [('semantic_backlog', 1), ('patient_context', 1), ('reextract', None)])
def test_other_routes_and_unbound_messages_have_no_new_fields(tmp_path, monkeypatch, capsys, route, mid):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *a, **kw: True)
    with closing(_db(tmp_path)) as ledger:
        with legacy_cloud_routes():
            reservation = _reserve(ledger, 'synthetic prompt', route=route, message_id=mid)
        cloud_policy.record_result(ledger, reservation, _result(reservation, 'timeout', 'timeout'))
        with pytest.raises(cloud_client.CloudError) as caught:
            cloud_policy._guard_repeated_request(ledger, reservation)
        assert not hasattr(caught.value, 'history_observation')
        cloud_inference._history_read_error('request_send_block_reason', caught.value)
    event, = _events(capsys)
    assert not set(cloud_inference._HISTORY_OBSERVATIONS).intersection(event)


def test_untrusted_observation_values_are_never_emitted(capsys):
    error = cloud_client.CloudError('budget_exceeded', 'cloud_unit_unresolved')
    error.history_observation = {key: 'synthetic-private' for key in cloud_inference._HISTORY_OBSERVATIONS}
    cloud_inference._history_read_error('request_send_block_reason', error,
                                       observation={'recovery_path': ['synthetic-private']})
    event, = _events(capsys)
    assert event['primary_reason'] == 'observation_unknown'
    assert all(event[key] == 'unknown' for key in cloud_inference._HISTORY_OBSERVATIONS if key != 'primary_reason')


@pytest.mark.parametrize('damage', [None, 'missing', 'hash_mismatch'])
def test_exact_recovery_keeps_success_and_answer_failures_in_their_existing_phase(paid, capsys, damage):
    dbpath, _cfg, calls, _locks, _state, unit, _restart = paid
    with closing(Ledger(dbpath)) as ledger:
        with legacy_cloud_routes():
            _ask(unit(ledger))
        if damage is not None:
            _damage(ledger, damage)
            with pytest.raises(cloud_client.CloudNotSent):
                _ask(unit(ledger))
        else:
            assert _ask(unit(ledger))['reused_response']
        assert len(calls) == 1
    events = _events(capsys)
    if damage is None:
        assert events == []
    else:
        event, = events
        assert event['phase'] == 'recover_response'
        assert event['error_reason'] == ('cloud_result_missing' if damage == 'missing' else 'cloud_answer_invalid')
        assert not set(cloud_inference._HISTORY_OBSERVATIONS).intersection(event)


def test_recovery_distinguishes_confirmed_rejections_using_existing_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *a, **kw: True)
    with closing(_db(tmp_path)) as ledger:
        with legacy_cloud_routes():
            reservation = _reserve(ledger, 'synthetic prompt', route='reextract')
        cloud_policy.record_result(ledger, reservation, _result(reservation, 'not_sent', None))
        observation = {}
        args = dict(route='reextract', project_id=1, message_id=1, unit_key=reservation['unit_key'],
                    input_binding=None, request_fp=reservation['request_fp'], observation=observation)
        # The exact query's NULL equality returns no rows for a legacy unbound reservation.
        assert cloud_policy.recover_response(ledger, **args) is None
        assert observation == {'recovery_path': 'no_exact_rows'}
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.input_binding',?) "
                              "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')", ('a' * 64,))
        args['input_binding'] = 'a' * 64
        assert cloud_policy.recover_response(ledger, **args) is None
        assert observation == {'recovery_path': 'confirmed_rejection_only'}


def test_changed_history_can_make_exact_keys_same_without_proving_a_race(paid, monkeypatch, capsys):
    dbpath, _cfg, calls, _locks, _state, unit, _restart = paid
    with closing(Ledger(dbpath)) as ledger:
        with legacy_cloud_routes():
            _ask(unit(ledger))
        provider = unit(ledger, binding='synthetic-changed')
        recover = cloud_policy.recover_response
        def changed(*args, **kwargs):
            result = recover(*args, **kwargs)
            assert result is None
            with ledger.db:
                ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.input_binding',?) "
                                  "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')", (provider._input_binding(),))
            return result
        monkeypatch.setattr(cloud_policy, 'recover_response', changed)
        with pytest.raises(cloud_client.CloudNotSent):
            _ask(provider)
        assert len(calls) == 1
    event, = _events(capsys)
    assert event['recovery_path'] == 'no_exact_rows'
    assert event['primary_reason'] == 'exact_keys_same_unexplained'


def test_observation_is_fresh_for_each_call_and_repair_child(paid, monkeypatch, capsys):
    dbpath, _cfg, calls, _locks, _state, unit, _restart = paid
    with closing(Ledger(dbpath)) as ledger:
        with legacy_cloud_routes():
            _ask(unit(ledger))
        provider = unit(ledger)
        child = provider.spawn_quality_repair()
        observed = []
        recover = cloud_policy.recover_response
        def capture(*args, **kwargs):
            observed.append(kwargs['observation'])
            return recover(*args, **kwargs)
        monkeypatch.setattr(cloud_policy, 'recover_response', capture)
        for caller, tokens in [(provider, 101), (provider, 102), (child, 103)]:
            with pytest.raises(cloud_client.CloudNotSent):
                _ask(caller, tokens)
        assert len({id(value) for value in observed}) == 3
        assert len(calls) == 1
    assert all(event['recovery_path'] == 'no_exact_rows' for event in _events(capsys))


def test_observation_build_failure_preserves_original_refusal(paid, monkeypatch, capsys):
    dbpath, _cfg, calls, _locks, _state, unit, _restart = paid
    def failed(*args, **kwargs):
        raise RuntimeError('synthetic-private')
    with closing(Ledger(dbpath)) as ledger:
        with legacy_cloud_routes():
            _ask(unit(ledger))
        monkeypatch.setattr(cloud_policy, '_repeat_observation', failed)
        with pytest.raises(cloud_client.CloudNotSent) as caught:
            _ask(unit(ledger), 101)
        assert (caught.value.kind, caught.value.detail) == ('budget_exceeded', 'cloud_unit_unresolved')
        assert len(calls) == 1
    event, = _events(capsys)
    assert event['history_refusal'] == 'repeat_success'
    assert event['primary_reason'] == 'observation_unknown' and event['guard_match'] == 'unknown'
