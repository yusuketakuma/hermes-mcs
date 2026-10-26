"""Owner-thread synthetic accounting, scope, safe key routing and stop semantics."""
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

import cloud_policy as c
from anthropic_client import AnthropicError, MODEL
from ledger import Ledger


@pytest.fixture
def now(monkeypatch):
    value = datetime(2026, 10, 26, 12, tzinfo=c.JST).timestamp()
    monkeypatch.setattr(c.time, "time", lambda: value)
    return value


def _cfg(**kw):
    return {"cloud_llm": {"mode": "on", "project_ids": [1], **kw}}


def _reserve(db, cfg=None, unit="u", **kw):
    return c.usage_reserver(db, cfg or _cfg(), route="reextract", project_id=1,
                            unit_key=unit, **kw)({"model": MODEL, "max_tokens": 100}, 2)


def _result(r, **kw):
    return {"model": MODEL, "request_fp": r["request_fp"], "usage": {"input_tokens": 10, "output_tokens": 2},
            "http_class": "http_200", "request_id": "req_synthetic", "error_kind": None, **kw}


@pytest.mark.parametrize("scope,allowed", [(None,True), ([],False), ([1],True), ([2],False)])
def test_explicit_scope_and_exclusion(scope, allowed):
    assert c.eligible(_cfg(project_ids=scope), "reextract", 1) is allowed
    assert not c.eligible(_cfg(project_ids=scope, exclude_project_ids=[1]), "reextract", 1)
    assert not c.eligible(_cfg(project_ids=scope), "other", 1)


@pytest.mark.parametrize("settings", [dict(mode="invalid"), dict(project_ids=[True]), dict(model="other"),
                                     dict(project_ids="all"), dict(exclude_project_ids=None),
                                     dict(daily_request_budget=True), dict(monthly_cost_limit_usd=float("nan")),
                                     dict(monthly_cost_limit_usd=float("inf")), dict(extra=True)])
def test_invalid_config_is_off(settings):
    out, errors = c.cloud_config(_cfg(**settings))
    assert errors and out["mode"] == "off"


def test_scope_key_required_and_defaults_stay_off():
    assert c.cloud_config({})[0]["mode"] == "off"
    assert c.cloud_config({"cloud_llm": {"mode": "on"}})[1]
    assert c.cloud_config(_cfg())[0]["daily_request_budget"] is None


def test_key_is_only_mcs_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("MCS_ANTHROPIC_API_KEY", "not-the-mcs-key")
    assert c.api_key(str(tmp_path)) is None
    (tmp_path / ".env").write_text("MCS_ANTHROPIC_API_KEY=synthetic-file-key\n")
    (tmp_path / ".env").chmod(0o600)
    assert c.api_key(str(tmp_path)) == "synthetic-file-key"


