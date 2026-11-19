"""Synthetic checks that a cloud unit names why its last call stopped, without changing what it raises."""
from contextlib import nullcontext
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

import anthropic_client
import cloud_backlog
import cloud_client
import cloud_inference
import cloud_policy as policy
import external_transfer
from semantic_cloud_testkit import cloud as cloud, rows


def _unit(ledger, cfg, *, unit_key="synthetic", current=lambda: True, **kwargs):
    deadline = time.monotonic() + 60
    return cloud_inference.CloudInferenceUnit(ledger, cfg, route="patient_context", project_id=1,
        unit_key=unit_key, deadline=deadline, current=current,
        dispatch=lambda call, **_kwargs: call(), **kwargs)


def _ask(unit, prompt="synthetic prompt"):
    return unit(prompt, deadline=time.monotonic() + 60, max_tokens=100)


def _refused(unit, error_type, message, prompt="synthetic prompt"):
    with pytest.raises(error_type) as caught:
        _ask(unit, prompt)
    assert type(caught.value) is error_type and str(caught.value) == message
    return caught.value


def _reply(status=200, usage=True, size=None, headers=None):
    def request(self, method, body, timeout, **kwargs):
        if size is not None:
            return 200, {}, b" " * size
        payload = {"model": body["model"], "stop_reason": "end_turn",
                   "content": [{"type": "text", "text": '{"facts": []}'}]}
        if usage:
            payload["usage"] = {"input_tokens": 10, "output_tokens": 10}
        return status, headers or {}, json.dumps(payload).encode()
    return request


@pytest.fixture
def priced(cloud, monkeypatch):
    monkeypatch.setattr(policy, "pricing_valid", lambda *args, **kwargs: True)
    ledger, _cfg_value, _job, calls = cloud

    def request(self, method, body, timeout, **kw):
        assert not ledger.db.in_transaction
        calls.append(body)
        return 200, {}, json.dumps({
            "model": body["model"], "stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"facts": []}'}],
            "usage": {"input_tokens": 10, "output_tokens": 10}}).encode()

    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)
    return cloud


def test_a_new_unit_has_no_refusal(priced):
    ledger, cfg, _, _ = priced
    assert _unit(ledger, cfg).refusal is None


def test_daily_cap_before_send_is_a_pre_send_refusal(priced):
    ledger, cfg, _, calls = priced
    cfg["cloud_llm"]["daily_request_budget"] = 0
    unit = _unit(ledger, cfg)
    _refused(unit, cloud_inference.CloudFallback, "cloud_send_blocked")
    assert unit.refusal == ("pre_send", "daily_cap") and not calls


def test_a_cap_reached_at_reservation_is_a_pre_send_refusal(priced, monkeypatch):
    ledger, cfg, _, calls = priced
    real = policy.budget_block_reason
    seen = []

    def budget(*args, **kwargs):
        seen.append(True)
        return None if len(seen) == 1 else "daily_cost_cap"

    monkeypatch.setattr(policy, "budget_block_reason", budget)
    unit = _unit(ledger, cfg)
    error = _refused(unit, cloud_client.CloudNotSent, "budget_exceeded")
    assert error.detail == "daily_cost_cap" and unit.refusal == ("pre_send", "daily_cost_cap")
    assert not calls and real is not budget


def test_no_capacity_is_a_pre_send_refusal(priced, monkeypatch):
    ledger, cfg, _, calls = priced
    monkeypatch.setattr(cloud_backlog, "request_slot", lambda *args, **kwargs: nullcontext(False))
    unit = _unit(ledger, cfg)
    _refused(unit, cloud_inference.CloudBusy, "cloud_capacity")
    assert unit.refusal == ("pre_send", "cloud_capacity") and not calls


def test_checkpoint_refusal_is_a_pre_send_refusal(priced):
    ledger, cfg, _, calls = priced
    unit = _unit(ledger, cfg, dispatch_current=lambda: False)
    error = _refused(unit, cloud_client.CloudNotSent, "not_sent")
    assert error.detail == "checkpoint_unverified"
    assert unit.refusal == ("pre_send", "checkpoint_unverified") and not calls


@pytest.mark.parametrize("status,code", [(503, "http_5xx"), (500, "http_5xx"), (529, "http_529"),
                                         (401, "http_4xx"), (402, "http_4xx"), (404, "http_4xx")])
