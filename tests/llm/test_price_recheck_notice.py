"""Synthetic checks for the price-table recheck notice and the 2026-11-15 rotation."""
from datetime import datetime
from decimal import Decimal

import anthropic_client
import cloud_usage_accounting as accounting


def _at(year, month, day):
    return datetime(year, month, day, 12, tzinfo=accounting.JST).timestamp()


def test_rotation_keeps_the_older_definition_with_identical_prices():
    older = accounting.PRICE_DEFINITIONS[(anthropic_client.MODEL, "anthropic-haiku-5-5-2026-10-26")]
    current = accounting.PRICE_DEFINITIONS[(anthropic_client.MODEL, anthropic_client.PRICE_VERSION)]
    assert anthropic_client.PRICE_VERSION == "anthropic-haiku-5-5-2026-11-15"
    assert dict(older["prices"]) == dict(current["prices"])
    assert (current["price_checked_on"], current["price_valid_until"]) == ("2026-11-15", "2026-12-15")
    usage = {"input_tokens": 1000, "output_tokens": 100}
    assert (accounting.usage_cost(usage, model=anthropic_client.MODEL,
                                  price_version="anthropic-haiku-5-5-2026-10-26")
            == accounting.usage_cost(usage, model=anthropic_client.MODEL) == Decimal("0.00015"))


def test_notice_starts_seven_days_before_the_recheck_date():
    model = anthropic_client.MODEL
    early = accounting.price_info(model, now=_at(2026, 12, 7))
    assert early["price_valid"] and early["price_days_remaining"] == 8 and not early["price_recheck_due"]
    due = accounting.price_info(model, now=_at(2026, 12, 8))
    assert due["price_valid"] and due["price_days_remaining"] == 7 and due["price_recheck_due"]
    last = accounting.price_info(model, now=_at(2026, 12, 15))
    assert last["price_valid"] and last["price_days_remaining"] == 0
    expired = accounting.price_info(model, now=_at(2026, 12, 16))
    assert not expired["price_valid"] and expired["price_recheck_due"] and expired["price_days_remaining"] == -1


def test_new_sends_use_the_table_already_rechecked_on_that_day():
    model = anthropic_client.MODEL
    assert accounting.current_price_version(model, _at(2026, 11, 1)) == "anthropic-haiku-5-5-2026-10-26"
    assert accounting.pricing_valid(_at(2026, 11, 1), model=model)
    assert accounting.current_price_version(model, _at(2026, 11, 15)) == anthropic_client.PRICE_VERSION
    assert accounting.current_price_version(model, _at(2026, 11, 26)) == anthropic_client.PRICE_VERSION
    assert accounting.pricing_valid(_at(2026, 11, 26), model=model)
    # Before any table was rechecked nothing is in force.
    assert not accounting.pricing_valid(_at(2026, 10, 1), model=model)
