"""Synthetic provider rotation preserves historical accounting and caps."""
from contextlib import closing
from decimal import Decimal
import json
from types import MappingProxyType

import pytest

import cloud_policy as c
import cloud_cli
from ledger import Ledger
from test_cloud_policy import _cfg, _reserve, _result, now

__all__ = ["now"]


def _rotate_anthropic(monkeypatch):
    previous = c.PRICE_DEFINITIONS[(c.MODEL, c.PRICE_VERSION)]
    model, version = "synthetic-next-model", "synthetic-next-version"
    prices = MappingProxyType({**previous["prices"], "input": "1", "output": "5"})
    successor = MappingProxyType({**previous, "model": model, "price_version": version, "prices": prices})
    monkeypatch.setattr(c, "PRICE_DEFINITIONS", MappingProxyType({**c.PRICE_DEFINITIONS, (model, version): successor}))
    monkeypatch.setitem(c.PROVIDERS, "anthropic", model)
    monkeypatch.setattr(c.anthropic_client, "PRICE_VERSION", version)
    return model


def test_old_reservation_and_result_keep_original_prices_after_rotation(tmp_path, now, monkeypatch):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        reservation = c.usage_reserver(ledger, _cfg(), route="reextract", project_id=1, unit_key="u")(
            {"model": c.MODEL, "max_tokens": 100, "messages": [{"role": "user", "content": "synthetic"}]}, 2)
        maximum = Decimal(reservation["max_cost_usd"])
        model = _rotate_anthropic(monkeypatch)
        pending = c.usage_report(ledger)
        assert Decimal(pending["monthly_cost_usd"]) == maximum
        assert pending["unresolved_requests"] == 1 and not pending["pricing_unverified"]
        capped = _cfg(provider="anthropic", provider_budgets={"anthropic": {"daily_cost_limit_usd": 0.0008,
                                                       "monthly_cost_limit_usd": 0.0008}})
        assert c.max_cost(100, input_bound=256, model=model) < Decimal("0.0008")
        assert c.budget_block_reason(ledger, capped, max_tokens=100, input_bound=256) == "provider_daily_cap"
        with pytest.raises(c.AnthropicError) as duplicate:
            c.usage_reserver(ledger, _cfg(), route="reextract", project_id=1, unit_key="u")(
                {"model": model, "max_tokens": 100,
                 "messages": [{"role": "user", "content": "synthetic"}]}, 2)
        assert duplicate.value.detail == "cloud_unit_unresolved"
        c.record_result(ledger, reservation, _result(reservation))
        report = c.usage_report(ledger)
        assert Decimal(report["monthly_cost_usd"]) == Decimal("0.000002")
        assert report["unresolved_requests"] == 0 and not report["pricing_unverified"]
        assert Decimal(report["providers"]["anthropic"]["cost_usd"]) == Decimal("0.000002")
        assert c.usage_cost({"input_tokens": 10, "output_tokens": 2}, model=model) == Decimal("0.00002")
        assert c._tally(ledger).provider_totals["anthropic"] == Decimal("0.000002")
        assert cloud_cli._calibration(ledger.db, 0)["settled_requests_with_characters"] == 1


def test_unknown_historical_version_stops_accounting_and_new_reservations(tmp_path, now):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        _reserve(ledger)
        row = ledger.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind=?", (c.KINDS[0],)).fetchone()
        meta = json.loads(row["meta"])
        meta["price_version"] = "synthetic-unknown-version"
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), row["artifact_id"]))
        assert c.usage_report(ledger)["pricing_unverified"] is True
        assert c.budget_block_reason(ledger, _cfg()) == "pricing_unverified"
        with pytest.raises(c.AnthropicError):
            _reserve(ledger, unit="another")


def test_openai_old_reservation_keeps_price_and_provider_caps_after_rotation(tmp_path, now, monkeypatch):
    monkeypatch.setattr(c, "pricing_valid", lambda *args, **kwargs: True)
    old_model, old_version = c.openai_client.MODEL, c.OPENAI_PRICE_VERSION
    previous = c.PRICE_DEFINITIONS[(old_model, old_version)]
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
             "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
             "output_tokens_details": {"reasoning_tokens": 0}}
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        body = c.openai_client.OpenAIClient("synthetic-key").body("synthetic prompt", 100, None)
        reservation = c.usage_reserver(ledger, _cfg(provider="openai"), route="reextract",
                                      project_id=1, unit_key="openai-old")(body, 2)
        maximum = Decimal(reservation["max_cost_usd"])
        model, version = "synthetic-openai-next", "synthetic-openai-next-version"
        prices = MappingProxyType({**previous["prices"], "input": "1", "output": "5"})
        successor = MappingProxyType({**previous, "model": model, "price_version": version, "prices": prices})
        monkeypatch.setattr(c, "PRICE_DEFINITIONS", MappingProxyType({**c.PRICE_DEFINITIONS, (model, version): successor}))
        monkeypatch.setitem(c.PROVIDERS, "openai", model)
        monkeypatch.setattr(c.openai_client, "PRICE_VERSION", version)

        pending = c.usage_report(ledger)
        assert Decimal(pending["providers"]["openai"]["cost_usd"]) == maximum
        assert pending["unresolved_requests"] == 1 and not pending["pricing_unverified"]
        next_cost = c.max_cost(100, input_bound=256, model=model)
        pending_cap = float(next_cost + maximum / 2)
        capped = _cfg(provider="openai", provider_budgets={"openai": {
            "daily_cost_limit_usd": pending_cap, "monthly_cost_limit_usd": pending_cap}})
        assert next_cost < Decimal(str(pending_cap))
        assert c.budget_block_reason(ledger, capped, max_tokens=100, input_bound=256) == "provider_daily_cap"

        metadata = {"model": old_model, "request_fp": reservation["request_fp"], "usage": usage,
                    "http_class": "http_200", "request_id": "synthetic", "error_kind": None}
        c.record_result(ledger, reservation, metadata)
        old_cost = c.usage_cost(usage, model=old_model, price_version=old_version)
        assert old_cost == Decimal("0.000002")
        assert c.usage_cost(usage, model=model) == Decimal("0.00002")
        report = c.usage_report(ledger)
        assert Decimal(report["providers"]["openai"]["cost_usd"]) == old_cost
        assert report["unresolved_requests"] == 0 and not report["pricing_unverified"]
        assert c._tally(ledger).provider_totals["openai"] == old_cost
        settled_cap = float(next_cost + old_cost / 2)
        capped = _cfg(provider="openai", provider_budgets={"openai": {
            "daily_cost_limit_usd": settled_cap, "monthly_cost_limit_usd": settled_cap}})
        assert next_cost < Decimal(str(settled_cap))
        assert c.budget_block_reason(ledger, capped, max_tokens=100, input_bound=256) == "provider_daily_cap"


