"""Synthetic mcs update services contracts; shared helpers preserve isolated runtime state."""
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import update_services
import service_contract
import service_management
import owned_process
import runtime_inspection
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
import mcs_update
import mcs_util
from ops_testkit import _FakeLaunchd
from mcs_update_testkit import _EnsureLaunchd as _EnsureLaunchd, _HANG_EXCS as _HANG_EXCS, _HungLaunchd as _HungLaunchd, _standalone_heartbeat as _standalone_heartbeat, prepared_run_log as prepared_run_log, updater as updater


def test_standalone_quiesce_and_restart_keep_the_host_alive(updater, monkeypatch):
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {"runtime_mode": "standalone"})
    (Path(updater.DATA).parent / "config.json").write_text('{"runtime_mode":"standalone"}')
    _standalone_heartbeat(updater, update_in_progress=True, children={"update": {"pid": os.getpid(), "kind": "core"}})
    monkeypatch.setattr(updater, "_run", lambda *a, **k: pytest.fail("native service must not be stopped"))
    monkeypatch.setattr(update_services, '_run', updater._run)
    assert updater.quiesce() == ["extract-0", "extract-2", "extract-night"]
    assert Path(updater.MARKER_PATH).exists()
    _standalone_heartbeat(updater)
    assert updater.restart_agents() == []
    assert not Path(updater.MARKER_PATH).exists()


def test_standalone_quiesce_fails_closed_on_missing_host(updater, monkeypatch):
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {"runtime_mode": "standalone"})
    ticks = iter([0, 21])
    monkeypatch.setattr(updater.time, "monotonic", lambda: next(ticks))
    with pytest.raises(updater.UpdateError, match="standalone_quiesce_unverifiable"):
        updater.quiesce()


@pytest.mark.parametrize("outcomes,late,problems,boots,loads", [
    ([5, 0], False, [], 2, True),
    ([5, 5, 5], True, [], 3, True),
    ([5, 5, 5], False, ["bootstrap_failed:ai.mcs.x"], 3, True),
    # exit 0 is not proof: the label must answer `print` afterwards
    ([0], False, ["bootstrap_failed:ai.mcs.x"], 1, False),
])
def test_restart_agents_retries_transient_bootstrap(
        tmp_path, monkeypatch, outcomes, late, problems, boots, loads):
    from types import SimpleNamespace
    fake = _FakeLaunchd(outcomes, late_load=late, loads=loads)
    sleeps = []
    monkeypatch.setattr(mcs_update.subprocess, "run", fake)
    monkeypatch.setattr(service_management, "time",
                        SimpleNamespace(time=time.time, sleep=sleeps.append))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(update_lifecycle_context, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_preflight, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_services, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ("ai.mcs.x",))
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_agent_pid",
                        lambda label, unknown=None: 4242 if fake.loaded else None)
    monkeypatch.setattr(update_execution, '_agent_pid', mcs_update._agent_pid)
    monkeypatch.setattr(update_services, '_agent_pid', mcs_update._agent_pid)
    monkeypatch.setattr(mcs_update, "_remove_marker", lambda: None)
    monkeypatch.setattr(update_apply, '_remove_marker', mcs_update._remove_marker)
    monkeypatch.setattr(update_recovery, '_remove_marker', mcs_update._remove_marker)
    monkeypatch.setattr(update_services, '_remove_marker', mcs_update._remove_marker)
    assert mcs_update.restart_agents() == problems
    assert fake.calls[0] == "bootout"
    assert fake.calls.count("bootstrap") == boots
    assert sleeps.count(1) == sum(rc != 0 for rc in outcomes)


