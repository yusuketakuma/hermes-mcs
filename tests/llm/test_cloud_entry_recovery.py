"""Next-pass history checks precede send caps, including incomplete old envelopes."""
from contextlib import closing
import json
from datetime import datetime, timedelta
import time

import pytest

import anthropic_client
import cloud_client
import cloud_inference
import cloud_policy
from ledger import Ledger
from test_cloud_response_recovery import legacy_cloud_routes, paid

__all__ = ['paid']


def previous_month():
    return (datetime.now(cloud_policy.JST).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=1)).timestamp()


@pytest.mark.parametrize('barrier', ['budget', 'circuit'])
@pytest.mark.parametrize('old', ['saved', 'unknown', 'legacy', 'no_body', 'tampered', 'orphan', 'orphan_prior_month', 'fresh'])
def test_next_unit_checks_history_before_fresh_send_denial(paid, monkeypatch, old, barrier):
    path, cfg, calls, locks, state, unit, _ = paid
    with closing(Ledger(path)) as ledger:
        if old in ('saved', 'no_body', 'orphan', 'orphan_prior_month'):
            with legacy_cloud_routes():
                unit(ledger)('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
            if old == 'orphan_prior_month':
                with ledger.db:
                    ledger.db.execute("UPDATE artifacts SET created_at=? WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')",
                                      (previous_month(),))
            if old in ('orphan', 'orphan_prior_month'):
                with ledger.db:
                    ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_reserve'")
            if old == 'no_body':
                with ledger.db:
                    ledger.db.execute("UPDATE artifacts SET content='{}' WHERE kind='cloud_usage_result'")
        elif old != 'fresh':
            first = unit(ledger)
            with legacy_cloud_routes():
                first.reserve(first.client.body('synthetic prompt', 100, None), 30)
            if old in ('legacy', 'tampered'):
                row = ledger.artifacts('cloud_usage_reserve')[0]
                meta = json.loads(row['meta'])
                if old == 'legacy':
                    for key in ('logical_request_fp', 'input_binding'):
                        meta.pop(key, None)
                else:
                    meta['input_binding'] = '0' * 64
                    meta['owner'] = []
                with ledger.db:
                    ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                                      (json.dumps(meta), row['artifact_id']))
        before = len(calls), len(locks), len(ledger.artifacts('cloud_usage_reserve'))
        if barrier == 'budget':
            cfg['cloud_llm']['daily_cost_limit_usd'] = 0
        else:
            monkeypatch.setattr(cloud_policy, 'block_reason', lambda *a, **k: 'circuit_open')
        next_pass = unit(ledger, owner='next-pass')
        if old == 'saved':
            answer = next_pass('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
            assert answer['reused_response'] and answer['text'] == '{"synthetic":"answer"}'
        elif old == 'fresh':
            with pytest.raises(cloud_inference.CloudFallback, match='cloud_send_blocked'):
                next_pass('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
            assert not next_pass.possibly_sent
        else:
            # Token envelope changes cannot turn a legacy unknown into new work.
            with pytest.raises(cloud_client.CloudNotSent) as denied:
                next_pass('synthetic prompt', deadline=time.monotonic() + 60,
                          max_tokens=101 if old == 'legacy' else 100)
            assert denied.value.detail == 'cloud_unit_unresolved'
        assert (len(calls), len(locks), len(ledger.artifacts('cloud_usage_reserve'))) == before


def test_current_source_denial_still_precedes_saved_answer_recovery(paid):
    path, cfg, calls, _locks, state, unit, _ = paid
    with closing(Ledger(path)) as ledger:
        with legacy_cloud_routes():
            unit(ledger)('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
        cfg['cloud_llm']['daily_cost_limit_usd'] = 0
        state['current'] = False
        with pytest.raises(cloud_inference.CloudFallback, match='cloud_unit_changed'):
            unit(ledger)('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
        assert len(calls) == 1


def unrelated_reservation(ledger, cfg, route, *, project_id=1, message_id=1):
    purpose = 'patient_context' if route == 'patient_context' else 'backlog'
    selection, errors = cloud_policy.resolve_profile(cfg, route=route, purpose=purpose)
    assert not errors and selection['provider'] == 'anthropic'
    client = anthropic_client.AnthropicClient('synthetic-key')
    return cloud_policy.usage_reserver(ledger,
        {'cloud_llm': cloud_inference.resolve_cloud_settings(cfg)}, route=route, project_id=project_id,
        message_id=message_id, unit_key='unrelated-old-unit', selection=selection)(
            client.body('unrelated synthetic old prompt', selection['max_tokens'] or 100, None), 30)


@pytest.mark.parametrize('barrier', ['budget', 'circuit'])
@pytest.mark.parametrize('age', ['current', 'prior_month'])
def test_unrelated_unknown_does_not_pin_new_input_to_cloud(paid, monkeypatch, barrier, age):
    path, cfg, calls, locks, state, unit, _ = paid
    with closing(Ledger(path)) as ledger:
        with legacy_cloud_routes():
            unrelated_reservation(ledger, cfg, 'reextract')
        if age == 'prior_month':
            with ledger.db:
                ledger.db.execute("UPDATE artifacts SET created_at=? WHERE kind='cloud_usage_reserve'", (previous_month(),))
        if barrier == 'budget':
            cfg['cloud_llm']['daily_cost_limit_usd'] = 0
        else:
            monkeypatch.setattr(cloud_policy, 'block_reason', lambda *a, **k: 'circuit_open')
        provider = unit(ledger)
        with pytest.raises(cloud_inference.CloudFallback, match='cloud_send_blocked'):
            provider('synthetic new prompt', deadline=time.monotonic() + 60, max_tokens=100)
        assert not provider.possibly_sent and not calls and not locks
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


@pytest.mark.parametrize('old', ['saved', 'unknown'])
def test_same_text_new_source_occurrence_never_reuses_or_falls_back_at_cap(paid, old):
    path, cfg, calls, locks, _state, unit, _ = paid
    with closing(Ledger(path)) as ledger:
        original = unit(ledger)
        with legacy_cloud_routes():
            if old == 'saved':
                original('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
            else:
                original.reserve(original.client.body('synthetic prompt', 100, None), 30)
        cfg['cloud_llm']['daily_cost_limit_usd'] = 0
        before = len(calls), len(locks)
        different_occurrence = unit(ledger, binding='different-source-occurrence')
        with pytest.raises(cloud_client.CloudNotSent) as denied:
            different_occurrence('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
        assert denied.value.detail == 'cloud_unit_unresolved'
        assert (len(calls), len(locks)) == before
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1


@pytest.mark.parametrize('barrier', ['budget', 'circuit'])
@pytest.mark.parametrize('age', ['current', 'prior_month'])
def test_other_project_valid_unknown_does_not_block_local_new_input(paid, monkeypatch, barrier, age):
    from semantic_testkit import _message, _patient
    path, cfg, calls, locks, _state, unit, _ = paid
    with closing(Ledger(path)) as ledger:
        _patient(ledger, 2)
        ledger.save_messages([_message(2, pid=2)])
        cfg['cloud_llm']['project_ids'] = [1, 2]
        with legacy_cloud_routes():
            unrelated_reservation(ledger, cfg, 'reextract', project_id=2, message_id=2)
        if age == 'prior_month':
            with ledger.db:
                ledger.db.execute("UPDATE artifacts SET created_at=? WHERE kind='cloud_usage_reserve'", (previous_month(),))
        if barrier == 'budget':
            cfg['cloud_llm']['daily_cost_limit_usd'] = 0
        else:
            monkeypatch.setattr(cloud_policy, 'block_reason', lambda *a, **k: 'circuit_open')
        provider = unit(ledger)
        with pytest.raises(cloud_inference.CloudFallback, match='cloud_send_blocked'):
            provider('synthetic new prompt', deadline=time.monotonic() + 60, max_tokens=100)
        assert not provider.possibly_sent and not calls and not locks
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1
