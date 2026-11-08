"""Journal bounded coordinated restart requests without guessing recovery outcomes."""
from __future__ import annotations

import json
import inspect
import math
import os
from pathlib import Path
import re
import runpy
import sys
import time
import uuid

import mcs_config
from mcs_util import payload_hash, strict_json
from health_policy import freshness_deadline
import notify_spool
import safe_io
import service_contract
import update_journal
import writer_lock
from mcs_standalone import service

STATE_REL = os.path.join("data", "auto_recovery.json")
MAX_STATE_BYTES = 64 * 1024
HISTORY_MAX = 50
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_WINDOW_S = 24 * 3600
TARGET = service_contract.STANDALONE_LABEL
GATEWAY = "ai.hermes.gateway"
TARGETS = frozenset({TARGET, GATEWAY})
# A dead/missing host cannot consume a cooperative request. Never bootstrap it.
HOST_RESTART_REASONS = frozenset({
    "standalone_check_stalled", "standalone_child_failing", "standalone_connector_down"})
_PENDING_KEYS = {"operation_id", "generation", "reserved_at", "request_id", "result", "reasons"}
_GATEWAY_KEYS = {"pid", "started_at", "receipt_at"}
_GATEWAY_FAILURES = frozenset({
    "gateway_restart_auto_args_invalid", "gateway_restart_lock_fd_mismatch",
    "gateway_service_not_owned", "gateway_restart_launcher_unavailable",
    "gateway_restart_restore_hold", "gateway_service_unavailable", "gateway_service_ambiguous",
    "gateway_restart_target_changed", "gateway_restart_busy_timeout",
    "gateway_restart_unverifiable", "gateway_restart_spawn_failed"})


def _stamp(value) -> bool:
    return type(value) in (int, float) and 0 <= value < 1e12 and math.isfinite(value)


def _identity(value) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _generation(target, value) -> bool:
    return (type(value) is str and re.fullmatch(
        r"[0-9a-f]{64}" if target == GATEWAY else r"[0-9a-f]{32}", value) is not None)


def _pending_keys(target):
    return _PENDING_KEYS | _GATEWAY_KEYS if target == GATEWAY else _PENDING_KEYS


def _options(cfg: dict) -> tuple[bool, int, int] | None:
    if type(cfg) is not dict:
        return None
    section = cfg.get("auto_recovery", {})
    if mcs_config._auto_recovery(section) is not None:
        return None
    return (section.get("enabled", True),
            section.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
            section.get("window_s", DEFAULT_WINDOW_S))


def _valid_pending(row, target=TARGET) -> bool:
    reasons = {"worker_start_failed"} if target == GATEWAY else HOST_RESTART_REASONS
    results = ("reserved", "requested", "unknown", "failed", "process_replaced") if target == GATEWAY \
        else ("reserved", "requested", "unknown")
    common = (type(row) is dict and set(row) == _pending_keys(target)
            and _identity(row["operation_id"]) and _generation(target, row["generation"])
            and _stamp(row["reserved_at"])
            and row["result"] in results
            and (row["request_id"] is None or _identity(row["request_id"]))
            and (row["result"] != "requested" or row["request_id"] is not None)
            and (row["result"] != "reserved" or row["request_id"] is None or target == GATEWAY)
            and type(row["reasons"]) is list and bool(row["reasons"])
            and all(type(reason) is str and reason in reasons for reason in row["reasons"])
            and row["reasons"] == sorted(set(row["reasons"])))
    if not common or target != GATEWAY:
        return common
    return (type(row["pid"]) is int and row["pid"] > 0
            and _stamp(row["started_at"]) and 0 < row["started_at"] <= row["reserved_at"]
            and row["generation"] == payload_hash({"pid": row["pid"], "started_at": row["started_at"]})
            and row["request_id"] == row["operation_id"]
            and (row["receipt_at"] is None or _stamp(row["receipt_at"])
                 and row["receipt_at"] >= row["reserved_at"]))


