"""Owner-thread synthetic accounting, scope, safe key routing and stop semantics."""
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

import cloud_usage_accounting
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


def _body(unit):
    # Distinct accounting units here represent distinct, provable synthetic prompts.
    prompt = hashlib.sha256(unit.encode("utf-8")).hexdigest()
    return {"model": MODEL, "max_tokens": 100, "system": "synthetic system",
            "messages": [{"role": "user", "content": prompt}]}


def _reserve(db, cfg=None, unit="u", **kw):
    return c.usage_reserver(db, cfg or _cfg(), route="patient_context", project_id=1,
                            unit_key=unit, **kw)(_body(unit), 2)


def _max():
    """Each synthetic unit has the same input bound and reservation maximum."""
    return c.max_cost(100, input_bound=c.input_bound(_body("u")))


def _result(r, **kw):
    return {"model": MODEL, "request_fp": r["request_fp"], "usage": {"input_tokens": 10, "output_tokens": 2},
            "http_class": "http_200", "request_id": "req_synthetic", "error_kind": None, **kw}


@pytest.mark.parametrize('route,purpose,model,effort,limit', [
    ('reextract', 'backlog', 'gpt-6-luna', 'medium', 8192),
    ('semantic_backlog', 'backlog', 'gpt-6-luna', 'medium', 8192),
    ('patient_context', 'patient_context', 'gpt-6.1-sol', 'medium', 16384),
    ('semantic_backlog', 'quality_repair', 'gpt-6.1-sol', 'high', 32768),
])
def test_openai_profile_resolution_is_purpose_bound(route, purpose, model, effort, limit):
    selection, errors = c.resolve_profile(_cfg(provider='openai'), route=route, purpose=purpose)
    assert not errors
    assert (selection['model'], selection['effort'], selection['max_tokens']) == (model, effort, limit)
    with pytest.raises(TypeError):
        selection['model'] = 'other'


def test_explicit_legacy_model_and_invalid_profile_fail_closed():
    cfg = _cfg(provider='openai', model='gpt-6-luna')
    selection, errors = c.resolve_profile(cfg, route='patient_context', purpose='patient_context')
    assert not errors and (selection['model'], selection['effort'], selection['max_tokens']) == ('gpt-6-luna', 'low', 16384)
    cfg['cloud_llm']['profiles'] = {'patient_context': 'sol-medium'}
    selection, errors = c.resolve_profile(cfg, route='patient_context', purpose='patient_context')
    assert not errors and selection['model'] == 'gpt-6.1-sol'
    cfg['cloud_llm']['profiles'] = {'patient_context': 'unknown'}
    assert c.resolve_profile(cfg, route='patient_context', purpose='patient_context')[0] is None
    assert c.resolve_profile(_cfg(provider='openai'), route='reextract', purpose='patient_context')[0] is None


def test_explicit_sol_model_keeps_medium_for_formal_quality_repair():
    cfg = _cfg(provider='openai', model='gpt-6.1-sol')
    normal, errors = c.resolve_profile(cfg, route='patient_context', purpose='patient_context')
    assert not errors and (normal['model'], normal['effort'], normal['max_tokens']) == (
        'gpt-6.1-sol', 'medium', 16384)
    repair, errors = c.resolve_profile(cfg, route='patient_context', purpose='quality_repair')
    assert not errors and (repair['model'], repair['effort'], repair['max_tokens']) == (
        'gpt-6.1-sol', 'medium', 32768)


def test_reservation_covers_the_costliest_accepted_usage_for_the_request():
    body = {"model": MODEL, "max_tokens": 100,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "a" * 120000, "cache_control": {"type": "ephemeral"}}]}]}
    bound = c.input_bound(body)
    assert bound > c.LONG_INPUT_THRESHOLD   # long-context prices, where the write premium bites
    worst = [{"input_tokens": 0, "cache_creation_input_tokens": bound, "output_tokens": 100,
              "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": bound}},
             {"input_tokens": 0, "cache_creation_input_tokens": bound, "output_tokens": 100}]  # unknown TTL
    assert all(c.usage_cost(u) <= c.max_cost(100, input_bound=bound) for u in worst)


def test_null_cloud_block_is_a_config_error_not_an_omission():
    settings, errors = c.cloud_config({"cloud_llm": None}, key_present=True)
    assert errors == ["config: cloud_llm_not_object"] and settings["mode"] == "off"
    # An absent block uses the spending defaults; a malformed block never does.
    assert c.cloud_config({}, key_present=True)[0]["mode"] == "on"
    assert c.cloud_config({"cloud_llm": {"monthly_cost_limit_usd": 40}}, key_present=True)[0]["mode"] == "on"


