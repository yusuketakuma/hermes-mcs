"""Synthetic guardrails: run windows, key-only spending caps and key diagnostics."""
from contextlib import closing
from datetime import datetime
import os

import pytest

import cloud_policy as c
import environment_diagnostics
import mcs_util
import semantic_policy
from ledger import Ledger

NOW = datetime(2026, 10, 26, 12, tzinfo=c.JST).timestamp()


@pytest.fixture
def now(monkeypatch):
    monkeypatch.setattr(c.time, "time", lambda: NOW)
    monkeypatch.setattr(semantic_policy.time, "time", lambda: NOW)
    return NOW


def test_key_only_sending_requires_a_cost_cap():
    off, errors = c.cloud_config({}, key_present=True)
    assert errors == [c.COST_LIMIT_REQUIRED] and off["mode"] == "off"
    assert c.blocking_errors(errors) == []
    for cap in ("daily_cost_limit_usd", "monthly_cost_limit_usd"):
        on, errors = c.cloud_config({"cloud_llm": {cap: 80}}, key_present=True)
        assert errors == [] and on["mode"] == "on" and on["project_ids"] is None
    # An explicit mode is the operator's own choice and keeps its meaning.
    explicit, errors = c.cloud_config({"cloud_llm": {"mode": "on", "project_ids": None}}, key_present=True)
    assert errors == [] and explicit["mode"] == "on"
    # Without a key nothing is auto-enabled, so nothing is required.
    assert c.cloud_config({}, key_present=False) == (c.cloud_config({})[0], [])


@pytest.mark.parametrize("value", ["2026-10-28T23:59:59", "tomorrow", 1793200000, "x" * 65])
def test_until_needs_an_offset_aware_iso_time(value):
    settings, errors = c.cloud_config({"cloud_llm": {"mode": "on", "project_ids": None, "until": value}})
    assert errors == ["config: cloud_llm_until_invalid"] and settings["mode"] == "off"
    sem, errors = semantic_policy.semantic_config({"semantic": {"until": value}})
    assert "config: semantic_until_invalid" in errors and sem["mode"] == "off"


def test_cloud_until_stops_new_reservations_after_the_window(tmp_path, now):
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "daily_cost_limit_usd": 80,
                         "monthly_cost_limit_usd": 80, "until": "2026-10-26T12:00:01+09:00"}}
    with closing(Ledger(str(tmp_path / "l.db"))) as ledger:
        assert c.budget_block_reason(ledger, cfg) is None
        settings = c.cloud_config(cfg)[0]
        assert c.setting_block_reason(settings, now=NOW + 1) == "expired"
        cfg["cloud_llm"]["until"] = "2026-10-26T03:00:00+00:00"   # the same instant in UTC
        assert c.budget_block_reason(ledger, cfg) == "expired"
        with pytest.raises(c.AnthropicError) as raised:
            c.usage_reserver(ledger, cfg, route="reextract", project_id=1, unit_key="u")(
                {"model": c.MODEL, "max_tokens": 100}, 2)
        assert (raised.value.kind, raised.value.detail) == ("budget_exceeded", "expired")
        assert ledger.artifacts("cloud_usage_reserve") == []


def test_semantic_until_turns_every_jev_feature_off_after_the_window(now):
    block = {"mode": "assist", "project_ids": None, "daily_request_budget": 100,
             "summary_mode": "assist", "loop_mode": "assist", "extract_qc": "annotate"}
    live, errors = semantic_policy.semantic_config({"semantic": {**block, "until": "2026-10-26T12:00:01+09:00"}})
    assert errors == [] and live["mode"] == "assist" and live["extract_qc"] == "annotate"
    ended, errors = semantic_policy.semantic_config({"semantic": {**block, "until": "2026-10-26T12:00:00+09:00"}})
    assert errors == []
    assert (ended["mode"], ended["summary_mode"], ended["loop_mode"], ended["extract_qc"]) == ("off",) * 4
    # A run window never changes interpretation identities or receipts.
    assert semantic_policy.policy_fingerprint(live) == semantic_policy.policy_fingerprint(ended)


def _env(root, text, mode=0o600):
    path = root / ".env"
    path.write_text(text)
    os.chmod(path, mode)


@pytest.mark.parametrize("text, mode, code", [
    ("MCS_ANTHROPIC_API_KEY=sk-synthetic\n", 0o600, None),
    ("MCS_ANTHROPIC_API_KEY=sk-synthetic\n", 0o644, "env_file_mode_not_0600"),
    ("export MCS_ANTHROPIC_API_KEY=sk-synthetic\n", 0o600, "key_export_prefix"),
    ("MCS_ANTHROPIC_API_KEY=a\nMCS_ANTHROPIC_API_KEY=b\n", 0o600, "key_duplicate"),
    ("MCS_ANTHROPIC_API_KEY=sk-synthetic \n", 0o600, "key_whitespace_or_control"),
    ("MCS_ANTHROPIC_API_KEY=\n", 0o600, "key_empty"),
    ("OTHER=1\n", 0o600, "key_missing"),
])
def test_private_env_problem_names_the_rejection_without_the_value(tmp_path, text, mode, code):
    _env(tmp_path, text, mode)
    assert mcs_util.private_env_problem("MCS_ANTHROPIC_API_KEY", tmp_path) == code
    assert (mcs_util.private_env_value("MCS_ANTHROPIC_API_KEY", tmp_path) is not None) is (code is None)
    warnings = environment_diagnostics.cloud_setting_warnings({}, tmp_path)
    assert all("sk-synthetic" not in w for w in warnings)
    assert any("env_file_mode_not_0600" in w or "key_" in w for w in warnings) is (code not in (None, "key_missing"))


def test_missing_env_file_is_plain_absence(tmp_path):
    assert mcs_util.private_env_problem("TYPESAFE_API_KEY", tmp_path) == "env_file_missing"
    assert environment_diagnostics.cloud_setting_warnings({}, tmp_path) == []


def test_check_explains_key_only_without_cap_and_jev_zero_budget(tmp_path, now):
    _env(tmp_path, "MCS_ANTHROPIC_API_KEY=sk-synthetic\n")
    cfg = {"runtime_mode": "hermes", "semantic": {"mode": "assist", "project_ids": None}}
    warnings = environment_diagnostics.cloud_setting_warnings(cfg, tmp_path)
    assert any("cost_limit_required" in w for w in warnings)
    assert any("jev_budget_zero" in w for w in warnings)
    capped = {**cfg, "cloud_llm": {"daily_cost_limit_usd": 80},
              "semantic": {"mode": "assist", "project_ids": None, "daily_request_budget": 100,
                           "until": "2026-10-26T11:00:00+09:00"}}
    warnings = environment_diagnostics.cloud_setting_warnings(capped, tmp_path)
    assert not any("cost_limit_required" in w or "jev_budget_zero" in w for w in warnings)
    assert any("semantic.until" in w and "expired" in w for w in warnings)
