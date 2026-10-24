"""Standalone host supervision with stubbed children, locks and clocks (synthetic only)."""
import asyncio
from datetime import datetime, timezone
import errno
import fcntl
import json
import io
import plistlib
import pytest
from types import SimpleNamespace

from mcs_standalone import runtime, service
from writer_lock import UPDATE_MARKER_NAME

# 05:11 UTC: no calendar slot of the default jobs falls on this minute
QUIET = datetime(2026, 10, 2, 5, 11, tzinfo=timezone.utc).timestamp()


class Child:
    def __init__(self, pid=1001):
        self.pid, self.code, self.signals = pid, None, []

    def poll(self):
        return self.code

    def terminate(self):
        self.signals.append("term")

    def kill(self):
        self.signals.append("kill")


def _host(tmp_path, monkeypatch, events=None):
    (tmp_path / "data").mkdir(exist_ok=True)
    if events is not None:
        monkeypatch.setattr(runtime, "log", lambda event, **f: events.append((event, f)))
    # never a real child process: tests that count starts patch this again
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda argv, **kw: Child())
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    monkeypatch.setattr(host, "_start_identity", lambda: None)
    return host


def _complete_identity(host, monkeypatch, job, identity, now, operation=None):
    """Run the isolated-worker protocol on synthetic I/O, then apply its fenced result."""
    pid = host.orphan_candidates.get(job) or host.children[job]["process"].pid
    request = {"pid": pid, "argv": list(host.argv[job]), "operation": int(operation) if operation else None,
               "generation": host.generation, "owner_pid": runtime.os.getpid()}
    output = io.StringIO()
    with monkeypatch.context() as worker:
        if identity is True or identity is False:
            found = (1, " ".join(request["argv"]) if identity else "synthetic-other-process")
            worker.setattr(runtime, "_process_args", lambda _pid: found)
        elif identity == "exited" or isinstance(identity, tuple):
            # Only inside the isolated worker: exercise exit/self-parent classification.
            worker.setattr(runtime, "_process_args", lambda _pid: None if identity == "exited" else identity)
        else:
            worker.setattr(runtime, "_is_orphan", lambda pid, argv, owner_pid: identity)
        worker.setattr(runtime.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())))
        worker.setattr(runtime.sys, "stdout", output)
        runtime._identity_worker()
    result = json.loads(output.getvalue())
    host._identity_result({**request, "job": job}, result, now)
    return result


# ---------------------------------------------------------------- H1 connector

def test_connector_failure_backs_off_without_stopping_the_host(tmp_path, monkeypatch):
    cfg = {"runtime_mode": "standalone"}
    clock = [QUIET]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    monkeypatch.setattr(runtime.config, "load", lambda root: cfg)
    started = []
    monkeypatch.setattr(runtime.subprocess, "Popen",
                        lambda argv, **kw: started.append(argv) or Child())
    events, ticks, attempts = [], [], []
    monkeypatch.setattr(runtime, "log", lambda event, **f: events.append((event, f)))
    original_tick = runtime.Runtime.tick

    def tick(self, now=None, **kwargs):
        ticks.append(self)
        if len(ticks) == 400:
            self.restarting = True     # the test ends the host like a signal
            for row in self.children.values():
                row["process"].code = 0
        result = original_tick(self, now, **kwargs)
        clock[0] += 10                 # each loop pass advances 10 s
        return result

    monkeypatch.setattr(runtime.Runtime, "tick", tick)
    sleep = asyncio.sleep

    async def fast_sleep(*args):
        await sleep(0)

    monkeypatch.setattr(runtime.asyncio, "sleep", fast_sleep)

    async def connector(root, stopping, *, native_admission, delivery_admission, delivery_identity):
        attempts.append(clock[0])
        if len(attempts) == 3:
            raise ValueError("slack_configuration_changed_restart_required")
        raise ValueError("slack_identity_unverified")

    assert asyncio.run(runtime.serve(tmp_path, cfg, connector)) == 0
    host = ticks[0]
    # workers kept running: started once, never restarted with the connector
    assert sum(1 for argv in started if argv == host.argv["extract-0"]) == 1
    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    # detected on the next 10 s pass, then 30 s, 60 s; a settings change
    # reconnects at once; then 120 s
    assert gaps[:4] == [40, 70, 10, 130]
    assert all(gap <= runtime.BACKOFF_CAP_S + 10 for gap in gaps)
    ended = [f for e, f in events if e == "connector_ended"]
    assert ended[0] == {"code": "slack_identity_unverified", "failures": 1, "retry_in": 30}
    assert ended[2]["retry_in"] == 0
    assert not any(e == "child_ended" and f["job"] == "extract-0" and f["code"]
                   for e, f in events)


