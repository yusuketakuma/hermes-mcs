#!/usr/bin/env python3
"""MCS health watcher — independent supervised reader of health.json.

run_check exits 0 even for a partial run by design, and a dead producer
leaves the last health.json behind. This watcher never trusts process
exit codes: it re-derives service status from the file itself —
presence, parseability, freshness — so 'missing', 'stale' and 'corrupt'
are detectable even when every run "succeeded".

Freshness deadline comes from config (health.tick_interval_s, default
600 — the deployed 10-minute tick — and health.max_missed_runs, default
2). Collection runs at the same interval around the clock.

Output contract: alert content is status codes/counters only — never
patient data. stdout carries one alert line on alert transitions
(watchdog convention) and the same lines go to notify_system_target
(else notify_target); data/health_watch_status.json is the durable
machine-readable status, published atomically.
"""
import argparse
from datetime import datetime
import fcntl
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
import safe_io  # noqa: E402

from mcs_queries import JST
from health_policy import HEALTH_REL, STATUS_MAX_BYTES, RUN_GRACE_S, freshness_deadline
from mcs_util import HOME, load_config

STATE_REL = os.path.join("data", "health_watch.json")
STATUS_REL = os.path.join("data", "health_watch_status.json")

REALERT_S = 4 * 3600        # unchanged bad state re-alerts every 4h
# Reasons that usually clear on their own: a degraded verdict made only of
# these alerts once one of them was seen in STREAK_ALERT consecutive fresh
# health observations (distinct health_at); every other reason alerts at once.
TRANSIENT_REASONS = frozenset({"stage_errors", "run_deadline_exceeded", "collection_incomplete",
                               "notification_deferred", "notification_parked",
                               "notification_pending"})
STREAK_ALERT = 3
DELIVERY_RETRY_S = 60       # only provably unsent alerts may retry
UNKNOWN_HISTORY = 50        # retained uncertain-delivery records

OVERALL_STATUS = {"ok": "ok", "degraded": "degraded",
                  "failed": "failed"}

# runtime_mode=standalone: the host's own status file (mcs_standalone)
STANDALONE_STATUS_REL = os.path.join("data", "standalone-status.json")
STANDALONE_HOST_STALE_S = 180      # heartbeat every 5 s; restart throttle 30 s
STANDALONE_CHILD_FAILURES = 3      # consecutive failed exits of one job
STANDALONE_CONNECTOR_DOWN_S = 600
STANDALONE_UPDATE_STUCK_S = 2 * 3600
# jobs whose non-zero exit is a real failure (calendar helpers such as
# the daily LLM restart may exit non-zero by design on some hosts)
STANDALONE_WATCHED_JOBS = ("extract-0", "extract-2", "lineworks", "cmd",
                           "cmd_int", "mcs_check", "mcs_deep")


from stats_math import finite_number  # noqa: E402 - one shared rule






def _is_code(value) -> bool:
    """Producer reason code (run_check emits lowercase snake_case only)."""
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,64}", value) is not None


def _wait_code(value) -> bool:
    """A pending-wait reason: a producer code, optionally `kind:code`."""
    return isinstance(value, str) and re.fullmatch(
        r"[a-z0-9_]{1,64}(:[a-z0-9_]{1,64})?", value) is not None


_BACKUP_LOCAL_STATES = frozenset({"missing", "unreadable", "unverified", "verified", "unknown"})


def _count(value) -> bool:
    """A non-negative integer count (a float is not rounded into one)."""
    return type(value) is int and value >= 0


def _optional_blocks(h: dict) -> dict:
    """The additive observation blocks, each None unless well-formed — an
    absent or malformed block never changes the verdict."""
    notify = h.get("notify") if isinstance(h.get("notify"), dict) else {}
    waits = notify.get("pending_by_reason")
    wait = None
    if (isinstance(waits, dict) and isinstance(waits.get("by_reason"), dict)
            and _count(waits.get("total")) and _count(waits.get("attention"))
            and waits["attention"] <= waits["total"]):
        counts = {}
        for code, slot in waits["by_reason"].items():
            if not (_wait_code(code) and isinstance(slot, dict)
                    and _count(slot.get("count"))):
                counts = None
                break
            counts[code] = slot["count"]
        # the reasons partition the population: anything else is not read
        if counts is not None and sum(counts.values()) == waits["total"]:
            wait = {"attention": waits["attention"], "reasons": counts}
    backup = h.get("backup") if isinstance(h.get("backup"), dict) else {}
    local = backup.get("local")
    backup_local = None
    if (isinstance(local, dict) and isinstance(local.get("state"), str)
            and local["state"] in _BACKUP_LOCAL_STATES):
        age = local.get("newest_age_s")
        backup_local = {"state": local["state"],
                        "newest_age_s": age if finite_number(age) and age >= 0 else None}
    run = h.get("run") if isinstance(h.get("run"), dict) else {}
    last = run.get("last_interrupt")
    interrupt = None
    if (isinstance(last, dict) and _is_code(last.get("reason"))
            and finite_number(last.get("detected_at"))):
        interrupt = {"reason": last["reason"], "detected_at": last["detected_at"]}
    return {"notify_wait": wait, "backup_local": backup_local,
            "last_interrupt": interrupt}




