"""Synthetic checks for the shared cloud-send refusal types, stop-reason classifier and cap comparison."""
import ast
import inspect

import pytest

import cloud_inference
import cloud_policy
import cloud_send_admission as admission
import mcs_util
import semantic_runtime

SETTINGS = {"daily_request_budget": 10, "daily_cost_limit_usd": 5.0, "monthly_cost_limit_usd": 50.0,
            "window_budget_usd": None, "window_start": None, "provider": "anthropic",
            "provider_budgets": {"anthropic": {"daily_cost_limit_usd": 2.0}}}


def test_refusal_types_are_the_ones_callers_already_catch():
    assert cloud_inference.CloudFallback is admission.CloudFallback
    assert issubclass(cloud_inference.CloudBusy, cloud_inference.CloudFallback)
    assert issubclass(cloud_inference.CloudObservationIncomplete, cloud_inference.CloudFallback)


def test_caps_cover_period_and_provider_limits():
    caps = admission.caps(SETTINGS)
    assert caps["provider_daily_cost_limit_usd"] == 2.0
    assert caps["provider_monthly_cost_limit_usd"] == 0      # a configured provider table without it caps at 0
    assert caps["window_start"] is None


def test_only_a_set_or_lowered_cap_stops_an_unsent_request():
    reserved = admission.caps(SETTINGS)
    assert not admission.tightened(reserved, admission.caps(SETTINGS))
    assert not admission.tightened(reserved, admission.caps({**SETTINGS, "daily_cost_limit_usd": 9.0}))
    assert admission.tightened(reserved, admission.caps({**SETTINGS, "daily_cost_limit_usd": 1.0}))
    assert admission.tightened(reserved, admission.caps({**SETTINGS, "window_budget_usd": 3.0}))
    assert not admission.tightened(admission.caps({**SETTINGS, "window_budget_usd": 3.0}), reserved)


# --- refusal classifier -------------------------------------------------------------------

VERDICTS = {"defer", "local", "hold"}


def _string_returns(function):
    """Every literal string a function can return directly: a constant, either side of
    ``x or "code"``, either branch of a conditional, or a tuple's second element."""
    def literals(node):
        if isinstance(node, ast.Constant) and type(node.value) is str:
            return {node.value}
        if isinstance(node, ast.BoolOp):
            return set().union(*(literals(value) for value in node.values))
        if isinstance(node, ast.IfExp):
            return literals(node.body) | literals(node.orelse)
        if isinstance(node, ast.Tuple) and len(node.elts) == 2:
            return literals(node.elts[1])
        return set()
    tree = ast.parse(inspect.getsource(function).lstrip())
    return set().union(*(literals(node.value) for node in ast.walk(tree)
                         if isinstance(node, ast.Return) and node.value is not None))


def _block_codes():
    codes = set()
    for function in (cloud_policy.block_reason, cloud_policy.global_block_reason,
                     cloud_policy.setting_block_reason, cloud_policy.budget_block_reason):
        codes |= _string_returns(function)
    # every failure class a cloud circuit may hold (global_block_reason returns it), or circuit_open
    codes |= set(semantic_runtime._CIRCUIT_FAILURE_CLASSES) | set(semantic_runtime._CLOUD_CIRCUIT_CLASSES)
    return codes


def _key_codes():
    return _string_returns(mcs_util._private_env_lookup)


def test_classifier_covers_every_code_the_block_functions_return():
    codes = _block_codes() | _key_codes()
    # the source walk found the families it must cover
    assert {"auth_error", "payment_required", "contract_error"} <= codes
    assert {"scope", "paused", "circuit_open", "expired", "not_started", "daily_cap", "window_cap",
            "provider_budget_missing", "window_history_unverified", "http_5xx"} <= codes
    assert {"env_file_missing", "env_file_unreadable", "key_missing", "key_whitespace_or_control"} <= codes
    for code in codes:
        assert code in admission.PRE_SEND_VERDICTS, code
        assert code in admission.REFUSAL_CODES, code
        for progress in (True, False):
            assert admission.classify_refusal("pre_send", code, progress=progress) in VERDICTS
    # the refined codes a caller reports, and the history refusals, are declared too
    for code in ("price_expired", "window_history_aged", "window_history_pruned", "mode_off",
                 "route_closed", "provider_not_approved", "cloud_capacity", "deadline",
                 "owner_transaction_active", "checkpoint_unverified", "profile_invalid",
                 "cloud_worker_unavailable", "local_pinned", "transfer_review_unreadable"):
        assert code in admission.PRE_SEND_VERDICTS and code in admission.REFUSAL_CODES, code
    history = set(cloud_policy.HISTORY_REFUSALS) | set(cloud_inference._HISTORY_ERROR_REASONS)
    assert "cloud_result_superseded" in history
    for code in history:
        assert code in admission.HISTORY_CODES and code in admission.REFUSAL_CODES, code
        assert admission.classify_refusal("history", code, progress=True) == "local"
        assert admission.classify_refusal("history", code, progress=False) == "local"
    assert "unknown" in admission.REFUSAL_CODES
    assert all(type(code) is str and code for code in admission.REFUSAL_CODES)


