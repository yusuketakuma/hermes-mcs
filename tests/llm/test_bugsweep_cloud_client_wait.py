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
def test_cloud_5xx_is_returned_without_waiting_or_a_second_reservation(monkeypatch, provider, status):
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
    assert reserved == [1] and waits == []
    # the possibly billed attempt keeps its full-charge result row
    assert len(results) == 1 and results[0]["http_class"] == f"http_{status}"
    assert results[0]["usage"] is None and results[0]["error_kind"] == "transport"


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