@pytest.mark.parametrize("scope,allowed", [(None,True), ([],False), ([1],True), ([2],False)])
def test_explicit_scope_and_exclusion(scope, allowed):
    # Historical scope stays readable on closed routes; new sends additionally
    # require an open send route, which only patient_context has.
    assert c.in_scope(_cfg(project_ids=scope), "reextract", 1) is allowed
    assert not c.in_scope(_cfg(project_ids=scope, exclude_project_ids=[1]), "reextract", 1)
    assert not c.in_scope(_cfg(project_ids=scope), "other", 1)
    assert c.eligible(_cfg(project_ids=scope), "patient_context", 1) is allowed
    assert not c.eligible(_cfg(project_ids=scope), "reextract", 1)


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
        assert Decimal(report["monthly_cost_usd"]) == 2 * _max()


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
        assert Decimal(report["monthly_cost_usd"]) == _max()
        assert c.block_reason(db, _cfg(), "patient_context", 1) == "pricing_unverified"
        # Later success must not reset a terminal circuit.
        c._circuit_result(db, None)
        assert c.block_reason(db, _cfg(), "patient_context", 1) == "pricing_unverified"


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
        # Pre-existing legacy receipt has no profile fields; replay must not rewrite it.
        assert "effort" not in reservation and "profile" not in reservation
        old = {key: reservation.get(key) for key in c._BINDING_KEYS if key not in ("effort", "profile", "input_binding")}
        old["disposition"] = "discarded_scope"
        receipt = db.artifact_add("cloud_usage_disposition", "{}", project_id=1, model=MODEL, meta=old)
        old_rows = [dict(row) for row in db.artifacts("cloud_usage_disposition")]
        db.db.execute("BEGIN IMMEDIATE")
        assert c.record_disposition_tx(db, reservation, "discarded_scope") == receipt
        db.db.commit()
        assert [dict(row) for row in db.artifacts("cloud_usage_disposition")] == old_rows
        assert c.usage_report(db) == before
        assert [dict(row) for row in db.artifacts("cloud_usage_result")] == results
        if settled:
            c.record_result(db, reservation, _result(reservation))
            assert c.usage_report(db) == before
        assert len(db.artifacts("cloud_usage_disposition")) == 1


