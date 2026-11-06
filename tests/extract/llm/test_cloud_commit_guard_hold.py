"""Synthetic paid results distinguish a missing proof from scope withdrawal."""
import extract_llm_runner
from contextlib import nullcontext
import json
import time

import pytest

import cloud_inference
import extract_llm as worker
from anthropic_client import AnthropicClient
from test_anthropic_runtime import old, routed as routed, _bound_source, _use_real_cloud_accounting
from test_cloud_response_recovery import legacy_cloud_routes
from test_commit_hold_fairness import claim_rows


@pytest.mark.parametrize('failure,guard_at', [
    ('unknown', 2), ('unknown', 3), ('changed', 2), ('changed', 3),
])
def test_paid_result_commit_guard_fences_defer_and_scope_loss(routed, monkeypatch, failure, guard_at):
    ledger, cfg, _, _, _ = routed
    old(ledger)
    _use_real_cloud_accounting(monkeypatch)
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *_a: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
    posts, responses, fail_once = [], [], [True]

    def request(self, method, body, timeout, **kwargs):
        posts.append(1)
        return 200, {}, json.dumps({'model': worker.anthropic_client.MODEL, 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': '{"symptoms":[]}'}],
            'usage': {'input_tokens': 10, 'output_tokens': 5}}).encode()

    monkeypatch.setattr(AnthropicClient, 'request', request)
    actual_init, actual_call = cloud_inference.CloudInferenceUnit.__init__, cloud_inference.CloudInferenceUnit.__call__

    def init(self, *args, **kwargs):
        actual_init(self, *args, **kwargs)
        self._test_posted = False
        self._test_checks = 0
        real_guard = self.commit_guard

        def guarded():
            if self._test_posted:
                self._test_checks += 1
                if self._test_checks == guard_at and fail_once[0]:
                    fail_once[0] = False
                    if failure == 'unknown':
                        raise cloud_inference.CloudObservationIncomplete('synthetic_unverified')
                    cfg['cloud_llm']['mode'] = 'off'
            return real_guard()

        self.commit_guard = guarded

    def call(self, *args, **kwargs):
        answer = actual_call(self, *args, **kwargs)
        self._test_posted = True
        responses.append(answer)
        return answer

    monkeypatch.setattr(cloud_inference.CloudInferenceUnit, '__init__', init)
    monkeypatch.setattr(cloud_inference.CloudInferenceUnit, '__call__', call)

    def infer(_body, *, provider, deadline, **_kwargs):
        provider('synthetic prompt', deadline=deadline, max_tokens=100)
        return {'symptoms': []}

    monkeypatch.setattr(worker, 'llm_extract', infer)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        result = worker.run_pending(ledger, limit=1, budget_s=180)
    assert not fail_once[0] and len(posts) == 1
    assert result['done'] == 0 and result['deferred'] == 1
    receipt, = ledger.artifacts('cloud_usage_result')
    assert json.loads(receipt['meta'])['http_class'] == 'http_200'
    assert json.loads(receipt['content'])['text'] == '{"symptoms":[]}'
    if failure == 'changed':
        disposition, = ledger.artifacts('cloud_usage_disposition')
        assert json.loads(disposition['meta'])['disposition'] == 'discarded_scope'
        return
    assert not ledger.artifacts('cloud_usage_disposition')
    waiting, = claim_rows(ledger)
    now = time.time()
    assert now < waiting['next_try'] <= now + worker._PREPARE_HOLD_S
    # Advance the synthetic claim beyond its bounded hold without sleeping.
    with ledger.db:
        ledger.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='extract_claim'")
    with legacy_cloud_routes():
        later = worker.run_pending(ledger, limit=1, budget_s=180)
    assert later['done'] == 1 and len(posts) == 1
    assert responses[-1]['reused_response'] is True
    assert not ledger.artifacts('cloud_usage_disposition')


@pytest.mark.parametrize('failure', ['unknown', 'changed'])
def test_direct_replace_rolls_back_without_discarding_unverified_response(routed, failure):
    ledger, *_ = routed
    old(ledger)
    before = [tuple(row) for row in ledger.artifacts(worker.KIND)]
    row = _bound_source(ledger)
    discarded = []

    def guard():
        assert ledger.db.in_transaction
        if failure == 'unknown':
            raise cloud_inference.CloudObservationIncomplete('synthetic_unverified')
        raise cloud_inference.CloudFallback('cloud_unit_changed')

    row['_cloud_guard'] = guard
    row['_cloud_discard'] = lambda: discarded.append(True)
    assert worker._replace_current(ledger, row, '{}') is False
    assert discarded == ([True] if failure == 'changed' else [])
    assert [tuple(item) for item in ledger.artifacts(worker.KIND)] == before
    assert not ledger.db.in_transaction
