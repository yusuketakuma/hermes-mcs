"""Run bounded patient-context cloud workers under the existing lane-2 resident lifecycle."""
from __future__ import annotations

from contextlib import contextmanager, closing
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import _mcs_path  # noqa: F401

from run_log_install import helper_path, LogInstallError

_parent = None


_RUNTIME_ERROR_CODES = frozenset({
    "transport_inside_db_transaction", "extract_insert_unverified",
    "extract_revive_unverified", "extract_source_changed_during_write",
    "extract_hold_unverified", "extract_proof_readback_failed",
    "extract_stale_gc_unverified", "extract_gc_cursor_unverified",
    "cloud_disposition_owner_transaction_active", "cloud_circuit_transaction_active",
    "cloud_circuit_transaction_required", "cloud_history_transaction_required",
    "cloud_reservation_hook_invalid", "cloud_result_owner_transaction_active",
    "cloud_disposition_transaction_required", "cloud_dryrun_transaction_required",
    "patient_source_transaction_required", "patient_summary_transaction_required",
    "patient_summary_transaction_active", "patient_context_source_transaction_required",
    "patient_context_read_changed", "patient_summary_snapshot_required",
})
_SQLITE_ERROR_PRIMARY = frozenset({
    "SQLITE_ERROR", "SQLITE_INTERNAL", "SQLITE_PERM", "SQLITE_ABORT", "SQLITE_BUSY",
    "SQLITE_LOCKED", "SQLITE_NOMEM", "SQLITE_READONLY", "SQLITE_INTERRUPT", "SQLITE_IOERR",
    "SQLITE_CORRUPT", "SQLITE_NOTFOUND", "SQLITE_FULL", "SQLITE_CANTOPEN", "SQLITE_PROTOCOL",
    "SQLITE_EMPTY", "SQLITE_SCHEMA", "SQLITE_TOOBIG", "SQLITE_CONSTRAINT", "SQLITE_MISMATCH",
    "SQLITE_MISUSE", "SQLITE_NOLFS", "SQLITE_AUTH", "SQLITE_FORMAT", "SQLITE_RANGE",
    "SQLITE_NOTADB", "SQLITE_NOTICE", "SQLITE_WARNING",
})
_SQLITE_ERROR_CODES = {name: code for name, code in vars(sqlite3).items()
                       if type(code) is int and (name in _SQLITE_ERROR_PRIMARY or any(
                           name.startswith(primary + "_") for primary in _SQLITE_ERROR_PRIMARY))}


def _exception_observation(error):
    """Expose only a fixed runtime reason or a matching standard SQLite error identity."""
    record = {"class": type(error).__name__}
    if (type(error) is RuntimeError and len(error.args) == 1
            and type(error.args[0]) is str and error.args[0] in _RUNTIME_ERROR_CODES):
        record["error_code"] = error.args[0]
    elif isinstance(error, sqlite3.Error):
        try:
            code = getattr(error, "sqlite_errorcode", None)
            name = getattr(error, "sqlite_errorname", None)
            if (type(code) is int and type(name) is str
                    and _SQLITE_ERROR_CODES.get(name) == code):
                record.update(sqlite_errorcode=code, sqlite_errorname=name)
        except Exception:
            pass  # Diagnostic metadata must never replace the original failure handling.
    return record



def _event(event, **values):
    print(json.dumps({"event": event, "pid": os.getpid(), "ts": time.time(), **values}), flush=True)



def owner_alive():
    return _parent is None or os.getppid() == _parent


def _lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    held = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError("cloud_lock_untrusted")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        held = True
        return fd
    finally:
        if not held:
            os.close(fd)


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
    """Bound the pool by free memory after a ten-percent reserve and 256 MiB per worker."""
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


