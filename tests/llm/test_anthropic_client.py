"""Synthetic Messages wire contract, attempt hooks and safe failure metadata."""
import hashlib
import json
import time

import pytest

import anthropic_client as a
import cloud_client
from test_semantic_cloud import cloud

__all__ = ["cloud"]


def _reply(**values):
    return {"model": a.MODEL, "stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": "ignored"},
                        {"type": "text", "text": '{"synthetic":true}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5}, **values}


def test_success_hash_covers_returned_text_not_raw_response():
    answer = '合成応答 synthetic-canary'
    raw = json.dumps(_reply(content=[{'type': 'thinking', 'thinking': 'untrusted'},
                                    {'type': 'text', 'text': answer}]), ensure_ascii=False).encode()
    recorded = []
    provider = a.AnthropicClient('synthetic', post_fn=lambda *args: (200, {}, raw))
    result = provider.complete('synthetic', deadline=time.monotonic() + 10,
                               after_result=lambda _reservation, meta: recorded.append(meta))
    assert result['text'] == answer
    assert recorded[0]['answer_text_sha256_v1'] == hashlib.sha256(answer.encode('utf-8')).hexdigest()
    assert recorded[0]['answer_finish_reason_v1'] == 'stop'
    assert answer not in json.dumps(recorded[0], ensure_ascii=False)


def test_provider_error_identity_and_neutral_base_are_compatible():
    assert a.AnthropicError is cloud_client.CloudError
    assert a.AnthropicClient.__bases__ == (cloud_client.CloudClient,)


@pytest.mark.parametrize("kind", ["duplicate", "nan", "infinity"])
def test_success_with_non_strict_json_is_never_accepted(kind):
    raw = json.dumps(_reply()).encode()
    if kind == "duplicate":
        raw = raw.replace(b'"input_tokens": 10', b'"input_tokens": 10, "input_tokens": 10', 1)
    else:
        value = b"NaN" if kind == "nan" else b"1e9999"
        raw = raw.replace(b'{"model":', b'{"synthetic_invalid":' + value + b',"model":', 1)
    calls, recorded = [], []
    provider = a.AnthropicClient("synthetic", post_fn=lambda *args: (calls.append(True) or 200, {}, raw))
    with pytest.raises(a.AnthropicError, match="protocol_error"):
        provider.complete("synthetic", deadline=time.monotonic() + 10,
                          after_result=lambda _reservation, meta: recorded.append(meta))
    assert len(calls) == len(recorded) == 1
    assert recorded[0]["http_class"] == "http_200" and recorded[0]["usage"] is None


def test_proven_unsent_timeout_is_recorded_once_as_not_sent():
    calls, recorded = [], []
    def post(*args):
        calls.append(True)
        raise a.bounded_http.NotSentTimeout("synthetic")
    provider = a.AnthropicClient("synthetic", post_fn=post)
    with pytest.raises(a.AnthropicError, match="budget_exceeded"):
        provider.complete("synthetic", deadline=time.monotonic() + 10,
                          after_result=lambda _reservation, meta: recorded.append(meta))
    assert len(calls) == len(recorded) == 1
    assert recorded[0]["http_class"] == "not_sent" and recorded[0]["usage"] is None


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


@pytest.mark.parametrize("stop", ["stop_sequence", "refusal", None])
def test_only_end_turn_is_accepted_but_usage_is_reported(stop):
    results = []
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (
        200, {}, json.dumps(_reply(stop_reason=stop)).encode()))
    with pytest.raises(a.AnthropicError, match="contract_error"):
        client.complete("synthetic", deadline=time.monotonic() + 10,
                        after_result=lambda r, meta: results.append(meta))
    assert len(results) == 1 and results[0]["usage"]["input_tokens"] == 10


def test_a_max_tokens_stop_is_a_paid_length_answer_not_a_failure():
    results = []
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (
        200, {}, json.dumps(_reply(stop_reason="max_tokens")).encode()))
    result = client.complete("synthetic", deadline=time.monotonic() + 10,
                             after_result=lambda r, meta: results.append(meta))
    assert result["finish_reason"] == "length" and result["status"] == 200
    assert results[0]["error_kind"] is None and results[0]["usage"]["output_tokens"] == 5
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (
        200, {}, json.dumps(_reply(stop_reason="max_tokens", content=[])).encode()))
    with pytest.raises(a.AnthropicError, match="contract_error"):
        client.complete("synthetic", deadline=time.monotonic() + 10)


