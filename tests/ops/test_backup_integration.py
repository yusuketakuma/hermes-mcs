"""Explicit backup settings render safely and health reads only synthetic records."""
import json
from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import mcs_backup
import mcs_setup
import run_check
from ledger import Ledger
from test_mcs_backup import NOW, world as world


@pytest.fixture
def backup_settings(tmp_path, monkeypatch):
    root, destination, scratch = (tmp_path / name for name in ("local", "external", "scratch"))
    for directory in (root, destination, scratch, root / "data", root / "snapshots"):
        directory.mkdir(mode=0o700)
    identity = destination.stat()
    policy = mcs_backup.BackupPolicy(
        destination=str(destination), destination_device=identity.st_dev,
        destination_inode=identity.st_ino, scratch_dir=str(scratch),
        policy_id="fictional", key_custody_confirmed=True, allow_os_openssl=True,
        max_snapshots=2, deletion="manual", max_rpo_seconds=200,
        max_snapshot_bytes=1024 * 1024)
    path = root / "policy.json"
    path.write_text(json.dumps({**asdict(policy), "scheduled": True}))
    path.chmod(0o600)
    monkeypatch.setattr(run_check, "HOME", str(root))
    monkeypatch.setattr(run_check.time, "time", lambda: 100)
    config = {"backup": {
        "enabled": True, "policy": str(path), "snapshot_dir": str(root / "snapshots"),
        "schedule": "17 3 * * *", "verify_interval_s": 60, "drill_interval_s": 120}}
    return root, config


