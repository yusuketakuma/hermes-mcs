"""The real patient worker reaches recovery before its provider request budget."""
from contextlib import nullcontext
import json
import time

import pytest

import anthropic_client
import cloud_backlog
import cloud_inference
import cloud_policy
import extract_llm
import patient_context_refresh as refresh
from test_patient_context_refresh import CFG, runtime, seed, step

from test_cloud_entry_recovery import unrelated_reservation

from test_anthropic_context_refresh import CFG as CLOUD_CFG, cloud as partial_cloud, step as cloud_step

__all__ = ['runtime', 'partial_cloud']


@pytest.mark.parametrize('barrier', ['budget', 'circuit'])
@pytest.mark.parametrize('prior', ['saved', 'unknown', 'fresh'])
def test_patient_resident_next_pass_checks_old_input(runtime, monkeypatch, prior, barrier):
    ledger, clock, local = runtime
    seed(ledger, body='完全合成の住環境の記録。')
    clock.mono = time.monotonic()
    cfg = {**CFG, 'cloud_llm': {'mode': 'on', 'provider': 'anthropic', 'project_ids': [1]}}
    monkeypatch.setattr(extract_llm, 'load_config', lambda: cfg)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(cloud_policy, 'selected_key', lambda *a: 'synthetic-key')
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *a, **kw: True)
    monkeypatch.setattr(cloud_inference.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(extract_llm, '_WRITER_STATE', (False, 1))
    monkeypatch.setattr(extract_llm.extract, '_WRITER_STATE', (False, 1))
    monkeypatch.setattr(cloud_backlog, 'owner_alive', lambda: True)
    monkeypatch.setattr(cloud_backlog, 'request_slot', lambda *a: nullcontext(True))
    calls, answers = [], []
    class Interrupted(BaseException):
        pass
    def request(self, method, body, timeout, **kwargs):
        if prior == 'unknown':
            raise Interrupted()
        calls.append(body)
        return 200, {}, json.dumps({'model': anthropic_client.MODEL, 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': '{"patient_context":[]}'}],
            'usage': {'input_tokens': 10, 'output_tokens': 2}}).encode()
    monkeypatch.setattr(anthropic_client.AnthropicClient, 'request', request)
    def scan(cfg, source, saved, night_id, deadline, admit, *, provider=None, **kwargs):
        answers.append(refresh._ask(cfg, {'source': 'synthetic resident input'}, deadline, admit, provider=provider))
        # Suspend after accounting before persisting this source's scan cursor.
        raise refresh.Deferred('synthetic_after_answer')
    monkeypatch.setattr(refresh, '_scan', scan)
    if prior == 'saved':
        assert step(runtime, cfg=cfg)['status'] == 'deferred'
        assert len(calls) == 1
    elif prior == 'unknown':
        with pytest.raises(Interrupted):
            step(runtime, cfg=cfg)
    if prior == 'fresh':
        unrelated_reservation(ledger, cfg, 'patient_context')
    before = len(calls)
    if barrier == 'budget':
        cfg['cloud_llm']['daily_cost_limit_usd'] = 0
    else:
        monkeypatch.setattr(cloud_policy, 'block_reason', lambda *a, **k: 'circuit_open')
    again = step(runtime, cfg=cfg)
    assert again['status'] == 'deferred' and len(calls) == before
    assert len(ledger.artifacts('cloud_usage_reserve')) == 1
    if prior == 'saved':
        assert answers == [{'patient_context': []}, {'patient_context': []}] and not local
    elif prior == 'unknown':
        assert not answers and not local
    else:
        assert len(local) == len(answers) == 1


def test_send_cap_does_not_reset_a_persisted_cloud_cursor(runtime, partial_cloud, monkeypatch):
    import patient_context_plan as plan
    ledger, _clock, local = runtime
    seed(ledger, body='見出しのない完全合成の居住記録。')
    snapshot = refresh._snapshot(ledger.db, 1, CLOUD_CFG)
    meta = {'source_sig': plan.source_signature(snapshot['sources'][0]),
            'contract': snapshot['contract'], 'provider': snapshot['contract']['provider']}
    with ledger.db:
        refresh._replace(ledger, refresh.SCAN, 1,
            {'cursor': 1, 'chunks': 2, 'complete': False, 'facts': []}, meta, 1)
    seen = []
    original = cloud_inference.CloudInferenceUnit
    class Blocked(original):
        def __call__(self, prompt, **kwargs):
            seen.append(prompt)
            assert not self.possibly_sent
            raise cloud_inference.CloudFallback('cloud_send_blocked')
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', Blocked)
    result = cloud_step(runtime)
    assert result['status'] == 'deferred' and result['reason'] == 'nightly_cloud_budget'
    assert not local and len(seen) == 1
    saved = refresh._current_scans(ledger.db, 1, snapshot['sources'], snapshot['contract'])[1]
    assert saved['_provider_contract'] == snapshot['contract']['provider'] and saved['cursor'] == 1
