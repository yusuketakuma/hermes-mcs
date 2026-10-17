"""Human lifecycle guides use synthetic plans and never contact services."""
import json
import plistlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

import mcs_cli
from ops_testkit import _load
from test_mcs_upgrade import _launcher


def _terminal(monkeypatch, answers):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    iterator = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _: next(iterator))


@pytest.mark.parametrize("answer,expected", [("", 1), ("0", 1), ("2", 3)])
def test_update_requires_explicit_execution(monkeypatch, answer, expected):
    launcher = _launcher()
    _terminal(monkeypatch, [answer])
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"route": "noop", "blockers": []} if len(calls) == 3 else {"route": "reinstall", "install_args": []}))

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.interactive_update(["python", "updater", "plan", "--tag", "v1.2.0"], {}, "/synthetic") == 0
    assert len(calls) == expected
    if expected == 3:
        assert calls[1] == ["python", "updater", "apply", "--tag", "v1.2.0", "--reinstall"]


@pytest.mark.parametrize("report", [{"route": "blocked"}, {"route": "unexpected"}, []])
def test_update_invalid_or_blocked_plan_never_applies(monkeypatch, report):
    launcher = _launcher()
    calls = []
    monkeypatch.setattr(launcher.subprocess, "run", lambda cmd, **kwargs:
                        calls.append(cmd) or SimpleNamespace(returncode=0, stdout=json.dumps(report)))
    assert launcher.interactive_update(["python", "updater", "plan"], {}, "/synthetic") == 1
    assert len(calls) == 1


def test_update_guide_requires_terminal_before_git(monkeypatch):
    launcher = _launcher()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(launcher, "_git", lambda *a, **kw: pytest.fail("no git without terminal"))
    assert launcher.main(["guide"]) == 2


def test_update_eof_is_cancellation(monkeypatch):
    launcher = _launcher()
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError()))
    assert launcher._choose("test", [("1", "recommended")]) == "0"


def _recovery(monkeypatch, tmp_path, state):
    mod = _load()
    _terminal(monkeypatch, [])
    monkeypatch.setattr(mod, "_load_state", lambda: state)
    monkeypatch.setattr(mod, "_awaiting_consent", lambda: None)
    monkeypatch.setattr(mod, "MARKER_PATH", str(tmp_path / "missing"))
    return mod


@pytest.mark.parametrize("state,code", [({}, 0), ({"_corrupt": True}, 2)])
def test_recovery_diagnosis_never_runs_when_not_ready(monkeypatch, tmp_path, state, code):
    mod = _recovery(monkeypatch, tmp_path, state)
    monkeypatch.setattr(mod, "recover", lambda: pytest.fail("must not recover"))
    assert mod.interactive_recovery() == code


@pytest.mark.parametrize("answer", ["", "0", "1"])
def test_recovery_default_does_not_execute(monkeypatch, tmp_path, answer):
    mod = _recovery(monkeypatch, tmp_path, {"applying": {"tag": "v1.2.0"}})
    _terminal(monkeypatch, [answer])
    monkeypatch.setattr(mod, "recover", lambda: pytest.fail("must not recover"))
    assert mod.interactive_recovery() == 0


def test_recovery_zero_exit_with_pending_record_is_not_success(monkeypatch, tmp_path, capsys):
    mod = _recovery(monkeypatch, tmp_path, {"applying": {"tag": "v1.2.0"}})
    _terminal(monkeypatch, ["invalid", "2"])
    monkeypatch.setattr(mod, "recover", lambda: 0)
    assert mod.interactive_recovery() == 1
    assert "まだ更新記録" in capsys.readouterr().out


def test_recovery_checks_consent_after_execution(monkeypatch, tmp_path):
    state = {"applying": {"tag": "v1.2.0"}}
    mod = _recovery(monkeypatch, tmp_path, state)
    _terminal(monkeypatch, ["2"])

    def recover():
        state["restore_consent"] = {"report_id": "synthetic"}
        return 0

    monkeypatch.setattr(mod, "recover", recover)
    assert mod.interactive_recovery() == 1


def test_unified_update_without_arguments_routes_to_guide(monkeypatch):
    monkeypatch.setenv("MCS_LIFECYCLE_PINNED", "1")
    calls = []
    monkeypatch.setattr(mcs_cli.os, "execv", lambda exe, argv: calls.append(argv))
    mcs_cli.main(["update"])
    assert calls[0][-1] == "guide"