def test_disabled_backup_never_reads_policy_or_keys(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled backup must not inspect storage or keys")
    monkeypatch.setattr(mcs_backup, "load_policy", forbidden)
    monkeypatch.setattr(mcs_backup, "_keychain_read", forbidden)
    assert run_check._backup_health({}, 100) == {"state": "disabled", "reasons": []}
    assert mcs_setup._service_subs({})["BACKUP_ENABLED"] == "0"


def test_backup_configuration_requires_owner_paths_schedule_and_intervals(backup_settings):
    _, config = backup_settings
    settings = config["backup"]
    assert mcs_setup._backup_config(settings) is None
    for key in ("policy", "snapshot_dir", "schedule", "verify_interval_s", "drill_interval_s"):
        incomplete = {k: v for k, v in settings.items() if k != key}
        assert mcs_setup._backup_config(incomplete) is not None
    for schedule in ("60 3 * * *", "17 24 * * *", "*/5 * * * *", "17 3 * * 1"):
        assert mcs_setup._backup_config({**settings, "schedule": schedule}) is not None
    subs = mcs_setup._service_subs(config)
    assert subs["BACKUP_ENABLED"] == "1"
    assert subs["BACKUP_POLICY"] == settings["policy"]
    assert subs["BACKUP_SNAPSHOT_DIR"] == settings["snapshot_dir"]


def test_backup_health_requires_drill_and_keeps_records_out_of_output(
        backup_settings, monkeypatch):
    root, config = backup_settings
    state_path = root / "data" / "backup_state.json"
    state = {"v": 1, "policy_id": "fictional", "last_offsite_at": 100,
             "last_verify_at": 100, "source_last_successful_run": 100,
             "receipt": {"bundle": "PRIVATE_PATH_CANARY", "sha256": "a" * 64}}
    state_path.write_text(json.dumps(state))
    state_path.chmod(0o600)
    def forbidden(*args, **kwargs):
        pytest.fail("health must not read keys")
    monkeypatch.setattr(mcs_backup, "_keychain_read", forbidden)
    assert run_check._backup_health(config, 100)["reasons"] == ["backup_drill_at_unknown"]
    state["last_drill_at"] = 100
    state_path.write_text(json.dumps(state))
    health = run_check._backup_health(config, 100)
    assert health["state"] == "ok"
    assert "PRIVATE_PATH_CANARY" not in json.dumps(health)
    assert "policy_id" not in health
    assert "backup_verify_at_stale" in run_check._backup_health(config, 161)["reasons"]
    assert "backup_rpo_exceeded" in run_check._backup_health(config, 301)["reasons"]
    monkeypatch.setattr(run_check, "_free_mb", lambda: 10000)
    monkeypatch.setattr(run_check, "_prev_health", lambda: {})
    with_db = Ledger(str(root / "data" / "synthetic.db"))
    try:
        state["last_action_status"] = "failed"
        state_path.write_text(json.dumps(state))
        report = run_check._health(with_db, {"errors": []}, "ok", cfg=config)
        assert report["overall"] == "degraded"
        assert report["backup"]["reasons"] == ["backup_failed"]
        assert report["backup"]["failure_code"] is None       # cause not recorded
        state.update(last_error="backup_retention_capacity")
        state_path.write_text(json.dumps(state))
        detail = run_check._backup_health(config, 100)
        assert detail["reasons"] == ["backup_failed"]
        assert detail["failure_code"] == "backup_retention_capacity"
        assert "backup_not_verified" in report["state_reasons"]
    finally:
        with_db.close()


def test_invalid_private_policy_is_a_safe_failed_health_state(backup_settings):
    root, config = backup_settings
    (root / "policy.json").chmod(0o644)
    assert run_check._backup_health(config, 100) == {
        "state": "failed", "reasons": ["backup_io_or_policy_failed"]}


def test_explicit_static_path_and_two_hours_are_shared_by_both_schedulers(
        backup_settings, monkeypatch):
    from types import SimpleNamespace
    from mcs_standalone.runtime import Runtime
    root, cfg = backup_settings
    backup = cfg["backup"]
    backup["snapshot"] = str(root / "snapshots/ledger-snapshot.db")
    del backup["snapshot_dir"]
    backup["schedule"] = "17 3,15 * * *"
    jobs = mcs_setup.configured_cron_jobs(cfg)
    assert jobs[:-1] == mcs_setup.CRON_JOBS
    assert jobs[-1][1:] == ("17 3,15 * * *", "mcs_offsite.sh")
    calendars = [{"Hour": 3, "Minute": 17}, {"Hour": 15, "Minute": 17}]
    assert mcs_setup._calendar(backup["schedule"]) == calendars
    independent = Runtime(root, {**cfg, "runtime_mode": "standalone"})
    assert dict(independent.schedules)["mcs_offsite"] == calendars
    assert len(independent.schedules) == len(mcs_setup.CRON_JOBS) + 1
    entries = []
    calls = []
    monkeypatch.setattr(mcs_setup, "SCRIPTS_DIR", str(root / "scripts"))

    def hermes(argv, **kwargs):
        calls.append(argv)
        if argv[2] == "list":
            text = "\n".join(
                f"  {entry['id']}\n    Name: {entry['name']}\n"
                f"    Schedule: {entry['schedule']}\n    Script: {entry['script']}"
                for entry in entries) if entries else "No scheduled jobs."
            return SimpleNamespace(returncode=0, stdout=text, stderr="")
        if argv[2] == "create":
            entries.append({"id": f"{len(entries) + 1:06x}",
                            "name": argv[argv.index("--name") + 1],
                            "schedule": argv[3], "script": argv[argv.index("--script") + 1]})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mcs_setup, "_run", hermes)
    monkeypatch.setattr(mcs_setup.subprocess, "run", hermes)
    problems, _ = mcs_setup._sync_cron(
        {"cron": []}, "synthetic-hermes", {"cron": []}, lambda _: None, False, jobs=jobs)
    assert problems == 0
    offsite = [call for call in calls if call[2] == "create" and call[3] == backup["schedule"]]
    assert len(offsite) == 1
    assert offsite[0][offsite[0].index("--script") + 1] == "mcs_offsite.sh"


@pytest.mark.parametrize("schedule", ["0 2,14 * * *", "59 0,23 * * *",
                                     "17 " + ",".join(map(str, range(24))) + " * * *"])
