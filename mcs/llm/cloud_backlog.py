"""Run bounded cloud-only backlog workers under the existing resident lifecycle."""
from __future__ import annotations

from contextlib import contextmanager, closing
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

_parent = None


def owner_alive():
    return _parent is None or os.getppid() == _parent


def _lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError("cloud_lock_untrusted")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


@contextmanager
def request_slot(root, capacity):
    """Bound in-flight cloud units across every process and release crashed owners."""
    if type(capacity) is not int or not 1 <= capacity <= 32:
        raise ValueError("cloud_capacity_invalid")
    directory = Path(root) / "data" / "cloud_locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    admission = _lock(directory / "admission.lock")
    until = time.monotonic() + 0.1
    while admission is None and time.monotonic() < until:
        time.sleep(0.005)
        admission = _lock(directory / "admission.lock")
    free, slot = [], None
    try:
        if admission is not None:
            for index in range(32):
                fd = _lock(directory / f"{index}.lock")
                if fd is not None:
                    free.append(fd)
            if 32 - len(free) < capacity:
                slot = free.pop(0)
    finally:
        for fd in free:
            os.close(fd)
        if admission is not None:
            os.close(admission)
    try:
        yield slot is not None
    finally:
        if slot is not None:
            os.close(slot)


def memory_workers(limit):
    """Keep ten percent of physical memory plus 256 MiB per additional worker."""
    if sys.platform != "darwin":
        try:
            values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
            total = int(values["MemTotal"].split()[0]) * 1024
            available = int(values["MemAvailable"].split()[0]) * 1024
        except (OSError, ValueError, KeyError):
            return 0
    else:
        import re
        try:
            total = int(subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=2, check=True).stdout.strip())
            output = subprocess.run(["/usr/bin/memory_pressure", "-Q"],
                capture_output=True, text=True, timeout=2, check=True).stdout
            match = re.search(r"System-wide memory free percentage:\s*(\d+)%", output)
            if match is None:
                return 0
            available = total * int(match[1]) // 100
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0
    return max(0, min(limit, int((available - total / 10) // (256 * 1024 * 1024))))


class Pool:
    """Own child processes only while the normal lane-2 resident is alive."""
    def __init__(self, root):
        self.root = Path(root)
        self.children = []
        self.lock = None
        self.log = None
        self.last_sync = 0.0

    def sync(self, cfg, *, ledger=None):
        import extract_llm
        now = time.monotonic()
        if now - self.last_sync < 10:
            return
        self.last_sync = now
        settings = extract_llm._cloud_settings(cfg)
        if settings["mode"] != "on":
            self.close()
            return
        if not extract_llm.cloud_policy.selected_key(self.root):
            return
        self.children = [p for p in self.children if p.poll() is None]
        if ledger is not None and (extract_llm.cloud_policy.global_block_reason(ledger, model=settings["model"])
                or extract_llm.cloud_policy.budget_block_reason(ledger, settings)):
            return
        directory = self.root / "data"
        if self.lock is None:
            self.lock = _lock(directory / "cloud_pool.lock")
            if self.lock is None:
                return
        self.children = [p for p in self.children if p.poll() is None]
        target = min(settings["max_concurrency"], len(self.children) + memory_workers(settings["max_concurrency"]))
        if self.log is None:
            fd = os.open(directory / "cloud_backlog.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                os.close(fd)
                raise ValueError("cloud_log_untrusted")
            self.log = os.fdopen(fd, "ab", buffering=0)
        for _ in range(max(0, target - len(self.children))):
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                "--parent", str(os.getpid())], stdin=subprocess.DEVNULL,
                stdout=self.log, stderr=self.log, close_fds=True)
            self.children.append(child)

    def close(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
        until = time.monotonic() + 5
        for child in self.children:
            try:
                child.wait(timeout=max(0.1, until - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1)
        self.children = []
        if self.log is not None:
            self.log.close()
            self.log = None
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None


def worker(parent):
    """Alternate guarded cloud extraction and semantic jobs with an independent ledger."""
    global _parent
    _parent = parent
    import extract_llm
    import semantic_drain
    import semantic_runtime
    import writer_lock
    from writer_lock import acquire_run_lock
    from ledger import Ledger
    from mcs_util import DB, CONF_PATH, load_config
    initial = writer_lock.updater_state()
    phase = 0
    print(json.dumps({"event": "cloud_worker_started", "pid": os.getpid(), "parent": parent,
                      "source_digests": extract_llm._LOADED_SOURCE_DIGESTS}), flush=True)
    while os.getppid() == parent and writer_lock.updater_state() == initial and not initial[0]:
        startup_lock = acquire_run_lock()
        if startup_lock is not None:
            break
        time.sleep(1)
    else:
        return
    try:
        if writer_lock.updater_state() != initial or not owner_alive():
            return
        ledger = Ledger(DB)
    finally:
        os.close(startup_lock)
    with closing(ledger):
        while os.getppid() == parent and writer_lock.updater_state() == initial and not initial[0]:
            if not extract_llm._context_memory_available():
                return
            cfg = load_config()
            settings = extract_llm._cloud_settings(cfg)
            if (settings["mode"] != "on"
                    or extract_llm.cloud_policy.global_block_reason(ledger, model=settings["model"])
                    or extract_llm.cloud_policy.budget_block_reason(ledger, settings)):
                time.sleep(10)
                continue
            started = time.monotonic()
            try:
                if phase % 2 == 0:
                    result = extract_llm.run_pending(ledger, limit=1, budget_s=900,
                        per_write_lock=True, workers=1, background=True,
                        cloud_only=True, prioritize_patients=True,
                        admitted_ids=extract_llm.legacy_admissions(ledger, cfg))
                else:
                    lock = writer_lock.acquire_run_lock()
                    if lock is None:
                        time.sleep(1)
                        continue
                    try:
                        scfg = semantic_drain.runtime_config(cfg)[0]
                        result = semantic_drain.run_due(ledger, cfg, {"errors": []},
                            time.monotonic() + scfg["job_budget_seconds"] + 30,
                            max_jobs=1, cfg_path=CONF_PATH, run_lock_fd=lock,
                            lane="backlog", cloud_only=True)
                    finally:
                        os.close(lock)
                record = {"event": "cloud_backlog_pass", "pid": os.getpid(),
                    "phase": "extract" if phase % 2 == 0 else "semantic", "done": result.get("done", 0),
                    "elapsed_s": round(time.monotonic() - started, 3), "ts": time.time()}
                if phase % 2 and result.get("budget_exhausted") is True:
                    record["reason"] = "backlog_budget_reserved"
                print(json.dumps(record), flush=True)
                if not result.get("done"):
                    time.sleep(2)
            except writer_lock.RunLockLost:
                return
            except semantic_runtime.RuntimeGuardError:
                time.sleep(2)
            except Exception as error:
                print(json.dumps({"event": "cloud_backlog_error", "class": type(error).__name__}), flush=True)
                time.sleep(10)
            phase += 1


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=int, required=True)
    args = parser.parse_args()
    if args.parent != os.getppid():
        return 2
    worker(args.parent)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import _mcs_path  # noqa: F401
    raise SystemExit(main())