@pytest.mark.parametrize("types", [("thinking",), ("redacted_thinking",),
                                    ("thinking", "redacted_thinking")],
                         ids=["thinking", "redacted", "both"])
def test_thinking_only_max_tokens_is_one_paid_length_response(types):
    calls, results = [], []
    content = [{"type": kind, "thinking" if kind == "thinking" else "data":
                "synthetic-private-thinking"} for kind in types]
    for block in content:
        if block["type"] == "thinking":
            block["signature"] = "synthetic-private-signature"

    def post(body, timeout):
        calls.append(body)
        return 200, {}, json.dumps(_reply(stop_reason="max_tokens", content=content)).encode()

    client = a.AnthropicClient("synthetic", post_fn=post)
    result = client.complete("synthetic", deadline=time.monotonic() + 10,
                             reserve_fn=lambda *args: "synthetic-reservation",
                             after_result=lambda reservation, meta: results.append((reservation, meta)))
    assert result["status"] == 200 and result["finish_reason"] == "length" and result["text"] == ""
    assert len(calls) == len(results) == 1 and results[0][0] == "synthetic-reservation"
    assert results[0][1]["error_kind"] is None
    assert result["usage"] == results[0][1]["usage"] and result["usage"]["output_tokens"] == 5
    assert "synthetic-private-thinking" not in json.dumps((result, results))
    assert "synthetic-private-signature" not in json.dumps((result, results))


@pytest.mark.parametrize("stop,include_text", [("end_turn", True), ("max_tokens", False)])
def test_empty_signed_thinking_is_accepted_without_exposing_opaque_content(stop, include_text):
    calls, results = [], []
    signature = "synthetic-private-signature"
    content = [{"type": "thinking", "thinking": "", "signature": signature}]
    if include_text:
        content.append({"type": "text", "text": '{"synthetic":true}'})

    def post(body, timeout):
        calls.append(body)
        return 200, {}, json.dumps(_reply(stop_reason=stop, content=content)).encode()

    result = a.AnthropicClient("synthetic", post_fn=post).complete(
        "synthetic", deadline=time.monotonic() + 10,
        reserve_fn=lambda *args: "synthetic-reservation",
        after_result=lambda reservation, meta: results.append((reservation, meta)))
    assert result["text"] == ('{"synthetic":true}' if include_text else "")
    assert result["finish_reason"] == ("stop" if include_text else "length")
    assert len(calls) == len(results) == 1 and results[0][0] == "synthetic-reservation"
    assert results[0][1]["error_kind"] is None and results[0][1]["usage"] == result["usage"]
    assert signature not in json.dumps((result, results))


def test_signed_thinking_is_not_saved_in_usage_records(cloud, monkeypatch):
    import cloud_policy
    from test_semantic_cloud import rows

    ledger, cfg, _, _ = cloud
    monkeypatch.setattr(cloud_policy, "pricing_valid", lambda *args, **kwargs: True)
    signature, thinking = "synthetic-private-signature", "synthetic-private-thinking"
    content = [{"type": "thinking", "thinking": "", "signature": signature},
               {"type": "thinking", "thinking": thinking, "signature": signature},
               {"type": "text", "text": '{"synthetic":true}'}]
    calls = []

    def request(self, method, body, timeout, **kwargs):
        calls.append(True)
        return 200, {}, json.dumps(_reply(content=content)).encode()

    monkeypatch.setattr(a.AnthropicClient, "request", request)
    client = a.AnthropicClient("synthetic")
    result = client.complete(
        "synthetic", deadline=time.monotonic() + 10, max_tokens=100,
        reserve_fn=cloud_policy.usage_reserver(ledger, cfg, route="reextract", project_id=1, unit_key="synthetic"),
        after_result=lambda reservation, meta: cloud_policy.record_result(ledger, reservation, meta))
    reserves = rows(ledger, "cloud_usage_reserve")
    results = rows(ledger, "cloud_usage_result")
    assert calls == [True] and len(reserves) == len(results) == 1
    assert result["text"] == '{"synthetic":true}' and result["finish_reason"] == "stop"
    saved = json.loads(results[0]["meta"])
    assert saved["error_kind"] is None and saved["usage"] == result["usage"] and float(saved["cost_usd"]) > 0
    recorded = json.dumps((result, [dict(row) for row in reserves + results]))
    assert signature not in recorded and thinking not in recorded


