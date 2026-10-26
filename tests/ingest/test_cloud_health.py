"""Cloud stops appear as fixed health reasons, independent of semantic rollout."""
import json
import time

import pytest

import cloud_policy
import health_report
import health_watch
import semantic_runtime
from anthropic_client import AnthropicError
from common_testkit import ledger_fixture

db = ledger_fixture(name="db")


@pytest.mark.parametrize("kind", ["auth_error", "payment_required", "pricing_unverified"])
def test_cloud_stop_is_visible_and_human_reset_clears_it(db, monkeypatch, kind):
    monkeypatch.setattr(cloud_policy, "api_key", lambda root=None: "synthetic-key")
    monkeypatch.setattr(cloud_policy, "pricing_valid", lambda now=None: True)
    semantic_runtime.record_circuit_result(db, AnthropicError(kind), kind="cloud_circuit")
    reason = "cloud_llm_" + kind
    assert health_report.backlog_stalls(db.db, {}, time.time()) == [reason]
    assert all(health_watch._REASON_JA[reason])
    semantic_runtime.record_circuit_result(db, None, kind="cloud_circuit")
    assert health_report.cloud_stop_reason(db.db, {}) == kind
    cloud_policy.resume(db)
    assert health_report.cloud_stop_reason(db.db, {}) is None


def test_disabled_scope_and_intentional_pause_do_not_raise_alarm(db, monkeypatch):
    monkeypatch.setattr(cloud_policy, "api_key", lambda root=None: "synthetic-key")
    monkeypatch.setattr(cloud_policy, "pricing_valid", lambda now=None: True)
    semantic_runtime.record_circuit_result(db, AnthropicError("auth_error"), kind="cloud_circuit")
    for block in ({"mode": "off"}, {"mode": "dry_run", "project_ids": None},
                  {"project_ids": []}, {"routes": []}):
        assert health_report.cloud_stop_reason(db.db, {"cloud_llm": block}) is None
    cloud_policy.stop(db)
    assert health_report.cloud_stop_reason(db.db, {}) is None
    monkeypatch.setattr(cloud_policy, "api_key", lambda root=None: None)
    assert health_report.cloud_stop_reason(db.db, {}) is None


def test_repeated_failure_is_bounded_and_error_text_never_enters_reason(db, monkeypatch):
    monkeypatch.setattr(cloud_policy, "api_key", lambda root=None: "synthetic-key")
    monkeypatch.setattr(cloud_policy, "pricing_valid", lambda now=None: True)
    for _ in range(3):
        semantic_runtime.record_circuit_result(db, AnthropicError("timeout", "synthetic-private-detail", retryable=True),
                                              kind="cloud_circuit")
    assert health_report.cloud_stop_reason(db.db, {}) == "circuit_open"
    db.artifact_add("cloud_circuit", json.dumps({"failure_class": "synthetic-private-detail"}))
    assert health_report.cloud_stop_reason(db.db, {}) == "pricing_unverified"


@pytest.mark.parametrize("mode", ["on", "shadow"])
@pytest.mark.parametrize("scope", [None, [1], [1, 2]])
def test_explicit_cloud_without_key_warns_and_explains_local_continuation(db, monkeypatch, mode, scope):
    monkeypatch.setattr(cloud_policy, "api_key", lambda root=None: None)
    monkeypatch.setattr(cloud_policy, "pricing_valid", lambda now=None: True)
    cfg = {"cloud_llm": {"mode": mode, "project_ids": scope}}
    assert health_report.backlog_stalls(db.db, cfg, time.time()) == ["cloud_llm_no_api_key"]
    meaning, action = health_watch._REASON_JA["cloud_llm_no_api_key"]
    assert "ローカルLLM" in meaning and "継続" in meaning
    assert "MCS_ANTHROPIC_API_KEY" in action and ".env" in action and "0600" in action
    assert not db.artifacts("cloud_control") and not db.artifacts("cloud_circuit")


@pytest.mark.parametrize("block", [None, {}, {"project_ids": None}, {"mode": "off"},
    {"mode": "dry_run", "project_ids": None}, {"mode": "on", "project_ids": []},
    {"mode": "on", "project_ids": None, "routes": []},
    {"mode": "shadow", "project_ids": [1], "exclude_project_ids": [1]},
    {"mode": "on", "project_ids": None, "unknown": True}])
def test_keyless_auto_disabled_or_empty_cloud_scope_has_no_warning(db, monkeypatch, block):
    monkeypatch.setattr(cloud_policy, "api_key", lambda root=None: None)
    cfg = {} if block is None else {"cloud_llm": block}
    assert health_report.cloud_stop_reason(db.db, cfg) is None


@pytest.mark.parametrize("mode", ["on", "shadow"])
def test_intentional_pause_suppresses_keyless_warning_without_clearing_stop(db, monkeypatch, mode):
    monkeypatch.setattr(cloud_policy, "api_key", lambda root=None: None)
    cfg = {"cloud_llm": {"mode": mode, "project_ids": None}}
    cloud_policy.stop(db)
    before = [dict(row) for row in db.artifacts("cloud_control")]
    assert health_report.cloud_stop_reason(db.db, cfg) is None
    assert [dict(row) for row in db.artifacts("cloud_control")] == before


def test_keyless_warning_never_resets_terminal_cloud_failure(db, monkeypatch):
    monkeypatch.setattr(cloud_policy, "pricing_valid", lambda now=None: True)
    semantic_runtime.record_circuit_result(db, AnthropicError("auth_error"), kind="cloud_circuit")
    before = [dict(row) for row in db.artifacts("cloud_circuit")]
    cfg = {"cloud_llm": {"mode": "on", "project_ids": None}}
    monkeypatch.setattr(cloud_policy, "api_key", lambda root=None: None)
    assert health_report.cloud_stop_reason(db.db, cfg) == "no_api_key"
    assert [dict(row) for row in db.artifacts("cloud_circuit")] == before
    monkeypatch.setattr(cloud_policy, "api_key", lambda root=None: "synthetic-private-key")
    assert health_report.cloud_stop_reason(db.db, cfg) == "auth_error"
    assert "synthetic-private-key" not in str(health_watch._REASON_JA["cloud_llm_no_api_key"])