@pytest.mark.parametrize("outcomes,late,loads,problems", [
    ([5, 0], False, True, []),
    ([5, 5, 5], True, True, []),
    ([5, 5, 5], False, True, ["watcher_not_loaded:ai.mcs.w"]),
    ([0], False, False, ["watcher_not_loaded:ai.mcs.w"]),
])
def test_restart_agents_watcher_uses_verified_bootstrap(
        tmp_path, monkeypatch, outcomes, late, loads, problems):
    """An unloaded watcher goes through the shared verified bootstrap —
    its result decides, not a second ad-hoc `print`."""
    from types import SimpleNamespace
    fake = _FakeLaunchd(outcomes, late_load=late, loads=loads)
    monkeypatch.setattr(mcs_update.subprocess, "run", fake)
    monkeypatch.setattr(service_management, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(update_lifecycle_context, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_preflight, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_services, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ("ai.mcs.w",))
    monkeypatch.setattr(mcs_update, "_remove_marker", lambda: None)
    monkeypatch.setattr(update_apply, '_remove_marker', mcs_update._remove_marker)
    monkeypatch.setattr(update_recovery, '_remove_marker', mcs_update._remove_marker)
    monkeypatch.setattr(update_services, '_remove_marker', mcs_update._remove_marker)
    assert mcs_update.restart_agents() == problems
    assert fake.calls == ["print"] + ["bootstrap"] * len(outcomes) + ["print"]


@pytest.mark.parametrize("exc", _HANG_EXCS)
@pytest.mark.parametrize("label,verb,problems", [
    ("ai.mcs.a", "bootout", []),
    ("ai.mcs.a", "bootstrap", ["bootstrap_failed:ai.mcs.a"]),
    ("ai.mcs.a", "print", ["drainer_stop_failed:ai.mcs.a"]),
    ("local.mcs-w", "print", ["watcher_not_loaded:local.mcs-w"]),
    ("local.mcs-w", "bootstrap", ["watcher_not_loaded:local.mcs-w"]),
])
def test_restart_agents_survives_hung_launchctl(
        updater, tmp_path, monkeypatch, label, verb, problems, exc):
    from types import SimpleNamespace
    fake = _HungLaunchd(label, verb, exc)
    monkeypatch.setattr(mcs_update.subprocess, "run", fake)
    monkeypatch.setattr(service_management, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(update_lifecycle_context, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_preflight, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_services, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS',
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ("local.mcs-w",))
    Path(mcs_update.MARKER_PATH).write_text("1")
    assert mcs_update.restart_agents() == problems
    if problems == ["drainer_stop_failed:ai.mcs.a"]:
        assert not any(verb == "bootstrap" for verb, _ in fake.calls)
        assert os.path.exists(mcs_update.MARKER_PATH)
    else:
        assert ("bootstrap", "ai.mcs.b") in fake.calls
        assert fake.calls[-1][1] == "local.mcs-w"
        assert not os.path.exists(mcs_update.MARKER_PATH)


def test_launchd_waits_ignore_wall_clock_jump(updater, monkeypatch):
    """quiesce/restart_agents pid waits run on the monotonic clock: a
    sleep/NTP jump of time.time() must not end them on the first poll."""
    from types import SimpleNamespace
    wall, mono = [1000.0], [0.0]

    def sleep(s):
        mono[0] += s
        wall[0] += 3600                # system clock jumps an hour per poll
    monkeypatch.setattr(mcs_update, "time", SimpleNamespace(
        time=lambda: wall[0], monotonic=lambda: mono[0], sleep=sleep))
    monkeypatch.setattr(update_apply, 'time', mcs_update.time)
    monkeypatch.setattr(update_cli, 'time', mcs_update.time)
    monkeypatch.setattr(update_execution, 'time', mcs_update.time)
    monkeypatch.setattr(update_lifecycle_context, 'time', mcs_update.time)
    monkeypatch.setattr(update_recovery, 'time', mcs_update.time)
    monkeypatch.setattr(update_repository, 'time', mcs_update.time)
    monkeypatch.setattr(update_restore, 'time', mcs_update.time)
    monkeypatch.setattr(update_runtime_migration, 'time', mcs_update.time)
    monkeypatch.setattr(update_services, 'time', mcs_update.time)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ("ai.mcs.a",))
    monkeypatch.setattr(service_contract, 'LEGACY_LABELS', ())
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    monkeypatch.setattr(mcs_update, "_run", lambda argv:
                        subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(update_services, '_run', mcs_update._run)
    monkeypatch.setattr(mcs_update, "_bootstrap_agent", lambda label, plist: True)
    monkeypatch.setattr(update_services, '_bootstrap_agent', mcs_update._bootstrap_agent)
    monkeypatch.setattr(mcs_update, "_stray_drainer_pids", lambda: [])
    monkeypatch.setattr(update_services, '_stray_drainer_pids', mcs_update._stray_drainer_pids)
    pids = iter([4242, 4242, None, None])
    monkeypatch.setattr(mcs_update, "_agent_pid", lambda label, unknown=None: next(pids))
    monkeypatch.setattr(update_execution, '_agent_pid', mcs_update._agent_pid)
    monkeypatch.setattr(update_services, '_agent_pid', mcs_update._agent_pid)
    assert mcs_update.quiesce() == ["ai.mcs.a"]
    pids = iter([None, None, 4343])
    assert mcs_update.restart_agents() == []


@pytest.mark.parametrize("exc", _HANG_EXCS)
def test_quiesce_never_takes_unverifiable_stop_as_stopped(
        updater, monkeypatch, exc):
    """A hung `print` cannot prove the drainer stopped — quiesce fails
    closed (drainer_stop_failed) instead of merging under a live one;
    a hung `bootout` alone is fine once `print` confirms the stop."""
    from types import SimpleNamespace
    clock = [1000.0]
    monkeypatch.setattr(mcs_update, "time", SimpleNamespace(
        time=lambda: clock[0], monotonic=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(update_apply, 'time', mcs_update.time)
    monkeypatch.setattr(update_cli, 'time', mcs_update.time)
    monkeypatch.setattr(update_execution, 'time', mcs_update.time)
    monkeypatch.setattr(update_lifecycle_context, 'time', mcs_update.time)
    monkeypatch.setattr(update_recovery, 'time', mcs_update.time)
    monkeypatch.setattr(update_repository, 'time', mcs_update.time)
    monkeypatch.setattr(update_restore, 'time', mcs_update.time)
    monkeypatch.setattr(update_runtime_migration, 'time', mcs_update.time)
    monkeypatch.setattr(update_services, 'time', mcs_update.time)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS',
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(mcs_update, "_stray_drainer_pids", lambda: [])
    monkeypatch.setattr(update_services, '_stray_drainer_pids', mcs_update._stray_drainer_pids)
    monkeypatch.setattr(mcs_update.subprocess, "run",
                        _HungLaunchd("ai.mcs.a", "bootout", exc))
    assert mcs_update.quiesce() == ["ai.mcs.a", "ai.mcs.b"]
    monkeypatch.setattr(mcs_update.subprocess, "run",
                        _HungLaunchd("ai.mcs.b", "print", exc))
    with pytest.raises(mcs_update.UpdateError,
                       match="drainer_stop_failed: ai.mcs.b"):
        mcs_update.quiesce()


@pytest.mark.parametrize("outcome", [
    subprocess.TimeoutExpired(["pgrep"], 30),
    FileNotFoundError("pgrep"),
    subprocess.CompletedProcess(["pgrep"], 3, "", "internal error")])
def test_quiesce_fails_closed_when_stray_check_unverifiable(
        updater, monkeypatch, outcome):
    def run(argv, *a, **k):
        assert argv[0] == "pgrep"
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    monkeypatch.setattr(mcs_update.subprocess, "run", run)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    assert mcs_update._stray_drainer_pids() is None
    with pytest.raises(mcs_update.UpdateError,
                       match="stray_drainer_unverifiable"):
        mcs_update.quiesce()


def test_stray_check_no_match_is_clean(updater, monkeypatch):
    monkeypatch.setattr(
        mcs_update.subprocess, "run",
        lambda argv, *a, **k: subprocess.CompletedProcess(argv, 1, "", ""))
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS', ())
    assert mcs_update._stray_drainer_pids() == []
    assert mcs_update.quiesce() == []


# ------------------------------------ bounded restart (T_POST_MERGE)

def test_restart_agents_all_hung_stays_within_budget(
        updater, tmp_path, monkeypatch):
    """Every launchctl call hangs until its timeout: restart_agents must
    finish far enough under T_POST_MERGE that the post-merge child (with
    services 120s + postcheck) is not killed mid-restart, reporting the
    labels it never reached."""
    from types import SimpleNamespace
    clock = [0.0]

    def advance(s):
        clock[0] += s

    def run(argv, *a, **k):
        advance(k["timeout"])
        raise subprocess.TimeoutExpired(argv, k["timeout"])
    fake_time = SimpleNamespace(time=lambda: clock[0],
                                monotonic=lambda: clock[0], sleep=advance)
    monkeypatch.setattr(mcs_update.subprocess, "run", run)
    monkeypatch.setattr(mcs_update, "time", fake_time)
    monkeypatch.setattr(update_apply, 'time', mcs_update.time)
    monkeypatch.setattr(update_cli, 'time', mcs_update.time)
    monkeypatch.setattr(update_execution, 'time', mcs_update.time)
    monkeypatch.setattr(update_lifecycle_context, 'time', mcs_update.time)
    monkeypatch.setattr(update_recovery, 'time', mcs_update.time)
    monkeypatch.setattr(update_repository, 'time', mcs_update.time)
    monkeypatch.setattr(update_restore, 'time', mcs_update.time)
    monkeypatch.setattr(update_runtime_migration, 'time', mcs_update.time)
    monkeypatch.setattr(update_services, 'time', mcs_update.time)
    monkeypatch.setattr(service_management, "time", fake_time)
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(update_lifecycle_context, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_preflight, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_services, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS',
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS',
                        ("local.mcs-w", "local.mcs-v"))
    problems = mcs_update.restart_agents()
    # worst case: a label started just before the budget runs out costs
    # 78s more; services 120 + postcheck 206 follow in the same child
    assert clock[0] <= mcs_update.RESTART_BUDGET_S + 78
    assert mcs_update.RESTART_BUDGET_S + 78 + 326 < mcs_update.T_POST_MERGE
    assert problems == ["drainer_stop_failed:ai.mcs.a",
                        "drainer_stop_failed:ai.mcs.b"]


def test_restart_agents_ensure_mode_never_bounces_a_running_drainer(
        updater, tmp_path, monkeypatch):
    from types import SimpleNamespace
    fake = _EnsureLaunchd(loaded={"ai.mcs.run", "ai.mcs.stopped"},
                          running={"ai.mcs.run"}, hung={"ai.mcs.hung"})
    monkeypatch.setattr(mcs_update.subprocess, "run", fake)
    monkeypatch.setattr(service_management, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(update_lifecycle_context, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_preflight, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_services, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS',
                        ("ai.mcs.run", "ai.mcs.stopped", "ai.mcs.gone",
                         "ai.mcs.hung"))
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    assert mcs_update.restart_agents(bounce=False) == [
        "bootstrap_failed:ai.mcs.hung"]
    assert not any(verb == "bootout" for verb, _ in fake.calls)
    assert [c for c in fake.calls if c[1] == "ai.mcs.run"] \
        == [("print", "ai.mcs.run")]                 # left alone
    assert ("kickstart", "ai.mcs.stopped") in fake.calls
    assert ("bootstrap", "ai.mcs.stopped") not in fake.calls
    assert ("bootstrap", "ai.mcs.gone") in fake.calls
    assert fake.running >= {"ai.mcs.run", "ai.mcs.stopped", "ai.mcs.gone"}
    # unverifiable is never read as running: a start was attempted
    assert ("bootstrap", "ai.mcs.hung") in fake.calls


def test_restart_agents_slow_drainer_does_not_starve_the_next(
        updater, tmp_path, monkeypatch):
    """The 15s pid wait used to overwrite the RESTART_BUDGET_S deadline:
    one drainer that never came up made every later label
    restart_deadline without even being started (H4)."""
    from types import SimpleNamespace
    fake = _EnsureLaunchd(inert={"ai.mcs.a"})
    clock = iter(range(0, 10 ** 6))
    monkeypatch.setattr(mcs_update.subprocess, "run", fake)
    monkeypatch.setattr(mcs_update, "time", SimpleNamespace(
        time=time.time, monotonic=lambda: next(clock), sleep=lambda s: None))
    monkeypatch.setattr(update_apply, 'time', mcs_update.time)
    monkeypatch.setattr(update_cli, 'time', mcs_update.time)
    monkeypatch.setattr(update_execution, 'time', mcs_update.time)
    monkeypatch.setattr(update_lifecycle_context, 'time', mcs_update.time)
    monkeypatch.setattr(update_recovery, 'time', mcs_update.time)
    monkeypatch.setattr(update_repository, 'time', mcs_update.time)
    monkeypatch.setattr(update_restore, 'time', mcs_update.time)
    monkeypatch.setattr(update_runtime_migration, 'time', mcs_update.time)
    monkeypatch.setattr(update_services, 'time', mcs_update.time)
    monkeypatch.setattr(service_management, "time",
                        SimpleNamespace(time=time.time, sleep=lambda s: None))
    monkeypatch.setattr(mcs_update, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(update_lifecycle_context, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_preflight, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(update_services, 'AGENTS_DIR', mcs_update.AGENTS_DIR)
    monkeypatch.setattr(service_contract, 'RESIDENT_LABELS',
                        ("ai.mcs.a", "ai.mcs.b"))
    monkeypatch.setattr(service_contract, 'WATCHER_LABELS', ())
    assert mcs_update.restart_agents() == ["drainer_not_running:ai.mcs.a"]
    assert "ai.mcs.b" in fake.running


@pytest.mark.parametrize("mode", ["hermes", "standalone"])
def test_quiesce_for_verified_mode_does_not_read_pending_config(updater, monkeypatch, mode):
    updater._ensure_recovery_marker()
    monkeypatch.setattr(mcs_util, "load_config", lambda: pytest.fail("pending config was read"))
    calls = []
    monkeypatch.setattr(service_contract, "LEGACY_LABELS", ())
    monkeypatch.setattr(service_contract, "RESIDENT_LABELS", ("synthetic.a", "synthetic.b"))
    monkeypatch.setattr(updater, "_run", lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
    monkeypatch.setattr(update_services, '_run', updater._run)
    monkeypatch.setattr(updater, "_agent_pid", lambda *_a, **_k: None)
    monkeypatch.setattr(update_execution, '_agent_pid', updater._agent_pid)
    monkeypatch.setattr(update_services, '_agent_pid', updater._agent_pid)
    monkeypatch.setattr(updater, "_stray_drainer_pids", lambda: [])
    monkeypatch.setattr(update_services, '_stray_drainer_pids', updater._stray_drainer_pids)
    monkeypatch.setattr(updater, "_standalone_status", lambda: {
        "update_in_progress": True, "children": {"update": {"pid": 123}}})
    monkeypatch.setattr(update_services, '_standalone_status', updater._standalone_status)
    stopped = updater._quiesce_for_mode(mode)
    assert Path(updater.MARKER_PATH).exists()
    if mode == "standalone":
        assert stopped == ["extract-0", "extract-2", "extract-night"] and not calls
    else:
        assert stopped == ["synthetic.a", "synthetic.b"]
        assert [argv[0:2] for argv in calls] == [["launchctl", "bootout"]] * 2
        assert [argv[-1].rsplit("/", 1)[-1] for argv in calls] == stopped


@pytest.mark.parametrize("print_code", [113, 1, 124])
def test_membership_removes_an_agent_plist_only_after_launchd_says_gone(
        updater, tmp_path, monkeypatch, print_code):
    agents = tmp_path / "update-agents"
    agents.mkdir()
    plist = agents / "ai.mcs.extract-retired.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(updater, "AGENTS_DIR", str(agents))
    monkeypatch.setattr(update_lifecycle_context, 'AGENTS_DIR', updater.AGENTS_DIR)
    monkeypatch.setattr(update_preflight, 'AGENTS_DIR', updater.AGENTS_DIR)
    monkeypatch.setattr(update_services, 'AGENTS_DIR', updater.AGENTS_DIR)
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda path: False)
    monkeypatch.setattr(updater.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, print_code if argv[1] == "print" else 0, "", ""))
    monkeypatch.setattr(owned_process, "run_owned", lambda argv, **kw: subprocess.CompletedProcess(
        argv, print_code if argv[1] == "print" else 0, "", ""))
    problems = service_management.retire_to_snapshot({'cron': [], 'agents': []}, cfg=mcs_util.load_config(), paths=updater._service_paths())
    gone = print_code == 113
    assert plist.exists() is not gone
    assert ("agent_retirement_unverifiable:ai.mcs.extract-retired" in problems) is not gone


@pytest.mark.parametrize(("code", "expected"), [(113, None), (1, "unknown"), (124, "unknown")])
def test_agent_pid_treats_only_not_found_as_stopped(updater, monkeypatch, code, expected):
    monkeypatch.setattr(updater.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, code, "", ""))
    assert updater._agent_pid("ai.mcs.synthetic", unknown="unknown") == expected


def test_stopping_an_update_child_closes_its_pipes(updater):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    real = child.communicate

    def interrupted(*a, **k):        # reaping must still close the pipes
        raise subprocess.TimeoutExpired(child.args, 5)
    child.communicate = interrupted
    updater._stop_update_child(child)
    child.communicate = real
    assert child.returncode is not None
    assert child.stdout.closed and child.stderr.closed
