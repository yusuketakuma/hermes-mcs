"""Run bounded cloud-only backlog workers under the existing resident lifecycle."""
from __future__ import annotations

from contextlib import contextmanager, closing
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import threading
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


_TRANSPORT_SECONDS = ("llm_transport_s", "llm_reacquire_s", "jev_transport_s", "jev_reacquire_s")


def _pass_timing_observation(result):
    """Forward only measured transport/reacquire seconds and fixed lock-loss detail."""
    def seconds(value):
        return (value if type(value) in (int, float)
                and 0 <= value <= sys.float_info.max and math.isfinite(value) else None)
    transport = result.get("transport")
    transport = transport if isinstance(transport, dict) else {}
    record = {"transport": {key: seconds(transport.get(key)) for key in _TRANSPORT_SECONDS},
              "transport_basis": "unlocked_call_wall_not_gpu_or_http_only",
              "request_slot_wait_s": None, "worker_rss_bytes": None,
              "run_lock_lost_detail": None}
    detail = result.get("run_lock_lost_detail")
    if isinstance(detail, dict) and detail.get("kind") in ("llm", "jev"):
        record["run_lock_lost_detail"] = {
            "kind": detail["kind"], **{key: seconds(detail.get(key))
                for key in ("transport_s", "reacquire_s", "window_s")}}
    return record


def _event(event, **values):
    print(json.dumps({"event": event, "pid": os.getpid(), "ts": time.time(), **values}), flush=True)


@contextmanager
def _phase_progress(phase):
    started = time.monotonic()
    stopped = threading.Event()

    def report():
        while not stopped.wait(60):
            try:
                _event("cloud_backlog_progress", phase=phase,
                       elapsed_s=round(time.monotonic() - started, 3))
            except OSError:
                return

    _event("cloud_backlog_phase", phase=phase)
    reporter = threading.Thread(target=report, daemon=True)
    reporter.start()
    try:
        yield
    finally:
        stopped.set()
        reporter.join(timeout=0.1)


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

    def sync(self, cfg, *, ledger=None):
        import cloud_inference
        import cloud_policy
        import extract_llm
        now = time.monotonic()
        if now - self.last_sync < 10:
            return
        self.last_sync = now
        settings = cloud_inference.resolve_cloud_settings(cfg)
        self.children = [p for p in self.children if p.poll() is None]
        self.indices = {p: index for p, index in self.indices.items() if p in self.children}
        if settings["mode"] != "on":
            # Workers retire at the idle boundary after paid carry is settled.
            return
        if not cloud_policy.selected_key(self.root):
            return
        selection, errors = cloud_policy.resolve_profile(
            cfg, route="reextract", purpose="backlog", root=self.root)
        if errors or selection is None:
            return
        limit = selection["max_tokens"] if selection["max_tokens"] is not None else extract_llm.MAX_TOKENS
        if ledger is not None and (cloud_policy.global_block_reason(ledger, model=selection["model"])
                or cloud_policy.budget_block_reason(ledger, settings, max_tokens=limit, selection=selection)):
            return
        directory = self.root / "data"
        if self.lock is None:
            self.lock = _lock(directory / "cloud_pool.lock")
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


# Fixed waiting-reason codes: a worker may only report one of these — never a
# raw block reason, config value, id or any provider detail.
_WAIT_CODES = {"paused", "circuit_open", "pricing_unverified", "profile_invalid",
               "daily_cap", "daily_cost_cap", "monthly_cap", "provider_budget_missing",
               "provider_daily_cap", "provider_monthly_cap", "window_history_unverified",
               "window_cap"}

# Fixed defer-reason vocabulary reported by extract_llm.run_pending — a raw
# error class, config value or id is never a valid reason in the pass record.
_DEFER_REASONS = {"deadline", "probe_unobserved", "admission", "endpoint_down",
                  "not_sent", "busy", "cloud_fallback", "circuit_open",
                  "prepare_blocked", "currentness_unverified", "cloud_guard",
                  "commit_rejected", "prior_unavailable", "worker_error", "write_unavailable",
                  "update_fence", "lease_renewal_failed", "checkpoint_unverified",
                  "repair_unavailable", "unclassified",
                  "cloud_unit_unresolved", "cloud_owner_transaction", "cloud_deadline",
                  "cloud_budget_blocked", "cloud_pricing_unverified",
                  "cloud_not_sent", "cloud_scope_or_budget", "llm_turn_busy", "budget_short",
                  "paused", "mode_off", "out_of_scope", "jev_skip_unverified",
                  "prepare_deadline", "prepare_source_changed", "prepare_cloud_disabled",
                  "prepare_profile_unavailable", "prepare_medication_changed",
                  "prepare_context_invalid", "prepare_database_unavailable", "other"}


