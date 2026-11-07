"""health_watch in runtime_mode=standalone reads the host's status file (synthetic)."""
import json
import os
from pathlib import Path

import health_watch

CFG = {"runtime_mode": "standalone"}
NOW = 1_800_000_000.0


def _write(home, name, value):
    path = Path(home) / "data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def _status(home, **overrides):
    value = {"pid": os.getpid(), "updated_at": NOW - 5, "started_at": NOW - 7200,
             "update_in_progress": False, "update_marker": None, "children": {},
             "exits": {"mcs_check": {"last_exit": 0, "failures": 0, "last_ok_at": NOW - 300}},
             "connector": {"state": "running", "failures": 0, "failing_since": None,
                           "last_error": None, "retry_at": None}}
    value.update(overrides)
    return _write(home, "standalone-status.json", value)


def _health(home, overall="ok"):
    return _write(home, "health.json", {"overall": overall, "at": NOW - 60,
                                        "unread_at": NOW - 60, "state_reasons": []})


def test_healthy_host_adds_nothing(tmp_path):
    _status(tmp_path)
    assert health_watch.standalone_host_reasons(str(tmp_path), NOW, CFG) == ("ok", [], {})


def test_dead_or_silent_host_is_failed(tmp_path):
    assert health_watch.standalone_host_reasons(str(tmp_path), NOW, CFG)[1] == ["standalone_host_down"]
    _status(tmp_path, updated_at=NOW - 600)
    severity, reasons, details = health_watch.standalone_host_reasons(str(tmp_path), NOW, CFG)
    assert (severity, reasons) == ("failed", ["standalone_host_down"])
    assert details["standalone_host_down"] == "最終更新 0.2時間前"
    _status(tmp_path, pid=2 ** 22 + 12345)     # no such process
    assert health_watch.standalone_host_reasons(str(tmp_path), NOW, CFG)[1] == ["standalone_host_down"]


def test_crash_loop_connector_update_and_check_stall_are_reported(tmp_path):
    _status(tmp_path,
            exits={"extract-0": {"last_exit": 1, "failures": 4},
                   "llamacpp_restart_if_idle": {"last_exit": 1, "failures": 9},
                   "mcs_check": {"last_exit": 0, "failures": 0, "last_ok_at": NOW - 4000}},
            connector={"state": "waiting", "failures": 6, "failing_since": NOW - 3600,
                       "last_error": "slack_identity_unreachable", "retry_at": NOW + 60},
            update_in_progress=True,
            update_marker={"since": NOW - 3 * 3600, "state": "journal_pending"})
    severity, reasons, details = health_watch.standalone_host_reasons(str(tmp_path), NOW, CFG)
    assert severity == "degraded"
    assert reasons == ["standalone_child_failing", "standalone_connector_down",
                       "standalone_update_stuck", "standalone_check_stalled"]
    # job codes and numbers only; helper jobs that may exit non-zero are not watched
    assert details["standalone_child_failing"] == "extract-0 4回連続"
    assert details["standalone_connector_down"] == "1.0時間 slack_identity_unreachable"
    assert details["standalone_update_stuck"] == "3.0時間 journal_pending"


def test_evaluate_merges_host_reasons_and_uses_standalone_hints(tmp_path):
    _health(tmp_path)
    _status(tmp_path, exits={"cmd": {"last_exit": 1, "failures": 3},
                             "mcs_check": {"last_exit": 0, "failures": 0, "last_ok_at": NOW - 60}})
    report = health_watch.evaluate(home=str(tmp_path), now=NOW, cfg=CFG)
    assert report["status"] == "degraded" and report["alert"] is True
    assert report["state_reasons"] == ["standalone_child_failing"]
    text = "\n".join(health_watch._alert_lines(report))
    assert report["host_details"]["standalone_child_failing"] == "cmd 3回連続"
    assert "cmd 3回連続" not in text
    assert "hostの子プロセスが異常終了を繰り返しています" in text
    assert "mcs_standalone status" in text
    assert "コード: degraded standalone_child_failing" in text
    rows = health_watch._reason_rows({**report, "oldest_age_s": {}},
                                     ["extract_backlog_stalled", "card_delivery_stalled"])
    assert "launchctl list" not in "\n".join(rows) and "hermes gateway" not in "\n".join(rows)
    assert "ai.mcs.standalone" in rows[3]


def test_stale_health_gets_the_current_host_cause(tmp_path):
    _write(tmp_path, "health.json", {"overall": "ok", "at": NOW - 7200, "state_reasons": ["run_failed"]})
    report = health_watch.evaluate(home=str(tmp_path), now=NOW, cfg=CFG)
    assert report["status"] == "stale"
    assert report["state_reasons"] == ["standalone_host_down"]
    assert report["recorded_state_reasons"] == ["run_failed"]


def test_hermes_mode_is_unchanged(tmp_path):
    _health(tmp_path)
    report = health_watch.evaluate(home=str(tmp_path), now=NOW, cfg={"runtime_mode": "hermes"})
    assert report["status"] == "ok" and "runtime_mode" not in report
    rows = health_watch._reason_rows({"oldest_age_s": {}}, ["card_delivery_stalled"])
    assert "hermes gateway restart" in rows[1]


def test_host_dying_during_a_stale_episode_alerts_again(tmp_path):
    _write(tmp_path, "health.json", {"overall": "ok", "at": NOW - 7200, "state_reasons": []})
    _status(tmp_path)
    first = health_watch.evaluate(home=str(tmp_path), now=NOW, cfg=CFG)
    assert first["status"] == "stale" and first["alert"] is True
    assert health_watch.evaluate(home=str(tmp_path), now=NOW + 60, cfg=CFG)["alert"] is False
    _status(tmp_path, updated_at=NOW - 600)          # the host stops reporting
    report = health_watch.evaluate(home=str(tmp_path), now=NOW + 120, cfg=CFG)
    assert report["state_reasons"] == ["standalone_host_down"] and report["alert"] is True
    assert health_watch.evaluate(home=str(tmp_path), now=NOW + 180, cfg=CFG)["alert"] is False