def classify_health(path: str, now: float, deadline_s: int) -> dict:
    """File evidence -> status. Staleness is checked BEFORE the recorded
    payload: a dead producer leaves a fresh-looking 'ok' forever."""
    try:
        h = safe_io.read_json_nofollow(path, STATUS_MAX_BYTES)
    except FileNotFoundError:
        return {"status": "corrupt" if os.path.lexists(path) else "missing"}
    except (OSError, ValueError, RecursionError):
        return {"status": "corrupt"}
    if not isinstance(h, dict) or not finite_number(h.get("at")):
        return {"status": "corrupt"}
    overall = h.get("overall")
    if not isinstance(overall, str) or overall not in OVERALL_STATUS:
        return {"status": "corrupt"}
    if h["at"] > now + RUN_GRACE_S:
        return {"status": "corrupt"}
    # unread collection time, when recorded — --jobs-only deep runs
    # refresh 'at' without collecting unread and must not mask a
    # stopped unread check (older files lack the field: use 'at')
    unread_at = h.get("unread_at")
    # tie keeps unread_at (min returns its first minimal argument)
    binding = (min(unread_at, h["at"]) if finite_number(unread_at)
               else h["at"])
    age = now - binding
    report = {"health_at": h["at"], "age_s": round(max(age, 0), 1),
              "overall": overall, "run_status": h.get("run_status"),
              "run_id": h.get("run_id"), "deadline_s": deadline_s,
              "disk_low": h.get("disk_low") is True,
              "disk_free_mb": h.get("disk_free_mb")
              if finite_number(h.get("disk_free_mb")) else None}
    report["status"] = ("stale" if age > deadline_s
                        else OVERALL_STATUS[overall])
    unread_unknown = "unread_at" in h and not finite_number(unread_at)
    if report["status"] == "ok" and unread_unknown:
        report["status"] = "degraded"
    run = h.get("run")
    if isinstance(run, dict):
        overshoot = run.get("overshoot_s")
        report["run"] = run
        if (report["status"] == "ok" and isinstance(overshoot, (int, float))
                and finite_number(overshoot)
                and overshoot > 0):
            report["status"] = "degraded"
    # explain non-ok from the producer's own codes/counters (never text):
    # absent or malformed fields stay unknown (None), never "no reason"
    reasons = h.get("state_reasons")
    reasons = (reasons if isinstance(reasons, list)
               and all(_is_code(r) for r in reasons) else None)
    if unread_unknown and report["status"] == "degraded":
        reasons = list(reasons or [])
        if "unread_collection_unknown" not in reasons:
            reasons.append("unread_collection_unknown")
    if report["status"] == "stale":
        # the stale file's codes describe its own run, not the current
        # staleness: keep them labelled as recorded, the cause unknown
        report["recorded_state_reasons"] = reasons
        reasons = None
    report["state_reasons"] = reasons
    from collection_diagnostics import validated
    report["collection_diagnostics"] = validated(h.get("collection_diagnostics"), h["at"], fine=True)
    backup = h.get("backup")
    if (isinstance(backup, dict) and backup.get("state") in ("degraded", "failed")
            and report["status"] in ("degraded", "failed")):
        cause = backup.get("failure_code")
        backup_reasons = backup.get("reasons")
        if (isinstance(cause, str) and cause in _BACKUP_FAILURE_JA
                and isinstance(backup_reasons, list) and "backup_failed" in backup_reasons
                and reasons is not None and "backup_not_verified" in reasons):
            report["backup_failure_code"] = cause
    last_ok = h.get("last_ok_at")
    report["last_ok_at"] = (last_ok if finite_number(last_ok)
                            and last_ok >= 0 else None)
    notify = h.get("notify") if isinstance(h.get("notify"), dict) else {}
    pending = notify.get("pending")
    report["notify_pending"] = (pending if finite_number(pending)
                                and pending >= 0 else None)
    held = notify.get("held_reasons")
    report["held_reasons"] = (
        held if isinstance(held, dict)
        and all(_is_code(k) and finite_number(v) and v >= 0
                for k, v in held.items())
        else None)
    # every queue key always present: missing block == malformed == None
    report["oldest_age_s"] = {
        q: (block.get("oldest_age_s")
            if isinstance(block, dict)
            and finite_number(block.get("oldest_age_s")) else None)
        for q, block in (("notify", h.get("notify")),
                         ("semantic_jobs", h.get("semantic_jobs")),
                         ("extract_qc_jobs", h.get("extract_qc_jobs")))}
    report.update(_optional_blocks(h))
    # a stale file's observations describe its own run, not the present
    report["observations_recorded"] = report["status"] == "stale"
    # dedup stamp: unread_at only when that age is what made it stale
    report["evidence_at"] = (binding if report["status"] == "stale"
                             else h["at"])
    return report


def _pid_alive(pid) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:          # gone, or another user's reused pid
        return False


