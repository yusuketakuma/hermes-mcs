"""runtime_mode=standalone: config grants, launchd schedules and services
without Hermes; Hermes mode keeps its existing surface."""
import lifecycle_io
import mcs_config
import scheduled_jobs
import runtime_inspection
import json
import os
import plistlib
import time
from types import SimpleNamespace
import shlex
import subprocess
from pathlib import Path

import pytest

import mcs_runtime
import mcs_setup
import mcs_util
from test_mcs_setup import _services_env
from mcs_standalone import runtime, service


def _process_gone(pid, wait=10.0):
    """Poll until ``pid`` no longer runs. A zombie (state Z in /proc) counts
    as gone: a container's pid 1 may never reap an orphan. Without /proc
    (macOS) signal 0 is the probe."""
    deadline = time.monotonic() + wait
    while True:
        if os.path.isdir("/proc/self"):
            try:
                with open(f"/proc/{pid}/stat") as handle:
                    gone = handle.read().rsplit(")", 1)[1].split()[0] == "Z"
            except FileNotFoundError:
                gone = True
        else:
            gone = not _signal0(pid)
        if gone:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _signal0(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def config(interactive="discord"):
    return {
        "runtime_mode": "standalone", "mcs_login_id": "synthetic",
        "notify_target": "discord:1000000000000000001",
        "notify": {"interactive": interactive, "discord": {
            "profile": "default", "application_id": "2000000000000000002",
            "guild_id": "3000000000000000003", "channel_id": "1000000000000000001",
            "allowed_user_ids": ["4000000000000000004"], "project_ids": [101]}},
    }


@pytest.mark.parametrize("schedule,want", [
    ("*/5 * * * *", [{"Minute": m} for m in range(0, 60, 5)]),
    ("7,37 * * * *", [{"Minute": 7}, {"Minute": 37}]),
    ("0 */6 * * *", [{"Hour": h, "Minute": 0} for h in (0, 6, 12, 18)]),
    ("10 5 * * *", [{"Hour": 5, "Minute": 10}]),
])
def test_cron_schedules_become_launchd_calendars(schedule, want):
    assert scheduled_jobs.calendar_entries(schedule) == want


def test_every_cron_job_has_a_calendar_and_unsupported_fields_fail():
    for _, schedule, _ in mcs_setup.CRON_JOBS:
        assert scheduled_jobs.calendar_entries(schedule)
    with pytest.raises(ValueError):
        scheduled_jobs.calendar_entries("0 0 1 * *")


def test_agent_labels_and_ownership_follow_the_runtime():
    assert mcs_setup._agent_labels({}) == mcs_setup.AGENT_LABELS
    labels = mcs_setup._agent_labels(config())
    cron = [mcs_setup._cron_label(s) for _, _, s in mcs_setup.CRON_JOBS]
    # the health watch stays outside the host it watches
    assert labels == [mcs_setup.STANDALONE_LABEL, mcs_setup.HEALTH_LABEL]
    # The same host owns collection even without a card connection.
    assert mcs_setup._agent_labels(config("off")) == labels
    for label in cron + [mcs_setup.STANDALONE_LABEL, "local.mcs-cmd"]:
        assert mcs_setup._owned_label(label)
    for label in ("ai.mcs.llamaserver", "org.mcs.recovery", "ai.hermes.gateway"):
        assert not mcs_setup._owned_label(label)


def test_standalone_config_validation():
    assert mcs_setup.validate_config(config()) == ([], [])
    cfg = config()
    del cfg["runtime_mode"]
    assert mcs_setup.validate_config(cfg)[0] == []        # Hermes keeps plugin grants
    for mutate, key in (
            (lambda d: d.pop("allowed_user_ids"), "allowed_user_ids"),
            (lambda d: d.update(project_ids=[]), "project_ids"),
            (lambda d: d.update(allowed_role_ids=["3000000000000000003"]), "allowed_role_ids"),
            (lambda d: d.update(allowed_chat_ids=[""]), "allowed_chat_ids")):
        cfg = config()
        mutate(cfg["notify"]["discord"])
        assert any(f"notify.discord.{key}" in e for e in mcs_setup.validate_config(cfg)[0])
    cfg = config()
    cfg["notify_target"] = "telegram:1"
    assert any("独立動作の通知先は" in e for e in mcs_setup.validate_config(cfg)[0])
    cfg["notify_target"] = "discord:#general"
    assert any(e.startswith("notify_target:") for e in mcs_setup.validate_config(cfg)[0])
    cfg["runtime_mode"] = "other"
    assert any(e.startswith("runtime_mode") for e in mcs_setup.validate_config(cfg)[0])


def _standalone_services(monkeypatch, tmp_path, cfg, **kw):
    calls, args = _services_env(monkeypatch, tmp_path, **kw)
    monkeypatch.setattr(mcs_setup, "load_config",
                        lambda path=None: mcs_util.load_config(path) if path else cfg)
    monkeypatch.setattr(mcs_runtime, "HOME", str(tmp_path))
    return calls, args


def test_standalone_services_use_launchd_not_hermes(monkeypatch, tmp_path):
    calls, args = _standalone_services(monkeypatch, tmp_path, config())
    assert mcs_setup.cmd_services(args) == 0
    venv = str(tmp_path / "venv" / "bin" / "python3")
    script = (tmp_path / "scripts" / "mcs_check.sh").read_text()
    assert venv in script and not (tmp_path / "data" / "scripts").exists()
    host = runtime.Runtime(tmp_path, config())
    argv = host.argv["mcs_check"]
    assert argv[0] == "/usr/bin/perl"  # the host retains the whole-job timeout
    assert argv[3:] == [str(scheduled_jobs.CRON_TIMEOUT_S), "/bin/bash",
                       str(tmp_path / "scripts" / "mcs_check.sh")]
    plist = plistlib.loads((tmp_path / "agents" / "ai.mcs.standalone.plist").read_bytes())
    assert plist["ProgramArguments"] == [venv, "-m", "mcs_standalone", "run", "--root", str(tmp_path)]
    assert plist["AbandonProcessGroup"] is True  # Chrome outlives a tick
    assert len(host.schedules) == 6
    assert not any(a[0] == "/x/hermes" for a in calls)
    manifest = json.loads((tmp_path / "data" / "service_manifest.json").read_text())
    assert manifest["cron"] == [] and [row["label"] for row in manifest["agents"]] == [
        mcs_setup.STANDALONE_LABEL, mcs_setup.HEALTH_LABEL]
    health = plistlib.loads((tmp_path / "agents" / "local.mcs-health.plist").read_bytes())
    assert health["ProgramArguments"][0] == venv and health["StartInterval"] == 300
    assert health["EnvironmentVariables"] == {"MCS_ROOT": str(tmp_path)}


@pytest.mark.parametrize("state,expected", [(3, 0), (0, 1), (1, 1)])
def test_linux_switch_stops_and_verifies_owned_systemd_service(monkeypatch, tmp_path, state, expected):
    unit = tmp_path / "mcs-standalone.service"
    unit.write_text("synthetic")
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup, "_standalone_service_path", lambda: str(unit))
    calls = []
    def systemctl(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=state if "is-active" in argv else 0)
    monkeypatch.setattr(mcs_setup, "_run", systemctl)
    assert mcs_setup._retire_agents({mcs_setup.STANDALONE_LABEL}, lambda msg: None, True) == 0
    assert not calls and unit.exists()
    assert mcs_setup._retire_agents({mcs_setup.STANDALONE_LABEL}, lambda msg: None, False) == expected
    assert calls[:2] == [
        ["systemctl", "--user", "disable", "--now", mcs_setup.STANDALONE_UNIT],
        ["systemctl", "--user", "is-active", "--quiet", mcs_setup.STANDALONE_UNIT]]
    assert unit.exists() is bool(expected)
    if not expected:
        assert calls[-1] == ["systemctl", "--user", "daemon-reload"]


