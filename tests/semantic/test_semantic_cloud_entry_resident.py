"""Resident semantic routing preserves paid/unknown inputs across a later cap."""
import json
import time

import pytest

import anthropic_client
import cloud_backlog
import cloud_policy
import semantic_drain
import semantic_transport as transport
from test_semantic_cloud import cloud, direct

from test_cloud_entry_recovery import unrelated_reservation

__all__ = ['cloud']


@pytest.mark.parametrize('barrier', ['budget', 'circuit'])
@pytest.mark.parametrize('prior', ['saved', 'unknown', 'fresh'])
def test_semantic_resident_next_pass_checks_old_input(cloud, monkeypatch, prior, barrier):
    ledger, cfg, job, calls = cloud
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *a, **kw: True)
    monkeypatch.setattr(cloud_backlog, 'owner_alive', lambda: True)
    monkeypatch.setattr(semantic_drain.semantic_store, 'refresh_cards_allowed', lambda *a: False)
    local, outputs, diagnostics = [], [], {}
    class Interrupted(BaseException):
        pass
    original = anthropic_client.AnthropicClient.request
    def request(self, *args, **kwargs):
        if prior == 'unknown':
            raise Interrupted()
        return original(self, *args, **kwargs)
    monkeypatch.setattr(anthropic_client.AnthropicClient, 'request', request)
    def stages(ledger, scfg, selected, jev, llm, deadline, **kwargs):
        transport.set_inference_target(selected['message_id'], 'facts')
        outputs.append(llm('synthetic historical input', timeout=30))
        return 'done'
    monkeypatch.setattr(semantic_drain, '_process_job_inner', stages)
    def local_call(prompt, timeout=None):
        local.append(prompt)
        return '{"facts":[]}'
    def resident():
        return semantic_drain._process_job(ledger, semantic_drain.runtime_config(cfg)[0], job, None,
            local_call, time.monotonic() + 180, lane='backlog', diagnostics=diagnostics, cloud_transport=direct)
    if prior == 'saved':
        assert resident() == 'done'
    elif prior == 'unknown':
        with pytest.raises(Interrupted):
            resident()
    if prior == 'fresh':
        unrelated_reservation(ledger, cfg, 'semantic_backlog')
    before = len(calls)
    if barrier == 'budget':
        cfg['cloud_llm']['daily_cost_limit_usd'] = 0
    else:
        monkeypatch.setattr(cloud_policy, 'block_reason', lambda *a, **k: 'circuit_open')
    if prior == 'unknown':
        assert resident() == 'deferred'
        assert diagnostics['not_sent'] == 'cloud_unit_unresolved' and not local
    else:
        assert resident() == 'done'
        assert len(local) == int(prior == 'fresh')
        if prior == 'saved':
            assert json.loads(outputs[0]) == json.loads(outputs[1]) == {'facts': []}
    assert len(calls) == before
    assert len(ledger.artifacts('cloud_usage_reserve')) == 1


@pytest.mark.parametrize('barrier', ['budget', 'circuit'])
def test_cloud_only_fresh_denial_is_wait_without_paid_failure(cloud, monkeypatch, barrier):
    ledger, cfg, job, calls = cloud
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *a, **kw: True)
    monkeypatch.setattr(cloud_backlog, 'owner_alive', lambda: True)
    if barrier == 'budget':
        cfg['cloud_llm']['daily_cost_limit_usd'] = 0
    else:
        monkeypatch.setattr(cloud_policy, 'block_reason', lambda *a, **k: 'circuit_open')
    diagnostics = {}
    def stages(ledger, scfg, selected, jev, llm, deadline, **kwargs):
        transport.set_inference_target(selected['message_id'], 'facts')
        llm('synthetic unsent input', timeout=30)
        pytest.fail('a refused new request must not finish a generation')
    monkeypatch.setattr(semantic_drain, '_process_job_inner', stages)
    status = semantic_drain._process_job(ledger, semantic_drain.runtime_config(cfg)[0], job, None,
        lambda *a, **k: pytest.fail('cloud-only denial cannot call local'), time.monotonic() + 180,
        lane='backlog', diagnostics=diagnostics, cloud_transport=direct, cloud_only=True)
    assert status == 'deferred' and diagnostics['not_sent'] == 'cloud_not_sent'
    assert not calls and not ledger.artifacts('cloud_usage_reserve')
    assert not ledger.artifacts('cloud_usage_result') and not ledger.artifacts('cloud_usage_disposition')
