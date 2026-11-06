"""A checkpoint dispatch fence never substitutes for live scope or paid-answer proof."""
from contextlib import closing
import time

import pytest

import cloud_client
import cloud_inference
from ledger import Ledger
from mcs_util import payload_hash
from test_cloud_response_recovery import legacy_cloud_routes, paid as paid


def provider(ledger, cfg, state, fence):
    return cloud_inference.CloudInferenceUnit(ledger, cfg, route='reextract', project_id=1,
        message_id=1, unit_key='synthetic-unit', deadline=time.monotonic() + 180,
        current=lambda: state['current'], dispatch=lambda fn, **kwargs: fn(),
        owner={'lease': 'synthetic-first'}, input_binding=payload_hash('original-input'),
        dispatch_current=fence)


@pytest.mark.parametrize('failure', [False, None, 'exception'])
def test_dispatch_fence_is_presend_and_cannot_discard_existing_paid_answer(paid, failure):
    path, cfg, calls, _locks, state, _unit, _restart = paid
    blocked = [False]
    def fence():
        if not blocked[0]:
            return True
        if failure == 'exception':
            raise ValueError('synthetic-private-detail')
        return failure
    with closing(Ledger(path)) as ledger:
        unit = provider(ledger, cfg, state, fence)
        with legacy_cloud_routes():
            first = unit('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
        assert first['text'] == '{"synthetic":"answer"}' and len(calls) == 1
        money = [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts WHERE kind LIKE 'cloud_usage_%'")]
        blocked[0] = True
        unit.commit_guard()  # Save scope remains valid; only another dispatch is refused.
        recovered = provider(ledger, cfg, state, fence)(
            'synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
        assert recovered['reused_response'] is True and len(calls) == 1
        with legacy_cloud_routes(), pytest.raises(cloud_client.CloudNotSent) as refused:
            unit('new synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
        assert cloud_client.not_sent_reason(refused.value) == 'checkpoint_unverified'
        assert len(calls) == 1
        assert [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts WHERE kind LIKE 'cloud_usage_%'")] == money


def test_scope_loss_with_checkpoint_fence_still_rejects_paid_adoption(paid):
    path, cfg, calls, _locks, state, _unit, _restart = paid
    with closing(Ledger(path)) as ledger:
        unit = provider(ledger, cfg, state, lambda: True)
        with legacy_cloud_routes():
            unit('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
        state['current'] = False
        with legacy_cloud_routes(), pytest.raises(cloud_inference.CloudFallback, match='cloud_unit_changed'):
            provider(ledger, cfg, state, lambda: False)(
                'synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
        assert len(calls) == 1
        assert len(ledger.artifacts('cloud_usage_reserve')) == len(ledger.artifacts('cloud_usage_result')) == 1
        assert not ledger.db.in_transaction


def test_quality_child_inherits_checkpoint_dispatch_fence(paid):
    path, cfg, _calls, _locks, state, _unit, _restart = paid
    with closing(Ledger(path)) as ledger:
        unit = provider(ledger, cfg, state, lambda: False)
        child = unit.spawn_quality_repair()
        child.commit_guard()
        with legacy_cloud_routes(), pytest.raises(cloud_client.CloudNotSent) as refused:
            child.guard()
        assert cloud_client.not_sent_reason(refused.value) == 'checkpoint_unverified'
        assert not ledger.artifacts('cloud_usage_reserve')
