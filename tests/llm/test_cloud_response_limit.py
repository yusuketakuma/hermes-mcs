"""Synthetic per-profile attempt caps and the cloud path's own response-size limit."""
from contextlib import closing
from decimal import Decimal
import json
import time

import pytest

import anthropic_client
import bounded_http
import cloud_client
import cloud_inference
import cloud_policy as policy
import openai_client
from ledger import Ledger
from semantic_cloud_testkit import cloud as cloud, rows

PROVIDERS = {"anthropic": anthropic_client, "openai": openai_client}
CLIENTS = {"anthropic": anthropic_client.AnthropicClient, "openai": openai_client.OpenAIClient}
LARGE_TOKENS = 32768


def _reply(provider, text='{"synthetic":true}', model=None):
    if provider == "anthropic":
        return {"model": model or anthropic_client.MODEL, "stop_reason": "end_turn",
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 10, "output_tokens": 5}}
    return {"model": model or openai_client.MODEL, "service_tier": "default", "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                      "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}},
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": text}]}]}


def _complete(client, results, **kw):
    return client.complete("synthetic", deadline=kw.pop("deadline", time.monotonic() + 10),
                           after_result=lambda _reservation, meta, **_answer: results.append(meta), **kw)


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_every_request_shape_has_one_bounded_attempt_cap(provider):
    module = PROVIDERS[provider]
    assert set(module.ATTEMPT_TIMEOUTS) == module.VALID_MODEL_EFFORTS
    assert all(0 < value <= cloud_client.ATTEMPT_TIMEOUT_CEILING for value in module.ATTEMPT_TIMEOUTS.values())
    assert module.ATTEMPT_TIMEOUTS[module.ORIGINAL_CONTRACT] == 120.0
    for (model, effort), seconds in module.ATTEMPT_TIMEOUTS.items():
        assert CLIENTS[provider]("synthetic", model=model, effort=effort).attempt_timeout == seconds
        assert CLIENTS[provider]("synthetic", model=model, effort=effort, attempt_timeout=7).attempt_timeout == 7
    assert CLIENTS[provider]("synthetic").attempt_timeout == 120.0


@pytest.mark.parametrize("value", [0, -1, cloud_client.ATTEMPT_TIMEOUT_CEILING + 1, float("inf"), "120", True])
def test_an_attempt_cap_outside_the_ceiling_is_refused(value):
    with pytest.raises(ValueError, match="attempt_policy_invalid"):
        anthropic_client.AnthropicClient("synthetic", attempt_timeout=value)


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_an_attempt_waits_for_its_profile_cap_but_never_past_the_deadline(provider, monkeypatch):
    module = PROVIDERS[provider]
    monkeypatch.setattr(cloud_client.time, "monotonic", lambda: 0.0)
    for (model, effort), seconds in module.ATTEMPT_TIMEOUTS.items():
        for deadline, expected in ((10000.0, seconds), (10.0, 10.0)):
            seen = []
            raw = json.dumps(_reply(provider, model=model)).encode()
            client = CLIENTS[provider]("synthetic", model=model, effort=effort,
                                       post_fn=lambda body, timeout: seen.append(timeout) or (200, {}, raw))  # noqa: B023
            reserved = []
            client.complete("synthetic", deadline=deadline,
                            reserve_fn=lambda body, timeout: reserved.append(timeout))  # noqa: B023
            assert seen == [expected] and reserved == [expected]


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_a_timeout_after_sending_is_recorded_once_and_never_sent_again(provider):
    module = PROVIDERS[provider]
    model, effort = next(key for key in sorted(module.PROFILE_DEFINITIONS.values()))
    calls, results = [], []

    def post(body, timeout):
        calls.append(timeout)
        raise TimeoutError("synthetic")

    client = CLIENTS[provider]("synthetic", model=model, effort=effort, post_fn=post)
    with pytest.raises(cloud_client.CloudError, match="^timeout$"):
        _complete(client, results)
    assert len(calls) == len(results) == 1
    assert results[0]["http_class"] == "transport" and results[0]["error_kind"] == "timeout"
    assert results[0]["usage"] is None


