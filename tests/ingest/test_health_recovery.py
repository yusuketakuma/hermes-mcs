"""Bounded auto-recovery: fixed causes -> fixed actions, journaled and
capped; unproven states never produce an action. Temp home + injected
launchctl/restart doubles only."""
import json
import os
import types

import health_recovery

NOW = 1_000_000.0


def _home(tmp_path):
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True)
    return str(home)


def _manifest(home, labels):
    (open(os.path.join(home, "data", "service_manifest.json"), "w")).write(
        json.dumps({"agents": [{"label": lb, "loaded": True}
                               for lb in labels]}))


def _journal(home, *, updated_at=NOW - 4000, pid=4194304):
    """An install_progress.json proving an interrupted install: running
    status, stale timestamp, dead recorded pid."""
    (open(os.path.join(home, "data", "install_progress.json"), "w")).write(
        json.dumps({"version": 1, "status": "running", "stage": "services",
                    "invocation": "install", "run_id": f"{pid}-{int(updated_at)}",
                    "updated_at": updated_at}))


def _launch(calls, states):
    """states keys: 'loaded:<label>' -> rc0, 'absent:<label>' -> rc113,
    anything else -> rc124 (probe failure)."""
    def fake(argv):
        calls.append(argv)
        if argv[1] == "print":
            label = argv[-1].rsplit("/", 1)[-1]
            if states.get("loaded:" + label) == 0:
                rc = 0
            elif states.get("absent:" + label):
                rc = 113
            else:
                rc = 124
            return types.SimpleNamespace(returncode=rc)
        return types.SimpleNamespace(returncode=0)
    return fake


def _report(reasons, mode="standalone"):
    return {"status": "degraded", "state_reasons": reasons,
            "runtime_mode": mode}


def test_disabled_config_never_acts(tmp_path):
    calls = []
    out = health_recovery.run(
        _report(["standalone_check_stalled"]), _home(tmp_path),
        {"auto_recovery": {"enabled": False}}, NOW,
        launch=_launch(calls, {}), restart=lambda h: "rid")
    assert out["enabled"] is False and out["actions"] == [] and calls == []


def test_invalid_config_fails_closed(tmp_path):
    out = health_recovery.run(
        _report(["standalone_check_stalled"]), _home(tmp_path),
        {"auto_recovery": "yes"}, NOW, launch=_launch([], {}),
        restart=lambda h: "rid")
    assert out["enabled"] is False and out["reason"] == "config_invalid"


def test_healthy_report_never_acts(tmp_path):
    calls = []
    out = health_recovery.run(
        _report([], "hermes"), _home(tmp_path), {}, NOW,
        launch=_launch(calls, {}), restart=lambda h: "rid")
    assert out["actions"] == [] and calls == []


def test_standalone_alive_host_gets_graceful_restart(tmp_path):
    home = _home(tmp_path)
    calls = []
    seen = []

    def restart(root):
        seen.append(root)
        return "rid123"

    out = health_recovery.run(
        _report(["standalone_check_stalled", "notification_pending"]), home,
        {"runtime_mode": "standalone"}, NOW,
        launch=_launch(calls, {"loaded:ai.mcs.standalone": 0}), restart=restart)
    assert seen == [home]
    assert out["actions"] == [
        {"target": "ai.mcs.standalone", "action": "restart_request",
         "result": "requested", "reasons": ["standalone_check_stalled"]}]
    state = json.loads(open(os.path.join(home, "data", "auto_recovery.json")).read())
    assert state["targets"]["ai.mcs.standalone"]["attempts"] == 1
    assert state["history"][-1]["action"] == "restart_request"


def test_second_failure_escalates_to_kickstart(tmp_path):
    home = _home(tmp_path)
    calls = []
    states = {"loaded:ai.mcs.standalone": 0}
    cfg = {"runtime_mode": "standalone"}
    report = _report(["standalone_child_failing"])

    def restart(_home):
        return "rid"

    first = health_recovery.run(report, home, cfg, NOW,
                                launch=_launch(calls, states), restart=restart)
    assert first["actions"][0]["action"] == "restart_request"
    second = health_recovery.run(report, home, cfg, NOW + 300,
                                 launch=_launch(calls, states), restart=restart)
    assert second["actions"][0]["action"] == "kickstart"
    assert any("kickstart" in " ".join(c) for c in calls)


def test_unreachable_host_kickstarts_directly(tmp_path):
    home = _home(tmp_path)
    calls = []
    states = {"loaded:ai.mcs.standalone": 0}

    def restart(_root):
        raise ValueError("standalone_status_unavailable")

    out = health_recovery.run(
        _report(["standalone_host_down"]), home,
        {"runtime_mode": "standalone"}, NOW,
        launch=_launch(calls, states), restart=restart)
    assert out["actions"][0]["action"] == "kickstart"
    assert out["actions"][0]["result"] == "done"