def test_openai_30m_cache_details_survive_historical_settlement_and_tally(tmp_path, now, monkeypatch):
    monkeypatch.setattr(c, "pricing_valid", lambda *args, **kwargs: True)
    old_model, old_version = c.openai_client.MODEL, c.OPENAI_PRICE_VERSION
    previous = c.PRICE_DEFINITIONS[(old_model, old_version)]
    details = {"cached_tokens": 2, "cache_write_tokens": 4,
               "cache_write_tokens_details": {"ttl_30m_tokens": 4, "ttl_12h_tokens": 0}}
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
             "input_tokens_details": details, "output_tokens_details": {"reasoning_tokens": 0}}
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        body = c.openai_client.OpenAIClient("synthetic-key").body("synthetic prompt", 100, None)
        reservation = c.usage_reserver(ledger, _cfg(provider="openai"), route="reextract",
                                      project_id=1, unit_key="openai-cache")(body, 2)
        model, version = "synthetic-openai-next", "synthetic-openai-next-version"
        prices = MappingProxyType({**previous["prices"], "input": "1", "output": "5"})
        successor = MappingProxyType({**previous, "model": model, "price_version": version, "prices": prices})
        monkeypatch.setattr(c, "PRICE_DEFINITIONS", MappingProxyType({**c.PRICE_DEFINITIONS, (model, version): successor}))
        monkeypatch.setitem(c.PROVIDERS, "openai", model)
        monkeypatch.setattr(c.openai_client, "PRICE_VERSION", version)
        metadata = {"model": old_model, "request_fp": reservation["request_fp"], "usage": usage,
                    "http_class": "http_200", "request_id": "synthetic", "error_kind": None}
        c.record_result(ledger, reservation, metadata)
        saved = json.loads(ledger.artifacts("cloud_usage_result")[0]["meta"])
        assert saved["usage"]["input_tokens_details"] == details
        assert Decimal(saved["cost_usd"]) == Decimal("0.00000192")
        assert c.usage_cost(usage, model=old_model, price_version=old_version) == Decimal("0.00000192")
        report = c.usage_report(ledger)
        assert Decimal(report["providers"]["openai"]["cost_usd"]) == Decimal("0.00000192")
        assert report["unresolved_requests"] == 0 and not report["pricing_unverified"]
        assert c._tally(ledger).provider_totals["openai"] == Decimal("0.00000192")
        assert cloud_cli._calibration(ledger.db, 0)["settled_requests_with_characters"] == 1


@pytest.mark.parametrize("breakdown", [
    {"ttl_30m_tokens": 3, "ttl_12h_tokens": 1},
    {"ttl_30m_tokens": 3, "ttl_12h_tokens": 0},
    {"ttl_30m_tokens": True, "ttl_12h_tokens": 0},
    {"ttl_30m_tokens": 4},
    {"ttl_30m_tokens": 4, "ttl_12h_tokens": 0, "unknown": 0},
])
def test_openai_unpriced_cache_details_do_not_settle_or_release_reservation(tmp_path, now, monkeypatch, breakdown):
    monkeypatch.setattr(c, "pricing_valid", lambda *args, **kwargs: True)
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
             "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 4,
                                      "cache_write_tokens_details": breakdown},
             "output_tokens_details": {"reasoning_tokens": 0}}
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        body = c.openai_client.OpenAIClient("synthetic-key").body("synthetic prompt", 100, None)
        reservation = c.usage_reserver(ledger, _cfg(provider="openai"), route="reextract",
                                      project_id=1, unit_key="openai-cache-invalid")(body, 2)
        metadata = {"model": reservation["model"], "request_fp": reservation["request_fp"], "usage": usage,
                    "http_class": "http_200", "request_id": "synthetic", "error_kind": None}
        with pytest.raises(c.AnthropicError):
            c.record_result(ledger, reservation, metadata)
        assert not ledger.artifacts("cloud_usage_result")
        report = c.usage_report(ledger)
        assert report["unresolved_requests"] == 1
        assert Decimal(report["providers"]["openai"]["cost_usd"]) == Decimal(reservation["max_cost_usd"])
