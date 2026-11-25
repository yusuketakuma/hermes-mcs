"""Boundary safety invariants of the cloud patient-context path, independent of its internals.

Only ``AnthropicClient.request`` is synthetic; reservation, accounting and the
retry decisions run for real. These must hold for any implementation:
a paused cloud never sends, and a request whose outcome is unknown is retried
only through the bounded, fully accounted cloud retry path.
"""
import json

import anthropic_client
import cloud_policy
from llm_retry import CLOUD_MAX_ATTEMPTS
from test_context_cloud_update_identity import cloud, real_cloud, runtime  # noqa: F401  (fixtures)
from test_patient_context_refresh import step


def _passes(state, cfg, count):
    return [step(state, cfg=cfg, local_admit=lambda: False) for _ in range(count)]


def test_paused_cloud_never_sends_or_reserves(runtime, real_cloud, monkeypatch):  # noqa: F811
    ledger, _, local_calls = runtime
    cfg, posts = real_cloud
    cloud_policy.stop(ledger)
    _passes(runtime, cfg, 6)
    assert posts == [] and not local_calls
    assert ledger.artifacts("cloud_usage_reserve") == []


def test_unknown_outcome_retry_is_bounded_and_recorded(runtime, real_cloud, monkeypatch):  # noqa: F811
    ledger, _, _ = runtime
    cfg, posts = real_cloud
    answered = anthropic_client.AnthropicClient.request
    bodies = []

    def request(client, method, body, timeout, **kwargs):
        bodies.append(json.dumps(body["messages"], sort_keys=True, ensure_ascii=False))
        if len(bodies) == 1:
            raise OSError("synthetic connection reset after the request may have left")
        return answered(client, method, body, timeout, **kwargs)

    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)
    _passes(runtime, cfg, 10)
    unknown = bodies[0]
    assert 1 < bodies.count(unknown) <= CLOUD_MAX_ATTEMPTS, "the unknown-outcome request did not follow the bounded retry path"
    reserves = ledger.artifacts("cloud_usage_reserve")
    results = [json.loads(row["meta"]) for row in ledger.artifacts("cloud_usage_result")]
    assert len(reserves) == len(bodies)
    assert sum(meta.get("http_class") == "transport" for meta in results) >= 1
