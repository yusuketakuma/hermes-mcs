"""Run bounded cloud-only backlog workers under the existing resident lifecycle."""
from __future__ import annotations

from contextlib import contextmanager, closing
import fcntl
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import _mcs_path  # noqa: F401

import cloud_send_admission
from run_log_install import current_helper, helper_path

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
# CloudFallback carries one of these fixed codes (never provider text).
_CLOUD_FALLBACK_CODES = frozenset({
    "cloud_profile_invalid", "cloud_unit_changed", "cloud_repair_invalid", "cloud_send_blocked",
    "cloud_unit_failed", "cloud_incomplete_response", "cloud_invalid_response",
    "cloud_owner_stopped", "cloud_validation_failed", "cloud_local_context_unavailable",
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
    elif (type(error).__name__ == "CloudFallback" and len(error.args) == 1
            and type(error.args[0]) is str and error.args[0] in _CLOUD_FALLBACK_CODES):
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


_TRANSPORT_SECONDS = ("llm_transport_s", "llm_reacquire_s", "jev_transport_s", "jev_reacquire_s",
                      "select_transport_s", "select_reacquire_s")
_LOCK_HOLD_KEYS = ("hold_max_s", "hold_sum_s", "hold_count")


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
    if isinstance(detail, dict) and detail.get("kind") in ("llm", "jev", "select"):
        record["run_lock_lost_detail"] = {
            "kind": detail["kind"], **{key: seconds(detail.get(key))
                for key in ("transport_s", "reacquire_s", "window_s")}}
    hold = result.get("lock_hold")
    if isinstance(hold, dict):
        record["lock_hold"] = {key: seconds(hold.get(key)) for key in _LOCK_HOLD_KEYS}
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


def _open_lock_file(path):
    """Open one trusted lock leaf: a private regular file of this user, never a link."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError("cloud_lock_untrusted")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _lock(path):
    fd = _open_lock_file(path)
    held = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        held = True
        return fd
    finally:
        if not held:
            os.close(fd)


# Patient-context worker roles. Every context child holds ROLE_CLOUD, and every local lane
# serving patient context holds ROLE_LOCAL, as a shared lock for as long as it serves. A
# worker of one role probes the other role only, never its own: flock would see itself.
ROLE_CLOUD = "context_workers.lock"
ROLE_LOCAL = "context_local_workers.lock"
_ROLE_PROBE_TRIES = 100   # 5 ms apart: about half a second


@contextmanager
def _role_probe(directory, name):
    """Serialise holders and probers of one role, so that two probers never see each other.

    Each holds the mutex for microseconds; one that is not free within the tries is unavailable."""
    fd = _open_lock_file(directory / (name + ".probe"))
    try:
        for attempt in range(_ROLE_PROBE_TRIES):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == _ROLE_PROBE_TRIES - 1:
                    raise
                time.sleep(0.005)
        yield
    finally:
        os.close(fd)


def _role_directory(root):
    directory = Path(root) / "data" / "cloud_locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def hold_role(root, name):
    """Announce this process in one role; the fd holds it until closed. None when not held now,
    which the caller retries on its next pass."""
    if name not in (ROLE_CLOUD, ROLE_LOCAL):
        raise ValueError("context_role_invalid")
    try:
        directory = _role_directory(root)
        with _role_probe(directory, name):
            fd = _open_lock_file(directory / name)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BaseException:
                os.close(fd)
                raise
            return fd
    except (OSError, ValueError):
        return None


def role_alive(root, name):
    """True while any process holds the role, False when none does, None when unobservable.

    Never call it for a role this process holds itself."""
    if name not in (ROLE_CLOUD, ROLE_LOCAL):
        raise ValueError("context_role_invalid")
    try:
        directory = _role_directory(root)
        with _role_probe(directory, name):
            fd = _open_lock_file(directory / name)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            finally:
                os.close(fd)   # closing also drops the probe's own exclusive lock
            return False
    except (OSError, ValueError):
        return None


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


_WORKER_BYTES = 256 * 1024 * 1024
_QUICK_EXIT_S = 60
_RESTART_BACKOFF_MAX_S = 600


def memory_headroom():
    """Return free bytes above the foreground reserve, or None while memory is unobservable."""
    try:
        if sys.platform == "darwin":
            import llm_availability
            if llm_availability.memory_pressure_critical():
                return -1
            env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
            total = int(subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True,
                                       text=True, timeout=2, check=True, env=env).stdout.strip())
            output = subprocess.run(["/usr/bin/memory_pressure", "-Q"], capture_output=True,
                                    text=True, timeout=2, check=True, env=env).stdout
            match = re.search(r"System-wide memory free percentage:\s*(\d+)%", output)
            if match is None or not 0 <= int(match[1]) <= 100:
                return None
            available, reserve = total * int(match[1]) // 100, total // 10
        else:
            values = dict(re.findall(r"^(MemAvailable|MemTotal):\s*(\d+) kB$",
                                     Path("/proc/meminfo").read_text(), re.M))
            total, available = int(values["MemTotal"]) * 1024, int(values["MemAvailable"]) * 1024
            # Small Linux standalone hosts keep at least 1 GiB for foreground work.
            reserve = max(1 << 30, total // 10)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None
    if total <= 0 or not 0 <= available <= total:
        return None
    return available - reserve


def memory_workers(limit):
    """Bound the pool by the shared memory headroom at 256 MiB per worker."""
    headroom = memory_headroom()
    return 0 if headroom is None else max(0, min(limit, headroom // _WORKER_BYTES))


def capacity_reduced(index, limit):
    """Whether an indexed worker is past the pool's memory target; one worker of
    hysteresis, because its own resident memory is in the sample."""
    capacity = memory_workers(limit)
    return index >= min(limit, capacity + bool(capacity))


class CloudBacklogPool:
    """Own child processes only while the normal lane-2 resident is alive."""
    def __init__(self, root, *, context_only=False):
        self.root = Path(root)
        self.context_only = context_only
        self.children = []
        self.indices = {}
        self.started = {}
        self.quick_exits = 0
        self.retry_at = 0.0
        self.lock = None
        self.last_sync = 0.0
        self.failed = False
        # The context pool's owner also keeps the stored-message workers of a
        # bounded reopening: they spawn only while that window is open.
        self.backlog = CloudBacklogPool(root) if context_only else None

    def sync(self, cfg, *, ledger=None):
        """Return True only when a pass finished and no pool in the chain is
        in a failure streak; a throttled return decided nothing. The owner
        reads this, not ``last_sync``: a nested pool that raised is throttled
        on the next call, and the outer pass alone must not count as recovery."""
        decided = self.backlog.sync(cfg, ledger=ledger) if self.backlog is not None else False
        now = time.monotonic()
        if now - self.last_sync >= 10:
            self.last_sync = now
            try:
                self._decide(cfg, ledger, now)
            except Exception:
                self.failed = True
                raise
            self.failed = False
            decided = True
        return decided and not (self.failed or (self.backlog is not None and self.backlog.failed))

    def _decide(self, cfg, ledger, now):
        import cloud_inference
        import cloud_policy
        settings = cloud_inference.resolve_cloud_settings(cfg)
        self._reap(now)
        if not self._admits(cfg, settings, ledger):
            # Children retire by policy while the gate is closed: those exits
            # are not crashes and never delay a restart once it reopens.
            self.quick_exits, self.retry_at = 0, 0.0
            return
        directory = self.root / "data"
        if self.lock is None:
            self.lock = _lock(directory / ("context_pool.lock" if self.context_only else "cloud_pool.lock"))
            if self.lock is None:
                return
        self._reap(now)
        target = memory_workers(settings["max_concurrency"])
        # Keep one worker of hysteresis: its resident memory changes the sample.
        if self.children and target > 0 and abs(target - len(self.children)) <= 1:
            target = min(settings["max_concurrency"], len(self.children))
        if target <= len(self.children) or now < self.retry_at:
            return
        helper = current_helper(self.root, installed=helper_path)
        for _ in range(max(0, target - len(self.children))):
            index = next(i for i in range(32) if i not in self.indices.values())
            child = subprocess.Popen(
                [sys.executable, "-I", helper, "exec", "--data-dir", str(directory), "--",
                 sys.executable, str(Path(__file__).resolve()), "--parent", str(os.getpid()),
                 "--index", str(index), *(["--context-only"] if self.context_only else [])],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, env={**os.environ, cloud_policy.BACKLOG_WORKER_ENV: "0" if self.context_only else "1"})
            self.children.append(child)
            self.indices[child] = index
            self.started[child] = now

    def _admits(self, cfg, settings, ledger):
        """Return whether mode, routes, key, profile and budget let this pool start children."""
        import cloud_policy
        if settings["mode"] != "on":
            # Workers retire at the idle boundary after paid carry is settled.
            return False
        if self.context_only:
            import patient_context_refresh
            if not patient_context_refresh.options(cfg)["enabled"]:
                return False
            # Cloud availability is decided inside run_due. A missing key or
            # a spend hold must not hide its existing local fallback/recovery.
        else:
            extract_open = cloud_policy.reextract_planned(settings)
            semantic_open = ("semantic_backlog" in settings["routes"]
                             and cloud_policy.semantic_backlog_window(settings))
            if not (extract_open or semantic_open):
                # Existing workers settle paid carry before retiring.
                return False
            if not cloud_policy.selected_key(self.root):
                return False
            selection, errors = cloud_policy.resolve_profile(
                cfg, route="reextract" if extract_open else "semantic_backlog",
                purpose="backlog", root=self.root, ledger=ledger, ordered=True)
            if errors or selection is None:
                return False
        return True

    def _reap(self, now):
        """Forget exited children; a child that exits right after starting backs off its restart."""
        alive = [p for p in self.children if p.poll() is None]
        lifetimes = [now - self.started.pop(child, float("-inf")) for child in self.children if child not in alive]
        if any(lifetime >= _QUICK_EXIT_S for lifetime in lifetimes):
            self.quick_exits, self.retry_at = 0, 0.0
        elif lifetimes:
            # Children that exit together are one failure, not one per child.
            self.quick_exits += 1
            self.retry_at = now + min(_RESTART_BACKOFF_MAX_S, 10 * 2 ** min(self.quick_exits, 6))
        self.children = alive
        self.indices = {p: index for p, index in self.indices.items() if p in alive}

    def close(self):
        if self.backlog is not None:
            self.backlog.close()
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
        self.started = {}
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None


# Fixed waiting-reason codes: a worker may only report one of these — never a
# raw block reason, config value, id or any provider detail.
_WAIT_CODES = {"paused", "circuit_open", "pricing_unverified", "profile_invalid",
               "daily_cap", "daily_cost_cap", "monthly_cap", "provider_budget_missing",
               "provider_daily_cap", "provider_monthly_cap", "window_history_unverified",
               "window_cap", "expired", "not_started",
               # fixed circuit failure classes (semantic_runtime): global_block_reason returns one while open
               "auth_error", "payment_required", "pricing_unverified", "contract_error", "transport",
               "timeout", "rate_limited", "http_402", "http_429", "http_529", "http_5xx"}

# Fixed defer-reason vocabulary reported by extract_llm.run_pending — a raw
# error class, config value or id is never a valid reason in the pass record.
_DEFER_REASONS = {"deadline", "probe_unobserved", "admission", "endpoint_down",
                  "not_sent", "busy", "cloud_fallback", "circuit_open",
                  "prepare_blocked", "currentness_unverified", "cloud_guard",
                  "commit_rejected", "prior_unavailable", "worker_error", "write_unavailable",
                  "update_fence", "lease_renewal_failed", "checkpoint_unverified", "quality_checkpoint_unverified",
                  "repair_unavailable", "unclassified",
                  "cloud_unit_unresolved", "cloud_owner_transaction", "cloud_deadline",
                  "cloud_budget_blocked", "cloud_pricing_unverified",
                  "cloud_not_sent", "cloud_scope_or_budget", "llm_turn_busy", "budget_short",
                  "paused", "mode_off", "out_of_scope", "jev_skip_unverified",
                  "prepare_deadline", "prepare_source_changed", "prepare_cloud_disabled",
                  "prepare_profile_unavailable", "prepare_medication_changed",
                  "prepare_context_invalid", "prepare_database_unavailable", "other",
                  "prepare_context_load_superseded",
                  "cloud_capacity", "cloud_profile_unavailable", "cloud_source_unverified",
                  "cloud_target_unverified", "audit_pending", "loops_incomplete",
                  "local_rebuild_proof_changed", "local_rebuild_unverified"}
# Semantic gates emit only these branch labels; no provider text or source id.
_SEMANTIC_STOP_REASONS = frozenset({
    "fact_stage_incomplete", "fact_stage_retryable", "fact_stage_hard_fail",
    "fact_repaired_producer_unverified", "fact_repaired_contract_changed",
    "fact_cached_rebuild_unverified",
    "fact_extract_deadline", "fact_extract_model", "fact_extract_incomplete",
    "fact_coverage_incomplete", "fact_audit_incomplete", "fact_audit_deadline",
    "fact_audit_wait", "fact_repair_deadline", "empty_repair_unverified",
    "empty_repair_marker_missing", "empty_repair_marker_invalid",
    "empty_repair_marker_binding", "empty_repair_marker_currentness",
    "empty_repair_candidate_missing", "empty_repair_candidate_invalid",
    "empty_repair_candidate_currentness", "empty_repair_candidate_binding",
    "empty_repair_marker_link", "empty_repair_claims_invalid",
    "empty_repair_claim_binding", "empty_repair_prior_unverified",
    "empty_repair_provenance_unverified", "empty_repair_audit_not_ready",
    "empty_repair_validation_unavailable",
})
_DEFER_REASONS.update(_SEMANTIC_STOP_REASONS)
_PREPARE_CONTEXT_REJECTION_CODES = frozenset({
    "prepare_context_invalid", "prepare_context_other", "prepare_context_load_unverifiable",
    "prepare_context_load_superseded", "prepare_context_load_reference_changed",
    "prepare_context_medication_input_invalid",
    "prepare_context_medication_snapshot_invalid", "prepare_context_prepare_transaction_owned",
    "prepare_context_prepare_options_invalid", "prepare_context_prepare_budget_exceeded",
    "prepare_context_prepare_medication_invalid", "prepare_context_prepare_source_changed",
    "prepare_context_prepare_snapshot_invalid",
})
_DEFER_REASONS.update(_PREPARE_CONTEXT_REJECTION_CODES)
_COMMIT_REJECTION_REASONS = frozenset({
    "transaction", "source_or_owner", "checkpoint", "prior_changed", "quality_proof",
    "completeness", "context", "reference", "medication", "retained_proof", "result_seal",
    "insert_fence", "cloud_guard", "superseded_guard",
})
_COMPLETENESS_REJECTION_REASONS = frozenset("completeness_" + reason for reason in (
    "shape", "layout", "pending", "adopted", "terminal", "coverage", "validation"))
_COMMIT_REJECTION_REASONS |= _COMPLETENESS_REJECTION_REASONS
_COMMIT_REJECTION_CODES = frozenset("commit_" + reason for reason in _COMMIT_REJECTION_REASONS)
_DEFER_REASONS.update(_COMMIT_REJECTION_CODES)
# Defer reasons that keep a bounded claim instead of re-heading the queue:
# the unit's evidence cannot be proven this pass — an unverifiable head
# must not starve the tail under a limited claim window.
# CloudFallback's fixed codes, observed per pass apart from the defer reasons ("other" otherwise).
_FALLBACK_CODES = frozenset({
    "cloud_incomplete_response", "cloud_invalid_response", "cloud_owner_stopped", "cloud_validation_failed",
    "cloud_local_context_unavailable", "cloud_profile_invalid", "cloud_unit_changed", "cloud_repair_invalid",
    "cloud_send_blocked", "cloud_unit_failed", "other"})

_HOLD_DEFER_REASONS = frozenset(
    {"cloud_unit_unresolved", "cloud_source_unverified", "currentness_unverified",
     "prepare_profile_unavailable", "local_rebuild_proof_changed", "local_rebuild_unverified"})


def _defer(waiting, code):
    """Wait one fixed interval; a persistent hold emits a heartbeat, not one event per retry."""
    if code != waiting["code"] or time.monotonic() - waiting["at"] >= 60:
        waiting.update(code=code, at=time.monotonic())
        _event("cloud_backlog_deferred", reason=code)
    time.sleep(10)


def _note_provider(seen, settings):
    """One fixed event when the selected provider changes between this worker's reads."""
    provider = settings.get("provider")
    if seen["provider"] is not None and provider != seen["provider"]:
        _event("cloud_provider_changed", previous=seen["provider"], provider=provider)
    seen["provider"] = provider


# Each semantic lock try polls at the shared RUN_LOCK_POLL_S for this long
# (three tries before yielding to the extract turn), so a cloud semantic job
# competes for released windows on equal terms with the local lanes' polls.
_SEMANTIC_LOCK_TRY_S = 10.0


def _poll_run_lock(writer_lock, seconds, fence):
    """Poll the run lock for up to ``seconds``; None once it expires or ``fence`` fails."""
    until = time.monotonic() + seconds
    while True:
        fd = writer_lock.acquire_run_lock()
        if fd is not None:
            return fd
        remaining = until - time.monotonic()
        if remaining <= 0 or not fence():
            return None
        time.sleep(min(writer_lock.RUN_LOCK_POLL_S, remaining))


def worker(parent, *, index=None, context_only=False):
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
    ocr_turn = True
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
        if context_only:
            _context_worker(ledger, initial, index)
            return
        carry = []
        allow_exit_settle = True
        waiting = {"code": None, "at": 0.0}
        provider_seen = {"provider": None}
        carry_failures = 0
        semantic_lock_failures = 0
        capacity_checked = float("-inf")

        def wait(code):
            _defer(waiting, code)

        def settle_carry(trigger):
            deadline = time.monotonic() + 3.0
            held = len(carry)
            settled = extract_llm._settle_carry(
                ledger, carry, final=False, fence=initial,
                lock_context=extract_llm._write_lock(True, wait_s=1, deadline=deadline),
                preserve_on_fence_loss=True, deadline=deadline, report=False)[0]
            if any(type(settled.get(key)) is int and settled[key] > 0 for key in ("done", "held_prior", "deferred")):
                # A pass's `done` counts only what that run_pending call saved; work carried
                # out of it is saved (done) or released (deferred) here, so it is reported here.
                reasons = {key: count for key, count in (settled.get("defer_reasons") or {}).items()
                           if key in _DEFER_REASONS and type(count) is int and count > 0}
                _event("cloud_carry_settled", carried=held, kept=len(carry), done=settled["done"],
                       done_unit="messages", held_prior=settled["held_prior"],
                       deferred=settled["deferred"], defer_reasons=reasons, trigger=trigger)

        try:
            while os.getppid() == parent and writer_lock.updater_state() == initial and not initial[0]:
                if carry:
                    try:
                        settle_carry("loop")
                    except writer_lock.RunLockLost:
                        return
                    except Exception as error:
                        carry_failures += 1
                        # Jitter keeps sibling workers' settle retries from
                        # landing on the same contended second (thundering herd).
                        delay = min(60, 2 ** min(carry_failures, 6) * random.uniform(0.5, 1.5))
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
                _note_provider(provider_seen, settings)
                extract_open = cloud_policy.reextract_planned(settings)
                semantic_open = ("semantic_backlog" in settings["routes"]
                                 and cloud_policy.route_open("semantic_backlog", settings))
                routes_open = extract_open or semantic_open
                if index is not None:
                    retire = (settings["mode"] != "on" or not routes_open
                              or index >= settings["max_concurrency"])
                    if time.monotonic() - capacity_checked >= 60:
                        capacity_checked = time.monotonic()
                        retire = retire or capacity_reduced(index, settings["max_concurrency"])
                    if retire:
                        _event("cloud_worker_retired", reason=(
                            "cloud_mode_off" if settings["mode"] != "on"
                            else "cloud_route_closed" if not routes_open
                            else "capacity_reduced"))
                        return
                if routes_open and not (semantic_open if phase % 2 else extract_open):
                    phase += 1
                selection, errors = cloud_policy.resolve_profile(
                    cfg, route="semantic_backlog" if phase % 2 else "reextract",
                    purpose="backlog", ledger=ledger, ordered=True)
                if settings["mode"] != "on":
                    wait("cloud_mode_off")
                    continue
                if not routes_open:
                    wait("cloud_route_closed")
                    continue
                if errors or selection is None:
                    blocked = errors[0] if errors else None
                    if not blocked or str(blocked).startswith("config:"):
                        wait("cloud_profile_unavailable")
                    else:
                        wait(blocked if blocked in _WAIT_CODES else "cloud_blocked")
                    continue
                started = time.monotonic()
                try:
                    if phase % 2 == 0:
                        waiting["code"] = None
                        with _phase_progress("extract"):
                            result = None
                            try_ocr, ocr_turn = ocr_turn, not ocr_turn
                            # attachment pages never reopen with the stored-message backlog
                            if (try_ocr and not carry and cloud_policy.send_route("reextract")
                                    and isinstance(cfg.get('attachment_ocr'), dict)
                                    and cfg['attachment_ocr'].get('enabled') is True):
                                import attachment_inference
                                ocr = attachment_inference.run_pending(ledger, cfg, stage='cloud',
                                    deadline=time.monotonic() + 120, limit=1)
                                if ocr['done'] or ocr['deferred']:
                                    result = {**ocr, 'attachment_pages': True}
                            if result is None:
                                result = extract_llm.run_pending(ledger, limit=1, budget_s=900,
                                    per_write_lock=True, workers=1, background=True,
                                    cloud_only=True, prioritize_patients=True,
                                    admitted_ids=extract_llm.cloud_backlog_admissions(ledger, cfg), carry=carry)
                        carry = result.get("carry", carry)
                        if result.get("updated"):
                            allow_exit_settle = False
                    else:
                        lock = _poll_run_lock(writer_lock, _SEMANTIC_LOCK_TRY_S, lambda: (
                            os.getppid() == parent and writer_lock.updater_state() == initial))
                        if lock is None:
                            # Give a brief lock contention time to clear, but
                            # do not indefinitely starve the extract turn.
                            if (waiting["code"] != "writer_lock_busy"
                                    or time.monotonic() - waiting["at"] >= 60):
                                waiting.update(code="writer_lock_busy", at=time.monotonic())
                                _event("cloud_backlog_deferred", reason="writer_lock_busy", phase="semantic")
                            semantic_lock_failures += 1
                            time.sleep(1)
                            if semantic_lock_failures >= 3:
                                semantic_lock_failures = 0
                                phase += 1
                                _event("cloud_backlog_deferred", reason="writer_lock_yield", phase="semantic")
                            continue
                        semantic_lock_failures = 0
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
                        "done_unit": ("pages" if result.get('attachment_pages')
                                      else "messages" if phase % 2 == 0 else "jobs"),
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
                    codes = result.get("fallback_codes")
                    codes = ({key: count for key, count in codes.items()
                              if key in _FALLBACK_CODES and type(count) is int and count > 0}
                             if isinstance(codes, dict) else {})
                    if codes:
                        record["fallback_codes"] = codes
                    refilled = result.get("refilled")
                    if type(refilled) is int and 0 <= refilled <= 64:   # the extract pass's finite lookahead
                        record["refilled"] = refilled
                    if result.get("refill_stopped") is True:
                        # a failed replacement preparation stopped refilling: never silent
                        code = result.get("refill_stop_error")
                        record["refill_stopped"] = True
                        record["refill_stop_error"] = (code if type(code) is str and code in _REFILL_STOP_CODES
                                                        else "unclassified")
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
                            "run_lock_lost": "cloud_selection_run_lock_lost",
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
                    settle_carry("exit")
                except Exception as error:
                    _event("cloud_carry_settle_error", **_exception_observation(error))


_CONTEXT_PASS_REASONS = frozenset({
    "transaction_active", "patient_context_waiting", "patient_context_scan_incomplete", "patient_context_nothing_due",
    "patient_context_patient_invalid",
    "patient_context_readiness_unverified", "patient_context_generation_changed", "run_lock",
    "patient_context_selection_busy", "patient_context_selection_unavailable",
    "patient_context_completed_expired", "patient_context_capacity_or_deadline", "patient_context_admission_deferred",
    "patient_context_turn_busy", "patient_context_endpoint_unsent", "patient_context_endpoint_unavailable",
    "patient_context_local_capacity", "patient_context_source_changed", "patient_context_cloud_budget",
    "patient_context_generation_active", "patient_context_generation_clock_changed",
    "patient_context_generation_unverified",
    "patient_context_generation_daily_limit", "patient_context_generation_interval",
    "patient_context_cloud_not_sent", "patient_context_cloud_pending", "patient_context_cloud_source_changed",
    "patient_context_cloud_deferred", "patient_context_role_changed", "patient_context_other_role",
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


_REFILL_STOP_CODES = frozenset({"sqlite_error", "os_error", "value_error", "type_error", "key_error",
                                "runtime_error", "other"})


def _bounded_pass_code(value):
    """Identifier-shaped failure identity only; free text never qualifies."""
    return (type(value) is str and value.isascii() and value.isidentifier()
            and len(value) <= 80)


# Only exception classes the quality check handles may enter the pass log.
_QUALITY_EXCEPTION_NAMES = frozenset({
    "Deferred", "ValueError", "JSONDecodeError", "UnicodeError", "UnicodeDecodeError",
    "RuntimeError", "RecursionError", "RuntimeGuardError", "RuntimeOff", "RuntimeStale",
    "RuntimeBudget", "RuntimeBudgetShort", "LLMNotSent", "LLMRejected",
})


def _quality_detail_declared(value):
    """Fixed identifiers only: registered codes, pass reasons, deferral gates and exception type names."""
    import error_codes
    import patient_context_quality_controller
    return type(value) is str and (
        value in error_codes.KNOWN_ERROR_CODES or value in _CONTEXT_PASS_REASONS
        or value in patient_context_quality_controller.DEFERRAL_CODES
        or value in _QUALITY_EXCEPTION_NAMES)


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
        # by its fixed base; the suffix never enters the reason (a declared one is
        # recorded as quality_detail below).
        # failed/error carry safe_error_code output or a type name — both are
        # bounded identifiers, so they pass through verbatim.
        base = reason.partition(":")[0] if type(reason) is str else None
        record["reason"] = base if base in _CONTEXT_PASS_REASONS or (
                record["status"] in ("failed", "error") and _bounded_pass_code(base)) \
            else "unclassified"
    # Why a quality check did not finish: the declared code after ``..._quality_unavailable:``
    # (a registered error code, a pass reason or an exception type name), or the gate that
    # deferred the step. Anything else is recorded as unclassified, never as written.
    detail = result.get("quality_detail")
    if detail is None and type(reason) is str and reason.partition(":")[0] in (
            "source_quality_unavailable", "summary_quality_unavailable"):
        detail = reason.partition(":")[2] or None
    if record.get("reason") in ("source_quality_deferred", "summary_quality_deferred",
                                "source_quality_unavailable", "summary_quality_unavailable",
                                "source_quality_unknown", "summary_quality_unknown"):
        record["quality_detail"] = detail if _quality_detail_declared(detail) else "unclassified"
    for key, limit in (("done", 1), ("scanned", 8), ("scan_ms", 900000)):
        value = result.get(key)
        if type(value) is int and 0 <= value <= limit:
            record[key] = value
    lane = result.get("lane")
    if type(lane) is str and lane in ("A", "B", "C"):
        record["lane"] = lane
    # Save-stage detail: seconds spent waiting for the run lock (the wait
    # stops at 60) and the fixed code of a guard refusal.
    wait = result.get("lock_wait_s")
    if type(wait) is int and 0 < wait <= 60:
        record["lock_wait_s"] = wait
    guard = result.get("guard_error")
    if guard is not None:
        record["guard_error"] = guard if _bounded_pass_code(guard) else "unclassified"
    # The classified reason the cloud did not serve the unit; only declared codes pass.
    stop = result.get("cloud_refusal")
    if stop is not None:
        record["cloud_refusal"] = (stop if type(stop) is str and stop in cloud_send_admission.REFUSAL_CODES
                                   else "unclassified")
    refusal = result.get("scan_refusal")
    if refusal is not None:
        record["scan_refusal"] = refusal if type(refusal) is str and refusal in _SCAN_REFUSALS else "unclassified"
    # The first skipped patient's registered code or exception type name only.
    skipped = result.get("skip_patient_code")
    if skipped is not None:
        record["skip_patient_code"] = skipped if _bounded_pass_code(skipped) else "unclassified"
    # The generation quota's own denial reason (fixed identifiers only).
    generation = result.get("generation_reason")
    if generation is not None:
        record["generation_reason"] = (generation if type(generation) is str
                                       and generation in _CONTEXT_PASS_REASONS else "unclassified")
    covered = result.get("covered")
    if type(covered) is bool:
        record["covered"] = covered
    # Real per-pass bounds: one try-wide interrupt, 8 candidates x at most 2
    # waiting additions (quota-fail double count or two single-count scopes),
    # and at most 2 pending reads per candidate; at most one skip per candidate.
    # A patient busy with another worker counts once per candidate; patients
    # of the other worker role have their own budget of 64 per pass. These are
    # a child's bounds: only a local lane's takeover passes over patients that
    # wait for a child (up to 64 more ``waiting``), and its passes are not
    # recorded here.
    for key, limit in (("waiting", 16), ("scan_reads", 16), ("scan_interrupts", 1),
                       ("skip_patient_invalid", 8), ("unverified_records", 1 << 20),
                       ("wait_no_eligible_sources", 8), ("wait_no_due_lane", 16),
                       ("wait_generation_quota", 8), ("wait_readiness_unverified", 16),
                       ("wait_dependency_incomplete", 16), ("wait_patient_busy", 8),
                       ("wait_other_role", 64)):
        value = result.get(key)
        if type(value) is int and 0 <= value <= limit:
            record[key] = value
    return record


def deferral_delay(reason):
    """Seconds before a unit the cloud deferred is tried again: a full send slot clears within
    seconds, every other deferral (cap, circuit, pending result, waiting child) does not."""
    return 5 if reason == "patient_context_cloud_deferred:cloud_capacity" else 60


def _context_retry_delay(result):
    """Backoff for one context pass. selection_busy means another worker
    holds the selection lock (usually scanning, not necessarily doing work);
    a scan that found nothing startable rarely finds work seconds later. A full cloud slot is retried sooner than a daily cap."""
    if result.get("done"):
        return 1
    reason = result.get("reason")
    if reason == "patient_context_selection_busy":
        return 20
    base, _, code = reason.partition(":") if type(reason) is str else ("", "", "")
    if base == "patient_context_cloud_deferred":
        return deferral_delay(reason)
    if reason == "patient_context_scan_incomplete" and result.get("covered") is False and not result.get("scan_interrupts"):
        return 5  # The next page may still contain a due patient.
    if reason in ("patient_context_waiting", "patient_context_scan_incomplete", "patient_context_cloud_budget", "patient_context_other_role",
                  "patient_context_nothing_due"):
        # Every patient the pass reached is served by the other worker role, or none was due.
        return 60
    return 5


def _context_worker(ledger, initial, index):
    import cloud_inference
    import extract_llm_resident
    import local_llm
    import writer_lock
    from mcs_util import HOME, ProviderSettingsUnreadable, load_config

    capacity_checked = float("-inf")
    waiting = {"code": None, "at": 0.0}
    provider_seen = {"provider": None}
    role = None   # this child's ROLE_CLOUD hold; local lanes see the cloud lane served through it
    try:
        with local_llm.scheduling_scope(os.path.join(HOME, "data")):
            while owner_alive() and writer_lock.updater_state() == initial and not initial[0]:
                try:
                    cfg = load_config()
                except ProviderSettingsUnreadable:
                    # A settings commit in progress: wait here instead of exiting the child.
                    _defer(waiting, "provider_settings_unreadable")
                    continue
                waiting["code"] = None
                settings = cloud_inference.resolve_cloud_settings(cfg)
                _note_provider(provider_seen, settings)
                if (settings["mode"] != "on" or index is not None and index >= settings["max_concurrency"]
                        or not extract_llm_resident._context_memory_available()):
                    return
                if index is not None and time.monotonic() - capacity_checked >= 60:
                    capacity_checked = time.monotonic()
                    if capacity_reduced(index, settings["max_concurrency"]):
                        _event("cloud_worker_retired", reason="capacity_reduced")
                        return
                if role is None:
                    # A hold that failed is retried on every pass, so a live child never stays hidden.
                    role = hold_role(HOME, ROLE_CLOUD)
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
    finally:
        if role is not None:
            os.close(role)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--index", type=int, choices=range(32))
    parser.add_argument("--context-only", action="store_true")
    args = parser.parse_args()
    if args.parent != os.getppid():
        return 2
    worker(args.parent, index=args.index, context_only=args.context_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
