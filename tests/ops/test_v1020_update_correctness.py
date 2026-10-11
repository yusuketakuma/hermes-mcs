"""Synthetic regressions for update rollback, delayed notices and restart scope."""
from pathlib import Path
import subprocess

import pytest

from ops_testkit import _git
from test_mcs_update import _apply_env, updater as updater


@pytest.mark.parametrize("stop_fails", [False, True])
def test_post_merge_bail_stops_restarted_writers_before_rollback(updater, monkeypatch, stop_fails):
    repo = _apply_env(updater, monkeypatch)
    _git(repo, "reset", "-q", "--hard", "v1.0.0")
    previous = updater._head_sha()
    target = _git(repo, "rev-parse", "v1.1.0^{commit}").stdout.strip()
    monkeypatch.setattr(updater, "remote_tag_sha", lambda tag: target)
    monkeypatch.setattr(updater, "_reconcile_membership", lambda snapshot: [])
    events = []

    def quiesce():
        events.append("quiesce")
        updater._write_marker()
        if stop_fails and events.count("quiesce") == 2:
            raise updater.UpdateError("drainer_stop_failed: synthetic")
        return []

    def restarted_child(expected):
        events.append("child_restart")
        updater._remove_marker()
        raise updater.UpdateError("post_merge_failed: synthetic")

    rollback = updater._rollback_tree

    def restore(entry, on_hold):
        events.append("rollback")
        assert Path(updater.MARKER_PATH).exists()
        rollback(entry, on_hold)

    monkeypatch.setattr(updater, "quiesce", quiesce)
    monkeypatch.setattr(updater, "_run_post_merge", restarted_child)
    monkeypatch.setattr(updater, "_rollback_tree", restore)
    monkeypatch.setattr(updater, "restart_agents", lambda: events.append("restart") or [])
    assert updater.apply("v1.1.0", target, "synthetic-command") == 1
    assert events[:3] == ["quiesce", "child_restart", "quiesce"]
    state = updater.load_state()
    if stop_fails:
        assert "rollback" not in events and "restart" not in events
        assert updater._head_sha() == target
        assert state["applying"] and state["stages"]
        assert Path(updater.MARKER_PATH).exists()
    else:
        assert events[3:] == ["rollback", "restart"]
        assert updater._head_sha() == previous
        assert not state["applying"] and not state["stages"]
        assert not Path(updater.MARKER_PATH).exists()


def test_deferred_update_notice_refetches_release_context(updater, monkeypatch):
    monkeypatch.setattr(updater, "load_config", lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(updater, "detect_latest", lambda pre: ("v1.1.0", "a" * 40))
    monkeypatch.setattr(updater, "current_version", lambda: ("v1.0.0", "b" * 40))
    monkeypatch.setattr(updater, "scan_pending_approvals", lambda state: ([], []))
    monkeypatch.setattr(updater, "_git", lambda args, **kw:
                        subprocess.CompletedProcess(args, int(args[0] == "merge-base"), "", ""))
    fetched, notices = [], []
    monkeypatch.setattr(updater, "fetch_notes", lambda tag: fetched.append(tag) or "synthetic release notes")
    monkeypatch.setattr(updater, "impact_summary", lambda current, tag: ["synthetic restart required"])
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text, **kw:
                        notices.append(text) or len(notices) > 1)
    assert updater.cmd_check() == 0
    assert updater.load_state()["latest_tag"] == "v1.1.0"
    assert updater.load_state().get("notified_at") != "v1.1.0"
    assert updater.cmd_check() == 0
    assert fetched == ["v1.1.0", "v1.1.0"]
    assert "synthetic release notes" in notices[1]
    assert "synthetic restart required" in notices[1]
    assert updater.load_state()["notified_at"] == "v1.1.0"
    assert updater.cmd_check() == 0
    assert len(fetched) == 2 and len(notices) == 2


