"""Observe configured notification delivery without changing its state."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import time

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import _mcs_path  # noqa: F401

import health_recovery
import mcs_config
from health_policy import CARD_STALL_S
from mcs_util import strict_json
import notify_scope
import safe_io

OBSERVATION_S = 3.0
_CLEANUP_S = 0.5
_STATUS_S = 1.0
WORKER_RESERVE_S = _STATUS_S + 0.5  # Allow the gateway witness to run before the status check.
MAX_INPUT_BYTES = 16384
MAX_OUTPUT_BYTES = 8192
_RESTART_STATES = frozenset({"requested", "unknown", "failed", "supervisor_restart_verified"})
_DELIVERY_ISSUES = ("waiting_without_progress", "outcome_unconfirmed")
_DELIVERY_STATES = frozenset({"waiting_without_progress", "progressing", "pending",
                              "outcome_unconfirmed", "idle", "unknown"})
_WORKER_REASONS = frozenset({"worker_started", "worker_start_failed", "worker_start_unverified",
                              "witness_missing", "witness_version", "witness_reason",
                              "witness_started_unknown", "launchd_pid_mismatch", "birth_mismatch"})
DELIVERY_REASONS = frozenset({"marker_present", "database_identity", "database_busy", "schema_mismatch",
                              "observation_timeout", "queue_unverified"})
_WORKER_DETAIL_REASONS = frozenset({"observation_timeout", "witness_missing", "witness_version",
                                    "witness_reason", "witness_started_unknown",
                                    "launchd_pid_mismatch", "birth_mismatch"})


def _stamp(value, now):
    return type(value) in (int, float) and 0 <= value < 1e12 and math.isfinite(value) and value <= now


def _empty(now):
    return {"observed_at": now,
            "worker": {"state": "unknown", "reason": "worker_start_unverified", "started_at": None},
            "delivery": {"state": "unknown", "reason": "observation_timeout", "issues": [],
                         "queued": None, "sending": None, "unknown": None,
                         "oldest_created_at": None, "last_progress_at": None},
            "restart_request": {"state": "unverified", "at": None}}


def _settings(cfg):
    if type(cfg) is not dict or cfg.get("runtime_mode", "hermes") != "hermes":
        return None
    notify = cfg.get("notify")
    if type(notify) is not dict or mcs_config.validate_notify(notify):
        return None
    transport = notify.get("interactive")
    if transport not in ("slack", "discord"):
        return None
    scope = notify_scope.delivery_scope(cfg)
    if scope is None:
        return None
    settings = {key: scope[key] for key in notify_scope.scope_fields(transport)}
    return {"runtime_mode": "hermes", "notify": {"interactive": transport,
            "route_epoch": notify_scope.route_epoch(cfg), transport: settings}}


def _restart(data, now):
    try:
        row = strict_json(safe_io.read_private_bytes(data / "gateway_restart.json", 4096))
        allowed = {"status", "at", "request_id", "error", "service", "previous_pid",
                   "pid", "expected_pid", "observed_pid"}
        if type(row) is not dict or set(row) - allowed or row.get("status") not in _RESTART_STATES \
                or not _stamp(row.get("at"), now):
            raise ValueError("restart_unverified")
        if "request_id" in row and (type(row["request_id"]) is not str
                                    or re.fullmatch(r"[0-9a-f]{32}", row["request_id"]) is None):
            raise ValueError("restart_unverified")
        status = row["status"]
        if status == "requested" and set(row) - {"status", "at", "request_id"}:
            raise ValueError("restart_unverified")
        if status in ("unknown", "failed") and (type(row.get("error")) is not str
                or re.fullmatch(r"gateway_[a-z0-9_]{1,80}", row["error"]) is None):
            raise ValueError("restart_unverified")
        if "service" in row and row["service"] not in (
                f"gui/{os.getuid()}/ai.hermes.gateway", f"user/{os.getuid()}/ai.hermes.gateway"):
            raise ValueError("restart_unverified")
        for key in ("pid", "expected_pid", "observed_pid", "previous_pid"):
            if key in row and row[key] is not None and (type(row[key]) is not int or row[key] < 0):
                raise ValueError("restart_unverified")
        if status == "supervisor_restart_verified" and not (
                set(row) - {"request_id"} == {"status", "at", "service", "previous_pid", "pid"}
                and type(row["pid"]) is int and row["pid"] > 0
                and (row["previous_pid"] is None or type(row["previous_pid"]) is int
                     and row["previous_pid"] > 0 and row["previous_pid"] != row["pid"])):
            raise ValueError("restart_unverified")
        return {"state": status, "at": row["at"]}
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        return {"state": "unverified", "at": None}


def _worker(home, cfg, now, deadline=None):
    try:
        deadline = time.monotonic() + OBSERVATION_S if deadline is None else deadline
        witness_deadline = deadline - _STATUS_S
        if time.monotonic() >= witness_deadline:
            return {"state": "unknown", "reason": "observation_timeout", "started_at": None}
        result = health_recovery._gateway_observation(str(home), cfg, now, deadline=witness_deadline)
        if result["healthy"] is True and result["reasons"] == []:
            return {"state": "started", "reason": "worker_started", "started_at": result["started_at"]}
        if result["healthy"] is False and result["reasons"] == ["worker_start_failed"]:
            return {"state": "disabled", "reason": "worker_start_failed", "started_at": result["started_at"]}
    except ValueError as error:
        if error.args and error.args[0] in _WORKER_DETAIL_REASONS:
            return {"state": "unknown", "reason": error.args[0], "started_at": None}
    except (OSError, TypeError, KeyError, RecursionError, UnicodeError):
        pass
    return {"state": "unknown", "reason": "worker_start_unverified", "started_at": None}


def _scope_sql(cfg):
    transport = cfg["notify"]["interactive"]
    scope = notify_scope.delivery_scope(cfg)
    clauses = ["r.transport=?", "r.route_epoch=?"]
    params = [transport, notify_scope.route_epoch(cfg)]
    for key in notify_scope.scope_fields(transport):
        if transport == "discord":
            clauses.append(f"(r.{key} IS NULL OR r.{key}='' OR r.{key}=?)")
        else:
            clauses.append(f"r.{key}=?")
        params.append(scope[key])
    return " AND ".join(clauses), params


def _identity(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("database_unverified")
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)


def _progress(db, source, where, params, now):
    column = "p.updated_at" if source == "parts" else "a.finished_at"
    tables = ("notification_render_parts p JOIN notification_renders r ON r.delivery_id=p.delivery_id"
              if source == "parts" else
              "notification_delivery_attempts a JOIN notification_renders r ON r.delivery_id=a.delivery_id")
    # Rows older than the stall window cannot prove recent progress, so the
    # window sits on the progress column itself: lossless for the MAX, unlike
    # r.updated_at which rollup bookkeeping leaves unchanged on some deliveries.
    state = ("p.state='delivered'" if source == "parts"
             else "r.state='delivered' AND a.state='delivered'")
    invalid = f"typeof({column}) NOT IN ('integer','real') OR {column}<0 OR {column}>?"
    sql = (f"SELECT MAX(CASE WHEN NOT ({invalid}) THEN {column} END), "
           f"COALESCE(SUM(CASE WHEN {invalid} THEN 1 ELSE 0 END),0) FROM {tables} "
           f"WHERE {where} AND {state} AND ({invalid} OR {column}>=?)")
    return db.execute(sql, [now, now, *params, now, now - CARD_STALL_S]).fetchone()


def _delivery(data, cfg, now, deadline):
    path = data / "ledger.db"
    markers = (data / "update_in_progress.marker", data / "restore_pending.json")
    if any(os.path.lexists(marker) for marker in markers):
        raise ValueError("marker_present")
    try:
        identity = _identity(path)
    except (OSError, ValueError):
        raise ValueError("database_identity") from None
    where, params = _scope_sql(cfg)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("observation_timeout")
    db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=min(0.2, remaining))
    try:
        db.execute("PRAGMA query_only=ON")
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute("BEGIN")
        invalid = "typeof(r.created_at) NOT IN ('integer','real') OR r.created_at<0 OR r.created_at>?"
        row = db.execute(
            "SELECT COUNT(*),COALESCE(SUM(r.state='sending'),0),COALESCE(SUM(r.state='unknown'),0),"
            "MIN(CASE WHEN r.state='queued' THEN r.created_at END),"
            f"COALESCE(SUM(CASE WHEN r.state='queued' AND ({invalid}) THEN 1 ELSE 0 END),0) "
            f"FROM notification_renders r WHERE {where} AND r.state IN ('queued','sending','unknown')",
            [now, *params]).fetchone()
        count, sending, unknown, oldest, invalid_queued = row
        if invalid_queued:
            raise ValueError("queue_unverified")
        queued = count - sending - unknown
        old_queued = bool(queued and oldest is not None and now - oldest > CARD_STALL_S)
        recent = ([_progress(db, source, where, params, now) for source in ("renders", "parts")]
                  if old_queued else [])
        valid_progress = [value for value, _ in recent if value is not None]
        last = max(valid_progress, default=None)
        stale_attempts, invalid_attempts = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN typeof(a.created_at) IN ('integer','real') "
            "AND a.created_at>=0 AND a.created_at<? THEN 1 ELSE 0 END),0),"
            "COALESCE(SUM(CASE WHEN typeof(a.created_at) NOT IN ('integer','real') "
            "OR a.created_at<0 OR a.created_at>? THEN 1 ELSE 0 END),0) "
            "FROM notification_delivery_attempts a JOIN notification_renders r ON r.delivery_id=a.delivery_id "
            f"WHERE {where} AND r.state IN ('sending','unknown') AND a.state IN ('granted','unknown')",
            [now - CARD_STALL_S, now, *params]).fetchone()
        missing_attempts = db.execute(
            "SELECT COUNT(*) FROM notification_renders r "
            f"WHERE {where} AND r.state IN ('sending','unknown') AND NOT EXISTS "
            "(SELECT 1 FROM notification_delivery_attempts a WHERE a.delivery_id=r.delivery_id "
            "AND a.state IN ('granted','unknown'))", params).fetchone()[0]
        if time.monotonic() >= deadline:
            raise TimeoutError("observation_timeout")
        if any(os.path.lexists(marker) for marker in markers):
            raise ValueError("marker_present")
        try:
            if _identity(path) != identity:
                raise ValueError("database_identity")
        except OSError:
            raise ValueError("database_identity") from None
        recent_progress = last is not None and now - last <= CARD_STALL_S
        issues = []
        if old_queued and not recent_progress and not any(x[1] for x in recent):
            issues.append("waiting_without_progress")
        if stale_attempts:
            issues.append("outcome_unconfirmed")
        if issues:
            state = issues[0]
        elif invalid_attempts or missing_attempts or unknown:
            state = "unknown"
        elif not queued and not sending:
            state = "idle"
        elif recent_progress:
            state = "progressing"
        elif old_queued:
            state = "unknown"
        else:
            state = "pending"
        return {"state": state, "reason": None, "issues": issues, "queued": queued, "sending": sending,
                "unknown": unknown, "oldest_created_at": oldest, "last_progress_at": last}
    finally:
        db.close()


def _delivery_error_reason(error):
    if isinstance(error, TimeoutError):
        return "observation_timeout"
    if isinstance(error, sqlite3.OperationalError) and "locked" in str(error).lower():
        return "database_busy"
    if isinstance(error, sqlite3.Error):
        text = str(error).lower()
        if text == "interrupted":
            return "observation_timeout"
        if "no such table" in text or "no such column" in text:
            return "schema_mismatch"
        return "database_busy" if "busy" in text else "schema_mismatch"
    if isinstance(error, ValueError) and error.args and error.args[0] in DELIVERY_REASONS:
        return error.args[0]
    return "observation_timeout"


def _observe(home, cfg, now, deadline):
    result = _empty(now)
    data = Path(home) / "data"
    result["restart_request"] = _restart(data, now)
    # The delivery scan must never starve the worker check: its budget ends
    # early enough for the worker pass to run on every observation.
    delivery_deadline = deadline - WORKER_RESERVE_S
    try:
        if time.monotonic() < delivery_deadline:
            result["delivery"] = _delivery(data, cfg, now, delivery_deadline)
        else:
            result["delivery"]["reason"] = "observation_timeout"
    except (OSError, ValueError, TypeError, sqlite3.Error, TimeoutError, RecursionError) as error:
        result["delivery"]["reason"] = _delivery_error_reason(error)
    result["worker"] = _worker(home, cfg, now, deadline)
    return result


def _valid_result(value, now):
    if type(value) is not dict or set(value) != {"observed_at", "worker", "delivery", "restart_request"} \
            or not _stamp(value["observed_at"], now) or value["observed_at"] != now:
        return False
    worker, delivery, restart = value["worker"], value["delivery"], value["restart_request"]
    if (type(worker) is not dict or set(worker) != {"state", "reason", "started_at"}
            or worker["state"] not in ("started", "disabled", "unknown")
            or worker["reason"] not in _WORKER_REASONS | {"observation_timeout"}
            or (worker["state"] == "started" and worker["reason"] != "worker_started")
            or (worker["state"] == "disabled" and worker["reason"] != "worker_start_failed")
            or (worker["state"] == "unknown" and worker["reason"] not in
                {"worker_start_unverified"} | _WORKER_DETAIL_REASONS)
            or (worker["state"] == "unknown") != (worker["started_at"] is None)
            or worker["started_at"] is not None and not (0 < worker["started_at"] and
                                                           _stamp(worker["started_at"], now))):
        return False
    if (type(delivery) is not dict or set(delivery) != {"state", "reason", "issues", "queued", "sending",
            "unknown", "oldest_created_at", "last_progress_at"} or delivery["state"] not in _DELIVERY_STATES
            or (delivery["reason"] is not None) != (delivery["state"] == "unknown"
                                                    and not delivery["issues"]
                                                    and delivery["queued"] is None)
            or delivery["reason"] is not None and delivery["reason"] not in DELIVERY_REASONS
            or any(delivery[key] is not None and (type(delivery[key]) is not int or delivery[key] < 0)
                   for key in ("queued", "sending", "unknown"))
            or any(delivery[key] is not None and not _stamp(delivery[key], now)
                   for key in ("oldest_created_at", "last_progress_at"))):
        return False
    state, queued, sending, unknown = (delivery[key] for key in ("state", "queued", "sending", "unknown"))
    issues = delivery["issues"]
    if (type(issues) is not list or issues not in ([], [_DELIVERY_ISSUES[0]], [_DELIVERY_ISSUES[1]],
                                                   list(_DELIVERY_ISSUES))
            or bool(issues) != (state in _DELIVERY_ISSUES) or issues and state != issues[0]):
        return False
    if state != "unknown" and any(value is None for value in (queued, sending, unknown)):
        return False
    if state == "idle" and (queued or sending or unknown):
        return False
    if "waiting_without_progress" in issues and (not queued or delivery["oldest_created_at"] is None
            or now - delivery["oldest_created_at"] <= CARD_STALL_S
            or delivery["last_progress_at"] is not None
            and now - delivery["last_progress_at"] <= CARD_STALL_S):
        return False
    if "outcome_unconfirmed" in issues and not (sending or unknown):
        return False
    if state == "progressing" and delivery["last_progress_at"] is None:
        return False
    if state == "pending" and (unknown or not (queued or sending)
            or queued and (delivery["oldest_created_at"] is None
                           or now - delivery["oldest_created_at"] > CARD_STALL_S)
            or delivery["last_progress_at"] is not None
            and now - delivery["last_progress_at"] <= CARD_STALL_S):
        return False
    return (type(restart) is dict and set(restart) == {"state", "at"}
            and restart["state"] in _RESTART_STATES | {"unverified"}
            and (restart["state"] == "unverified") == (restart["at"] is None)
            and (restart["at"] is None or _stamp(restart["at"], now)))


def observe(home, cfg, now):
    """Return a bounded, non-identifying observation of the configured delivery scope."""
    if not _stamp(now, now):
        raise ValueError("observation_time_invalid")
    deadline = time.monotonic() + OBSERVATION_S
    result = _empty(now)
    try:
        settings = _settings(cfg)
        if settings is None:
            return result
        payload = json.dumps({"home": os.fspath(home), "cfg": settings, "now": now,
                              "deadline": deadline - _CLEANUP_S}, allow_nan=False)
        if len(payload.encode("utf-8")) > MAX_INPUT_BYTES:
            return result
        environment = {key: os.environ[key] for key in ("HOME", "TMPDIR", "XDG_CONFIG_HOME",
                       "XDG_CACHE_HOME", "XDG_DATA_HOME", "TZ") if key in os.environ}
        environment.update(PATH=os.environ.get("PATH", os.defpath), LC_ALL="C")
        if os.environ.get("MCS_TEST_SANDBOX") == "1":
            environment["MCS_TEST_SANDBOX"] = "1"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return result
        child = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child"],
                               input=payload, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               cwd="/", env=environment, timeout=remaining)
        if child.returncode != 0 or time.monotonic() >= deadline \
                or len(child.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
            return result
        value = strict_json(child.stdout)
        return value if _valid_result(value, now) else result
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired, RecursionError, UnicodeError):
        return result


if __name__ == "__main__" and sys.argv[1:] == ["--child"]:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("observation_input_invalid")
        args = strict_json(raw)
        cfg = _settings(args["cfg"])
        deadline = args["deadline"]
        if (cfg is None or not _stamp(args["now"], args["now"])
                or type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline < 0 or deadline > time.monotonic() + OBSERVATION_S):
            raise ValueError("observation_input_invalid")
        print(json.dumps(_observe(args["home"], cfg, args["now"], deadline),
                         allow_nan=False, separators=(",", ":")))
    except (OSError, ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        sys.exit(1)
