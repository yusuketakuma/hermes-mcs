"""Journal bounded coordinated restart requests without guessing recovery outcomes."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import uuid

import mcs_config
from mcs_util import strict_json
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
TARGETS = frozenset({TARGET, "ai.hermes.gateway"})
# A dead/missing host cannot consume a cooperative request. Never bootstrap it.
HOST_RESTART_REASONS = frozenset({
    "standalone_check_stalled", "standalone_child_failing", "standalone_connector_down"})
_PENDING_KEYS = {"operation_id", "generation", "reserved_at", "request_id", "result", "reasons"}


def _stamp(value) -> bool:
    return type(value) in (int, float) and 0 <= value < 1e12 and math.isfinite(value)


def _identity(value) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _options(cfg: dict) -> tuple[bool, int, int] | None:
    if type(cfg) is not dict:
        return None
    section = cfg.get("auto_recovery", {})
    if mcs_config._auto_recovery(section) is not None:
        return None
    return (section.get("enabled", True),
            section.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
            section.get("window_s", DEFAULT_WINDOW_S))


def _valid_pending(row) -> bool:
    return (type(row) is dict and set(row) == _PENDING_KEYS
            and _identity(row["operation_id"]) and _identity(row["generation"])
            and _stamp(row["reserved_at"])
            and row["result"] in ("reserved", "requested", "unknown")
            and (row["request_id"] is None or _identity(row["request_id"]))
            and (row["result"] != "requested" or row["request_id"] is not None)
            and (row["result"] != "reserved" or row["request_id"] is None)
            and type(row["reasons"]) is list and bool(row["reasons"])
            and all(type(reason) is str and reason in HOST_RESTART_REASONS for reason in row["reasons"])
            and row["reasons"] == sorted(set(row["reasons"])))


def _valid_state(state) -> bool:
    if (type(state) is not dict or set(state) != {"v", "window_start", "attempts", "targets", "history"}
            or type(state["v"]) is not int or state["v"] != 1
            or not _stamp(state["window_start"])
            or type(state["attempts"]) is not int or not 0 <= state["attempts"] <= 10
            or type(state["targets"]) is not dict or set(state["targets"]) - TARGETS
            or type(state["history"]) is not list or len(state["history"]) > HISTORY_MAX):
        return False
    for row in state["targets"].values():
        if (type(row) is not dict or set(row) != {"pending"}
                or row["pending"] is not None and (
                    not _valid_pending(row["pending"]) or state["attempts"] < 1
                    or row["pending"]["reserved_at"] < state["window_start"])):
            return False
    for row in state["history"]:
        if (type(row) is not dict or set(row) != _PENDING_KEYS | {
                "target", "observation", "observed_at", "observed_generation"}
                or not _valid_pending({key: row[key] for key in _PENDING_KEYS})
                or row["target"] not in TARGETS or row["observation"] != "healthy_observed"
                or not _stamp(row["observed_at"]) or row["observed_at"] < row["reserved_at"]
                or not _identity(row["observed_generation"])
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


def run(report: dict, home: str, cfg: dict, now: float) -> dict:
    """Reserve before requesting; unresolved attempts never authorize another request."""
    result = {"enabled": False, "actions": []}
    options = _options(cfg)
    if options is None:
        return {**result, "reason": "config_invalid"}
    enabled, maximum, window_s = options
    if not enabled:
        return result
    if cfg.get("runtime_mode") != "standalone":
        return {**result, "reason": "runtime_unsupported"}
    if (type(report) is not dict or report.get("runtime_mode") != "standalone"
            or not _stamp(now) or not _stamp(report.get("watched_at")) or report["watched_at"] != now
            or report.get("state_reasons") is not None and (
                type(report["state_reasons"]) is not list
                or any(type(reason) is not str for reason in report["state_reasons"]))):
        return {**result, "reason": "observation_unverified"}
    result["enabled"] = True
    try:
        live = service.status(home, now=now)  # Fresh heartbeat, live PID and generation are required.
    except (OSError, ValueError, TypeError):
        return {**result, "reason": "host_unavailable"}
    if not _host_idle(live):
        return {**result, "reason": "host_busy"}
    from health_watch import standalone_host_reasons
    host_severity, host_reasons, _ = standalone_host_reasons(home, now, cfg, status=live)
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
        row = state["targets"].get(TARGET)
        pending = row["pending"] if row else None
        if now < state["window_start"] or any(
                item["pending"] and now < item["pending"]["reserved_at"]
                for item in state["targets"].values()):
            return {**result, "reason": "clock_unverified"}
        if pending is not None:
            if (live["generation"] != pending["generation"] and host_severity == "ok"
                    and not host_reasons):
                # This proves healthy observation, not that our request caused the restart.
                state["history"] = (state["history"] + [{**pending, "target": TARGET,
                    "observation": "healthy_observed", "observed_generation": live["generation"],
                    "observed_at": now}])[-HISTORY_MAX:]
                row["pending"] = None
                try:
                    _save_state(home, state)
                except (OSError, ValueError):
                    return {**result, "reason": "state_persist_failed"}
                return {**result, "reason": "healthy_observed"}
            return {**result, "reason": "outcome_unresolved"}
        if any(item["pending"] is not None for item in state["targets"].values()):
            return {**result, "reason": "outcome_unresolved"}
        reasons = sorted(HOST_RESTART_REASONS & set(host_reasons) & set(report.get("state_reasons") or []))
        if not reasons or report.get("status") not in ("degraded", "failed", "stale", "missing", "corrupt"):
            return {**result, "reason": "no_restart_cause"}
        if os.path.lexists(Path(home) / "data" / service.RESTART_FILE):
            return {**result, "reason": "restart_request_present"}
        if now - state["window_start"] >= window_s:
            state.update(window_start=now, attempts=0)
        if state["attempts"] >= maximum:
            return {**result, "reason": "budget_exhausted", "exhausted": [TARGET]}
        pending = {"operation_id": uuid.uuid4().hex, "generation": live["generation"],
                   "reserved_at": now, "request_id": None, "result": "reserved", "reasons": reasons}
        state["targets"][TARGET] = {"pending": pending}
        state["attempts"] += 1
        try:
            _save_state(home, state)
        except (OSError, ValueError):
            return {**result, "reason": "state_persist_failed"}
        try:
            request_id = service.request_restart(home, expected_generation=live["generation"])
            if not _identity(request_id):
                raise ValueError("restart_request_unverified")
        except Exception:
            # No replay: publication may have succeeded before its response was lost.
            pending["result"] = "unknown"
        else:
            pending.update(result="requested", request_id=request_id)
        result["actions"] = [{"target": TARGET, "action": "restart_request", **pending}]
        try:
            _save_state(home, state)
        except (OSError, ValueError):
            result["reason"] = "state_persist_failed"  # The durable reservation still blocks retries.
        return result
    finally:
        os.close(fd)
