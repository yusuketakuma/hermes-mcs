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
