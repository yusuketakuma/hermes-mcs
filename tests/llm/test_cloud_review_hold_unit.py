"""Synthetic cloud units under the facility review: nothing is posted to an unapproved provider."""
import time

import pytest

import cloud_inference
import cloud_policy
import external_transfer
from test_anthropic_runtime import routed as routed
from test_cloud_response_recovery import legacy_cloud_routes

_REFUSED = (cloud_inference.CloudFallback, cloud_inference.CloudObservationIncomplete)


@pytest.fixture
def reviewed(routed, tmp_path, monkeypatch):
    led, cfg, calls, reserved, _results = routed
    cfg["cloud_llm"]["provider"] = "anthropic"      # explicit mode and provider
    monkeypatch.setattr(cloud_inference, "HOME", str(tmp_path))
    external_transfer.require(tmp_path)
    external_transfer.acknowledge(by="管理者", reason="契約確認", services=["anthropic"],
                                  confirm_human=True, root=tmp_path)
    return led, cfg, calls, reserved, tmp_path


def _withdraw(root):
    external_transfer.acknowledge(by="管理者", reason="取消", services=["openai"],
                                  confirm_human=True, root=root)


def _unit(led, cfg):
    return cloud_inference.CloudInferenceUnit(led, cfg, route="patient_context", project_id=1,
        unit_key="synthetic", deadline=time.monotonic() + 120,
        current=lambda: True, dispatch=lambda fn, **_kwargs: fn())


def _ask(unit):
    with legacy_cloud_routes():
        return unit("synthetic prompt", deadline=time.monotonic() + 120, max_tokens=1600)


def test_an_approved_unit_posts(reviewed):
    led, cfg, calls, _reserved, _root = reviewed
    assert _ask(_unit(led, cfg))["status"] == 200 and len(calls) == 1


def test_the_guard_refuses_an_explicit_mode_the_review_no_longer_covers(reviewed):
    led, cfg, calls, reserved, root = reviewed
    unit = _unit(led, cfg)
    _withdraw(root)
    assert cloud_policy.resolve_settings(cfg, root)[0]["mode"] == "off"
    with pytest.raises(_REFUSED):
        unit.guard()
    with pytest.raises(_REFUSED):
        _ask(unit)
    assert calls == [] and reserved == []


def test_a_withdrawal_after_the_reservation_stops_the_post(reviewed, monkeypatch):
    led, cfg, calls, reserved, root = reviewed
    reserve_factory = cloud_policy.usage_reserver

    def withdrawing(ledger, config, **identity):
        reserve = reserve_factory(ledger, config, **identity)

        def reserve_then_withdraw(body, timeout):
            item = reserve(body, timeout)
            _withdraw(root)            # the review narrows between reservation and POST
            return item
        return reserve_then_withdraw
    monkeypatch.setattr(cloud_policy, "usage_reserver", withdrawing)
    unit = _unit(led, cfg)
    with pytest.raises(_REFUSED):
        _ask(unit)
    assert len(reserved) == 1 and calls == []


@pytest.mark.parametrize("mode", ["shadow", "dry_run"])
def test_shadow_and_dry_run_post_nothing_under_a_hold(reviewed, mode):
    led, cfg, calls, reserved, root = reviewed
    cfg["cloud_llm"]["mode"] = mode
    _withdraw(root)
    settings, _errors, held = cloud_policy.resolve_selection(cfg, root)
    assert settings["mode"] == "off" and held == "anthropic"
    assert not cloud_policy.in_scope(settings, "patient_context", 1)
    try:
        unit = _unit(led, cfg)
    except _REFUSED:
        unit = None
    if unit is not None:
        with pytest.raises(_REFUSED):
            _ask(unit)
    assert calls == [] and reserved == []