def test_bounded_explicit_hour_lists_are_representable(backup_settings, schedule):
    _, cfg = backup_settings
    cfg["backup"]["schedule"] = schedule
    assert mcs_setup._backup_config(cfg["backup"]) is None
    assert len(mcs_setup._calendar(schedule)) == len(schedule.split()[1].split(","))


@pytest.mark.parametrize("schedule", [
    "17 3,3 * * *", "17 3,03 * * *", "17 3,24 * * *",
    "17 3, * * *", "17 * * * *", "17 */12 * * *",
    "*/5 3,15 * * *", "60 3,15 * * *", "17 3,15 1 * *",
    "17 " + ",".join(["1"] * 25) + " * * *",
])
def test_invalid_or_unbounded_backup_cadence_never_creates_jobs(backup_settings, schedule):
    _, cfg = backup_settings
    cfg["backup"]["schedule"] = schedule
    with pytest.raises(ValueError, match="backup_config_invalid"):
        mcs_setup.configured_cron_jobs(cfg)


@pytest.mark.parametrize("sources", [
    {}, {"snapshot": "relative.db"}, {"snapshot": ""},
    {"snapshot": True}, {"snapshot": "/synthetic/static.db", "snapshot_dir": "/synthetic"},
])
def test_explicit_source_selector_cannot_be_missing_relative_or_ambiguous(
        backup_settings, sources):
    _, cfg = backup_settings
    del cfg["backup"]["snapshot_dir"]
    cfg["backup"].update(sources)
    with pytest.raises(ValueError, match="backup_config_invalid"):
        mcs_setup._service_subs(cfg)


@pytest.mark.parametrize("mode", ["hermes", "standalone"])
@pytest.mark.parametrize("age", ["fresh", "stale", "unknown"])
def test_rendered_wrapper_consumes_only_explicit_static_generation(
        world, monkeypatch, tmp_path, mode, age):
    import ledger
    daily, _, _, policy = world
    if age != "fresh":
        with sqlite3.connect(daily) as db:
            db.execute("UPDATE runs SET finished_at=?",
                       (NOW - 86401 if age == "stale" else None,))
    published = ledger.publish_snapshot(str(daily), str(tmp_path / "published ' snapshots"))
    assert Path(published).name == "ledger-snapshot.db"
    source = Path(published)
    before = (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns)
    root = tmp_path / "local"
    (root / "data").mkdir(mode=0o700, parents=True)
    (root / "venv/bin").mkdir(parents=True)
    repo = Path(__file__).resolve().parents[2]
    calls = tmp_path / "wrapper-argv.json"
    driver = root / "venv/bin/python3"
    driver.write_text(
        f"#!{sys.executable}\n"
        "import json,runpy,sys\n"
        f"runpy.run_path({str(repo / 'tests/conftest.py')!r})\n"
        "import mcs_backup\n"
        f"mcs_backup.time.time=lambda:{NOW!r}\n"
        f"with open({str(calls)!r},'w') as stream:json.dump(sys.argv[1:],stream)\n"
        "sys.exit(mcs_backup.main(sys.argv[2:],keychain_reader=lambda:bytes(range(32))))\n")
    driver.chmod(0o700)
    policy_path = root / "owner ' policy.json"
    policy_path.write_text(json.dumps({**asdict(policy), "scheduled": True}))
    policy_path.chmod(0o600)
    cfg = {"runtime_mode": mode, "backup": {
        "enabled": True, "policy": str(policy_path), "snapshot": str(source),
        "schedule": "17 3,15 * * *", "verify_interval_s": 3600, "drill_interval_s": 86400}}
    monkeypatch.setattr(mcs_setup, "HOME", str(root))
    monkeypatch.setattr(mcs_setup, "HERMES_PY", str(driver))
    wrapper = root / "wrapper.sh"
    wrapper.write_text(dict(mcs_setup._rendered_scripts(mcs_setup._service_subs(cfg)))[
        "mcs_offsite.sh"])
    result = subprocess.run(["/bin/bash", str(wrapper)], capture_output=True, text=True,
                            env=dict(os.environ), timeout=30, check=False)
    argv = json.loads(calls.read_text())
    assert argv[0] == str(repo / "mcs/ops/mcs_backup.py")
    assert argv[argv.index("--snapshot") + 1] == str(source)
    assert "--snapshot-dir" not in argv and "--scheduled" in argv
    assert (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns) == before
    assert policy.max_rpo_seconds == 86400
    local = mcs_backup.status(str(root / "data"), policy)
    if age == "fresh":
        assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
        assert local["source_last_successful_run"] == NOW and local["within_rpo"] is True
        state = mcs_backup._records(str(root / "data"), policy).load("backup_state.json")
        assert state["receipt"]["last_successful_run"] == NOW
    else:
        assert result.returncode == 1 and "backup failed (exit 1)" in result.stdout
        assert local["last_attempt_failed"] is True and local["last_offsite_at"] is None
        assert local["within_rpo"] is None
        assert not list(Path(policy.destination).glob("snapshot-*.mcsb"))
        assert run_check._backup_health(cfg, NOW)["state"] != "healthy"
    with pytest.raises(mcs_backup.BackupError, match="backup_no_valid_daily_snapshot"):
        mcs_backup._latest_snapshot(str(source.parent), policy)