def test_switching_retires_the_other_runtimes_jobs(monkeypatch, tmp_path):
    # Hermes -> standalone: owned hermes cron jobs are removed
    owned = [{"id": f"{i:06d}", "name": n, "schedule": s, "script": sc}
             for i, (n, s, sc) in enumerate(mcs_setup.CRON_JOBS)]
    calls, args = _standalone_services(monkeypatch, tmp_path, config(), cron_entries=owned)
    listing = mcs_setup._cron_list
    # like the real parser: a fresh list per call, not the one removes mutate
    monkeypatch.setattr(mcs_setup, "_cron_list", lambda h: list(listing(h)))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "service_manifest.json").write_text(json.dumps(
        {"cron": [{"script": e["script"]} for e in owned]}))
    assert mcs_setup.cmd_services(args) == 0
    removed = [a[3] for a in calls if a[1:3] == ["cron", "remove"]]
    assert sorted(removed) == sorted(e["id"] for e in owned)
    # standalone -> Hermes: the launchd schedule and connector are retired,
    # and the gateway reloads so /mcs and the card workers come back
    calls, args = _services_env(monkeypatch, tmp_path)
    assert mcs_setup.cmd_services(args) == 0
    assert ["/x/hermes", "gateway", "restart"] in [a[:3] for a in calls]
    outs = {a[2].rsplit("/", 1)[-1] for a in calls if a[:2] == ["launchctl", "bootout"]}
    assert mcs_setup.STANDALONE_LABEL in outs
    assert not (tmp_path / "agents" / "ai.mcs.standalone.plist").exists()