def test_the_response_limit_follows_the_output_bound_and_never_drops_below_the_default():
    default = bounded_http.MAX_RESPONSE_BYTES
    assert cloud_client.response_limit(1) == cloud_client.response_limit(8192) == default
    assert cloud_client.response_limit(16384) == 65536 + 16384 * 16 > default
    assert cloud_client.response_limit(LARGE_TOKENS) == 65536 + LARGE_TOKENS * 16
    assert cloud_client.response_limit(cloud_client.MAX_TOKENS) <= bounded_http.MAX_RESPONSE_BYTES_CEILING


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_request_raises_the_worker_limit_only_for_a_body_with_a_large_output_bound(provider, monkeypatch):
    seen = []
    monkeypatch.setattr(bounded_http, "bounded_http_request",
                        lambda *a, **kw: seen.append(kw) or (200, {}, b"{}"))
    client = CLIENTS[provider]("synthetic-private")
    field = client.OUTPUT_LIMIT_FIELD
    assert field in client.body("synthetic", 100, None)
    for body in ({}, {field: 8192}, {field: "32768"}, {field: True}, {field: 0},
                 {field: cloud_client.MAX_TOKENS + 1}, client.body("synthetic", 8192, None)):
        client.request("POST", body, 1, deadline=123)
        assert "max_response_bytes" not in seen.pop()
    client.request("POST", client.body("synthetic", LARGE_TOKENS, None), 1, deadline=123)
    assert seen.pop()["max_response_bytes"] == cloud_client.response_limit(LARGE_TOKENS)


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
@pytest.mark.parametrize("max_tokens", [8192, LARGE_TOKENS])
def test_a_200_response_past_the_limit_is_its_own_fixed_kind_without_usage(provider, max_tokens):
    calls, results = [], []
    limit = cloud_client.response_limit(max_tokens)
    exact = json.dumps(_reply(provider)).encode()
    client = CLIENTS[provider]("synthetic", post_fn=lambda body, timeout: (
        calls.append(True) or 200, {}, b" " * (limit + 1)))
    with pytest.raises(cloud_client.CloudError) as error:
        _complete(client, results, max_tokens=max_tokens)
    assert error.value.kind == cloud_client.RESPONSE_TOO_LARGE == "response_too_large"
    assert not error.value.retryable and len(calls) == len(results) == 1
    assert results[0]["http_class"] == "http_200" and results[0]["usage"] is None
    assert results[0]["error_kind"] == "response_too_large"
    # a response of exactly the limit is still read and validated
    padded = exact + b" " * (limit - len(exact))
    accepted = CLIENTS[provider]("synthetic", post_fn=lambda body, timeout: (200, {}, padded))
    assert _complete(accepted, [], max_tokens=max_tokens)["text"] == '{"synthetic":true}'


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_a_200_response_that_is_not_bytes_stays_a_protocol_error(provider):
    results = []
    client = CLIENTS[provider]("synthetic", post_fn=lambda body, timeout: (200, {}, "x" * 10))
    with pytest.raises(cloud_client.CloudError, match="^protocol_error$"):
        _complete(client, results)
    assert results[0]["error_kind"] == "protocol_error" and results[0]["usage"] is None


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_an_answer_text_past_the_stored_bound_keeps_its_validated_usage(provider):
    results = []
    text = "x" * (bounded_http.MAX_RESPONSE_BYTES + 1)
    raw = json.dumps(_reply(provider, text=text)).encode()
    assert len(raw) <= cloud_client.response_limit(LARGE_TOKENS)
    client = CLIENTS[provider]("synthetic", post_fn=lambda body, timeout: (200, {}, raw))
    with pytest.raises(cloud_client.CloudError) as error:
        _complete(client, results, max_tokens=LARGE_TOKENS)
    assert error.value.kind == "response_too_large" and error.value.detail == "answer_too_large"
    assert results[0]["http_class"] == "http_200" and results[0]["usage"] is not None
    assert results[0]["error_kind"] == "response_too_large"
    assert "answer_text_sha256_v1" not in results[0] and "answer_finish_reason_v1" not in results[0]