@pytest.mark.parametrize("stop,content,usage,kind", [
    ("end_turn", [{"type": "thinking", "thinking": "synthetic"}], None, "contract_error"),
    ("end_turn", [{"type": "thinking", "thinking": "", "signature": "synthetic"}], None, "contract_error"),
    ("end_turn", [{"type": "text", "text": ""}], None, "contract_error"),
    ("end_turn", [{"type": "text", "text": "  "}], None, "contract_error"),
    ("max_tokens", [], None, "contract_error"),
    ("max_tokens", [{"type": "tool_use"}], None, "protocol_error"),
    ("max_tokens", [{"type": "thinking"}], {"input_tokens": 1, "output_tokens": -1}, "pricing_unverified"),
])
def test_empty_or_unverified_responses_do_not_gain_the_paid_length_contract(stop, content, usage, kind):
    results = []
    reply = _reply(stop_reason=stop, content=content)
    if usage is not None:
        reply["usage"] = usage
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (200, {}, json.dumps(reply).encode()))
    with pytest.raises(a.AnthropicError, match=kind):
        client.complete("synthetic", deadline=time.monotonic() + 10,
                        after_result=lambda reservation, meta: results.append(meta))
    assert len(results) == 1 and results[0]["error_kind"] == kind


@pytest.mark.parametrize("block", [
    {"type": "thinking"}, {"type": "thinking", "thinking": None},
    {"type": "thinking", "thinking": 1}, {"type": "thinking", "thinking": ""},
    {"type": "thinking", "thinking": "", "signature": ""},
    {"type": "thinking", "thinking": "", "signature": None},
    {"type": "thinking", "thinking": "", "signature": 1},
    {"type": "thinking", "thinking": "  ", "signature": "synthetic"},
    {"type": "thinking", "thinking": "  "}, {"type": "redacted_thinking"},
    {"type": "redacted_thinking", "data": {}}, {"type": "redacted_thinking", "data": ""},
])
def test_malformed_thinking_cannot_become_a_length_replan(block):
    calls, results = [], []

    def post(body, timeout):
        calls.append(body)
        return 200, {}, json.dumps(_reply(stop_reason="max_tokens", content=[block])).encode()

    client = a.AnthropicClient("synthetic", post_fn=post)
    with pytest.raises(a.AnthropicError, match="protocol_error"):
        client.complete("synthetic", deadline=time.monotonic() + 10,
                        after_result=lambda reservation, meta: results.append(meta))
    assert len(calls) == len(results) == 1 and results[0]["error_kind"] == "protocol_error"


def test_a_cache_prefix_becomes_the_first_cached_text_block():
    seen = []
    client = a.AnthropicClient("synthetic", post_fn=lambda body, timeout: (
        seen.append(body) or 200, {}, json.dumps(_reply()).encode()))
    client.complete("HEAD\n\nbody", deadline=time.monotonic() + 10, cache_prefix="HEAD\n\n")
    assert seen[0]["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "HEAD\n\n", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "body"}]}]
    client.complete("HEAD", deadline=time.monotonic() + 10, cache_prefix="HEAD")
    assert len(seen[1]["messages"][0]["content"]) == 1   # never an empty text block
    with pytest.raises(ValueError, match="cache_prefix_invalid"):
        client.complete("body", deadline=time.monotonic() + 10, cache_prefix="HEAD")


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": True, "output_tokens": 1},
                                  {"input_tokens": 1, "output_tokens": -1},
                                  {"input_tokens": 1, "output_tokens": 2, "new_charge": 3}])
