"""Synthetic long answers retain accounting and exact replay without a second send."""
from contextlib import closing
import json
import time

import pytest
import anthropic_client
import bounded_http
import cloud_client
import cloud_inference
from ledger import Ledger
from mcs_util import payload_hash
from test_cloud_response_recovery import legacy_cloud_routes, paid as paid


@pytest.mark.parametrize('text', ['x' * 270000, '\x00' * 200000])
def test_long_answer_is_settled_saved_and_recovered(paid, text):
    path, _cfg, _calls, _locks, _state, unit, _restart = paid
    sent = []
    with closing(Ledger(path)) as ledger:
        provider = unit(ledger, purpose='quality_repair')
        def post(body, timeout):
            sent.append(True)
            return 200, {}, json.dumps({'model': provider.client.model, 'stop_reason': 'end_turn',
                'content': [{'type': 'text', 'text': text}],
                'usage': {'input_tokens': 10, 'output_tokens': 30000}}).encode()
        provider.client._post_fn = post
        with legacy_cloud_routes():
            answer = provider('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=65536)
        assert answer['text'] == text
        assert len(ledger.artifacts('cloud_usage_result')) == 1
        row = ledger.artifacts('cloud_usage_reserve')[0]
        reservation = json.loads(row['meta'])
        proof = cloud_inference.verify_cloud_repair_evidence(ledger.db,
            reservation_artifact_id=row['artifact_id'], reservation_id=reservation['reservation_id'],
            route='reextract', project_id=1, message_id=1, unit_key='synthetic-unit',
            owner={'lease': 'first'}, prompt='synthetic prompt', answer_text=text,
            contract=cloud_inference.cloud_provider_contract(selection=provider.selection), purpose='quality_repair')
        assert proof['reservation']['reservation_id'] == reservation['reservation_id']
        recovered = unit(ledger, owner='restart', purpose='quality_repair')('synthetic prompt',
                    deadline=time.monotonic() + 60, max_tokens=65536)
        assert recovered['reused_response'] and recovered['text'] == text
        assert sent == [True]


@pytest.mark.parametrize("backend", ["anthropic", "openai"])
def test_dispatched_transport_receives_per_request_bound(paid, monkeypatch, backend):
    path, cfg, _calls, _locks, _state, unit, _restart = paid
    cfg['cloud_llm']['provider'] = backend
    with closing(Ledger(path)) as ledger:
        real_client = type(unit(ledger).client)
        monkeypatch.setattr(anthropic_client, 'AnthropicClient', real_client)
        seen = []
        def request(self, method, body, timeout, *, deadline, max_bytes=bounded_http.MAX_RESPONSE_BYTES):
            seen.append((max_bytes, cloud_client.response_limit(body.get("max_tokens", body.get("max_output_tokens")))))
            if backend == "openai":
                return 200, {}, json.dumps({"model": self.model, "status": "completed", "service_tier": "default",
                    "output": [{"type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": "synthetic"}]}],
                    "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                              "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}}).encode()
            return 200, {}, json.dumps({'model': self.model, 'stop_reason': 'end_turn',
                'content': [{'type': 'text', 'text': 'synthetic'}],
                'usage': {'input_tokens': 10, 'output_tokens': 2}}).encode()
        monkeypatch.setattr(real_client, 'request', request)
        provider = cloud_inference.CloudInferenceUnit(ledger, cfg, route='reextract', project_id=1,
            message_id=1, unit_key='synthetic-transport', deadline=time.monotonic() + 120,
            current=lambda: True, dispatch=lambda fn, **kw: fn(), owner={'lease': 'synthetic'},
            input_binding=payload_hash('synthetic'), transport=lambda fn, *a, **kw: fn(*a, **kw))
        with legacy_cloud_routes():
            provider('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=32768)
        assert len(seen) == 1 and seen[0][0] == seen[0][1]
        assert seen[0][0] > bounded_http.MAX_RESPONSE_BYTES