def test_an_http_failure_after_sending_is_an_after_send_refusal(priced, monkeypatch, status, code):
    ledger, cfg, _, _ = priced
    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", _reply(status))
    unit = _unit(ledger, cfg)
    _refused(unit, cloud_inference.CloudFallback, "cloud_unit_failed")
    assert unit.refusal == ("after_send", code)


def test_a_rate_limit_after_its_attempts_is_an_after_send_refusal(priced, monkeypatch):
    ledger, cfg, _, _ = priced
    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", _reply(429))
    unit = _unit(ledger, cfg, max_attempts=1)
    _refused(unit, cloud_inference.CloudFallback, "cloud_unit_failed")
    assert unit.refusal == ("after_send", "http_429")


@pytest.mark.parametrize("error,code", [(OSError("synthetic"), "transport"),
                                        (TimeoutError("synthetic"), "timeout")])
def test_an_unknown_transport_outcome_is_an_after_send_refusal(priced, monkeypatch, error, code):
    ledger, cfg, _, _ = priced

    def request(self, method, body, timeout, **kwargs):
        raise error

    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)
    unit = _unit(ledger, cfg)
    _refused(unit, cloud_inference.CloudFallback, "cloud_unit_failed")
    assert unit.refusal == ("after_send", code)


def test_a_raw_response_past_its_bound_is_an_after_send_refusal(priced, monkeypatch):
    ledger, cfg, _, _ = priced
    unit = _unit(ledger, cfg)
    monkeypatch.setattr(anthropic_client.AnthropicClient, "request",
                        _reply(size=cloud_client.response_limit(unit.selection["max_tokens"]) + 1))
    _refused(unit, cloud_inference.CloudFallback, "cloud_unit_failed")
    assert unit.refusal == ("after_send", "response_too_large")


def test_a_200_without_usage_is_named_before_its_result_is_saved(priced, monkeypatch):
    ledger, cfg, _, _ = priced
    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", _reply(usage=False))
    unit = _unit(ledger, cfg)
    _refused(unit, cloud_inference.CloudFallback, "cloud_unit_failed")
    assert unit.refusal == ("after_send", "usage_unknown")


def test_a_settled_same_request_is_a_history_refusal(priced):
    ledger, cfg, _, calls = priced
    unit = _unit(ledger, cfg)
    assert _ask(unit)["text"] == '{"facts": []}' and unit.refusal is None
    again = _unit(ledger, cfg)
    error = _refused(again, cloud_client.CloudNotSent, "budget_exceeded")
    assert error.detail == "cloud_unit_unresolved"
    assert again.refusal == ("history", "repeat_success") and len(calls) == 1


def test_expired_prices_are_told_apart_from_an_unverified_price_table(cloud, monkeypatch):
    ledger, cfg, _, calls = cloud
    monkeypatch.setattr(policy, "pricing_valid", lambda *args, **kwargs: False)
    unit = _unit(ledger, cfg)
    _refused(unit, cloud_inference.CloudFallback, "cloud_send_blocked")
    assert unit.refusal == ("pre_send", "price_expired") and not calls


def test_a_latched_pricing_circuit_stays_unverified(priced):
    ledger, cfg, _, calls = priced
    with ledger.db:
        ledger.db.execute("BEGIN IMMEDIATE")
        from semantic_runtime import _record_cloud_circuit_result_tx
        _record_cloud_circuit_result_tx(ledger, anthropic_client.AnthropicError("pricing_unverified"))
    unit = _unit(ledger, cfg)
    _refused(unit, cloud_inference.CloudFallback, "cloud_send_blocked")
    assert unit.refusal == ("pre_send", "pricing_unverified") and not calls


def test_an_aged_period_is_told_apart_from_pruned_history(priced):
    ledger, cfg, _, calls = priced
    start = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 70 * 86400))
    cfg["cloud_llm"].update(window_start=start, window_budget_usd=50)
    unit = _unit(ledger, cfg)
    _refused(unit, cloud_inference.CloudFallback, "cloud_send_blocked")
    assert unit.refusal == ("pre_send", "window_history_aged") and not calls