def test_unknown_usage_is_never_a_zero_cost_success(usage):
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (
        200, {}, json.dumps(_reply(usage=usage)).encode()))
    with pytest.raises(a.AnthropicError, match="pricing_unverified"):
        client.complete("synthetic", deadline=time.monotonic() + 10)


def test_second_guard_rejection_never_dispatches_and_settles_the_reservation_as_unsent():
    events = []

    def guard():
        events.append("guard")
        if len(events) == 3:
            raise RuntimeError("scope changed while waiting for DB")

    client = a.AnthropicClient("synthetic", post_fn=lambda *args: pytest.fail("not sent"))
    with pytest.raises(RuntimeError):
        client.complete("synthetic", deadline=time.monotonic() + 10, before_attempt=guard,
                        reserve_fn=lambda *args: events.append("reserve") or {"r": 1},
                        after_result=lambda r, meta: events.append((r, meta)))
    assert events[:3] == ["guard", "reserve", "guard"]
    assert events[3][0] == {"r": 1} and events[3][1]["http_class"] == "not_sent"
    assert events[3][1]["usage"] is None and events[3][1]["error_kind"] == "budget_exceeded"


def test_each_retry_reserves_and_accounts_http_failures(monkeypatch):
    monkeypatch.setattr(a.time, "sleep", lambda delay: None)
    reservations, results, calls = [], [], []

    def post(*args):
        calls.append(1)
        if len(calls) == 1:
            return 529, {}, b"private response must not appear"   # an HTTP status is a definite outcome
        return 200, {}, json.dumps(_reply()).encode()

    def reserve(*args):
        reservations.append(len(reservations))
        return reservations[-1]

    client = a.AnthropicClient("synthetic", post_fn=post)
    client.complete("synthetic", deadline=time.monotonic() + 10, reserve_fn=reserve,
                    after_result=lambda r, meta: results.append((r, meta)))
    assert reservations == [0, 1]
    assert results[0][1]["usage"] is None and results[0][1]["error_kind"] == "transport"
    assert results[0][1]["http_class"] == "http_529"
    assert results[1][1]["usage"]["output_tokens"] == 5
    assert "private response" not in json.dumps(results)


@pytest.mark.parametrize("error", [TimeoutError("synthetic"), ConnectionResetError("synthetic"),
                                   ConnectionRefusedError("synthetic"), OSError("synthetic")])
def test_a_failure_that_may_have_been_sent_is_never_retried(monkeypatch, error):
    monkeypatch.setattr(a.time, "sleep", lambda delay: None)
    calls, results = [], []

    def post(*args):
        calls.append(1)
        raise error

    client = a.AnthropicClient("synthetic", post_fn=post)
    with pytest.raises(a.AnthropicError):
        client.complete("synthetic", deadline=time.monotonic() + 10, reserve_fn=lambda *args: {"r": 1},
                        after_result=lambda r, meta: results.append(meta))
    # one attempt only: its reservation stays counted as possibly billed, never as not_sent
    assert len(calls) == 1 and results[0]["http_class"] != "not_sent"


@pytest.mark.parametrize("error", [OSError("http worker transport"), ConnectionRefusedError("http worker connection refused")])
def test_the_real_request_path_never_resends_a_transport_failure(monkeypatch, error):
    # bounded_http's parent re-raises the worker's classification as these generic types,
    # whatever stage failed: no type proves the request never left.
    monkeypatch.setattr(a.time, "sleep", lambda delay: None)
    calls = []

    def request(self, method, body, timeout, *, deadline):
        calls.append(method)
        raise error

    monkeypatch.setattr(a.AnthropicClient, "request", request)
    client = a.AnthropicClient("synthetic")
    with pytest.raises(a.AnthropicError):
        client.complete("synthetic", deadline=time.monotonic() + 10, reserve_fn=lambda *args: {"r": 1},
                        after_result=lambda r, meta: None)
    assert calls == ["POST"]


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


