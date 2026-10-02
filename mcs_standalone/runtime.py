"""Own scheduled jobs and workers, draining active jobs before a host restart."""
from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
from pathlib import Path
import plistlib
import signal
import stat
import subprocess
import time
import uuid
from xml.sax.saxutils import escape

from mcs_runtime import python_executable
from mcs_setup import CRON_JOBS, CRON_TIMEOUT_S, _TIMEOUT_PL, _calendar, _render_template
from mcs_util import UPDATE_MARKER_NAME, atomic_write

from . import config, service
from .host import log

REPO = Path(__file__).resolve().parents[1]
BACKGROUND = {"extract-0": "ai.mcs.extract-drainer", "extract-2": "ai.mcs.extract-drainer-2"}
COMMANDS = {"cmd": "local.mcs-cmd", "cmd_int": "local.mcs-int"}


class Runtime:
    def __init__(self, root, cfg):
        self.root = Path(root).expanduser().resolve()
        self.data = self.root / "data"
        self.cfg = cfg
        self.generation = uuid.uuid4().hex
        self.children = {}
        self.retry_at = {}
        self.last_minute = None
        self.restarting = False
        subs = {"PYTHON": python_executable(cfg, root=self.root), "REPO": str(REPO),
                "DATA": str(self.data), "RUNTIME_HOME": str(self.root)}
        self.argv = {}
        for job, label in {**BACKGROUND, **COMMANDS}.items():
            text = (REPO / "deployment/launchagents" / (label + ".plist")).read_text()
            self.argv[job] = plistlib.loads(_render_template(
                text, {k: escape(v) for k, v in subs.items()}).encode())["ProgramArguments"]
        if (cfg.get("notify") or {}).get("interactive") == "lineworks":
            self.argv["lineworks"] = [subs["PYTHON"], "-m", "lineworks_adapter", "run",
                                      "--root", str(self.root)]
        self.schedules = []
        for _, schedule, script in CRON_JOBS:
            job = "update" if script == "mcs_update.sh" else script.removesuffix(".sh")
            self.argv[job] = ["/usr/bin/perl", "-e", _TIMEOUT_PL, str(CRON_TIMEOUT_S),
                              "/bin/bash", str(self.root / "scripts" / script)]
            self.schedules.append((job, _calendar(schedule)))

    def start(self, job, kind, now):
        if job in self.children or now < self.retry_at.get(job, 0):
            return
        path = self.data / (job + ".log")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "ab") as output:
                os.fchmod(output.fileno(), 0o600)
                child = subprocess.Popen(self.argv[job], cwd=REPO,
                    env={**os.environ, "MCS_ROOT": str(self.root)}, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=output, close_fds=True)
        except OSError:
            self.restarting = True
            log("child_start_failed", job=job)
            return
        self.children[job] = {"process": child, "kind": kind, "stopping_at": None}
        log("child_started", job=job)

    def _restart_requested(self, now):
        path = self.data / service.RESTART_FILE
        try:
            raw = config._private_bytes(path, "request_missing", "request_invalid")
            value = json.loads(raw)
            stamp = value["requested_at"]
            return (value["generation"] == self.generation
                    and isinstance(value["request_id"], str)
                    and len(value["request_id"]) == 32
                    and type(stamp) in (int, float) and math.isfinite(stamp)
                    and -2 <= now - stamp <= service.STATUS_TTL)
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            return False

    def tick(self, now=None):
        now = time.time() if now is None else now
        self.restarting |= self._restart_requested(now)
        updating = os.path.lexists(self.data / UPDATE_MARKER_NAME)
        for job, row in list(self.children.items()):
            child = row["process"]
            if child.poll() is not None:
                del self.children[job]
                self.retry_at[job] = now if updating else now + 30
                log("child_ended", job=job)
            elif (updating or self.restarting) and row["kind"] == "background":
                if row["stopping_at"] is None:
                    child.terminate()
                    row["stopping_at"] = now
                elif now - row["stopping_at"] >= 10:
                    child.kill()
        if not updating and not self.restarting:
            for job in [*BACKGROUND, *(["lineworks"] if "lineworks" in self.argv else [])]:
                self.start(job, "background", now)
            for job in COMMANDS:
                if any((self.data / job).glob("*.json")):
                    self.start(job, "core", now)
            minute = int(now // 60)
            if minute != self.last_minute:
                self.last_minute = minute
                local = time.localtime(now)
                for job, entries in self.schedules:
                    if any(entry.get("Hour", local.tm_hour) == local.tm_hour
                           and entry.get("Minute", local.tm_min) == local.tm_min for entry in entries):
                        self.start(job, "core", now)
        snapshot = {"pid": os.getpid(), "generation": self.generation, "updated_at": now,
                    "update_in_progress": updating,
                    "children": {job: {"pid": row["process"].pid, "kind": row["kind"]}
                                 for job, row in self.children.items()}}
        atomic_write(str(self.data / service.STATUS_FILE),
                     lambda stream: json.dump(snapshot, stream), mode=0o600)
        return self.restarting and not self.children


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
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, setattr, runtime, "restarting", True)
        worker = asyncio.create_task(connector(root, stopping)) if connector else None
        try:
            while True:
                try:
                    runtime.restarting |= config.load(root) != cfg
                except ValueError:
                    runtime.restarting = True
                if worker and worker.done():
                    runtime.restarting = True
                if runtime.tick():
                    break
                await asyncio.sleep(1)
        finally:
            runtime.restarting = True
            while runtime.children:
                runtime.tick()
                await asyncio.sleep(1)
            stopping.set()
            if worker:
                try:
                    await asyncio.wait_for(worker, timeout=30)
                except asyncio.TimeoutError:
                    log("connector_stop_timeout")
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
    finally:
        os.close(fd)
    return 0