def test_init_stores_standalone_tokens_in_mcs_env_only(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(env))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "synthetic-discord-token")
    monkeypatch.setattr(mcs_setup, "_hermes_exe",
                        lambda *a: pytest.fail("standalone must not use Hermes"))
    assert mcs_setup._apply_plugin_integration(config(), SimpleNamespace(yes=True)) is True
    assert env.read_text() == "DISCORD_BOT_TOKEN=synthetic-discord-token\n"
    assert env.stat().st_mode & 0o077 == 0
    # already stored -> nothing re-prompted; a missing Slack token fails init
    cfg = config("slack")
    cfg["notify_target"] = "slack:C0SYNTHETIC"
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    assert mcs_setup._apply_plugin_integration(cfg, SimpleNamespace(yes=True)) is False


def test_init_reuses_a_hermes_token_only_with_consent(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(env))
    hermes = tmp_path / "home" / ".hermes"
    hermes.mkdir(parents=True)
    (hermes / ".env").write_text("DISCORD_BOT_TOKEN=from-hermes\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda *a: "")
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert mcs_setup._apply_plugin_integration(config(), SimpleNamespace(yes=False)) is False
    assert not env.exists()
    assert mcs_setup._apply_plugin_integration(config(), SimpleNamespace(yes=False)) is True
    assert env.read_text() == "DISCORD_BOT_TOKEN=from-hermes\n"


def test_init_warns_when_plugin_flags_have_no_standalone_scope(
        monkeypatch, tmp_path, capsys):
    """--plugin-* grants move into notify.<transport> — with no slack/
    discord interactive scope (or a --plugin-profile that standalone
    never uses) the flags must not be dropped silently."""
    from test_mcs_setup import _init_env
    cfg = config()
    cfg["notify"] = {}                       # no interactive scope at all
    _init_env(monkeypatch, tmp_path, cfg)
    path = tmp_path / "c.json"
    path.write_text(json.dumps(cfg))
    before = path.read_bytes()
    monkeypatch.setattr(mcs_setup.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes",
                         "--plugin-user-ids", "1,2",
                         "--plugin-profile", "work"])
    assert mcs_setup.main() == 1
    out = capsys.readouterr().out
    assert "--plugin-user-ids" in out and "--plugin-profile" in out \
        and "適用されません" in out
    assert path.read_bytes() == before       # invalid scope never rewritten
    assert not (tmp_path / ".env").exists()


