#!/usr/bin/env python3
"""MCS health watcher — independent supervised reader of health.json.

run_check exits 0 even for a partial run by design, and a dead producer
leaves the last health.json behind. This watcher never trusts process
exit codes: it re-derives service status from the file itself —
presence, parseability, freshness — so 'missing', 'stale' and 'corrupt'
are detectable even when every run "succeeded".

Freshness deadline comes from config (health.tick_interval_s, default
300 — the deployed 5-minute tick — and health.max_missed_runs, default
2). The deployed check skips most 5-minute ticks overnight, so the
default watcher counts the actual scheduled ticks and allows one run
deadline for the last due tick. No hardcoded 'healthy'.

Output contract: alert content is status codes/counters only — never
patient data. stdout carries one alert line on alert transitions
(watchdog convention); data/health_watch_status.json is the durable
machine-readable status, published atomically.
"""
import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

import maintenance
from mcs_util import HOME, load_config

HEALTH_REL = os.path.join("data", "health.json")
STATE_REL = os.path.join("data", "health_watch.json")
STATUS_REL = os.path.join("data", "health_watch_status.json")

DEFAULT_TICK_S = 300        # deployed cron cadence: */5 * * * *
DEFAULT_MAX_MISSED = 2      # miss two whole ticks before 'stale'
RUN_GRACE_S = 480           # run_check's whole-run deadline
NIGHT_HOURS = {22, 23, 0, 1, 2, 3, 4, 5, 6}
REALERT_S = 3600            # unchanged bad state re-alerts hourly

OVERALL_STATUS = {"ok": "ok", "degraded": "degraded",
                  "failed": "failed"}


def _finite_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _tick_settings(cfg: dict) -> tuple[dict, float, float]:
    """(health block, tick_interval_s, max_missed_runs) with defaults
    applied to missing or invalid values."""
    h = cfg.get("health") if isinstance(cfg, dict) else None
    h = h if isinstance(h, dict) else {}
    tick = h.get("tick_interval_s", DEFAULT_TICK_S)
    missed = h.get("max_missed_runs", DEFAULT_MAX_MISSED)
    if not (_finite_number(tick) and tick > 0):
        tick = DEFAULT_TICK_S
    if not (_finite_number(missed) and missed >= 0):
        missed = DEFAULT_MAX_MISSED
    return h, tick, missed


def freshness_deadline(cfg: dict) -> int:
    """tick_interval_s * (max_missed_runs + 1) from config — a reader
    must not assume 'healthy' while the producer could legitimately be
    inside its allowed missed-run window."""
    h, tick, missed = _tick_settings(cfg)
    deadline = tick * (int(missed) + 1)
    return max(1, int(deadline)) if _finite_number(deadline) \
        else DEFAULT_TICK_S * (DEFAULT_MAX_MISSED + 1)