def test_anthropic_quota_shaped_429_keeps_existing_rate_limit_behavior(monkeypatch):
    monkeypatch.setattr(a.time, "sleep", lambda delay: None)
    calls = []
    body = b'{"error":{"code":"insufficient_quota"}}'
    provider = a.AnthropicClient("synthetic", post_fn=lambda *args: (
        calls.append(True) or 429, {"Retry-After": "0"}, body))
    with pytest.raises(a.AnthropicError, match="rate_limited"):
        provider.complete("synthetic", deadline=time.monotonic() + 10)
    assert len(calls) == 3


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
             "inference_geo": "global", "server_tool_use": {"web_search_requests": 0},
             "output_tokens_details": {"thinking_tokens": 2}, "speed": "standard"}
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (
        200, {}, json.dumps(_reply(usage=usage)).encode()))
    assert client.complete("synthetic", deadline=time.monotonic() + 10)["usage"]["input_tokens"] == 10
    for extra in ({"speed": "fast"}, {"speed": None}, {"speed": True}, {"speed": "future"},
                  {"service_tier": "priority"}, {"inference_geo": "us"}, {"new_charge_usd": "10"},
                  {"service_tier": {"tier": "standard"}}, {"inference_geo": ["global"]}, {"service_tier": True},
                  {"server_tool_use": {"web_search_requests": 1}}, {"unknown_flag": None},
                  {"output_tokens_details": {"thinking_tokens": -1}}, {"output_tokens_details": 3}):
        client = a.AnthropicClient("synthetic", post_fn=lambda *args, e=extra: (
            200, {}, json.dumps(_reply(usage={"input_tokens": 1, "output_tokens": 1, **e})).encode()))
        with pytest.raises(a.AnthropicError, match="pricing_unverified"):
            client.complete("synthetic", deadline=time.monotonic() + 10)


