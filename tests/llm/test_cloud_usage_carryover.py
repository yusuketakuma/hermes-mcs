"""Synthetic checks: spending carried over by a patient-context reset still counts against every limit."""
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import json

import pytest

import cloud_policy as c
from ledger import Ledger
from test_cloud_policy import _cfg, _reserve, _result


@pytest.fixture
def now(monkeypatch):
    value = datetime(2026, 11, 20, 12, tzinfo=c.JST).timestamp()
    monkeypatch.setattr(c.time, "time", lambda: value)
    return value


def _carry(ledger, entries, created_at):
    with ledger.db:
        ledger.db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
                          (c.CARRYOVER_KIND, json.dumps({"schema": c.CARRYOVER_KIND, "entries": entries}),
                           "", "{}", created_at))


def test_carried_spending_counts_in_day_month_and_provider_totals(tmp_path, now):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        _carry(ledger, [{"at": now - 60, "provider": "anthropic", "requests": 1, "cost_usd": "0.40"},
                        {"at": now - 3 * 86400, "provider": "anthropic", "requests": 1, "cost_usd": "0.10"},
                        {"at": now - 40 * 86400, "provider": "anthropic", "requests": 1, "cost_usd": "9"}], now)
        report = c.usage_report(ledger)
        assert report["daily_requests"] == 1 and not report["pricing_unverified"]
        assert Decimal(report["monthly_cost_usd"]) == Decimal("0.50")
        assert Decimal(report["providers"]["anthropic"]["cost_usd"]) == Decimal("0.50")
        tally = c._tally(ledger)
        day = c._periods(now)[0]
        assert tally.days[day] == [1, Decimal("0.40")] and tally.total == Decimal("0.50")
        assert c.budget_block_reason(ledger, _cfg(daily_cost_limit_usd=0.4)) == "daily_cost_cap"
        assert c.budget_block_reason(ledger, _cfg(daily_request_budget=1)) == "daily_cap"
        assert c.budget_block_reason(ledger, _cfg(daily_cost_limit_usd=1, monthly_cost_limit_usd=0.5)) == "monthly_cap"
        assert c.budget_block_reason(ledger, _cfg(daily_cost_limit_usd=1, monthly_cost_limit_usd=2)) is None


def test_carried_spending_adds_to_new_reservations(tmp_path, now):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        _carry(ledger, [{"at": now - 60, "provider": "anthropic", "requests": 1, "cost_usd": "0.25"}], now)
        reservation = _reserve(ledger)
        c.record_result(ledger, reservation, _result(reservation))
        report = c.usage_report(ledger)
        assert report["daily_requests"] == 2
        assert Decimal(report["monthly_cost_usd"]) == Decimal("0.25") + Decimal("0.000002")


@pytest.mark.parametrize("entries", [
    [{"at": 1, "provider": "anthropic", "requests": 1}],
    [{"at": 1, "provider": "unknown", "requests": 1, "cost_usd": "1"}],
    [{"at": 1, "provider": "anthropic", "requests": 2, "cost_usd": "1"}],
    [{"at": 1, "provider": "anthropic", "requests": 1, "cost_usd": "-1"}],
])
def test_malformed_carryover_stops_sending(tmp_path, now, entries):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        _carry(ledger, entries, now)
        assert c.usage_report(ledger)["pricing_unverified"] is True
        assert c.budget_block_reason(ledger, _cfg()) == "pricing_unverified"
