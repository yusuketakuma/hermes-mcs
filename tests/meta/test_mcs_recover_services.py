"""Synthetic mcs recover services contracts; shared helpers preserve isolated runtime state."""
import scheduled_jobs
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from ops_testkit import _FakeLaunchd, _load
from mcs_recover_testkit import _EnsureLaunchd as _EnsureLaunchd, _applying as _applying, _backup_membership as _backup_membership, _make_repo as _make_repo, _standalone_recovery as _standalone_recovery, gateway_restarts as gateway_restarts, rec as rec


def test_membership_reconcile_removes_undesired_agents(rec, tmp_path,
                                                       monkeypatch):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "ai.mcs.extract-old.plist").write_text("<plist/>")
    (agents / "ai.mcs.llamaserver.plist").write_text("<plist/>")
    (agents / "unrelated.other.plist").write_text("<plist/>")
    snapshot = {"agents": [{"label": "ai.mcs.extract-drainer"}],
                "cron": []}
    commands = []

    def record_command(args, **kwargs):
        commands.append(args)
        # launchd answers 113 once the booted-out label is gone
        return subprocess.CompletedProcess(args, 113 if args[1] == "print" else 0, "")

    monkeypatch.setattr(rec.subprocess, "run", record_command)
    monkeypatch.setattr(rec.shutil, "which", lambda name: None)
    rec.mcs_recovery_services._reconcile_membership(snapshot)
    # undesired owned agent removed only after launchd confirms it is gone
    target = f"gui/{os.getuid()}/ai.mcs.extract-old"
    assert commands == [["launchctl", "bootout", target], ["launchctl", "print", target]]
    assert not (agents / "ai.mcs.extract-old.plist").exists()
    assert (agents / "ai.mcs.llamaserver.plist").exists()
    assert (agents / "unrelated.other.plist").exists()


@pytest.mark.parametrize("returncode", [0, 1])
def test_cron_job_removal_requires_successful_list(
        rec, tmp_path, monkeypatch, returncode):
    hermes = tmp_path / "synthetic-hermes"
    hermes.write_text("# synthetic executable placeholder")
    Path(rec.mcs_recovery_core.MANIFEST_PATH).write_text(json.dumps(
        {"cron": [{"id": "abcdef", "script": "/synthetic/mcs_removed.sh"}]}))
    monkeypatch.setattr(rec.shutil, "which", lambda name: str(hermes))
    calls = []

    def list_result(argv, **kwargs):
        calls.append(argv)
        if argv[1:] == ["cron", "list", "--all"]:
            return subprocess.CompletedProcess(
                argv, returncode,
                "  abcdef [disabled]\n    Script:  /synthetic/mcs_removed.sh\n",
                "synthetic listing failure")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rec.subprocess, "run", list_result)
    problems = rec.mcs_recovery_services._reconcile_membership({"cron": [], "agents": []})
    assert ("cron_list_unverifiable" in problems) == (returncode != 0)
    expected = [[str(hermes), "cron", "list", "--all"]]
    if returncode == 0:
        expected.append([str(hermes), "cron", "remove", "abcdef"])
    assert calls == expected


def test_reconcile_reports_failed_service_command(rec, tmp_path, monkeypatch):
    setup = tmp_path / "repo" / "mcs" / "ops" / "mcs_setup.py"
    setup.parent.mkdir(parents=True)
    setup.write_text("# synthetic")
    monkeypatch.setattr(rec.mcs_recovery_services, "_setup_python", lambda: "synthetic-python")
    monkeypatch.setattr(rec.mcs_recovery_core, "_run_owned", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, "", "failed"))
    assert "services_reconcile_failed" in rec.mcs_recovery_services._reconcile_membership(None)


def test_known_cron_scripts_cover_setup_cron_jobs():
    """mcs_setup.CRON_JOBS scripts must all live in KNOWN_CRON_SCRIPTS —
    a missing entry makes a snapshot-based recovery delete a live,
    desired cron job (the mcs_health.sh drift class)."""
    rec = _load()
    desired = {script for _name, _sched, script in scheduled_jobs.CRON_JOBS}
    assert desired <= rec.mcs_recovery_core.KNOWN_CRON_SCRIPTS
    assert "mcs_offsite.sh" in rec.mcs_recovery_core.KNOWN_CRON_SCRIPTS
    assert "ai.mcs.cron.mcs-offsite" not in rec.mcs_recovery_core.KNOWN_AGENT_LABELS