def test_init_applies_plugin_flags_into_the_standalone_scope(
        monkeypatch, tmp_path, capsys):
    from test_mcs_setup import _init_env
    _init_env(monkeypatch, tmp_path, config())
    monkeypatch.setattr(mcs_setup.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes",
                         "--plugin-user-ids", "4,5",
                         "--plugin-project-ids", "7"])
    assert mcs_setup.main() == 0
    assert "適用されません" not in capsys.readouterr().out
    saved = json.loads((tmp_path / "c.json").read_text())
    assert saved["notify"]["discord"]["allowed_user_ids"] == ["4", "5"]
    assert saved["notify"]["discord"]["project_ids"] == [7]


def test_back_to_hermes_without_hermes_keeps_the_launchd_schedule(monkeypatch, tmp_path):
    calls, args = _standalone_services(monkeypatch, tmp_path, config())
    assert mcs_setup.cmd_services(args) == 0
    calls, args = _services_env(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda exe: False)
    mcs_setup.cmd_services(args)
    outs = {a[2].rsplit("/", 1)[-1] for a in calls if a[:2] == ["launchctl", "bootout"]}
    assert mcs_setup.STANDALONE_LABEL not in outs
    assert (tmp_path / "agents" / "ai.mcs.standalone.plist").exists()
    assert not any(label.startswith("ai.mcs.cron.") for label in outs)


def test_services_never_reloads_the_job_it_runs_inside(monkeypatch, tmp_path):
    calls, args = _standalone_services(monkeypatch, tmp_path, config())
    helpers = []
    # never a real process here: the helper would drive the real launchd
    monkeypatch.setattr(mcs_setup.subprocess, "Popen", lambda argv, **kw: helpers.append((argv, kw)))
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.mcs.cron.mcs-update")
    plist = tmp_path / "agents" / "ai.mcs.cron.mcs-update.plist"
    plist.parent.mkdir()
    plist.write_text("synthetic")
    assert mcs_setup._agent_reconcile("ai.mcs.cron.mcs-update", str(plist), lambda m: None, False)
    touched = [a for a in calls if a[0] == "launchctl" and "ai.mcs.cron.mcs-update" in " ".join(a)]
    assert touched == []
    plist = tmp_path / "agents" / "ai.mcs.cron.mcs-update.plist"
    assert plist.exists()
    # ...a detached helper reloads it once this run (pid) has exited
    (argv, kw), = helpers
    assert argv[4:] == [str(mcs_setup.os.getpid()), "gui/501/ai.mcs.cron.mcs-update",
                        "gui/501", str(plist)]
    assert kw["start_new_session"] and "XPC_SERVICE_NAME" not in kw["env"]


def test_cron_timeout_wrapper_kills_the_whole_job(tmp_path):
    import subprocess
    marker = tmp_path / "child.pid"
    script = tmp_path / "job.sh"
    # the job's own child must die too (it would hold run.lock otherwise)
    script.write_text(f"sleep 300 & echo $! > {marker}; wait\n")
    started = time.monotonic()
    proc = subprocess.run(["/usr/bin/perl", "-e", scheduled_jobs.TIMEOUT_PERL, "1",
                           "/bin/bash", str(script)], timeout=60)
    assert proc.returncode == 124 and time.monotonic() - started < 30
    child = int(marker.read_text())
    assert _process_gone(child)
    ok = subprocess.run(["/usr/bin/perl", "-e", scheduled_jobs.TIMEOUT_PERL, "30",
                         "/bin/sh", "-c", "exit 3"], timeout=60)
    assert ok.returncode == 3                     # normal exits pass through


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGKILL"])
def test_cron_timeout_wrapper_preserves_child_signal_failure(signal_name):
    import signal
    import subprocess
    import sys

    child = "import os, signal, sys; os.kill(os.getpid(), getattr(signal, sys.argv[1]))"
    proc = subprocess.run(["/usr/bin/perl", "-e", scheduled_jobs.TIMEOUT_PERL, "30",
                           sys.executable, "-c", child, signal_name], timeout=10)
    assert proc.returncode == 128 + getattr(signal, signal_name)