def test_unknown_result_never_refunds_and_retry_is_another_request(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        c.record_result(db, r, _result(r, usage=None, http_class="transport", error_kind="transport"))
        _reserve(db, unit="retry")
        report = c.usage_report(db)
        assert report["daily_requests"] == 2 and report["unresolved_requests"] == 2
        assert Decimal(report["monthly_cost_usd"]) == 2 * c.max_cost(100)


def test_usage_settlement_and_duplicate_delivery_are_exact(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        c.record_result(db, r, _result(r))
        c.record_result(db, r, _result(r))
        report = c.usage_report(db)
        assert report["daily_requests"] == 1 and report["unresolved_requests"] == 0
        assert Decimal(report["monthly_cost_usd"]) == Decimal("0.000002")
        assert len(db.artifacts("cloud_usage_result")) == 1


def test_conflicting_result_restores_max_and_requires_explicit_resume(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        c.record_result(db, r, _result(r))
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            c.record_result(db, r, _result(r, usage={"input_tokens": 11, "output_tokens": 2}))
        report = c.usage_report(db)
        assert report["pricing_unverified"]
        assert Decimal(report["monthly_cost_usd"]) == c.max_cost(100)
        assert c.block_reason(db, _cfg(), "reextract", 1) == "pricing_unverified"
        # Later success must not reset a terminal circuit.
        c._circuit_result(db, None)
        assert c.block_reason(db, _cfg(), "reextract", 1) == "pricing_unverified"


@pytest.mark.parametrize("settled", [False, True])
def test_discard_receipt_preserves_billing_and_result_idempotence(tmp_path, now, settled):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        reservation = _reserve(db)
        if settled:
            c.record_result(db, reservation, _result(reservation))
        before = c.usage_report(db)
        results = [dict(row) for row in db.artifacts("cloud_usage_result")]
        with pytest.raises(RuntimeError, match="transaction_required"):
            c.record_disposition_tx(db, reservation, "discarded_scope")
        db.db.execute("BEGIN IMMEDIATE")
        receipt = c.record_disposition_tx(db, reservation, "discarded_scope")
        assert c.record_disposition_tx(db, reservation, "discarded_scope") == receipt
        db.db.commit()
        assert c.usage_report(db) == before
        assert [dict(row) for row in db.artifacts("cloud_usage_result")] == results
        if settled:
            c.record_result(db, reservation, _result(reservation))
            assert c.usage_report(db) == before
        assert len(db.artifacts("cloud_usage_disposition")) == 1


@pytest.mark.parametrize("change", ["project_id", "request_fp", "unit_key", "disposition"])
def test_discard_receipt_rejects_changed_identity_or_reason(tmp_path, now, change):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        reservation = _reserve(db)
        db.db.execute("BEGIN IMMEDIATE")
        c.record_disposition_tx(db, reservation, "discarded_scope")
        db.db.commit()
        edited = dict(reservation)
        reason = "discarded_scope"
        if change == "disposition":
            reason = "discarded_workday"
        else:
            edited[change] = 2 if change == "project_id" else "different"
        db.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(AnthropicError):
            c.record_disposition_tx(db, edited, reason)
        db.db.rollback()
        assert len(db.artifacts("cloud_usage_disposition")) == 1
        db.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="disposition_invalid"):
            c.record_disposition_tx(db, reservation, "free-form-error")
        db.db.rollback()


def test_discard_receipt_rolls_back_with_owner_transaction(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        reservation = _reserve(db)
        db.db.execute("BEGIN IMMEDIATE")
        c.record_disposition_tx(db, reservation, "discarded_workday")
        db.db.rollback()
        assert db.artifacts("cloud_usage_disposition") == []
        assert c.usage_report(db)["unresolved_requests"] == 1


def test_maximum_budget_boundary_and_no_refund(tmp_path, now):
    limit = float(c.max_cost(100))
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db, _cfg(monthly_cost_limit_usd=limit))
        with pytest.raises(AnthropicError, match="budget_exceeded"):
            _reserve(db, _cfg(monthly_cost_limit_usd=limit), unit="second")
        assert c.usage_report(db)["daily_requests"] == 1
        c.record_result(db, r, _result(r))
        _reserve(db, _cfg(monthly_cost_limit_usd=limit + .001), unit="next")


def test_daily_zero_and_null_have_distinct_meaning(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        with pytest.raises(AnthropicError, match="budget_exceeded"):
            _reserve(db, _cfg(daily_request_budget=0))
        _reserve(db, _cfg(daily_request_budget=None))
        assert c.usage_report(db)["daily_requests"] == 1


def test_simultaneous_connections_cannot_overspend(tmp_path, now):
    path = str(tmp_path / "l.db")
    Ledger(path).close()
    cfg = _cfg(monthly_cost_limit_usd=float(c.max_cost(100)))

    def attempt(i):
        with closing(Ledger(path)) as db:
            try:
                _reserve(db, cfg, unit=str(i))
                return True
            except AnthropicError as error:
                assert error.kind == "budget_exceeded"
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt, range(2))) == 1


def test_jst_day_changes_without_month_refund(tmp_path, now, monkeypatch):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        _reserve(db)
        next_day = datetime(2026, 10, 27, tzinfo=c.JST).timestamp()
        report = c.usage_report(db, now=next_day)
        assert report["daily_requests"] == 0
        assert Decimal(report["monthly_cost_usd"]) == c.max_cost(100)


def test_cache_prices_and_long_threshold():
    assert c.usage_cost({"input_tokens": 100000, "output_tokens": 10}) == Decimal("0.010005")
    assert c.usage_cost({"input_tokens": 100001, "output_tokens": 10}) == Decimal("0.0500255")
    assert c.usage_cost({"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 10,
                         "cache_read_input_tokens": 10}) == Decimal("0.0000021")


def test_expired_prices_stop_and_dryrun_is_scope_free(tmp_path, now, monkeypatch):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        with db.db:
            db.db.execute("BEGIN")
            c.dryrun_tx(db, route="reextract", project_id=1, unit_key="private-id", input_chars=123)
        row = db.artifacts("cloud_usage_dryrun")[0]
        assert row["project_id"] is None and row["message_id"] is None
        assert "private-id" not in json.dumps(dict(row))
        assert c.usage_report(db)["daily_requests"] == 0
        monkeypatch.setattr(c.time, "time", lambda: datetime(2026, 11, 26, tzinfo=c.JST).timestamp())
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            _reserve(db)
        assert c.block_reason(db, _cfg(), "reextract", 1) == "pricing_unverified"


def test_dryrun_requires_success_transaction_and_rolls_back_with_it(tmp_path):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        with pytest.raises(RuntimeError, match="cloud_dryrun_transaction_required"):
            c.dryrun_tx(db, route="reextract", project_id=1, unit_key="synthetic")
        with pytest.raises(ValueError, match="synthetic publish failure"):
            with db.db:
                db.artifact_add_tx("synthetic_success", "{}")
                c.dryrun_tx(db, route="reextract", project_id=1, unit_key="synthetic")
                raise ValueError("synthetic publish failure")
        assert db.artifacts("synthetic_success") == []
        assert db.artifacts("cloud_usage_dryrun") == []


@pytest.mark.parametrize("kind", ["auth_error", "payment_required", "pricing_unverified"])
def test_terminal_circuit_only_resume_clears(tmp_path, now, kind):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        c._circuit_result(db, AnthropicError(kind))
        c._circuit_result(db, None)
        assert c.block_reason(db, _cfg(), "reextract", 1) == kind
        c.resume(db)
        assert c.block_reason(db, _cfg(), "reextract", 1) is None
        c.stop(db)
        assert c.block_reason(db, _cfg(), "reextract", 1) == "paused"


def test_borrowed_sqlite_connection_rejects_worker_thread_writes(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(sqlite3.ProgrammingError):
                pool.submit(_reserve, db).result()


def test_repeated_contract_failures_open_but_mixed_classes_do_not(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        for kind in ("contract_error", "transport", "contract_error"):
            c._circuit_result(db, AnthropicError(kind, retryable=kind == "transport"))
        assert c.block_reason(db, _cfg(), "reextract", 1) is None
        c._circuit_result(db, AnthropicError("contract_error"))
        c._circuit_result(db, AnthropicError("contract_error"))
        assert c.block_reason(db, _cfg(), "reextract", 1) == "contract_error"


def test_corrupt_usage_result_never_reduces_reserve(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        metadata = {**r, "usage": {"input_tokens": -1, "output_tokens": 2}, "cost_usd": "0"}
        db.artifact_add("cloud_usage_result", "{}", project_id=1, model=MODEL, meta=metadata)
        report = c.usage_report(db)
        assert report["pricing_unverified"]
        assert Decimal(report["monthly_cost_usd"]) == c.max_cost(100)
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            _reserve(db, unit="blocked")


def test_successful_http_without_usage_stops_and_retains_maximum(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            c.record_result(db, r, _result(r, usage=None))
        assert Decimal(c.usage_report(db)["monthly_cost_usd"]) == c.max_cost(100)
        assert c.block_reason(db, _cfg(), "reextract", 1) == "pricing_unverified"


def test_key_based_auto_mode_preserves_explicit_scope_budget_and_off():
    assert c.cloud_config({}, key_present=True)[0]["mode"] == "on"
    assert c.cloud_config({}, key_present=True)[0]["project_ids"] is None
    assert c.cloud_config({"cloud_llm": {"project_ids": [1]}}, key_present=True)[0]["mode"] == "on"
    assert c.cloud_config({"cloud_llm": {"project_ids": [1]}})[0]["mode"] == "off"
    for mode in ("off", "dry_run", "shadow", "on"):
        normalized, errors = c.cloud_config(_cfg(mode=mode, daily_request_budget=0), key_present=True)
        assert not errors and normalized["mode"] == mode and normalized["daily_request_budget"] == 0
    normalized, errors = c.cloud_config({"cloud_llm": {"project_ids": [], "daily_request_budget": 0}}, key_present=True)
    assert not errors and normalized["project_ids"] == [] and normalized["daily_request_budget"] == 0


def test_active_caller_transaction_is_not_committed_or_reused(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        db.db.execute("BEGIN")
        db.artifact_add_tx("synthetic_caller_work", "{}")
        with pytest.raises(AnthropicError, match="budget_exceeded"):
            _reserve(db)
        assert db.db.in_transaction
        db.db.rollback()
        assert not db.artifacts("synthetic_caller_work")
        assert not db.artifacts("cloud_usage_reserve")


def test_unrequested_output_tokens_are_unpriced(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            c.record_result(db, r, _result(r, usage={"input_tokens": 1, "output_tokens": 101}))
        assert Decimal(c.usage_report(db)["monthly_cost_usd"]) == c.max_cost(100)


def test_broken_reservation_amount_never_creates_budget(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        changed = {**r, "max_cost_usd": "0"}
        db.db.execute("UPDATE artifacts SET meta=? WHERE kind='cloud_usage_reserve'", (json.dumps(changed),))
        db.db.commit()
        assert c.usage_report(db)["pricing_unverified"]


@pytest.mark.parametrize("disposition", ["discarded_scope", "discarded_workday"])
def test_discarded_result_still_settles_actual_usage(tmp_path, now, disposition):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        c.record_result(db, r, _result(r, disposition=disposition))
        meta = json.loads(db.artifacts("cloud_usage_result")[0]["meta"])
        assert meta["disposition"] == disposition
        assert Decimal(c.usage_report(db)["monthly_cost_usd"]) == Decimal("0.000002")


def test_matching_rows_filters_in_sql_and_keeps_all_retained_duplicates(tmp_path, now, monkeypatch):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        reservation = _reserve(db)
        target = reservation["reservation_id"]
        boundary = now - 62 * 86400
        duplicate = db.artifact_add("cloud_usage_reserve", "{}", project_id=1, model=MODEL,
                                    meta={**reservation, "unit_key": "conflicting-binding"})
        db.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (boundary, duplicate))
        expired = db.artifact_add("cloud_usage_reserve", "{}", project_id=1, model=MODEL, meta=reservation)
        db.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (boundary - 1, expired))
        db.db.executemany("INSERT INTO artifacts(kind,meta,created_at) VALUES('cloud_usage_reserve',?,?)",
                          [(json.dumps({"reservation_id": f"{i:032x}"}), now) for i in range(200)])
        db.db.commit()
        statements = []
        db.db.set_trace_callback(statements.append)
        rows = c._matching_rows(db, "cloud_usage_reserve", target)
        assert len(rows) == 2 and expired not in {row["artifact_id"] for row in rows}
        assert {json.loads(row["meta"])["unit_key"] for row in rows} == {"u", "conflicting-binding"}
        selects = [sql for sql in statements if sql.startswith("SELECT")]
        assert len(selects) == 2
        assert all("created_at>=" in sql and "json_valid(meta)" in sql for sql in selects)
        assert "json_extract(meta,'$.reservation_id')" in selects[1]
        assert all("SELECT *" not in sql for sql in selects)


def _late_results(db, count, reserve_at, result_at):
    reservation = _reserve(db)
    db.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_reserve'")
    for index in range(count):
        prior = {**reservation, "reservation_id": f"{index + 10000:032x}", "unit_key": str(index)}
        reserve_id = db.artifact_add_tx("cloud_usage_reserve", "{}", project_id=1, model=MODEL, meta=prior)
        result = {key: prior.get(key) for key in ("reservation_id", "request_fp", "route", "project_id",
                  "unit_key", "message_id", "model", "price_version")}
        result.update(usage=None, cost_usd=prior["max_cost_usd"], http_class="transport", error_kind="transport")
        result_id = db.artifact_add_tx("cloud_usage_result", "{}", project_id=1, model=MODEL, meta=result)
        db.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (reserve_at, reserve_id))
        db.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (result_at, result_id))
    db.db.commit()


@pytest.mark.parametrize("count", [40, 400, 901])
def test_month_crossing_bindings_use_batched_queries_and_linear_python_decoding(tmp_path, now, monkeypatch, count):
    current = datetime(2026, 11, 1, 0, 1, tzinfo=c.JST).timestamp()
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        _late_results(db, count, current - 120, current - 30)
        # Unrelated retained history must not be parsed once for every late result.
        db.db.executemany("INSERT INTO artifacts(kind,meta,created_at) VALUES('cloud_usage_reserve',?,?)",
            [(json.dumps({"reservation_id": f"{index:032x}"}), current - 86400) for index in range(1000)])
        db.db.commit()
        original = c._loads
        decoded = []

        def tracked(raw):
            decoded.append(1)
            return original(raw)

        monkeypatch.setattr(c, "_loads", tracked)
        statements = []
        db.db.set_trace_callback(statements.append)
        report = c.usage_report(db, now=current)
        assert not report["pricing_unverified"] and report["daily_requests"] == 0
        assert Decimal(report["monthly_cost_usd"]) == 0  # All reservations belong to October.
        selects = [sql for sql in statements if sql.startswith("SELECT")]
        assert len(selects) == 2 + (count + 899) // 900
        assert len(decoded) == 2 * count
        assert all("created_at>=" in sql for sql in selects)


@pytest.mark.parametrize("duplicate", [False, True])
def test_month_crossing_binding_at_retention_boundary_is_not_lost(tmp_path, now, duplicate):
    current = datetime(2026, 11, 1, 0, 1, tzinfo=c.JST).timestamp()
    boundary = current - 62 * 86400
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        _late_results(db, 1, boundary, current - 30)
        if duplicate:
            row = db.artifacts("cloud_usage_reserve")[0]
            meta = json.loads(row["meta"])
            conflicting = db.artifact_add("cloud_usage_reserve", "{}", project_id=1, model=MODEL,
                                           meta={**meta, "unit_key": "conflict"})
            db.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (boundary, conflicting))
            db.db.commit()
        report = c.usage_report(db, now=current)
        assert report["pricing_unverified"] is duplicate
        if not duplicate:
            db.db.execute("UPDATE artifacts SET created_at=? WHERE kind='cloud_usage_reserve'", (boundary - 1,))
            db.db.commit()
            assert c.usage_report(db, now=current)["pricing_unverified"]


def test_result_without_any_reservation_still_fails_closed(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        reservation = _reserve(db)
        c.record_result(db, reservation, _result(reservation))
        db.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_reserve'")
        db.db.commit()
        assert c.usage_report(db)["pricing_unverified"]


@pytest.mark.parametrize("invalid_meta", ["{invalid", "[]", "null", "1", None])
def test_retained_corrupt_prior_reservation_stops_cross_month_usage(tmp_path, now, invalid_meta):
    current = datetime(2026, 11, 1, 0, 1, tzinfo=c.JST).timestamp()
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        _late_results(db, 1, current - 120, current - 30)
        db.db.execute("INSERT INTO artifacts(kind,meta,created_at) VALUES('cloud_usage_reserve',?,?)",
                      (invalid_meta, current - 86400))
        db.db.commit()
        assert c.usage_report(db, now=current)["pricing_unverified"]
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            c._matching_rows_many(db, "cloud_usage_reserve", [f"{10000:032x}"], now=current)


@pytest.mark.parametrize("http_class,billed", [("not_sent", False), ("http_429", True),
    ("http_400", True), ("http_529", True), ("http_500", True), ("transport", True), ("timeout", True)])
def test_only_a_proven_unsent_attempt_settles_at_zero(tmp_path, now, http_class, billed):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        c.record_result(db, r, _result(r, usage=None, http_class=http_class, error_kind="rate_limited"))
        report = c.usage_report(db)
        assert not report["pricing_unverified"]
        assert Decimal(report["monthly_cost_usd"]) == (c.max_cost(100) if billed else 0)
        assert report["unresolved_requests"] == int(billed)


@pytest.mark.parametrize("bad", [{"usage": {"input_tokens": -1, "output_tokens": 2}, "cost_usd": "0"},
                                 {"usage": None, "cost_usd": "0", "http_class": 123}])
def test_todays_result_of_yesterdays_reservation_is_validated_in_full(tmp_path, now, monkeypatch, bad):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)                       # reserved today; the result lands tomorrow
        monkeypatch.setattr(c.time, "time", lambda: now + 86400)
        db.artifact_add("cloud_usage_result", "{}", project_id=1, model=MODEL,
                        meta={**{k: r[k] for k in ("reservation_id", "request_fp", "route", "project_id",
                              "unit_key", "message_id", "model", "price_version")}, "error_kind": None,
                              "request_id": None, "http_class": "http_200", **bad})
        assert c.usage_report(db, period="day", now=now + 86400)["pricing_unverified"]


def test_uncapped_reservations_decode_only_today_but_still_catch_corruption(tmp_path, now, monkeypatch):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        c.record_result(db, r, _result(r))
        periods = []
        real = c.usage_report
        monkeypatch.setattr(c, "usage_report", lambda *a, **kw: periods.append(kw.get("period")) or real(*a, **kw))
        _reserve(db, unit="uncapped")
        _reserve(db, _cfg(monthly_cost_limit_usd=10), unit="capped")
        assert periods == ["day", "month"]
        db.artifact_add("cloud_usage_result", "{}", project_id=1, model=MODEL,
                        meta={**r, "usage": {"input_tokens": -1, "output_tokens": 2}, "cost_usd": "0"})
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            _reserve(db, unit="after_corruption")


def test_scope_generation_ignores_caps_rate_and_currency():
    base = c.scope_generation(_cfg())
    assert base == c.scope_generation(_cfg(monthly_cost_limit_usd=5, daily_request_budget=3,
                                           max_concurrency=2, usd_jpy=150))
    assert base != c.scope_generation(_cfg(exclude_project_ids=[1]))
    assert base != c.scope_generation(_cfg(mode="shadow"))


def test_daily_cost_cap_stops_reservations_once_reached(tmp_path, now):
    one = float(c.max_cost(100))
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        _reserve(db, _cfg(daily_cost_limit_usd=one))
        with pytest.raises(AnthropicError, match="budget_exceeded"):
            _reserve(db, _cfg(daily_cost_limit_usd=one), unit="second")
        assert c.budget_block_reason(db, _cfg(daily_cost_limit_usd=one), max_tokens=100) == "daily_cost_cap"
        _reserve(db, _cfg(daily_cost_limit_usd=one * 2 + 0.001), unit="third")


@pytest.mark.parametrize("value", [-1, "5", True])
def test_invalid_daily_cost_cap_turns_the_route_off(value):
    settings, errors = c.cloud_config(_cfg(daily_cost_limit_usd=value))
    assert errors and settings["mode"] == "off"
