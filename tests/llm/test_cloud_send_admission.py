"""Synthetic checks for the shared cloud-send refusal types and cap comparison."""
import cloud_inference
import cloud_send_admission as admission

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
