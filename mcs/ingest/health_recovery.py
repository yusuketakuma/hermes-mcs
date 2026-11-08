"""Bounded auto-recovery driven by the health watcher's fresh verdict.

The watcher already knows *why* the system is unhealthy; this module turns
a small fixed set of those causes into bounded service actions — never
into data writes, notification sends, or approval decisions. Every action
is journaled to data/auto_recovery.json and capped per target per window;
an exhausted target falls back to the ordinary alert path. Anything the
probes cannot prove stays untouched (fail-closed).
"""
from __future__ import annotations

import json
import math
import os
import sys

import safe_io

STATE_REL = os.path.join("data", "auto_recovery.json")
MANIFEST_REL = os.path.join("data", "service_manifest.json")
INSTALL_PROGRESS_REL = os.path.join("data", "install_progress.json")
MAX_STATE_BYTES = 64 * 1024
HISTORY_MAX = 50
LAUNCHCTL_TIMEOUT_S = 10
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_WINDOW_S = 24 * 3600
# a 'running' journal younger than this may be a live install; older than
# this with a dead recorded pid is treated as an interrupted install
INTERRUPTED_AFTER_S = 30 * 60

# host reasons that justify a restart request; update_stuck stays with
# org.mcs.recovery (the update domain owns interrupted-update recovery)
HOST_RESTART_REASONS = frozenset({
    "standalone_host_down", "standalone_check_stalled",
    "standalone_child_failing", "standalone_connector_down"})


def _options(cfg: dict) -> tuple[bool, int, int] | None:
    """(enabled, max_attempts, window_s); None when the section is unusable."""
    section = cfg.get("auto_recovery") if isinstance(cfg, dict) else None
    if section is None:
        return True, DEFAULT_MAX_ATTEMPTS, DEFAULT_WINDOW_S
    if not isinstance(section, dict):
        return None
    enabled = section.get("enabled", True)
    attempts = section.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    window = section.get("window_s", DEFAULT_WINDOW_S)
    if type(enabled) is not bool \
            or type(attempts) is not int or not 1 <= attempts <= 10 \
            or type(window) is not int or not 3600 <= window <= 7 * 86400:
        return None
    return enabled, attempts, window


def _load_state(home: str) -> dict:
    try:
        value = safe_io.read_json_nofollow(
            os.path.join(home, STATE_REL), MAX_STATE_BYTES)
    except (OSError, ValueError, RecursionError):
        return {"targets": {}, "history": []}
    if not isinstance(value, dict) \
            or not isinstance(value.get("targets"), dict) \
            or not isinstance(value.get("history"), list):
        return {"targets": {}, "history": []}
    return value


def _save_state(home: str, state: dict) -> None:
    safe_io.atomic_publish_text(
        os.path.join(home, STATE_REL),
        json.dumps(state, ensure_ascii=False))


def _manifest_loaded(home: str, label: str) -> bool:
    """True only when the service manifest recorded this agent loaded —
    evidence it should be running, never proof it may be started now."""
    try:
        value = safe_io.read_json_nofollow(
            os.path.join(home, MANIFEST_REL), MAX_STATE_BYTES)
    except (OSError, ValueError, RecursionError):
        return False
    agents = value.get("agents") if isinstance(value, dict) else None
    return isinstance(agents, list) and any(
        isinstance(a, dict) and a.get("label") == label and a.get("loaded") is True
        for a in agents)


def _default_launch(argv: list):
    # pytest sets MCS_AUTO_RECOVERY=off (conftest): tests drive main() with
    # synthetic homes and must never reach the real launchctl
    if os.environ.get("MCS_AUTO_RECOVERY") == "off":
        raise RuntimeError("auto_recovery_disabled")
    from lifecycle_io import run_bounded
    return run_bounded(argv, timeout=LAUNCHCTL_TIMEOUT_S)


