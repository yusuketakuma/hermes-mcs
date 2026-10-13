"""Malformed synthetic Jev bodies retain the bounded protocol-error contract."""
import json
import time

import pytest

import semantic_jev as jev


def _deep_json():
    return b"[" * 10000 + b"0" + b"]" * 10000


def test_deep_response_retries_within_attempt_ceiling_and_recovers(monkeypatch, recursion_limited_json):
    deep = _deep_json()
    with pytest.raises(RecursionError):
        json.loads(deep)
    valid = json.dumps({"model": jev.JEV_MODEL,
                        "answers": {"q": {"type": "noul", "noul": .9}},
                        "usage": {"input_tokens": 2, "output_tokens": 1}}).encode()
    replies = iter([deep, valid])
    client = jev.JevClient(api_key="synthetic", max_attempts=2,
                           post_fn=lambda *_: (200, {}, next(replies)))
    monkeypatch.setattr(jev.time, "sleep", lambda _: None)
    out = client.evaluate({}, {"q": jev.noul_question("synthetic", "yes", "no")},
                           time.monotonic() + 60)
    assert out["answers"]["q"]["noul"] == .9
    assert client.requests_made == 2
    assert client.usage_totals["reported_requests"] == 1
    assert client.last_error is None


def test_deep_response_stops_at_attempt_ceiling(monkeypatch, recursion_limited_json):
    client = jev.JevClient(api_key="synthetic", max_attempts=2,
                           post_fn=lambda *_: (200, {}, _deep_json()))
    monkeypatch.setattr(jev.time, "sleep", lambda _: None)
    with pytest.raises(jev.JevError) as raised:
        client.evaluate({}, {"q": jev.noul_question("synthetic", "yes", "no")},
                        time.monotonic() + 60)
    assert (raised.value.kind, raised.value.detail) == ("protocol_error", "not_json")
    assert raised.value.retryable is True
    assert client.requests_made == 2
    assert client.last_error is raised.value
    assert client.usage_totals["reported_requests"] == 0


def test_deep_models_response_is_fixed_protocol_error(monkeypatch, recursion_limited_json):
    client = jev.JevClient(api_key="synthetic")
    monkeypatch.setattr(client, "_http_request", lambda *a, **k: (200, {}, _deep_json()))
    with pytest.raises(jev.JevError) as raised:
        client.models()
    assert (raised.value.kind, raised.value.detail) == ("protocol_error", "not_json")
    assert raised.value.retryable is False