def test_unified_recovery_bypasses_broken_runtime_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(mcs_cli.Path, "home", lambda: tmp_path)
    tool = tmp_path / ".mcs-recovery" / "mcs_recover.py"
    tool.parent.mkdir()
    tool.write_text("# synthetic\nINTERACTIVE_GUIDE_VERSION = 1\n")
    plist = tmp_path / "Library/LaunchAgents/org.mcs.recovery.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"ProgramArguments": [sys.executable, str(tool), "--if-stale"]}))
    monkeypatch.setattr(mcs_cli, "load_config", lambda: pytest.fail("do not load broken runtime config"))
    calls = []
    monkeypatch.setattr(mcs_cli.os, "execv", lambda exe, argv: calls.append(argv))
    mcs_cli.main(["recover"])
    assert calls == [[sys.executable, str(tool), "--interactive"]]


def test_guide_fetch_failure_is_japanese_and_nonfatal(monkeypatch, capsys):
    launcher = _launcher()
    _terminal(monkeypatch, [""])
    monkeypatch.setattr(launcher, "_git", lambda *a, **kw: (_ for _ in ()).throw(SystemExit("synthetic failure")))
    assert launcher.main(["guide"]) == 1
    assert "更新の準備に失敗" in capsys.readouterr().out


def test_recovery_help_never_executes(monkeypatch):
    mod = _load()
    monkeypatch.setattr(sys, "argv", ["mcs_recover.py", "--help"])
    monkeypatch.setattr(mod, "recover", lambda **kwargs: pytest.fail("must not recover"))
    assert mod.main() == 0


def test_recovery_watchdog_remains_noninteractive(monkeypatch):
    mod = _load()
    monkeypatch.setattr(sys, "argv", ["mcs_recover.py", "--if-stale"])
    calls = []
    monkeypatch.setattr(mod, "recover", lambda **kwargs: calls.append(kwargs) or 0)
    monkeypatch.setattr(mod, "interactive_recovery", lambda: pytest.fail("no dialogue in watchdog"))
    assert mod.main() == 0
    assert calls == [{"if_stale": True}]


def test_old_recovery_tool_is_never_executed(monkeypatch, tmp_path):
    monkeypatch.setattr(mcs_cli.Path, "home", lambda: tmp_path)
    tool = tmp_path / ".mcs-recovery" / "mcs_recover.py"
    tool.parent.mkdir()
    tool.write_text("# synthetic old version: unknown arguments execute recovery\n")
    monkeypatch.setattr(mcs_cli.os, "execv", lambda *args: pytest.fail("old tool must not run"))
    assert mcs_cli.main(["recover"]) == 1


def test_recovery_success_is_reported_only_after_journal_clears(monkeypatch, tmp_path, capsys):
    state = {"applying": {"tag": "v1.2.0"}}
    mod = _recovery(monkeypatch, tmp_path, state)
    _terminal(monkeypatch, ["2"])

    def recover():
        state.clear()
        return 0

    monkeypatch.setattr(mod, "recover", recover)
    assert mod.interactive_recovery() == 0
    assert "処理が終了しました" in capsys.readouterr().out


def test_recovery_guide_requires_terminal_before_reading_state(monkeypatch):
    mod = _load()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(mod, "_load_state", lambda: pytest.fail("no read without terminal"))
    assert mod.interactive_recovery() == 2


def test_update_shows_required_upgrade_steps_before_confirmation(monkeypatch, capsys):
    launcher = _launcher()
    _terminal(monkeypatch, [""])
    report = {"route": "apply", "notes": [{"version": "v1.2.0", "notes": "合成の更新後手順"}]}
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(report)))
    assert launcher.interactive_update(["python", "updater", "plan"], {}, "/synthetic") == 0
    assert "合成の更新後手順" in capsys.readouterr().out


@pytest.mark.parametrize("install_args", [None, ["--unknown"]])
def test_reinstall_missing_or_invalid_previous_settings_stops(monkeypatch, install_args):
    launcher = _launcher()
    calls = []
    report = {"route": "reinstall", "install_args": install_args}
    monkeypatch.setattr(launcher.subprocess, "run", lambda cmd, **kwargs:
                        calls.append(cmd) or SimpleNamespace(returncode=0, stdout=json.dumps(report)))
    assert launcher.interactive_update(["python", "updater", "plan"], {}, "/synthetic") == 1
    assert len(calls) == 1


