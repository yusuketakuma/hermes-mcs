"""Own scheduled jobs and workers, draining active jobs before a host restart."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import errno
import fcntl
import json
import safe_io
import math
import os
from pathlib import Path
import plistlib
import re
import signal
import stat
import subprocess
import time
import uuid

import update_journal
from xml.sax.saxutils import escape

from mcs_runtime import python_executable
from scheduled_jobs import (CRON_TIMEOUT_S, TIMEOUT_PERL, calendar_entries,
                            configured_cron_jobs, latest_calendar_slots, render_template)
from writer_lock import UPDATE_MARKER_NAME
from safe_io import atomic_write

from . import config, service
from .host import log

REPO = Path(__file__).resolve().parents[1]
BACKGROUND = {"extract-0": "ai.mcs.extract-drainer", "extract-2": "ai.mcs.extract-drainer-2"}
COMMANDS = {"cmd": "local.mcs-cmd", "cmd_int": "local.mcs-int"}

# Crash restarts back off exponentially (30 s doubling) up to this cap, for
# child jobs and for the Slack/Discord connector alike; a success resets it.
RESTART_BACKOFF_S = 30
BACKOFF_CAP_S = 1800
# A connector that stayed up this long ended for a new reason: its failure
# streak starts over instead of continuing to grow the backoff.
CONNECTOR_STABLE_S = 600
# The status file is rewritten when its content changes and otherwise as a
# heartbeat well inside service.STATUS_TTL (15 s).
HEARTBEAT_S = 5
# A restart drain waits for running scheduled jobs at most this long, then
# stops them (they are lock-safe and caught up after the restart). `update`
# and the command intakes are never interrupted.
DRAIN_CAP_S = 600
STOP_GRACE_S = 10
# A marker without its live writer is re-checked at most this often.
MARKER_PROBE_S = 60
SCHEDULE_FILE = "standalone-schedule.json"
# Missed calendar slots are looked up this far back (daily jobs included).
CATCHUP_WINDOW_MIN = 24 * 60
_CODE = re.compile(r"[a-z][a-z0-9_:.-]{0,63}")


def _backoff(failures):
    return min(RESTART_BACKOFF_S * 2 ** max(failures - 1, 0), BACKOFF_CAP_S)


def _error_code(exc):
    """A fixed code for a connector end: codes as raised, else the type name."""
    text = str(exc)
    return text if _CODE.fullmatch(text) else type(exc).__name__


def _try_lock(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _process_args(pid):
    """(parent pid, command line) of a live process, or None."""
    try:
        result = subprocess.run(["ps", "-ww", "-o", "ppid=", "-o", "args=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    parts = result.stdout.strip().split(None, 1) if result.returncode == 0 else []
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1]


def _is_orphan(pid, argv):
    """True for a matching orphan, False for another process, None if unreadable."""
    found = _process_args(pid)
    if found is None:
        return None
    return found[0] != os.getpid() and found[1] == " ".join(argv)


class _Adopted:
    """A child left by a host that was killed; it is watched, never re-parented."""

    def __init__(self, pid, argv):
        self.pid = pid
        self.argv = argv
        self.ended = False
        self.checked = time.monotonic()

    def poll(self):
        if not self.ended:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                self.ended = True
            except OSError:
                pass  # An unreadable live process is not proof of exit.
            # A failed liveness probe is not exit proof, but a readable
            # different identity still proves this adopted process is gone.
            if not self.ended and time.monotonic() - self.checked >= 30:
                self.checked = time.monotonic()
                self.ended = _is_orphan(self.pid, self.argv) is False
        return 0 if self.ended else None

    def _signal(self, sig):
        if self.ended:
            return
        identity = _is_orphan(self.pid, self.argv)
        if identity is False:
            self.ended = True
        elif identity is True:
            with suppress(OSError):
                os.kill(self.pid, sig)
        # Unknown identity holds ownership and sends nothing; a later poll retries.

    def terminate(self):
        self._signal(signal.SIGTERM)

    def kill(self):
        self._signal(signal.SIGKILL)


class Runtime:
    def __init__(self, root, cfg):
        self.root = Path(root).expanduser().resolve()
        self.data = self.root / "data"
        self.cfg = cfg
        self.generation = uuid.uuid4().hex
        self.started_at = time.time()
        self.children = {}
        self.retry_at = {}
        # job -> last exit code/time, last success and consecutive failed exits
        # (one row per configured job, so bounded): a crash loop is visible in status
        self.exits = {}
        # Command inbox names drained by the last successful run; None after a failure.
        self.drained = {}
        self.last_minute = None
        self.restarting = False
        self.restart_since = None
        self.marker = None
        # job -> start of the newest calendar slot already fired (persisted)
        self.fired, self.inflight = self._load_fired()
        self.due = {}
        self.skipped = {}
        self.connector = {"state": "off", "failures": 0, "failing_since": None,
                          "last_error": None, "retry_at": None, "started_at": None}
        self.status_body = None
        self.status_written = None
        self.write_error = {}
        subs = {"PYTHON": python_executable(cfg, root=self.root), "REPO": str(REPO),
                "DATA": str(self.data), "RUNTIME_HOME": str(self.root)}
        self.argv = {}
        for job, label in {**BACKGROUND, **COMMANDS}.items():
            text = (REPO / "deployment/launchagents" / (label + ".plist")).read_text()
            self.argv[job] = plistlib.loads(render_template(
                text, {k: escape(v) for k, v in subs.items()}).encode())["ProgramArguments"]
        if (cfg.get("notify") or {}).get("interactive") == "lineworks":
            self.argv["lineworks"] = [subs["PYTHON"], "-m", "lineworks_adapter", "run",
                                      "--root", str(self.root)]
        self.schedules = []
        for _, schedule, script in configured_cron_jobs(cfg):
            job = "update" if script == "mcs_update.sh" else script.removesuffix(".sh")
            self.argv[job] = ["/usr/bin/perl", "-e", TIMEOUT_PERL, str(CRON_TIMEOUT_S),
                              "/bin/bash", str(self.root / "scripts" / script)]
            self.schedules.append((job, calendar_entries(schedule)))

    # ---------------------------------------------------------------- files

    def _write_failed(self, name, exc):
        code = errno.errorcode.get(exc.errno, "unknown") if isinstance(exc, OSError) else "unknown"
        if self.write_error.get(name) != code:
            # Logged once per distinct failure: a full disk must not crash-loop
            # the host or fill its own log.
            self.write_error[name] = code
            log("state_write_failed", file=name, code=code)

    def _write_ok(self, name):
        if self.write_error.pop(name, None) is not None:
            log("state_write_recovered", file=name)

    def _load_fired(self):
        """(fired, inflight): the newest slot fired per job, and for a slot
        whose run has not been seen to end, the mark to restore if it never
        does (the host was killed with it)."""
        def stamps(rows):
            return {job: stamp for job, stamp in rows.items()
                    if isinstance(job, str) and type(stamp) in (int, float) and math.isfinite(stamp)}
        try:
            raw = safe_io.read_private_bytes(self.data / SCHEDULE_FILE, 65536)
            value = json.loads(raw)
            fired = stamps(value["fired"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
            return {}, {}
        try:
            return fired, stamps(value.get("inflight") or {})
        except (TypeError, AttributeError):
            return fired, {}

    def _save_fired(self):
        body = {"v": 1, "fired": self.fired, "inflight": self.inflight}
        try:
            atomic_write(str(self.data / SCHEDULE_FILE),
                         lambda stream: json.dump(body, stream), mode=0o600)
        except OSError as exc:
            self._write_failed(SCHEDULE_FILE, exc)
        else:
            self._write_ok(SCHEDULE_FILE)

    def _publish_status(self, now, updating):
        body = {"pid": os.getpid(), "generation": self.generation, "started_at": self.started_at,
                "update_in_progress": updating,
                "update_marker": dict(self.marker) if self.marker else None,
                "restarting": self.restarting,
                "children": {job: {"pid": row["process"].pid, "kind": row["kind"]}
                             for job, row in self.children.items()},
                "exits": self.exits,
                "connector": {key: value for key, value in self.connector.items()
                              if key != "started_at"}}
        text = json.dumps(body, sort_keys=True)
        if text == self.status_body and self.status_written is not None \
                and 0 <= now - self.status_written < HEARTBEAT_S:
            return
        snapshot = {**json.loads(text), "updated_at": now}
        try:
            atomic_write(str(self.data / service.STATUS_FILE),
                         lambda stream: json.dump(snapshot, stream), mode=0o600)
        except OSError as exc:
            self._write_failed(service.STATUS_FILE, exc)
            return
        self._write_ok(service.STATUS_FILE)
        self.status_body, self.status_written = text, now

    # ------------------------------------------------------------- children

    def start(self, job, kind, now, pending=None):
        # A failed start earlier in this tick requested a restart: start nothing more.
        if self.restarting or job in self.children or now < self.retry_at.get(job, 0):
            return
        path = self.data / (job + ".log")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                output = os.fdopen(fd, "ab")
            except BaseException:
                os.close(fd)
                raise
            with output:
                os.fchmod(output.fileno(), 0o600)
                child = subprocess.Popen(self.argv[job], cwd=REPO,
                    env={**os.environ, "MCS_ROOT": str(self.root)}, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=output, close_fds=True)
        except OSError:
            self.restarting = True
            log("child_start_failed", job=job)
            return
        self.children[job] = {"process": child, "kind": kind, "stopping_at": None,
                              "pending": pending}
        log("child_started", job=job)

    def adopt_orphans(self, now=None):
        """Children of a host that was killed (SIGKILL, crash) outlive it under
        launchd's AbandonProcessGroup. Verified leftovers are taken back: a
        resident worker is stopped and started fresh, a core job is waited
        for so it never runs twice."""
        now = time.time() if now is None else now
        try:
            raw = safe_io.read_private_bytes(self.data / service.STATUS_FILE,
                                        1024 * 1024)
            previous = json.loads(raw)
            rows = previous["children"]
            if previous.get("pid") == os.getpid() or not isinstance(rows, dict):
                return
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            return
        for job, row in rows.items():
            pid = row.get("pid") if isinstance(row, dict) else None
            if job not in self.argv or job in self.children or type(pid) is not int or pid <= 1:
                continue
            if not _is_orphan(pid, self.argv[job]):
                continue
            process = _Adopted(pid, self.argv[job])
            kind = "background" if job in BACKGROUND or job == "lineworks" else "core"
            entry = {"process": process, "kind": kind, "stopping_at": None,
                     "pending": None, "adopted": True}
            if kind == "background":
                process.terminate()
                entry["stopping_at"] = now
            if job in self.inflight:
                entry["unfired"] = self.inflight[job]
            self.children[job] = entry
            log("orphan_adopted", job=job, action="stop" if kind == "background" else "wait")

    def recover_inflight(self):
        """A slot whose run outlived neither the host nor as an adopted
        orphan was cut short by an abrupt shutdown: keep it pending so it
        is caught up (after adopt_orphans)."""
        lost = [job for job in self.inflight if job not in self.children]
        for job in lost:
            self.fired[job] = self.inflight.pop(job)
            log("slot_interrupted", job=job)
        if lost:
            self._save_fired()

    def _ended(self, job, row, code, now, updating):
        del self.children[job]
        if self.inflight.pop(job, None) is not None:
            self._save_fired()
        prev = self.exits.get(job) or {}
        if row.get("adopted"):
            # The exit status of a process we did not start is unknown.
            self.exits[job] = {**prev, "last_exit": None, "ended_at": now}
            self.retry_at[job] = now
            log("child_ended", job=job, code=None, adopted=True)
            return
        failures = prev.get("failures", 0) + 1 if code else 0
        if updating:
            backoff = 0
        elif code == 0:
            # Card/approval intake is local and cheap: match launchd's 10 s throttle
            # after success. `cmd` contacts MCS, so it keeps the 30 s backoff.
            backoff = 10 if job == "cmd_int" else RESTART_BACKOFF_S
        else:
            backoff = _backoff(failures)
        self.retry_at[job] = now + backoff
        self.drained[job] = row.get("pending") if code == 0 else None
        self.exits[job] = {"last_exit": code, "ended_at": now, "failures": failures,
                           "last_ok_at": now if code == 0 else prev.get("last_ok_at")}
        log("child_ended", job=job, code=code, failures=failures, retry_in=backoff)

    def _should_stop(self, job, row, now, updating):
        if row["stopping_at"] is not None:
            return True
        if row["kind"] == "background":
            return updating or self.restarting
        return (self.restarting and job in self.scheduled and job != "update"
                and now - self.restart_since >= DRAIN_CAP_S)

    @property
    def scheduled(self):
        return {job for job, _ in self.schedules}

    # --------------------------------------------------------------- update

    def _updating(self, now):
        """The update marker pauses every job while its writer lives. A marker
        left without one (no update.lock holder, empty journal) is an orphan:
        it is cleared under the updater's own locks, exactly as recovery does.
        A marker kept by an unfinished journal stays: recovery owns it."""
        path = self.data / UPDATE_MARKER_NAME
        try:
            since = os.lstat(path).st_mtime
        except FileNotFoundError:
            self.marker = None
            return False
        except OSError:
            since = now
        if self.marker is None:
            self.marker = {"since": since, "state": "present", "probed_at": None}
            log("update_marker_seen")
        marker = self.marker
        if "update" in self.children:
            marker["state"] = "updating"
            return True
        probed = marker["probed_at"]
        if now - marker["since"] < MARKER_PROBE_S or (
                probed is not None and 0 <= now - probed < MARKER_PROBE_S):
            return True
        marker["probed_at"] = now
        verdict = self._probe_marker(path)
        if verdict == "orphan_cleared":
            log("update_marker_orphan_cleared", age_s=int(now - marker["since"]))
            self.marker = None
            return False
        if verdict != marker["state"]:
            log("update_marker_held", state=verdict, age_s=int(now - marker["since"]))
        marker["state"] = verdict
        return True

    def _probe_marker(self, path):
        update_fd = _try_lock(self.data / "update.lock")
        if update_fd is None:
            return "updater_alive"
        try:
            run_fd = _try_lock(self.data / "run.lock")
            if run_fd is None:
                return "run_busy"
            try:
                state = update_journal.load_state(self.data / "update_state.json")
                if state.get("_corrupt"):
                    return "journal_unreadable"
                if state.get("applying") or state.get("stages"):
                    return "journal_pending"
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                except OSError:
                    return "marker_unremovable"
                return "orphan_cleared"
            finally:
                os.close(run_fd)
        finally:
            os.close(update_fd)

    # ------------------------------------------------------------- schedule

    def _schedule(self, now):
        minute = int(now // 60)
        if minute != self.last_minute:
            self.last_minute = minute
            self.due = latest_calendar_slots(
                self.schedules, minute, CATCHUP_WINDOW_MIN, time.localtime)
        changed = False
        for job, slot in self.due.items():
            fired = self.fired.get(job)
            current = slot == minute * 60
            if fired is None and not current:
                # First sight of this job: no history to replay.
                self.fired[job] = slot
                changed = True
                continue
            if fired is not None and slot <= fired:
                continue
            if job in self.children:
                if self.skipped.get(job) != slot:
                    self.skipped[job] = slot
                    log("slot_skipped_running", job=job, slot=int(slot))
                continue
            if not current:
                log("slot_catchup", job=job, slot=int(slot))
            # The crash-loop backoff is for workers; a slot fires only once,
            # so a run that ended just before it must not drop it (launchd parity).
            self.retry_at.pop(job, None)
            self.start(job, "core", now)
            if self.restarting:
                break   # a failed start: the slot is caught up after the restart
            if job in self.children:
                # What to restore if the drain cap kills this run: the slot
                # must stay pending so it is caught up after the restart.
                # Never None: an unknown job past its slot reads as first sight.
                self.children[job]["unfired"] = fired if fired is not None else slot - 1
                self.inflight[job] = self.children[job]["unfired"]
            self.fired[job] = slot
            changed = True
        if changed:
            self._save_fired()

    # ----------------------------------------------------------------- tick

    def tick(self, now=None, *, draining=False):
        now = time.time() if now is None else now
        if not draining:
            self.restarting |= self._restart_requested(now)
        if self.restarting and self.restart_since is None:
            self.restart_since = now
        updating = self._updating(now)
        for job, row in list(self.children.items()):
            child = row["process"]
            code = child.poll()
            if code is not None:
                self._ended(job, row, code, now, updating)
            elif self._should_stop(job, row, now, updating):
                if row["stopping_at"] is None:
                    child.terminate()
                    row["stopping_at"] = now
                    if row["kind"] == "core":
                        log("drain_cap_reached", job=job)
                        if row.get("unfired") is not None:
                            # killed before it finished: keep its slot pending
                            self.fired[job] = row["unfired"]
                            self._save_fired()
                elif now - row["stopping_at"] >= STOP_GRACE_S:
                    child.kill()
        if not updating and not self.restarting and not draining:
            for job in [*BACKGROUND, *(["lineworks"] if "lineworks" in self.argv else [])]:
                self.start(job, "background", now)
            for job in COMMANDS:
                names = {path.name for path in (self.data / job).glob("*.json")}
                if not names:
                    if self.drained.get(job) is not None:
                        self.drained[job] = set()
                    continue
                # A new card click after a successful run must not wait out the backoff
                # (Discord waits 20 s). Only cmd_int: `cmd` contacts MCS. Failures and
                # files left by a consent hold still wait.
                drained = self.drained.get(job)
                if job == "cmd_int" and drained is not None and names - drained:
                    self.retry_at.pop(job, None)
                self.start(job, "core", now, names)
            self._schedule(now)
        self._publish_status(now, updating)
        return self.restarting and not self.children

    def _restart_requested(self, now):
        path = self.data / service.RESTART_FILE
        try:
            raw = safe_io.read_private_bytes(path)
            value = json.loads(raw)
            stamp = value["requested_at"]
            return (value["generation"] == self.generation
                    and isinstance(value["request_id"], str)
                    and len(value["request_id"]) == 32
                    and type(stamp) in (int, float) and math.isfinite(stamp)
                    and -2 <= now - stamp <= service.STATUS_TTL)
        except (OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError):
            return False

    # ------------------------------------------------------------ connector

    def connector_started(self, now):
        self.connector.update(state="running", started_at=now, retry_at=None)
        log("connector_started", failures=self.connector["failures"])

    def connector_running(self, now):
        """A connection that stayed up ends the failure streak."""
        row = self.connector
        if row["failures"] and now - row["started_at"] >= CONNECTOR_STABLE_S:
            row.update(failures=0, failing_since=None)
            log("connector_stable")

    def connector_ended(self, task, now):
        if task.cancelled():
            code = "cancelled"
        else:
            exc = task.exception()
            code = "returned" if exc is None else _error_code(exc)
        row = self.connector
        if now - (row["started_at"] or now) >= CONNECTOR_STABLE_S:
            row.update(failures=0, failing_since=None)
        if code.endswith("_restart_required"):
            # Changed settings or credentials: reconnect with the new ones now.
            delay = 0
        else:
            row["failures"] += 1
            if row["failing_since"] is None:
                row["failing_since"] = now
            delay = _backoff(row["failures"])
        row.update(state="waiting", last_error=code, retry_at=now + delay, started_at=None)
        log("connector_ended", code=code, failures=row["failures"], retry_in=delay)


async def serve(root, cfg, connector=None):
    runtime = Runtime(root, cfg)
    runtime.data.mkdir(mode=0o700, parents=True, exist_ok=True)
    if runtime.data.is_symlink() or runtime.data.stat().st_uid != os.getuid():
        raise config.ConfigError("standalone_data_directory_invalid")
    os.chmod(runtime.data, 0o700)
    fd = os.open(runtime.data / "standalone.lock",
                 os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_uid != os.getuid():
            raise config.ConfigError("standalone_lock_invalid")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise config.ConfigError("standalone_already_running") from None
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        registered = []
        worker = None
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, setattr, runtime, "restarting", True)
                registered.append(sig)
            # Only after the single-host lock: the previous host is gone.
            runtime.adopt_orphans()
            runtime.recover_inflight()
            if connector:
                runtime.connector["retry_at"] = 0
            while True:
                try:
                    runtime.restarting |= config.load(root) != cfg
                except ValueError:
                    runtime.restarting = True
                if connector:
                    # The connection is supervised on its own: its failure
                    # (token revoked, Slack/Discord outage, network down at
                    # start) backs off and reconnects without stopping the
                    # scheduler or the workers.
                    now = time.time()
                    if worker is not None and worker.done():
                        runtime.connector_ended(worker, now)
                        worker = None
                    elif worker is not None:
                        runtime.connector_running(now)
                    if worker is None and not runtime.restarting \
                            and now >= runtime.connector["retry_at"]:
                        worker = asyncio.create_task(connector(root, stopping))
                        runtime.connector_started(now)
                if runtime.tick():
                    break
                await asyncio.sleep(1)
        finally:
            try:
                runtime.restarting = True
                while runtime.children:
                    # Log/status failures must not strand children or skip connector cleanup.
                    with suppress(OSError):
                        runtime.tick(draining=True)
                    await asyncio.sleep(1)
                stopping.set()
                if worker:
                    try:
                        await asyncio.wait_for(worker, timeout=30)
                    except asyncio.TimeoutError:
                        log("connector_stop_timeout")
                    except Exception as exc:
                        # Supervised like any other end: a code, never a crash.
                        log("connector_ended", code=_error_code(exc), stopping=True)
            finally:
                for sig in registered:
                    loop.remove_signal_handler(sig)
    finally:
        os.close(fd)
    return 0
