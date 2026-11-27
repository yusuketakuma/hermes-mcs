"""Post-install verification is bounded, read-only and leaves an agent repair brief."""
import json
from pathlib import Path

import pytest

import mcs_cli
import mcs_setup
import postinstall_watch


def _runner(results):
    calls = []

    def run(argv, timeout):
        calls.append((list(argv), timeout))
        rc, output = results[len(calls) - 1]
        return {"rc": rc, "output": output}

    return run, calls


def test_once_success_writes_bounded_status(tmp_path):
    runner, calls = _runner([(0, "設定確認: 正常"), (0, "動作診断: 正常")])
    messages = []
    rc = postinstall_watch.run(home=str(tmp_path), repo="/synthetic/repo", once=True,
                               runner=runner, sleep=lambda _: None, out=messages.append)
    assert rc == 0
    assert [call[0][2] for call in calls] == ["check", "doctor"]
    assert "--probe" in calls[1][0] and "services" in calls[1][0] and "data" in calls[1][0]
    status = json.loads((tmp_path / "data/postinstall-watch/status.json").read_text())
    assert status["healthy"] is True and status["healthy_streak"] == 1
    assert "正常" in messages[-1]
    assert not (tmp_path / "data/postinstall-watch/agent-repair-brief.md").exists()


def test_failure_brief_excludes_raw_output_and_is_retired_on_recovery(tmp_path):
    runner, _calls = _runner([(1, "check failed synthetic"), (0, "doctor ok")])
    rc = postinstall_watch.run(home=str(tmp_path), repo="/synthetic/repo", once=True,
                               runner=runner, sleep=lambda _: None, out=lambda _: None)
    assert rc == 1
    brief = (tmp_path / "data/postinstall-watch/agent-repair-brief.md").read_text()
    assert "command_failed" in brief
    assert "check failed synthetic" not in brief
    assert "doctor ok" not in brief
    assert postinstall_watch.REPOSITORY_URL in brief
    assert "セッションを終了せず" in brief
    status = json.loads((tmp_path / "data/postinstall-watch/status.json").read_text())
    assert status["healthy"] is False
    assert oct((tmp_path / "data/postinstall-watch/agent-repair-brief.md").stat().st_mode & 0o777) == "0o600"

    runner, _ = _runner([(0, ""), (0, "")])
    assert postinstall_watch.run(home=str(tmp_path), repo="/synthetic", once=True, runner=runner) == 0
    assert not (tmp_path / "data/postinstall-watch/agent-repair-brief.md").exists()


@pytest.mark.parametrize("duration,interval", [(float("nan"), 1), (1, float("nan"))])
def test_nonfinite_budget_rejected(tmp_path, duration, interval):
    assert postinstall_watch.run(home=str(tmp_path), repo="/synthetic", duration=duration,
                                 interval=interval, runner=lambda *_: pytest.fail("probe ran")) == 2


def test_deadline_stops_probes_even_when_command_succeeds(tmp_path):
    now = [0.0]
    def runner(argv, timeout):
        assert argv[2] == "check" and timeout == 1
        now[0] += 2
        return {"rc": 0}

    assert postinstall_watch.run(home=str(tmp_path), repo="/synthetic", once=True, duration=1,
                                 clock=lambda: now[0], runner=runner) == 1
    assert "deadline" in (tmp_path / "data/postinstall-watch/agent-repair-brief.md").read_text()


@pytest.mark.parametrize("scope", [None, "services", "collection", "card_worker", "restore_delivery"])
def test_shared_doctor_requires_observations(scope):
    from support_diagnostics import SCHEMA_VERSION
    healthy = {"status": "healthy"}
    report = {"schema_version": SCHEMA_VERSION, "software": {"mode": "standalone"},
              "checks": dict.fromkeys(("services", "card_worker", "restore_delivery"), healthy),
              "collection": healthy}
    if scope == "collection":
        report["collection"] = {"status": "unknown"}
    elif scope:
        report["checks"][scope] = {"status": "warning"}
    assert postinstall_watch._doctor_result(json.dumps(report).encode())["rc"] == int(scope is not None)
    assert postinstall_watch._doctor_result(b"invalid")["rc"] == 1


