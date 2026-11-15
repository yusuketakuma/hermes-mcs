"""Synthetic reopening keeps daily/monthly caps and optional legacy periods binding."""
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

import cloud_policy as policy
from ledger import Ledger
from test_cloud_policy import _body


def settings(**changes):
    return {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "anthropic",
        "daily_cost_limit_usd": 30, "monthly_cost_limit_usd": 300,
        "provider_budgets": {name: {"daily_cost_limit_usd": 15, "monthly_cost_limit_usd": 150}
                             for name in ("anthropic", "openai")},
        "reextract_backlog_until": "2026-11-16T00:00:00+09:00", **changes}}


@pytest.fixture
def clock(monkeypatch):
    now = datetime(2026, 11, 15, 1, tzinfo=policy.JST).timestamp()
    monkeypatch.setattr(policy.time, "time", lambda: now)
    return now


@pytest.mark.parametrize("route", ["reextract", "semantic"])
def test_period_is_optional_but_worker_scope_and_expiry_still_bind(monkeypatch, clock, route):
    cfg = settings(**{route + "_backlog_until": "2026-11-16T00:00:00+09:00"})
    assert policy.cloud_config(cfg)[1] == []
    name = "semantic_backlog" if route == "semantic" else route
    assert not policy.route_open(name, cfg)
    monkeypatch.setenv(policy.BACKLOG_WORKER_ENV, "1")
    assert policy.eligible(cfg, name, 1)
    assert not policy.eligible(cfg, name, 2)
    assert not policy.route_open("attachment", cfg)
    cfg["cloud_llm"][route + "_backlog_until"] = "2026-11-15T01:00:00+09:00"
    assert not policy.route_open(name, cfg)
    if route == "semantic":
        assert policy.semantic_planned(cfg)  # a paid answer keeps its contract after expiry


@pytest.mark.parametrize("change", [
    {"daily_cost_limit_usd": None}, {"monthly_cost_limit_usd": None},
    {"provider_budgets": {}},
    {"provider": "auto", "provider_budgets": {
        "anthropic": {"daily_cost_limit_usd": 15, "monthly_cost_limit_usd": 150}}},
    {"window_start": "2026-11-15T00:00:00+09:00"},
    {"window_budget_usd": 5},
])
def test_new_reopening_rejects_missing_caps_or_half_a_legacy_period(change, clock):
    cfg = settings(**change)
    out, errors = policy.cloud_config(cfg)
    assert errors and out["mode"] == "off"
    assert not policy.reextract_backlog_window(cfg)


def test_period_removal_skips_only_window_accounting_and_preserves_reservations(tmp_path, monkeypatch, clock):
    monkeypatch.setenv(policy.BACKLOG_WORKER_ENV, "1")
    cfg = settings()
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        with monkeypatch.context() as patch:
            patch.setattr(policy, "_window_tally", lambda *a: pytest.fail("no period was configured"))
            reserve = policy.usage_reserver(ledger, cfg, route="reextract", project_id=1, unit_key="synthetic")
            receipt = reserve(_body("synthetic"), 2)
            assert receipt["max_cost_usd"] != "0"
            assert len(ledger.artifacts("cloud_usage_reserve")) == 1
            assert policy.budget_block_reason(ledger, cfg, max_tokens=100) is None
        legacy = settings(window_start="2026-11-15T00:00:00+09:00", window_budget_usd=0)
        assert policy.budget_block_reason(ledger, legacy, max_tokens=100) == "window_cap"


@pytest.mark.parametrize("reason,day_total,month_total,provider_day,provider_month", [
    ("daily_cost_cap", 30, 30, 0, 0),
    ("monthly_cap", 0, 300, 0, 0),
    ("provider_daily_cap", 0, 0, 15, 15),
    ("provider_monthly_cap", 0, 0, 0, 150),
])
def test_all_four_caps_are_checked_against_the_same_tally(
        monkeypatch, clock, reason, day_total, month_total, provider_day, provider_month):
    day = policy._periods(clock)[0]
    tally = SimpleNamespace(invalid=False, days={day: [1, Decimal(day_total)]},
        total=Decimal(month_total), provider_days={("anthropic", day): Decimal(provider_day)},
        provider_totals={"anthropic": Decimal(provider_month)})
    observed = []

    def read(ledger, now):
        observed.append(now)
        return tally

    monkeypatch.setattr(policy, "_tally", read)
    assert policy.budget_block_reason(None, settings(), max_tokens=100) == reason
    assert observed == [clock]
