"""Hermes-mode LINE WORKS diagnostics, doctor wording and standalone init (synthetic)."""
import subprocess
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

import environment_diagnostics
import lifecycle_io
import local_llm
import mcs_setup
import mcs_util
import service_contract
import service_management
import setup_wizard
from test_mcs_setup_doctor import local_config  # noqa: F401  shared doctor fixture
from test_mcs_setup_lineworks import config

LW = service_contract.LINEWORKS_LABEL


def test_lineworks_adapter_is_diagnosed_but_never_installed_by_services():
    hermes = config()
    assert LW not in service_contract.desired_agent_labels(hermes)
    assert service_contract.diagnosed_agent_labels(hermes) == \
        service_contract.desired_agent_labels(hermes) + [LW]
    standalone = {**config(), "runtime_mode": "standalone"}
    assert LW not in service_contract.diagnosed_agent_labels(standalone)
    slack = {"notify": {"interactive": "slack"}}
    assert service_contract.diagnosed_agent_labels(slack) == service_contract.desired_agent_labels(slack)
    assert not service_contract.owns_agent(LW)


def _darwin(monkeypatch, *, exists, loaded, launchctl_out=""):
    monkeypatch.setattr(environment_diagnostics.sys, "platform", "darwin")
    monkeypatch.setattr(environment_diagnostics.os.path, "exists", exists)
    monkeypatch.setattr(environment_diagnostics.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(environment_diagnostics.os, "access", lambda p, m: True)
    monkeypatch.setattr(mcs_util.shutil, "which", lambda *a: "/x/hermes")
    monkeypatch.setattr(local_llm, "bounded_request", lambda *a, **k: (200, {}, b"{}"))
    monkeypatch.setattr(lifecycle_io.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(lifecycle_io, "run_bounded",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=launchctl_out, stderr=""))
    monkeypatch.setattr(service_management, "agent_loaded",
                        lambda label, **_kw: label in loaded)


def test_missing_lineworks_plist_points_to_the_adapter_guide(monkeypatch):
    _darwin(monkeypatch, exists=lambda p: not p.endswith(LW + ".plist"),
            loaded=set(service_contract.AGENT_LABELS))
    errors, warnings = mcs_setup.check_environment(deepcopy(config()))
    [warning] = [w for w in warnings if LW in w]
    assert "lineworks_adapter service" in warning and "docs/guides/LINEWORKS.md" in warning
    assert "deployment/launchagents" not in warning
    assert not any(LW in e for e in errors)


def test_unloaded_lineworks_adapter_is_a_warning_without_services_hint(monkeypatch):
    _darwin(monkeypatch, exists=lambda p: True, loaded=set(service_contract.AGENT_LABELS))
    errors, warnings = mcs_setup.check_environment(deepcopy(config()))
    assert not any(LW in e for e in errors)   # warning only: no new updater postcheck error
    [warning] = [w for w in warnings if LW in w and "読み込まれていません" in w]
    assert "mcs_setup.py services" not in warning
    assert "launchctl bootstrap" in warning


@pytest.mark.parametrize("newest,warned", [(2000.0, True), (500.0, False), (None, False)])
def test_lineworks_adapter_older_than_its_sources_needs_restart(monkeypatch, newest, warned):
    _darwin(monkeypatch, exists=lambda p: True,
            loaded=set(service_contract.AGENT_LABELS) | {LW}, launchctl_out="\tpid = 4242\n")
    monkeypatch.setattr(environment_diagnostics, "process_started_at",
                        lambda pid: 1000.0 if pid == 4242 else None)
    monkeypatch.setattr(environment_diagnostics, "gateway_sources_mtime",
                        lambda checkout, dirs=None: newest
                        if dirs == environment_diagnostics.LINEWORKS_SOURCE_DIRS else None)
    _, warnings = mcs_setup.check_environment(deepcopy(config()))
    stale = [w for w in warnings if "稼働中のアダプターより新しく" in w]
    assert bool(stale) is warned
    if warned:
        assert f"kickstart -k gui/{mcs_setup._service_paths().uid}/{LW}" in stale[0]


def test_doctor_names_the_lineworks_adapter_and_its_remedy(local_config, monkeypatch, capsys):  # noqa: F811
    import card_worker_status
    monkeypatch.setattr(mcs_util, "load_config", lambda: config())
    monkeypatch.setattr(card_worker_status, "diagnose", lambda *a, **kw: {
        "state": "disabled", "reason": "worker_start_failed",
        "restore_delivery_held": False, "recovery_scope": None})
    mcs_setup.main(["doctor"])
    out = capsys.readouterr().out
    assert "通知の配送処理（LINE WORKSアダプター）: 停止・無効（配送処理の起動に失敗しました）" in out
    assert "ai.mcs.lineworks" in out and "service.stderr.log" in out


def test_lineworks_update_diagnostic_keeps_one_deadline(monkeypatch):
    _darwin(monkeypatch, exists=lambda p: True,
            loaded=set(service_contract.AGENT_LABELS) | {LW})
    calls = []
    def record(name, result):
        def probe(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result
        return probe
    monkeypatch.setattr(service_management, 'agent_loaded', record('loaded', True))
    monkeypatch.setattr(environment_diagnostics, 'launchd_pid', record('pid', 4242))
    monkeypatch.setattr(environment_diagnostics, 'process_started_at', record('started', 1000))
    monkeypatch.setattr(environment_diagnostics, 'gateway_sources_mtime', record('sources', 2000))
    _, warnings = environment_diagnostics.check_environment(
        deepcopy(config()), paths=mcs_setup._service_paths(), chrome_bin='/synthetic/chrome',
        credential_paths=(), agents_dir='/synthetic/agents', deadline=101)
    assert any('稼働中のアダプターより新しく' in warning for warning in warnings)
    assert all(kwargs['deadline'] == 101 for _, _, kwargs in calls)
    assert {name for name, _, _ in calls} == {'loaded', 'pid', 'started', 'sources'}
    assert next(args[1] for name, args, _ in calls if name == 'sources') == \
        environment_diagnostics.LINEWORKS_SOURCE_DIRS


def test_doctor_keeps_not_hermes_owned_wording_when_not_applicable(local_config, monkeypatch, capsys):  # noqa: F811
    import card_worker_status
    # diagnose answers not_applicable for standalone; the doctor line must not name the adapter.
    monkeypatch.setattr(mcs_util, "load_config", lambda: config())
    monkeypatch.setattr(card_worker_status, "diagnose", lambda *a, **kw: {
        "state": "not_applicable", "reason": "worker_not_hermes_owned",
        "restore_delivery_held": False, "recovery_scope": None})
    mcs_setup.main(["doctor"])
    out = capsys.readouterr().out
    assert "通知の配送処理: 対象外（Hermes経由の配送は対象外です）" in out
    assert "LINE WORKSアダプター" not in out


@pytest.mark.parametrize("yes", [False, True])
def test_standalone_init_runs_the_lineworks_adapter_initializer(monkeypatch, yes):
    calls = []
    monkeypatch.setattr(mcs_setup.subprocess, "run",
                        lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(returncode=0))
    cfg = {**config(), "runtime_mode": "standalone"}
    assert mcs_setup._init_standalone_connectors(cfg, SimpleNamespace(yes=yes), True) is True
    [(argv, kw)] = calls
    assert argv == [sys.executable, "-m", "lineworks_adapter", "init", "--root", mcs_setup.HOME]
    # --yes never lets the adapter prompt for credentials.
    assert kw["stdin"] == (subprocess.DEVNULL if yes else None)
    monkeypatch.setattr(mcs_setup.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert mcs_setup._init_standalone_connectors(cfg, SimpleNamespace(yes=yes), True) is False


def test_wizard_lineworks_scope_follows_transport_enabled():
    text_only = {"runtime_mode": "standalone", "notify_target": "lineworks:room-synthetic",
                 "notify": {"interactive": "slack"}}
    assert setup_wizard._lineworks_on(text_only) is True
    assert setup_wizard._lineworks_on({**text_only, "runtime_mode": "hermes"}) is False
    assert setup_wizard._lineworks_on(config()) is True


def test_standalone_save_summary_includes_lineworks_credentials(monkeypatch, capsys):
    cfg = {"runtime_mode": "standalone", "notify_target": "lineworks:synthetic-room",
           "notify": {"interactive": "lineworks"}}
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    setup_wizard.confirm_save(cfg, plugin_profile=None)
    out = capsys.readouterr().out
    assert "Slack・Discord・LINE WORKSの認証情報と接続設定" in out
    assert "別手順" not in out