def test_core_change_requires_gateway_restart_and_notice(updater, monkeypatch):
    def diff(args):
        if "--name-status" in args:
            return "M\tmcs/notify/notify_cards.py\n"
        return "mcs/notify/notify_cards.py\0" if "mcs" in args else ""

    monkeypatch.setattr(updater, "_git_out", diff)
    monkeypatch.setattr(updater, "load_config",
                        lambda: {"notify": {"interactive": "lineworks"}})
    assert updater._plugin_changed("v1.1.0", {})
    impact = updater.impact_summary("synthetic-before", "v1.1.0")
    assert any("gateway restart" in line for line in impact)
    assert any("LINE WORKS" in line and "再起動" in line for line in impact)


def test_lineworks_impact_only_when_the_adapter_is_in_use(updater, monkeypatch):
    monkeypatch.setattr(updater, "_git_out", lambda args: "M\tmcs/notify/notify_cards.py\n")
    for cfg in ({}, {"notify": {"interactive": "slack"}}, {"notify": "lineworks"}):
        monkeypatch.setattr(updater, "load_config", lambda cfg=cfg: cfg)
        impact = updater.impact_summary("synthetic-before", "v1.1.0")
        assert any("gateway restart" in line for line in impact)
        assert not any("LINE WORKS" in line for line in impact)


def test_checkout_ahead_of_latest_tag_does_not_refetch_daily(updater, monkeypatch):
    monkeypatch.setattr(updater, "load_config", lambda: {"update": {"mode": "notify"}})
    monkeypatch.setattr(updater, "detect_latest", lambda pre: ("v1.1.0", "a" * 40))
    monkeypatch.setattr(updater, "current_version", lambda: ("v1.1.0-3-gsynthetic", "b" * 40))
    monkeypatch.setattr(updater, "scan_pending_approvals", lambda state: ([], []))
    calls = []
    monkeypatch.setattr(updater, "_git", lambda args, **kw: calls.append(args[0])
                        or subprocess.CompletedProcess(args, 0, "", ""))   # ancestor of HEAD
    fetched = []
    monkeypatch.setattr(updater, "fetch_notes", lambda tag: fetched.append(tag) or "notes")
    monkeypatch.setattr(updater, "impact_summary", lambda current, tag: [])
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text, **kw: pytest.fail("no notice"))
    assert updater.cmd_check() == 0         # first sighting of the tag fetches once
    assert fetched == ["v1.1.0"] and calls.count("fetch") == 1
    for _ in range(2):
        assert updater.cmd_check() == 0
    assert fetched == ["v1.1.0"] and calls.count("fetch") == 1
    assert updater.load_state().get("notified_at") is None


def test_invalid_runtime_mode_is_reported_not_raised(updater, tmp_path, monkeypatch):
    from ops_testkit import _make_repo
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    monkeypatch.setattr(updater, "load_config", lambda: {"runtime_mode": "synthetic-invalid"})
    monkeypatch.setattr(updater, "precheck_local", lambda cfg: [])
    result = updater.plan("v1.1.0")
    assert result["route"] == "blocked"
    assert any(b.startswith("plan_failed: runtime_mode_invalid") for b in result["blockers"])
    monkeypatch.setattr(updater, "_git_out", lambda args: "M\tmcs/notify/notify_cards.py\n")
    with pytest.raises(ValueError):
        updater.impact_summary("synthetic-before", "v1.1.0")
    monkeypatch.setattr(updater, "load_config", lambda: {
        "runtime_mode": "synthetic-invalid", "update": {"mode": "notify"}})
    monkeypatch.setattr(updater, "detect_latest", lambda pre: ("v1.1.0", "a" * 40))
    monkeypatch.setattr(updater, "current_version", lambda: ("v1.0.0", "b" * 40))
    monkeypatch.setattr(updater, "scan_pending_approvals", lambda state: ([], []))
    monkeypatch.setattr(updater, "_git", lambda args, **kw: subprocess.CompletedProcess(args, 1, "", ""))
    monkeypatch.setattr(updater, "fetch_notes", lambda tag: "notes")
    notices = []
    monkeypatch.setattr(updater, "_enqueue_notice", lambda text, **kw: notices.append(text) or True)
    assert updater.cmd_check() == 0 and notices
