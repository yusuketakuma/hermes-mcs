"""Cloud is patient-context-only while legacy configuration remains readable."""
import hashlib
import time
from contextlib import closing

import pytest

import cloud_cli
import cloud_inference
import cloud_policy
import extract_llm
import extract_llm_selection
import semantic_due_selection
from anthropic_client import AnthropicError, MODEL
from ledger import Ledger
from semantic_cloud_testkit import cloud as cloud
from semantic_testkit import _cfg, _ledger, _message, _patient


@pytest.mark.parametrize("route", ["reextract", "semantic_backlog"])
def test_individual_message_routes_are_ineligible_even_when_configured(route):
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1],
                          "routes": [route, "patient_context"]}}
    assert not cloud_policy.eligible(cfg, route, 1)


@pytest.mark.parametrize("projects,excluded,allowed", [
    (None, [], True), ([1], [], True), ([2], [], False), ([1], [1], False),
])
def test_patient_context_keeps_project_scope_and_exclusions(projects, excluded, allowed):
    cfg = {"cloud_llm": {"mode": "on", "project_ids": projects,
                          "exclude_project_ids": excluded}}
    assert cloud_policy.eligible(cfg, "patient_context", 1) is allowed


@pytest.mark.parametrize("route", ["reextract", "semantic_backlog"])
def test_legacy_history_scope_does_not_authorize_a_new_request(route):
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
    assert cloud_policy.in_scope(cfg, route, 1)
    assert not cloud_policy.eligible(cfg, route, 1)
    assert not cloud_policy.in_scope(cfg, route, 2)


def test_send_route_permits_only_patient_context():
    assert cloud_policy.send_route("patient_context")
    for route in ("reextract", "semantic_backlog", "other"):
        assert not cloud_policy.send_route(route)


@pytest.mark.parametrize("route", ["reextract", "semantic_backlog"])
def test_reservation_and_send_check_deny_closed_routes(tmp_path, route):
    """The accounting gate itself refuses new sends on closed individual routes."""
    body = {"model": MODEL, "max_tokens": 100, "system": "synthetic system",
            "messages": [{"role": "user", "content": hashlib.sha256(b"u").hexdigest()}]}
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "routes": [route, "patient_context"]}}
    with closing(Ledger(str(tmp_path / "l.db"))) as ledger:
        reserve = cloud_policy.usage_reserver(ledger, cfg, route=route, project_id=1,
                                              unit_key="u")
        with pytest.raises(AnthropicError) as caught:
            reserve(body, 2)
        assert caught.value.detail == "scope"
        assert cloud_policy.request_send_block_reason(
            ledger, cfg, body, route=route, project_id=1, unit_key="u") == "scope"
        assert not ledger.artifacts("cloud_usage_reserve")