def test_cron_timeout_wrapper_preserves_success():
    import subprocess

    proc = subprocess.run(["/usr/bin/perl", "-e", scheduled_jobs.TIMEOUT_PERL, "30",
                           "/bin/sh", "-c", "exit 0"], timeout=10)
    assert proc.returncode == 0


@pytest.mark.parametrize("target,ok", [
    ("slack:C0SYNTHETIC", True), ("slack:G0SYNTHETIC", True), ("slack:D0SYNTHETIC", True),
    ("slack:U0SYNTHETIC", False), ("slack:#general", False), ("slack:general", False),
    ("slack:c0synthetic", False), ("slack:C0123:1700000000.000100", False),
    ("discord:123", True), ("discord:#mcs", False), ("telegram:1", False),
    ("lineworks:room-synthetic", True)])
def test_standalone_targets(target, ok):
    cfg = {"runtime_mode": "standalone", "notify_target": target}
    assert (mcs_config.validate_standalone(cfg) == []) is ok
    assert mcs_config.validate_standalone({**cfg, "runtime_mode": "hermes"}) == []


def test_failed_hermes_cron_keeps_the_launchd_schedule(monkeypatch, tmp_path):
    calls, args = _standalone_services(monkeypatch, tmp_path, config())
    assert mcs_setup.cmd_services(args) == 0
    calls, args = _services_env(monkeypatch, tmp_path, cron_applies=False)  # create never lands
    assert mcs_setup.cmd_services(args) == 1
    outs = {a[2].rsplit("/", 1)[-1] for a in calls if a[:2] == ["launchctl", "bootout"]}
    assert not any(label.startswith("ai.mcs.cron.") for label in outs)
    assert (tmp_path / "agents" / "ai.mcs.standalone.plist").exists()
    assert ["/x/hermes", "gateway", "restart"] not in [a[:3] for a in calls]


def test_self_reload_waits_for_the_updater_not_for_services(monkeypatch, tmp_path):
    calls, args = _standalone_services(monkeypatch, tmp_path, config())
    helpers = []
    monkeypatch.setattr(mcs_setup.subprocess, "Popen", lambda argv, **kw: helpers.append(argv))
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.mcs.cron.mcs-update")
    monkeypatch.setenv("MCS_JOB_PID", "424242")
    assert mcs_setup._agent_reconcile("ai.mcs.cron.mcs-update", "/synthetic/plist", lambda m: None, False)
    assert helpers[0][4] == "424242"


def test_cron_timeout_wrapper_forwards_sigterm(tmp_path):
    import signal
    import subprocess
    marker = tmp_path / "child.pid"
    script = tmp_path / "job.sh"
    script.write_text(f"sleep 300 & echo $! > {marker}; wait\n")
    proc = subprocess.Popen(["/usr/bin/perl", "-e", scheduled_jobs.TIMEOUT_PERL, "3600",
                             "/bin/bash", str(script)])
    deadline = time.monotonic() + 30
    while not (marker.exists() and marker.read_text().strip()):
        assert time.monotonic() < deadline, "job never started its child"
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)          # what launchd does on bootout
    assert proc.wait(timeout=30) == 143
    child = int(marker.read_text())
    assert _process_gone(child)


def test_retire_previous_hermes_removes_plists_so_login_does_not_reload(monkeypatch, tmp_path):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    for label in ("ai.mcs.extract-drainer", "local.mcs-cmd"):
        (agents / (label + ".plist")).write_text("x")
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(agents))
    # launchd answers 113 once a booted-out label is gone
    monkeypatch.setattr(mcs_setup, "_run", lambda argv, **k: SimpleNamespace(
        returncode=113 if argv[:2] == ["launchctl", "print"] else 0))
    prev = {"runtime_mode": "hermes",
            "agents": [{"label": "ai.mcs.extract-drainer"}, {"label": "local.mcs-cmd"}]}
    assert mcs_setup._retire_previous_runtime(prev, "standalone", lambda m: None, False) == 0
    assert list(agents.iterdir()) == []


