"""Update and recovery in runtime_mode=standalone: restart the connector
agent instead of the Hermes gateway and never require Hermes."""
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
    monkeypatch.setattr(mcs_update, "_uid", lambda: 501)
    mcs_update.restart_gateway({})
    assert started[-1][-1] == "gui/501/ai.hermes.gateway"
    from mcs_standalone import service
    requests = []
    monkeypatch.setattr(service, "request_restart", lambda root: requests.append(root))
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path / "data"))
    mcs_update.restart_gateway({"runtime_mode": "standalone"})
    assert requests == [str(tmp_path)]
    assert len(started) == 1  # no forced host kill, so an active updater can finish


def test_update_precheck_needs_no_hermes_in_standalone(monkeypatch):
    import mcs_setup
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda exe: False)
    monkeypatch.setattr(mcs_setup, "_py_problem", lambda exe: None)
    monkeypatch.setattr(mcs_setup, "validate_config", lambda cfg: ([], []))
    assert "hermes_not_resolvable" in mcs_update.precheck_local({})
    assert "hermes_not_resolvable" not in mcs_update.precheck_local({"runtime_mode": "standalone"})
    monkeypatch.setattr(mcs_setup, "_py_problem", lambda exe: "missing")
    assert "standalone_interpreter_unavailable" in mcs_update.precheck_local({"runtime_mode": "standalone"})


def test_recovery_tool_follows_the_runtime(monkeypatch, tmp_path):
    recover = _load()
    monkeypatch.setattr(recover, "HOME", str(tmp_path))
    monkeypatch.setattr(recover, "DATA", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(recover, "AGENTS_DIR", str(tmp_path))
    started = _popen_log(monkeypatch, recover)
    recover._restart_gateway()
    assert started[-1][-1].endswith("/ai.hermes.gateway")
    (tmp_path / "config.json").write_text(json.dumps({"runtime_mode": "standalone"}))
    (tmp_path / "ai.mcs.standalone.plist").write_text("")
    monkeypatch.setattr(recover, "_standalone_status", lambda: {"generation": "a" * 32})
    recover._restart_gateway()
    assert len(started) == 1
    request = json.loads((tmp_path / "data" / "standalone-restart.request").read_text())
    assert request["generation"] == "a" * 32 and len(request["request_id"]) == 32
    monkeypatch.setattr(recover, "_setup_python", lambda: None)
    recover._notify("synthetic")       # no interpreter: best-effort skip
    assert len(started) == 1
    # standalone-owned agents count as known: a rollback never deletes them
    assert {"ai.mcs.standalone", "ai.mcs.cron.mcs-check"} <= recover.KNOWN_AGENT_LABELS


def test_update_ops_find_the_standalone_wrapper(monkeypatch, tmp_path):
    import mcs_operations
    import mcs_runtime
    monkeypatch.setattr(mcs_runtime, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_update, "WRAPPER", str(tmp_path / "hermes" / "mcs_update.sh"))
    cfg = {"runtime_mode": "standalone", "update": {"mode": "notify"}}
    monkeypatch.setattr(mcs_update, "load_config", lambda *a: cfg)
    monkeypatch.setattr("mcs_util.load_config", lambda *a: cfg)
    wrapper = tmp_path / "scripts" / "mcs_update.sh"
    assert mcs_update.wrapper_path() == str(wrapper)
    req = {"cmd": "ops.update_apply", "tag": "v9.9.9"}
    assert mcs_operations._apply_update_op_tx(None, req, None)[0] == "updater_not_deployed"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\n")
    assert mcs_operations._apply_update_op_tx(None, req, None)[0] != "updater_not_deployed"


def test_connector_changes_restart_only_in_standalone(monkeypatch):
    monkeypatch.setattr(mcs_update, "_git_out", lambda args: "M\tmcs_standalone/slack_runtime.py\n")
    monkeypatch.setattr(mcs_update, "load_config", lambda *a: {})
    assert not any("gateway restart" in n for n in mcs_update.impact_summary("a", "v1"))
    monkeypatch.setattr(mcs_update, "load_config", lambda *a: {"runtime_mode": "standalone"})
    assert any("gateway restart" in n for n in mcs_update.impact_summary("a", "v1"))


def test_rollback_to_a_pre_standalone_tree_is_refused(monkeypatch):
    import pytest
    from types import SimpleNamespace
    monkeypatch.setattr(mcs_update, "load_config", lambda *a: {"runtime_mode": "standalone"})
    monkeypatch.setattr(mcs_update, "_git", lambda args: SimpleNamespace(returncode=128))
    resets = []
    monkeypatch.setattr(mcs_update, "_git_out", lambda args: resets.append(args) or "")
    with pytest.raises(mcs_update.UpdateError, match="standalone_runtime_missing_in_target"):
        mcs_update._rollback_tree({"prev_sha": "0" * 40})
    assert resets == []


def test_detached_updater_drops_the_launchd_job_identity(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path))
    monkeypatch.setattr(mcs_update, "wrapper_path", lambda *a, **kw: "/x/mcs_update.sh")
    monkeypatch.setattr(mcs_update.subprocess, "Popen", lambda argv, **kw: seen.update(kw))
    monkeypatch.setenv("XPC_SERVICE_NAME", "local.mcs-cmd")
    monkeypatch.setenv("MCS_JOB_PID", "1")
    mcs_update.spawn_detached()
    assert "XPC_SERVICE_NAME" not in seen["env"] and "MCS_JOB_PID" not in seen["env"]
