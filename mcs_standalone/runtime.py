"""Own scheduled jobs and workers, draining active jobs before a host restart."""
from __future__ import annotations

import asyncio
import copy
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
import select
import sys
import stat
import subprocess
import time
import threading
import uuid

import update_journal
from run_log_install import helper_path, LogInstallError
from xml.sax.saxutils import escape

from mcs_runtime import python_executable
from scheduled_jobs import (CRON_TIMEOUT_S, TIMEOUT_PERL, calendar_entries,
                            configured_cron_jobs, latest_calendar_slots, render_template)
from writer_lock import UPDATE_MARKER_NAME
from safe_io import atomic_write
from adapters.common.commands import NativeAdmission
from adapters.common.worker import await_settled

from . import config, service
from .restart_transport import RestartCarrier
from .host import DeliveryAdmission, log

REPO = Path(__file__).resolve().parents[1]
BACKGROUND = {"extract-0": "ai.mcs.extract-drainer", "extract-2": "ai.mcs.extract-drainer-2",
              "extract-night": "ai.mcs.extract-drainer-night"}
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


def _is_background(job) -> bool:
    """A drainer or the LINE WORKS connector: stopped, never waited on, when orphaned."""
    return job in BACKGROUND or job == "lineworks"


def _backoff(failures):
    return min(RESTART_BACKOFF_S * 2 ** max(failures - 1, 0), BACKOFF_CAP_S)


def _error_code(exc):
    """A fixed code for a connector end: codes as raised, else the type name."""
    text = str(exc)
    return text if _CODE.fullmatch(text) else type(exc).__name__


def _try_lock(path, *, require_known=False):
    fd = None
    retained = False
    try:
        fd = safe_io.open_lock_file(path)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError(errno.EPERM, "standalone_lock_invalid")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        retained = True
        return fd
    except BlockingIOError:
        return None
    except OSError:
        if require_known:
            raise
        return None
    finally:
        if fd is not None and not retained:
            os.close(fd)


IDENTITY_BYTES = 16384
IDENTITY_TIMEOUT_S = 5.0
IDENTITY_RETRY_S = 30.0


def _drain_probe_stdout(probe, process) -> None:
    """Read what the identity probe wrote so far, up to one byte past the cap."""
    try:
        room = IDENTITY_BYTES + 1 - len(probe["raw"])
        if room > 0:
            probe["raw"].extend(os.read(process.stdout.fileno(), room))
    except BlockingIOError:
        pass
    except OSError:
        probe["discard"] = True


