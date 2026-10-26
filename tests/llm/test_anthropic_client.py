"""Synthetic Messages wire contract, attempt hooks and safe failure metadata."""
import json
import time

import pytest

import anthropic_client as a


def _reply(**values):
    return {"model": a.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": "ignored"},
                        {"type": "text", "text": '{"synthetic":true}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5}, **values}


def test_fixed_body_and_shared_chat_envelope():
    seen, events = [], []

    def post(body, timeout):
        seen.append(body)
        events.append("post")
        return 200, {"Request-Id": "req_synthetic"}, json.dumps(_reply()).encode()

    client = a.AnthropicClient("synthetic", post_fn=post)
    result = client.complete("synthetic input", deadline=time.monotonic() + 10,
                             before_attempt=lambda: events.append("guard"),
                             reserve_fn=lambda body, timeout: events.append("reserve") or {"r": 1},
                             after_result=lambda r, meta: events.append((r, meta)))
    assert events[:4] == ["guard", "reserve", "guard", "post"]
    assert events[4][0] == {"r": 1}
    assert events[4][1]["request_id"] == "req_synthetic"
    assert "synthetic input" not in json.dumps(events[4])
    assert seen[0] == {"model": a.MODEL, "max_tokens": 8192, "system": a.SYSTEM,
                       "output_config": {"effort": "low"},
                       "messages": [{"role": "user", "content": "synthetic input"}]}
    assert result["text"] == '{"synthetic":true}'
    assert result["finish_reason"] == "stop" and result["status"] == 200


@pytest.mark.parametrize("stop", ["max_tokens", "stop_sequence", "refusal", None])
def test_only_end_turn_is_accepted_but_usage_is_reported(stop):
    results = []
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (
        200, {}, json.dumps(_reply(stop_reason=stop)).encode()))
    with pytest.raises(a.AnthropicError, match="contract_error"):
        client.complete("synthetic", deadline=time.monotonic() + 10,
                        after_result=lambda r, meta: results.append(meta))
    assert len(results) == 1 and results[0]["usage"]["input_tokens"] == 10


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": True, "output_tokens": 1},
                                  {"input_tokens": 1, "output_tokens": -1},
                                  {"input_tokens": 1, "output_tokens": 2, "new_charge": 3}])
def test_unknown_usage_is_never_a_zero_cost_success(usage):
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (
        200, {}, json.dumps(_reply(usage=usage)).encode()))
    with pytest.raises(a.AnthropicError, match="pricing_unverified"):
        client.complete("synthetic", deadline=time.monotonic() + 10)


def test_second_guard_rejection_never_dispatches_and_keeps_reservation():
    events = []

    def guard():
        events.append("guard")
        if len(events) == 3:
            raise RuntimeError("scope changed while waiting for DB")

    client = a.AnthropicClient("synthetic", post_fn=lambda *args: pytest.fail("not sent"))
    with pytest.raises(RuntimeError):
        client.complete("synthetic", deadline=time.monotonic() + 10, before_attempt=guard,
                        reserve_fn=lambda *args: events.append("reserve"))
    assert events == ["guard", "reserve", "guard"]


def test_each_retry_reserves_and_accounts_transport_failures(monkeypatch):
    monkeypatch.setattr(a.time, "sleep", lambda delay: None)
    reservations, results, calls = [], [], []

    def post(*args):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("private response must not appear")
        return 200, {}, json.dumps(_reply()).encode()

    def reserve(*args):
        reservations.append(len(reservations))
        return reservations[-1]

    client = a.AnthropicClient("synthetic", post_fn=post)
    client.complete("synthetic", deadline=time.monotonic() + 10, reserve_fn=reserve,
                    after_result=lambda r, meta: results.append((r, meta)))
    assert reservations == [0, 1]
    assert results[0][1]["usage"] is None and results[0][1]["error_kind"] == "transport"
    assert results[1][1]["usage"]["output_tokens"] == 5
    assert "private response" not in json.dumps(results)


@pytest.mark.parametrize("status,kind,retries", [(401,"auth_error",1), (403,"auth_error",1),
                                                (402,"payment_required",1), (400,"contract_error",1),
                                                (429,"rate_limited",3), (529,"transport",3),
                                                (503,"transport",3)])