def test_duplicate_prune_marks_are_pruned_history(priced):
    ledger, cfg, _, calls = priced
    start = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 3 * 86400))
    cfg["cloud_llm"].update(window_start=start, window_budget_usd=50)
    for _ in range(2):
        ledger.artifact_add(policy.PRUNE_MARK_KIND, str(time.time() - 86400))
    unit = _unit(ledger, cfg)
    _refused(unit, cloud_inference.CloudFallback, "cloud_send_blocked")
    assert unit.refusal == ("pre_send", "window_history_pruned") and not calls


def test_a_provider_held_by_the_facility_review_is_named(priced, monkeypatch):
    ledger, cfg, _, calls = priced
    unit = _unit(ledger, cfg)
    monkeypatch.setattr(external_transfer, "status", lambda root=None: {"state": "pending", "auto_enable": False})
    assert policy.resolve_selection(cfg)[2] == "anthropic"
    _refused(unit, cloud_inference.CloudFallback, "cloud_unit_changed")
    assert unit.refusal == ("pre_send", "provider_not_approved") and not calls


def test_mode_off_during_a_unit_is_named(priced):
    ledger, cfg, _, calls = priced
    unit = _unit(ledger, cfg)
    cfg["cloud_llm"]["mode"] = "off"
    _refused(unit, cloud_inference.CloudFallback, "cloud_unit_changed")
    assert unit.refusal == ("pre_send", "mode_off") and not calls


def test_refusal_is_cleared_between_calls_and_after_success(priced, monkeypatch):
    ledger, cfg, _, calls = priced
    replies = iter([(429, True), (200, True)])

    def request(self, method, body, timeout, **kwargs):
        status, usage = next(replies)
        calls.append(body)
        return _reply(status, usage)(self, method, body, timeout)

    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)
    monkeypatch.setattr(cloud_client.time, "sleep", lambda delay: None)
    unit = _unit(ledger, cfg, max_attempts=1)
    _refused(unit, cloud_inference.CloudFallback, "cloud_unit_failed", prompt="synthetic first")
    assert unit.refusal == ("after_send", "http_429")
    # a later call that stops before any attempt does not keep the earlier code
    monkeypatch.setattr(cloud_client, "time", SimpleNamespace(monotonic=lambda: 1e12, sleep=lambda delay: None))
    error = _refused(unit, cloud_client.CloudNotSent, "budget_exceeded", prompt="synthetic second")
    assert error.detail == "deadline" and unit.refusal is None and len(calls) == 1
    monkeypatch.setattr(cloud_client, "time", time)
    # a clean 200 clears it
    unit.refusal = ("after_send", "http_429")
    assert _ask(unit, "synthetic third")["text"] == '{"facts": []}'
    assert unit.refusal is None


def test_a_retry_that_succeeds_clears_the_attempt_before_it(priced, monkeypatch):
    ledger, cfg, _, _ = priced
    replies = iter([429, 200])
    monkeypatch.setattr(anthropic_client.AnthropicClient, "request",
                        lambda self, method, body, timeout, **kw: _reply(
                            next(replies), headers={"Retry-After": "1"})(self, method, body, timeout))
    monkeypatch.setattr(cloud_client.time, "sleep", lambda delay: None)
    unit = _unit(ledger, cfg, max_attempts=2)
    assert _ask(unit)["text"] == '{"facts": []}'
    assert unit.refusal is None
    assert [json.loads(row["meta"])["http_class"] for row in rows(ledger, "cloud_usage_result")] == [
        "http_429", "http_200"]


def test_a_recovered_answer_clears_the_refusal(priced):
    ledger, cfg, _, calls = priced
    binding = "a" * 64
    first = _unit(ledger, cfg, input_binding=binding)
    assert _ask(first)["text"] == '{"facts": []}'
    again = _unit(ledger, cfg, input_binding=binding)
    again.refusal = ("after_send", "http_429")
    answer = _ask(again)
    assert answer["reused_response"] and again.refusal is None and len(calls) == 1


