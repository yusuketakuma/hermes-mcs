"""runtime_mode=standalone: config grants, launchd schedules and services
without Hermes; Hermes mode keeps its existing surface."""
import json
import plistlib
from types import SimpleNamespace

import pytest

import mcs_runtime
import mcs_setup
import mcs_util
from test_mcs_setup import _services_env


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
    assert mcs_setup._calendar(schedule) == want


def test_every_cron_job_has_a_calendar_and_unsupported_fields_fail():
    for _, schedule, _ in mcs_setup.CRON_JOBS:
        assert mcs_setup._calendar(schedule)
    with pytest.raises(ValueError):
        mcs_setup._calendar("0 0 1 * *")


def test_agent_labels_and_ownership_follow_the_runtime():
    assert mcs_setup._agent_labels({}) == mcs_setup.AGENT_LABELS
    labels = mcs_setup._agent_labels(config())
    cron = [mcs_setup._cron_label(s) for _, _, s in mcs_setup.CRON_JOBS]
    assert labels == mcs_setup.AGENT_LABELS + cron + [mcs_setup.STANDALONE_LABEL]
    # no Slack/Discord cards -> no connector process
    assert mcs_setup.STANDALONE_LABEL not in mcs_setup._agent_labels(config("off"))
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
    assert any("standalone sends only" in e for e in mcs_setup.validate_config(cfg)[0])
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
    script = (tmp_path / "data" / "scripts" / "mcs_check.sh").read_text()
    assert venv in script and not (tmp_path / "scripts").exists()
    plist = plistlib.loads((tmp_path / "agents" / "ai.mcs.cron.mcs-check.plist").read_bytes())
    assert plist["ProgramArguments"][0] == "/usr/bin/perl"     # 3600s cap, as hermes cron
    assert plist["ProgramArguments"][3:] == [
        str(mcs_setup.CRON_TIMEOUT_S), "/bin/bash", str(tmp_path / "data" / "scripts" / "mcs_check.sh")]
    assert len(plist["StartCalendarInterval"]) == 12
    assert plist["AbandonProcessGroup"] is True       # Chrome outlives the tick
    connector = (tmp_path / "agents" / "ai.mcs.standalone.plist").read_text()
    assert venv in connector and "mcs_standalone/__main__.py" in connector
    assert not any(a[0] == "/x/hermes" for a in calls)
    manifest = json.loads((tmp_path / "data" / "service_manifest.json").read_text())
    assert manifest["cron"] == [] and len(manifest["agents"]) == 4 + 6 + 1


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
    assert mcs_setup.STANDALONE_LABEL in outs and "ai.mcs.cron.mcs-check" in outs
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
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes",
                         "--plugin-user-ids", "1,2",
                         "--plugin-profile", "work"])
    assert mcs_setup.main() == 0
    out = capsys.readouterr().out
    assert "--plugin-user-ids" in out and "--plugin-profile" in out \
        and "適用されません" in out
    saved = json.loads((tmp_path / "c.json").read_text())
    assert saved["notify"] == {}             # nothing silently invented


def test_init_applies_plugin_flags_into_the_standalone_scope(
        monkeypatch, tmp_path, capsys):
    from test_mcs_setup import _init_env
    _init_env(monkeypatch, tmp_path, config())
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
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda exe: False)
    mcs_setup.cmd_services(args)
    outs = {a[2].rsplit("/", 1)[-1] for a in calls if a[:2] == ["launchctl", "bootout"]}
    assert mcs_setup.STANDALONE_LABEL in outs
    assert not any(label.startswith("ai.mcs.cron.") for label in outs)


def test_services_never_reloads_the_job_it_runs_inside(monkeypatch, tmp_path):
    calls, args = _standalone_services(monkeypatch, tmp_path, config())
    helpers = []
    # never a real process here: the helper would drive the real launchd
    monkeypatch.setattr(mcs_setup.subprocess, "Popen", lambda argv, **kw: helpers.append((argv, kw)))
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.mcs.cron.mcs-update")
    assert mcs_setup.cmd_services(args) == 0
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
    import time
    marker = tmp_path / "child.pid"
    script = tmp_path / "job.sh"
    # the job's own child must die too (it would hold run.lock otherwise)
    script.write_text(f"sleep 300 & echo $! > {marker}; wait\n")
    started = time.monotonic()
    proc = subprocess.run(["/usr/bin/perl", "-e", mcs_setup._TIMEOUT_PL, "1",
                           "/bin/bash", str(script)], timeout=60)
    assert proc.returncode == 124 and time.monotonic() - started < 30
    child = int(marker.read_text())
    time.sleep(0.5)
    assert subprocess.run(["kill", "-0", str(child)], capture_output=True).returncode != 0
    ok = subprocess.run(["/usr/bin/perl", "-e", mcs_setup._TIMEOUT_PL, "30",
                         "/bin/sh", "-c", "exit 3"], timeout=60)
    assert ok.returncode == 3                     # normal exits pass through


@pytest.mark.parametrize("target,ok", [
    ("slack:C0SYNTHETIC", True), ("slack:G0SYNTHETIC", True), ("slack:D0SYNTHETIC", True),
    ("slack:U0SYNTHETIC", False), ("slack:#general", False), ("slack:general", False),
    ("slack:c0synthetic", False), ("slack:C0123:1700000000.000100", False),
    ("discord:123", True), ("discord:#mcs", False), ("telegram:1", False),
    ("lineworks:room-synthetic", True)])
def test_standalone_targets(target, ok):
    cfg = {"runtime_mode": "standalone", "notify_target": target}
    assert (mcs_setup._validate_standalone(cfg) == []) is ok
    assert mcs_setup._validate_standalone({**cfg, "runtime_mode": "hermes"}) == []


def test_failed_hermes_cron_keeps_the_launchd_schedule(monkeypatch, tmp_path):
    calls, args = _standalone_services(monkeypatch, tmp_path, config())
    assert mcs_setup.cmd_services(args) == 0
    calls, args = _services_env(monkeypatch, tmp_path, cron_applies=False)  # create never lands
    assert mcs_setup.cmd_services(args) == 1
    outs = {a[2].rsplit("/", 1)[-1] for a in calls if a[:2] == ["launchctl", "bootout"]}
    assert not any(label.startswith("ai.mcs.cron.") for label in outs)
    assert (tmp_path / "agents" / "ai.mcs.cron.mcs-check.plist").exists()


def test_self_reload_waits_for_the_updater_not_for_services(monkeypatch, tmp_path):
    calls, args = _standalone_services(monkeypatch, tmp_path, config())
    helpers = []
    monkeypatch.setattr(mcs_setup.subprocess, "Popen", lambda argv, **kw: helpers.append(argv))
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.mcs.cron.mcs-update")
    monkeypatch.setenv("MCS_JOB_PID", "424242")
    assert mcs_setup.cmd_services(args) == 0
    assert helpers[0][4] == "424242"


def test_cron_timeout_wrapper_forwards_sigterm(tmp_path):
    import signal
    import subprocess
    import time
    marker = tmp_path / "child.pid"
    script = tmp_path / "job.sh"
    script.write_text(f"sleep 300 & echo $! > {marker}; wait\n")
    proc = subprocess.Popen(["/usr/bin/perl", "-e", mcs_setup._TIMEOUT_PL, "3600",
                             "/bin/bash", str(script)])
    for _ in range(100):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)          # what launchd does on bootout
    assert proc.wait(timeout=30) == 143
    time.sleep(0.5)
    child = int(marker.read_text())
    assert subprocess.run(["kill", "-0", str(child)], capture_output=True).returncode != 0