def _valid_state(state) -> bool:
    if (type(state) is not dict or set(state) != {"v", "window_start", "attempts", "targets", "history"}
            or type(state["v"]) is not int or state["v"] != 1
            or not _stamp(state["window_start"])
            or type(state["attempts"]) is not int or not 0 <= state["attempts"] <= 10
            or type(state["targets"]) is not dict or set(state["targets"]) - TARGETS
            or type(state["history"]) is not list or len(state["history"]) > HISTORY_MAX):
        return False
    for target, row in state["targets"].items():
        if (type(row) is not dict or set(row) != {"pending"}
                or row["pending"] is not None and (
                    not _valid_pending(row["pending"], target) or state["attempts"] < 1
                    or row["pending"]["reserved_at"] < state["window_start"])):
            return False
    for row in state["history"]:
        if (type(row) is not dict or type(row.get("target")) is not str or row["target"] not in TARGETS):
            return False
        keys = _pending_keys(row["target"])
        if (set(row) != keys | {
                "target", "observation", "observed_at", "observed_generation"}
                or not _valid_pending({key: row[key] for key in keys}, row["target"])
                or row["target"] not in TARGETS or row["observation"] != "healthy_observed"
                or not _stamp(row["observed_at"]) or row["observed_at"] < row["reserved_at"]
                or row.get("receipt_at") is not None and row["receipt_at"] > row["observed_at"]
                or not _generation(row["target"], row["observed_generation"])
                or row["observed_generation"] == row["generation"]):
            return False
    return True


def _read(path: Path, limit: int):
    return strict_json(safe_io.read_private_bytes(path, limit))


def _load_state(home: str) -> dict:
    path = Path(home) / STATE_REL
    try:
        state = _read(path, MAX_STATE_BYTES)
    except FileNotFoundError:
        if os.path.lexists(path):
            raise ValueError("state_unverified") from None
        return {"v": 1, "window_start": 0, "attempts": 0, "targets": {}, "history": []}
    if not _valid_state(state):
        raise ValueError("state_unverified")
    return state


def _save_state(home: str, state: dict) -> None:
    if not _valid_state(state):
        raise ValueError("state_unverified")
    raw = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError("state_unverified")
    safe_io.atomic_write(os.path.join(home, STATE_REL), lambda stream: stream.write(raw),
                         mode=0o600, makedirs=False)


def _update_blocked(home: str) -> bool:
    data = Path(home) / "data"
    if os.path.lexists(data / writer_lock.UPDATE_MARKER_NAME):
        return True
    try:
        state = _read(data / "update_state.json", 1024 * 1024)
    except FileNotFoundError:
        if os.path.lexists(data / "update_state.json"):
            return True
        state = update_journal.default_state()  # The updater also treats first-run absence as idle.
    return (not update_journal.valid_state(state) or bool(state.get("_corrupt"))
            or state.get("applying") is not None
            or bool(state.get("stages")) or "restore_consent" in state
            or notify_spool.restore_writer_hold(str(data)) is not None)


def _host_idle(live: dict) -> bool:
    return (type(live) is dict and _identity(live.get("generation"))
            and type(live.get("exits")) is dict and type(live.get("connector")) is dict
            and _stamp(live.get("started_at"))
            and live.get("restarting") is False and live.get("update_in_progress") is False
            and "restart_reservation" in live and live["restart_reservation"] is None
            and "update_marker" in live and live["update_marker"] is None)


def _gateway_observation(home: str, cfg: dict, now: float) -> dict:
    import card_worker_status
    import environment_diagnostics

    notify = cfg.get("notify")
    if (sys.platform != "darwin" or type(notify) is not dict
            or mcs_config.validate_notify(notify)
            or notify.get("interactive") not in ("slack", "discord")):
        raise ValueError("gateway_unavailable")
    transport = notify["interactive"]
    profile = notify[transport]["profile"]
    data = Path(home) / "data"
    path = card_worker_status._path(data, transport, profile)
    record = _read(path, 2048)
    if (type(record) is not dict or set(record) != {"version", "reason", "pid", "at", "started_at"}
            or record["version"] != card_worker_status.VERSION
            or record["reason"] not in ("worker_started", "worker_start_failed")
            or type(record["pid"]) is not int or record["pid"] <= 0
            or not _stamp(record["at"]) or record["at"] > now
            or not _stamp(record["started_at"]) or not 0 < record["started_at"] <= record["at"]):
        raise ValueError("gateway_unavailable")
    deadline = time.monotonic() + 3
    pids = [environment_diagnostics.launchd_pid(f"{domain}/{os.getuid()}/{GATEWAY}", deadline=deadline)
            for domain in ("gui", "user")]
    if (any(pid is not None and (type(pid) is not int or pid <= 0) for pid in pids)
            or {pid for pid in pids if pid is not None} != {record["pid"]}):
        raise ValueError("gateway_unavailable")
    birth = environment_diagnostics.process_started_at(record["pid"], deadline=deadline)
    if type(birth) not in (int, float) or birth != record["started_at"]:
        raise ValueError("gateway_unavailable")
    worker = card_worker_status.status(data, transport, profile)
    expected = "started" if record["reason"] == "worker_started" else "disabled"
    if worker != {"state": expected, "reason": record["reason"]} or _read(path, 2048) != record:
        raise ValueError("gateway_unavailable")
    identity = {"pid": record["pid"], "started_at": birth}
    return {**identity, "generation": payload_hash(identity), "healthy": expected == "started",
            "reasons": [] if expected == "started" else ["worker_start_failed"]}


