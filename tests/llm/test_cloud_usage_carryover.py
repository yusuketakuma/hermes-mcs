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
        _carry(ledger, [{"at": now - 60, "provider": "anthropic", "requests": 1, "cost_usd": "0.40", "settled": True},
                        {"at": now - 3 * 86400, "provider": "anthropic", "requests": 1, "cost_usd": "0.10", "settled": True},
                        {"at": now - 40 * 86400, "provider": "anthropic", "requests": 1, "cost_usd": "9", "settled": True}], now)
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
        _carry(ledger, [{"at": now - 60, "provider": "anthropic", "requests": 1, "cost_usd": "0.25", "settled": True}], now)
        reservation = _reserve(ledger)
        c.record_result(ledger, reservation, _result(reservation))
        report = c.usage_report(ledger)
        assert report["daily_requests"] == 2
        assert Decimal(report["monthly_cost_usd"]) == Decimal("0.25") + Decimal("0.000002")


def test_unsettled_carryover_stays_unresolved_at_its_maximum(tmp_path, now):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        _carry(ledger, [{"at": now - 60, "provider": "anthropic", "requests": 2, "cost_usd": "0.30", "settled": False}], now)
        report = c.usage_report(ledger)
        assert report["unresolved_requests"] == 2 and report["daily_requests"] == 2
        assert Decimal(report["max_reserved_cost_usd"]) == Decimal("0.30")
        assert report["providers"]["anthropic"]["unresolved_requests"] == 2


def test_long_lived_tally_rebuilds_when_rows_after_it_are_carried_over(tmp_path, now):
    """Rows written after a cached tally's last row and then removed by a reset leave
    the hashed prefix unchanged; the new carry-over must still reach the totals."""
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        first = _reserve(ledger, unit="first")
        c.record_result(ledger, first, _result(first))
        cached = c._tally(ledger)
        later = _reserve(ledger, unit="later")
        c.record_result(ledger, later, _result(later))
        with ledger.db:
            ledger.db.execute("DELETE FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') "
                              "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.reservation_id') END=?",
                              (later["reservation_id"],))
            ledger.db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
                              (c.CARRYOVER_KIND, json.dumps({"schema": c.CARRYOVER_KIND, "entries": [
                                  {"at": now, "provider": "anthropic", "requests": 1, "cost_usd": "5",
                                   "settled": True}]}), "", "{}", now))
        rebuilt = c._tally(ledger)
        assert rebuilt is not cached
        assert rebuilt.total == Decimal("5") + Decimal("0.000002")


@pytest.mark.parametrize("entries", [
    [{"at": 1, "provider": "anthropic", "requests": 1, "settled": True}],
    [{"at": 1, "provider": "unknown", "requests": 1, "cost_usd": "1", "settled": True}],
    [{"at": 1, "provider": "anthropic", "requests": True, "cost_usd": "1", "settled": True}],
    [{"at": 1, "provider": "anthropic", "requests": 1, "cost_usd": "-1", "settled": True}],
    [{"at": 1, "provider": "anthropic", "requests": 1, "cost_usd": "1", "settled": 1}],
])
def test_malformed_carryover_stops_sending(tmp_path, now, entries):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        _carry(ledger, entries, now)
        assert c.usage_report(ledger)["pricing_unverified"] is True
        assert c.budget_block_reason(ledger, _cfg()) == "pricing_unverified"


def test_maintenance_removes_carryover_only_after_every_entry_ages_out(tmp_path, now):
    import maintenance
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        cutoff = now - maintenance.CLOUD_USAGE_KEEP_S
        old = {"at": cutoff - 10, "provider": "anthropic", "requests": 1, "cost_usd": "1", "settled": True}
        _carry(ledger, [old], cutoff)
        _carry(ledger, [old, {**old, "at": cutoff + 10}], cutoff)
        with ledger.db:
            ledger.db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
                              (c.CARRYOVER_KIND, "not json", "", "{}", cutoff - 10))
        assert maintenance._prune_cloud_usage(ledger.db, cutoff) == 1
        assert len(ledger.artifacts(c.CARRYOVER_KIND)) == 2, "the mixed row and the unreadable row stay"
