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
import fcntl
import json
import math
import os
import re
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

DEFAULT_TICK_S = 600        # deployed cron cadence: */10 * * * *
DEFAULT_MAX_MISSED = 2      # miss two whole ticks before 'stale'
RUN_GRACE_S = 480           # run_check's whole-run deadline
REALERT_S = 4 * 3600        # unchanged bad state re-alerts every 4h
DELIVERY_RETRY_S = 60       # only provably unsent alerts may retry
UNKNOWN_HISTORY = 50        # retained uncertain-delivery records

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


def _is_code(value) -> bool:
    """Producer reason code (run_check emits lowercase snake_case only)."""
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,64}", value) is not None


def classify_health(path: str, now: float, deadline_s: int) -> dict:
    """File evidence -> status. Staleness is checked BEFORE the recorded
    payload: a dead producer leaves a fresh-looking 'ok' forever."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return {"status": "missing"}
    except UnicodeError:
        return {"status": "corrupt"}
    try:
        h = json.loads(raw)
    except (ValueError, RecursionError):
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
    age = now - binding
    report = {"health_at": h["at"], "age_s": round(max(age, 0), 1),
              "overall": overall, "run_status": h.get("run_status"),
              "run_id": h.get("run_id"), "deadline_s": deadline_s,
              "disk_low": h.get("disk_low") is True,
              "disk_free_mb": h.get("disk_free_mb")
              if _finite_number(h.get("disk_free_mb")) else None}
    report["status"] = ("stale" if age > deadline_s
                        else OVERALL_STATUS[overall])
    unread_unknown = "unread_at" in h and not _finite_number(unread_at)
    if report["status"] == "ok" and unread_unknown:
        report["status"] = "degraded"
    run = h.get("run")
    if isinstance(run, dict):
        overshoot = run.get("overshoot_s")
        report["run"] = run
        if (report["status"] == "ok" and isinstance(overshoot, (int, float))
                and _finite_number(overshoot)
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
    last_ok = h.get("last_ok_at")
    report["last_ok_at"] = (last_ok if _finite_number(last_ok)
                            and last_ok >= 0 else None)
    notify = h.get("notify") if isinstance(h.get("notify"), dict) else {}
    held = notify.get("held_reasons")
    report["held_reasons"] = (
        held if isinstance(held, dict)
        and all(_is_code(k) and _finite_number(v) and v >= 0
                for k, v in held.items())
        else None)
    # every queue key always present: missing block == malformed == None
    report["oldest_age_s"] = {
        q: (block.get("oldest_age_s")
            if isinstance(block, dict)
            and _finite_number(block.get("oldest_age_s")) else None)
        for q, block in (("notify", h.get("notify")),
                         ("semantic_jobs", h.get("semantic_jobs")),
                         ("extract_qc_jobs", h.get("extract_qc_jobs")))}
    # dedup stamp: unread_at only when that age is what made it stale
    report["evidence_at"] = (binding if report["status"] == "stale"
                             else h["at"])
    return report


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
    if not _finite_number(now):
        raise ValueError("health_now_invalid")
    cfg = load_config() if cfg is None else cfg
    deadline = freshness_deadline(cfg)
    health_path = os.path.join(home, HEALTH_REL)
    state_path = os.path.join(home, STATE_REL)
    status_path = os.path.join(home, STATUS_REL)

    obs = classify_health(health_path, now, deadline)
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
    new_reason = (prior_known and not transition
                  and obs["status"] not in ("ok", "stale")
                  and not set(reasons) <= seen)
    alerted_at = state.get("detected_at", state.get("alerted_at"))
    invalid_alert_at = not _finite_number(alerted_at) \
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
    disk_prev = state.get("disk_low") is True
    disk_low = (obs["disk_low"] if obs["status"] in OVERALL_STATUS
                else disk_prev)
    disk_alert = disk_low and not disk_prev
    state["disk_low"] = disk_low

    report = dict(obs)
    report.update({"deadline_s": obs.get("deadline_s", deadline), "alert": alert,
                   "disk_low": disk_low, "disk_alert": disk_alert,
                   "watched_at": now})
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
        maintenance.atomic_publish_text(
            state_path, json.dumps(state, ensure_ascii=False))
    except OSError:
        report["delivery_error"] = "state_persist_failed"
    try:
        maintenance.atomic_publish_text(
            status_path, json.dumps(report, ensure_ascii=False))
    except OSError:
        report["delivery_error"] = "status_persist_failed"
    return report


def _archive_unknown(state: dict, delivery: dict) -> None:
    """Unresolved history is evidence, but the state file must stay
    bounded: keep the newest UNKNOWN_HISTORY records."""
    held = state.get("unknown_deliveries")
    held = held if isinstance(held, list) else []
    state["unknown_deliveries"] = [*held, dict(delivery)][-UNKNOWN_HISTORY:]


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
        import notify_flush
        target = notify_flush._target(cfg, "run_failed")
        if not target:
            return False
        argv = notify_flush._send_argv(cfg, target)
    except Exception:
        return False                   # transport has not been called
    try:
        notify_flush._send(argv,
                           "[MCS] 監視警報\n" + text,
                           deadline=time.monotonic() + 60)
        return True
    except (notify_flush._SendFailed, notify_flush._SendUsage):
        return False
    except Exception:
        return None                    # includes _SendUncertain


_STATUS_JA = {"degraded": "一部異常", "failed": "停止・失敗",
              "stale": "更新が途絶", "missing": "記録なし", "corrupt": "記録破損"}
_REASON_JA = {
    "run_failed": "収集の実行失敗", "session_expired": "MCSログイン切れ",
    "stage_errors": "処理段階のエラー", "code_changed": "処理中のコード更新",
    "run_deadline_exceeded": "実行時間の超過",
    "ledger_relation_violations": "保存データの整合性違反",
    "backup_not_verified": "バックアップ未確認",
    "collection_incomplete": "収集が不完全", "notification_failed": "通知の送信失敗",
    "notification_deferred": "通知の先送り", "notification_parked": "通知の待機",
    "notification_held": "通知の保留", "notification_pending": "通知の送信待ち",
    "disk_low": "空き容量不足", "card_delivery_stalled": "カード配送の停滞",
    "extract_backlog_stalled": "解析待ちの停滞",
    "semantic_backlog_stalled": "意味解析待ちの停滞",
    "unread_collection_unknown": "未読収集の状況不明",
}


def _jst(ts) -> str:
    if not _finite_number(ts):
        return "不明"
    return time.strftime("%m-%d %H:%M", time.gmtime(ts + 9 * 3600))


def _alert_lines(report: dict) -> list[str]:
    """Staff-readable alert text: Japanese status/reasons and JST times,
    with the raw codes kept on one line for operators to search."""
    lines = []
    if report["alert"] and report["status"] != "ok":
        status = report["status"]
        reasons = report.get("state_reasons")
        lines.append(f"MCS監視: {_STATUS_JA.get(status, status)}（{status}）")
        if reasons:
            lines.append("理由: " + "、".join(
                _REASON_JA.get(r.split(":")[0], r) for r in reasons))
        elif reasons is None:
            lines.append("理由: 不明")
        lines.append(f"最終正常: {_jst(report.get('last_ok_at'))}"
                     f" / 最新記録: {_jst(report.get('health_at'))}（JST）")
        lines.append("コード: " + (",".join(reasons) if reasons else
                                    "none" if reasons is not None else "unknown"))
    if report["disk_alert"] and report["disk_low"]:
        free = report.get("disk_free_mb")
        lines.append(f"空き容量不足: 残り {free} MB" if _finite_number(free)
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
            or (_finite_number(tried) and now - tried < DELIVERY_RETRY_S):
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
        maintenance.atomic_publish_text(state_path, json.dumps(state, ensure_ascii=False))
    except OSError:
        report["delivery_error"] = "state_persist_failed"
        report["delivery"] = {**delivery, "outcome": "not_sent"}
        try:
            maintenance.atomic_publish_text(os.path.join(args.home, STATUS_REL),
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
        maintenance.atomic_publish_text(state_path, json.dumps(state, ensure_ascii=False))
    except OSError:
        report["delivery_error"] = "state_persist_failed"
    try:
        maintenance.atomic_publish_text(os.path.join(args.home, STATUS_REL),
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
    if args.now is not None and not _finite_number(args.now):
        ap.error("now must be a finite number")
    cfg = load_config(args.config) if args.config else load_config()
    try:
        directory = os.path.join(args.home, "data")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(directory, "health_watch.lock"),
                     os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return _watch(args, cfg)
    except BlockingIOError:
        return 0                         # the existing watcher owns this observation
    except OSError:
        return 1                         # unavailable state store is not successful delivery


if __name__ == "__main__":
    sys.exit(main())
