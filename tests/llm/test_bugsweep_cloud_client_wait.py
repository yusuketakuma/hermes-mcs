"""Synthetic regressions: the cloud attempt loop never waits for a retry that cannot be sent."""
import json

import pytest

import anthropic_client
import cloud_client
import openai_client


def _anthropic_reply():
    return {"model": anthropic_client.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"synthetic":true}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}


def _clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(cloud_client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(cloud_client.time, "sleep", lambda _: pytest.fail("real sleep was used"))
    return now


@pytest.mark.parametrize("provider,status", [("anthropic", 529), ("openai", 500), ("openai", 503)])
def test_cloud_5xx_is_retried_to_the_shared_cap_each_with_its_own_reservation(monkeypatch, provider, status):
    """New bounded-retry contract: a 5xx is retried up to CLOUD_MAX_ATTEMPTS total
    sends (1 initial + 5 retries), every attempt independently reserved and
    resulted, never with a real sleep and never leaking the response body."""
    _clock(monkeypatch)
    reserved, waits, results = [], [], []
    make = (anthropic_client.AnthropicClient if provider == "anthropic"
            else openai_client.OpenAIClient)
    client = make("synthetic", post_fn=lambda *args: (status, {}, b"private body"), wait_fn=waits.append)
    with pytest.raises(cloud_client.CloudError) as caught:
        client.complete("synthetic", deadline=300, reserve_fn=lambda *a: reserved.append(1) or len(reserved),
                        after_result=lambda r, meta, **_answer: results.append(meta))
    assert caught.value.kind == "transport" and caught.value.status == status
    assert not isinstance(caught.value, cloud_client.CloudNotSent)
    assert reserved == [1] * cloud_client.CLOUD_MAX_ATTEMPTS
    assert len(waits) == cloud_client.CLOUD_MAX_ATTEMPTS - 1
    assert all(type(w) in (int, float) and w >= 0 for w in waits)
    # every possibly billed attempt keeps its full-charge result row
    assert len(results) == cloud_client.CLOUD_MAX_ATTEMPTS
    for meta in results:
        assert meta["http_class"] == f"http_{status}"
        assert meta["usage"] is None and meta["error_kind"] == "transport"
        assert "private" not in str(meta)


@pytest.mark.parametrize("provider,status", [("anthropic", 401), ("anthropic", 403), ("openai", 402), ("openai", 400)])
def test_cloud_auth_payment_and_contract_errors_are_never_resent(monkeypatch, provider, status):
    """Safety side of the contract: auth, payment and contract failures of the
    same input are never automatically resent — one attempt, one result row."""
    _clock(monkeypatch)
    reserved, waits, results = [], [], []
    make = (anthropic_client.AnthropicClient if provider == "anthropic"
            else openai_client.OpenAIClient)
    client = make("synthetic", post_fn=lambda *args: (status, {}, b"private body"), wait_fn=waits.append)
    with pytest.raises(cloud_client.CloudError) as caught:
        client.complete("synthetic", deadline=300, reserve_fn=lambda *a: reserved.append(1) or len(reserved),
                        after_result=lambda r, meta, **_answer: results.append(meta))
    assert reserved == [1] and waits == [] and len(results) == 1
    assert caught.value.status == status


def test_retry_after_beyond_the_deadline_returns_429_without_waiting(monkeypatch):
    _clock(monkeypatch)
    posts, waits = [], []
    client = anthropic_client.AnthropicClient(
        "synthetic", post_fn=lambda *args: posts.append(1) or (429, {"Retry-After": "600"}, b""),
        wait_fn=waits.append)
    with pytest.raises(cloud_client.CloudError, match="rate_limited"):
        client.complete("synthetic", deadline=300, reserve_fn=lambda *a: {"r": 1},
                        after_result=lambda *a, **_answer: None)
    assert posts == [1] and waits == []


def test_retry_after_within_the_deadline_still_waits_and_retries(monkeypatch):
    now = _clock(monkeypatch)
    replies = iter([(429, {"Retry-After": "30"}, b""),
                    (200, {}, json.dumps(_anthropic_reply()).encode())])
    reserved, waits = [], []

    def wait(delay):
        waits.append(delay)
        now[0] += delay

    client = anthropic_client.AnthropicClient("synthetic", post_fn=lambda *args: next(replies), wait_fn=wait)
    answer = client.complete("synthetic", deadline=300, reserve_fn=lambda *a: reserved.append(1) or len(reserved),
                             after_result=lambda *a, **_answer: None)
    assert answer["status"] == 200 and waits == [30.0] and reserved == [1, 1]