def _reserve(ledger, provider, unit="synthetic", prompt="synthetic", max_tokens=100):
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": provider}}
    body = CLIENTS[provider]("synthetic").body(prompt, max_tokens, None)
    return policy.usage_reserver(ledger, cfg, route="patient_context", project_id=1, unit_key=unit)(body, 2)


def _result(reservation, **kw):
    return {"model": reservation["model"], "request_fp": reservation["request_fp"], "usage": None,
            "http_class": "http_200", "request_id": None, "error_kind": "response_too_large", **kw}


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_an_oversized_response_holds_only_its_unit_at_the_reserved_maximum(provider, tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "pricing_valid", lambda *args, **kwargs: True)
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        reservation = _reserve(ledger, provider)
        policy.record_result(ledger, reservation, _result(reservation))
        policy.record_result(ledger, reservation, _result(reservation))   # repeat delivery is idempotent
        saved = ledger.artifacts("cloud_usage_result")
        assert len(saved) == 1
        meta = json.loads(saved[0]["meta"])
        assert meta["error_kind"] == "response_too_large" and meta["usage"] is None
        assert Decimal(meta["cost_usd"]) == Decimal(reservation["max_cost_usd"]) > 0
        report = policy.usage_report(ledger)
        assert Decimal(report["monthly_cost_usd"]) == Decimal(reservation["max_cost_usd"])
        assert report["unresolved_requests"] == 1 and report["pricing_unverified"] is False
        # the global stop is untouched: no circuit row, and another unit still reserves
        assert not ledger.artifacts("cloud_circuit")
        assert policy.global_block_reason(ledger, model=reservation["model"]) is None
        assert _reserve(ledger, provider, unit="synthetic-other", prompt="synthetic other")["reservation_id"]
        # the held unit's own request is never reserved (and so never sent) again
        with pytest.raises(cloud_client.CloudError):
            _reserve(ledger, provider)
        assert len(ledger.artifacts("cloud_usage_reserve")) == 2


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_an_oversized_answer_settles_at_its_actual_cost_without_a_global_stop(provider, tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "pricing_valid", lambda *args, **kwargs: True)
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        reservation = _reserve(ledger, provider)
        usage = _reply(provider)["usage"]
        policy.record_result(ledger, reservation, _result(reservation, usage=usage))
        meta = json.loads(ledger.artifacts("cloud_usage_result")[0]["meta"])
        assert meta["error_kind"] == "response_too_large" and meta["usage"] is not None
        assert 0 < Decimal(meta["cost_usd"]) < Decimal(reservation["max_cost_usd"])
        assert policy.usage_report(ledger)["unresolved_requests"] == 0
        assert policy.global_block_reason(ledger, model=reservation["model"]) is None
        with pytest.raises(cloud_client.CloudError):
            _reserve(ledger, provider)


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
@pytest.mark.parametrize("kind", ["protocol_error", "contract_error", None])
def test_any_other_200_without_usage_still_stops_all_cloud_sending(provider, kind, tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "pricing_valid", lambda *args, **kwargs: True)
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        reservation = _reserve(ledger, provider)
        with pytest.raises(cloud_client.CloudError, match="pricing_unverified"):
            policy.record_result(ledger, reservation, _result(reservation, error_kind=kind))
        assert policy.global_block_reason(ledger, model=reservation["model"]) == "pricing_unverified"


def _unit(ledger, cfg, deadline):
    return cloud_inference.CloudInferenceUnit(ledger, cfg, route="patient_context", project_id=1,
        unit_key="synthetic", deadline=deadline, current=lambda: True,
        dispatch=lambda call, **_kwargs: call())


def _local_fallback_refusal(ledger, cfg, unit, body):
    with pytest.raises(cloud_client.CloudError) as local:
        policy.guard_local_fallback(ledger, route="patient_context", project_id=1, message_id=None,
            unit_key="synthetic", request_factory=lambda: (
                policy._request_identity(cloud_inference.resolve_cloud_settings(cfg), body,
                    route="patient_context", project_id=1, message_id=None, unit_key="synthetic",
                    owner=None, selection=unit.selection, input_binding=None), body))
    return local.value