# The corrected decision table (review of plan v2, owner decisions of 2026-11-16).
TABLE = {
    "defer/defer": ("cloud_capacity", "env_file_unreadable", "daily_cap", "daily_cost_cap",
                    "provider_daily_cap", "circuit_open", "rate_limited", "http_429", "transport",
                    "timeout", "http_5xx", "http_529", "http_402", "contract_error"),
    "local/local": ("expired", "monthly_cap", "provider_monthly_cap", "paused", "mode_off", "scope",
                    "route_closed", "provider_not_approved", "window_cap", "price_expired",
                    "window_history_aged", "local_pinned"),
    "defer/hold": ("auth_error", "payment_required", "not_started", "deadline",
                   "owner_transaction_active", "checkpoint_unverified"),
    "defer/local": ("cloud_worker_unavailable",),
    "hold/hold": ("pricing_unverified", "profile_invalid", "provider_budget_missing",
                  "window_history_pruned", "window_history_unverified", "transfer_review_unreadable",
                  "env_file_missing",
                  "env_file_not_regular", "env_file_owner", "env_file_mode_not_0600", "env_file_too_large",
                  "env_file_not_utf8", "key_missing", "key_export_prefix", "key_duplicate",
                  "key_quote_invalid", "key_empty", "key_whitespace_or_control"),
}


def test_pre_send_table_is_the_owner_table():
    declared = {}
    for verdicts, codes in TABLE.items():
        progress, fresh = verdicts.split("/")
        for code in codes:
            assert code not in declared, code
            declared[code] = (progress, fresh)
            assert admission.classify_refusal("pre_send", code, progress=True) == progress, code
            assert admission.classify_refusal("pre_send", code, progress=False) == fresh, code
    assert declared == dict(admission.PRE_SEND_VERDICTS)


def test_fresh_units_defer_only_on_transient_codes():
    assert admission.FRESH_DEFER == frozenset(TABLE["defer/defer"])
    for code in admission.PRE_SEND_VERDICTS:
        fresh = admission.classify_refusal("pre_send", code, progress=False)
        assert (fresh == "defer") == (code in admission.FRESH_DEFER), code


@pytest.mark.parametrize("code", ["http_4xx", "http_5xx", "http_529", "transport", "timeout",
                                  "response_too_large", "usage_unknown", "protocol_error", "contract_error"])
def test_after_send_failures_go_local(code):
    for progress in (True, False):
        assert admission.classify_refusal("after_send", code, progress=progress) == "local"


@pytest.mark.parametrize("code", ["http_429", "rate_limited"])
def test_after_send_rate_limit_defers_only_with_progress_from_before_the_attempt(code):
    assert admission.classify_refusal("after_send", code, progress=True) == "defer"
    assert admission.classify_refusal("after_send", code, progress=False) == "local"


def test_after_send_table_is_closed():
    assert admission.AFTER_SEND_LOCAL | admission.AFTER_SEND_PROGRESS_DEFER <= admission.REFUSAL_CODES
    for code in ("auth_error", "daily_cap", "paused", "repeat_success", "cloud_capacity"):
        assert admission.classify_refusal("after_send", code, progress=True) == "hold"


@pytest.mark.parametrize("stage", ["pre_send", "after_send", "history"])
@pytest.mark.parametrize("code", [None, "unknown", "synthetic_unlisted", "", 7])
def test_unknown_code_is_hold(stage, code):
    for progress in (True, False):
        assert admission.classify_refusal(stage, code, progress=progress) == "hold"


@pytest.mark.parametrize("stage,progress", [("before", True), (None, True), ("pre_send", 1),
                                            ("pre_send", None), ("history", "yes")])
def test_invalid_stage_or_progress_is_refused(stage, progress):
    with pytest.raises(ValueError, match="^cloud_refusal_stage_invalid$"):
        admission.classify_refusal(stage, "daily_cap", progress=progress)
