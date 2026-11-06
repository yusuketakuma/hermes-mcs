"""Unverified result accounting retains pricing visibility and never authorizes fallback."""
from contextlib import closing
import time

import pytest

import cloud_client
import cloud_policy
from ledger import Ledger
from test_cloud_response_recovery import legacy_cloud_routes, paid

__all__ = ['paid']


@pytest.mark.parametrize('circuit_error', [None, RuntimeError, TypeError])
def test_corrupt_result_preflight_keeps_pricing_stop_and_never_falls_back(paid, monkeypatch, circuit_error):
    path, _cfg, calls, _locks, _state, unit, _restart = paid
    with closing(Ledger(path)) as ledger:
        with legacy_cloud_routes():
            unit(ledger)('synthetic original prompt', deadline=time.monotonic() + 60, max_tokens=100)
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.usage.input_tokens',-1) "
                              "WHERE kind='cloud_usage_result'")
        original = [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
                    "('cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id")]
        if circuit_error is not None:
            def unavailable(*args, **kwargs):
                raise circuit_error('synthetic circuit store unavailable')
            monkeypatch.setattr(cloud_policy, '_circuit_result', unavailable)
        provider = unit(ledger, owner='next-synthetic-pass')
        with pytest.raises(cloud_client.CloudNotSent) as refused:
            provider('different synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
        assert refused.value.detail == 'cloud_unit_unresolved'
        assert len(calls) == 1 and provider.unresolved
        assert [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
                "('cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id")] == original
        if circuit_error is None:
            assert cloud_policy.global_block_reason(ledger, model=provider.model) == 'pricing_unverified'
        else:
            assert not ledger.artifacts('cloud_circuit')


@pytest.mark.parametrize('damage,expected', [
    ('duplicate_result', 'pricing_unverified'),
    ('missing_reserve', 'budget_exceeded'),
    ('duplicate_reserve', 'budget_exceeded'),
])
def test_same_prompt_changed_binding_classifies_full_receipt_set(paid, damage, expected):
    import json
    path, _cfg, calls, _locks, _state, unit, _restart = paid
    with closing(Ledger(path)) as ledger:
        with legacy_cloud_routes():
            unit(ledger)('synthetic same prompt', deadline=time.monotonic() + 60, max_tokens=100)
        if damage == 'missing_reserve':
            with ledger.db:
                ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_reserve'")
        else:
            kind = 'cloud_usage_result' if damage == 'duplicate_result' else 'cloud_usage_reserve'
            original = ledger.artifacts(kind)[0]
            ledger.artifact_add(kind, original['content'], project_id=1, message_id=1,
                                model=original['model'], meta=json.loads(original['meta']))
        before = [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
                  "('cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id")]
        # Miss exact answer recovery while keeping logical_request_fp equal.
        provider = unit(ledger, binding='different-synthetic-occurrence')
        body = provider.client.body('synthetic same prompt', 100, None)
        with pytest.raises(cloud_client.CloudNotSent) as held:
            provider('synthetic same prompt', deadline=time.monotonic() + 60, max_tokens=100)
        assert held.value.detail == 'cloud_unit_unresolved' and len(calls) == 1
        if damage == 'duplicate_result':
            assert cloud_policy.global_block_reason(ledger, model=provider.model) == 'pricing_unverified'
        else:
            assert not ledger.artifacts('cloud_circuit')
        with pytest.raises(cloud_client.CloudError) as classified:
            cloud_policy.request_send_block_reason(ledger, _cfg, body,
                route=provider.route, project_id=provider.project_id, message_id=provider.message_id,
                unit_key=provider.unit_key, owner=provider.owner, selection=provider.selection,
                input_binding=provider._input_binding())
        assert classified.value.kind == expected and classified.value.detail == 'cloud_unit_unresolved'
        assert [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
                "('cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id")] == before