@pytest.mark.parametrize("print_code", [124, 127])
def test_unverifiable_launchd_stop_keeps_the_old_agent_and_starts_nothing(
        monkeypatch, tmp_path, print_code):
    """A timed-out or unstartable `launchctl print` is unknown, not stopped:
    the old plist stays and the new runtime is not bootstrapped."""
    label = "ai.mcs.extract-drainer"
    real_loaded = mcs_setup._agent_loaded
    calls, args = _standalone_services(monkeypatch, tmp_path, config(), loaded={label})
    fixture_loaded = mcs_setup._agent_loaded
    monkeypatch.setattr(mcs_setup, "_agent_loaded",
                        lambda name: real_loaded(name) if name == label else fixture_loaded(name))
    fixture_run = mcs_setup.subprocess.run

    def run(argv, **kw):
        if argv[:2] in (["launchctl", "bootout"], ["launchctl", "print"]) \
                and argv[2].endswith("/" + label):
            calls.append(list(argv))
            return SimpleNamespace(returncode=124 if argv[1] == "bootout" else print_code,
                                   stdout="", stderr="synthetic unavailable")
        return fixture_run(argv, **kw)
    monkeypatch.setattr(mcs_setup.subprocess, "run", run)
    (tmp_path / "agents").mkdir()
    (tmp_path / "data").mkdir()
    old = tmp_path / "agents" / (label + ".plist")
    old.write_text("synthetic old service")
    manifest = tmp_path / "data" / "service_manifest.json"
    manifest.write_text(json.dumps({"runtime_mode": "hermes", "agents": [{"label": label}], "cron": []}))
    assert mcs_setup.cmd_services(args) == 1
    assert old.exists()
    assert not any(a[:2] == ["launchctl", "bootstrap"] and a[-1].endswith(mcs_setup.STANDALONE_LABEL + ".plist")
                   for a in calls)
    assert json.loads(manifest.read_text())["runtime_mode"] == "hermes"


@pytest.mark.parametrize("print_code", [124, 127])
def test_retiring_an_agent_with_unknown_state_keeps_its_plist(monkeypatch, tmp_path, print_code):
    agents = tmp_path / "agents"
    agents.mkdir()
    plist = agents / "ai.mcs.cron.mcs-check.plist"
    plist.write_text("synthetic")
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(agents))
    monkeypatch.setattr(mcs_setup, "_run", lambda argv, **kw: SimpleNamespace(
        returncode=print_code if argv[1] == "print" else 0, stdout="", stderr=""))
    assert mcs_setup._agent_loaded("ai.mcs.cron.mcs-check") is None
    assert mcs_setup._retire_agents({"ai.mcs.cron.mcs-check"}, lambda m: None, False) == 1
    assert plist.exists()


def _switch_back_env(monkeypatch, tmp_path):
    """standalone -> hermes with a supervised gateway already running."""
    cfg = config()
    cfg["runtime_mode"] = "hermes"
    calls, args = _standalone_services(monkeypatch, tmp_path, cfg, loaded={mcs_setup.STANDALONE_LABEL})
    (tmp_path / "agents").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "agents" / (mcs_setup.STANDALONE_LABEL + ".plist")).write_text("synthetic old host")
    manifest = tmp_path / "data" / "service_manifest.json"
    manifest.write_text(json.dumps({"runtime_mode": "standalone",
                                    "agents": [{"label": mcs_setup.STANDALONE_LABEL}], "cron": []}))
    return calls, args, manifest


def _restarts(calls):
    return sum(a[1:3] == ["gateway", "restart"] for a in calls)


