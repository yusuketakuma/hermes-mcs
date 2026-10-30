"""Synthetic service plans keep unknown observations and ownership proof distinct."""
import copy
import shlex

import pytest

import run_log_install
import service_contract as contract
import update_launch

JOBS = [("desired", "*/10 * * * *", "desired.sh")]


def entry(script="desired.sh", **values):
    return {"id": "abcdef", "name": "desired", "schedule": "*/10 * * * *", "script": script, **values}


def test_unknown_and_verified_empty_never_share_an_action_plan():
    unknown = contract.plan_cron({}, JOBS, None)
    empty = contract.plan_cron({}, JOBS, [])
    assert unknown["unverifiable"] is True
    assert unknown["actions"] == []
    assert empty["unverifiable"] is False
    assert [action["kind"] for action in empty["actions"]] == ["create"]
    assert contract.parse_cron_listing("No scheduled jobs in profile 'default'.\n", 0) == []
    assert contract.parse_cron_listing("Jobs changed format\n", 0) is None
    assert contract.parse_cron_listing("No scheduled jobs.\n", 1) is None


def test_duplicates_collision_and_order_are_preserved_without_mutation():
    previous = {"cron": [{"script": "old.sh"}]}
    observed = [entry("other.sh"), entry("old.sh", name="previous")]
    before = copy.deepcopy((previous, observed))
    plan = contract.plan_cron(previous, JOBS, observed)
    assert [action["kind"] for action in plan["actions"]] == ["collision", "remove"]
    assert plan["actions"][1]["script"] == "old.sh"
    assert (previous, observed) == before
    duplicate = contract.plan_cron({}, JOBS, [entry(), entry(id="123456")])
    assert duplicate["duplicates"] == ["desired.sh"]
    assert duplicate["actions"] == []
    # An unknown schedule decoration never becomes an invented mismatch.
    tolerant = contract.plan_cron({}, JOBS, [entry(schedule="human scheduling text")])
    assert [action["kind"] for action in tolerant["actions"]] == ["keep"]


def test_partial_manifest_requires_confirmed_creation_and_keeps_survivor_identity():
    planned = [{"script": "desired.sh"}, {"script": "unseen.sh"}]
    observed = [entry(), entry("old.sh", id="123456", name="previous")]
    before = copy.deepcopy((planned, observed))
    result = contract.confirm_cron(["desired.sh", "unseen.sh"],
        {"desired.sh", "old.sh"}, {"desired.sh"}, planned, observed, JOBS)
    assert result["persist_ownership"] is True
    assert result["problems"] == 1
    assert result["surviving_scripts"] == ["old.sh"]
    assert result["cron"] == [{"script": "desired.sh"}, entry("old.sh", id="123456", name="previous")]
    assert (planned, observed) == before
    unknown = contract.confirm_cron(["desired.sh"], {"desired.sh"},
        {"desired.sh"}, planned, None, JOBS)
    assert unknown["persist_ownership"] is False
    assert unknown["unverifiable"] is True
    missing = contract.confirm_cron(["desired.sh"], {"desired.sh"},
        {"desired.sh"}, planned, [], JOBS)
    assert missing["persist_ownership"] is False
    assert missing["unverifiable"] is True


def substitutions(cfg):
    return contract.service_substitutions(cfg, root="/synthetic/root", checkout="/synthetic/repo",
        standalone_runtime_home="/synthetic/standalone", hermes_runtime_home="/synthetic/hermes",
        hermes_python="/synthetic/hermes/python")


def test_runtime_selection_validation_and_rendering_use_supplied_facts(monkeypatch):
    monkeypatch.setattr(run_log_install, "helper_path",
                        lambda root, recovery_dir, *, checkout: "/synthetic/run_log.py")
    assert substitutions({})["PYTHON"] == "/synthetic/hermes/python"
    assert substitutions({})["RUN_LOG_HELPER"] == "/synthetic/run_log.py"
    standalone = substitutions({"runtime_mode": "standalone", "watchdog_grace_s": 0})
    assert standalone["PYTHON"] == "/synthetic/standalone/venv/bin/python3"
    assert standalone["WATCHDOG_GRACE"] == "0"
    assert contract.desired_agent_labels({"runtime_mode": "standalone"}) == ["ai.mcs.standalone", "local.mcs-health"]
    assert contract.desired_agent_labels({}) == ["local.mcs-cmd", "local.mcs-int", "local.mcs-health", "ai.mcs.extract-drainer", "ai.mcs.extract-drainer-2"]
    for cfg in ({"runtime_mode": "invalid"}, {"watchdog_grace_s": True},
                {"watchdog_grace_s": 3601}, {"backup": {"enabled": "yes"}}):
        with pytest.raises(ValueError):
            substitutions(cfg)
    value = "/synthetic/space & <quote> '"
    assert shlex.split(contract.render_script("run __VALUE__", {"VALUE": value})) == ["run", value]
    assert contract.render_agent("<string>__VALUE__</string>", {"VALUE": value}) == "<string>/synthetic/space &amp; &lt;quote&gt; '</string>"


def test_job_identity_strip_does_not_strip_detached_update_flags():
    original = {"XPC_SERVICE_NAME": "synthetic-service", "MCS_JOB_PID": "123",
                "_HERMES_GATEWAY": "1", "PATH": "/synthetic/bin"}
    assert update_launch.without_job_identity(original) == {
        "_HERMES_GATEWAY": "1", "PATH": "/synthetic/bin"}
    assert update_launch.launch_environment(original, detached=True) == {"PATH": "/synthetic/bin"}
    assert original["XPC_SERVICE_NAME"] == "synthetic-service"


def test_command_watcher_retries_pending_work_without_another_directory_event():
    import plistlib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    text = (root / "deployment/launchagents/local.mcs-int.plist").read_text()
    agent = plistlib.loads(contract.render_agent(text, {"DATA": "/synthetic/root/data"}).encode())
    assert agent["WatchPaths"] == ["/synthetic/root/data/cmd_int"]
    assert agent["StartInterval"] == agent["ThrottleInterval"] == 5
    assert agent["ProgramArguments"][-2:] == ["--json", "--commands-only"]
    assert not agent.get("KeepAlive")
