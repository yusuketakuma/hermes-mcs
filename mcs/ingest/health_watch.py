#!/usr/bin/env python3
"""MCS health watcher — independent supervised reader of health.json.

run_check exits 0 even for a partial run by design, and a dead producer
leaves the last health.json behind. This watcher never trusts process
exit codes: it re-derives service status from the file itself —
presence, parseability, freshness — so 'missing', 'stale' and 'corrupt'
are detectable even when every run "succeeded".

Freshness deadline comes from config (health.tick_interval_s, default
300 — the deployed 5-minute tick — and health.max_missed_runs, default
2): deadline = tick * (missed + 1). No hardcoded 'healthy'.

Output contract: alert content is status codes/counters only — never
patient data. stdout carries one alert line on alert transitions
(watchdog convention); data/health_watch_status.json is the durable
machine-readable status, published atomically.
"""
import argparse
import json
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
REALERT_S = 3600            # unchanged bad state re-alerts hourly

OVERALL_STATUS = {"ok": "ok", "degraded": "degraded",
                  "failed": "failed"}


def freshness_deadline(cfg: dict) -> int:
    """tick_interval_s * (max_missed_runs + 1) from config — a reader
    must not assume 'healthy' while the producer could legitimately be
    inside its allowed missed-run window."""
    h = cfg.get("health") if isinstance(cfg, dict) else None
    h = h if isinstance(h, dict) else {}
    tick = h.get("tick_interval_s", DEFAULT_TICK_S)
    missed = h.get("max_missed_runs", DEFAULT_MAX_MISSED)
    if not (isinstance(tick, (int, float)) and tick > 0):
        tick = DEFAULT_TICK_S
    if not (isinstance(missed, (int, float)) and missed >= 0):
        missed = DEFAULT_MAX_MISSED
    return int(tick * (int(missed) + 1))


def classify_health(path: str, now: float, deadline_s: int) -> dict:
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
    if not isinstance(h, dict) \
            or not isinstance(h.get("at"), (int, float)):
        return {"status": "corrupt"}
    overall = h.get("overall")
    if overall not in OVERALL_STATUS:
        return {"status": "corrupt"}
    age = now - h["at"]
    report = {"health_at": h["at"], "age_s": round(max(age, 0), 1),
              "overall": overall, "run_status": h.get("run_status"),
              "run_id": h.get("run_id")}
    if age > deadline_s:
        report["status"] = "stale"
        return report
    report["status"] = OVERALL_STATUS[overall]
    return report


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def evaluate(home: str = HOME, now: float | None = None,
             cfg: dict | None = None) -> dict:
    """Classify current evidence, apply alert dedup, persist state.

    Dedup key is (status, health_at): an unchanged file never re-alerts.
    A non-ok status re-alerts after REALERT_S. 'ok' is only produced by
    a fresh in-deadline file — recovery can never be assumed."""
    now = time.time() if now is None else now
    cfg = load_config() if cfg is None else cfg
    deadline = freshness_deadline(cfg)
    health_path = os.path.join(home, HEALTH_REL)
    state_path = os.path.join(home, STATE_REL)
    status_path = os.path.join(home, STATUS_REL)

    obs = classify_health(health_path, now, deadline)
    state = _load_state(state_path)
    last = state.get("last") or {}
    key = (obs["status"], obs.get("health_at"))
    last_key = (last.get("status"), last.get("health_at"))
    prior_known = bool(last)
    transition = key != last_key
    realert = (obs["status"] != "ok"
               and now - (state.get("alerted_at") or 0) >= REALERT_S)
    alert = transition if prior_known else obs["status"] != "ok"
    alert = alert or realert

    report = dict(obs)
    report.update({"deadline_s": deadline, "alert": alert,
                   "watched_at": now})
    state["last"] = {"status": obs["status"],
                     "health_at": obs.get("health_at")}
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