class CloudBacklogPool:
    """Own child processes only while the normal lane-2 resident is alive."""
    def __init__(self, root):
        self.root = Path(root)
        self.children = []
        self.indices = {}
        self.lock = None
        self.last_sync = 0.0

    def sync(self, cfg):
        import cloud_inference
        import patient_context_refresh
        now = time.monotonic()
        if now - self.last_sync < 10:
            return
        self.last_sync = now
        settings = cloud_inference.resolve_cloud_settings(cfg)
        self.children = [p for p in self.children if p.poll() is None]
        self.indices = {p: index for p, index in self.indices.items() if p in self.children}
        # Cloud availability is decided inside run_due. A missing key or a spend
        # hold must not hide its existing local fallback/recovery.
        if settings["mode"] != "on" or not patient_context_refresh.options(cfg)["enabled"]:
            return
        directory = self.root / "data"
        if self.lock is None:
            self.lock = _lock(directory / "context_pool.lock")
            if self.lock is None:
                return
        self.children = [p for p in self.children if p.poll() is None]
        target = memory_workers(settings["max_concurrency"])
        self.indices = {p: index for p, index in self.indices.items() if p in self.children}
        # Keep one worker of hysteresis: its resident memory changes the sample.
        if self.children and target > 0 and abs(target - len(self.children)) <= 1:
            target = min(settings["max_concurrency"], len(self.children))
        inherited = os.environ.get("MCS_RUN_LOG_HELPER")
        recovery = str(Path(inherited).parent) if inherited and os.path.isabs(inherited) else None
        helper = helper_path(self.root, recovery, require_installed=True)
        if inherited and inherited != helper:
            raise LogInstallError("run_log_helper_unverified")
        for _ in range(max(0, target - len(self.children))):
            index = next(i for i in range(32) if i not in self.indices.values())
            child = subprocess.Popen(
                [sys.executable, "-I", helper, "exec", "--data-dir", str(directory), "--",
                 sys.executable, str(Path(__file__).resolve()), "--parent", str(os.getpid()),
                 "--index", str(index)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True)
            self.children.append(child)
            self.indices[child] = index

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
        self.indices = {}
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None


def worker(parent, *, index=None):
    """Run guarded patient-context passes with an independent ledger while the parent lives."""
    global _parent
    _parent = parent
    import extract_llm
    import writer_lock
    from ledger import Ledger
    from mcs_util import DB
    initial = writer_lock.updater_state()
    _event("cloud_worker_started", parent=parent, source_digests=extract_llm._LOADED_SOURCE_DIGESTS)
    while os.getppid() == parent and writer_lock.updater_state() == initial and not initial[0]:
        if not extract_llm._context_memory_available():
            return
        startup_lock = writer_lock.acquire_run_lock()
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
        _context_worker(ledger, initial, index)


_CONTEXT_PASS_REASONS = frozenset({
    "transaction_active", "patient_context_waiting", "patient_context_scan_incomplete",
    "patient_context_readiness_unverified", "patient_context_generation_changed", "run_lock",
    "patient_context_selection_busy", "patient_context_selection_unavailable",
    "patient_context_completed_expired", "patient_context_capacity_or_deadline", "patient_context_admission_deferred",
    "patient_context_turn_busy", "patient_context_local_capacity", "patient_context_source_changed", "patient_context_cloud_budget",
    "patient_context_generation_active", "patient_context_generation_clock_changed",
    "patient_context_generation_daily_limit", "patient_context_generation_interval",
    "patient_context_cloud_not_sent", "patient_context_cloud_pending", "patient_context_cloud_source_changed",
    "patient_context_length_stop", "patient_context_unsupported_items", "patient_context_unsupported_summary",
    "summary_quality_promotion", "summary_quality_unavailable", "source_quality_unavailable",
    "patient_summary_quality_lock", "patient_summary_quality_budget_changed",
    "patient_summary_quality_source_changed",
    "patient_context_source_repair_changed", "patient_context_source_repair_lock",
    "nightly_completed_expired", "nightly_capacity_or_deadline", "nightly_admission_deferred",
    "nightly_turn_busy", "nightly_local_capacity", "nightly_source_changed", "nightly_cloud_budget",
    "nightly_cloud_not_sent", "nightly_cloud_pending", "nightly_cloud_source_changed",
    "nightly_length_stop", "nightly_unsupported_items", "nightly_unsupported_summary",
    "nightly_source_repair_changed", "nightly_source_repair_lock",
}) | frozenset(prefix + state for prefix in ("source_quality_", "summary_quality_")
              for state in ("pending", "deferred", "held", "failed", "budget", "disabled", "unknown"))


_SCAN_REFUSALS = frozenset({
    "deadline", "admission", "writer_generation", "bookmark_cas",
    "scan_interrupted", "read_sqlite", "read_io",
    "invalid_bookmark", "invalid_source",
})


def _bounded_pass_code(value):
    """Identifier-shaped failure identity only; free text never qualifies."""
    return (type(value) is str and value.isascii() and value.isidentifier()
            and len(value) <= 80)


def _context_pass_observation(result):
    """Report bounded context progress and fixed reasons without source or exception values."""
    status = result.get("status")
    record = {"status": status if type(status) is str and status in {
        "off", "complete", "progressed", "deferred", "source_changed", "failed", "error"} else "unclassified"}
    reason = result.get("reason")
    if reason is None and record["status"] == "error":
        reason = result.get("error")
    if reason is not None or record["status"] in ("deferred", "failed", "error"):
        # Suffixed detail (e.g. source_quality_unavailable:RuntimeStale) classifies
        # by its fixed base; exception-derived suffixes never enter the record.
        # failed/error carry safe_error_code output or a type name — both are
        # bounded identifiers, so they pass through verbatim.
        base = reason.partition(":")[0] if type(reason) is str else None
        record["reason"] = base if base in _CONTEXT_PASS_REASONS or (
                record["status"] in ("failed", "error") and _bounded_pass_code(base)) \
            else "unclassified"
    for key, limit in (("done", 1), ("scanned", 8), ("scan_ms", 900000)):
        value = result.get(key)
        if type(value) is int and 0 <= value <= limit:
            record[key] = value
    lane = result.get("lane")
    if type(lane) is str and lane in ("A", "B", "C"):
        record["lane"] = lane
    refusal = result.get("scan_refusal")
    if refusal is not None:
        record["scan_refusal"] = refusal if type(refusal) is str and refusal in _SCAN_REFUSALS else "unclassified"
    covered = result.get("covered")
    if type(covered) is bool:
        record["covered"] = covered
    # Real per-pass bounds: one try-wide interrupt, 8 candidates x at most 2
    # waiting additions (quota-fail double count or two single-count scopes),
    # and at most 2 pending reads per candidate.
    for key, limit in (("waiting", 16), ("scan_reads", 16), ("scan_interrupts", 1),
                       ("wait_no_eligible_sources", 8), ("wait_no_due_lane", 16),
                       ("wait_generation_quota", 8), ("wait_readiness_unverified", 16),
                       ("wait_dependency_incomplete", 16)):
        value = result.get(key)
        if type(value) is int and 0 <= value <= limit:
            record[key] = value
    return record


def _context_retry_delay(result):
    """Backoff for one context pass. selection_busy means another worker
    holds the selection lock and is doing real work — retrying in seconds
    just adds convoy noise, so wait longer."""
    if result.get("done"):
        return 1
    if result.get("reason") == "patient_context_selection_busy":
        return 20
    return 5


def _context_worker(ledger, initial, index):
    import cloud_inference
    import extract_llm_resident
    import local_llm
    import writer_lock
    from mcs_util import HOME, load_config

    with local_llm.scheduling_scope(os.path.join(HOME, "data")):
        while owner_alive() and writer_lock.updater_state() == initial and not initial[0]:
            settings = cloud_inference.resolve_cloud_settings(load_config())
            if (settings["mode"] != "on" or index is not None and index >= settings["max_concurrency"]
                    or not extract_llm_resident._context_memory_available()):
                return
            with writer_lock.capture_writer_refusal() as diagnostic:
                result = extract_llm_resident._background_context_refresh(
                    ledger, initial, cloud_worker=True)
            record = _context_pass_observation(result)
            if diagnostic["writer_reason"] != "writer_refusal_unclassified":
                record.update(writer_reason=diagnostic["writer_reason"], writer_stage=diagnostic["writer_stage"])
            _event("patient_context_pass", **record)
            if result.get("run_lock_lost") == "updated":
                return
            time.sleep(_context_retry_delay(result))


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--index", type=int, choices=range(32))
    # A resident started before this version still passes the former pool flag.
    parser.add_argument("--context-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.parent != os.getppid():
        return 2
    worker(args.parent, index=args.index)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