def _agent_state(label: str, launch) -> str | None:
    """'loaded' | 'absent' | None (probe failed — never proof of a stop)."""
    try:
        result = launch(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
    except Exception:
        return None
    code = getattr(result, "returncode", None)
    if code == 0:
        return "loaded"
    if code == 113:
        return "absent"
    return None


def _do(label: str, verb: str, plist: str | None, launch) -> str:
    """One bounded launchctl verb; returns a fixed result word."""
    if verb == "kickstart":
        argv = ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"]
    elif verb == "bootstrap":
        if not plist:
            return "skipped"
        argv = ["launchctl", "bootstrap", f"gui/{os.getuid()}", plist]
    else:
        return "skipped"
    try:
        result = launch(argv)
    except Exception:
        return "failed"
    return "done" if getattr(result, "returncode", None) == 0 else "failed"


def _budget(state: dict, target: str, now: float,
            max_attempts: int, window_s: int) -> bool:
    row = state["targets"].get(target)
    if not isinstance(row, dict):
        return True
    started = row.get("window_start")
    if type(started) not in (int, float) or now - started > window_s:
        return True
    return type(row.get("attempts")) is int and row["attempts"] < max_attempts


def _record(state: dict, target: str, action: str, result: str,
            reasons: list, now: float, window_s: int) -> None:
    row = state["targets"].get(target)
    if not isinstance(row, dict) or type(row.get("window_start")) not in (int, float) \
            or now - row["window_start"] > window_s:
        row = {"window_start": now, "attempts": 0}
        state["targets"][target] = row
    prior = row.get("attempts")
    row["attempts"] = (prior if type(prior) is int else 0) + 1
    row["last_at"] = now
    row["last_action"] = action
    row["last_result"] = result
    state["history"] = (state.get("history") or [])[-(HISTORY_MAX - 1):] + [
        {"at": now, "target": target, "action": action,
         "result": result, "reasons": list(reasons)}]
    state["targets"] = dict(list(state["targets"].items())[-64:])


def _pid_dead(pid: int) -> bool:
    """True only when the pid provably does not exist."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OverflowError, OSError):
        return False                   # alive, permission or invalid: unproven
    return False


def _interrupted_install(home: str, now: float) -> bool:
    """True only when the install journal proves an interrupted run:
    status 'running', stale update, and the recorded pid no longer exists.
    Anything less certain (fresh journal, live/unparseable pid, unreadable
    or malformed file) is not proof — bootstrap stays off (fail-closed)."""
    try:
        value = safe_io.read_json_nofollow(
            os.path.join(home, INSTALL_PROGRESS_REL), MAX_STATE_BYTES)
    except (OSError, ValueError, RecursionError):
        return False
    if not isinstance(value, dict) or value.get("status") != "running":
        return False
    updated = value.get("updated_at")
    if isinstance(updated, bool) or not isinstance(updated, (int, float)) \
            or (isinstance(updated, float) and not math.isfinite(updated)):
        return False
    if now - updated <= INTERRUPTED_AFTER_S:
        return False
    run_id = value.get("run_id")
    if not isinstance(run_id, str):
        return False
    try:
        pid = int(run_id.split("-", 1)[0])
    except (ValueError, IndexError):
        return False
    return pid > 0 and _pid_dead(pid)


def _agents_dir() -> str:
    return os.path.expanduser("~/Library/LaunchAgents")


def _agent_pass(cfg: dict, home: str, state: dict, now: float, launch,
                max_attempts: int, window_s: int,
                resume_install: bool) -> list[dict]:
    """Complete an interrupted install's service layer: bootstrap owned
    agents the manifest proves were running but launchd no longer has.
    Only runs on proven install interruption — a manually booted-out
    agent is operator intent, never auto-reversed. Never labels outside
    desired_agent_labels, never on a failed or timed-out probe."""
    if not resume_install:
        return []
    import service_contract as contract
    try:
        labels = contract.desired_agent_labels(cfg)
    except (ValueError, TypeError):
        return []
    actions = []
    for label in labels:
        if label == contract.STANDALONE_LABEL:
            continue                     # the host path owns this label
        if not _budget(state, label, now, max_attempts, window_s):
            continue
        if _agent_state(label, launch) != "absent":
            continue
        if not _manifest_loaded(home, label):
            continue
        plist = os.path.join(_agents_dir(), label + ".plist")
        if not os.path.isfile(plist):
            continue
        result = _do(label, "bootstrap", plist, launch)
        _record(state, label, "bootstrap", result, ["agent_absent"],
                now, window_s)
        actions.append({"target": label, "action": "bootstrap",
                        "result": result, "reasons": ["agent_absent"]})
    return actions


def _host_pass(report: dict, home: str, state: dict, now: float,
               launch, restart, max_attempts: int, window_s: int,
               resume_install: bool) -> list[dict]:
    """Restart the standalone host on host-level evidence: a graceful drain
    request first, one hard kickstart when the graceful attempt did not
    clear the cause, bootstrap only with manifest+plist proof."""
    import service_contract as contract
    reasons = sorted(HOST_RESTART_REASONS & set(report.get("state_reasons") or []))
    if not reasons:
        return []
    label = contract.STANDALONE_LABEL
    if not _budget(state, label, now, max_attempts, window_s):
        return []
    last = state["targets"].get(label) or {}
    if sys.platform == "darwin":
        loaded = _agent_state(label, launch)
        if loaded == "absent":
            plist = os.path.join(_agents_dir(), label + ".plist")
            # bootstrap resurrects a service launchd no longer has — only
            # a proven interrupted install justifies reversing that state
            if not (resume_install and _manifest_loaded(home, label)
                    and os.path.isfile(plist)):
                return []
            result = _do(label, "bootstrap", plist, launch)
            _record(state, label, "bootstrap", result, reasons, now,
                    window_s)
            return [{"target": label, "action": "bootstrap",
                     "result": result, "reasons": reasons}]
        if loaded is None:
            return []                        # probe unknown: fail closed
    # loaded (or a non-darwin host where only the file request applies):
    # a graceful drain request first, one hard kickstart when the graceful
    # attempt did not clear the cause
    if last.get("last_action") == "restart_request":
        if sys.platform != "darwin":
            return []                    # no hard-restart verb on this platform
        result = _do(label, "kickstart", None, launch)
        _record(state, label, "kickstart", result, reasons, now,
                window_s)
        return [{"target": label, "action": "kickstart",
                 "result": result, "reasons": reasons}]
    try:
        rid = restart(home)
    except Exception:
        rid = None
    if rid is not None:
        _record(state, label, "restart_request", "requested", reasons, now,
                window_s)
        return [{"target": label, "action": "restart_request",
                 "result": "requested", "reasons": reasons}]
    if sys.platform != "darwin":
        return []                        # graceful request impossible, no verb left
    result = _do(label, "kickstart", None, launch)
    _record(state, label, "kickstart", result, reasons, now,
            window_s)
    return [{"target": label, "action": "kickstart",
             "result": result, "reasons": reasons}]


def run(report: dict, home: str, cfg: dict, now: float, *,
        launch=None, restart=None) -> dict:
    """One bounded recovery pass after a health evaluation.

    Returns a small machine-readable summary for the watch report; every
    attempted action is journaled. Probes that cannot prove a state never
    produce an action.
    """
    options = _options(cfg)
    if options is None:
        return {"enabled": False, "actions": [], "reason": "config_invalid"}
    enabled, max_attempts, window_s = options
    if not enabled:
        return {"enabled": False, "actions": []}
    launch = launch or _default_launch
    if restart is None:
        try:
            from mcs_standalone.service import request_restart
            restart = request_restart
        except Exception:
            def restart(_root):
                return None
    state = _load_state(home)
    resume_install = _interrupted_install(home, now)
    actions = []
    if report.get("runtime_mode") == "standalone":
        actions += _host_pass(report, home, state, now, launch, restart,
                              max_attempts, window_s, resume_install)
    if sys.platform == "darwin":
        actions += _agent_pass(cfg, home, state, now, launch,
                               max_attempts, window_s, resume_install)
    if actions:
        try:
            _save_state(home, state)      # journal only real attempts
        except OSError:
            pass                        # journal loss never blocks the watch
    exhausted = sorted(t for t, row in state["targets"].items()
                       if isinstance(row, dict)
                       and type(row.get("attempts")) is int
                       and row["attempts"] >= max_attempts
                       and type(row.get("window_start")) in (int, float)
                       and now - row["window_start"] <= window_s)
    return {"enabled": True, "actions": actions, "exhausted": exhausted}