def test_a_refused_retry_keeps_the_outcome_of_the_attempt_sent_before_it(priced, monkeypatch):
    ledger, cfg, _, calls = priced
    real = policy.budget_block_reason
    monkeypatch.setattr(policy, "budget_block_reason",
                        lambda *args, **kwargs: "daily_cap" if calls else real(*args, **kwargs))

    def request(self, method, body, timeout, **kwargs):
        calls.append(body)
        return _reply(429)(self, method, body, timeout)

    monkeypatch.setattr(anthropic_client.AnthropicClient, "request", request)
    monkeypatch.setattr(cloud_client.time, "sleep", lambda delay: None)
    unit = _unit(ledger, cfg, max_attempts=2)
    with pytest.raises(Exception):
        _ask(unit)
    assert len(calls) == 1 and unit.refusal == ("after_send", "http_429")


@pytest.mark.parametrize("change", ["current", "updater"])
def test_a_denial_for_a_reason_other_than_scope_names_nothing(priced, monkeypatch, change):
    ledger, cfg, _, calls = priced
    if change == "current":
        unit = _unit(ledger, cfg, current=lambda: False)
    else:
        unit = _unit(ledger, cfg)
        monkeypatch.setattr(cloud_inference.writer_lock, "updater_state", lambda: (False, 2))
    _refused(unit, cloud_inference.CloudFallback, "cloud_unit_changed")
    assert unit.refusal is None and not calls


@pytest.mark.parametrize("error,code", [
    (anthropic_client.AnthropicError("contract_error", "cloud_profile_invalid"), "unknown"),
    (anthropic_client.AnthropicError("contract_error", "model_not_allowed"), "unknown"),
    (anthropic_client.AnthropicError("transport", "synthetic"), "unknown"),
    (anthropic_client.AnthropicError("budget_exceeded", "synthetic_unlisted"), "unknown"),
    (anthropic_client.AnthropicError("pricing_unverified", "reservation_bound_invalid"), "pricing_unverified"),
    (anthropic_client.AnthropicError("budget_exceeded", "provider_daily_cap"), "provider_daily_cap"),
])
def test_a_reservation_error_is_named_only_by_a_declared_code(priced, monkeypatch, error, code):
    ledger, cfg, _, calls = priced

    def reserver(*args, **kwargs):
        def reserve(body, timeout):
            raise error
        return reserve

    monkeypatch.setattr(policy, "usage_reserver", reserver)
    unit = _unit(ledger, cfg)
    with pytest.raises(Exception):
        _ask(unit)
    assert unit.refusal == ("pre_send", code) and not calls


def test_an_undeclared_send_block_code_is_reported_as_unknown(priced, monkeypatch):
    ledger, cfg, _, calls = priced
    monkeypatch.setattr(policy, "request_send_block_reason", lambda *args, **kwargs: "synthetic_block")
    unit = _unit(ledger, cfg)
    _refused(unit, cloud_inference.CloudFallback, "cloud_send_blocked")
    assert unit.refusal == ("pre_send", "unknown") and not calls


@pytest.mark.parametrize("error,code", [
    (sqlite3.OperationalError("database is locked"), "unknown"),
    (TypeError("synthetic"), "unknown"),
    (anthropic_client.AnthropicError("contract_error", "cloud_profile_invalid"), "unknown"),
])
def test_a_history_read_error_without_a_classification_is_unknown(priced, monkeypatch, error, code):
    ledger, cfg, _, calls = priced

    def blocked(*args, **kwargs):
        raise error

    monkeypatch.setattr(policy, "request_send_block_reason", blocked)
    unit = _unit(ledger, cfg)
    _refused(unit, cloud_client.CloudNotSent, "budget_exceeded")
    assert unit.refusal == ("history", code) and not calls


def test_a_classified_history_refusal_keeps_its_label(priced, monkeypatch):
    ledger, cfg, _, calls = priced

    def blocked(*args, **kwargs):
        error = anthropic_client.AnthropicError("budget_exceeded", "cloud_unit_unresolved")
        error.history_refusal = "receipt_inconsistent"
        raise error

    monkeypatch.setattr(policy, "request_send_block_reason", blocked)
    unit = _unit(ledger, cfg)
    _refused(unit, cloud_client.CloudNotSent, "budget_exceeded")
    assert unit.refusal == ("history", "receipt_inconsistent") and not calls


