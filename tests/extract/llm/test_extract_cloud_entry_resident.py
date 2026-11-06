"""Resident extraction resumes the same paid input before deciding local fallback."""
import extract_llm_runner
from contextlib import nullcontext
import json
import time

import pytest

import anthropic_client
import cloud_backlog
import cloud_policy
import extract_llm as worker
from test_anthropic_runtime import old, routed, real_usage_reserver, real_record_result
from test_cloud_response_recovery import legacy_cloud_routes

from test_cloud_entry_recovery import unrelated_reservation

__all__ = ['routed']


@pytest.mark.parametrize('barrier', ['budget', 'circuit'])
@pytest.mark.parametrize('prior', ['saved', 'unknown', 'fresh'])
def test_resident_next_pass_reaches_input_recovery(routed, monkeypatch, prior, barrier):
    ledger, cfg, _fake_calls, _reserves, _results = routed
    old(ledger)
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=?', (time.time() - 601,))
        ledger.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 601,))
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-entry-recovery', time.time())
    monkeypatch.setattr(cloud_policy, 'usage_reserver', real_usage_reserver)
    monkeypatch.setattr(cloud_policy, 'record_result', real_record_result)
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *a, **kw: True)
    monkeypatch.setattr(cloud_backlog, 'owner_alive', lambda: True)
    monkeypatch.setattr(cloud_backlog, 'request_slot', lambda *a: nullcontext(True))
    calls, local, recovered = [], [], []
    client = anthropic_client.AnthropicClient.__bases__[0]
    # routed replaces the client class; use the original class from its MRO.
    def request(self, method, body, timeout, **kw):
        calls.append(body)
        return 200, {}, json.dumps({'model': anthropic_client.MODEL, 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': '{"symptoms":[]}'}],
            'usage': {'input_tokens': 10, 'output_tokens': 2}}).encode()
    monkeypatch.setattr(client, 'request', request)
    monkeypatch.setattr(anthropic_client, 'AnthropicClient', client)
    pending = [prior]
    def extraction(body, **kwargs):
        provider = kwargs.get('provider')
        if provider is None:
            local.append(body)
        elif pending[0] == 'unknown':
            provider.reserve(provider.client.body('synthetic resident input', 100, None), 30)
            pending[0] = 'done'
        else:
            answer = provider('synthetic resident input', deadline=kwargs['deadline'], max_tokens=100)
            recovered.append(bool(answer.get('reused_response')))
        # Simulate suspension after accounting, before extraction persistence.
        return worker._DEFERRED
    monkeypatch.setattr(worker, 'llm_extract', extraction)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    if prior != 'fresh':
        with legacy_cloud_routes():
            first = worker.run_pending(ledger, limit=1, workers=1, budget_s=180, background=True)
        assert first['deferred'] == 1
        assert len(ledger.artifacts('cloud_usage_reserve')) == 1
    if prior == 'fresh':
        with legacy_cloud_routes():
            unrelated_reservation(ledger, cfg, 'reextract')
    before = len(calls)
    if barrier == 'budget':
        cfg['cloud_llm']['daily_cost_limit_usd'] = 0
    else:
        monkeypatch.setattr(cloud_policy, 'block_reason', lambda *a, **k: 'circuit_open')
    with legacy_cloud_routes():
        assert worker._cloud_mode(ledger, cfg, 'reextract', 1) == 'on'
        again = worker.run_pending(ledger, limit=1, workers=1, budget_s=180, background=True)
    assert again['deferred'] == 1 and len(calls) == before
    if prior == 'saved':
        assert recovered == [False, True] and not local
    elif prior == 'unknown':
        assert not recovered and not local
    else:
        assert len(local) == 1 and not recovered


def test_send_cap_does_not_reset_partial_cloud_chunks(routed, monkeypatch):
    ledger, cfg, _calls, _reserved, _results = routed
    old(ledger)
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=?', (time.time() - 601,))
        ledger.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 601,))
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-entry-recovery', time.time())
    cfg['cloud_llm']['daily_cost_limit_usd'] = 0
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *a, **kw: True)
    seen = []
    def extraction(body, **kwargs):
        assert kwargs.get('provider') is not None, 'a partial cloud post cannot restart locally at a cap'
        seen.append(body)
        kwargs['chunks_out'][0] = {'summary': 'synthetic validated earlier chunk'}
        return kwargs['provider']('synthetic unsent remainder', deadline=kwargs['deadline'], max_tokens=100)
    monkeypatch.setattr(worker, 'llm_extract', extraction)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    with legacy_cloud_routes():
        report = worker.run_pending(ledger, limit=1, workers=1, budget_s=180, background=True)
    assert report['deferred'] == 1 and report['done'] == 0 and len(seen) == 1
    assert not ledger.artifacts('cloud_usage_reserve')