@pytest.mark.parametrize(("listed", "evidence", "owned"), [
    ("mcs_offsite.sh", [], False),
    ("/shared/mcs_offsite.sh", [], False),
    ("mcs_custom.sh", [], False),
    ("mcs_offsite.sh", [{"id": "abcdef", "script": "mcs_offsite.sh"}], True),
    ("mcs_offsite.sh", [{"id": "badbad", "script": "mcs_offsite.sh"}], False),
    ("/shared/mcs_offsite.sh", [{"id": "abcdef", "script": "mcs_offsite.sh"}], False),
    ("mcs_offsite.sh", [{"id": "abcdef", "script": "mcs_check.sh"}], False),
    ("mcs_custom.sh", [None, {}, {"id": "abcdef", "script": "mcs_custom.sh"}], True),
])
def test_cron_owner_requires_id_script_pair_or_canonical_identity(
        rec, listed, evidence, owned):
    assert rec.mcs_recovery_services._owned_cron("abcdef", listed, evidence) is owned


def test_canonical_offsite_identity_is_distinct_from_shared_basename(rec):
    canonical = str(Path(rec.mcs_recovery_core.SCRIPTS_DIR, "mcs_offsite.sh"))
    assert rec.mcs_recovery_services._owned_cron("abcdef", canonical, [])
    assert not rec.mcs_recovery_services._owned_cron(
        "abcdef", str(Path(rec.mcs_recovery_core.SCRIPTS_DIR, "mcs_shared.sh")), [])


@pytest.mark.parametrize(("enabled", "template", "retired"), [
    (False, False, True), (False, True, True),
    (True, False, True), (True, True, False),
])
def test_optional_backup_current_desire_requires_opt_in_and_restored_capability(
        rec, tmp_path, monkeypatch, enabled, template, retired):
    hermes, calls, _ = _backup_membership(
        rec, tmp_path, monkeypatch, enabled=enabled, template=template)
    rec.mcs_recovery_services._reconcile_membership({"cron": [], "agents": []})
    expected = [[str(hermes), "cron", "list", "--all"]]
    if retired:
        expected.append([str(hermes), "cron", "remove", "abcdef"])
    assert calls == expected


def test_snapshot_desire_protects_backup_without_installing_it(rec, tmp_path, monkeypatch):
    hermes, calls, snapshot = _backup_membership(
        rec, tmp_path, monkeypatch, proof="snapshot")
    rec.mcs_recovery_services._reconcile_membership(snapshot)
    assert calls == [[str(hermes), "cron", "list", "--all"]]


@pytest.mark.parametrize("listed", ["mcs_offsite.sh", "/shared/mcs_offsite.sh"])
def test_missing_or_retargeted_backup_ownership_never_deletes_shared_job(
        rec, tmp_path, monkeypatch, listed):
    hermes, calls, _ = _backup_membership(
        rec, tmp_path, monkeypatch, proof="none", listed=listed)
    rec.mcs_recovery_services._reconcile_membership({"cron": [], "agents": []})
    assert calls == [[str(hermes), "cron", "list", "--all"]]


def test_canonical_identity_can_retire_old_owned_backup(rec, tmp_path, monkeypatch):
    hermes, calls, _ = _backup_membership(
        rec, tmp_path, monkeypatch, proof="none",
        listed=str(Path(rec.mcs_recovery_core.SCRIPTS_DIR, "mcs_offsite.sh")))
    rec.mcs_recovery_services._reconcile_membership({"cron": [], "agents": []})
    assert calls == [[str(hermes), "cron", "list", "--all"],
                     [str(hermes), "cron", "remove", "abcdef"]]


@pytest.mark.parametrize("contents", ["broken", "[]", '{"cron":null}', '{"cron":[null]}'])
def test_unusable_manifest_does_not_turn_basename_into_ownership(
        rec, tmp_path, monkeypatch, contents):
    hermes, calls, _ = _backup_membership(rec, tmp_path, monkeypatch, proof="none")
    Path(rec.mcs_recovery_core.MANIFEST_PATH).write_text(contents)
    rec.mcs_recovery_services._reconcile_membership({"cron": [], "agents": []})
    assert calls == [[str(hermes), "cron", "list", "--all"]]


def test_failed_job_list_never_retires_optional_backup(rec, tmp_path, monkeypatch):
    hermes, calls, _ = _backup_membership(rec, tmp_path, monkeypatch, returncode=1)
    assert "cron_list_unverifiable" in rec.mcs_recovery_services._reconcile_membership({})
    assert calls == [[str(hermes), "cron", "list", "--all"]]


