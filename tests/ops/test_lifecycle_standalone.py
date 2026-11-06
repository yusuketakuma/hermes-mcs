"""Update and recovery in runtime_mode=standalone: restart the connector
agent instead of the Hermes gateway and never require Hermes."""
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import update_services
import mcs_util
import service_contract
import update_launch
import runtime_inspection
import json

import mcs_update
from ops_testkit import _load


def _popen_log(monkeypatch, module):
    started = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    return started


def test_update_restarts_the_connector_or_gateway(monkeypatch, tmp_path):
    started = _popen_log(monkeypatch, mcs_update)
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(update_lifecycle_context, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_preflight, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_services, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(mcs_update, "_uid", lambda: 501)
    monkeypatch.setattr(update_services, '_uid', mcs_update._uid)
    mcs_update.restart_gateway({})
    assert started[-1][1:3] == ["-I", "-c"] and started[-1][-1] == "501"
    from mcs_standalone import service
    requests = []
    monkeypatch.setattr(service, "request_restart", lambda root: requests.append(root))
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(update_apply, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_lifecycle_context, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_planning, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_preflight, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_recovery, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_restore, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_services, 'DATA', mcs_update.DATA)
    mcs_update.restart_gateway({"runtime_mode": "standalone"})
    assert requests == [str(tmp_path)]
    assert len(started) == 1  # no forced host kill, so an active updater can finish


def test_update_precheck_needs_no_hermes_in_standalone(monkeypatch):
    import mcs_setup
    monkeypatch.setattr(mcs_update, "_tree_clean", lambda: True)
    monkeypatch.setattr(update_apply, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_execution, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_preflight, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_recovery, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_restore, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(update_runtime_migration, '_tree_clean', mcs_update._tree_clean)
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda exe: False)
    monkeypatch.setattr(runtime_inspection, "python_problem", lambda exe: None)
    monkeypatch.setattr(mcs_setup, "validate_config", lambda cfg: ([], []))
    assert "hermes_not_resolvable" in mcs_update.precheck_local({})
    assert "hermes_not_resolvable" not in mcs_update.precheck_local({"runtime_mode": "standalone"})
    monkeypatch.setattr(runtime_inspection, "python_problem", lambda exe: "missing")
    assert "standalone_interpreter_unavailable" in mcs_update.precheck_local({"runtime_mode": "standalone"})


def test_recovery_tool_follows_the_runtime(monkeypatch, tmp_path):
    recover = _load()
    core, services = recover.mcs_recovery_core, recover.mcs_recovery_services
    monkeypatch.setattr(core, "HOME", str(tmp_path))
    monkeypatch.setattr(core, "DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(core, "AGENTS_DIR", str(tmp_path))
    started = _popen_log(monkeypatch, services)
    services._restart_gateway()
    assert started[-1][1:3] == ["-I", "-c"] and started[-1][-3] == core.DATA
    (tmp_path / "config.json").write_text(json.dumps({"runtime_mode": "standalone"}))
    (tmp_path / "ai.mcs.standalone.plist").write_text("")
    monkeypatch.setattr(services, "_standalone_status", lambda: {"generation": "a" * 32})
    services._restart_gateway()
    assert len(started) == 1
    request = json.loads((tmp_path / "data" / "standalone-restart.request").read_text())
    assert request["generation"] == "a" * 32 and len(request["request_id"]) == 32
    (tmp_path / "config.json").write_text(json.dumps({
        "runtime_mode": "standalone", "notify_target": "slack:C12345678",
        "notify_system_target": "discord:42"}))
    checked = []
    def unavailable(path, mode):
        checked.append((path, mode))
        return False
    monkeypatch.setattr(services.os, "access", unavailable)
    services._notify("synthetic")       # no interpreter: best-effort skip
    assert checked == [(str(tmp_path / "venv/bin/python3"), services.os.X_OK)]
    assert len(started) == 1
    # standalone-owned agents count as known: a rollback never deletes them
    assert {"ai.mcs.standalone", "ai.mcs.cron.mcs-check"} <= core.KNOWN_AGENT_LABELS


def test_update_ops_find_the_standalone_wrapper(monkeypatch, tmp_path):
    import mcs_operations
    import mcs_runtime
    monkeypatch.setattr(mcs_runtime, "HOME", str(tmp_path))
    monkeypatch.setattr("mcs_util.HOME", str(tmp_path))
    monkeypatch.setattr(update_launch, 'HERMES_WRAPPER', str(tmp_path / "hermes" / "mcs_update.sh"))
    cfg = {"runtime_mode": "standalone", "update": {"mode": "notify"}}
    monkeypatch.setattr(mcs_util, 'load_config', lambda *a: cfg)
    monkeypatch.setattr("mcs_util.load_config", lambda *a: cfg)
    wrapper = tmp_path / "scripts" / "mcs_update.sh"
    assert update_launch.wrapper_path(cfg, root=tmp_path,
                                      hermes_wrapper=update_launch.HERMES_WRAPPER) == str(wrapper)
    req = {"cmd": "ops.update_apply", "tag": "v9.9.9"}
    assert mcs_operations._apply_update_op_tx(None, req, None)[0] == "updater_not_deployed"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\n")
    assert mcs_operations._apply_update_op_tx(None, req, None)[0] != "updater_not_deployed"


def test_connector_changes_restart_only_in_standalone(monkeypatch):
    monkeypatch.setattr(mcs_update, "_git_out", lambda args: "M\tmcs_standalone/__main__.py\n")
    monkeypatch.setattr(update_apply, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_planning, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_repository, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_restore, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(mcs_util, 'load_config', lambda *a: {})
    assert not any("通知接続（gateway）の再起動" in n for n in mcs_update.impact_summary("a", "v1"))
    monkeypatch.setattr(mcs_util, 'load_config', lambda *a: {"runtime_mode": "standalone"})
    assert any("通知接続（gateway）の再起動" in n for n in mcs_update.impact_summary("a", "v1"))


def test_rollback_to_a_pre_standalone_tree_is_refused(monkeypatch):
    import pytest
    from types import SimpleNamespace
    monkeypatch.setattr(mcs_util, 'load_config', lambda *a: {"runtime_mode": "standalone"})
    monkeypatch.setattr(mcs_update, "_git", lambda args: SimpleNamespace(returncode=128))
    monkeypatch.setattr(update_apply, '_git', mcs_update._git)
    monkeypatch.setattr(update_cli, '_git', mcs_update._git)
    monkeypatch.setattr(update_planning, '_git', mcs_update._git)
    monkeypatch.setattr(update_preflight, '_git', mcs_update._git)
    monkeypatch.setattr(update_recovery, '_git', mcs_update._git)
    monkeypatch.setattr(update_repository, '_git', mcs_update._git)
    monkeypatch.setattr(update_restore, '_git', mcs_update._git)
    monkeypatch.setattr(update_runtime_migration, '_git', mcs_update._git)
    resets = []
    monkeypatch.setattr(mcs_update, "_git_out", lambda args: resets.append(args) or "")
    monkeypatch.setattr(update_apply, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_planning, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_preflight, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_recovery, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_repository, '_git_out', mcs_update._git_out)
    monkeypatch.setattr(update_restore, '_git_out', mcs_update._git_out)
    with pytest.raises(mcs_update.UpdateError, match="standalone_runtime_missing_in_target"):
        mcs_update._rollback_tree({"prev_sha": "0" * 40})
    assert resets == []


def test_detached_updater_drops_the_launchd_job_identity(monkeypatch, tmp_path):
    monkeypatch.setattr("run_log_install.helper_path", lambda *a, **k: "/synthetic/helper.py")
    seen = {}
    monkeypatch.setattr("mcs_util.load_config", lambda: {})
    monkeypatch.setattr(update_launch, 'wrapper_path', lambda *a, **kw: "/x/mcs_update.sh")
    monkeypatch.setattr(update_launch.subprocess, "Popen", lambda argv, **kw: seen.update(kw))
    monkeypatch.setenv("XPC_SERVICE_NAME", "local.mcs-cmd")
    monkeypatch.setenv("MCS_JOB_PID", "1")
    update_launch.spawn_detached(
        root=tmp_path, data_dir=tmp_path, hermes_wrapper="/x/mcs_update.sh",
        environment=mcs_update.os.environ)
    assert "XPC_SERVICE_NAME" not in seen["env"] and "MCS_JOB_PID" not in seen["env"]


def test_the_hermes_independent_health_watch_is_known_everywhere():
    """local.mcs-health keeps alerting when one long hermes cron job blocks
    the rest; services render it, the updater reloads it and recovery must
    never delete it."""
    import plistlib
    from pathlib import Path

    import mcs_setup
    recover = _load()
    assert "local.mcs-health" in service_contract.AGENT_LABELS
    assert set(service_contract.AGENT_LABELS) <= recover.KNOWN_AGENT_LABELS
    assert "local.mcs-health" in service_contract.WATCHER_LABELS
    body = Path(mcs_setup.REPO_ROOT, "deployment", "launchagents",
                "local.mcs-health.plist").read_bytes()
    plist = plistlib.loads(body)
    assert plist["StartInterval"] == 300 and plist["RunAtLoad"] is False
    assert plist["ProgramArguments"][8].endswith("/mcs/ingest/health_watch.py")
    assert plist["ProgramArguments"][1:2] == ["-I"] and plist["ProgramArguments"][3:7] == ["exec", "--data-dir", "__DATA__", "--"]