def test_partial_switch_back_keeps_the_gateway_restart_for_the_next_run(monkeypatch, tmp_path):
    calls, args, manifest = _switch_back_env(monkeypatch, tmp_path)
    fixture_run = mcs_setup.subprocess.run
    fail, creates = [True], [0]

    def run(argv, **kw):
        if argv[1:3] == ["cron", "create"]:
            creates[0] += 1
            if fail[0] and creates[0] == 2:
                calls.append(list(argv))
                return SimpleNamespace(returncode=1, stdout="", stderr="synthetic create failure")
        if argv[1:3] == ["gateway", "status"]:
            calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="supervised", stderr="")
        return fixture_run(argv, **kw)
    monkeypatch.setattr(mcs_setup.subprocess, "run", run)
    assert mcs_setup.cmd_services(args) == 1
    assert _restarts(calls) == 0
    saved = json.loads(manifest.read_text())
    assert saved["runtime_mode"] == "hermes" and saved["gateway_restart_pending"] is True
    split, fail[0] = len(calls), False
    assert mcs_setup.cmd_services(args) == 0
    assert _restarts(calls[split:]) == 1
    assert "gateway_restart_pending" not in json.loads(manifest.read_text())


def test_flags_publish_failure_fails_services_and_defers_the_switch_restart(monkeypatch, tmp_path):
    import notify_cards
    calls, args, manifest = _switch_back_env(monkeypatch, tmp_path)
    fixture_run = mcs_setup.subprocess.run

    def run(argv, **kw):
        if argv[1:3] == ["gateway", "status"]:
            calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="supervised", stderr="")
        return fixture_run(argv, **kw)
    monkeypatch.setattr(mcs_setup.subprocess, "run", run)

    publish = notify_cards.publish_flags

    def broken(cfg, data):
        raise OSError("synthetic flags write failure")
    monkeypatch.setattr(notify_cards, "publish_flags", broken)
    assert mcs_setup.cmd_services(args) == 1
    assert _restarts(calls) == 0
    saved = json.loads(manifest.read_text())   # the verified jobs are still recorded
    assert saved["runtime_mode"] == "hermes" and saved["gateway_restart_pending"] is True
    monkeypatch.setattr(notify_cards, "publish_flags", publish)
    split = len(calls)
    assert mcs_setup.cmd_services(args) == 0
    assert _restarts(calls[split:]) == 1


def _slack_target_config():
    cfg = config(interactive="off")
    cfg["notify_target"] = "slack:C0SYNTHETIC"
    cfg["notify"] = {"interactive": "off", "slack": {
        "profile": "default", "application_id": "A0SYNTHETIC", "team_id": "T0SYNTHETIC",
        "channel_id": "C0SYNTHETIC", "allowed_user_ids": ["U0OLD"], "project_ids": [1]}}
    return cfg


def test_init_applies_plugin_flags_to_the_single_delivery_transport(monkeypatch, tmp_path, capsys):
    """interactive=off with a Slack target: the grants belong to the Slack
    scope the validator requires; Discord-only grants stay unapplied."""
    from test_mcs_setup import _init_env
    _init_env(monkeypatch, tmp_path, _slack_target_config())
    monkeypatch.setattr(mcs_setup.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes", "--plugin-user-ids", "U1,U2",
                         "--plugin-project-ids", "5", "--plugin-chat-ids", "9"])
    assert mcs_setup.main() == 0
    out = capsys.readouterr().out
    assert "--plugin-chat-ids" in out and "--plugin-user-ids" not in out
    slack = json.loads((tmp_path / "c.json").read_text())["notify"]["slack"]
    assert slack["allowed_user_ids"] == ["U1", "U2"] and slack["project_ids"] == [5]
    assert "allowed_chat_ids" not in slack