def test_connector_status_and_stable_reset(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch, [])
    failed = SimpleNamespace(cancelled=lambda: False, exception=lambda: RuntimeError("Bad token text"))
    host.connector_started(100)
    host.connector_ended(failed, 101)
    assert host.connector["last_error"] == "RuntimeError"   # free text is never logged
    assert host.connector["failing_since"] == 101 and host.connector["retry_at"] == 131
    host.connector_started(131)
    host.connector_running(131 + runtime.CONNECTOR_STABLE_S)
    assert host.connector["failures"] == 0 and host.connector["failing_since"] is None
    cancelled = SimpleNamespace(cancelled=lambda: True, exception=lambda: None)
    host.connector_ended(cancelled, 2000)
    assert host.connector["last_error"] == "cancelled" and host.connector["retry_at"] == 2030
    host.tick(QUIET)
    snapshot = json.loads((tmp_path / "data" / service.STATUS_FILE).read_text())
    assert snapshot["connector"]["state"] == "waiting" and "started_at" not in snapshot["connector"]


# ------------------------------------------------------- H2 child crash backoff

def test_child_crash_restarts_back_off_exponentially_to_a_cap(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch, [])
    delays = []
    for _ in range(8):
        host.children["extract-0"] = {"process": SimpleNamespace(pid=1, poll=lambda: 1),
                                      "kind": "background", "stopping_at": None}
        host.tick(QUIET, draining=True)
        delays.append(host.retry_at["extract-0"] - QUIET)
    assert delays == [30, 60, 120, 240, 480, 960, 1800, 1800]
    host.children["extract-0"] = {"process": SimpleNamespace(pid=1, poll=lambda: 0),
                                  "kind": "background", "stopping_at": None}
    host.tick(QUIET + 5, draining=True)
    row = host.exits["extract-0"]
    assert row["failures"] == 0 and row["last_ok_at"] == QUIET + 5
    assert host.retry_at["extract-0"] == QUIET + 35


# ------------------------------------------------- status write and heartbeat

def test_status_is_written_on_change_or_heartbeat_and_survives_enospc(tmp_path, monkeypatch):
    events = []
    host = _host(tmp_path, monkeypatch, events)
    monkeypatch.setattr(host, "schedules", [])
    monkeypatch.setattr(runtime, "BACKGROUND", {})
    writes = []
    real = runtime.atomic_write
    monkeypatch.setattr(runtime, "atomic_write", lambda path, *a, **k: writes.append(path) or real(path, *a, **k))
    for second in range(12):
        host.tick(QUIET + second)
    status = [p for p in writes if p.endswith(service.STATUS_FILE)]
    assert len(status) == 3          # first write, then a heartbeat every 5 s

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "synthetic full disk")

    monkeypatch.setattr(runtime, "atomic_write", full)
    host.children["cmd"] = {"process": Child(), "kind": "core", "stopping_at": None}
    for second in range(12, 20):
        host.tick(QUIET + second)    # never raises: no crash loop on a full disk
    failures = [f for e, f in events if e == "state_write_failed"]
    assert failures == [{"file": service.STATUS_FILE, "code": "ENOSPC"}]
    monkeypatch.setattr(runtime, "atomic_write", real)
    host.tick(QUIET + 20)
    assert ("state_write_recovered", {"file": service.STATUS_FILE}) in events
    assert "cmd" in json.loads((tmp_path / "data" / service.STATUS_FILE).read_text())["children"]


# --------------------------------------------------------- M8 update marker

def test_orphan_update_marker_is_cleared_after_the_probe_delay(tmp_path, monkeypatch):
    events = []
    host = _host(tmp_path, monkeypatch, events)
    marker = tmp_path / "data" / UPDATE_MARKER_NAME
    marker.write_text("synthetic")
    import os
    os.utime(marker, (QUIET, QUIET))
    assert host._updating(QUIET + 10) is True        # too young to judge
    assert marker.exists()
    assert host._updating(QUIET + runtime.MARKER_PROBE_S) is False
    assert not marker.exists()
    assert any(e == "update_marker_orphan_cleared" for e, _ in events)


