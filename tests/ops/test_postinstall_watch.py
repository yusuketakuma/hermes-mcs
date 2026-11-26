"""Post-install verification is bounded, read-only and leaves an agent repair brief."""
import json
from pathlib import Path

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


def test_once_failure_writes_agent_brief_with_only_failed_output(tmp_path):
    runner, _calls = _runner([(1, "check failed synthetic"), (0, "doctor ok")])
    rc = postinstall_watch.run(home=str(tmp_path), repo="/synthetic/repo", once=True,
                               runner=runner, sleep=lambda _: None, out=lambda _: None)
    assert rc == 1
    brief = (tmp_path / "data/postinstall-watch/agent-repair-brief.md").read_text()
    assert "check failed synthetic" in brief
    assert "doctor ok" not in brief
    assert postinstall_watch.REPOSITORY_URL in brief
    assert "セッションを終了せず" in brief
    status = json.loads((tmp_path / "data/postinstall-watch/status.json").read_text())
    assert status["healthy"] is False
    assert oct((tmp_path / "data/postinstall-watch/agent-repair-brief.md").stat().st_mode & 0o777) == "0o600"


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
