"""Synthetic ownership oracles for setup-compatible environment diagnostics."""
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_preflight
import update_recovery
import update_repository
import update_runtime_migration
import update_services
import os

import pytest

import environment_diagnostics
import mcs_setup
import mcs_update
import mcs_util
import service_management


def test_setup_compatibility_entry_passes_finite_paths_at_call_time(monkeypatch, tmp_path):
    root, checkout, chrome = map(str, (tmp_path / "root", tmp_path / "checkout", tmp_path / "chrome"))
    monkeypatch.setattr(mcs_setup, "HOME", root)
    monkeypatch.setattr(mcs_setup, "REPO_ROOT", checkout)
    monkeypatch.setattr(mcs_setup, "CHROME_BIN", chrome)
    errors, warnings, calls = [], [], []

    def diagnose(cfg, **facts):
        calls.append((cfg, facts))
        return errors, warnings

    monkeypatch.setattr(environment_diagnostics, "check_environment", diagnose)
    cfg = {"runtime_mode": "hermes"}
    result = mcs_setup.check_environment(cfg)
    assert result[0] is errors and result[1] is warnings
    assert calls[0][0] is cfg
    facts = calls[0][1]
    assert facts["paths"].root == root and facts["paths"].checkout == checkout
    assert facts["chrome_bin"] == chrome
    assert facts["credential_paths"] == (os.path.join(mcs_util.HOME, ".env"),
                                         os.path.expanduser("~/.hermes/.env"))
    assert facts["agents_dir"] == os.path.expanduser("~/Library/LaunchAgents")
    next_root = str(tmp_path / "second-root")
    monkeypatch.setattr(mcs_setup, "HOME", next_root)
    mcs_setup.check_environment(cfg)
    assert calls[1][1]["paths"].root == next_root
    assert calls[0][1]["paths"].root == root


@pytest.mark.parametrize("unavailable", [False, True])
def test_updater_baseline_uses_diagnostic_owner_without_setup_entry(monkeypatch, tmp_path, unavailable):
    root, checkout = str(tmp_path / "root"), str(tmp_path / "checkout")
    monkeypatch.setattr(mcs_update, "HOME", root)
    monkeypatch.setattr(update_apply, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_cli, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_execution, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_lifecycle_context, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_preflight, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_recovery, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_runtime_migration, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(mcs_update, "REPO", checkout)
    monkeypatch.setattr(update_apply, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_cli, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_execution, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_repository, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_services, 'REPO', mcs_update.REPO)
    calls = []
    cfg, errors = {}, ["synthetic environment finding"]

    def forbidden(*args, **kwargs):
        pytest.fail("new updater must not call the setup CLI diagnostic facade")

    def diagnose(value, **facts):
        calls.append((value, facts))
        if unavailable:
            raise RuntimeError("synthetic unavailable diagnostic")
        return errors, []

    monkeypatch.setattr(mcs_setup, "check_environment", forbidden)
    monkeypatch.setattr(environment_diagnostics, "check_environment", diagnose)
    result = mcs_update._baseline_check(cfg)
    assert result == ([] if unavailable else errors)
    assert len(calls) == 1 and calls[0][0] is cfg
    assert calls[0][1]["paths"].root == root
    assert calls[0][1]["paths"].checkout == checkout


def test_invalid_mode_does_not_start_environment_probes(monkeypatch, tmp_path):
    paths = service_management.default_paths(root=str(tmp_path), checkout=str(tmp_path),
                                             platform="linux", uid=0)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid runtime mode must return before machine or secret probes")

    monkeypatch.setattr(environment_diagnostics, "env_value", forbidden)
    monkeypatch.setattr(environment_diagnostics.lifecycle_io, "run_bounded", forbidden)
    monkeypatch.setattr(environment_diagnostics, "check_llm", forbidden)
    errors, warnings = environment_diagnostics.check_environment(
        {"runtime_mode": "synthetic-invalid"}, paths=paths, chrome_bin="synthetic-chrome",
        credential_paths=(), agents_dir="synthetic-agents")
    assert len(errors) == 1 and "実行方法が不正" in errors[0]
    assert warnings == []


def _synthetic_env(root, content):
    path = root / ".env"
    path.write_text(content)
    path.chmod(0o600)


def test_openai_only_auto_selection_controls_budget_and_window_warning(tmp_path, monkeypatch):
    _synthetic_env(tmp_path, "MCS_OPENAI_API_KEY=synthetic-secret-canary\n")
    cfg = {"cloud_llm": {"daily_cost_limit_usd": None, "monthly_cost_limit_usd": None}}
    warnings = environment_diagnostics.cloud_setting_warnings(cfg, tmp_path)
    assert any("OpenAI" in item and "cost_limit_required" in item for item in warnings)
    assert all("synthetic-secret-canary" not in item for item in warnings)
    monkeypatch.setattr(environment_diagnostics.time, "time", lambda: 2000000000)
    cfg = {"cloud_llm": {"mode": "on", "project_ids": None, "provider": "auto",
                         "until": "2026-10-26T00:00:00+09:00"}}
    warnings = environment_diagnostics.cloud_setting_warnings(cfg, tmp_path)
    assert any("OpenAI" in item and "expired" in item for item in warnings)
    assert not any("Anthropicへの" in item for item in warnings)


def test_auto_provider_fallback_and_openai_key_problem_are_safe(tmp_path, monkeypatch):
    _synthetic_env(tmp_path, "MCS_ANTHROPIC_API_KEY=synthetic-secret-canary\n"
                             "MCS_OPENAI_API_KEY=synthetic-invalid-value \n")
    monkeypatch.setattr(environment_diagnostics.time, "time", lambda: 2000000000)
    cfg = {"cloud_llm": {"mode": "on", "project_ids": None, "provider": "auto",
                         "provider_order": ["openai", "anthropic"],
                         "until": "2026-10-26T00:00:00+09:00"}}
    warnings = environment_diagnostics.cloud_setting_warnings(cfg, tmp_path)
    assert any("MCS_OPENAI_API_KEY" in item and "key_whitespace_or_control" in item for item in warnings)
    assert any("Anthropic" in item and "expired" in item for item in warnings)
    assert all("synthetic-secret-canary" not in item and "synthetic-invalid-value" not in item for item in warnings)


def test_jev_zero_budget_warning_names_existing_semantic_path(tmp_path):
    cfg = {"semantic": {"mode": "assist", "project_ids": None}}
    warnings = environment_diagnostics.cloud_setting_warnings(cfg, tmp_path)
    assert any("semantic.daily_request_budget" in item and "jev_budget_zero" in item for item in warnings)
