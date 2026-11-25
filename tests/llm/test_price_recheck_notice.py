"""Synthetic checks for the price-table recheck notice and the 2026-11-24 rotation."""
from datetime import datetime
from decimal import Decimal

import pytest

import anthropic_client
import cloud_usage_accounting as accounting
import openai_client


def _at(year, month, day):
    return datetime(year, month, day, 12, tzinfo=accounting.JST).timestamp()


@pytest.mark.parametrize("model,prefix,october_date", [
    (anthropic_client.MODEL, "anthropic-haiku-5-5", "2026-10-26"),
    (anthropic_client.SONNET_MODEL, "anthropic-sonnet-5-5", None),
    (openai_client.MODEL, "openai-gpt-6-luna", "2026-10-29"),
    (openai_client.SOL_MODEL, "openai-gpt-6.1-sol", "2026-10-31"),
])
def test_dated_rechecks_keep_each_models_original_billing_rules(model, prefix, october_date):
    middle = prefix + "-2026-11-15"
    current = prefix + "-2026-11-24"
    if october_date is None:
        assert not accounting.pricing_valid(_at(2026, 11, 14), model=model)
    else:
        october = prefix + "-" + october_date
        for day in (1, 14):
            assert accounting.current_price_version(model, _at(2026, 11, day)) == october
            assert accounting.pricing_valid(_at(2026, 11, day), model=model)
        assert accounting._price_definition(model, october)["prices"] == accounting._price_definition(model, middle)["prices"]
    for day, version in ((15, middle), (23, middle), (24, current), (26, current)):
        assert accounting.current_price_version(model, _at(2026, 11, day)) == version
        assert accounting.pricing_valid(_at(2026, 11, day), model=model)
    before = accounting._price_definition(model, middle)
    after = accounting._price_definition(model, current)
    assert (before["price_checked_on"], before["price_valid_until"]) == ("2026-11-15", "2026-12-15")
    assert (after["price_checked_on"], after["price_valid_until"]) == ("2026-11-24", "2026-12-24")
    for field in ("prices", "long_input_threshold", "max_tokens", "legacy_reservation"):
        assert before[field] == after[field]


def test_haiku_stored_usage_keeps_its_cost_across_rechecks():
    model = anthropic_client.MODEL
    usage = {"input_tokens": 1000, "output_tokens": 100}
    for day in ("2026-10-26", "2026-11-15", "2026-11-24"):
        assert accounting.usage_cost(usage, model=model, price_version="anthropic-haiku-5-5-" + day) == Decimal("0.00015")
    assert not accounting.pricing_valid(_at(2026, 10, 1), model=model)


def test_notice_starts_seven_days_before_the_recheck_date():
    model = anthropic_client.MODEL
    early = accounting.price_info(model, now=_at(2026, 12, 16))
    assert early["price_valid"] and early["price_days_remaining"] == 8 and not early["price_recheck_due"]
    due = accounting.price_info(model, now=_at(2026, 12, 17))
    assert due["price_valid"] and due["price_days_remaining"] == 7 and due["price_recheck_due"]
    last = accounting.price_info(model, now=_at(2026, 12, 24))
    assert last["price_valid"] and last["price_days_remaining"] == 0
    expired = accounting.price_info(model, now=_at(2026, 12, 25))
    assert not expired["price_valid"] and expired["price_recheck_due"] and expired["price_days_remaining"] == -1


def test_an_undefined_configured_table_stops_instead_of_falling_back(monkeypatch):
    monkeypatch.setattr(anthropic_client, "PRICE_VERSION", "anthropic-haiku-5-5-undefined")
    model = anthropic_client.MODEL
    assert accounting.current_price_version(model, _at(2026, 11, 20)) == "anthropic-haiku-5-5-undefined"
    with pytest.raises(anthropic_client.AnthropicError):
        accounting.max_cost(100, input_bound=100, model=model, now=_at(2026, 11, 20))


def test_notice_follows_the_configured_model_whose_table_runs_out_first():
    import cloud_policy
    cfg = {"cloud_llm": {"mode": "on", "provider": "openai", "model": openai_client.SOL_MODEL,
                         "profiles": {"backlog": "luna-medium"},
                         "project_ids": [1], "daily_cost_limit_usd": 1, "monthly_cost_limit_usd": 10}}
    settings, errors = cloud_policy.resolve_settings(cfg)
    assert not cloud_policy.blocking_errors(errors)
    models = cloud_policy.configured_models(cfg, settings)
    assert models == sorted({openai_client.SOL_MODEL, openai_client.MODEL}), "the backlog profile uses Luna"
    for day, expires in ((20, "2026-12-15"), (24, "2026-12-24")):
        info = cloud_policy.earliest_price_info(cfg, settings, now=_at(2026, 11, day))
        assert (info["price_model"], info["price_valid_until"]) == (openai_client.MODEL, expires)