def test_reinstall_preserves_previous_optouts(monkeypatch):
    launcher = _launcher()
    _terminal(monkeypatch, ["2"])
    calls = []
    report = {"route": "reinstall", "install_args": ["--no-llm", "--no-plugin"]}
    monkeypatch.setattr(launcher.subprocess, "run", lambda cmd, **kwargs:
                        calls.append(cmd) or SimpleNamespace(returncode=0, stdout=json.dumps({"route": "noop", "blockers": []} if len(calls) == 3 else report)))
    assert launcher.interactive_update(["python", "updater", "plan"], {}, "/synthetic") == 0
    assert calls[1][-3:] == ["--reinstall", "--install-arg=--no-llm", "--install-arg=--no-plugin"]


@pytest.mark.parametrize("error", [KeyboardInterrupt, OSError])
def test_recovery_interruption_reports_incomplete_in_japanese(monkeypatch, tmp_path, capsys, error):
    mod = _recovery(monkeypatch, tmp_path, {"applying": {"tag": "v1.2.0"}})
    _terminal(monkeypatch, ["2"])
    monkeypatch.setattr(mod, "recover", lambda: (_ for _ in ()).throw(error()))
    assert mod.interactive_recovery() == 1
    assert "途中まで変更された可能性" in capsys.readouterr().out


@pytest.mark.parametrize("after_apply", [False, True])
def test_guide_plan_timeout_does_not_claim_completion(monkeypatch, capsys, after_apply):
    launcher = _launcher()
    _terminal(monkeypatch, ["2"])
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[2] == "plan":
            assert kwargs["timeout"] == 600
            if not after_apply or len(calls) == 3:
                raise subprocess.TimeoutExpired(cmd, 600)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"route": "apply"}))

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.interactive_update(["python", "updater", "plan"], {}, "/synthetic") == 1
    assert len(calls) == (3 if after_apply else 1)
    assert ("完了を確認できません" if after_apply else "更新は開始していません") in capsys.readouterr().out


@pytest.mark.parametrize("report", [{"route": "apply"},
                                   {"route": "noop", "blockers": ["update_in_progress_or_interrupted"]}])
def test_zero_apply_exit_requires_verified_completion(monkeypatch, report, capsys):
    launcher = _launcher()
    _terminal(monkeypatch, ["2"])
    calls = []
    monkeypatch.setattr(launcher.subprocess, "run", lambda cmd, **kw:
                        calls.append(cmd) or SimpleNamespace(returncode=0, stdout=json.dumps(
                            report if len(calls) == 3 else {"route": "apply"})))
    assert launcher.interactive_update(["python", "updater", "plan"], {}, "/synthetic") == 1
    assert "完了を確認できません" in capsys.readouterr().out


def test_initial_noop_with_blockers_is_not_complete(monkeypatch, capsys):
    launcher = _launcher()
    report = {"route": "noop", "blockers": ["update_in_progress_or_interrupted"]}
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=json.dumps(report)))
    assert launcher.interactive_update(["python", "updater", "plan"], {}, "/synthetic") == 1
    assert "更新が進行中、または中断" in capsys.readouterr().out


def test_recovery_runtime_repair_stops_before_reinstall(monkeypatch, capsys):
    launcher = _launcher()
    report = {"route": "reinstall", "install_args": [], "reinstall_requires_recovery_repair": True}
    calls = []
    monkeypatch.setattr(launcher.subprocess, "run", lambda cmd, **kw:
                        calls.append(cmd) or SimpleNamespace(returncode=0, stdout=json.dumps(report)))
    assert launcher.interactive_update(["python", "updater", "plan"], {}, "/synthetic") == 1
    assert len(calls) == 1
    assert "復旧用 Python 修復手順" in capsys.readouterr().out


@pytest.mark.parametrize("answer,approved,expected,calls_expected", [
    ("", False, 0, 0), ("2", False, 1, 1), ("2", True, 0, 1)])
def test_held_recovery_delegates_approval_to_existing_recover(
        monkeypatch, tmp_path, answer, approved, expected, calls_expected):
    state = {"restore_consent": {"report_id": "synthetic"}}
    mod = _recovery(monkeypatch, tmp_path, state)
    _terminal(monkeypatch, [answer])
    calls = []

    def recover():
        calls.append(True)
        if approved:
            state.clear()
        return 0

    monkeypatch.setattr(mod, "recover", recover)
    assert mod.interactive_recovery() == expected
    assert len(calls) == calls_expected