@pytest.mark.parametrize("backup", [None, [], {}, {"enabled": "false"}])
def test_unknown_backup_intent_holds_owned_job(rec, tmp_path, monkeypatch, backup):
    hermes, calls, _ = _backup_membership(rec, tmp_path, monkeypatch)
    Path(rec.mcs_recovery_core.DATA).parent.joinpath("config.json").write_text(json.dumps({"backup": backup}))
    assert "backup_config_unverifiable" in rec.mcs_recovery_services._reconcile_membership({})
    assert calls == [[str(hermes), "cron", "list", "--all"]]


def test_backup_manifest_never_adds_external_scheduler_to_standalone_host(
        rec, tmp_path, monkeypatch):
    _standalone_recovery(rec)
    Path(rec.mcs_recovery_core.MANIFEST_PATH).write_text(json.dumps(
        {"cron": [{"id": "abcdef", "script": "mcs_offsite.sh"}]}))
    setup = Path(rec.mcs_recovery_core.REPO, "mcs/ops/mcs_setup.py")
    setup.parent.mkdir(parents=True)
    setup.write_text("# synthetic services entry: never executed")
    monkeypatch.setattr(rec.mcs_recovery_services, "_setup_python", lambda: "/synthetic/python")
    calls = []
    def run(argv, *args, **kw):
        assert argv == ["/synthetic/python", str(setup), "services"]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(rec.mcs_recovery_core, "_run_owned", run)
    assert rec.mcs_recovery_services._reconcile_membership({"cron": [], "agents": []}) == []
    assert calls == [["/synthetic/python", str(setup), "services"]]