def test_attempts_exhaust_then_stop(tmp_path):
    home = _home(tmp_path)
    calls = []
    states = {"loaded:ai.mcs.standalone": 0}
    cfg = {"runtime_mode": "standalone",
           "auto_recovery": {"max_attempts": 2}}
    report = _report(["standalone_check_stalled"])

    def restart(_home):
        return "rid"

    launch = _launch(calls, states)
    health_recovery.run(report, home, cfg, NOW, launch=launch, restart=restart)
    health_recovery.run(report, home, cfg, NOW + 300, launch=launch, restart=restart)
    third = health_recovery.run(report, home, cfg, NOW + 600,
                                launch=launch, restart=restart)
    assert third["actions"] == [] and "ai.mcs.standalone" in third["exhausted"]


def test_update_stuck_is_not_a_restart_trigger(tmp_path):
    calls = []
    out = health_recovery.run(
        _report(["standalone_update_stuck"]), _home(tmp_path),
        {"runtime_mode": "standalone"}, NOW,
        launch=_launch(calls, {"loaded:ai.mcs.standalone": 0}),
        restart=lambda h: "rid")
    assert out["actions"] == []


def test_absent_agent_bootstraps_only_with_manifest_and_plist(tmp_path, monkeypatch):
    home = _home(tmp_path)
    calls = []
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "local.mcs-cmd.plist").write_text("<plist/>")
    monkeypatch.setattr(health_recovery, "_agents_dir", lambda: str(agents))
    _manifest(home, ["local.mcs-cmd"])
    _journal(home)
    cfg = {"runtime_mode": "hermes"}
    states = {"absent:local.mcs-cmd": True}
    out = health_recovery.run(_report([], "hermes"), home, cfg, NOW,
                              launch=_launch(calls, states), restart=lambda h: None)
    assert out["actions"] == [
        {"target": "local.mcs-cmd", "action": "bootstrap", "result": "done",
         "reasons": ["agent_absent"]}]
    assert any(a[:2] == ["launchctl", "bootstrap"] for a in calls)


def test_absent_agent_without_manifest_evidence_stays_down(tmp_path, monkeypatch):
    home = _home(tmp_path)
    calls = []
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "local.mcs-cmd.plist").write_text("<plist/>")
    monkeypatch.setattr(health_recovery, "_agents_dir", lambda: str(agents))
    _journal(home)
    out = health_recovery.run(_report([], "hermes"), home,
                              {"runtime_mode": "hermes"}, NOW,
                              launch=_launch(calls, {"absent:local.mcs-cmd": True}),
                              restart=lambda h: None)
    assert out["actions"] == []
    assert not any(a[1] == "bootstrap" for a in calls)


def test_absent_agent_without_interrupted_install_stays_down(tmp_path, monkeypatch):
    """A manually booted-out agent is operator intent: no journal proving
    an interrupted install means no bootstrap, even with manifest+plist."""
    home = _home(tmp_path)
    calls = []
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "local.mcs-cmd.plist").write_text("<plist/>")
    monkeypatch.setattr(health_recovery, "_agents_dir", lambda: str(agents))
    _manifest(home, ["local.mcs-cmd"])
    out = health_recovery.run(_report([], "hermes"), home,
                              {"runtime_mode": "hermes"}, NOW,
                              launch=_launch(calls, {"absent:local.mcs-cmd": True}),
                              restart=lambda h: None)
    assert out["actions"] == [] and calls == []


def test_live_install_journal_never_bootstraps(tmp_path, monkeypatch):
    """A fresh 'running' journal may be a live install — services are
    quiesced mid-run, so nothing may be resurrected."""
    home = _home(tmp_path)
    calls = []
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "local.mcs-cmd.plist").write_text("<plist/>")
    monkeypatch.setattr(health_recovery, "_agents_dir", lambda: str(agents))
    _manifest(home, ["local.mcs-cmd"])
    _journal(home, updated_at=NOW - 60)          # fresh: possibly live
    out = health_recovery.run(_report([], "hermes"), home,
                              {"runtime_mode": "hermes"}, NOW,
                              launch=_launch(calls, {"absent:local.mcs-cmd": True}),
                              restart=lambda h: None)
    assert out["actions"] == [] and calls == []


def test_absent_host_bootstraps_only_on_proven_interruption(tmp_path, monkeypatch):
    home = _home(tmp_path)
    calls = []
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "ai.mcs.standalone.plist").write_text("<plist/>")
    monkeypatch.setattr(health_recovery, "_agents_dir", lambda: str(agents))
    _manifest(home, ["ai.mcs.standalone"])
    states = {"absent:ai.mcs.standalone": True}
    cfg = {"runtime_mode": "standalone"}
    report = _report(["standalone_host_down"])
    without = health_recovery.run(report, home, cfg, NOW,
                                  launch=_launch(calls, states),
                                  restart=lambda h: "rid")
    assert without["actions"] == []              # no journal: manual stop
    _journal(home)
    with_journal = health_recovery.run(report, home, cfg, NOW + 300,
                                       launch=_launch(calls, states),
                                       restart=lambda h: "rid")
    assert with_journal["actions"][0]["action"] == "bootstrap"


def test_failed_probe_never_acts(tmp_path):
    home = _home(tmp_path)
    calls = []
    out = health_recovery.run(
        _report(["standalone_check_stalled"]), home,
        {"runtime_mode": "standalone"}, NOW,
        launch=_launch(calls, {}), restart=lambda h: "rid")
    # launchctl print returned 124 (timeout) for every label -> fail closed
    assert out["actions"] == []