def test_standalone_host_owns_only_explicit_offsite_schedule(backup_settings):
    from mcs_standalone.runtime import Runtime

    root, config = backup_settings
    cfg = {**config, "runtime_mode": "standalone"}
    enabled = Runtime(root, cfg)
    assert dict(enabled.schedules)["mcs_offsite"] == [{"Minute": 17, "Hour": 3}]
    assert enabled.argv["mcs_offsite"][-1] == str(root / "scripts" / "mcs_offsite.sh")
    disabled = Runtime(root, {"runtime_mode": "standalone"})
    assert "mcs_offsite" not in disabled.argv
    assert len(disabled.schedules) == len(mcs_setup.CRON_JOBS)
    assert len(enabled.schedules) == len(disabled.schedules) + 1


def test_hermes_offsite_registration_and_disable_preserve_shared_jobs(
        backup_settings, monkeypatch):
    from types import SimpleNamespace

    _, config = backup_settings
    entries = [{"id": f"synthetic-{i}", "name": name, "schedule": schedule, "script": script}
               for i, (name, schedule, script) in enumerate(mcs_setup.CRON_JOBS)]
    entries.append({"id": "shared", "name": "shared external",
                    "schedule": "0 0 * * *", "script": "external.sh"})
    calls = []
    monkeypatch.setattr(mcs_setup, "_cron_list", lambda _: [dict(row) for row in entries])
    def execute(argv, **kwargs):
        calls.append(argv)
        if argv[2] == "create":
            entries.append({"id": "synthetic-offsite", "name": argv[argv.index("--name") + 1],
                            "schedule": argv[3], "script": argv[argv.index("--script") + 1]})
        elif argv[2] == "remove":
            entries[:] = [row for row in entries if row["id"] != argv[3]]
        else:
            pytest.fail("unexpected synthetic cron operation")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mcs_setup, "_run", execute)
    enabled_manifest = {"cron": []}
    problems, _ = mcs_setup._sync_cron(
        {}, "synthetic-hermes", enabled_manifest, lambda _: None, False,
        jobs=mcs_setup.configured_cron_jobs(config))
    assert problems == 0
    assert len(calls) == 1 and calls[0][2] == "create"
    assert calls[0][3] == "17 3 * * *"
    assert enabled_manifest["cron"][-1]["script"] == "mcs_offsite.sh"
    disabled_manifest = {"cron": []}
    problems, _ = mcs_setup._sync_cron(
        enabled_manifest, "synthetic-hermes", disabled_manifest, lambda _: None, False,
        jobs=mcs_setup.configured_cron_jobs({}))
    assert problems == 0
    assert calls[-1] == ["synthetic-hermes", "cron", "remove", "synthetic-offsite"]
    assert entries[-1]["id"] == "shared"
    assert len(entries) == len(mcs_setup.CRON_JOBS) + 1