@pytest.mark.parametrize("speed,accepted", [("standard", True), ("fast", False), (None, False)])
def test_speed_metadata_preserves_reservation_accounting(speed, accepted):
    events = []
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 3,
             "cache_read_input_tokens": 2, "cache_creation": {
                 "ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 2}, "speed": speed}
    client = a.AnthropicClient("synthetic", post_fn=lambda *args: (
        200, {}, json.dumps(_reply(usage=usage)).encode()))
    def complete():
        return client.complete("synthetic", deadline=time.monotonic() + 10,
                               reserve_fn=lambda *args: "synthetic-reservation",
                               after_result=lambda reservation, meta: events.append((reservation, meta)))
    if accepted:
        result = complete()
        assert result["usage"] == {key: value for key, value in usage.items() if key != "speed"}
        assert events[0][1]["usage"] == result["usage"] and events[0][1]["error_kind"] is None
    else:
        with pytest.raises(a.AnthropicError, match="pricing_unverified"):
            complete()
        assert events[0][1]["usage"] is None and events[0][1]["error_kind"] == "pricing_unverified"
    assert len(events) == 1 and events[0][0] == "synthetic-reservation"
    assert events[0][1]["http_class"] == "http_200"


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


@pytest.mark.parametrize("wait_fn", [False, 0, "sleep", [], {}])
def test_retry_wait_hook_must_be_callable(wait_fn):
    with pytest.raises(ValueError, match="wait_fn_invalid"):
        a.AnthropicClient("synthetic", wait_fn=wait_fn)


@pytest.mark.parametrize("outcome", ["success", "wait_failed", "scope_changed", "deadline"])
def test_retry_wait_follows_settlement_and_preserves_send_guards(monkeypatch, outcome):
    now, events = [0.0], []
    reservations, calls, results = [], [], []
    allowed = [True]
    monkeypatch.setattr(a.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(a.time, "sleep", lambda _: pytest.fail("custom wait was bypassed"))

    def guard():
        events.append("guard")
        if not allowed[0]:
            raise RuntimeError("synthetic_scope_changed")

    def reserve(*args):
        events.append("reserve")
        reservations.append(len(reservations))
        return reservations[-1]

    def post(*args):
        events.append("post")
        calls.append(True)
        if len(calls) == 1:
            return 429, {"Retry-After": "20" if outcome == "deadline" else "2"}, b""
        return 200, {}, json.dumps(_reply()).encode()

    def settle(reservation, meta):
        events.append("settle")
        results.append((reservation, meta))

    def wait(delay):
        assert events == ["guard", "reserve", "guard", "post", "settle"]
        assert len(reservations) == len(calls) == len(results) == 1
        assert results[0][1]["http_class"] == "http_429"
        events.append("wait")
        assert delay == (10 if outcome == "deadline" else 2)
        if outcome == "wait_failed":
            raise RuntimeError("synthetic_wait_failed")
        now[0] += delay
        if outcome == "scope_changed":
            allowed[0] = False

    client = a.AnthropicClient("synthetic", post_fn=post, wait_fn=wait)
    if outcome == "success":
        answer = client.complete("synthetic", deadline=10, before_attempt=guard,
                                 reserve_fn=reserve, after_result=settle)
        assert answer["status"] == 200
        assert events[6:] == ["guard", "reserve", "guard", "post", "settle"]
        assert len(reservations) == len(calls) == len(results) == 2
    else:
        error = a.AnthropicError if outcome == "deadline" else RuntimeError
        reason = "rate_limited" if outcome == "deadline" else "synthetic_" + outcome
        with pytest.raises(error, match=reason):
            client.complete("synthetic", deadline=10, before_attempt=guard,
                            reserve_fn=reserve, after_result=settle)
        assert len(reservations) == len(calls) == len(results) == 1
        assert events[6:] == (["guard"] if outcome == "scope_changed" else [])


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_semantic_retry_wait_releases_writer_lock_after_settlement(cloud, tmp_path, monkeypatch, provider):
    import os
    import subprocess
    import sys
    import cloud_policy
    import extract_llm
    import openai_client
    import semantic_transport
    import writer_lock
    from test_openai_client import response
    from test_semantic_cloud import rows

    led, cfg, job, _ = cloud
    cfg["cloud_llm"]["provider"] = provider
    monkeypatch.setattr(extract_llm, "HOME", str(tmp_path))
    monkeypatch.setattr(cloud_policy, "provider_key", lambda *args: "synthetic-key")
    monkeypatch.setattr(cloud_policy, "pricing_valid", lambda *args, **kwargs: True)
    lock_path = tmp_path / "synthetic-run.lock"
    lock_fd = writer_lock.acquire_run_lock(str(lock_path))
    assert lock_fd is not None
    deadline = time.monotonic() + 60
    events = []
    original_result = cloud_policy.record_result

    def assert_locked():
        other = writer_lock.acquire_run_lock(str(lock_path))
        if other is not None:
            os.close(other)
            pytest.fail("writer lock was not reacquired")

    def settle(*args):
        assert_locked()
        events.append("settle")
        return original_result(*args)

    def request(self, *args, **kwargs):
        events.append("post")
        if events == ["post"]:
            return 429, {"Retry-After": "2"}, b""
        reply = _reply() if provider == "anthropic" else response()
        return 200, {}, json.dumps(reply).encode()

    def wait(delay):
        assert delay == 2 and events == ["post", "settle"]
        assert len(rows(led, "cloud_usage_reserve")) == len(rows(led, "cloud_usage_result")) == 1
        script = """\
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_WRONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print('acquired')
finally:
    os.close(fd)
"""
        probe = subprocess.run([sys.executable, "-I", "-c", script, str(lock_path)],
                               capture_output=True, text=True, timeout=10)
        assert probe.returncode == 0 and probe.stdout.strip() == "acquired"
        events.append("wait")

    def transport(call, *args, **kwargs):
        with writer_lock.unlocked_transport(led.db, lock_fd, wait_s=1, deadline=deadline):
            return call(*args, **kwargs)

    cls = a.AnthropicClient if provider == "anthropic" else openai_client.OpenAIClient
    monkeypatch.setattr(cls, "request", request)
    monkeypatch.setattr(cloud_policy, "record_result", settle)
    monkeypatch.setattr(extract_llm.time, "sleep", wait)
    try:
        with semantic_transport.backlog_inference(led, cfg, job, deadline, lambda: True, transport) as chat:
            assert json.loads(chat("Synthetic JSON:")) == {"synthetic": True}
        assert events == ["post", "settle", "wait", "post", "settle"]
        assert len(rows(led, "cloud_usage_reserve")) == len(rows(led, "cloud_usage_result")) == 2
        assert_locked()
    finally:
        os.close(lock_fd)


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
