"""Synthetic ownership oracles for setup-compatible environment diagnostics."""
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
    monkeypatch.setattr(mcs_update, "REPO", checkout)
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