def test_extract_cloud_selection_gate_rejects_closed_route(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    _patient(led)
    led.save_messages([_message()])
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        led.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        cloud_policy.capture_history_tx(led.db, 'synthetic-gate', time.time())
    cfg = _cfg()
    cfg["cloud_llm"] = {"mode": "on", "project_ids": [1], "routes": ["reextract"]}
    monkeypatch.setattr(extract_llm_selection, "load_config", lambda *a: cfg)
    monkeypatch.setattr(cloud_policy, "api_key", lambda *a: "synthetic-key")
    result = extract_llm._select_pending(led, 64, None, None, False,
                                       cloud_only=True, prioritize_patients=True)
    assert result == ([], 0)
    # Positive control: the same pending target is selected once the route is
    # simulated open, so the empty result above comes from the send gate.
    monkeypatch.setattr(cloud_policy, "send_route", lambda route: True)
    rows, _new = extract_llm._select_pending(led, 64, None, None, False,
                                             cloud_only=True, prioritize_patients=True)
    assert rows


def test_semantic_cloud_due_gate_rejects_closed_route(cloud, monkeypatch):
    led, cfg, _job, _calls = cloud
    cfg["cloud_llm"]["routes"] = ["semantic_backlog"]
    result = semantic_due_selection._select_cloud_due(led, "backlog", ("semantic",), False, 1)
    assert result == ([], [], [], [], {"turn": None})
    # Positive control: the same due job is selected once the route is open.
    monkeypatch.setattr(cloud_policy, "send_route", lambda route: True)
    selected = semantic_due_selection._select_cloud_due(led, "backlog", ("semantic",), False, 1)
    assert selected[0]


def test_route_counts_skip_closed_individual_routes(cloud, monkeypatch):
    led, cfg, _job, _calls = cloud
    settings = cloud_inference.resolve_cloud_settings(cfg)
    deadline = time.monotonic() + 60
    monkeypatch.setattr(cloud_policy, "send_route",
                        lambda route: route == "patient_context")
    out = cloud_cli._route_counts(led.db, cfg, settings, deadline)
    assert out["reextract"]["targets"] == 0
    assert out["semantic_backlog"]["targets"] == 0
    # The same pending job is counted once the routes are simulated open, so the
    # zero above comes from the caller gate rather than an empty fixture.
    monkeypatch.setattr(cloud_policy, "send_route", lambda route: True)
    out = cloud_cli._route_counts(led.db, cfg, settings, deadline)
    assert out["semantic_backlog"]["targets"] >= 1


# ---- owner-approved bounded reopening of the stored-message backlog ----

def _reopened(until="2999-01-01T00:00:00+09:00", **cloud):
    return {"cloud_llm": {"mode": "on", "project_ids": [1], "window_start": "2026-11-01T00:00:00+09:00",
                          "window_budget_usd": 60, "reextract_backlog_until": until, **cloud}}


def test_the_stored_message_backlog_reopens_only_inside_its_bounded_window(monkeypatch):
    cfg = _reopened()
    assert cloud_policy.cloud_config(cfg)[1] == []
    assert cloud_policy.reextract_planned(cfg)
    assert not cloud_policy.eligible(cfg, "reextract", 1)             # a resident or tick process never sends
    monkeypatch.setenv(cloud_policy.BACKLOG_WORKER_ENV, "1")          # the backlog worker process
    assert cloud_policy.eligible(cfg, "reextract", 1)
    assert not cloud_policy.eligible(cfg, "reextract", 2)             # project scope still applies
    assert not cloud_policy.eligible(cfg, "semantic_backlog", 1)      # never part of the reopening
    assert not cloud_policy.send_route("reextract")                   # callers without settings stay closed
    assert cloud_policy.reextract_backlog_window(cfg, now=cloud_policy.until_epoch("2998-12-31T23:59:59+09:00"))
    assert not cloud_policy.reextract_backlog_window(cfg, now=cloud_policy.until_epoch("2999-01-01T00:00:00+09:00"))
    assert not cloud_policy.eligible(_reopened(until="2026-11-02T00:00:00+09:00"), "reextract", 1)


@pytest.mark.parametrize("block", [
    {"mode": "on", "project_ids": [1], "reextract_backlog_until": "2999-01-01T00:00:00+09:00"},   # no spending cap
    {"mode": "on", "project_ids": [1], "window_start": "2026-11-01T00:00:00+09:00",
     "window_budget_usd": 60, "reextract_backlog_until": "next week"},
])
def test_a_reopening_without_a_period_budget_or_a_valid_end_keeps_every_send_off(block):
    settings, errors = cloud_policy.cloud_config({"cloud_llm": block})
    assert errors == ["config: cloud_llm_reextract_backlog_invalid"]
    assert settings["mode"] == "off" and settings["reextract_backlog_until"] is None
    assert not cloud_policy.eligible({"cloud_llm": block}, "reextract", 1)


def test_the_reopened_backlog_reserves_within_the_period_budget_and_stops_at_it(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_policy, "api_key", lambda *a: "synthetic-key")
    body = {"model": MODEL, "max_tokens": 100, "system": "synthetic system",
            "messages": [{"role": "user", "content": hashlib.sha256(b"u").hexdigest()}]}
    with closing(Ledger(str(tmp_path / "resident.db"))) as ledger:    # outside the backlog worker: no send
        with pytest.raises(AnthropicError) as caught:
            cloud_policy.usage_reserver(ledger, _reopened(provider="anthropic"), route="reextract",
                                        project_id=1, unit_key="u")(body, 2)
        assert caught.value.detail == "scope" and not ledger.artifacts("cloud_usage_reserve")
    monkeypatch.setenv(cloud_policy.BACKLOG_WORKER_ENV, "1")
    with closing(Ledger(str(tmp_path / "open.db"))) as ledger:
        open_cfg = _reopened(provider="anthropic")
        assert cloud_policy.request_send_block_reason(
            ledger, open_cfg, body, route="reextract", project_id=1, unit_key="u") is None
        cloud_policy.usage_reserver(ledger, open_cfg, route="reextract", project_id=1, unit_key="u")(body, 2)
        assert len(ledger.artifacts("cloud_usage_reserve")) == 1
    for name, cfg, detail in (
            ("spent", _reopened(provider="anthropic", window_budget_usd=0.000001), "window_cap"),
            ("ended", _reopened(provider="anthropic", until="2026-11-02T00:00:00+09:00"), "scope")):
        with closing(Ledger(str(tmp_path / (name + ".db")))) as ledger:
            with pytest.raises(AnthropicError) as caught:
                cloud_policy.usage_reserver(ledger, cfg, route="reextract", project_id=1, unit_key="u")(body, 2)
            assert caught.value.detail == detail
            assert not ledger.artifacts("cloud_usage_reserve")


def test_attachment_pages_stay_closed_while_the_backlog_is_reopened(monkeypatch):
    import attachment_extraction
    monkeypatch.setenv(cloud_policy.BACKLOG_WORKER_ENV, "1")
    monkeypatch.setattr(cloud_policy, "api_key", lambda *a: "synthetic-key")
    assert attachment_extraction.requirement(_reopened(provider="anthropic"), 1) == (False, None)


def test_the_reopening_end_is_an_operating_limit_not_a_new_generation():
    import semantic_runtime
    assert (semantic_runtime.config_generation(_reopened())
            == semantic_runtime.config_generation(_reopened(until="2998-01-01T00:00:00+09:00")))
    assert (cloud_policy.scope_generation(_reopened())
            == cloud_policy.scope_generation(_reopened(until="2998-01-01T00:00:00+09:00")))


def test_extract_cloud_selection_follows_the_reopened_backlog(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    _patient(led)
    led.save_messages([_message()])
    with led.db:
        led.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        led.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        cloud_policy.capture_history_tx(led.db, 'synthetic-gate', time.time())
    cfg = _cfg()
    cfg["cloud_llm"] = {**_reopened()["cloud_llm"], "routes": ["reextract"]}
    monkeypatch.setattr(extract_llm_selection, "load_config", lambda *a: cfg)
    monkeypatch.setattr(cloud_policy, "api_key", lambda *a: "synthetic-key")
    assert extract_llm._select_pending(led, 64, None, None, False,
                                       cloud_only=True, prioritize_patients=True) == ([], 0)
    monkeypatch.setenv(cloud_policy.BACKLOG_WORKER_ENV, "1")
    rows, _new = extract_llm._select_pending(led, 64, None, None, False,
                                             cloud_only=True, prioritize_patients=True)
    assert rows
    cfg["cloud_llm"]["reextract_backlog_until"] = "2026-11-02T00:00:00+09:00"
    assert extract_llm._select_pending(led, 64, None, None, False,
                                       cloud_only=True, prioritize_patients=True) == ([], 0)
