"""Boundary safety invariants of the cloud patient-context path, independent of its internals.

Only ``AnthropicClient.request`` is synthetic; reservation, accounting and the
retry decisions run for real. These must hold for any implementation:
a paused cloud never sends, and a request whose outcome is unknown is never
sent again.
"""
import json

import anthropic_client
import cloud_policy
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


def test_unknown_outcome_is_resent_once_within_the_cap(runtime, real_cloud, monkeypatch):  # noqa: F811
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
    # bounded retry contract (owner 2026-11-26): the unknown-outcome send is retried
    # in-pass and the retry succeeds, so the same body is sent exactly twice.
    assert bodies.count(unknown) == 2, "the unknown-outcome send was retried more than the contract allows"
    reserves = ledger.artifacts("cloud_usage_reserve")
    results = [json.loads(row["meta"]) for row in ledger.artifacts("cloud_usage_result")]
    assert len(reserves) == len(bodies)
    assert sum(meta.get("http_class") == "transport" for meta in results) == 1
