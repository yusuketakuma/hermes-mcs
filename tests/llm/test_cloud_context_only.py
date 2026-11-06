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
from semantic_testkit import _cfg, _ledger


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
    cfg = _cfg()
    cfg["cloud_llm"] = {"mode": "on", "project_ids": [1], "routes": ["reextract"]}
    monkeypatch.setattr(extract_llm_selection, "load_config", lambda *a: cfg)
    result = extract_llm._select_pending(led, 64, None, None, False,
                                       cloud_only=True, prioritize_patients=True)
    assert result == ([], 0)


def test_semantic_cloud_due_gate_rejects_closed_route(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    cfg = _cfg()
    cfg["cloud_llm"] = {"mode": "on", "project_ids": [1],
                        "routes": ["semantic_backlog"]}
    monkeypatch.setattr(semantic_due_selection, "load_config", lambda *a: cfg)
    result = semantic_due_selection._select_cloud_due(led, "backlog", ("semantic",), False, 1)
    assert result == ([], [], [], [], {"turn": None})


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