def _load_gateway_restart():
    try:
        path = Path(__file__).resolve().parents[2] / "deployment/recovery/mcs_recover.py"
        request = runpy.run_path(str(path))["request_gateway_restart"]
        parameters = inspect.signature(request).parameters
        if not all(name in parameters and parameters[name].kind in (
                inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                for name in ("expected_pid", "request_id", "update_lock_fd")):
            return None
        return request
    except Exception:
        return None


def _gateway_previous_clear(home: str, now: float) -> bool:
    path = Path(home) / "data/gateway_restart.json"
    try:
        row = _read(path, 4096)
    except FileNotFoundError:
        return not os.path.lexists(path)
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        return False
    if (type(row) is not dict or not _stamp(row.get("at")) or row["at"] > now
            or "request_id" in row and not _identity(row["request_id"])):
        return False
    keys = set(row) - {"request_id"}
    if row.get("status") == "failed":
        if type(row.get("error")) is not str or row["error"] not in _GATEWAY_FAILURES:
            return False
        if row["error"] == "gateway_restart_target_changed":
            return (keys == {"status", "at", "error", "expected_pid", "observed_pid"}
                    and type(row["expected_pid"]) is int and row["expected_pid"] > 0
                    and type(row["observed_pid"]) is int and row["observed_pid"] >= 0
                    and row["expected_pid"] != row["observed_pid"])
        return keys == {"status", "at", "error"}
    if row.get("status") == "supervisor_restart_verified":
        return (keys == {"status", "at", "service", "previous_pid", "pid"}
                and row["service"] in (f"gui/{os.getuid()}/{GATEWAY}", f"user/{os.getuid()}/{GATEWAY}")
                and type(row["pid"]) is int and row["pid"] > 0
                and (row["previous_pid"] is None or type(row["previous_pid"]) is int
                     and row["previous_pid"] > 0)
                and row["pid"] != row["previous_pid"])
    # A later native PID cannot prove a delayed restart helper has finished acting.
    # Requested/unknown receipts stay held regardless of elapsed time or replacement.
    return False


def _gateway_receipt(home: str, pending: dict, now: float, cfg: dict) -> bool:
    try:
        row = _read(Path(home) / "data/gateway_restart.json", 4096)
        if (type(row) is not dict or row.get("request_id") != pending["operation_id"]
                or not _stamp(row.get("at")) or not pending["reserved_at"] <= row["at"] <= now
                or now - row["at"] > freshness_deadline(cfg)
                or pending["receipt_at"] is not None and row["at"] <= pending["receipt_at"]):
            return False
        identities = [row[key] for key in ("expected_pid", "previous_pid") if key in row]
        if not identities or any(type(pid) is not int or pid != pending["pid"] for pid in identities):
            return False
        status = row.get("status")
        if status == "supervisor_restart_verified":
            if type(row.get("pid")) is not int or row["pid"] <= 0 or row["pid"] == pending["pid"]:
                return False
            status = "process_replaced"
        if status not in ("requested", "unknown", "failed", "process_replaced"):
            return False
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        return False
    pending.update(result=status, receipt_at=row["at"])
    return True


def run(report: dict, home: str, cfg: dict, now: float) -> dict:
    """Reserve before requesting; unresolved attempts never authorize another request."""
    result = {"enabled": False, "actions": []}
    options = _options(cfg)
    if options is None:
        return {**result, "reason": "config_invalid"}
    enabled, maximum, window_s = options
    if not enabled:
        return result
    mode = cfg.get("runtime_mode", "hermes")
    if mode not in ("standalone", "hermes"):
        return {**result, "reason": "runtime_unsupported"}
    if (type(report) is not dict or report.get("runtime_mode", "hermes") != mode
            or not _stamp(now) or not _stamp(report.get("watched_at")) or report["watched_at"] != now
            or report.get("state_reasons") is not None and (
                type(report["state_reasons"]) is not list
                or any(type(reason) is not str for reason in report["state_reasons"]))):
        return {**result, "reason": "observation_unverified"}
    result["enabled"] = True
    target = TARGET if mode == "standalone" else GATEWAY
    try:
        if target == TARGET:
            live = service.status(home, now=now)  # Fresh heartbeat, live PID and generation are required.
            if not _host_idle(live):
                return {**result, "reason": "host_busy"}
            from health_watch import standalone_host_reasons
            severity, reasons, _ = standalone_host_reasons(home, now, cfg, status=live)
            live.update(healthy=severity == "ok" and not reasons, reasons=reasons)
        else:
            live = _gateway_observation(home, cfg, now)
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        return {**result, "reason": "host_unavailable" if target == TARGET else "gateway_unavailable"}
    try:
        fd = writer_lock.acquire_run_lock(os.path.join(home, "data", "update.lock"))
    except OSError:
        return {**result, "reason": "update_lock_unavailable"}
    if fd is None:
        return {**result, "reason": "update_busy"}
    try:
        try:
            if _update_blocked(home):
                return {**result, "reason": "update_or_restore_held"}
            state = _load_state(home)
        except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
            return {**result, "reason": "state_unverified"}
        row = state["targets"].get(target)
        pending = row["pending"] if row else None
        if now < state["window_start"] or any(
                item["pending"] and (now < item["pending"]["reserved_at"]
                    or item["pending"].get("receipt_at") is not None
                    and now < item["pending"]["receipt_at"])
                for item in state["targets"].values()):
            return {**result, "reason": "clock_unverified"}
        if pending is not None:
            receipt_changed = target == GATEWAY and _gateway_receipt(home, pending, now, cfg)
            if live["generation"] != pending["generation"] and live["healthy"]:
                # This proves healthy observation, not that our request caused the restart.
                state["history"] = (state["history"] + [{**pending, "target": target,
                    "observation": "healthy_observed", "observed_generation": live["generation"],
                    "observed_at": now}])[-HISTORY_MAX:]
                row["pending"] = None
                try:
                    _save_state(home, state)
                except (OSError, ValueError):
                    return {**result, "reason": "state_persist_failed"}
                return {**result, "reason": "healthy_observed"}
            if receipt_changed:
                try:
                    _save_state(home, state)
                except (OSError, ValueError):
                    return {**result, "reason": "state_persist_failed"}
            return {**result, "reason": "outcome_unresolved"}
        if any(item["pending"] is not None for item in state["targets"].values()):
            return {**result, "reason": "outcome_unresolved"}
        allowed = {"worker_start_failed"} if target == GATEWAY else HOST_RESTART_REASONS
        reasons = sorted(allowed & set(live["reasons"]) & set(report.get("state_reasons") or []))
        if not reasons or report.get("status") not in ("degraded", "failed", "stale", "missing", "corrupt"):
            return {**result, "reason": "no_restart_cause"}
        if target == GATEWAY and (report.get("status") not in ("degraded", "failed")
                or not _stamp(report.get("health_at"))
                or not 0 <= now - report["health_at"] <= freshness_deadline(cfg)):
            return {**result, "reason": "observation_unverified"}
        if target == TARGET and service.automatic_restart_blocked(home, live["generation"]):
            return {**result, "reason": "restart_request_present"}
        if target == GATEWAY and not _gateway_previous_clear(home, now):
            return {**result, "reason": "gateway_request_unresolved"}
        gateway_request = _load_gateway_restart() if target == GATEWAY else None
        if target == GATEWAY and gateway_request is None:
            return {**result, "reason": "gateway_helper_unavailable"}
        if now - state["window_start"] >= window_s:
            state.update(window_start=now, attempts=0)
        if state["attempts"] >= maximum:
            return {**result, "reason": "budget_exhausted", "exhausted": [target]}
        pending = {"operation_id": uuid.uuid4().hex, "generation": live["generation"],
                   "reserved_at": now, "request_id": None, "result": "reserved", "reasons": reasons}
        if target == GATEWAY:
            pending.update(pid=live["pid"], started_at=live["started_at"], receipt_at=None,
                           request_id=pending["operation_id"])
        state["targets"][target] = {"pending": pending}
        state["attempts"] += 1
        try:
            _save_state(home, state)
        except (OSError, ValueError):
            return {**result, "reason": "state_persist_failed"}
        try:
            if target == TARGET:
                request_id = service.request_restart(home, expected_generation=live["generation"])
            else:
                if gateway_request(str(Path(home) / "data"), str(Path.home() / "Library/LaunchAgents"),
                        os.getuid(), expected_pid=live["pid"], request_id=pending["operation_id"],
                        update_lock_fd=fd) is not True:
                    raise ValueError("restart_request_unverified")
                request_id = pending["operation_id"]
            if not _identity(request_id):
                raise ValueError("restart_request_unverified")
        except Exception:
            # No replay: publication may have succeeded before its response was lost.
            pending["result"] = "unknown"
        else:
            pending.update(result="requested", request_id=request_id)
        result["actions"] = [{"target": target, "action": "restart_request", **pending}]
        try:
            _save_state(home, state)
        except (OSError, ValueError):
            result["reason"] = "state_persist_failed"  # The durable reservation still blocks retries.
        return result
    finally:
        os.close(fd)
