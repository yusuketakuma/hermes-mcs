"""Synthetic OpenAI Responses transport, usage and guarded billing regressions."""
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import json
import time

import pytest

import cloud_policy as policy
from anthropic_client import AnthropicError
from ledger import Ledger
import openai_client as client


def usage(n=100, out=10, cached=20, written=30):
    return {"input_tokens": n, "output_tokens": out, "total_tokens": n + out,
            "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written},
            "output_tokens_details": {"reasoning_tokens": 2}}


def response(**kw):
    return {"model": client.MODEL, "service_tier": "default", "status": "completed", "usage": usage(),
            "output": [{"type": "reasoning"}, {"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": '{"synthetic":true}'}]}], **kw}


def test_responses_uses_fixed_model_no_storage_and_exact_attempt_hooks():
    seen, sent = [], []

    def post(body, timeout):
        assert body["model"] == client.MODEL and body["max_output_tokens"] == 100
        assert body["service_tier"] == "default" and body["store"] is False and body["tools"] == []
        assert body["prompt_cache_options"] == {"ttl": "30m"}
        sent.append(body)
        return 200, {"x-request-id": "req_synthetic"}, json.dumps(response()).encode()

    provider = client.OpenAIClient("synthetic-private", post_fn=post)
    result = provider.complete("synthetic JSON request", deadline=time.monotonic() + 10,
        max_tokens=100, before_attempt=lambda: seen.append("guard"),
        reserve_fn=lambda body, timeout: seen.append("reserve") or 123,
        after_result=lambda reserved, meta: seen.append((reserved, meta)))
    assert len(sent) == 1 and result["model"] == client.MODEL and result["finish_reason"] == "stop"
    assert seen[:3] == ["guard", "reserve", "guard"]
    assert seen[3][0] == 123 and seen[3][1]["request_id"] == "req_synthetic"
    assert seen[3][1]["usage"] == usage()


@pytest.mark.parametrize("doc,kind", [
    (response(model="unexpected"), "model_mismatch"),
    (response(service_tier="priority"), "pricing_unverified"),
    (response(status="failed"), "contract_error"),
    (response(output=[{"type": "function_call"}]), "contract_error"),
    (response(usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}), "pricing_unverified")])
def test_unknown_model_price_or_output_never_becomes_success(doc, kind):
    records = []
    provider = client.OpenAIClient("synthetic-private", post_fn=lambda *a: (200, {}, json.dumps(doc).encode()))
    with pytest.raises(AnthropicError) as error:
        provider.complete("synthetic JSON", max_tokens=100, deadline=time.monotonic() + 10,
            reserve_fn=lambda *a: 123, after_result=lambda *a: records.append(a))
    assert error.value.kind == kind and len(records) == 1
    assert records[0][1]["error_kind"] == kind


def test_uncertain_send_is_recorded_once_and_never_retried():
    sent, records = [], []

    def post(*a):
        sent.append(True)
        raise TimeoutError("synthetic")

    provider = client.OpenAIClient("synthetic-private", post_fn=post)
    with pytest.raises(AnthropicError):
        provider.complete("synthetic JSON", deadline=time.monotonic() + 10,
            after_result=lambda *a: records.append(a))
    assert len(sent) == len(records) == 1 and records[0][1]["usage"] is None


def test_transport_pins_endpoint_and_bearer_auth(monkeypatch):
    seen = []
    monkeypatch.setattr(client.bounded_http, "bounded_http_request", lambda *a, **kw: seen.append((a, kw)) or (200, {}, b"{}"))
    provider = client.OpenAIClient("synthetic-private")
    assert provider.request("POST", {}, 1, deadline=123)[0] == 200
    args, kwargs = seen[0]
    assert args == ("https://api.openai.com/v1/responses", "POST", {}, 1)
    assert kwargs == {"api_key": "synthetic-private", "deadline": 123,
                      "allowed_endpoints": (client.ENDPOINT,), "auth": "bearer"}