def test_standalone_recovery_uses_own_python_and_one_scheduler(rec, tmp_path, monkeypatch):
    _standalone_recovery(rec)
    exe = tmp_path / "venv/bin/python3"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o700)
    setup = tmp_path / "repo/mcs/ops/mcs_setup.py"
    setup.parent.mkdir(parents=True)
    setup.write_text("# fixture")
    calls = []
    monkeypatch.setattr(rec.mcs_recovery_core, "_run_owned", lambda argv, *args, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert rec.mcs_recovery_services._setup_python() == str(exe)
    assert rec.mcs_recovery_services._reconcile_membership({"agents": [], "cron": []}) == []
    assert calls == [[str(exe), str(setup), "services"]]


def test_standalone_recovery_restores_drainers_and_requests_restart(rec, monkeypatch):
    value = _standalone_recovery(rec)
    monkeypatch.setattr(rec.mcs_recovery_services, "_launchctl", lambda *a, **k: pytest.fail("native host must not be killed"))
    Path(rec.mcs_recovery_core.MARKER_PATH).write_text("marker")
    assert rec.mcs_recovery_services._restart_drainers() == []
    assert not Path(rec.mcs_recovery_core.MARKER_PATH).exists()
    rec.mcs_recovery_services._restart_gateway()
    path = Path(rec.mcs_recovery_core.DATA, "standalone-restart.request")
    assert json.loads(path.read_text())["generation"] == value["generation"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_standalone_recovery_does_not_restart_an_unverified_host(rec):
    _standalone_recovery(rec)
    Path(rec.mcs_recovery_core.DATA, "standalone-status.json").unlink()
    rec.mcs_recovery_services._restart_gateway()
    assert not Path(rec.mcs_recovery_core.DATA, "standalone-restart.request").exists()


def test_reconcile_uses_install_interpreter_not_watchdog_python(
        rec, tmp_path, monkeypatch):
    """Services use the install-time venv, independent of watchdog Python."""
    setup = tmp_path / "repo" / "mcs" / "ops" / "mcs_setup.py"
    setup.parent.mkdir(parents=True)
    setup.write_text("# synthetic")
    venv_py = tmp_path / "venv-python"
    venv_py.write_text("#!/bin/sh\nexit 0\n")
    venv_py.chmod(0o755)
    monkeypatch.setattr(rec.mcs_recovery_core, "HERMES_PY", str(venv_py))
    calls = []

    def run(args, *extra, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rec.mcs_recovery_core, "_run_owned", run)
    assert rec.mcs_recovery_services._reconcile_membership(None) == ["cron_list_unverifiable"]
    assert [str(venv_py), str(setup), "services"] in calls


def test_reconcile_without_capable_interpreter_is_unavailable(
        rec, tmp_path, monkeypatch):
    setup = tmp_path / "repo" / "mcs" / "ops" / "mcs_setup.py"
    setup.parent.mkdir(parents=True)
    setup.write_text("# synthetic")
    monkeypatch.setattr(rec.mcs_recovery_core, "HERMES_PY", str(tmp_path / "absent"))
    monkeypatch.setattr(rec.sys, "version_info", (3, 9, 6))
    assert "services_reconcile_unavailable" in \
        rec.mcs_recovery_services._reconcile_membership(None)


def test_hung_launchctl_is_reported_not_raised(rec, monkeypatch, tmp_path):
    def hung(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

    monkeypatch.setattr(rec.subprocess, "run", hung)
    monkeypatch.setattr(rec.mcs_recovery_core, "RESIDENT_LABELS", ("ai.mcs.extract-drainer",))
    clock = iter(range(0, 10_000, 20))
    monkeypatch.setattr(rec.mcs_recovery_services, "time", SimpleNamespace(
        time=time.time, monotonic=lambda: next(clock), sleep=lambda s: None))
    assert rec.mcs_recovery_services._restart_drainers() == ["ai.mcs.extract-drainer"]


def test_restart_drainers_wait_ignores_wall_clock_jump(rec, monkeypatch):
    """A sleep/NTP jump of the wall clock must not expire the 15s pid wait."""
    wall, mono, pids = [1000.0], [0.0], iter([None, None, 4242, 4242])

    def sleep(s):
        mono[0] += s
        wall[0] += 3600                # system clock jumps an hour per poll
    monkeypatch.setattr(rec.mcs_recovery_services, "time", SimpleNamespace(
        time=lambda: wall[0], monotonic=lambda: mono[0], sleep=sleep))
    monkeypatch.setattr(rec.mcs_recovery_services, "_launchctl", lambda args: None)
    monkeypatch.setattr(rec.mcs_recovery_services, "_agent_pid", lambda label, unknown=None: next(pids))
    monkeypatch.setattr(rec.mcs_recovery_core, "RESIDENT_LABELS", ("ai.mcs.x",))
    assert rec.mcs_recovery_services._restart_drainers() == []


@pytest.mark.parametrize("outcomes,late,problems,boots", [
    ([5, 0], False, [], 2),
    ([5, 5, 5], True, [], 3),
    ([5, 5, 5], False, ["ai.mcs.x"], 3),
    # exit 0 is not proof: the label must answer `print` afterwards
    ([0], False, ["ai.mcs.x"], 1),
])
def test_restart_drainers_retries_transient_bootstrap(
        rec, tmp_path, monkeypatch, outcomes, late, problems, boots):
    fake = _FakeLaunchd(outcomes, late_load=late, loads=outcomes != [0])
    sleeps = []
    clock = iter(range(0, 10 ** 6, 5))
    monkeypatch.setattr(rec.subprocess, "run", fake)
    monkeypatch.setattr(rec.mcs_recovery_services, "time", SimpleNamespace(
        time=time.time, monotonic=lambda: next(clock), sleep=sleeps.append))
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "ai.mcs.x.plist").write_text("<plist/>")
    monkeypatch.setattr(rec.mcs_recovery_core, "RESIDENT_LABELS", ("ai.mcs.x",))
    assert rec.mcs_recovery_services._restart_drainers() == problems
    assert fake.calls[0] == "bootout"
    assert fake.calls.count("bootstrap") == boots
    assert sleeps.count(1) == sum(rc != 0 for rc in outcomes)


# ---------- repeated escalation ensures drainers (= mcs_update)

def test_repeated_escalation_ensures_drainers_instead_of_bouncing(
        rec, tmp_path, monkeypatch):
    """The 900s watchdog re-escalates the same stuck journal: bounce
    only the first time, later passes only ensure drainers run (the old
    code bootout+bootstrapped healthy drainers every pass)."""
    _make_repo(tmp_path)
    calls = []
    monkeypatch.setattr(rec.mcs_recovery_services, "_restart_drainers",
                        lambda **k: calls.append(k.get("bounce", True))
                        or [])
    state = _applying("0" * 40, stage="merge")      # unclassifiable
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    for _ in range(3):
        assert rec.recover() == 1
    assert calls == [True, False, False]
    state["applying"]["tag"] = "v1.2.0"
    with open(rec.mcs_recovery_core.STATE_PATH, "w") as f:
        json.dump(state, f)
    assert rec.recover() == 1
    assert calls[-1] is True


def test_restart_drainers_ensure_mode_never_bounces_a_running_drainer(
        rec, tmp_path, monkeypatch):
    fake = _EnsureLaunchd(loaded={"ai.mcs.run", "ai.mcs.stopped"},
                          running={"ai.mcs.run"}, hung={"ai.mcs.hung"})
    monkeypatch.setattr(rec.subprocess, "run", fake)
    monkeypatch.setattr(rec.time, "sleep", lambda s: None)
    agents = tmp_path / "agents"
    agents.mkdir()
    labels = ("ai.mcs.run", "ai.mcs.stopped", "ai.mcs.gone", "ai.mcs.hung")
    for label in labels:
        (agents / (label + ".plist")).write_text("<plist/>")
    monkeypatch.setattr(rec.mcs_recovery_core, "RESIDENT_LABELS", labels)
    clock = iter(range(0, 10 ** 6))
    monkeypatch.setattr(rec.mcs_recovery_services, "time", SimpleNamespace(
        time=time.time, monotonic=lambda: next(clock), sleep=lambda s: None))
    assert rec.mcs_recovery_services._restart_drainers(bounce=False) == ["ai.mcs.hung"]
    assert not any(verb == "bootout" for verb, _ in fake.calls)
    assert [c for c in fake.calls if c[1] == "ai.mcs.run"] \
        == [("print", "ai.mcs.run")]
    assert ("kickstart", "ai.mcs.stopped") in fake.calls
    assert ("bootstrap", "ai.mcs.stopped") not in fake.calls
    assert ("bootstrap", "ai.mcs.gone") in fake.calls
    assert ("bootstrap", "ai.mcs.hung") in fake.calls   # fail closed


def test_drainers_key_matches_updater():
    import mcs_update
    recovery = _load()
    state = _applying("p" * 40)
    for head in ("h" * 40, "", None):
        assert recovery.mcs_recovery_core._drainers_key(state, head) \
            == mcs_update._drainers_key(state, head)


def test_notify_kills_a_wedged_standalone_sender(tmp_path, monkeypatch):
    """communicate()'s 60s cap must reap the child — a wedged sender
    must not outlive the watchdog."""
    mod = _load()
    monkeypatch.setattr(mod.mcs_recovery_core, "HOME", str(tmp_path))
    monkeypatch.setattr(mod.mcs_recovery_core, "DATA", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    (repo / "mcs_standalone").mkdir(parents=True)
    (repo / "mcs_standalone" / "__main__.py").write_text("#")
    monkeypatch.setattr(mod.mcs_recovery_core, "REPO", str(repo))
    python = tmp_path / "venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("#!")
    python.chmod(0o755)
    (tmp_path / "config.json").write_text(json.dumps(
        {"runtime_mode": "standalone", "notify_target": "slack:C0SYNTH"}))
    calls = []

    class Wedged:
        def communicate(self, *a, **k):
            raise subprocess.TimeoutExpired("send", 60)

        def kill(self):
            calls.append("kill")

        def wait(self):
            calls.append("wait")

    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: Wedged())
    mod.mcs_recovery_services._notify("synthetic")
    assert calls == ["kill", "wait"]


def test_notify_refuses_a_symlinked_standalone_config(tmp_path, monkeypatch):
    """_notify reads config through _runtime_config's symlink guard —
    a symlinked config.json must not drive any sender."""
    mod = _load()
    monkeypatch.setattr(mod.mcs_recovery_core, "HOME", str(tmp_path))
    monkeypatch.setattr(mod.mcs_recovery_core, "DATA", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    (repo / "mcs_standalone").mkdir(parents=True)
    (repo / "mcs_standalone" / "__main__.py").write_text("#")
    monkeypatch.setattr(mod.mcs_recovery_core, "REPO", str(repo))
    python = tmp_path / "venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("#!")
    python.chmod(0o755)
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(
        {"runtime_mode": "standalone", "notify_target": "slack:C0SYNTH"}))
    (tmp_path / "config.json").symlink_to(real)
    calls = []
    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda *a, **k: calls.append(a) or None)
    mod.mcs_recovery_services._notify("synthetic")
    assert calls == []


@pytest.mark.parametrize("print_code", [1, 124])
def test_membership_keeps_an_agent_plist_launchd_did_not_report_gone(
        rec, tmp_path, monkeypatch, print_code):
    agents = tmp_path / "agents"
    agents.mkdir()
    plist = agents / "ai.mcs.extract-old.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(rec.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(
        args, print_code if args[1] == "print" else 0, ""))
    problems = rec.mcs_recovery_services._reconcile_membership({"agents": [], "cron": []})
    assert plist.exists()
    assert "agent_retirement_unverifiable:ai.mcs.extract-old" in problems