def test_default_runner_does_not_accept_unverified_success(tmp_path, monkeypatch):
    import subprocess
    def probe(argv, *, stdout, stderr, timeout):
        assert stderr == subprocess.DEVNULL
        if argv[2] == "check":
            assert stdout == subprocess.DEVNULL
            return subprocess.CompletedProcess(argv, 0)
        assert "--share" in argv and stdout == subprocess.PIPE
        return subprocess.CompletedProcess(argv, 0, stdout=b"synthetic unverified report")
    monkeypatch.setattr(subprocess, "run", probe)
    assert postinstall_watch.run(home=str(tmp_path), repo="/synthetic", once=True) == 1
    brief = (tmp_path / "data/postinstall-watch/agent-repair-brief.md").read_text()
    assert "diagnosis_unverified" in brief and "synthetic unverified report" not in brief


def test_unverified_output_directory_leaves_external_directory_untouched(tmp_path):
    root = tmp_path / "install"
    data = root / "data"
    data.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir(mode=0o755)
    before = outside.stat().st_mode
    (data / "postinstall-watch").symlink_to(outside, target_is_directory=True)
    runner, _ = _runner([(0, ""), (0, "")])
    assert postinstall_watch.run(home=str(root), repo="/synthetic", once=True, runner=runner) == 1
    assert outside.stat().st_mode == before and list(outside.iterdir()) == []


def test_continuous_requires_configured_success_streak(tmp_path):
    runner, calls = _runner([
        (0, "ok1"), (0, "ok2"),
        (0, "ok3"), (0, "ok4"),
    ])
    sleeps = []
    times = iter([0.0, 0.0])
    rc = postinstall_watch.run(home=str(tmp_path), repo="/synthetic/repo", duration=300.0,
                               interval=5.0, stable=2, runner=runner,
                               sleep=sleeps.append, clock=lambda: next(times, 0.0), out=lambda _: None)
    assert rc == 0
    assert len(calls) == 4
    assert sleeps == [5.0]
    status = json.loads((tmp_path / "data/postinstall-watch/status.json").read_text())
    assert status["healthy_streak"] == 2


def test_cli_rejects_unbounded_watch(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    assert mcs_setup.main(["postinstall", "--duration", "0"]) == 2


def test_cli_wires_postinstall_command(tmp_path, monkeypatch):
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(postinstall_watch, "run", fake_run)
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    assert mcs_setup.main(["postinstall", "--once", "--stable", "1"]) == 0
    assert seen["home"] == str(tmp_path)
    assert seen["once"] is True and seen["stable"] == 1
    assert (Path(seen["repo"]) / "mcs" / "ops" / "mcs_setup.py").is_file()


def test_public_setup_route_reaches_postinstall(monkeypatch):
    monkeypatch.setenv("MCS_LIFECYCLE_PINNED", "1")
    seen = []
    monkeypatch.setattr(postinstall_watch, "run", lambda **kwargs: seen.append(kwargs) or 0)
    assert mcs_cli.main(["setup", "postinstall", "--once"]) == 0
    assert seen and seen[0]["once"] is True


def test_installer_accepted_private_root_symlink_is_used_but_child_symlink_stays_rejected(tmp_path):
    target = tmp_path / "target-home"
    target.mkdir(mode=0o700)
    alias = tmp_path / "alias-home"
    alias.symlink_to(target, target_is_directory=True)
    runner, _ = _runner([(0, ""), (0, "")])
    assert postinstall_watch.run(home=str(alias), repo="/synthetic", once=True, runner=runner) == 0
    assert json.loads((target / "data/postinstall-watch/status.json").read_text())["healthy"] is True

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (target / "data/postinstall-watch").rename(target / "data/postinstall-watch.saved")
    (target / "data/postinstall-watch").symlink_to(outside, target_is_directory=True)
    def always_ok(*_args):
        return {"rc": 0}
    assert postinstall_watch.run(home=str(alias), repo="/synthetic", once=True, runner=always_ok) == 1
    assert list(outside.iterdir()) == []


def test_deadline_before_required_streak_publishes_failed_status_and_brief(tmp_path):
    now = [0.0]
    runner, _ = _runner([(0, ""), (0, "")])

    def clock():
        return now[0]

    def sleep(seconds):
        now[0] += seconds

    assert postinstall_watch.run(home=str(tmp_path), repo="/synthetic", duration=5, interval=5,
                                 stable=2, runner=runner, sleep=sleep, clock=clock,
                                 out=lambda _: None) == 1
    status = json.loads((tmp_path / "data/postinstall-watch/status.json").read_text())
    assert status["healthy"] is False and status["complete"] is False and status["reason"] == "deadline"
    assert status["healthy_streak"] == 1
    assert "deadline" in (tmp_path / "data/postinstall-watch/agent-repair-brief.md").read_text()


def test_probe_check_uses_offline_mode():
    commands = dict(postinstall_watch._probe_commands(Path("/synthetic/repo")))
    assert commands["check"][-1] == "--offline"