def test_init_leaves_plugin_flags_unapplied_when_two_transports_need_scopes(monkeypatch, tmp_path, capsys):
    from test_mcs_setup import _init_env
    cfg = _slack_target_config()
    cfg["notify"]["interactive"] = "discord"
    cfg["notify"]["discord"] = config()["notify"]["discord"]
    _init_env(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(mcs_setup.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes", "--plugin-user-ids", "U1"])
    assert mcs_setup.main() == 0
    assert "--plugin-user-ids" in capsys.readouterr().out
    saved = json.loads((tmp_path / "c.json").read_text())["notify"]
    assert saved["slack"]["allowed_user_ids"] == ["U0OLD"]
    assert saved["discord"]["allowed_user_ids"] == ["4000000000000000004"]


# Regression: keychain update under a different account; standalone
# target scope warned as ignored.
def test_keychain_store_updates_existing_item_under_its_account(monkeypatch):
    items = [("mcs", "oldpw")]   # first interactive init: login id unknown

    def run(argv, *, timeout=60, input_text=None, cwd=None):
        def ok(out=""):
            return subprocess.CompletedProcess(argv, 0, out, "")

        if argv[:2] == ["security", "-i"]:
            toks = shlex.split(input_text)
            a, w = toks[toks.index("-a") + 1], toks[toks.index("-w") + 1]
            for i, (acc, _) in enumerate(items):
                if acc == a:
                    items[i] = (a, w)
                    break
            else:
                items.append((a, w))
            return ok()
        if argv[1] == "find-generic-password":
            m = [x for x in items
                 if "-a" not in argv or x[0] == argv[argv.index("-a") + 1]]
            if not m:
                return subprocess.CompletedProcess(argv, 44, "", "")
            acc, w = m[0]
            return ok(w + "\n" if "-w" in argv
                      else f'attributes:\n    "acct"<blob>="{acc}"\n')
        if argv[1] == "delete-generic-password":
            a = argv[argv.index("-a") + 1]
            items[:] = [x for x in items if x[0] != a]
            return ok()
        raise AssertionError(argv)

    monkeypatch.setattr(lifecycle_io, "run_bounded", run)
    assert lifecycle_io.store_keychain("user1", "newpw") is True
    assert items == [("mcs", "newpw")]


def test_standalone_target_scope_is_not_warned_as_ignored():
    cfg = config("slack")
    warn = "notify.discord: 接続設定がありますが、現在の通知先は"
    assert not any(w.startswith(warn) for w in mcs_setup.validate_config(cfg)[1])
    del cfg["runtime_mode"]
    assert any(w.startswith(warn) for w in mcs_setup.validate_config(cfg)[1])


# Linux standalone unit: WorkingDirectory unquoted, '$' escaped in ExecStart.
def test_linux_unit_working_directory_unquoted_and_dollar_escaped(tmp_path):
    root = tmp_path / "a$b"
    root.mkdir()
    _, body = service.render(root, platform="linux")
    repo = str(Path(service.__file__).resolve().parents[1])
    assert "WorkingDirectory=" + repo.replace("%", "%%") + "/.\n" in body
    exec_line = next(line for line in body.splitlines() if line.startswith("ExecStart="))
    assert "a$$b" in exec_line and "a$b" not in exec_line.replace("$$", "")


def test_host_service_creates_owner_only_files(tmp_path):
    """Jobs inherit the host umask: both service forms must set 077."""
    import plistlib
    _, plist = service.render(tmp_path, platform="darwin")
    assert plistlib.loads(plist.encode())["Umask"] == 0o077
    _, unit = service.render(tmp_path, platform="linux")
    assert "\nUMask=0077\n" in unit.split("[Service]", 1)[1].split("[Install]", 1)[0]


@pytest.mark.parametrize("print_code", [1, 124])
def test_retirement_keeps_plists_unless_launchd_reports_not_found(
        monkeypatch, tmp_path, print_code):
    """Only launchd's 113 (not found) proves a booted-out agent stopped; a
    generic failure keeps the plist and blocks the new runtime."""
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "ai.mcs.extract-drainer.plist").write_text("x")
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(agents))
    monkeypatch.setattr(mcs_setup, "_run", lambda argv, **k: SimpleNamespace(
        returncode=print_code if argv[:2] == ["launchctl", "print"] else 0))
    prev = {"runtime_mode": "hermes", "agents": [{"label": "ai.mcs.extract-drainer"}]}
    assert mcs_setup._retire_previous_runtime(prev, "standalone", lambda m: None, False) == 1
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    assert mcs_setup._retire_agents({"ai.mcs.extract-drainer"}, lambda m: None, False) == 1
    assert (agents / "ai.mcs.extract-drainer.plist").exists()
