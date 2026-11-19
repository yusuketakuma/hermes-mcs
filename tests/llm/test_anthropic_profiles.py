"""Anthropic purpose profiles (D1): opt-in Haiku-medium / Sonnet-high, the unchanged
original Haiku-low contract, Sonnet prices and the fail-closed finite combinations."""
from decimal import Decimal
import json
import time

import pytest

import anthropic_client as a
import cloud_inference
import cloud_policy as c
import cloud_usage_accounting as accounting
import provider_settings
import openai_client
from test_anthropic_runtime import routed as routed

__all__ = ["routed"]
# Captured at import: the routed fixture replaces both with stubs.
_REAL_RESERVE = c.usage_reserver
_REAL_RESULT = c.record_result


def _cfg(**kw):
    return {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "anthropic", **kw}}


def _reply(model, stop="end_turn"):
    return {"model": model, "stop_reason": stop, "content": [{"type": "text", "text": '{"ok":true}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}


def test_original_haiku_contract_keeps_its_exact_wire_body():
    legacy = a.AnthropicClient("synthetic")
    assert (legacy.model, legacy.effort) == (a.MODEL, None)
    body = legacy.body("synthetic prompt", 100, None)
    assert body == {"model": a.MODEL, "max_tokens": 100, "system": a.SYSTEM,
                    "output_config": {"effort": "low"},
                    "messages": [{"role": "user", "content": "synthetic prompt"}]}


@pytest.mark.parametrize("profile", ["haiku-medium", "sonnet-high"])
def test_profiles_send_their_model_and_effort(profile):
    model, effort = a.PROFILE_DEFINITIONS[profile]
    body = a.AnthropicClient("synthetic", model=model, effort=effort).body("p", 100, None)
    assert body["model"] == model and body["output_config"] == {"effort": effort}


@pytest.mark.parametrize("model,effort", [
    (a.MODEL, "low"), (a.MODEL, "high"), (a.SONNET_MODEL, None), (a.SONNET_MODEL, "medium"),
    ("claude-unknown", "high"), (a.MODEL, 1),
])
def test_unknown_model_effort_combinations_are_refused(model, effort):
    with pytest.raises(ValueError, match="anthropic_profile_invalid"):
        a.AnthropicClient("synthetic", model=model, effort=effort)


def test_answer_matches_the_requested_model():
    sonnet = a.AnthropicClient("synthetic", model=a.SONNET_MODEL, effort="high")
    assert sonnet.answer(_reply(a.SONNET_MODEL), {})["model"] == a.SONNET_MODEL
    with pytest.raises(a.AnthropicError, match="model_mismatch"):
        sonnet.answer(_reply(a.MODEL), {})


def test_refusal_keeps_its_usage_and_is_never_an_answer():
    meta = {}
    with pytest.raises(a.AnthropicError) as caught:
        a.AnthropicClient("synthetic").answer(_reply(a.MODEL, stop="refusal"), meta)
    assert (caught.value.kind, caught.value.detail) == ("contract_error", "refusal")
    assert meta["usage"]["input_tokens"] == 10 and meta["usage"]["output_tokens"] == 5


def test_sonnet_prices_have_one_tier_and_their_own_cache_rates():
    cost = accounting.usage_cost({"input_tokens": 1000, "output_tokens": 1000}, model=a.SONNET_MODEL)
    assert cost == Decimal(2 * 1000 + 10 * 1000) / Decimal(1000000)
    # far above Haiku's 100k step: still the single Sonnet rate
    big = accounting.usage_cost({"input_tokens": 200000, "output_tokens": 0}, model=a.SONNET_MODEL)
    assert big == Decimal(2 * 200000) / Decimal(1000000)
    read = accounting.usage_cost({"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 1000000},
                                 model=a.SONNET_MODEL)
    assert read == Decimal("0.10")
    # the Haiku table and its current version are unchanged
    assert accounting._price_definition(a.MODEL)["price_version"] == a.PRICE_VERSION


@pytest.mark.parametrize("provider,model,effort,profile,tokens,valid", [
    ("anthropic", a.MODEL, None, None, None, True),
    ("anthropic", a.MODEL, "medium", "haiku-medium", 8192, True),
    ("anthropic", a.SONNET_MODEL, "high", "sonnet-high", 16384, True),
    ("anthropic", a.SONNET_MODEL, "high", "haiku-medium", 16384, False),
    ("anthropic", a.SONNET_MODEL, "high", None, 16384, False),
    ("anthropic", a.SONNET_MODEL, None, None, None, False),    # Sonnet only through sonnet-high
    ("anthropic", "synthetic-rotated-haiku", None, None, None, True),   # the caller checks the price

    ("anthropic", a.MODEL, "medium", "haiku-medium", 100, False),
    ("anthropic", a.MODEL, "low", None, None, False),
    ("openai", "gpt-6-luna", "low", None, 8192, True),
    ("openai", a.MODEL, None, None, None, False),
    ("jev", a.MODEL, None, None, None, False),
])
def test_profile_contracts_are_finite_per_provider(provider, model, effort, profile, tokens, valid):
    assert accounting.profile_contract_valid(provider, model, effort, profile, tokens) is valid


@pytest.mark.parametrize("route,purpose,model,effort,limit", [
    ("reextract", "backlog", a.MODEL, "medium", 8192),
    ("semantic_backlog", "backlog", a.MODEL, "medium", 8192),
    ("patient_context", "patient_context", a.SONNET_MODEL, "high", 16384),
    ("semantic_backlog", "quality_repair", a.SONNET_MODEL, "high", 32768),
])
@pytest.mark.parametrize("profiles", [{}, {"provider_profiles": {"anthropic": {}}}])
def test_anthropic_defaults_follow_the_owner_layout_with_or_without_profiles(route, purpose, model, effort, limit, profiles):
    # An unset profile map must not fall back to the 1600-token original contract.
    selection, errors = c.resolve_profile(_cfg(**profiles), route=route, purpose=purpose)
    assert not errors
    assert (selection["model"], selection["effort"], selection["max_tokens"]) == (model, effort, limit)
    contract = cloud_inference.cloud_provider_contract(selection=selection)
    assert contract != cloud_inference._CLOUD_CONTRACT and cloud_inference.is_supported_cloud_contract(contract)


def test_default_limit_changes_the_request_fingerprint_of_the_old_1600_reservation():
    selection, _ = c.resolve_profile(_cfg(), route="reextract", purpose="backlog")
    client = a.AnthropicClient("synthetic-key")
    old = client.body("synthetic prompt", 1600, None)
    assert client.body("synthetic prompt", selection["max_tokens"], None) != old
    assert cloud_inference.mcs_util.payload_hash(old) != cloud_inference.mcs_util.payload_hash(
        client.body("synthetic prompt", selection["max_tokens"], None))


def test_invalid_or_conflicting_profiles_fail_closed():
    bad = _cfg(provider_profiles={"anthropic": {"backlog": "sonnet-unknown"}})
    assert c.resolve_profile(bad, route="reextract", purpose="backlog")[0] is None
    assert "config: cloud_llm_profiles_invalid" in c.cloud_config(bad, key_present=True)[1]
    conflict = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "openai",
                              "profiles": {"backlog": "luna-medium"},
                              "provider_profiles": {"openai": {"backlog": "sol-medium"}}}}
    assert "config: cloud_llm_profiles_invalid" in c.cloud_config(conflict, key_present=True)[1]


def test_split_settings_carry_anthropic_profiles_only_for_anthropic():
    owners = {model: name for name, model in c.PROVIDERS.items()}
    raw = {"cloud_llm": {"provider": "anthropic", "mode": "off", "project_ids": [],
                         "provider_profiles": {"anthropic": {"backlog": "haiku-medium"}}}}
    common, providers = provider_settings.split_legacy(raw, owners)
    assert providers["anthropic"]["profiles"] == {"backlog": "haiku-medium"}
    assert provider_settings.compose(common, providers, selected_provider="anthropic", model_owners=owners) == raw
    openai_view = provider_settings.compose(common, providers, selected_provider="openai", model_owners=owners)
    assert "provider_profiles" not in openai_view["cloud_llm"] and "profiles" not in openai_view["cloud_llm"]
    providers["anthropic"]["profiles"] = {"backlog": "luna-medium"}      # an OpenAI name
    with pytest.raises(ValueError, match="provider_profiles_invalid"):
        provider_settings.compose(common, providers, selected_provider="anthropic", model_owners=owners)


def test_a_unit_settles_a_200_refusal_with_its_usage_and_sends_nothing_more(routed, monkeypatch):
    """Through CloudInferenceUnit: the refusal is one paid attempt. Its validated usage
    stays on the result row, no second POST follows and no other provider is built."""
    led, cfg, *_ = routed
    sent = []
    monkeypatch.setattr(a.time, "sleep", lambda _: None)
    monkeypatch.setattr(c, "usage_reserver", _REAL_RESERVE)
    monkeypatch.setattr(c, "record_result", _REAL_RESULT)

    def post(body, timeout):
        sent.append(body["model"])
        return 200, {"request-id": "req_synthetic_refusal"}, json.dumps(
            {**_reply(body["model"], stop="refusal"), "content": []}).encode()

    monkeypatch.setattr(a, "AnthropicClient", lambda key, **kw: _Recording(key, post, **kw))
    monkeypatch.setattr(openai_client, "OpenAIClient",
                        lambda *args, **kwargs: pytest.fail("a refusal must not switch provider"))
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="patient_context", project_id=1,
        unit_key="synthetic-refusal", deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kwargs: fn(), max_attempts=3)
    with pytest.raises(cloud_inference.CloudFallback):
        provider("synthetic prompt", deadline=time.monotonic() + 180, max_tokens=100)
    assert sent == [a.SONNET_MODEL]                           # one POST, never retried
    rows = led.db.execute("SELECT meta FROM artifacts WHERE kind='cloud_usage_result'").fetchall()
    assert len(rows) == 1
    meta = json.loads(rows[0][0])
    assert meta["usage"]["input_tokens"] == 10 and meta["usage"]["output_tokens"] == 5
    assert meta["error_kind"] == "contract_error" and meta["http_class"] == "http_200"
    assert "answer_text_sha256_v1" not in meta                 # never adopted as an answer
    report = c.usage_report(led)
    assert report["daily_requests"] == 1 and report["unresolved_requests"] == 0


class _Recording(a.AnthropicClient):
    def __init__(self, key, post, **kwargs):
        super().__init__(key, post_fn=post, **kwargs)


def _real_unit(led, cfg, monkeypatch, unit, prompt, post):
    """One CloudInferenceUnit send through the real reserve/result path."""
    monkeypatch.setattr(a.time, "sleep", lambda _: None)
    monkeypatch.setattr(a, "AnthropicClient", lambda key, **kw: _Recording(key, post, **kw))
    provider = cloud_inference.CloudInferenceUnit(led, cfg, route="patient_context", project_id=1,
        unit_key=unit, deadline=time.monotonic() + 180,
        current=lambda: True, dispatch=lambda fn, **kwargs: fn(), max_attempts=3)
    with pytest.raises(cloud_inference.CloudFallback):
        provider(prompt, deadline=time.monotonic() + 180, max_tokens=100)


def _result_metas(led):
    return [json.loads(row[0]) for row in led.db.execute(
        "SELECT meta FROM artifacts WHERE kind='cloud_usage_result' ORDER BY artifact_id")]


def test_five_units_with_200_refusals_each_send_once_and_never_open_the_circuit(routed, monkeypatch):
    """Five distinct units refused on a billed 200: one paid POST each, usage
    settled, nothing adopted, no other provider built and no circuit evidence."""
    import semantic_runtime
    led, cfg, *_ = routed
    monkeypatch.setattr(c, "usage_reserver", _REAL_RESERVE)
    monkeypatch.setattr(c, "record_result", _REAL_RESULT)
    monkeypatch.setattr(openai_client, "OpenAIClient",
                        lambda *args, **kwargs: pytest.fail("a refusal must not switch provider"))
    posts = []
    def post(body, timeout):
        posts.append(body["model"])
        return 200, {"request-id": f"req_refusal_{len(posts)}"}, json.dumps(
            {**_reply(body["model"], stop="refusal"), "content": []}).encode()
    for index in range(5):
        _real_unit(led, cfg, monkeypatch, f"synthetic-refusal-{index}", f"synthetic prompt {index}", post)
    assert posts == [a.SONNET_MODEL] * 5
    metas = _result_metas(led)
    assert len(metas) == 5
    for meta in metas:
        assert meta["usage"]["input_tokens"] == 10 and meta["error_kind"] == "contract_error"
        assert meta["http_class"] == "http_200" and "answer_text_sha256_v1" not in meta
        assert "contract_error_detail" not in meta      # the detail reaches the circuit, not the row
    report = c.usage_report(led)
    assert report["daily_requests"] == 5 and report["unresolved_requests"] == 0
    assert not semantic_runtime.circuit_open(led, kind="cloud_circuit")


@pytest.mark.parametrize('status', [400, 413])
def test_request_scoped_contract_errors_never_open_the_circuit_and_keep_the_maximum(routed, monkeypatch, status):
    """A non-retryable 4xx is the unit's own request: five units fail without a
    second POST, the cloud circuit stays closed and every maximum stays unsettled."""
    import semantic_runtime
    led, cfg, *_ = routed
    monkeypatch.setattr(c, "usage_reserver", _REAL_RESERVE)
    monkeypatch.setattr(c, "record_result", _REAL_RESULT)
    posts = []
    def post(body, timeout):
        posts.append(body["model"])
        return status, {}, json.dumps({"type": "error", "error": {
            "type": "invalid_request_error", "message": "synthetic request rejected"}}).encode()
    for index in range(5):
        _real_unit(led, cfg, monkeypatch, f"synthetic-4xx-{status}-{index}",
                   f"synthetic prompt {status}-{index}", post)
    assert posts == [a.SONNET_MODEL] * 5
    metas = _result_metas(led)
    assert len(metas) == 5
    assert all(meta["http_class"] == f"http_{status}" and meta["error_kind"] == "contract_error"
               and meta["usage"] is None for meta in metas)
    report = c.usage_report(led)
    assert report["unresolved_requests"] == 5
    assert report["monthly_cost_usd"] == report["max_reserved_cost_usd"] != "0"
    assert not semantic_runtime.circuit_open(led, kind="cloud_circuit")


def test_genuine_200_contract_violations_open_the_cloud_circuit(routed, monkeypatch):
    """A billed 200 that breaks the answer contract is provider-health evidence:
    three units' contract_error results open the cloud circuit."""
    import semantic_runtime
    led, cfg, *_ = routed
    monkeypatch.setattr(c, "usage_reserver", _REAL_RESERVE)
    monkeypatch.setattr(c, "record_result", _REAL_RESULT)
    posts = []
    def post(body, timeout):
        posts.append(body["model"])
        return 200, {"request-id": f"req_contract_{len(posts)}"}, json.dumps(
            _reply(body["model"], stop="incomplete")).encode()
    for index in range(3):
        _real_unit(led, cfg, monkeypatch, f"synthetic-contract-{index}",
                   f"synthetic prompt contract-{index}", post)
    assert posts == [a.SONNET_MODEL] * 3
    metas = _result_metas(led)
    assert len(metas) == 3
    assert all(meta["http_class"] == "http_200" and meta["error_kind"] == "contract_error"
               and meta["usage"] is not None for meta in metas)
    assert semantic_runtime.circuit_open(led, kind="cloud_circuit")


@pytest.mark.parametrize('status,marker_key,marker_value', [
    (400, 'anthropic_400_diagnostic', 'usage_limit'),
    (429, 'anthropic_error_code', 'enforced_spend_limit_reached')])
def test_a_verified_payment_limit_is_stored_and_opens_the_payment_circuit(routed, monkeypatch,
                                                                        status, marker_key, marker_value):
    """The real sender's finite diagnostic reaches the result row, keeps the
    reserved maximum and opens the terminal payment circuit."""
    import semantic_runtime
    led, cfg, *_ = routed
    monkeypatch.setattr(c, "usage_reserver", _REAL_RESERVE)
    monkeypatch.setattr(c, "record_result", _REAL_RESULT)
    body = ({"type": "error", "error": {"type": "invalid_request_error",
             "message": "You have reached your specified API usage limits. Synthetic."}}
            if status == 400 else
            {"type": "error", "error": {"type": "rate_limit_error",
             "details": {"error_code": "enforced_spend_limit_reached"}}})
    posts = []
    def post(request_body, timeout):
        posts.append(request_body["model"])
        return status, {}, json.dumps(body).encode()
    _real_unit(led, cfg, monkeypatch, f"synthetic-payment-{status}",
               f"synthetic prompt payment-{status}", post)
    assert posts == [a.SONNET_MODEL]
    meta, = _result_metas(led)
    assert meta["http_class"] == f"http_{status}" and meta["error_kind"] == "payment_required"
    assert meta["usage"] is None and meta[marker_key] == marker_value
    assert semantic_runtime.circuit_open(led, kind="cloud_circuit")