@pytest.mark.parametrize("error,code", [
    (ValueError("cloud_result_missing"), "cloud_result_missing"),
    (ValueError("cloud_result_superseded"), "cloud_result_superseded"),
    (ValueError("synthetic_unlisted"), "unknown"),
    (sqlite3.OperationalError("database is locked"), "unknown"),
    (anthropic_client.AnthropicError("pricing_unverified", "usage_invalid"), "unknown"),
])
def test_a_recovery_read_error_is_named_only_by_a_proven_reason(priced, monkeypatch, error, code):
    ledger, cfg, _, calls = priced

    def recover(*args, **kwargs):
        raise error

    monkeypatch.setattr(policy, "recover_response", recover)
    unit = _unit(ledger, cfg, input_binding="a" * 64)
    _refused(unit, cloud_client.CloudNotSent, "budget_exceeded")
    assert unit.refusal == ("history", code) and not calls


def test_an_unreadable_facility_review_record_is_not_named_unapproved(priced, monkeypatch):
    ledger, cfg, _, calls = priced
    unit = _unit(ledger, cfg)
    monkeypatch.setattr(external_transfer, "status", lambda root=None: dict(external_transfer.UNREADABLE))
    assert policy.resolve_selection(cfg)[2] == "anthropic"
    _refused(unit, cloud_inference.CloudFallback, "cloud_unit_changed")
    assert unit.refusal == ("pre_send", "transfer_review_unreadable") and not calls


def test_a_guard_denial_at_commit_time_also_names_the_stop(priced):
    ledger, cfg, _, calls = priced
    unit = _unit(ledger, cfg)
    assert _ask(unit)["text"] == '{"facts": []}' and unit.refusal is None
    cfg["cloud_llm"]["mode"] = "off"
    with pytest.raises(cloud_inference.CloudFallback):
        unit.commit_guard()
    assert unit.refusal == ("pre_send", "mode_off")


def _scope_code(tmp_path, block, *, key=None, mode=0o600, review=None, monkeypatch):
    if key is not None:
        env = tmp_path / ".env"
        env.write_text(f"{anthropic_client.KEY_ENV}={key}\n")
        env.chmod(mode)
    state = {"state": "not_required", "auto_enable": True} if review is None else review
    monkeypatch.setattr(external_transfer, "status", lambda root=None: dict(state))
    return policy.refusal_code({"cloud_llm": block}, "scope", model=anthropic_client.MODEL, root=str(tmp_path))


def test_scope_names_mode_off_only_for_an_explicit_off(tmp_path, monkeypatch):
    explicit = {"mode": "off", "project_ids": [1], "provider": "anthropic"}
    assert _scope_code(tmp_path, explicit, monkeypatch=monkeypatch) == "mode_off"
    # mode omitted: off is computed from the key, which names the key problem instead
    omitted = {"project_ids": [1], "provider": "anthropic"}
    assert _scope_code(tmp_path, omitted, monkeypatch=monkeypatch) == "env_file_missing"
    assert _scope_code(tmp_path, omitted, key="synthetic-key", mode=0o644,
                       monkeypatch=monkeypatch) == "env_file_mode_not_0600"
    # a computed off with a usable key and no named reason is not a definitive stop
    invalid = {"project_ids": [1], "provider": "anthropic", "synthetic_unknown_field": True}
    assert _scope_code(tmp_path, invalid, key="synthetic-key", monkeypatch=monkeypatch) == "unknown"
    # an open mode outside the patient's scope stays a scope stop
    other = {"mode": "on", "project_ids": [2], "provider": "anthropic"}
    assert _scope_code(tmp_path, other, key="synthetic-key", monkeypatch=monkeypatch) == "scope"


@pytest.mark.parametrize("review,code", [
    ({"state": "pending", "auto_enable": False}, "provider_not_approved"),
    ({"state": "acknowledged", "auto_enable": True, "acknowledged": {"services": ["openai"]}},
     "provider_not_approved"),
    (dict(external_transfer.UNREADABLE), "transfer_review_unreadable"),
])
def test_a_held_provider_is_named_by_its_review_state(tmp_path, monkeypatch, review, code):
    for block in ({"mode": "on", "project_ids": [1], "provider": "anthropic"},
                  {"project_ids": [1], "provider": "anthropic"}):
        assert _scope_code(tmp_path, block, key="synthetic-key", review=review,
                           monkeypatch=monkeypatch) == code