def _scheduled_deadline(health_at: float, cfg: dict,
                        fallback: int) -> float:
    """Deadline from the deployed day/night check schedule.

    Other tick settings use their configured uniform interval. A run
    may finish up to RUN_GRACE_S after its scheduled start, so the
    third missed tick is only stale after that finish window.
    """
    h, tick, missed = _tick_settings(cfg)
    if tick != DEFAULT_TICK_S or h.get("night_thinning") is False \
            or type(missed) is not int or not 0 <= missed <= 100:
        return fallback
    due = 0
    slot = (int(health_at) // DEFAULT_TICK_S + 1) * DEFAULT_TICK_S
    try:
        while due <= missed:
            local = time.localtime(slot)
            if local.tm_hour not in NIGHT_HOURS \
                    or local.tm_min in (0, 20, 40):
                due += 1
            slot += DEFAULT_TICK_S
    except (OverflowError, OSError, ValueError):
        return fallback
    return slot - DEFAULT_TICK_S + RUN_GRACE_S - health_at


def classify_health(path: str, now: float, deadline_s: int,
                    cfg: dict | None = None) -> dict:
    """File evidence -> status. Staleness is checked BEFORE the recorded
    payload: a dead producer leaves a fresh-looking 'ok' forever."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return {"status": "missing"}
    try:
        h = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "corrupt"}
    if not isinstance(h, dict) or not _finite_number(h.get("at")):
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
    binding = (min(unread_at, h["at"]) if _finite_number(unread_at)
               else h["at"])
    if cfg is not None:
        deadline_s = _scheduled_deadline(binding, cfg, deadline_s)
    age = now - binding
    report = {"health_at": h["at"], "age_s": round(max(age, 0), 1),
              "overall": overall, "run_status": h.get("run_status"),
              "run_id": h.get("run_id"), "deadline_s": deadline_s,
              "disk_low": h.get("disk_low") is True,
              "disk_free_mb": h.get("disk_free_mb")}
    report["status"] = ("stale" if age > deadline_s
                        else OVERALL_STATUS[overall])
    # dedup stamp: unread_at only when that age is what made it stale
    report["evidence_at"] = (binding if report["status"] == "stale"
                             else h["at"])
    return report


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


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
    a persistent 'degraded' every 5 minutes. An unchanged verdict
    never re-alerts. ok->ok never alerts even when the file is fresh —
    a healthy producer keeping cadence is not an event. Alerts fire
    on: first non-ok observation, every transition INTO a non-ok
    status, one bad->ok recovery, and an unchanged non-ok state
    re-alerted after REALERT_S. 'ok' is only produced by a fresh
    in-deadline file — recovery can never be assumed. State files
    that predate evidence_at are read via health_at.

    disk_low has its own dedup: disk_alert fires only when a fresh
    file flips it (either way); stale/missing/corrupt evidence keeps
    the last known value."""
    now = time.time() if now is None else now
    cfg = load_config() if cfg is None else cfg
    deadline = freshness_deadline(cfg)
    health_path = os.path.join(home, HEALTH_REL)
    state_path = os.path.join(home, STATE_REL)
    status_path = os.path.join(home, STATUS_REL)

    obs = classify_health(health_path, now, deadline, cfg)
    state = _load_state(state_path)
    last = state.get("last")
    last = last if isinstance(last, dict) else {}
    key = (obs["status"],
           _dedup_stamp(obs) if obs["status"] == "stale" else None)
    last_key = (last.get("status"),
                _dedup_stamp(last) if last.get("status") == "stale"
                else None)
    prior_known = bool(last)
    transition = key != last_key
    alerted_at = state.get("alerted_at")
    invalid_alert_at = not _finite_number(alerted_at) \
        or alerted_at > now
    realert = (obs["status"] != "ok"
               and (invalid_alert_at
                    or now - alerted_at >= REALERT_S))
    if not prior_known:
        alert = obs["status"] != "ok"
    else:
        # a fresh 'ok' replacing an 'ok' is a producer keeping its
        # cadence, not an event — only bad->ok recovery and any
        # transition into a non-ok status carry an alert
        alert = transition and (obs["status"] != "ok"
                                or last.get("status") != "ok")
    alert = alert or realert
    disk_prev = state.get("disk_low") is True
    disk_low = (obs["disk_low"] if obs["status"] in OVERALL_STATUS
                else disk_prev)
    disk_alert = disk_low != disk_prev
    state["disk_low"] = disk_low

    report = dict(obs)
    report.update({"deadline_s": obs.get("deadline_s", deadline), "alert": alert,
                   "disk_low": disk_low, "disk_alert": disk_alert,
                   "watched_at": now})
    state["last"] = {"status": obs["status"],
                     "health_at": obs.get("health_at"),
                     "evidence_at": _dedup_stamp(obs)}
    if alert:
        state["alerted_at"] = now
    try:
        maintenance.atomic_publish_text(
            state_path, json.dumps(state, ensure_ascii=False))
        maintenance.atomic_publish_text(
            status_path, json.dumps(report, ensure_ascii=False))
    except OSError:
        pass    # persistence failure must not crash the watcher
    return report


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="MCS health watcher")
    ap.add_argument("--home", default=HOME)
    ap.add_argument("--now", type=float, default=None,
                    help="inject clock for tests/dry-run")
    ap.add_argument("--config", default=None,
                    help="config.json path (default ~/.mcs/config.json)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config) if args.config else None
    report = evaluate(home=args.home, now=args.now, cfg=cfg)
    if report["alert"]:
        age = report.get("age_s")
        print("mcs health: {status} (overall={overall} "
              "health_at={health_at} age_s={age} deadline_s={dl})".format(
                  status=report["status"],
                  overall=report.get("overall"),
                  health_at=report.get("health_at"),
                  age=age, dl=report["deadline_s"]))
    if report["disk_alert"]:
        print("mcs disk: {} (free_mb={})".format(
            "low" if report["disk_low"] else "recovered",
            report.get("disk_free_mb")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
