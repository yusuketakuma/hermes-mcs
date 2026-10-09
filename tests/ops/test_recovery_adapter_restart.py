"""The independent recovery tool restarts what the completed update path
restarts (LINE WORKS adapter, standalone host) and never reports a lost
restart request as success. Temporary HOME and stubs only."""
import json
import time

import pytest
from ops_testkit import _load

TARGET, PREV = "b" * 40, "a" * 40
LW_LABEL = "ai.mcs.lineworks"


def _world(monkeypatch, tmp_path, cfg, *, path):
    recover = _load()
    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    for name, value in (("HOME", tmp_path), ("DATA", data), ("REPO", tmp_path / "repo"),
                        ("STATE_PATH", data / "update_state.json"),
                        ("UPDATE_LOCK", data / "update.lock"), ("RUN_LOCK", data / "run.lock"),
                        ("MARKER_PATH", data / "update_in_progress.marker"),
                        ("REPORT_PATH", data / "recovery_report.json"),
                        ("AGENTS_DIR", tmp_path / "agents")):
        monkeypatch.setattr(recover, name, str(value))
    started, notices = [], []

    def popen(argv, **kw):
        started.append(list(argv))
    monkeypatch.setattr(recover.subprocess, "Popen", popen)
    monkeypatch.setattr(recover, "_notify", notices.append)
    monkeypatch.setattr(recover, "_restart_drainers", lambda bounce=True: [])
    monkeypatch.setattr(recover, "_reconcile_membership", lambda snapshot: [])
    monkeypatch.setattr(recover, "_head", lambda: TARGET)
    monkeypatch.setattr(recover, "_clean", lambda: True)
    entry = {"tag": "v9.9.9", "sha": TARGET, "prev_sha": PREV,
             "command_id": "cid-1", "at": time.time()}
    if path == "resumed_done":
        state = {"v": 1, "applying": None, "applied": [entry],
                 "stages": [{"stage": "done", "at": time.time()}]}
    else:                                   # post-merge crash: head == target
        state = {"v": 1, "applying": entry, "applied": [],
                 "stages": [{"stage": "merge", "at": time.time()}]}
    (data / "update_state.json").write_text(json.dumps(state))
    return recover, started, notices


def _report(recover):
    return json.loads(open(recover.REPORT_PATH).read())


def _lineworks_kicks(started):
    return [argv for argv in started if argv[:3] == ["launchctl", "kickstart", "-k"]
            and argv[3].endswith("/" + LW_LABEL)]


@pytest.mark.parametrize("path", ["resumed_done", "resumed"])
def test_completed_recovery_restarts_the_lineworks_adapter(monkeypatch, tmp_path, path):
    recover, started, _ = _world(monkeypatch, tmp_path,
                                 {"notify": {"interactive": "lineworks"}}, path=path)
    assert recover.recover() == 0
    assert _report(recover)["result"] == path
    kicks = _lineworks_kicks(started)
    assert len(kicks) == 1 and kicks[0][3].startswith("gui/")


@pytest.mark.parametrize("cfg", [
    {"notify": {"interactive": "discord"}},
    {"notify": {"interactive": "slack"}},
    {"runtime_mode": "standalone", "notify": {"interactive": "lineworks"}},
    {"runtime_mode": "synthetic-invalid", "notify": {"interactive": "lineworks"}},
    {"notify": "lineworks"},
    {},
])
def test_no_lineworks_restart_outside_hermes_lineworks(monkeypatch, tmp_path, cfg):
    recover, started, _ = _world(monkeypatch, tmp_path, cfg, path="resumed_done")
    monkeypatch.setattr(recover, "_standalone_status", lambda: None)
    recover.recover()
    assert _lineworks_kicks(started) == []


@pytest.mark.parametrize("path", ["resumed_done", "resumed"])
def test_known_lineworks_kickstart_failure_is_reported(monkeypatch, tmp_path, path):
    recover, _, notices = _world(monkeypatch, tmp_path,
                                 {"notify": {"interactive": "lineworks"}}, path=path)

    def boom(argv, **kw):
        raise OSError("synthetic launchctl missing")
    monkeypatch.setattr(recover.subprocess, "Popen", boom)
    assert recover.recover() == 1
    report = _report(recover)
    assert report["result"] == "restart_request_failed" and report["completed"] == path
    assert any("再起動" in text for text in notices)
    assert "synthetic" not in json.dumps(report)
    state = json.loads(open(recover.STATE_PATH).read())
    assert state["stages"] == [] and not state.get("applying")   # bookkeeping kept


@pytest.mark.parametrize("path", ["resumed_done", "resumed"])
def test_lost_standalone_restart_request_is_reported_not_success(monkeypatch, tmp_path, path):
    recover, _, notices = _world(monkeypatch, tmp_path,
                                 {"runtime_mode": "standalone"}, path=path)
    monkeypatch.setattr(recover, "_standalone_status", lambda: {"generation": "a" * 32})
    real = recover.tempfile.mkstemp

    def full(*args, **kw):
        if kw.get("prefix") == ".restart.":
            raise OSError("synthetic disk full")
        return real(*args, **kw)
    monkeypatch.setattr(recover.tempfile, "mkstemp", full)
    assert recover.recover() == 1
    report = _report(recover)
    assert report["result"] == "restart_request_failed" and report["completed"] == path
    assert any("再起動" in text for text in notices)
    state = json.loads(open(recover.STATE_PATH).read())
    assert state["stages"] == [] and not state.get("applying")   # bookkeeping kept
    assert not (tmp_path / "data" / "standalone-restart.request").exists()


def test_lost_gateway_restart_request_is_reported_not_success(monkeypatch, tmp_path):
    recover, _, notices = _world(monkeypatch, tmp_path, {}, path="resumed")
    state = json.loads(open(recover.STATE_PATH).read())
    state["applying"]["plugin_changed"] = True
    open(recover.STATE_PATH, "w").write(json.dumps(state))
    monkeypatch.setattr(recover, "request_gateway_restart", lambda *a: False)
    assert recover.recover() == 1
    assert _report(recover)["result"] == "restart_request_failed"
    assert any("再起動" in text for text in notices)


def test_restart_request_success_keeps_the_completed_result(monkeypatch, tmp_path):
    recover, _, _ = _world(monkeypatch, tmp_path, {"runtime_mode": "standalone"},
                           path="resumed_done")
    monkeypatch.setattr(recover, "_standalone_status", lambda: {"generation": "a" * 32})
    assert recover.recover() == 0
    assert _report(recover)["result"] == "resumed_done"
    request = json.loads((tmp_path / "data" / "standalone-restart.request").read_text())
    assert request["generation"] == "a" * 32