def _process_args(pid):
    """Read bounded ps output only inside the isolated identity worker."""
    child = None
    try:
        child = subprocess.Popen(["/bin/ps", "-ww", "-o", "ppid=", "-o", "args=", "-p", str(pid)],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        os.set_blocking(child.stdout.fileno(), False)
        raw = bytearray()
        until = time.monotonic() + 4.0
        while time.monotonic() < until:
            try:
                chunk = os.read(child.stdout.fileno(), IDENTITY_BYTES + 1 - len(raw))
            except BlockingIOError:
                chunk = None
            if chunk:
                raw.extend(chunk)
                if len(raw) > IDENTITY_BYTES:
                    return None
            elif chunk == b"" and child.poll() is not None:
                parts = raw.decode("utf-8", errors="strict").strip().split(None, 1)
                return ((int(parts[0]), parts[1]) if child.returncode == 0
                        and len(parts) == 2 and parts[0].isdigit() else None)
            select.select([child.stdout], [], [], min(0.05, max(0, until - time.monotonic())))
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None
    finally:
        if child is not None:
            if child.poll() is None:
                with suppress(OSError):
                    child.kill()
            with suppress(subprocess.SubprocessError):
                child.wait(timeout=0.1)
            child.stdout.close()
    return None


def _is_orphan(pid, argv, owner_pid, starting_argv=None):
    found = _process_args(pid)
    if found is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            pass
        return None
    if (found[0] != owner_pid and starting_argv is not None
            and found[1] == " ".join(starting_argv)):
        # The verified helper has not execed yet: neither duplicate nor signal it.
        return None
    return found[0] != owner_pid and found[1] == " ".join(argv)


def _identity_worker():
    """Probe and optionally signal one exact process; never load host state or credentials."""
    raw = sys.stdin.buffer.read(IDENTITY_BYTES + 1)
    request = json.loads(raw)
    required = {"pid", "argv", "operation", "generation", "owner_pid"}
    if not isinstance(request, dict) or set(request) not in (required, required | {"starting_argv"}):
        raise ValueError("identity_request_invalid")
    pid, argv, operation = request["pid"], request["argv"], request["operation"]
    if (len(raw) > IDENTITY_BYTES or type(pid) is not int or not 1 < pid < 2**31
            or type(request["owner_pid"]) is not int or not 1 < request["owner_pid"] < 2**31
            or not isinstance(request["generation"], str)
            or re.fullmatch("[0-9a-f]{32}", request["generation"]) is None
            or not isinstance(argv, list) or not argv
            or not all(isinstance(arg, str) and "\0" not in arg for arg in argv)
            or operation is not None and (type(operation) is not int
                or operation not in (signal.SIGTERM, signal.SIGKILL))):
        raise ValueError("identity_request_invalid")
    starting = request.get("starting_argv")
    if starting is not None:
        if (not isinstance(starting, list) or len(starting) != len(argv) + 7
                or not all(isinstance(arg, str) and "\0" not in arg for arg in starting)
                or starting[1:2] != ["-I"] or starting[3:5] != ["exec", "--data-dir"]
                or starting[6:7] != ["--"] or starting[7:] != argv):
            raise ValueError("identity_request_invalid")
        identity = _is_orphan(pid, argv, request["owner_pid"], starting)
    else:
        identity = _is_orphan(pid, argv, request["owner_pid"])
    sent = False
    if identity is True and operation is not None:
        try:
            os.kill(pid, operation)
            sent = True
        except ProcessLookupError:
            identity = False
        except OSError:
            pass
    print(json.dumps({"generation": request["generation"], "pid": pid,
                      "identity": identity, "sent": sent}))


class _Adopted:
    """Track an orphan; identity checks and signals belong to the bounded worker."""
    def __init__(self, pid, argv):
        self.pid, self.argv = pid, argv
        self.ended = False
        self.operation = None

    def poll(self):
        if not self.ended:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                self.ended = True
            except OSError:
                pass
        return 0 if self.ended else None

    def terminate(self):
        self.operation = signal.SIGTERM

    def kill(self):
        self.operation = signal.SIGKILL


class Runtime:
    def __init__(self, root, cfg):
        self.root = Path(root).expanduser().resolve()
        self.data = self.root / "data"
        self.cfg = cfg
        self.generation = uuid.uuid4().hex
        self.native_admission = NativeAdmission()
        self.delivery_admission = DeliveryAdmission()
        self._delivery_identity = None
        self._reservation_owner_thread = threading.get_ident()
        self._restart_reservation = None
        self._restart_reservation_uncertain = False
        self._restart_carrier = None
        self._restart_execution_owner = None
        self._planning_host_fd = None
        self._support_restart = None
        self._restart_signal_seen = False
        self.started_at = time.time()
        self.children = {}
        self.orphan_candidates = {}
        self.identity_probe = None
        self.identity_due = {}
        self.identity_operation = {}
        self.identity_after = ""
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
        self._startup_restart_file_checked = False
        self.write_error = {}
        subs = {"PYTHON": python_executable(cfg, root=self.root), "REPO": str(REPO),
                "DATA": str(self.data), "RUNTIME_HOME": str(self.root)}
        self.argv = {}
        for job, label in {**BACKGROUND, **COMMANDS}.items():
            text = (REPO / "deployment/launchagents" / (label + ".plist")).read_text()
            argv = plistlib.loads(render_template(
                text, {k: escape(v) for k, v in subs.items()}).encode())["ProgramArguments"]
            # Reuse the independent exec helper when starting owned children.
            # Retain original argv here for post-exec orphan identity and TERM checks.
            if argv[1:2] == ["-I"] and argv[3:5] == ["exec", "--data-dir"] and argv[6:7] == ["--"]:
                argv = argv[7:]
            self.argv[job] = argv
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

    def _retire_previous_restart(self):
        """Retire a verified old request once, before exposing this host generation."""
        if self._startup_restart_file_checked:
            return False
        self._startup_restart_file_checked = True
        if self._planning_host_fd is None or self.status_written is not None:
            return False
        from mcs_util import strict_json
        path = self.data / service.RESTART_FILE
        try:
            value = strict_json(safe_io.read_private_bytes(path))
            if (type(value) is not dict or set(value) != {"generation", "request_id", "requested_at"}
                    or any(type(value[key]) is not str or re.fullmatch("[0-9a-f]{32}", value[key]) is None
                           for key in ("generation", "request_id"))
                    or type(value["requested_at"]) not in (int, float)
                    or not 0 <= value["requested_at"] < 1e12
                    or not math.isfinite(value["requested_at"])
                    or value["generation"] == self.generation):
                return False
        except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
            return False
        # The single-host lock proves the old owner exited. No legitimate caller
        # can target our generation before its first status publication.
        try:
            path.unlink()
            safe_io.fsync_dir(self.data)
        except OSError as exc:
            self._write_failed(service.RESTART_FILE, exc)
            return False
        return True

    def _publish_status(self, now, updating):
        body = {"pid": os.getpid(), "generation": self.generation, "started_at": self.started_at,
                "update_in_progress": updating,
                "update_marker": dict(self.marker) if self.marker else None,
                "restarting": self.restarting,
                "restart_reservation": ("uncertain" if self._restart_reservation_uncertain
                                        else "active" if self._restart_reservation is not None else None),
                "children": {
                    **{job: {"pid": pid, "kind": "background" if _is_background(job) else "core",
                             "state": "orphan_identity_unknown"}
                       for job, pid in self.orphan_candidates.items()},
                    **{job: {"pid": row["process"].pid, "kind": row["kind"]}
                       for job, row in self.children.items()}},
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

    # ---------------------------------------------------- local reservation

    def _reservation_owner(self):
        if threading.get_ident() != self._reservation_owner_thread:
            raise RuntimeError("standalone_restart_owner_required")

    def _reservation_config_current(self, expected):
        try:
            return self.cfg == expected and config.load(self.root) == expected
        except (OSError, ValueError, TypeError):
            return False

    def _reservation_idle(self):
        native = self.native_admission.snapshot()
        return (not native["queued"] and not native["running"]
                and not self.delivery_admission.snapshot()["active_passes"]
                and self._delivery_owner_ready()
                and not any(job in self.children for job in ("cmd", "cmd_int", "update", "lineworks"))
                and not self.orphan_candidates and self.identity_probe is None
                and not any(isinstance(row["process"], _Adopted) for row in self.children.values()))

    def _delivery_owner_ready(self):
        delivery = self.delivery_admission.snapshot()
        transport = (self.cfg.get("notify") or {}).get("interactive")
        if transport in ("slack", "discord"):
            return (self._delivery_identity is not None and delivery["ready"]
                    and self.connector["state"] == "running")
        return (not transport and self._delivery_identity is None
                and delivery["state"] == "off" and self.connector["state"] == "off")

    def _restart_update_ready(self, row):
        try:
            held, current = os.fstat(row["fd"]), os.lstat(self.data / "update.lock")
            if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                raise RuntimeError("standalone_restart_update_lock_unknown")
        except (OSError, TypeError):
            raise RuntimeError("standalone_restart_update_lock_unknown") from None
        try:
            os.lstat(self.data / UPDATE_MARKER_NAME)
        except FileNotFoundError:
            pass
        except OSError:
            raise RuntimeError("standalone_restart_update_marker_unknown") from None
        else:
            raise RuntimeError("standalone_restart_update_pending")
        state = update_journal.load_state(self.data / "update_state.json")
        if state.get("_corrupt") or state.get("applying") or state.get("stages"):
            raise RuntimeError("standalone_restart_update_pending")

    def reserve_restart(self):
        """Pause local admission with one live RAM reservation, without authorizing a restart.

        Configured chat owners need actual connector readiness. No grant, SPENT, HTTP,
        durable continuation or drain is created. A returned object is valid
        only on this Runtime's synchronous owner thread and current generation.
        """
        self._reservation_owner()
        if self._restart_reservation is not None or self._restart_reservation_uncertain or self.restarting:
            raise RuntimeError("standalone_restart_reservation_busy")
        if not self._reservation_config_current(self.cfg):
            raise RuntimeError("standalone_restart_configuration_changed")
        if not self._delivery_owner_ready():
            raise RuntimeError("native_owner_unknown")
        if not self.native_admission.snapshot()["open"] or not self.delivery_admission.snapshot()["open"]:
            raise RuntimeError("standalone_restart_admission_closed")
        handle = object()
        row = {"handle": handle, "generation": self.generation, "config": copy.deepcopy(self.cfg),
               "admission": self.native_admission, "delivery": self.delivery_admission,
               "delivery_identity": self._delivery_identity, "fd": None}
        self._restart_reservation = row
        self.native_admission.close()
        self.delivery_admission.close()
        try:
            if not self._reservation_idle():
                raise RuntimeError("standalone_restart_reservation_busy")
            try:
                row["fd"] = _try_lock(self.data / "update.lock", require_known=True)
            except OSError:
                self._restart_reservation_uncertain = True
                raise RuntimeError("standalone_restart_update_lock_unknown") from None
            if row["fd"] is None:
                raise RuntimeError("standalone_restart_update_busy")
            self._restart_update_ready(row)
            if (self.restarting or self._restart_requested(time.time())
                    or not self._reservation_config_current(row["config"]) or not self._reservation_idle()):
                raise RuntimeError("standalone_restart_reservation_busy")
            return handle
        except BaseException:
            self.release_restart(handle)
            raise

    def release_restart(self, handle):
        """Retire this exact local reservation; reopen only a proven unchanged, unstopped owner.

        Returning True means local admission reopened, never restart permission.
        Uncertain descriptor retirement keeps admission closed until owner exit.
        """
        self._reservation_owner()
        row = self._restart_reservation
        if row is None or row["handle"] is not handle:
            raise RuntimeError("standalone_restart_reservation_invalid")
        if self._restart_carrier is not None and self._restart_carrier.busy:
            raise RuntimeError("standalone_restart_transport_busy")
        if self._restart_execution_owner is not None:
            raise RuntimeError("standalone_restart_execution_busy")
        self._restart_reservation = None  # Revoke the live capability before fd retirement.
        fd, row["fd"] = row["fd"], None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                self._restart_reservation_uncertain = True
                raise RuntimeError("standalone_restart_retirement_unknown") from None
        current = (row["generation"] == self.generation and row["admission"] is self.native_admission
                   and row["delivery"] is self.delivery_admission
                   and row["delivery_identity"] is self._delivery_identity
                   and self._reservation_config_current(row["config"]))
        if not current or self._restart_requested(time.time()):
            self.restarting = True
        if current and not self.restarting and not self._restart_reservation_uncertain:
            self.native_admission.open()
            self.delivery_admission.open()
            return True
        return False

    def _restart_exchange_owner(self, handle):
        self._reservation_owner()
        row = self._restart_reservation
        if row is None or row["handle"] is not handle:
            raise RuntimeError("standalone_restart_reservation_invalid")
        if (row["generation"] != self.generation or row["admission"] is not self.native_admission
                or row["delivery"] is not self.delivery_admission
                or row["delivery_identity"] is not self._delivery_identity
                or self.restarting or self._restart_reservation_uncertain
                or self._restart_requested(time.time())
                or not self._reservation_config_current(row["config"])
                or not self._reservation_idle()
                or self.native_admission.snapshot()["open"]
                or self.delivery_admission.snapshot()["open"]):
            raise RuntimeError("standalone_restart_owner_changed")

    async def restart_exchange(self, handle, endpoint, path, raw, headers, *, deadline):
        """Borrow this live reservation for one bounded request, not restart authority.

        The caller supplies independently approved endpoint and signed request;
        it must validate the returned evidence and current permission separately.
        Cancellation waits for actual child retirement before the handle can be
        released. No request, response or execution continuation is persisted.
        """
        self._restart_exchange_owner(handle)
        if self._restart_carrier is None:
            self._restart_carrier = RestartCarrier()
        result = await self._restart_carrier.send(endpoint, path, raw, headers, deadline=deadline)
        self._restart_exchange_owner(handle)
        return result

    async def _close_restart_transport(self):
        if self._restart_execution_owner is not None:
            owner = self._restart_execution_owner
            owner.cancel()
            await owner.wait_closed()
        if self._restart_carrier is not None:
            self._restart_carrier.cancel()
            await self._restart_carrier.wait_closed()

    async def restart_execute(self, store, *, operation_envelope, approval, acceptance,
                              authority_supplier, deadline, verifier=None,
                              clock=time.time, monotonic=time.monotonic):
        """Run one live restart; authority joins its fresh read_credential callback each time."""
        from .restart_owner import execute
        return await execute(self, store, operation_envelope=operation_envelope,
                             approval=approval, acceptance=acceptance,
                             authority_supplier=authority_supplier, deadline=deadline,
                             verifier=verifier, clock=clock, monotonic=monotonic)

    def _restart_execution_check(self, owner, handle):
        if self._restart_execution_owner is not owner:
            raise RuntimeError("standalone_restart_execution_owner_changed")
        self._restart_exchange_owner(handle)
        self._restart_update_ready(self._restart_reservation)

    def _restart_support_start(self, owner, handle, *, operation_id, receipt_id,
                               start_receipt_id, started_at):
        self._restart_execution_check(owner, handle)
        if self._support_restart is not None:
            raise RuntimeError("standalone_restart_execution_spent")
        # A support cause exists only after its SPENT barrier, never for a signal/config stop.
        self._support_restart = {"operation_id": operation_id, "receipt_id": receipt_id,
                                 "start_receipt_id": start_receipt_id, "started_at": started_at}
        self.restarting = True
        if self._restart_signal_seen:
            self._support_restart = None
            raise RuntimeError("standalone_restart_owner_changed")
        return started_at

    def _restart_signal(self, _signal, _frame):
        self._restart_signal_seen = True
        self.restarting = True

    # ---------------------------------------------------- passive planning

    def _planning_owner_ready(self, expected_config, expected_generation):
        self._reservation_owner()
        if (self._planning_host_fd is None or self.restarting or self._restart_signal_seen
                or self._restart_reservation is not None or self._restart_reservation_uncertain
                or self._restart_execution_owner is not None
                or self.generation != expected_generation
                or not self._reservation_config_current(expected_config)
                or not self.native_admission.snapshot()["open"]
                or not self.delivery_admission.snapshot()["open"]
                or not self._reservation_idle()):
            raise RuntimeError("standalone_restart_planning_unavailable")
        held = os.fstat(self._planning_host_fd)
        current = os.lstat(self.data / "standalone.lock")
        if (not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
                or current.st_mode & 0o077 or current.st_nlink != 1
                or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
            raise RuntimeError("standalone_restart_planning_unavailable")

    def _planning_controls_clear(self):
        from adapters.sdc.protocol import strict_json

        try:
            request = strict_json(safe_io.read_private_bytes(self.data / service.RESTART_FILE))
        except FileNotFoundError:
            pass
        else:
            if (type(request) is not dict or set(request) != {"generation", "request_id", "requested_at"}
                    or any(type(request[k]) is not str or re.fullmatch("[0-9a-f]{32}", request[k]) is None
                           for k in ("generation", "request_id"))
                    or type(request["requested_at"]) not in (int, float)
                    or not math.isfinite(request["requested_at"])
                    or request["requested_at"] < 0
                    or request["generation"] == self.generation):
                raise RuntimeError("standalone_restart_planning_unavailable")
        # No O_CREAT: a missing lock is unknown, not evidence of an idle updater.
        fd = os.open(self.data / "update.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            held = os.fstat(fd)
            if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid()
                    or held.st_mode & 0o077 or held.st_nlink != 1):
                raise ValueError
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                os.lstat(self.data / UPDATE_MARKER_NAME)
            except FileNotFoundError:
                pass
            else:
                raise ValueError
            try:
                state = strict_json(safe_io.read_private_bytes(self.data / "update_state.json", 1024 * 1024))
            except FileNotFoundError:
                state = update_journal.default_state()
            if (not update_journal.valid_state(state) or state.get("_corrupt")
                    or state.get("applying") is not None or state.get("stages")):
                raise ValueError
            current = os.lstat(self.data / "update.lock")
            if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError
        finally:
            os.close(fd)

    def _restart_planning_preflight(self, request):
        """Observe this serving owner once; no reservation, cleanup or execution authority."""
        from adapters.sdc.restart_contract import EXPECTED_FIELDS, _preflight

        try:
            fields = {"request_id", "planning_owner", "phase", "expected_generation", "expires_at", "expected"}
            now = time.time() * 1000
            if (type(request) is not dict or set(request) != fields
                    or type(request["expected"]) is not dict
                    or set(request["expected"]) != set(EXPECTED_FIELDS) - {"service_revision", "params"}
                    or request["phase"] not in ("sample", "refresh", "confirm")
                    or type(request["expires_at"]) is not int
                    or not 0 <= now < request["expires_at"] <= 2**53 - 1
                    or (request["expected_generation"] is None and request["phase"] != "sample")
                    or request["expected_generation"] not in (None, self.generation)):
                raise ValueError
            expected_config, generation = copy.deepcopy(self.cfg), self.generation
            self._planning_owner_ready(expected_config, generation)
            self._planning_controls_clear()
            self._planning_owner_ready(expected_config, generation)
            observed_at = int(time.time() * 1000)
            if observed_at >= request["expires_at"]:
                raise ValueError
            return _preflight({"schema_version": "sdc.support.restart-preflight.v1", "product": "hermes-mcs",
                **{k: request["expected"][k] for k in ("tenant", "installation", "device_id", "environment", "case_id")},
                "service_id": "mcs.standalone", "runtime_generation": generation,
                "started_at": int(self.started_at * 1000), "observed_at": observed_at, "owner_alive": True,
                "update_in_progress": False, "command_intake_in_progress": False, "restarting": False}, observed_at)
        except Exception:
            raise RuntimeError("standalone_restart_planning_unavailable") from None

    def _respond_restart_planning(self, restart_provision):
        if self.restarting or restart_provision is not None and not callable(restart_provision):
            return False
        try:
            settings = self.cfg.get("sdc")
            if type(settings) is not dict or type(settings.get("state_root")) is not str or not settings["state_root"]:
                return False
            if restart_provision is None:
                for name in ('provision-state.json', 'restart-planning.json'):
                    marker = os.stat(Path(settings['state_root']) / name, follow_symlinks=False)
                    if not stat.S_ISREG(marker.st_mode):
                        return False
            self._reservation_owner()
            from adapters.sdc import provision
            from adapters.sdc.restart_planning import respond_once
            from adapters.sdc.storage import Store

            with Store(settings["state_root"]) as store:
                if restart_provision is None:
                    provision.profile_snapshot(store, profile='restart', now_ms=int(time.time() * 1000))
                # Only local metadata is read; the injected provider is never called.
                return respond_once(store, sample=self._restart_planning_preflight,
                                    now=lambda: int(time.time() * 1000))
        except Exception:
            # An unavailable metadata response must not stop the normal Runtime.
            return False

    # ------------------------------------------------------------- children

    def start(self, job, kind, now, pending=None):
        # A failed start earlier in this tick requested a restart: start nothing more.
        if (self.restarting or self._restart_reservation is not None or self._restart_reservation_uncertain
                or job in self.children or job in self.orphan_candidates
                or now < self.retry_at.get(job, 0)):
            return
        try:
            inherited = os.environ.get("MCS_RUN_LOG_HELPER")
            directory = str(Path(inherited).parent) if inherited and os.path.isabs(inherited) else None
            helper = helper_path(self.root, directory, require_installed=True)
            if inherited and inherited != helper:
                raise LogInstallError("run_log_helper_unverified")
            child = subprocess.Popen(
                [sys.executable,
                 "-I", helper, "exec", "--data-dir", str(self.data), "--", *self.argv[job]],
                cwd=REPO, env={**os.environ, "MCS_ROOT": str(self.root)},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        except (OSError, LogInstallError):
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
            self.orphan_candidates[job] = pid

    def _identity_result(self, probe, result, now):
        job, pid = probe["job"], probe["pid"]
        child = (self.children.get(job) or {}).get("process")
        pending = self.orphan_candidates.get(job) == pid
        if (probe["generation"] != self.generation or self.argv.get(job) != probe["argv"]
                or not pending and not (isinstance(child, _Adopted) and child.pid == pid)):
            return
        identity = result.get("identity") if (result.get("pid") == pid
                    and result.get("generation") == self.generation) else None
        if identity is not True and identity is not False:
            identity = None
        self.identity_due[job] = time.monotonic() + IDENTITY_RETRY_S
        if pending and identity is not None:
            del self.orphan_candidates[job]
            if identity is True:
                kind = "background" if _is_background(job) else "core"
                child = _Adopted(pid, self.argv[job])
                self.children[job] = {"process": child, "kind": kind, "pending": None,
                                      "adopted": True, "stopping_at": None}
                if job in self.inflight:
                    self.children[job]["unfired"] = self.inflight[job]
                if kind == "background":
                    child.terminate()
                log("orphan_adopted", job=job, action="stop" if kind == "background" else "wait")
        if isinstance(child, _Adopted):
            if identity is False:
                child.ended = True
            elif identity is True and result.get("sent") is True:
                row = self.children[job]
                if probe["operation"] == signal.SIGTERM and row["stopping_at"] is None:
                    row["stopping_at"] = now
                if child.operation == probe["operation"]:
                    child.operation = None
        self.recover_inflight()

    def _poll_identity(self, now):
        probe = self.identity_probe
        if probe is None:
            return
        process = probe["process"]
        if not probe["discard"]:
            if probe["input"]:
                try:
                    written = os.write(process.stdin.fileno(), probe["input"])
                    del probe["input"][:written]
                except BlockingIOError:
                    pass
                except OSError:
                    probe["discard"] = True
                if not probe["input"]:
                    with suppress(OSError):
                        process.stdin.close()
            _drain_probe_stdout(probe, process)
        if (probe["discard"] or len(probe["raw"]) > IDENTITY_BYTES
                or time.monotonic() - probe["started"] >= IDENTITY_TIMEOUT_S):
            probe["discard"] = True
            if process.poll() is None:
                with suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
        if process.poll() is None:
            return
        # A killed probe may leave an uninterruptible ps in its session. Do not
        # launch another probe until that group is gone; a reaped leader never authorizes a signal.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        except OSError:
            return
        else:
            return
        # A child can finish between the nonblocking read and poll; drain its final bytes.
        if not probe["discard"]:
            _drain_probe_stdout(probe, process)
        self.identity_probe = None
        for stream in (process.stdin, process.stdout):
            with suppress(OSError):
                stream.close()
        try:
            result = (json.loads(probe["raw"]) if not probe["discard"]
                      and len(probe["raw"]) <= IDENTITY_BYTES and process.returncode == 0 else {})
            if not isinstance(result, dict):
                result = {}
        except (ValueError, UnicodeError):
            result = {}
        self._identity_result(probe, result, now)

    def _start_identity(self):
        if self.identity_probe is not None:
            return
        targets = {job: (pid, signal.SIGTERM if _is_background(job) else None)
                   for job, pid in self.orphan_candidates.items()}
        targets.update({job: (row["process"].pid, row["process"].operation)
                        for job, row in self.children.items()
                        if isinstance(row["process"], _Adopted) and not row["process"].ended})
        for job in sorted(targets, key=lambda name: (name <= self.identity_after, name)):
            pid, operation = targets[job]
            if (time.monotonic() < self.identity_due.get(job, 0)
                    and self.identity_operation.get(job) == operation):
                continue
            self.identity_after = job
            self.identity_operation[job] = operation
            request = {"pid": pid, "argv": list(self.argv[job]), "operation": int(operation) if operation else None,
                       "generation": self.generation, "owner_pid": os.getpid()}
            try:
                inherited = os.environ.get("MCS_RUN_LOG_HELPER")
                directory = str(Path(inherited).parent) if inherited and os.path.isabs(inherited) else None
                helper = helper_path(self.root, directory, require_installed=True)
                if inherited and inherited != helper:
                    raise LogInstallError("run_log_helper_unverified")
                request["starting_argv"] = [sys.executable, "-I", helper, "exec", "--data-dir",
                                            str(self.data), "--", *self.argv[job]]
            except (OSError, LogInstallError):
                # Without verified startup identity, hold the candidate rather than clear it.
                self.identity_due[job] = time.monotonic() + IDENTITY_RETRY_S
                return
            code = ("import sys; sys.path.insert(0,sys.argv[1]); "
                    "sys.path.insert(0,sys.argv[1]+'/mcs'); import _mcs_path; "
                    "from mcs_standalone.runtime import _identity_worker; _identity_worker()")
            raw = json.dumps(request).encode()
            if len(raw) > IDENTITY_BYTES:
                self.identity_due[job] = time.monotonic() + IDENTITY_RETRY_S
                return
            try:
                process = subprocess.Popen([sys.executable, "-I", "-c", code, str(REPO)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, start_new_session=True)
            except OSError:
                self.identity_due[job] = time.monotonic() + IDENTITY_RETRY_S
                return
            self.identity_probe = {**request, "job": job, "process": process,
                                   "started": time.monotonic(), "raw": bytearray(),
                                   "input": bytearray(raw), "discard": False}
            try:
                os.set_blocking(process.stdin.fileno(), False)
                os.set_blocking(process.stdout.fileno(), False)
            except OSError:
                self.identity_probe["discard"] = True
                with suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
            return

    def recover_inflight(self):
        """A slot whose run outlived neither the host nor as an adopted
        orphan was cut short by an abrupt shutdown: keep it pending so it
        is caught up (after adopt_orphans)."""
        lost = [job for job in self.inflight
                if job not in self.children and job not in self.orphan_candidates]
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
            try:
                import notify_spool
                return notify_spool.restore_writer_hold(str(self.data)) is not None
            except Exception:
                return True
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
                    import notify_spool
                    held = notify_spool.restore_writer_hold(str(self.data))
                except (ImportError, AttributeError):
                    # Cannot verify the restore witness: an empty journal
                    # is not proof the updater's restore hold is done.
                    return "journal_unreadable"
                if held is not None:
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
        if self._restart_reservation is not None or self._restart_reservation_uncertain:
            return
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
            if job in self.children or job in self.orphan_candidates:
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
        if self.restarting:
            self.native_admission.close()
            self.delivery_admission.close()
        # Our reservation owns update.lock; do not probe/reacquire it as an updater.
        updating = (False if self._restart_reservation is not None or self._restart_reservation_uncertain
                    else self._updating(now))
        self._poll_identity(now)
        for job, row in list(self.children.items()):
            child = row["process"]
            code = child.poll()
            if code is not None:
                self._ended(job, row, code, now, updating)
            elif self._should_stop(job, row, now, updating):
                if row["stopping_at"] is None:
                    first = not isinstance(child, _Adopted) or child.operation is None
                    child.terminate()
                    if not isinstance(child, _Adopted):
                        row["stopping_at"] = now
                    if row["kind"] == "core" and first:
                        log("drain_cap_reached", job=job)
                        if row.get("unfired") is not None:
                            # killed before it finished: keep its slot pending
                            self.fired[job] = row["unfired"]
                            self._save_fired()
                elif now - row["stopping_at"] >= STOP_GRACE_S:
                    child.kill()
        if (not updating and not self.restarting and not draining
                and self._restart_reservation is None and not self._restart_reservation_uncertain
                and self.native_admission.snapshot()["open"]):
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
        self._start_identity()
        self._publish_status(now, updating)
        if self.restarting:
            self.native_admission.close()
            self.delivery_admission.close()
        native = self.native_admission.snapshot()
        return (self.restarting and not self.children and not self.orphan_candidates
                and self.identity_probe is None and not native["queued"]
                and not native["running"] and not self.delivery_admission.snapshot()["active_passes"])

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


async def serve(root, cfg, connector=None, *, restart_provision=None):
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("standalone_main_thread_required")
    runtime = Runtime(root, cfg)
    runtime.data.mkdir(mode=0o700, parents=True, exist_ok=True)
    if runtime.data.is_symlink() or runtime.data.stat().st_uid != os.getuid():
        raise config.ConfigError("standalone_data_directory_invalid")
    os.chmod(runtime.data, 0o700)
    fd = safe_io.open_lock_file(runtime.data / "standalone.lock")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_uid != os.getuid():
            raise config.ConfigError("standalone_lock_invalid")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise config.ConfigError("standalone_already_running") from None
        runtime._planning_host_fd = fd
        runtime._retire_previous_restart()
        stopping = asyncio.Event()
        registered = []
        worker = None

        async def drain():
            runtime.restarting = True
            runtime.native_admission.close()
            runtime.delivery_admission.close()
            try:
                await runtime._close_restart_transport()
                while runtime.children or runtime.orphan_candidates or runtime.identity_probe:
                    with suppress(OSError):
                        runtime.tick(draining=True)
                    await asyncio.sleep(1)
            finally:
                stopping.set()
                try:
                    if worker is not None:
                        try:
                            await asyncio.wait_for(worker, timeout=30)
                        except asyncio.TimeoutError:
                            log("connector_stop_timeout")
                        except (Exception, asyncio.CancelledError) as exc:
                            log("connector_ended", code=_error_code(exc), stopping=True)
                finally:
                    # A cancelled waiter is not proof that its owned work ended.
                    while True:
                        native = runtime.native_admission.snapshot()
                        delivery = runtime.delivery_admission.snapshot()
                        if not native["queued"] and not native["running"] and not delivery["active_passes"]:
                            break
                        await asyncio.sleep(0.05)
                    if runtime._delivery_identity is not None:
                        runtime.delivery_admission.end_connector(runtime._delivery_identity)
                        runtime._delivery_identity = None

        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                # Observe a stop during synchronous SPENT fsync before its final latch.
                previous = signal.signal(sig, runtime._restart_signal)
                registered.append((sig, previous))
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
                        runtime.delivery_admission.end_connector(runtime._delivery_identity)
                        runtime._delivery_identity = None
                        runtime.connector_ended(worker, now)
                        worker = None
                    elif worker is not None:
                        runtime.connector_running(now)
                    if worker is None and not runtime.restarting \
                            and runtime._restart_reservation is None and not runtime._restart_reservation_uncertain \
                            and now >= runtime.connector["retry_at"]:
                        runtime._delivery_identity = runtime.delivery_admission.begin_connector()
                        worker = asyncio.create_task(connector(
                            root, stopping, native_admission=runtime.native_admission,
                            delivery_admission=runtime.delivery_admission,
                            delivery_identity=runtime._delivery_identity))
                        runtime.connector_started(now)
                if runtime.tick():
                    break
                runtime._respond_restart_planning(restart_provision)
                await asyncio.sleep(1)
        finally:
            try:
                await await_settled(drain())
            finally:
                try:
                    if runtime._restart_reservation is not None:
                        runtime.release_restart(runtime._restart_reservation["handle"])
                finally:
                    for sig, previous in reversed(registered):
                        signal.signal(sig, previous)
    finally:
        runtime._planning_host_fd = None
        os.close(fd)
    return 0