def test_marker_with_pending_journal_or_live_updater_is_kept(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch, [])
    data = tmp_path / "data"
    marker = data / UPDATE_MARKER_NAME
    marker.write_text("synthetic")
    import os
    os.utime(marker, (QUIET, QUIET))
    (data / "update_state.json").write_text(json.dumps(
        {"v": 1, "stages": [{"stage": "merge", "at": QUIET}], "applying": {"at": QUIET}}))
    assert host._updating(QUIET + 3600) is True
    assert marker.exists() and host.marker["state"] == "journal_pending"
    (data / "update_state.json").write_text("{not json")
    host.marker["probed_at"] = None
    assert host._updating(QUIET + 3700) is True and host.marker["state"] == "journal_unreadable"
    # parseable, nothing staged, but a shape the updater itself rejects
    for corrupt in ({"v": 1, "stages": [], "applying": None, "applied": "x"},
                    {"v": 1, "stages": [], "applying": None, "restore_consent": []}):
        (data / "update_state.json").write_text(json.dumps(corrupt))
        host.marker["probed_at"] = None
        assert host._updating(QUIET + 3700) is True
        assert host.marker["state"] == "journal_unreadable"
    (data / "update_state.json").unlink()
    with open(data / "update.lock", "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        host.marker["probed_at"] = None
        assert host._updating(QUIET + 3800) is True and host.marker["state"] == "updater_alive"
    assert marker.exists()
    # probes are rate limited; the status file reports the marker's age and state
    host.tick(QUIET + 3810)
    snapshot = json.loads((data / service.STATUS_FILE).read_text())
    assert snapshot["update_in_progress"] is True
    assert snapshot["update_marker"]["since"] == QUIET


def test_own_running_updater_keeps_the_marker_without_probing(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch, [])
    marker = tmp_path / "data" / UPDATE_MARKER_NAME
    marker.write_text("synthetic")
    import os
    os.utime(marker, (QUIET, QUIET))
    host.children["update"] = {"process": Child(), "kind": "core", "stopping_at": None}
    monkeypatch.setattr(host, "_probe_marker", lambda path: (_ for _ in ()).throw(AssertionError))
    assert host._updating(QUIET + 7200) is True and marker.exists()


# ------------------------------------------------------- M9 missed slots

def test_missed_slot_runs_one_catch_up_after_a_restart(tmp_path, monkeypatch):
    events = []
    started = []
    host = _host(tmp_path, monkeypatch, events)
    monkeypatch.setattr(runtime.subprocess, "Popen",
                        lambda argv, **kw: started.append(argv) or Child())
    monkeypatch.setattr(runtime, "BACKGROUND", {})
    monkeypatch.setattr(runtime.time, "localtime", lambda stamp:
                        SimpleNamespace(tm_hour=12, tm_min=int(stamp // 60) % 60))
    host.schedules = [("mcs_check", [{"Minute": m} for m in range(0, 60, 10)])]
    host.tick(600)                    # minute 10: fires normally
    assert len(started) == 1
    host.children["mcs_check"]["process"].code = 0
    host.tick(660)
    # the host is down from minute 11 until minute 35: the 20 and 30 slots are missed
    later = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    later.schedules = host.schedules
    assert later.fired == {"mcs_check": 600}
    later.tick(35 * 60)
    assert len(started) == 2          # one catch-up, not one per missed slot
    assert ("slot_catchup", {"job": "mcs_check", "slot": 1800}) in events
    later.tick(36 * 60)
    assert len(started) == 2


def test_slot_while_previous_run_is_still_going_is_logged_then_caught_up(tmp_path, monkeypatch):
    events = []
    started = []
    host = _host(tmp_path, monkeypatch, events)
    monkeypatch.setattr(runtime.subprocess, "Popen",
                        lambda argv, **kw: started.append(argv) or Child())
    monkeypatch.setattr(runtime, "BACKGROUND", {})
    monkeypatch.setattr(runtime.time, "localtime", lambda stamp:
                        SimpleNamespace(tm_hour=12, tm_min=int(stamp // 60) % 60))
    host.schedules = [("mcs_check", [{"Minute": m} for m in range(0, 60, 10)])]
    host.tick(600)
    host.tick(1200)                   # minute 20: minute 10's run is still going
    host.tick(1230)
    skipped = [f for e, f in events if e == "slot_skipped_running"]
    assert skipped == [{"job": "mcs_check", "slot": 1200}] and len(started) == 1
    host.children["mcs_check"]["process"].code = 0
    host.tick(1290)                   # ended: the skipped slot runs once
    host.tick(1291)
    assert len(started) == 2 and ("slot_catchup", {"job": "mcs_check", "slot": 1200}) in events


def test_first_start_does_not_replay_old_slots(tmp_path, monkeypatch):
    started = []
    host = _host(tmp_path, monkeypatch, [])
    monkeypatch.setattr(runtime.subprocess, "Popen",
                        lambda argv, **kw: started.append(argv) or Child())
    monkeypatch.setattr(runtime, "BACKGROUND", {})
    host.tick(QUIET)
    assert started == []
    saved = json.loads((tmp_path / "data" / runtime.SCHEDULE_FILE).read_text())
    assert set(saved["fired"]) == {job for job, _ in host.schedules}
    assert (tmp_path / "data" / runtime.SCHEDULE_FILE).stat().st_mode & 0o077 == 0


# ----------------------------------------------------------- M10 drain cap

def test_restart_drain_is_capped_for_scheduled_jobs_only(tmp_path, monkeypatch):
    events = []
    host = _host(tmp_path, monkeypatch, events)
    check, update, command = Child(1), Child(2), Child(3)
    for job, child in (("mcs_check", check), ("update", update), ("cmd", command)):
        host.children[job] = {"process": child, "kind": "core", "stopping_at": None}
    host.restarting = True
    assert host.tick(QUIET) is False
    host.tick(QUIET + runtime.DRAIN_CAP_S - 1)
    assert check.signals == []
    host.tick(QUIET + runtime.DRAIN_CAP_S)
    assert check.signals == ["term"] and ("drain_cap_reached", {"job": "mcs_check"}) in events
    host.tick(QUIET + runtime.DRAIN_CAP_S + runtime.STOP_GRACE_S)
    assert check.signals == ["term", "kill"]
    assert update.signals == [] and command.signals == []



def test_a_slot_killed_by_the_drain_cap_is_caught_up_after_the_restart(tmp_path, monkeypatch):
    events = []
    started = []
    host = _host(tmp_path, monkeypatch, events)
    monkeypatch.setattr(runtime.subprocess, "Popen",
                        lambda argv, **kw: started.append(argv) or Child())
    monkeypatch.setattr(runtime, "BACKGROUND", {})
    monkeypatch.setattr(runtime.time, "localtime", lambda stamp:
                        SimpleNamespace(tm_hour=12, tm_min=int(stamp // 60) % 60))
    host.schedules = [("mcs_check", [{"Minute": m} for m in range(0, 60, 10)])]
    host.tick(600)
    host.children["mcs_check"]["process"].code = 0
    host.tick(660)
    host.tick(1200)                   # minute 20 starts and is still running
    assert len(started) == 2 and host.fired == {"mcs_check": 1200}
    host.restarting = True
    host.tick(1260)
    host.tick(1260 + runtime.DRAIN_CAP_S)
    assert ("drain_cap_reached", {"job": "mcs_check"}) in events
    assert host.fired == {"mcs_check": 600}     # minute 20 never finished
    later = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    later.schedules = host.schedules
    later.tick(1260 + runtime.DRAIN_CAP_S + 60)
    assert len(started) == 3
    assert ("slot_catchup", {"job": "mcs_check", "slot": 1800}) in events



def _killed_mid_slot(tmp_path, monkeypatch, events, started):
    host = _host(tmp_path, monkeypatch, events)
    monkeypatch.setattr(runtime.subprocess, "Popen",
                        lambda argv, **kw: started.append(argv) or Child())
    monkeypatch.setattr(runtime, "BACKGROUND", {})
    monkeypatch.setattr(runtime.time, "localtime", lambda stamp:
                        SimpleNamespace(tm_hour=12, tm_min=int(stamp // 60) % 60))
    host.schedules = [("mcs_check", [{"Minute": m} for m in range(0, 60, 10)])]
    host.tick(600)
    host.children["mcs_check"]["process"].code = 0
    host.tick(660)
    host.tick(1200)                   # minute 20 starts; then the host is SIGKILLed
    saved = json.loads((tmp_path / "data" / runtime.SCHEDULE_FILE).read_text())
    assert saved["inflight"] == {"mcs_check": 600}
    return host


def test_a_slot_cut_short_by_an_abrupt_shutdown_is_caught_up(tmp_path, monkeypatch):
    events, started = [], []
    host = _killed_mid_slot(tmp_path, monkeypatch, events, started)
    later = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    later.schedules = host.schedules
    later.adopt_orphans(QUIET)        # no surviving child to adopt
    later.recover_inflight()
    assert later.fired == {"mcs_check": 600} and later.inflight == {}
    assert ("slot_interrupted", {"job": "mcs_check"}) in events
    later.tick(1260)
    assert len(started) == 3
    assert ("slot_catchup", {"job": "mcs_check", "slot": 1200}) in events


def test_a_finished_or_adopted_slot_is_not_rerun(tmp_path, monkeypatch):
    events, started = [], []
    host = _killed_mid_slot(tmp_path, monkeypatch, events, started)
    host.children["mcs_check"]["process"].code = 0
    host.tick(1260)                   # the run was seen to end
    later = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    assert later.inflight == {} and later.fired == {"mcs_check": 1200}
    later.children["mcs_check"] = {"process": Child(), "kind": "core",
                                   "stopping_at": None, "adopted": True}
    later.inflight = {"mcs_check": 600}
    later.recover_inflight()          # a surviving orphan is awaited instead
    assert later.fired == {"mcs_check": 1200} and later.inflight == {"mcs_check": 600}

# ------------------------------------------------------ orphans of a killed host

def test_children_of_a_killed_host_are_stopped_or_awaited(tmp_path, monkeypatch):
    events = []
    host = _host(tmp_path, monkeypatch, events)
    data = tmp_path / "data"
    rows = {"extract-0": {"pid": 4001, "kind": "background"},
            "mcs_check": {"pid": 4002, "kind": "core"},
            "cmd": {"pid": 4003, "kind": "core"}}       # pid reused by something else
    (data / service.STATUS_FILE).write_text(json.dumps({"pid": 3999, "children": rows}))
    (data / service.STATUS_FILE).chmod(0o600)
    argv = {4001: host.argv["extract-0"], 4002: host.argv["mcs_check"], 4003: ["/bin/other"]}
    alive = {4001, 4002, 4003}
    sent = []

    def kill(pid, sig):
        if sig:
            sent.append((pid, sig))
        elif pid not in alive:
            raise ProcessLookupError

    monkeypatch.setattr(runtime.os, "kill", kill)
    started = []
    monkeypatch.setattr(runtime.subprocess, "Popen",
                        lambda a, **kw: started.append(a) or Child())
    host.adopt_orphans(QUIET)
    for job, pid in (("extract-0", 4001), ("mcs_check", 4002), ("cmd", 4003)):
        _complete_identity(host, monkeypatch, job, argv[pid] == host.argv[job], QUIET,
                           runtime.signal.SIGTERM if job == "extract-0" else None)
    assert set(host.children) == {"extract-0", "mcs_check"}
    assert sent == [(4001, runtime.signal.SIGTERM)]
    host.tick(QUIET + 1)
    assert host.argv["mcs_check"] not in started and host.argv["extract-0"] not in started
    alive -= {4001, 4002}
    host.tick(QUIET + 2)
    assert host.exits["mcs_check"]["last_exit"] is None   # unknown, not a failure
    host.tick(QUIET + 3)
    assert host.argv["extract-0"] in started
    assert [f["action"] for e, f in events if e == "orphan_adopted"] == ["stop", "wait"]


def test_live_child_of_this_host_is_never_adopted(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch, [])
    data = tmp_path / "data"
    (data / service.STATUS_FILE).write_text(json.dumps(
        {"pid": 3999, "children": {"mcs_check": {"pid": 4002, "kind": "core"}}}))
    (data / service.STATUS_FILE).chmod(0o600)
    # The worker reports a readable process owned by this host as non-orphan.
    host.adopt_orphans(QUIET)
    result = _complete_identity(host, monkeypatch, "mcs_check",
                                (runtime.os.getpid(), " ".join(host.argv["mcs_check"])), QUIET)
    assert result["sent"] is False
    assert host.children == {} and host.orphan_candidates == {}


# ------------------------------------------------------ manual plist template

def test_manual_plist_template_keeps_the_host_alive_like_the_rendered_one():
    text = (runtime.REPO / "deployment/launchagents/ai.mcs.standalone.plist").read_text()
    template = plistlib.loads(text.encode())
    rendered = plistlib.loads(service.render("/tmp/synthetic-root", platform="darwin")[1].encode())
    for key in ("KeepAlive", "ExitTimeOut", "AbandonProcessGroup", "ThrottleInterval", "Umask"):
        assert template[key] == rendered[key], key



# Adopted PIDs are observed identities, not reusable signal capabilities.
@pytest.mark.parametrize("operation", ["terminate", "kill"])
def test_adopted_process_checks_identity_before_each_signal(tmp_path, monkeypatch, operation):
    host = _host(tmp_path, monkeypatch)
    argv = host.argv["mcs_check"]
    sent = []
    monkeypatch.setattr(runtime.os, "kill", lambda pid, sig: sent.append((pid, sig)) if sig else None)
    host.orphan_candidates["mcs_check"] = 4001
    initial = _complete_identity(host, monkeypatch, "mcs_check", True, 0)
    assert initial["identity"] is True and initial["sent"] is False
    adopted = host.children["mcs_check"]["process"]
    assert adopted.argv == argv
    # Exit and PID reuse happen before the queued signal's worker checks identity.
    getattr(adopted, operation)()
    assert sent == []
    result = _complete_identity(host, monkeypatch, "mcs_check", False, 1, adopted.operation)
    assert result["sent"] is False and sent == []
    assert adopted.poll() == 0


def test_adopted_background_reuse_during_term_grace_never_receives_kill(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch)
    clock = [0.0]
    sent = []
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime.os, "kill", lambda pid, sig: sent.append((pid, sig)) if sig else None)
    status = tmp_path / "data" / service.STATUS_FILE
    status.write_text(json.dumps({"pid": 3999, "children": {"extract-0": {"pid": 4001}}}))
    status.chmod(0o600)
    host.adopt_orphans(100)
    _complete_identity(host, monkeypatch, "extract-0", True, 100, runtime.signal.SIGTERM)
    assert sent == [(4001, runtime.signal.SIGTERM)]
    host.schedules = []
    monkeypatch.setattr(runtime, "BACKGROUND", {})
    clock[0] = runtime.STOP_GRACE_S + 1
    host.tick(100 + runtime.STOP_GRACE_S + 1, draining=True)
    assert host.children["extract-0"]["process"].operation == runtime.signal.SIGKILL
    _complete_identity(host, monkeypatch, "extract-0", False,
                       100 + runtime.STOP_GRACE_S + 1, runtime.signal.SIGKILL)
    assert sent == [(4001, runtime.signal.SIGTERM)]
    host.tick(100 + runtime.STOP_GRACE_S + 2, draining=True)
    assert "extract-0" not in host.children
    assert host.exits["extract-0"]["last_exit"] is None


def test_unreadable_adopted_identity_keeps_child_and_inflight_slot(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(runtime.os, "kill", lambda pid, sig: sent.append((pid, sig)) if sig else None)
    adopted = runtime._Adopted(4002, host.argv["mcs_check"])
    host.children["mcs_check"] = {"process": adopted, "kind": "core", "stopping_at": None,
                                   "adopted": True, "unfired": 600}
    host.fired, host.inflight = {"mcs_check": 1200}, {"mcs_check": 600}
    _complete_identity(host, monkeypatch, "mcs_check", None, 1231)
    host.tick(1231, draining=True)
    assert host.children["mcs_check"]["process"] is adopted
    assert host.inflight == {"mcs_check": 600} and host.fired == {"mcs_check": 1200}
    assert host.exits == {}
    adopted.terminate()
    _complete_identity(host, monkeypatch, "mcs_check", None, 1232, adopted.operation)
    adopted.kill()
    _complete_identity(host, monkeypatch, "mcs_check", None, 1233, adopted.operation)
    assert sent == [] and adopted.poll() is None
    # A transient read failure must not permanently discard ownership.
    adopted.terminate()
    _complete_identity(host, monkeypatch, "mcs_check", True, 1234, adopted.operation)
    assert sent == [(4002, runtime.signal.SIGTERM)] and adopted.poll() is None


def test_unreadable_adopted_liveness_is_not_proof_of_exit(monkeypatch):
    adopted = runtime._Adopted(4003, ["synthetic-worker"])
    def unreadable(_pid, sig):
        assert sig == 0
        raise PermissionError("synthetic liveness probe unavailable")
    monkeypatch.setattr(runtime.os, "kill", unreadable)
    assert adopted.poll() is None
    assert not adopted.ended


def test_verified_adopted_background_still_terms_kills_and_reaps(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch)
    clock, alive, sent = [0.0], [True], []
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    def signal_process(pid, sig):
        if not alive[0]:
            raise ProcessLookupError("synthetic child gone")
        if sig:
            sent.append((pid, sig))
            if sig == runtime.signal.SIGKILL:
                alive[0] = False
    monkeypatch.setattr(runtime.os, "kill", signal_process)
    status = tmp_path / "data" / service.STATUS_FILE
    status.write_text(json.dumps({"pid": 3999, "children": {"extract-0": {"pid": 4001}}}))
    status.chmod(0o600)
    host.adopt_orphans(100)
    _complete_identity(host, monkeypatch, "extract-0", True, 100, runtime.signal.SIGTERM)
    host.schedules = []
    monkeypatch.setattr(runtime, "BACKGROUND", {})
    assert sent == [(4001, runtime.signal.SIGTERM)]
    clock[0] = runtime.STOP_GRACE_S - 1
    host.tick(100 + clock[0], draining=True)
    assert sent == [(4001, runtime.signal.SIGTERM)]
    clock[0] = runtime.STOP_GRACE_S
    host.tick(100 + clock[0], draining=True)
    assert host.children["extract-0"]["process"].operation == runtime.signal.SIGKILL
    _complete_identity(host, monkeypatch, "extract-0", True, 100 + clock[0], runtime.signal.SIGKILL)
    assert sent == [(4001, runtime.signal.SIGTERM), (4001, runtime.signal.SIGKILL)]
    host.tick(101 + clock[0], draining=True)
    assert host.children == {} and host.exits["extract-0"]["last_exit"] is None


def test_initial_unreadable_identity_is_not_adopted_or_signalled(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch)
    status = tmp_path / "data" / service.STATUS_FILE
    status.write_text(json.dumps({"pid": 3999, "children": {"extract-0": {"pid": 4001}}}))
    status.chmod(0o600)
    def no_signal(*_args):
        raise AssertionError("unknown initial identity cannot authorize a signal")
    monkeypatch.setattr(runtime.os, "kill", no_signal)
    host.adopt_orphans(100)
    _complete_identity(host, monkeypatch, "extract-0", None, 100, runtime.signal.SIGTERM)
    assert host.children == {} and host.orphan_candidates == {"extract-0": 4001}


@pytest.mark.parametrize("identity", [True, None, False], ids=["same", "unreadable", "reused"])
def test_adopted_eperm_rechecks_identity_without_releasing_unknown_slot(
        tmp_path, monkeypatch, identity):
    host = _host(tmp_path, monkeypatch)
    probes, identities = [], []
    worker = runtime._identity_worker
    def observed_worker():
        identities.append(json.loads(runtime.sys.stdin.buffer.getvalue())["pid"])
        return worker()
    monkeypatch.setattr(runtime, "_identity_worker", observed_worker)
    def denied_probe(pid, sig):
        probes.append((pid, sig))
        assert sig == 0  # no TERM/KILL or real process operation
        raise PermissionError(errno.EPERM, "synthetic liveness denial")
    monkeypatch.setattr(runtime.os, "kill", denied_probe)
    status = tmp_path / "data" / service.STATUS_FILE
    status.write_text(json.dumps({"pid": 3999, "children": {"mcs_check": {"pid": 4005}}}))
    status.chmod(0o600)
    host.fired, host.inflight = {"mcs_check": 1200}, {"mcs_check": 600}
    host.adopt_orphans(1200)
    _complete_identity(host, monkeypatch, "mcs_check", True, 1200)
    assert identities == [4005]
    adopted = host.children["mcs_check"]["process"]
    # Even EPERM liveness cannot suppress a later isolated identity result.
    assert adopted.poll() is None
    assert probes == [(4005, 0)]
    _complete_identity(host, monkeypatch, "mcs_check", identity, 1231)
    assert identities == [4005, 4005]
    host.tick(1231, draining=True)
    assert host.fired == {"mcs_check": 1200}
    if identity is False:
        assert adopted.ended and "mcs_check" not in host.children
        assert host.inflight == {}
        assert host.exits["mcs_check"]["last_exit"] is None
        assert host.retry_at["mcs_check"] == 1231
    else:
        assert not adopted.ended
        assert host.children["mcs_check"]["process"] is adopted
        assert host.inflight == {"mcs_check": 600}
        assert host.exits == {}
        assert probes == [(4005, 0), (4005, 0)]