def worker(parent, *, index=None):
    """Alternate guarded cloud extraction and semantic jobs with an independent ledger."""
    global _parent
    _parent = parent
    import cloud_inference
    import cloud_policy
    import extract_llm
    import semantic_drain
    import semantic_runtime
    import writer_lock
    from writer_lock import acquire_run_lock
    from ledger import Ledger
    from mcs_util import DB, CONF_PATH, ProviderSettingsUnreadable, load_config
    initial = writer_lock.updater_state()
    phase = 0
    _event("cloud_worker_started", parent=parent, source_digests=extract_llm._LOADED_SOURCE_DIGESTS)
    while os.getppid() == parent and writer_lock.updater_state() == initial and not initial[0]:
        if not extract_llm._context_memory_available():
            return
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
        carry = []
        allow_exit_settle = True
        waiting = {"code": None, "at": 0.0}
        carry_failures = 0
        capacity_checked = float("-inf")

        def wait(code):
            # A persistent hold emits a heartbeat, not one event per retry.
            if code != waiting["code"] or time.monotonic() - waiting["at"] >= 60:
                waiting.update(code=code, at=time.monotonic())
                _event("cloud_backlog_deferred", reason=code)
            time.sleep(10)

        def settle_carry():
            deadline = time.monotonic() + 3.0
            extract_llm._settle_carry(
                ledger, carry, final=False, fence=initial,
                lock_context=extract_llm._write_lock(True, wait_s=1, deadline=deadline),
                preserve_on_fence_loss=True, deadline=deadline)

        try:
            while os.getppid() == parent and writer_lock.updater_state() == initial and not initial[0]:
                if carry:
                    try:
                        settle_carry()
                    except writer_lock.RunLockLost:
                        return
                    except Exception as error:
                        carry_failures += 1
                        delay = min(60, 2 ** min(carry_failures, 6))
                        _event("cloud_carry_settle_error", **_exception_observation(error),
                               retry_s=delay, failures=carry_failures)
                        time.sleep(delay)
                        continue
                    carry_failures = 0
                    if writer_lock.updater_state() != initial or not extract_llm._writer_current(full=True):
                        return
                    if carry:
                        if not extract_llm._context_memory_available():
                            return
                        time.sleep(2)
                        continue
                if not extract_llm._context_memory_available():
                    return
                try:
                    cfg = load_config()
                except ProviderSettingsUnreadable:
                    wait("provider_settings_unreadable")
                    continue
                settings = cloud_inference.resolve_cloud_settings(cfg)
                if index is not None:
                    retire = settings["mode"] != "on" or index >= settings["max_concurrency"]
                    if time.monotonic() - capacity_checked >= 60:
                        capacity_checked = time.monotonic()
                        capacity = memory_workers(settings["max_concurrency"])
                        retire = retire or index >= min(settings["max_concurrency"], capacity + bool(capacity))
                    if retire:
                        _event("cloud_worker_retired", reason=("cloud_mode_off"
                            if settings["mode"] != "on" else "capacity_reduced"))
                        return
                selection, errors = cloud_policy.resolve_profile(
                    cfg, route="reextract", purpose="backlog")
                limit = (selection["max_tokens"] if selection is not None and selection["max_tokens"] is not None
                         else extract_llm.MAX_TOKENS)
                if settings["mode"] != "on":
                    wait("cloud_mode_off")
                    continue
                if errors or selection is None:
                    wait("cloud_profile_unavailable")
                    continue
                blocked = (cloud_policy.global_block_reason(ledger, model=selection["model"])
                           or cloud_policy.budget_block_reason(
                               ledger, settings, max_tokens=limit, selection=selection))
                if blocked:
                    wait(blocked if blocked in _WAIT_CODES else "cloud_blocked")
                    continue
                started = time.monotonic()
                try:
                    if phase % 2 == 0:
                        waiting["code"] = None
                        with _phase_progress("extract"):
                            result = extract_llm.run_pending(ledger, limit=1, budget_s=900,
                                per_write_lock=True, workers=1, background=True,
                                cloud_only=True, prioritize_patients=True,
                                admitted_ids=extract_llm.legacy_admissions(ledger, cfg), carry=carry)
                        carry = result.get("carry", carry)
                        if result.get("updated"):
                            allow_exit_settle = False
                    else:
                        lock = writer_lock.acquire_run_lock()
                        if lock is None:
                            # a busy run lock keeps this worker on the same
                            # semantic turn: short wait/reacquire cycles under
                            # the usual parent/fence/memory guards, so an
                            # unexecuted semantic job stays ahead of the next
                            # extract attempt.
                            if (waiting["code"] != "writer_lock_busy"
                                    or time.monotonic() - waiting["at"] >= 60):
                                waiting.update(code="writer_lock_busy", at=time.monotonic())
                                _event("cloud_backlog_deferred", reason="writer_lock_busy", phase="semantic")
                            time.sleep(1)
                            continue
                        waiting["code"] = None
                        try:
                            # The loop guards ran before the lock wait: a
                            # parent exit or update generation change in that
                            # window must not enter run_due holding the run
                            # lock.
                            if (os.getppid() != parent
                                    or writer_lock.updater_state() != initial
                                    or initial[0]):
                                return
                            scfg = semantic_drain.runtime_config(cfg)[0]
                            with _phase_progress("semantic"):
                                result = semantic_drain.run_due(ledger, cfg, {"errors": []},
                                    time.monotonic() + scfg["job_budget_seconds"] + 30,
                                    max_jobs=1, cfg_path=CONF_PATH, run_lock_fd=lock,
                                    lane="backlog", cloud_only=True)
                        finally:
                            os.close(lock)
                    record = {"event": "cloud_backlog_pass", "pid": os.getpid(),
                        "phase": "extract" if phase % 2 == 0 else "semantic", "done": result.get("done", 0),
                        "done_unit": "messages" if phase % 2 == 0 else "jobs",
                        "completion_basis": "saved_work_units_not_patient_clinical_or_delivery_completion",
                        "elapsed_s": round(time.monotonic() - started, 3), "ts": time.time()}
                    record.update(_pass_timing_observation(result))
                    if not allow_exit_settle:
                        done = result.get("done")
                        record["done"] = done if type(done) is int and done >= 0 else 0
                        record["updated"] = True
                    record.update({key: result[key] for key in
                        ("selected", "deferred", "context_blocked", "skipped", "lock_lost", "slow_deferred",
                         "failed", "held_prior", "carried")
                        if type(result.get(key)) is int and result[key] >= 0})
                    reasons = result.get("defer_reasons")
                    total = record.get("deferred")
                    if type(total) is int and total > 0:
                        reasons = ({key: count for key, count in reasons.items()
                                    if key in _DEFER_REASONS and type(count) is int and count > 0}
                                   if isinstance(reasons, dict) else {})
                        if sum(reasons.values()) != total:
                            reasons = {"unclassified": total}
                        record["defer_reasons"] = reasons
                    if type(result.get("disk_low")) is bool:
                        record["disk_low"] = result["disk_low"]
                    calls = result.get("llm_calls")
                    if isinstance(calls, dict) and type(calls.get("calls")) is int and calls["calls"] >= 0:
                        record["llm_calls"] = {"calls": calls["calls"]}
                    if result.get("cloud_selection_incomplete") is True:
                        record["cloud_selection_incomplete"] = True
                        record["reason"] = {
                            "deadline": "cloud_selection_deadline",
                            "active_transaction": "cloud_selection_active_transaction",
                            "reader_unavailable": "cloud_selection_reader_unavailable",
                            "reader_changed": "cloud_selection_reader_changed",
                            "reader_close_failed": "cloud_selection_reader_close_failed",
                            "memory": "cloud_selection_memory",
                            "selection_busy": "cloud_selection_busy",
                        }.get(result.get("cloud_selection_reason"), "cloud_selection_incomplete")
                    elif phase % 2 and result.get("budget_exhausted") is True:
                        record["reason"] = "jev_backlog_budget_reserved"
                        record["jev_effect"] = "skipped_if_eligible"
                    print(json.dumps(record), flush=True)
                    if not allow_exit_settle:
                        return
                    if not result.get("done"):
                        time.sleep(2)
                except writer_lock.RunLockLost:
                    return
                except semantic_runtime.RuntimeGuardError:
                    time.sleep(2)
                except Exception as error:
                    _event("cloud_backlog_error", **_exception_observation(error),
                           phase="extract" if phase % 2 == 0 else "semantic")
                    if not allow_exit_settle:
                        return
                    time.sleep(10)
                phase += 1
        finally:
            if (carry and allow_exit_settle and owner_alive()
                    and writer_lock.updater_state() == initial
                    and extract_llm._writer_current(full=True)):
                try:
                    settle_carry()
                except Exception as error:
                    _event("cloud_carry_settle_error", **_exception_observation(error))


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--index", type=int, choices=range(32))
    args = parser.parse_args()
    if args.parent != os.getppid():
        return 2
    worker(args.parent, index=args.index)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