def _read_standalone_status(home: str):
    try:
        value = safe_io.read_json_nofollow(os.path.join(home, STANDALONE_STATUS_REL), STATUS_MAX_BYTES)
    except (OSError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def standalone_host_reasons(home: str, now: float, cfg: dict):
    """runtime_mode=standalone: (severity, reason codes, details) from the
    host's own standalone-status.json. The host runs every job — this
    watcher included when it is not installed separately — so its death,
    crash loops, a lost connection or a wedged update are checked here,
    from outside it. Details are job codes and numbers only."""
    status = _read_standalone_status(home)
    stamp = status.get("updated_at") if status else None
    if status is None or not finite_number(stamp) or not _pid_alive(status.get("pid")) \
            or now - stamp > STANDALONE_HOST_STALE_S:
        age = now - stamp if finite_number(stamp) else None
        return "failed", ["standalone_host_down"], (
            {"standalone_host_down": f"最終更新 {_hours(age)}前"} if age and age > 0 else {})
    reasons, details = [], {}
    exits = status.get("exits") if isinstance(status.get("exits"), dict) else {}
    failing = []
    for job in STANDALONE_WATCHED_JOBS:
        row = exits.get(job)
        count = row.get("failures") if isinstance(row, dict) else None
        if finite_number(count) and count >= STANDALONE_CHILD_FAILURES:
            failing.append(f"{job} {int(count)}回連続")
    if failing:
        reasons.append("standalone_child_failing")
        details["standalone_child_failing"] = "、".join(failing)
    connector = status.get("connector") if isinstance(status.get("connector"), dict) else {}
    since = connector.get("failing_since")
    if connector.get("state") == "waiting" and finite_number(since) \
            and now - since >= STANDALONE_CONNECTOR_DOWN_S:
        reasons.append("standalone_connector_down")
        error = connector.get("last_error")
        details["standalone_connector_down"] = f"{_hours(now - since)}" + (
            f" {error}" if _is_code(error) else "")
    marker = status.get("update_marker") if isinstance(status.get("update_marker"), dict) else {}
    marked = marker.get("since")
    if status.get("update_in_progress") is True and finite_number(marked) \
            and now - marked >= STANDALONE_UPDATE_STUCK_S:
        reasons.append("standalone_update_stuck")
        state = marker.get("state")
        details["standalone_update_stuck"] = f"{_hours(now - marked)}" + (
            f" {state}" if _is_code(state) else "")
    check = exits.get("mcs_check") if isinstance(exits.get("mcs_check"), dict) else {}
    last_ok = check.get("last_ok_at")
    reference = last_ok if finite_number(last_ok) else status.get("started_at")
    if finite_number(reference) and now - reference > freshness_deadline(cfg):
        reasons.append("standalone_check_stalled")
        details["standalone_check_stalled"] = (f"最終成功 {_hours(now - last_ok)}前"
                                               if finite_number(last_ok) else "起動後の成功なし")
    return ("degraded" if reasons else "ok"), reasons, details


def _apply_standalone(obs: dict, home: str, now: float, cfg: dict) -> None:
    """Merge the host's own evidence into the health.json verdict: a
    stale/missing file gets its current cause, a fresh one its extra
    reasons; the status only ever becomes more severe."""
    obs["runtime_mode"] = "standalone"
    severity, reasons, details = standalone_host_reasons(home, now, cfg)
    if not reasons:
        return
    obs["host_details"] = details
    rank = {"ok": 0, "degraded": 1, "failed": 2}
    if obs["status"] in rank:
        if rank[severity] > rank[obs["status"]]:
            obs["status"] = severity
        base = list(obs.get("state_reasons") or [])
        obs["state_reasons"] = base + [r for r in reasons if r not in base]
    else:
        # stale/missing/corrupt: the file's own codes are not current;
        # the host evidence is, so it names the cause
        obs["state_reasons"] = list(reasons)


def _state_unreadable(path: str) -> bool:
    """A present state file that is no regular JSON object.

    load_config reads it as {} exactly like an absent file; only absence (or a
    readable object) may start a streak, so this never holds an alert back."""
    try:
        raw = safe_io.read_regular_nofollow(path, 1024 * 1024, OSError)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        return len(raw) > 1024 * 1024 or not isinstance(json.loads(raw), dict)
    except (ValueError, RecursionError):
        return True


def _errors_transient(obs: dict) -> bool:
    """stage_errors may wait only when every recorded error category is transient."""
    from collection_diagnostics import CATEGORIES
    errors = (obs.get("collection_diagnostics") or {}).get("errors") or {}
    counts = errors.get("categories")
    return (errors.get("state") == "known" and isinstance(counts, dict) and any(counts.values())
            and all(CATEGORIES[k][2] for k, n in counts.items() if n))


def _streak(state: dict, obs: dict) -> tuple[dict | None, bool]:
    """(streak block to persist, whether it can be trusted for holding an alert back)."""
    prior = state.get("streak")
    valid = "streak" not in state or (
        isinstance(prior, dict) and finite_number(prior.get("health_at"))
        and isinstance(prior.get("count"), dict) and isinstance(prior.get("first_at"), dict)
        and all(r in TRANSIENT_REASONS and type(n) is int and n >= 1
                and finite_number(prior["first_at"].get(r)) for r, n in prior["count"].items()))
    at = obs.get("health_at")
    if obs["status"] not in OVERALL_STATUS or not finite_number(at):
        return (prior if valid else None), valid      # not a fresh observation: nothing to count
    reasons = {r.split(":")[0] for r in obs.get("state_reasons") or []} & TRANSIENT_REASONS
    if valid and prior and at == prior["health_at"]:
        return prior, True                            # the same observation read again
    newer = bool(valid and prior and at > prior["health_at"])
    count = {r: (prior["count"].get(r, 0) + 1 if newer else 1) for r in reasons}
    first = {r: (prior["first_at"].get(r, at) if newer and r in prior["count"] else at)
             for r in reasons}
    return {"health_at": at, "count": count, "first_at": first}, valid


def _dedup_stamp(record: dict):
    """Verdict timestamp. Pre-evidence_at files key on health_at."""
    if "evidence_at" in record:
        return record.get("evidence_at")
    return record.get("health_at")


def evaluate(home: str = HOME, now: float | None = None,
             cfg: dict | None = None) -> dict:
    """Classify current evidence, apply alert dedup, persist state.

    Dedup key is (status, evidence_at) for a stale verdict —
    evidence_at is the unread-collection age or health_at, whichever
    made it stale — and the status alone otherwise: a fresh file
    carries a new health_at every tick, so keying on it would re-alert
    a persistent 'degraded' every 10 minutes. An unchanged verdict
    never re-alerts. ok->ok never alerts even when the file is fresh —
    a healthy producer keeping cadence is not an event. Alerts fire
    on: first non-ok observation, every transition INTO a non-ok
    status, and an unchanged non-ok state
    re-alerted after REALERT_S, plus a fresh non-ok verdict that gains
    a state reason not yet alerted in the current episode. 'ok' is only produced by a fresh
    in-deadline file — recovery can never be assumed. State files
    that predate evidence_at are read via health_at.

    Healthy observations and recovery are internal observations only.
    disk_low has its own dedup: disk_alert fires only when a fresh
    file turns low; stale/missing/corrupt evidence keeps
    the last known value."""
    now = time.time() if now is None else now
    if not finite_number(now):
        raise ValueError("health_now_invalid")
    cfg = load_config() if cfg is None else cfg
    deadline = freshness_deadline(cfg)
    health_path = os.path.join(home, HEALTH_REL)
    state_path = os.path.join(home, STATE_REL)
    status_path = os.path.join(home, STATUS_REL)

    obs = classify_health(health_path, now, deadline)
    notify = cfg.get("notify") if isinstance(cfg, dict) else None
    if isinstance(notify, dict) and notify.get("interactive") == "lineworks":
        obs["notify_transport"] = "lineworks"
    if isinstance(cfg, dict) and cfg.get("runtime_mode") == "standalone":
        _apply_standalone(obs, home, now, cfg)
    state = load_config(state_path)
    last = state.get("last")
    last = last if isinstance(last, dict) else {}
    key = (obs["status"],
           _dedup_stamp(obs) if obs["status"] == "stale" else None)
    last_key = (last.get("status"),
                _dedup_stamp(last) if last.get("status") == "stale"
                else None)
    prior_known = bool(last)
    transition = key != last_key
    # a fresh non-ok verdict whose producer codes gain a reason not yet
    # alerted in this episode is news (e.g. degraded by notifications,
    # then also by collection) — a reason that drops and returns stays
    # silent until the episode ends, so flapping codes cannot spam
    reasons = obs.get("state_reasons") or []
    seen = state.get("alerted_reasons")
    if transition:
        seen = set()
    elif seen is None:
        seen = set(reasons)     # state from before this field: no news
    else:
        seen = (set(seen) if isinstance(seen, list)
                and all(isinstance(r, str) for r in seen) else set())
    # a stale file's own codes are not current, but the standalone
    # host's evidence merged into it is (a host that died mid-episode)
    gained = set(reasons) - seen
    new_reason = bool(prior_known and not transition and gained
                  and obs["status"] != "ok"
                  and (obs["status"] != "stale"
                       or any(r.startswith("standalone_") for r in gained)))
    alerted_at = state.get("detected_at", state.get("alerted_at"))
    invalid_alert_at = not finite_number(alerted_at) \
        or alerted_at > now
    realert = (obs["status"] != "ok"
               and (invalid_alert_at
                    or now - alerted_at >= REALERT_S))
    # Flapping back into the same degraded episode (degraded -> ok ->
    # degraded with no new reason) inside REALERT_S is not news; severe
    # states (failed/stale/missing/corrupt) always alert on entry.
    episode = state.get("alerted_episode")
    episode = episode if isinstance(episode, dict) else {}
    episode_reasons = episode.get("reasons")
    episode_reasons = (set(episode_reasons) if isinstance(episode_reasons, list)
                       and all(isinstance(r, str) for r in episode_reasons) else None)
    reflap = (transition and obs["status"] == "degraded"
              and episode.get("status") == "degraded" and not realert
              and episode_reasons is not None and set(reasons) <= episode_reasons)
    if reflap:
        seen = set(episode_reasons)
    alert = obs["status"] != "ok" and (
        (transition and not reflap) or realert or new_reason)
    streak, trusted = _streak(state, obs)
    trusted = trusted and not _state_unreadable(state_path)
    bases = {r.split(":")[0] for r in reasons}
    transient_only = (obs["status"] == "degraded" and bool(bases)
                      and bases <= TRANSIENT_REASONS
                      and ("stage_errors" not in bases or _errors_transient(obs)))
    ready = bool(streak) and any(streak["count"].get(r, 0) >= STREAK_ALERT for r in bases)
    held = False
    if transient_only and trusted:
        if not ready:
            held, alert = alert or state.get("held_episode") is True, False
        elif state.get("held_episode") is True:
            alert = True                    # the held episode reaches the threshold
    state["held_episode"] = held
    if streak is not None:
        state["streak"] = streak
    else:
        state.pop("streak", None)
    disk_prev = state.get("disk_low") is True
    disk_low = (obs["disk_low"] if obs["status"] in OVERALL_STATUS
                else disk_prev)
    disk_alert = disk_low and not disk_prev
    state["disk_low"] = disk_low

    report = dict(obs)
    report.update({"deadline_s": obs.get("deadline_s", deadline), "alert": alert,
                   "disk_low": disk_low, "disk_alert": disk_alert,
                   "watched_at": now, "alert_held": held})
    if transient_only and streak and streak["count"]:
        top = max(bases, key=lambda r: streak["count"].get(r, 0))
        report["streak"] = {"count": streak["count"].get(top, 0),
                            "first_at": streak["first_at"].get(top)}
    state["last"] = {"status": obs["status"],
                     "health_at": obs.get("health_at"),
                     "evidence_at": _dedup_stamp(obs)}
    if alert:
        state["detected_at"] = now
        state["alerted_episode"] = {"status": obs["status"],
                                    "reasons": sorted(set(reasons) | (
                                        episode_reasons or set()
                                        if episode.get("status") == obs["status"]
                                        else set()))}
    state["alerted_reasons"] = sorted(seen | set(reasons) if alert
                                      else seen)
    delivery = _reconcile_delivery(state, obs["status"], key, alert,
                                   disk_low, disk_alert, transition,
                                   new_reason or realert, now)
    if delivery:
        state["delivery"] = delivery
        report["delivery"] = dict(delivery)
    try:
        safe_io.atomic_publish_text(
            state_path, json.dumps(state, ensure_ascii=False))
    except OSError:
        report["delivery_error"] = "state_persist_failed"
        if held:        # an unsaved streak cannot grow: never hold the alert back silently
            report["alert"], report["alert_held"] = True, False
    try:
        safe_io.atomic_publish_text(
            status_path, json.dumps(report, ensure_ascii=False))
    except OSError:
        report["delivery_error"] = "status_persist_failed"
    return report


def _archive_unknown(state: dict, delivery: dict) -> None:
    """Unresolved history is evidence, but the state file must stay
    bounded: keep the newest UNKNOWN_HISTORY records."""
    held = state.get("unknown_deliveries")
    held = held if isinstance(held, list) else []
    state["unknown_deliveries"] = [*held[-UNKNOWN_HISTORY:], dict(delivery)][-UNKNOWN_HISTORY:]


def _reconcile_delivery(state: dict, status: str, key: tuple,
                        alert: bool, disk_low: bool, disk_alert: bool,
                        transition: bool, new_reason: bool,
                        now: float) -> dict:
    """Carry over or replace the durable send record for this verdict.

    pending/not_sent deliveries survive only while their component is
    still active; an unknown outcome is held verbatim — it can neither
    be retried nor manufactured into a success. A REALERT_S re-alert
    arrives as new_reason: it is a fresh alert, not a retry, so a lost
    send cannot silence a persisting incident forever."""
    delivery = state.get("delivery")
    delivery = delivery if isinstance(delivery, dict) else {}
    current_alert = bool(delivery.get("alert") and status != "ok")
    current_disk_alert = bool(delivery.get("disk_alert") and disk_low)
    if delivery.get("outcome") == "unknown" and not (current_alert or current_disk_alert):
        # Recovery resolves the incident, not its uncertain send outcome.
        # Retain the witness as unknown; do not manufacture a successful send.
        _archive_unknown(state, delivery)
        state.pop("delivery", None)
        delivery = {}
    elif delivery.get("outcome") in ("pending", "not_sent"):
        # Old provably-unsent alerts may not be retried with a healthy
        # current report. Keep any still-active low-disk component.
        delivery.update(alert=current_alert, disk_alert=current_disk_alert)
        if not (current_alert or current_disk_alert):
            delivery.update(outcome="superseded", superseded_at=now)
    delivery_key = [*key, disk_low, state["alerted_reasons"]]
    if (alert or disk_alert) and not (delivery.get("outcome") == "unknown"
                                     and not (transition or new_reason or disk_alert)):
        # An uncertain send stays held, including hourly observations
        # of the same incident. A new verdict/reason is a different alert.
        if delivery.get("key") != delivery_key or delivery.get("outcome") in ("delivered", "superseded") \
                or (new_reason and delivery.get("outcome") == "unknown"):
            if delivery.get("outcome") == "unknown":
                _archive_unknown(state, delivery)
            delivery = {"key": delivery_key, "outcome": "pending",
                        "alert": bool(alert), "disk_alert": disk_alert}
        elif delivery.get("outcome") != "unknown":
            delivery.update(alert=bool(alert or delivery.get("alert")),
                            disk_alert=bool(disk_alert or delivery.get("disk_alert")))
    return delivery


def deliver_alert(cfg: dict, text: str) -> bool | None:
    """Best-effort copy of an alert line to the system notification
    target. cron/launchd stdout only reaches a log file, and the
    outbox drains inside run_check — the very producer a stale verdict
    says is down — so this uses notify_flush's sender directly.
    Return True for delivered, False for provably unsent, None for an
    uncertain outcome which must not be blindly repeated."""
    try:
        import notify_sender
        import mcs_util
        target = notify_sender.target(cfg, "run_failed")
        if not target:
            return False
        argv = notify_sender.send_argv(cfg, target, mcs_util.CONF_PATH)
    except Exception:
        return False                   # transport has not been called
    try:
        notify_sender.send(argv,
                           "[MCS] 監視警報\n" + text,
                           deadline=time.monotonic() + 60)
        return True
    except (notify_sender.SendFailed, notify_sender.SendUsage):
        return False
    except Exception:
        return None                    # includes _SendUncertain


_STATUS_JA = {"degraded": "一部に異常があります", "failed": "収集が失敗しました",
              "stale": "状態の記録が更新されていません", "missing": "状態の記録がありません",
              "corrupt": "状態の記録が壊れています"}
_SEVERE = frozenset({"failed", "stale", "missing", "corrupt"})
_CHECK = "python3 mcs/ops/mcs_setup.py check"
_COLLECTION_LOG = "data/run.log"  # mcs_check.sh redirects run_check output in both modes.
_STANDALONE_STATUS = "python -m mcs_standalone status"
_STANDALONE_RESTART = ("hostを再起動（macOS: launchctl kickstart -k gui/$(id -u)/ai.mcs.standalone、"
                       "Linux: systemctl --user restart mcs-standalone.service）")
# code -> (what happened, in plain words / what to do next)
_BACKUP_FAILURE_JA = {
    "backup_retention_capacity": (
        "前回のバックアップは保持上限で失敗しました",
        "保存済みバックアップと保持条件を管理者が確認"),
    "backup_cipher_unavailable": (
        "前回のバックアップで暗号化ツールを利用できませんでした",
        "バックアップ設定とOpenSSLの実行環境を確認"),
}

_REASON_JA = {
    "worker_start_unverified": ("通知workerの起動を確認できません",
                                "slack_profile / profile と対象gatewayの worker_started を確認"),
    "worker_start_failed": ("通知workerの起動に失敗しました",
                            "gatewayのworker_start_failed記録を確認してください"),
    "route_flags_unready": ("通知workerの配送条件が整っていません",
                            "通知設定と公開された配送条件を確認し、対象gatewayの起動記録を確認してください"),
    "worker_restart_required": ("通知workerのコード版が更新前です",
                                "対象の全profile gatewayを再起動し worker_started を確認"),
    "profile_mismatch": ("通知workerの担当profileが一致しません",
                         "通知設定のprofileとgatewayの所有profileを一致させて再起動"),
    "owner_unverified": ("通知workerの所有profileを確認できません",
                         "固定Hermes版とprofile設定を確認。所有不明のworkerは起動しません"),
    "application_mismatch": ("DiscordのBotと通知設定が一致しません",
                             "Bot IDとapplication_idを確認"),
    "standalone_owned": ("独立動作が配送を担当しています",
                         "runtime_mode と通知設定のどちらが配送を担当するか確認"),
    "restore_delivery_held": ("復元後の通知を保留しています",
                              "復元は収集再開までです。配送先照合と個別承認なしに通知を再開しません"),
    "run_failed": ("収集の実行が失敗しました",
                   f"{_COLLECTION_LOG} の末尾を確認。続く場合は {_CHECK}"),
    "session_expired": ("MCSのログインが切れ、自動の再ログインも失敗しました",
                        "MCSに手動でログインし直す（docs/guides/INSTALLATION.md の session_expired）"),
    "stage_errors": ("収集の一部の処理でエラーがありました",
                     f"次回の収集で再試行されます。続く場合は {_COLLECTION_LOG} を確認"),
    "code_changed": ("収集中にコードが更新されました", "対処不要。次回の収集で続行します"),
    "run_deadline_exceeded": ("収集が制限時間を超えました",
                              "続く場合は llama-server とネットワークの負荷を確認"),
    "ledger_relation_violations": ("保存データの整合性に問題があります",
                                   f"{_CHECK} を実行し、修復手順に従う"),
    "backup_not_verified": ("バックアップの完了を確認できません",
                            "バックアップの設定と保存先の空き容量を確認"),
    "collection_incomplete": ("一部の患者の記録を取得できていません",
                              "次回以降に再取得されます。続く場合はMCSの画面と閲覧権限を確認"),
    "notification_failed": ("通知の送信に失敗しました",
                            "通知先の設定と Hermes gateway（独立接続なら ai.mcs.standalone）の稼働を確認"),
    "notification_deferred": ("通知を先送りしました", "doctor で先送り理由を確認。送信条件が整うまで保留されます"),
    "notification_parked": ("通知が待機中です", "次回の収集で再確認されます"),
    "notification_held": ("通知が保留されています",
                          "python3 mcs/ops/mcs_setup.py doctor で保留理由を確認"),
    "notification_pending": ("送信待ちの通知があります",
                             "doctor で待ちの理由と通知先・gateway の稼働を確認"),
    "disk_low": ("ディスクの空き容量が不足しています", "不要なファイルを削除するか保存先を広げる"),
    "card_delivery_stalled": ("カードの配送が30分以上止まっています",
                              "slack_profile / profile と worker_started の記録を確認。設定が正しくworkerが停止している場合は hermes gateway restart"),
    "extract_backlog_stalled": ("要約の抽出が6時間以上止まっています",
                                "llama-server と extract-drainer の稼働を確認（launchctl list | grep mcs）"),
    "semantic_backlog_stalled": ("意味チェックが6時間以上止まっています",
                                 "extract-drainer の稼働と data/extract_drain.log の jev_error を確認"),
    "local_llm_down": ("ローカルLLMに1時間以上つながらず、要約の抽出が止まっています",
                       "llama-server の稼働を確認（再起動後は自動で再開します）"),
    "llm_endpoint_down": ("ローカルLLMの失敗が続き、要約の抽出が6時間以上進んでいません",
                          "llama-server の稼働と data/extract_drain.log を確認"),
    "backup_deferred_deadline": ("収集の制限時間内に日次バックアップを取れず、1日以上更新されていません",
                                 f"DBの大きさと収集の所要時間を確認。続く場合は {_CHECK}"),
    "stall_check_failed": ("停滞の判定処理が失敗しました",
                           f"{_COLLECTION_LOG} の stall_check_failed を確認。続く場合は {_CHECK}"),
    "semantic_jev_payment_required": ("TypeSafe Jev が支払い未了（HTTP 402）を返しています",
                                      "TypeSafe の契約・残高を確認。解消まで意味チェックは待機し、収集・通知には影響しません"),
    "semantic_jev_no_api_key": ("TypeSafe Jev のAPIキーが設定されていません",
                                "TYPESAFE_API_KEY を .env に設定"),
    "semantic_jev_budget_exceeded": ("TypeSafe Jev の1日の利用上限に達しました",
                                     "翌日に自動で再開します。上限は semantic.daily_request_budget"),
    "semantic_jev_auth_error": ("TypeSafe Jev の認証に失敗しました",
                                "TYPESAFE_API_KEY の値と有効期限を確認"),
    "cloud_llm_auth_error": ("Anthropic の認証に失敗し、外部AI処理を停止しました",
                              "MCS_ANTHROPIC_API_KEY を確認し、cloud-llm resume で再開。処理はローカルLLMへ戻ります"),
    "cloud_llm_payment_required": ("Anthropic の支払い条件を確認するため、外部AI処理を停止しました",
                                    "契約・残高を確認し、cloud-llm resume で再開。処理はローカルLLMへ戻ります"),
    "cloud_llm_pricing_unverified": ("Anthropic の料金または使用量を確認できず、外部AI処理を停止しました",
                                     "doctor で料金表の期限と使用量を確認し、問題を解消してから cloud-llm resume で再開"),
    "cloud_llm_circuit_open": ("Anthropic への要求が続けて失敗し、外部AI処理を一時停止しています",
                                "doctor で停止理由を確認。処理はローカルLLMへ戻り、待機期間後に再確認します"),
    "unread_collection_unknown": ("未読収集の状況が記録されていません",
                                  f"次回の収集を待ち、続く場合は {_COLLECTION_LOG} を確認"),
    "standalone_host_down": ("独立接続のhostが動いていません",
                             f"{_STANDALONE_STATUS} と data/standalone.log を確認し、{_STANDALONE_RESTART}"),
    "standalone_child_failing": ("hostの子プロセスが異常終了を繰り返しています",
                                 f"data/<ジョブ名>.log の末尾と {_STANDALONE_STATUS} の exits を確認"),
    "standalone_connector_down": ("Slack/Discordとの接続が回復していません",
                                  "data/standalone.log の connector_ended と認証情報・ネットワークを確認"
                                  "（収集と抽出は継続し、接続は自動で再試行します）"),
    "standalone_update_stuck": ("更新処理が終わらず、収集と抽出が止まっています",
                                "data/update.log と python3 mcs/ops/mcs_update.py status を確認し、"
                                "必要なら python3 mcs/ops/mcs_update.py recover"),
    "standalone_check_stalled": ("hostの未読収集が成功していません",
                                 f"data/mcs_check.log と {_COLLECTION_LOG} を確認。続く場合は {_CHECK}"),
}
# runtime_mode=standalone: the host owns the connection and the workers,
# so the Hermes gateway / per-agent hints do not apply
_STANDALONE_ACTION = {
    "notification_failed": "通知先の設定と独立接続のhostの稼働を確認"
                           f"（{_STANDALONE_STATUS}）",
    "notification_pending": f"doctor で待ちの理由と {_STANDALONE_STATUS} を確認",
    "card_delivery_stalled": _STANDALONE_RESTART,
    "extract_backlog_stalled": f"llama-server の稼働と {_STANDALONE_STATUS} の extract-0・extract-2 を確認",
}
# notify.interactive=lineworks (Hermes mode): the separate LINE WORKS adapter
# delivers, so the gateway/profile hints do not apply
_LW_RESTART = ("launchctl kickstart -k gui/$(id -u)/ai.mcs.lineworks で独立アダプターを再起動し、"
               "python3 -m lineworks_adapter check と data/lineworks_state/service.stderr.log を確認")
_LINEWORKS_ACTION = {
    "worker_start_unverified": "LINE WORKSアダプターの起動記録を確認。止まっていれば " + _LW_RESTART,
    "worker_start_failed": "data/lineworks_state/service.stderr.log で起動失敗を確認し、" + _LW_RESTART,
    "worker_restart_required": "更新後のコードで動かすため " + _LW_RESTART,
    "card_delivery_stalled": "LINE WORKSアダプターの稼働を確認。止まっていれば " + _LW_RESTART,
}
_AREA_JA = (("semantic", "意味チェック（任意機能）"), ("extract_backlog", "要約の抽出"),
            ("cloud_llm", "外部AI処理"),
            ("local_llm", "要約の抽出"), ("llm_endpoint", "要約の抽出"),
            ("notification", "通知"), ("card_delivery", "通知"),
            ("collection_incomplete", "一部患者の記録取得"), ("disk_low", "保存"),
            ("ledger", "保存データ"), ("backup", "バックアップ"))


def _jst(ts) -> str:
    if not finite_number(ts):
        return "不明"
    try:
        return datetime.fromtimestamp(ts, JST).strftime("%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "不明"


def _hours(seconds) -> str:
    return f"{seconds / 3600:.1f}時間" if finite_number(seconds) else ""


def _impact(report: dict, reasons) -> str:
    """One sentence a staff member can act on: what is affected and
    whether collection itself is still running."""
    if report["status"] in _SEVERE:
        return "新しい連絡の収集と通知が止まっている可能性があります。"
    areas = []
    for r in reasons or []:
        for prefix, name in _AREA_JA:
            if r.startswith(prefix) and name not in areas:
                areas.append(name)
    parts = []
    if areas:
        parts.append("・".join(areas) + "に遅れや不具合があります。")
    if report.get("run_status") == "ok":
        parts.append("収集は動いています。")
    return "".join(parts) or "収集は動いていますが、確認が必要な状態です。"


_WAIT_JA = {
    "notice_render_live": "お知らせの送信中",
    "notice_interactive_off": "カード表示が無効のためお知らせを保留",
    "notice_digest_unset": "要約の設定がないためお知らせを保留",
    "notice_superseded": "新しいお知らせがあるため保留",
    "notice_render_issued": "お知らせの作成待ち",
    "alert_outcome_pending": "緊急通知の送達確認待ち",
    "alert_render_live": "緊急通知の送信中",
    "alert_render_issued": "緊急通知の作成待ち",
    "signals_off_sealed": "設定によりシグナル通知を保留",
    "intent_cards_unsettled": "カードの配信完了待ち",
    "intent_cards_issued": "カードの作成待ち",
    "intent_card_delivery_unknown": "カードの配信結果が不明",
    "card_parts_pending": "前回の本文・添付の送信完了待ち",
    "card_parts_unknown": "前回の本文・添付の送信結果が不明（照合待ち）",
    "unknown": "状態を読めない記録",
    "stale": "最後の記録以降に状態が変化",
    "unrecorded": "待ちの理由が未記録",
}
_INTERRUPT_JA = {
    "watchdog_exit": "制限時間超過で打ち切り",
    "update_preempted": "更新処理による中断",
    "host_restart": "端末の再起動",
    "unknown": "原因を特定できません",
}
_BACKUP_LOCAL_JA = {
    "missing": "日次ローカルバックアップがありません",
    "unreadable": "日次ローカルバックアップを読み取れません",
    "unverified": "日次ローカルバックアップの検証記録が現在のファイルと一致しません",
    "unknown": "日次ローカルバックアップの状態を確認できません",
}


def _wait_label(code: str) -> str:
    if code in _WAIT_JA:
        return _WAIT_JA[code]
    kind = code.split(":")[0]
    return {"hold_reason": "送信を保留", "thread_hold": "スレッドの作成待ち"}.get(kind, "待機中")


def _wait_summary(wait, recorded: bool) -> str:
    """Largest waits first, at most three, e.g. 「内訳: カードの配信結果が不明 1件」."""
    if not wait or not wait["reasons"]:
        return ""
    top = sorted(wait["reasons"].items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    text = "、".join(f"{_wait_label(code)} {n}件" for code, n in top)
    head = "記録時点の内訳" if recorded else "内訳"
    return f"（{head}: {text}" + (f"、要確認 {wait['attention']}件" if wait["attention"] else "") + "）"


def _backup_local_text(local) -> str:
    if not local:
        return ""
    if local["state"] == "verified":
        age = _hours(local["newest_age_s"])
        # the age is the file's, not the check's: the existing record
        # matches the file saved then
        return (f"（日次ローカルバックアップ: {age}前の保存ファイルと検証記録が一致）" if age
                else "（日次ローカルバックアップ: 保存ファイルと検証記録が一致）")
    return f"（{_BACKUP_LOCAL_JA[local['state']]}）"


def _reason_rows(report: dict, reasons) -> list[str]:
    if reasons is None:
        return ["・理由を特定できません（状態の記録に理由がありません）",
                f"　→ {_CHECK} で確認"]
    if not reasons:
        return ["・記録された理由はありません", "　→ 次回の収集を待つ"]
    oldest = report.get("oldest_age_s") or {}
    standalone = report.get("runtime_mode") == "standalone"
    host = report.get("host_details") if isinstance(report.get("host_details"), dict) else {}
    rows = []
    for code in reasons:
        base = code.split(":")[0]
        meaning, action = _REASON_JA.get(base, (code, f"{_CHECK} で確認"))
        if standalone:
            action = _STANDALONE_ACTION.get(base, action)
        elif report.get("notify_transport") == "lineworks":
            action = _LINEWORKS_ACTION.get(base, action)
        if isinstance(host.get(base), str) and host[base]:
            meaning += f"（{host[base]}）"
        if base == "backup_not_verified":
            cause = report.get("backup_failure_code")
            if isinstance(cause, str) and cause in _BACKUP_FAILURE_JA:
                meaning, action = _BACKUP_FAILURE_JA[cause]
        extra = ""
        if base in ("notification_pending", "notification_deferred", "notification_parked"):
            wait = report.get("notify_wait") or {}
            waits = wait.get("reasons") or {}
            if report.get("observations_recorded") or not waits or any(
                    code in waits for code in ("stale", "unknown", "unrecorded")):
                action = "doctor で現在の待ち理由を確認。記録だけでは再送可否を判断できません"
            elif any("unknown" in code for code in waits):
                action = "送信先と配送記録を照合。送信結果が不明な通知は自動で再送しません"
            elif wait.get("attention") or any(
                    "hold" in code or "exhausted" in code for code in waits):
                action = "doctor で保留・再試行上限の理由を確認。解除には該当する承認手順が必要です"
            else:
                action = "送信条件を次回の収集で再確認します。配送完了はまだ確認できていません"
        if base == "notification_pending":
            n = report.get("notify_pending")
            age = _hours(oldest.get("notify"))
            extra = ((f" {int(n)}件" if finite_number(n) else "") + (f"（最古 {age}）" if age else "")
                     + _wait_summary(report.get("notify_wait"), report.get("observations_recorded", False)))
        elif base in ("backup_not_verified", "backup_deferred_deadline"):
            extra = ("" if report.get("observations_recorded")
                     else _backup_local_text(report.get("backup_local")))
        elif base == "semantic_backlog_stalled" and _hours(oldest.get("semantic_jobs")):
            extra = f"（最古 {_hours(oldest.get('semantic_jobs'))}）"
        guide = None
        if base in ("stage_errors", "collection_incomplete") and not report.get("observations_recorded") \
                and report.get("collection_diagnostics") is not None:
            from collection_diagnostics import detail_lines, guidance
            section = "errors" if base == "stage_errors" else "incomplete"
            details = detail_lines(report.get("collection_diagnostics"), section)
            extra += "（" + " / ".join(details) + "）"
            if base == "stage_errors":
                guide = guidance(report.get("collection_diagnostics"))
        if guide:
            # one fixed cause and next step per observed category, most frequent first
            rows.append(f"・{meaning}{extra}")
            for label, n, what, todo in guide:
                rows += [f"　・{label}（{n}件）: {what}", f"　　→ {todo}"]
            continue
        rows += [f"・{meaning}{extra}", f"　→ {action}"]
    return rows


def _alert_lines(report: dict) -> list[str]:
    """Staff-readable alert: severity and status first, then the impact,
    each reason with its next step, timestamps in JST, and the raw codes
    last on one line for operators to search."""
    lines = []
    if report["alert"] and report["status"] != "ok":
        status = report["status"]
        reasons = report.get("state_reasons")
        glyph = "🔴" if status in _SEVERE else "🟠"
        lines.append(f"{glyph} MCS監視: {_STATUS_JA.get(status, status)}")
        lines.append("影響: " + _impact(report, reasons))
        streak = report.get("streak") or {}
        count, first, at = streak.get("count"), streak.get("first_at"), report.get("health_at")
        if type(count) is int and count >= STREAK_ALERT and finite_number(first) \
                and finite_number(at) and 0 <= at - first:
            # observed count and the actual elapsed time, never a nominal cadence
            lines.append(f"継続: 同じ異常を{count}回続けて観測"
                         f"（初回 {_jst(first)}、約{round((at - first) / 60)}分継続）")
        lines.append("原因と対処:")
        lines.extend(_reason_rows(report, reasons))
        interrupt = report.get("last_interrupt")
        at = report.get("health_at")
        if (interrupt and not report.get("observations_recorded")
                and finite_number(at) and 0 <= at - interrupt["detected_at"] <= 86400):
            label = _INTERRUPT_JA.get(interrupt["reason"], _INTERRUPT_JA["unknown"])
            # the run's own end time is not known — only when it was found
            lines.append(f"直近の中断: {label}（{_jst(interrupt['detected_at'])} に検出）")
        lines.append(f"最終正常 {_jst(report.get('last_ok_at'))}"
                     f" / 最新記録 {_jst(report.get('health_at'))}（JST）")
        lines.append(f"コード: {status} " + (",".join(reasons) if reasons else
                                            "none" if reasons is not None else "unknown"))
    if report["disk_alert"] and report["disk_low"]:
        free = report.get("disk_free_mb")
        lines.append(f"空き容量不足: 残り {free} MB" if finite_number(free)
                     else "空き容量不足: 残り 不明")
    return lines


def _watch(args, cfg) -> int:
    report = evaluate(home=args.home, now=args.now, cfg=cfg)
    for line in _alert_lines(report):
        print(line)
    if report.get("delivery_error"):
        return 1
    delivery = report.get("delivery") or {}
    now = report["watched_at"]
    tried = delivery.get("attempted_at")
    if delivery.get("outcome") not in ("pending", "not_sent") \
            or (finite_number(tried) and now - tried < DELIVERY_RETRY_S):
        return 0
    lines = _alert_lines({**report, "alert": delivery.get("alert"),
                          "disk_alert": delivery.get("disk_alert")})
    if not lines:
        return 0
    state_path = os.path.join(args.home, STATE_REL)
    state = load_config(state_path)
    if state.get("delivery") != delivery:
        return 1                         # persistence failed or evidence changed
    delivery.update(outcome="unknown", attempted_at=now)
    state["delivery"] = delivery
    try:
        # Durable before crossing the wire: a killed watcher never
        # turns a possibly delivered message into a retryable send.
        safe_io.atomic_publish_text(state_path, json.dumps(state, ensure_ascii=False))
    except OSError:
        report["delivery_error"] = "state_persist_failed"
        report["delivery"] = {**delivery, "outcome": "not_sent"}
        try:
            safe_io.atomic_publish_text(os.path.join(args.home, STATUS_REL),
                                            json.dumps(report, ensure_ascii=False))
        except OSError:
            pass
        return 1                         # never send without the intent witness
    outcome = deliver_alert(cfg, "\n".join(lines))
    delivery["outcome"] = ("delivered" if outcome is True else
                           "not_sent" if outcome is False else "unknown")
    if outcome is True:
        state["alerted_at"] = now
    report["delivery"] = dict(delivery)
    try:
        safe_io.atomic_publish_text(state_path, json.dumps(state, ensure_ascii=False))
    except OSError:
        report["delivery_error"] = "state_persist_failed"
    try:
        safe_io.atomic_publish_text(os.path.join(args.home, STATUS_REL),
                                        json.dumps(report, ensure_ascii=False))
    except OSError:
        return 1
    if report.get("delivery_error"):
        return 1                         # old unknown witness safely holds the send
    return 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="MCS health watcher")
    ap.add_argument("--home", default=HOME)
    ap.add_argument("--now", type=float, default=None,
                    help="inject clock for tests/dry-run")
    ap.add_argument("--config", default=None,
                    help="config.json path (default ~/.mcs/config.json)")
    args = ap.parse_args(argv)
    if args.now is not None and not finite_number(args.now):
        ap.error("now must be a finite number")
    cfg = load_config(args.config) if args.config else load_config()
    try:
        directory = os.path.join(args.home, "data")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(directory, "health_watch.lock"),
                     os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with safe_io.fdopen_owned(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return _watch(args, cfg)
    except BlockingIOError:
        return 0                         # the existing watcher owns this observation
    except OSError:
        return 1                         # unavailable state store is not successful delivery


if __name__ == "__main__":
    sys.exit(main())