def test_discard_receipt_preserves_new_profile_fields_and_rejects_partial_binding(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        original = _reserve(db)
        reservation = {**original, "effort": None, "profile": None}
        db.db.execute("UPDATE artifacts SET meta=? WHERE kind='cloud_usage_reserve'", (json.dumps(reservation),))
        db.db.commit()
        db.db.execute("BEGIN IMMEDIATE")
        receipt = c.record_disposition_tx(db, reservation, "discarded_scope")
        assert c.record_disposition_tx(db, reservation, "discarded_scope") == receipt
        db.db.commit()
        rows = db.artifacts("cloud_usage_disposition")
        assert len(rows) == 1
        stored = json.loads(rows[0]["meta"])
        assert "effort" in stored and stored["effort"] is None
        assert "profile" in stored and stored["profile"] is None
        assert c.usage_report(db)["unresolved_requests"] == 1
        damaged = {key: value for key, value in reservation.items() if key != "profile"}
        db.db.execute("UPDATE artifacts SET meta=? WHERE kind='cloud_usage_reserve'", (json.dumps(damaged),))
        db.db.commit()
        db.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            c.record_disposition_tx(db, damaged, "discarded_scope")
        db.db.rollback()
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
    limit = float(_max())
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
    cfg = _cfg(monthly_cost_limit_usd=float(_max()))

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
        assert Decimal(report["monthly_cost_usd"]) == _max()


@pytest.mark.parametrize('start,finish,same_month', [
    ((2026, 10, 26, 23, 59), (2026, 10, 27, 0, 0), True),
    ((2026, 10, 31, 23, 59), (2026, 11, 1, 0, 0), False),
])
def test_cloud_result_across_midnight_stays_bound_to_reservation_period(tmp_path, monkeypatch, start, finish, same_month):
    clock = [datetime(*start, tzinfo=c.JST).timestamp()]
    monkeypatch.setattr(c.time, 'time', lambda: clock[0])
    with closing(Ledger(str(tmp_path / 'l.db'))) as db:
        reservation = _reserve(db)
        assert c.usage_report(db)['daily_requests'] == 1
        clock[0] = datetime(*finish, tzinfo=c.JST).timestamp()
        c.record_result(db, reservation, _result(reservation))
        report = c.usage_report(db)
        assert not report['pricing_unverified'] and report['daily_requests'] == 0
        assert Decimal(report['monthly_cost_usd']) == (Decimal('0.000002') if same_month else Decimal(0))
        _reserve(db, unit='new-day')
        assert c.usage_report(db)['daily_requests'] == 1
        assert len(db.artifacts('cloud_usage_result')) == 1


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
        assert c.block_reason(db, _cfg(), "patient_context", 1) == "pricing_unverified"


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
        assert c.block_reason(db, _cfg(), "patient_context", 1) == kind
        c.resume(db)
        assert c.block_reason(db, _cfg(), "patient_context", 1) is None
        c.stop(db)
        assert c.block_reason(db, _cfg(), "patient_context", 1) == "paused"


def test_openai_quota_429_is_reconstructed_as_terminal(tmp_path, now, monkeypatch):
    import openai_client
    import semantic_runtime

    for module in (c, cloud_usage_accounting):
        monkeypatch.setattr(module, "pricing_valid", lambda *args, **kwargs: True)
    seen = []
    original = semantic_runtime._record_cloud_circuit_result_tx
    def observe(ledger, error, *args, **kwargs):
        assert ledger.db.in_transaction
        assert len(ledger.artifacts("cloud_usage_result")) == 1
        seen.append(error)
        return original(ledger, error, *args, **kwargs)
    monkeypatch.setattr(semantic_runtime, "_record_cloud_circuit_result_tx", observe)
    with closing(Ledger(str(tmp_path / "l.db"))) as ledger:
        cfg = _cfg(provider="openai")
        body = openai_client.OpenAIClient("synthetic").body("synthetic", 100, None)
        reservation = c.usage_reserver(ledger, cfg, route="patient_context", project_id=1,
                                       unit_key="synthetic")(body, 2)
        c.record_result(ledger, reservation, {
            "model": openai_client.MODEL, "request_fp": reservation["request_fp"],
            "usage": None, "http_class": "http_429", "error_kind": "payment_required",
            "openai_error_code": "credit_balance_exhausted"})
        assert len(seen) == 1 and seen[0].kind == "payment_required" and seen[0].retryable is False
        assert seen[0].status == 429
        assert not ledger.db.in_transaction and len(ledger.artifacts("cloud_circuit")) == 1
        assert c.global_block_reason(ledger, model=openai_client.MODEL) == "payment_required"
        assert c.usage_report(ledger)["max_reserved_cost_usd"] == reservation["max_cost_usd"]


def test_borrowed_sqlite_connection_rejects_worker_thread_writes(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(sqlite3.ProgrammingError):
                pool.submit(_reserve, db).result()


def test_repeated_contract_failures_open_but_mixed_classes_do_not(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        for kind in ("contract_error", "transport", "contract_error"):
            c._circuit_result(db, AnthropicError(kind, retryable=kind == "transport"))
        assert c.block_reason(db, _cfg(), "patient_context", 1) is None
        c._circuit_result(db, AnthropicError("contract_error"))
        c._circuit_result(db, AnthropicError("contract_error"))
        assert c.block_reason(db, _cfg(), "patient_context", 1) == "contract_error"


def test_corrupt_usage_result_never_reduces_reserve(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        metadata = {**r, "usage": {"input_tokens": -1, "output_tokens": 2}, "cost_usd": "0"}
        db.artifact_add("cloud_usage_result", "{}", project_id=1, model=MODEL, meta=metadata)
        report = c.usage_report(db)
        assert report["pricing_unverified"]
        assert Decimal(report["monthly_cost_usd"]) == _max()
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            _reserve(db, unit="blocked")


def test_successful_http_without_usage_stops_and_retains_maximum(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            c.record_result(db, r, _result(r, usage=None))
        assert Decimal(c.usage_report(db)["monthly_cost_usd"]) == _max()
        assert c.block_reason(db, _cfg(), "patient_context", 1) == "pricing_unverified"


def test_key_based_auto_mode_preserves_explicit_scope_budget_and_off():
    capped = {"daily_cost_limit_usd": 2}
    assert c.cloud_config({"cloud_llm": capped}, key_present=True)[0]["mode"] == "on"
    assert c.cloud_config({"cloud_llm": capped}, key_present=True)[0]["project_ids"] is None
    assert c.cloud_config({"cloud_llm": {**capped, "project_ids": [1]}}, key_present=True)[0]["mode"] == "on"
    assert c.cloud_config({"cloud_llm": {"project_ids": [1]}})[0]["mode"] == "off"
    for mode in ("off", "dry_run", "shadow", "on"):
        normalized, errors = c.cloud_config(_cfg(mode=mode, daily_request_budget=0), key_present=True)
        assert not errors and normalized["mode"] == mode and normalized["daily_request_budget"] == 0
    normalized, errors = c.cloud_config({"cloud_llm": {"project_ids": [], "daily_request_budget": 0}}, key_present=True)
    assert not errors and normalized["project_ids"] == [] and normalized["daily_request_budget"] == 0


@pytest.mark.parametrize("daily,monthly", [(None, None), (0, 0), (1.5, 30)])
def test_explicit_cost_caps_survive_normalization_and_keep_scope(daily, monthly):
    cfg = _cfg(mode="off", daily_cost_limit_usd=daily, monthly_cost_limit_usd=monthly,
               exclude_project_ids=[2], daily_request_budget=0)
    before = json.dumps(cfg, sort_keys=True)
    normalized, errors = c.cloud_config(cfg, key_present=True)
    assert errors == [] and normalized["mode"] == "off"
    assert (normalized["daily_cost_limit_usd"], normalized["monthly_cost_limit_usd"]) == (daily, monthly)
    assert normalized["project_ids"] == [1] and normalized["exclude_project_ids"] == [2]
    assert normalized["daily_request_budget"] == 0
    assert json.dumps(cfg, sort_keys=True) == before


@pytest.mark.parametrize("block", [{}, {"daily_cost_limit_usd": 2}, {"monthly_cost_limit_usd": None}])
def test_missing_cost_caps_follow_defaults_without_pinning_config(block):
    cfg = {"cloud_llm": dict(block)}
    normalized, errors = c.cloud_config(cfg, key_present=True)
    assert errors == [] and normalized["mode"] == "on"
    assert normalized["daily_cost_limit_usd"] == block.get("daily_cost_limit_usd", 25)
    assert normalized["monthly_cost_limit_usd"] == block.get("monthly_cost_limit_usd", 75)
    assert cfg == {"cloud_llm": block}
    assert c.cloud_config(cfg, key_present=False)[0]["mode"] == "off"


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
        assert Decimal(c.usage_report(db)["monthly_cost_usd"]) == _max()


def test_broken_reservation_amount_never_creates_budget(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        changed = {**r, "max_cost_usd": "0"}
        db.db.execute("UPDATE artifacts SET meta=? WHERE kind='cloud_usage_reserve'", (json.dumps(changed),))
        db.db.commit()
        assert c.usage_report(db)["pricing_unverified"]
        # the reservation gate's incremental tally notices the edited row too
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            _reserve(db, unit="after_edit", message_id=1)


def test_a_reservation_stored_before_input_bound_keeps_its_plain_input_amount(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        legacy = {k: v for k, v in r.items() if k != "input_bound"}
        legacy["max_cost_usd"] = str(c._reservation_cost(c.INPUT_RESERVATION_BOUND, 100, legacy=True))
        db.db.execute("UPDATE artifacts SET meta=? WHERE kind='cloud_usage_reserve'", (json.dumps(legacy),))
        db.db.commit()
        assert not c.usage_report(db)["pricing_unverified"]
        assert Decimal(c.usage_report(db)["monthly_cost_usd"]) == Decimal(legacy["max_cost_usd"])


@pytest.mark.parametrize("disposition", ["discarded_scope", "discarded_workday"])
def test_discarded_result_still_settles_actual_usage(tmp_path, now, disposition):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        c.record_result(db, r, _result(r, disposition=disposition))
        meta = json.loads(db.artifacts("cloud_usage_result")[0]["meta"])
        assert meta["disposition"] == disposition
        assert Decimal(c.usage_report(db)["monthly_cost_usd"]) == Decimal("0.000002")


def test_legacy_result_replay_preserves_existing_row_without_profile_backfill(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        reservation = _reserve(db)
        assert "effort" not in reservation and "profile" not in reservation
        metadata = _result(reservation)
        usage = c._price_definition(MODEL, reservation["price_version"])["validate_usage"](metadata["usage"])
        old = {key: reservation.get(key) for key in c._BINDING_KEYS if key not in ("effort", "profile", "input_binding")}
        old.update(usage=usage, cost_usd=str(c.usage_cost(usage, model=MODEL,
                    price_version=reservation["price_version"])), http_class="http_200",
                   request_id="req_synthetic", error_kind=None)
        db.artifact_add("cloud_usage_result", "{}", project_id=1, model=MODEL, meta=old)
        before = [dict(row) for row in db.artifacts("cloud_usage_result")]
        c.record_result(db, reservation, metadata)
        assert [dict(row) for row in db.artifacts("cloud_usage_result")] == before
        assert not c.usage_report(db)["pricing_unverified"]


@pytest.mark.parametrize("missing", ["effort", "profile"])
def test_result_rejects_reservation_with_only_one_profile_field(tmp_path, now, missing):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        reservation = _reserve(db)
        damaged = {**reservation, "effort": None, "profile": None}
        del damaged[missing]
        db.db.execute("UPDATE artifacts SET meta=? WHERE kind='cloud_usage_reserve'", (json.dumps(damaged),))
        db.db.commit()
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            c.record_result(db, damaged, _result(damaged))
        assert not db.artifacts("cloud_usage_result")


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
        anchors = [sql for sql in selects if sql.startswith("SELECT artifact_id FROM (SELECT artifact_id")]
        timestamp_checks = [sql for sql in selects if sql.startswith("SELECT created_at FROM artifacts")]
        retained = [sql for sql in selects if sql not in anchors and sql not in timestamp_checks]
        assert len(anchors) == len(timestamp_checks) == 1
        assert "UNION ALL SELECT NULL" in anchors[0]
        assert "typeof(created_at)" in timestamp_checks[0]
        assert len(retained) == 2
        assert all("created_at>=" in sql and "json_valid(meta)" in sql for sql in retained)
        assert "json_extract(meta,'$.reservation_id')" in retained[1]
        assert all("SELECT *" not in sql for sql in selects)


def _late_results(db, count, reserve_at, result_at):
    reservation = _reserve(db)
    db.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_reserve'")
    for index in range(count):
        prior = {**reservation, "reservation_id": f"{index + 10000:032x}", "unit_key": str(index)}
        reserve_id = db.artifact_add_tx("cloud_usage_reserve", "{}", project_id=1, model=MODEL, meta=prior)
        result = {key: prior.get(key) for key in c._BINDING_KEYS
                  if key not in ("effort", "profile") or key in prior}
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
        original = c._accounting_meta
        decoded = []

        def tracked(raw):
            decoded.append(1)
            return original(raw)

        monkeypatch.setattr(c, "_accounting_meta", tracked)
        monkeypatch.setattr(cloud_usage_accounting, "_accounting_meta", tracked)
        statements = []
        db.db.set_trace_callback(statements.append)
        report = c.usage_report(db, now=current)
        assert not report["pricing_unverified"] and report["daily_requests"] == 0
        assert Decimal(report["monthly_cost_usd"]) == 0  # All reservations belong to October.
        selects = [sql for sql in statements if sql.startswith("SELECT")]
        anchors = [sql for sql in selects if sql.startswith("SELECT artifact_id FROM (SELECT artifact_id")]
        timestamp_checks = [sql for sql in selects if sql.startswith("SELECT created_at FROM artifacts")]
        period_queries = [sql for sql in selects if sql not in anchors and sql not in timestamp_checks]
        assert len(anchors) == len(timestamp_checks) == 1
        assert "UNION ALL SELECT NULL" in anchors[0]
        assert "typeof(created_at)" in timestamp_checks[0]
        assert len(period_queries) == 2 + (count + 899) // 900
        assert len(decoded) == 2 * count
        assert all("created_at>=" in sql for sql in period_queries)


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
        assert Decimal(report["monthly_cost_usd"]) == (_max() if billed else 0)
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


def test_reservation_gate_decodes_each_usage_row_once_but_still_catches_corruption(tmp_path, now, monkeypatch):
    with closing(Ledger(str(tmp_path / "l.db"))) as db:
        r = _reserve(db)
        c.record_result(db, r, _result(r))
        original = c._accounting_meta
        decoded = []
        monkeypatch.setattr(c, "_accounting_meta", lambda raw: decoded.append(1) or original(raw))
        monkeypatch.setattr(cloud_usage_accounting, "_accounting_meta", lambda raw: decoded.append(1) or original(raw))
        _reserve(db, unit="second")
        seen = len(decoded)
        assert seen >= 2
        _reserve(db, _cfg(monthly_cost_limit_usd=10), unit="capped")
        # only the rows written since the previous gate are decoded again
        assert len(decoded) - seen <= 2
        assert c.usage_report(db)["daily_requests"] == 3
        db.artifact_add("cloud_usage_result", "{}", project_id=1, model=MODEL,
                        meta={**r, "usage": {"input_tokens": -1, "output_tokens": 2}, "cost_usd": "0"})
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            _reserve(db, unit="after_corruption")
        with pytest.raises(AnthropicError, match="pricing_unverified"):
            _reserve(db, unit="still_blocked")


def test_scope_generation_ignores_caps_rate_and_currency():
    base = c.scope_generation(_cfg())
    assert base == c.scope_generation(_cfg(monthly_cost_limit_usd=5, daily_request_budget=3,
                                           max_concurrency=2, usd_jpy=150))
    assert base != c.scope_generation(_cfg(exclude_project_ids=[1]))
    assert base != c.scope_generation(_cfg(mode="shadow"))


def test_daily_cost_cap_stops_reservations_once_reached(tmp_path, now):
    one = float(_max())
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


def test_reservation_hook_commits_its_slot_with_real_usage(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as ledger:
        ledger.db.execute("CREATE TABLE synthetic_slot(reservation_id TEXT, request_fp TEXT)")
        ledger.db.commit()
        seen = []
        def on_reserved(owner, meta, artifact_id):
            assert owner is ledger and owner.db.in_transaction
            row = owner.db.execute("SELECT kind,meta FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            assert row[0] == "cloud_usage_reserve" and json.loads(row[1]) == meta
            seen.append(artifact_id)
            owner.db.execute("INSERT INTO synthetic_slot VALUES(?,?)", (meta["reservation_id"], meta["request_fp"]))
            return 1
        before = ledger.db.total_changes
        reservation = _reserve(ledger, on_reserved_tx=on_reserved)
        assert not ledger.db.in_transaction and ledger.db.total_changes - before == 2
        assert len(seen) == 1
        assert tuple(ledger.db.execute("SELECT * FROM synthetic_slot").fetchone()) == (
            reservation["reservation_id"], reservation["request_fp"])
        assert c.usage_report(ledger)["unresolved_requests"] == 1


@pytest.mark.parametrize("failure", ["raise", "count", "receipt"])
def test_reservation_hook_failure_rolls_back_usage_and_slot(tmp_path, now, failure):
    with closing(Ledger(str(tmp_path / "l.db"))) as ledger:
        ledger.db.execute("CREATE TABLE synthetic_slot(value TEXT)")
        ledger.db.commit()
        def on_reserved(owner, meta, artifact_id):
            owner.db.execute("INSERT INTO synthetic_slot VALUES('spent')")
            if failure == "raise":
                raise RuntimeError("synthetic")
            if failure == "receipt":
                owner.db.execute("UPDATE artifacts SET content='changed' WHERE artifact_id=?", (artifact_id,))
                return 2
            return 2
        with pytest.raises(RuntimeError):
            _reserve(ledger, on_reserved_tx=on_reserved)
        assert not ledger.db.in_transaction
        assert not ledger.artifacts("cloud_usage_reserve")
        assert ledger.db.execute("SELECT count(*) FROM synthetic_slot").fetchone()[0] == 0
        assert c.usage_report(ledger)["daily_requests"] == 0


def test_reservation_hook_never_commits_borrowed_transaction(tmp_path, now):
    with closing(Ledger(str(tmp_path / "l.db"))) as ledger:
        ledger.db.execute("CREATE TABLE synthetic_slot(value TEXT)")
        ledger.db.commit()
        ledger.db.execute("INSERT INTO synthetic_slot VALUES('owner')")
        def on_reserved(*args):
            pytest.fail("must not run while the caller owns a transaction")
        with pytest.raises(AnthropicError) as error:
            _reserve(ledger, on_reserved_tx=on_reserved)
        assert error.value.kind == "budget_exceeded" and error.value.detail == "owner_transaction_active"
        assert ledger.db.in_transaction
        assert ledger.db.execute("SELECT count(*) FROM synthetic_slot").fetchone()[0] == 1
        ledger.db.rollback()
        assert ledger.db.execute("SELECT count(*) FROM synthetic_slot").fetchone()[0] == 0
        assert not ledger.artifacts("cloud_usage_reserve")


def test_retained_original_receipt_is_verified_by_exact_id_without_a_62_day_lookup_window(tmp_path, now):
    with closing(Ledger(str(tmp_path / 'l.db'))) as ledger:
        reservation = _reserve(ledger)
        c.record_result(ledger, reservation, _result(reservation))
        aid = ledger.artifacts('cloud_usage_reserve')[0]['artifact_id']
        before = c.reservation_evidence(ledger.db, aid)
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET created_at=created_at-?', (80 * 86400,))
        after = c.reservation_evidence(ledger.db, aid)
        assert after['reservation'] == before['reservation'] == reservation
        assert after['result'] == before['result'] and after['witness'] != before['witness']
        ledger.db.execute('BEGIN')
        changes = ledger.db.total_changes
        assert c.reservation_evidence(ledger.db, aid) == after
        assert ledger.db.in_transaction and ledger.db.total_changes == changes
        ledger.db.rollback()


@pytest.mark.parametrize('damage', ['missing','duplicate','request_fp','scope','unknown','disposition'])
def test_original_receipt_evidence_rejects_ambiguous_or_changed_accounting(tmp_path, now, damage):
    with closing(Ledger(str(tmp_path / 'l.db'))) as ledger:
        reservation = _reserve(ledger)
        c.record_result(ledger, reservation, _result(reservation))
        aid = ledger.artifacts('cloud_usage_reserve')[0]['artifact_id']
        with ledger.db:
            if damage == 'missing':
                ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_usage_result'")
            elif damage == 'duplicate':
                ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,model,content,meta,created_at) "
                    "SELECT kind,project_id,message_id,model,content,meta,created_at FROM artifacts WHERE kind='cloud_usage_result'")
            elif damage == 'request_fp':
                ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.request_fp',?) WHERE kind='cloud_usage_result'", ('f' * 64,))
            elif damage == 'scope':
                ledger.db.execute("UPDATE artifacts SET project_id=2 WHERE kind='cloud_usage_result'")
            elif damage == 'unknown':
                ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.http_class','transport','$.error_kind','timeout') WHERE kind='cloud_usage_result'")
            else:
                ledger.db.execute('BEGIN')
                c.record_disposition_tx(ledger, reservation, 'discarded_scope')
        with pytest.raises(ValueError, match='cloud_receipt_invalid'):
            c.reservation_evidence(ledger.db, aid)


@pytest.mark.parametrize('damage', ['reserve_duplicate', 'reserve_nonfinite', 'result_http_duplicate',
                                  'result_usage_duplicate', 'result_nonfinite', 'result_nan',
                                  'hidden_disposition', 'foreign_hidden_disposition', 'unmatched_disposition_duplicate'])
def test_original_receipt_evidence_strictly_rejects_duplicate_and_nonfinite_json(tmp_path, now, damage):
    with closing(Ledger(str(tmp_path / 'l.db'))) as ledger:
        reservation = _reserve(ledger)
        c.record_result(ledger, reservation, _result(reservation))
        aid = ledger.artifacts('cloud_usage_reserve')[0]['artifact_id']
        assert c.reservation_evidence(ledger.db, aid)['reservation'] == reservation
        with ledger.db:
            if damage in ('hidden_disposition', 'foreign_hidden_disposition'):
                ledger.db.execute('BEGIN')
                c.record_disposition_tx(ledger, reservation, 'discarded_scope')
                target = ledger.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='cloud_usage_disposition'").fetchone()
                different = ('0' if reservation['reservation_id'][0] != '0' else '1') + reservation['reservation_id'][1:]
                raw = '{"reservation_id":' + json.dumps(different) + ',' + target['meta'][1:]
                if damage == 'foreign_hidden_disposition':
                    ledger.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (target['artifact_id'],))
            elif damage == 'unmatched_disposition_duplicate':
                ledger.db.execute('BEGIN')
                target = {'artifact_id': ledger.artifact_add_tx('cloud_usage_disposition', '{}', project_id=1,
                    meta={'reservation_id': 'synthetic-other', 'unused': 1})}
                raw = '{"reservation_id":"synthetic-other","unused":0,"unused":1}'
            else:
                kind = 'cloud_usage_reserve' if damage.startswith('reserve_') else 'cloud_usage_result'
                target = ledger.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=?', (kind,)).fetchone()
                raw = target['meta']
                if damage == 'reserve_duplicate':
                    raw = '{"max_tokens":0,' + raw[1:]
                elif damage == 'result_http_duplicate':
                    raw = '{"http_class":"transport",' + raw[1:]
                elif damage == 'result_usage_duplicate':
                    document = json.loads(raw)
                    usage = json.dumps(document['usage'])
                    raw = json.dumps(document).replace(usage, '{"input_tokens":999,' + usage[1:], 1)
                else:
                    raw = raw[:-1] + ',"unused":{"value":' + ('NaN' if damage == 'result_nan' else '1e999') + '}}'
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (raw, target['artifact_id']))
        with pytest.raises(ValueError, match='cloud_receipt_invalid'):
            c.reservation_evidence(ledger.db, aid)


def test_unknown_scope_corruption_holds_new_evidence_without_changing_existing_billing(tmp_path, now):
    with closing(Ledger(str(tmp_path / 'l.db'))) as ledger:
        reservation = _reserve(ledger)
        c.record_result(ledger, reservation, _result(reservation))
        aid = ledger.artifacts('cloud_usage_reserve')[0]['artifact_id']
        expected = c.reservation_evidence(ledger.db, aid)
        billing = c.usage_report(ledger)
        assert expected['reservation'] == reservation
        unrelated = ledger.artifact_add('cloud_usage_disposition', '{}', project_id=2)
        raw = '{"reservation_id":"synthetic-other","unused":0,"unused":1}'
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',
                             (raw, unrelated))
        assert c._loads(raw) == json.loads(raw)
        assert c.usage_report(ledger) == billing
        with pytest.raises(ValueError, match='cloud_receipt_invalid'):
            c.reservation_evidence(ledger.db, aid)


def test_original_receipt_evidence_permits_normal_bound_receipts_of_another_patient(tmp_path, now):
    with closing(Ledger(str(tmp_path / 'l.db'))) as ledger:
        reservation = _reserve(ledger)
        c.record_result(ledger, reservation, _result(reservation))
        aid = ledger.artifacts('cloud_usage_reserve')[0]['artifact_id']
        expected = c.reservation_evidence(ledger.db, aid)
        other = c.usage_reserver(ledger, _cfg(project_ids=[1, 2]), route='patient_context', project_id=2,
            unit_key='synthetic-other')({'model': MODEL, 'max_tokens': 100}, 2)
        c.record_result(ledger, other, _result(other))
        ledger.db.execute('BEGIN')
        with ledger.db:
            c.record_disposition_tx(ledger, other, 'discarded_scope')
        assert c.usage_report(ledger)['daily_requests'] == 2
        assert c.reservation_evidence(ledger.db, aid) == expected


@pytest.mark.parametrize('damage', ['syntax', 'duplicate', 'nonfinite', 'nan', 'oversize', 'null', 'nonobject'])
def test_genuine_disposition_with_lost_scope_and_corrupt_json_never_becomes_success(tmp_path, now, damage):
    with closing(Ledger(str(tmp_path / 'l.db'))) as ledger:
        reservation = _reserve(ledger)
        c.record_result(ledger, reservation, _result(reservation))
        aid = ledger.artifacts('cloud_usage_reserve')[0]['artifact_id']
        assert c.reservation_evidence(ledger.db, aid)['reservation'] == reservation
        billing = c.usage_report(ledger)
        ledger.db.execute('BEGIN')
        with ledger.db:
            discarded = c.record_disposition_tx(ledger, reservation, 'discarded_scope')
        raw = {'syntax': '{"broken":',
               'duplicate': '{"reservation_id":"synthetic-other","reservation_id":' + json.dumps(reservation['reservation_id']) + '}',
               'nonfinite': '{"unused":1e999}', 'nan': '{"unused":NaN}',
               'oversize': '{"unused":' + json.dumps('x' * 32768) + '}',
               'null': None, 'nonobject': '[]'}[damage]
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET project_id=2,meta=? WHERE artifact_id=?', (raw, discarded))
        assert c.usage_report(ledger) == billing
        with pytest.raises(ValueError, match='cloud_receipt_invalid'):
            c.reservation_evidence(ledger.db, aid)



def _slot(db, created_at, content):
    db.execute("INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES(?,?,?,?,?)",
               ("patient_context_source_quality_repair_candidate_v1", 1, content, "{}", created_at))


def test_evidence_hold_counts_are_bounded_observations_without_ids(tmp_path, now):
    with closing(Ledger(str(tmp_path / 'l.db'))) as ledger:
        reservation = _reserve(ledger)
        c.record_result(ledger, reservation, _result(reservation))
        assert c.evidence_hold_counts(ledger.db, now=now) == {
            "unattributable_accounting_rows": 0, "old_reserved_repair_slots": 0, "unparseable_repair_slots": 0,
            "reserved_age_s": c.RESERVED_AGE_S, "scan_limit": c.EVIDENCE_SCAN_LIMIT}
        old, young = now - c.RESERVED_AGE_S - 1, now - c.RESERVED_AGE_S + 1
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta='{\"a\":1,\"a\":2}' WHERE kind='cloud_usage_result'")
            _slot(ledger.db, old, json.dumps({"phase": "reserved"}))     # counted: old and reserved
            _slot(ledger.db, young, json.dumps({"phase": "reserved"}))   # may still be in flight
            _slot(ledger.db, old, json.dumps({"phase": "complete"}))
            _slot(ledger.db, old, '{"phase":"complete","phase":"reserved"}')  # duplicate key: unreadable
            _slot(ledger.db, old, "{broken")
            _slot(ledger.db, "yesterday", json.dumps({"phase": "reserved"}))  # unreadable creation time
            _slot(ledger.db, float("-inf"), json.dumps({"phase": "reserved"}))  # non-finite creation time
            _slot(ledger.db, old, '"' + "x" * 40000 + '"')                    # oversized, never parsed
        counts = c.evidence_hold_counts(ledger.db, now=now)
        assert (counts["unattributable_accounting_rows"], counts["old_reserved_repair_slots"],
                counts["unparseable_repair_slots"]) == (1, 1, 5)
        # The global structure check is the one that holds every repair adoption.
        with pytest.raises(ValueError, match='cloud_receipt_invalid'):
            c.reservation_evidence(ledger.db, ledger.artifacts('cloud_usage_reserve')[0]['artifact_id'])
        # Above the scan bound nothing is guessed: the category is unknown.
        statements = []
        ledger.db.set_trace_callback(statements.append)
        try:
            bounded = c.evidence_hold_counts(ledger.db, now=now, scan_limit=1)
        finally:
            ledger.db.set_trace_callback(None)
        # Over the bound only the two LIMIT-ed presence counts run; no row is scanned beyond it.
        assert len(statements) == 2 and all("LIMIT 2)" in sql for sql in statements)
        assert bounded["unattributable_accounting_rows"] is None and bounded["old_reserved_repair_slots"] is None
        assert bounded["unparseable_repair_slots"] is None and bounded["scan_limit"] == 1


def test_dryrun_records_selected_provider_model_and_version(tmp_path):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        with ledger.db:
            ledger.db.execute("BEGIN")
            c.dryrun_tx(ledger, route="reextract", project_id=1, unit_key="first", input_chars=10)
            c.dryrun_tx(ledger, route="reextract", project_id=1, unit_key="second", input_chars=20,
                        model=c.openai_client.MODEL)
        rows = ledger.artifacts("cloud_usage_dryrun")
        assert [(row["model"], json.loads(row["meta"])["price_version"]) for row in rows] == [
            (c.MODEL, c.PRICE_VERSION), (c.openai_client.MODEL, c.OPENAI_PRICE_VERSION)]
        assert all(row["project_id"] is None for row in rows)
        with pytest.raises(c.AnthropicError):
            with ledger.db:
                ledger.db.execute("BEGIN")
                c.dryrun_tx(ledger, route="reextract", project_id=1, unit_key="bad", model="unknown")


def test_explicit_sol_model_uses_supported_effort_without_pinning_profiles(monkeypatch):
    import openai_client

    monkeypatch.setattr(c, 'provider_key', lambda *args, **kwargs: 'synthetic')
    cfg = {'cloud_llm': {'provider': 'openai', 'mode': 'off', 'model': openai_client.SOL_MODEL}}
    choice, errors = c.resolve_profile(cfg, route='reextract', purpose='backlog')
    assert errors == []
    assert (choice['model'], choice['effort'], choice['profile'], choice['max_tokens']) == (
        openai_client.SOL_MODEL, 'medium', 'sol-medium', 8192)