def test_a_unit_whose_response_was_too_large_stays_held_like_an_unanswered_send(cloud, monkeypatch):
    ledger, cfg, _, _ = cloud
    monkeypatch.setattr(policy, "pricing_valid", lambda *args, **kwargs: True)
    calls = []

    def request(self, method, body, timeout, **kwargs):
        calls.append(body)
        return 200, {}, b" " * (cloud_client.response_limit(body["max_tokens"]) + 1)

    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)
    deadline = time.monotonic() + 10
    unit = _unit(ledger, cfg, deadline)
    with pytest.raises(cloud_inference.CloudFallback, match="cloud_unit_failed"):
        unit("synthetic", deadline=deadline, max_tokens=100)
    assert len(calls) == 1 and unit.possibly_sent and unit.unresolved
    reserves, results = rows(ledger, "cloud_usage_reserve"), rows(ledger, "cloud_usage_result")
    assert len(reserves) == len(results) == 1
    saved = json.loads(results[0]["meta"])
    assert saved["error_kind"] == "response_too_large" and saved["usage"] is None
    assert Decimal(saved["cost_usd"]) == Decimal(json.loads(reserves[0]["meta"])["max_cost_usd"])
    assert policy.global_block_reason(ledger, model=unit.model) is None
    # neither another cloud send nor a local substitute may answer the held request
    with pytest.raises(cloud_client.CloudNotSent) as held:
        unit("synthetic", deadline=deadline, max_tokens=100)
    assert held.value.detail == "cloud_unit_unresolved" and len(calls) == 1
    refusal = _local_fallback_refusal(ledger, cfg, unit, calls[0])
    assert refusal.detail == "cloud_unit_unresolved" and refusal.history_refusal == "repeat_unsettled"


def test_a_unit_whose_answer_text_was_too_large_is_settled_but_never_answered_again(cloud, monkeypatch):
    ledger, cfg, _, _ = cloud
    monkeypatch.setattr(policy, "pricing_valid", lambda *args, **kwargs: True)
    calls = []
    raw = json.dumps(_reply("anthropic", text="x" * (bounded_http.MAX_RESPONSE_BYTES + 1))).encode()

    def request(self, method, body, timeout, **kwargs):
        calls.append(body)
        assert len(raw) <= cloud_client.response_limit(body["max_tokens"])
        return 200, {}, raw

    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)
    deadline = time.monotonic() + 10
    unit = _unit(ledger, cfg, deadline)
    with pytest.raises(cloud_inference.CloudFallback, match="cloud_unit_failed"):
        unit("synthetic", deadline=deadline, max_tokens=LARGE_TOKENS)
    # the usage was read, so the unit itself is not left holding an unknown send
    assert len(calls) == 1 and unit.possibly_sent and not unit.unresolved
    reserves, results = rows(ledger, "cloud_usage_reserve"), rows(ledger, "cloud_usage_result")
    assert len(reserves) == len(results) == 1
    saved = json.loads(results[0]["meta"])
    assert saved["error_kind"] == "response_too_large" and saved["usage"] is not None
    assert saved["http_class"] == "http_200" and "answer_text_sha256_v1" not in saved
    assert 0 < Decimal(saved["cost_usd"]) < Decimal(json.loads(reserves[0]["meta"])["max_cost_usd"])
    assert policy.usage_report(ledger)["unresolved_requests"] == 0
    assert not rows(ledger, "cloud_circuit")
    assert policy.global_block_reason(ledger, model=unit.model) is None
    # the settled failure still answers for this request: no local substitute, no resend
    refusal = _local_fallback_refusal(ledger, cfg, unit, calls[0])
    assert refusal.detail == "cloud_unit_unresolved" and refusal.history_refusal == "repeat_failed"
    with pytest.raises(cloud_client.CloudNotSent) as held:
        unit("synthetic", deadline=deadline, max_tokens=LARGE_TOKENS)
    assert held.value.detail == "cloud_unit_unresolved" and len(calls) == 1