def test_shared_jev_retry_classes(monkeypatch, status, kind, retries):
    monkeypatch.setattr(a.time, "sleep", lambda delay: None)
    calls = []
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (
        calls.append(1) or status, {"Retry-After": "0"}, b"private error body"))
    with pytest.raises(a.AnthropicError, match=kind):
        client.complete("synthetic", deadline=time.monotonic() + 10)
    assert len(calls) == retries


@pytest.mark.parametrize("request_id,expected", [("req_test-ABC_12", "req_test-ABC_12"),
    ("x" * 64, "x" * 64), ("x" * 65, None), ("req.with.dot", None),
    ("req/fragment", None), ("req synthetic", None), ("req_非ASCII", None), ("", None)])
def test_request_id_has_only_the_bounded_provider_contract(request_id, expected):
    observed = []
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (
        200, {"request-id": request_id}, json.dumps(_reply()).encode()))
    client.complete("synthetic", deadline=time.monotonic() + 10,
                    after_result=lambda reservation, metadata: observed.append(metadata))
    assert observed[0]["request_id"] == expected


def test_only_known_unpriced_usage_metadata_is_accepted():
    usage = {"input_tokens": 10, "output_tokens": 5, "service_tier": "standard",
             "inference_geo": "global", "server_tool_use": {"web_search_requests": 0}}
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (
        200, {}, json.dumps(_reply(usage=usage)).encode()))
    assert client.complete("synthetic", deadline=time.monotonic() + 10)["usage"]["input_tokens"] == 10
    for extra in ({"service_tier": "priority"}, {"inference_geo": "us"}, {"new_charge_usd": "10"},
                  {"service_tier": {"tier": "standard"}}, {"inference_geo": ["global"]}, {"service_tier": True},
                  {"server_tool_use": {"web_search_requests": 1}}, {"unknown_flag": None}):
        client = a.AnthropicClient("synthetic", post_fn=lambda *args, e=extra: (
            200, {}, json.dumps(_reply(usage={"input_tokens": 1, "output_tokens": 1, **e})).encode()))
        with pytest.raises(a.AnthropicError, match="pricing_unverified"):
            client.complete("synthetic", deadline=time.monotonic() + 10)


def test_a_dated_model_id_is_not_assumed_to_share_the_price():
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (
        200, {}, json.dumps(_reply(model=a.MODEL + "-20261001")).encode()))
    with pytest.raises(a.AnthropicError, match="model_mismatch"):
        client.complete("synthetic", deadline=time.monotonic() + 10)


def test_retry_after_alone_paces_retries(monkeypatch):
    slept = []
    monkeypatch.setattr(a.time, "sleep", slept.append)
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (429, {"Retry-After": "2"}, b""))
    with pytest.raises(a.AnthropicError, match="rate_limited"):
        client.complete("synthetic", deadline=time.monotonic() + 60)
    assert slept == [2, 2]


def test_a_deadline_after_the_reservation_settles_it_as_unsent(monkeypatch):
    results, now = [], [0.0]
    monkeypatch.setattr(a.time, "monotonic", lambda: now[0])

    def reserve(*args):
        now[0] = 20.0          # the DB wait outlived the call's deadline
        return {"r": 1}
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: pytest.fail("not sent"))
    with pytest.raises(a.AnthropicError, match="budget_exceeded"):
        client.complete("synthetic", deadline=10.0, reserve_fn=reserve,
                        after_result=lambda r, meta: results.append((r, meta)))
    assert results == [({"r": 1}, {**results[0][1], "http_class": "not_sent"})]


def test_a_validated_answer_arriving_at_the_deadline_is_kept(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(a.time, "monotonic", lambda: now[0])

    def post(*args):
        now[0] = 11.0          # paid and validated just past the HTTP deadline
        return 200, {}, json.dumps(_reply()).encode()
    result = a.AnthropicClient("synthetic", post_fn=post).complete("synthetic", deadline=10.0)
    assert result["status"] == 200


def test_an_unexpected_transport_exception_still_reports_its_result():
    results = []

    def post(*args):
        raise ValueError("synthetic worker failure")
    with pytest.raises(a.AnthropicError, match="transport"):
        a.AnthropicClient("synthetic", post_fn=post, max_attempts=1).complete(
            "synthetic", deadline=time.monotonic() + 10, reserve_fn=lambda *args: {"r": 1},
            after_result=lambda r, meta: results.append(meta))
    assert results[0]["error_kind"] == "transport" and results[0]["usage"] is None