@pytest.mark.parametrize("n", [100, 272000, 272001, policy.INPUT_RESERVATION_BOUND])
def test_prices_include_cached_reads_writes_and_long_context(n):
    u = usage(n=n)
    factor = 2 if n > 272000 else 1
    expected = ((Decimal(n - 50) * Decimal("0.1") + Decimal(20) * Decimal("0.01")
                 + Decimal(30) * Decimal("0.125")) * factor
                + Decimal(10) * (Decimal("0.75") if factor == 2 else Decimal("0.5"))) / 1000000
    assert policy.usage_cost(u, model=client.MODEL) == expected
    assert expected <= policy._reservation_cost(n, 10, model=client.MODEL)


def test_mixed_provider_budgets_keep_legacy_reserves_and_original_receipts(tmp_path, monkeypatch):
    now = datetime(2026, 10, 29, 12, tzinfo=policy.JST).timestamp()
    monkeypatch.setattr(policy.time, "time", lambda: now)
    budgets = {"anthropic": {"daily_cost_limit_usd": 20, "monthly_cost_limit_usd": 60},
               "openai": {"daily_cost_limit_usd": 5, "monthly_cost_limit_usd": 15}}
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "openai", "provider_budgets": budgets}}
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        reserve = policy.usage_reserver(ledger, cfg, route="reextract", project_id=1, unit_key="synthetic")(
            client.OpenAIClient("synthetic").body("x" * 500, 100, None), 2)
        reserve_id = ledger.db.execute("SELECT max(artifact_id) FROM artifacts WHERE kind='cloud_usage_reserve'").fetchone()[0]
        meta = {"model": client.MODEL, "request_fp": reserve["request_fp"], "usage": usage(),
                "http_class": "http_200", "error_kind": None}
        policy.record_result(ledger, reserve, meta)
        report = policy.usage_report(ledger)
        assert not report["pricing_unverified"] and report["unresolved_requests"] == 0
        assert Decimal(report["monthly_cost_usd"]) == policy.usage_cost(usage(), model=client.MODEL)
        assert policy.reservation_evidence(ledger.db, reserve_id)["reservation"]["model"] == client.MODEL
        capped = {"cloud_llm": {**cfg["cloud_llm"], "provider_budgets": {**budgets, "openai": {
            "daily_cost_limit_usd": 0, "monthly_cost_limit_usd": 15}}}}
        assert policy.budget_block_reason(ledger, capped) == "provider_daily_cap"
        legacy = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
        policy.usage_reserver(ledger, legacy, route="reextract", project_id=1, unit_key="legacy")(
            {"model": policy.MODEL, "max_tokens": 100}, 2)
        tally = policy._tally(ledger)
        assert tally.provider_totals[policy.MODEL] > 0 and tally.provider_totals[client.MODEL] > 0
        assert policy.usage_report(ledger)["unresolved_requests"] == 1
        blocked = {"cloud_llm": {**legacy["cloud_llm"], "daily_cost_limit_usd": 0}}
        assert policy.budget_block_reason(ledger, blocked) == "daily_cost_cap"


def test_priority_uses_only_mcs_private_key_and_validates_split(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-process-not-allowed")
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "auto"}}
    assert policy.selected_key(tmp_path, cfg) is None
    path = tmp_path / ".env"
    path.write_text("MCS_OPENAI_API_KEY=synthetic-openai\n")
    path.chmod(0o600)
    settings, errors = policy.resolve_settings(cfg, tmp_path)
    assert not errors and settings["provider"] == "openai" and settings["model"] == client.MODEL
    path.write_text(path.read_text() + "MCS_ANTHROPIC_API_KEY=synthetic-anthropic\n")
    assert policy.resolve_settings(cfg, tmp_path)[0]["provider"] == "anthropic"
    cfg["cloud_llm"]["provider_order"] = ["openai", "anthropic"]
    assert policy.resolve_settings(cfg, tmp_path)[0]["provider"] == "openai"
    cfg["cloud_llm"]["provider_budgets"] = {"openai": {"daily_cost_limit_usd": 26, "monthly_cost_limit_usd": 15}}
    settings, errors = policy.resolve_settings(cfg, tmp_path)
    assert errors and settings["mode"] == "off"
