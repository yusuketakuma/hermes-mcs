"""Read bounded, count-only operational evidence without probing services."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

from health_policy import RUN_GRACE_S, freshness_deadline
from safe_io import read_regular_nofollow
from stats_math import finite_number

HEALTH_BYTES = 1024 * 1024
COVERAGE_FRESH_S = 3600
INTERRUPT_REASONS = ("watchdog_exit", "update_preempted", "host_restart", "unknown")


def _number(value):
    return value if finite_number(value) and value >= 0 else None


def _count(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _choice(value, choices):
    return value if isinstance(value, str) and value in choices else "unknown"


def _object(value):
    return value if isinstance(value, dict) else {}


def _coverage(raw, now):
    from extract_llm import EXTRACT_VERSION

    raw = _object(raw)
    at = _number(raw.get("counted_at"))
    eligible, current = _count(raw.get("eligible")), _count(raw.get("current"))
    valid = (at is not None and at <= now and eligible is not None
             and current is not None and current <= eligible
             and _count(raw.get("extract_version")) == EXTRACT_VERSION)
    return {
        "status": ("unknown" if not valid else
                   "stale" if now - at > COVERAGE_FRESH_S else "recorded"),
        "counted_at": at if valid else None,
        "generation": raw["extract_version"] if valid else None,
        "eligible": eligible if valid else None,
        "current": current if valid else None,
        "ratio": current / eligible if valid and eligible else None,
        "scope": "stored_body_eligible_messages",
        "basis": "current_extraction_artifact",
        # Coverage is not the scheduler's admission/claim/retry queue.
    }


def _queue(raw):
    raw = _object(raw)
    count, age = _count(raw.get("pending")), _number(raw.get("oldest_age_s"))
    return {"pending": count, "oldest_age_s": age,
            "status": "unknown" if count is None else "recorded"}


def _backup(raw):
    raw = _object(raw)
    local = _object(raw.get("local"))
    return {
        "offsite_state": _choice(raw.get("state"),
                                 ("disabled", "ok", "degraded", "failed")),
        "local": {
            "state": _choice(local.get("state"),
                             ("missing", "unreadable", "unverified", "verified", "unknown")),
            "newest_age_s": _number(local.get("newest_age_s")),
            "verification_basis": "recorded_file_signature",
        },
    }


def interrupted_runs(db, *, now):
    """Observe prior unfinished runs; finished_at is their detection time, not cause."""
    if _number(now) is None:
        raise ValueError("observation_time_invalid")
    try:
        row = db.execute("SELECT finished_at,error FROM runs WHERE status='crashed' "
                         "ORDER BY run_id DESC LIMIT 1").fetchone()
        marks = ",".join("?" for _ in INTERRUPT_REASONS)
        rows = db.execute(
            f"SELECT CASE WHEN error IN ({marks}) THEN error ELSE 'unknown' END reason, "
            "COUNT(*) FROM runs WHERE status='crashed' AND finished_at>=? "
            "AND finished_at<=? GROUP BY reason", (*INTERRUPT_REASONS, now - 86400, now))
        counts = {reason: count for reason, count in rows}
    except sqlite3.Error:
        return {"status": "unavailable", "reason": "observation_database_unavailable"}
    detected = _number(row[0]) if row else None
    if detected is not None and detected > now:
        detected = None
    return {"status": "recorded", "reason": _choice(row[1], INTERRUPT_REASONS) if row else None,
            "detected_at": detected, "count_24h": sum(counts.values()),
            "by_reason_24h": counts, "observed_at": now,
            "occurred_at": None}


def stored_status(home, cfg, *, now=None):
    """Read one bounded health file; return allowlisted facts, never paths or errors."""
    now = time.time() if now is None else now
    if _number(now) is None:
        raise ValueError("observation_time_invalid")
    try:
        raw = read_regular_nofollow(Path(home) / "data" / "health.json",
                                    HEALTH_BYTES, ValueError)
        if len(raw) > HEALTH_BYTES:
            raise ValueError
        record = json.loads(raw)
    except FileNotFoundError:
        return {"status": "missing", "observed_at": now}
    except OSError:
        return {"status": "unavailable", "observed_at": now}
    except (ValueError, UnicodeError, RecursionError):
        return {"status": "invalid", "observed_at": now}
    record = _object(record)
    at = _number(record.get("at"))
    overall = _choice(record.get("overall"), ("ok", "degraded", "failed"))
    if at is None or at > now + RUN_GRACE_S or overall == "unknown":
        return {"status": "invalid", "observed_at": now}
    unread = _number(record.get("unread_at"))
    binding = min(at, unread) if unread is not None else at
    unread_unknown = "unread_at" in record and unread is None
    status = ("stale" if now - binding > freshness_deadline(cfg) else
              "unknown" if unread_unknown and overall == "ok" else overall)
    coverage = _coverage(record.get("extract_v2_coverage"), now)
    gaps = _object(record.get("coverage_lagging"))
    interrupt = _object(_object(record.get("run")).get("last_interrupt"))
    detected = _number(interrupt.get("detected_at"))
    count_24h = _count(interrupt.get("count_24h"))
    no_interrupt = (interrupt.get("status") == "recorded" and count_24h == 0
                    and "reason" in interrupt and interrupt["reason"] is None
                    and interrupt.get("detected_at", -1) is None)
    return {
        "status": status, "observed_at": now, "recorded_at": at,
        "recorded_overall": overall, "evidence_at": binding,
        "unread_observed_at": unread,
        "collection": _choice(record.get("collection"), ("ok", "incomplete")),
        "coverage_lagging": {"count": _count(gaps.get("count")),
                             "max_lag_s": _number(gaps.get("max_lag_s"))},
        "extraction_coverage": coverage,
        "queues": {key: _queue(record.get(key))
                   for key in ("notify", "semantic_jobs", "extract_qc_jobs")},
        "backup": _backup(record.get("backup")),
        "last_interrupt": {
            "reason": None if no_interrupt else _choice(interrupt.get("reason"),
                                                       INTERRUPT_REASONS),
            "detected_at": detected if detected is not None and detected <= now else None,
            "count_24h": count_24h,
        },
        "detail_command": "python3 mcs/ops/semantic_observe.py --json",
    }


def status_lines(report):
    """Render recorded counters with their scope; unavailable facts stay explicit."""
    labels = {"ok": "正常", "degraded": "要確認", "failed": "失敗",
              "stale": "記録が古い", "missing": "記録なし",
              "invalid": "記録を確認できません", "unavailable": "読取不能",
              "unknown": "未確認"}
    lines = ["運用記録: " + labels.get(report.get("status"), "未確認")]
    if "recorded_at" not in report:
        return lines
    age = max(0, report["observed_at"] - report["recorded_at"])
    lines.append(f"  health記録: {age:.0f}秒前（各集計の観測時刻は別）")
    if report["unread_observed_at"] is None:
        lines.append("  未読収集の観測時刻: 未確認")
    cov = report["extraction_coverage"]
    if cov["status"] == "unknown":
        lines.append("  現行抽出世代の充足: 未確認")
    else:
        suffix = "・集計が古い" if cov["status"] == "stale" else ""
        lines.append(f"  現行抽出世代の充足: {cov['current']}/{cov['eligible']}投稿"
                     f"（{report['observed_at'] - cov['counted_at']:.0f}秒前{suffix}）")
    for key, title in (("notify", "通知"), ("semantic_jobs", "意味チェック"),
                       ("extract_qc_jobs", "抽出品質チェック")):
        count = report["queues"][key]["pending"]
        lines.append(f"  {title}の記録上の待ち件数: " +
                     ("未確認" if count is None else str(count)))
    backup = report["backup"]
    lines.append("  外部バックアップ: " + {
        "disabled": "無効", "ok": "記録上正常", "degraded": "要確認",
        "failed": "失敗"}.get(backup["offsite_state"], "未確認"))
    lines.append("  日次ローカルバックアップ: " + {
        "missing": "記録なし", "unreadable": "読取不能", "unverified": "検証記録不一致",
        "verified": "保存ファイルと既存検証記録が一致"}.get(backup["local"]["state"], "未確認"))
    interrupted = report["last_interrupt"]
    n = interrupted["count_24h"]
    lines.append("  前回未完了の検出（記録時点の24時間）: " +
                 ("未確認" if n is None else f"{n}件"))
    return lines
