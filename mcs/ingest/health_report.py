"""Observe subsystem health from a borrowed connection and previously published reports."""
from __future__ import annotations
import llm_availability

import json
import os
import shutil
import sqlite3
import stat
import time
from contextlib import suppress

from health_policy import HEALTH_REL, STATUS_MAX_BYTES, freshness_deadline
from stats_math import finite_number
import safe_io
from mcs_queries import ELIGIBLE_MESSAGE
from mcs_util import HOME, ATTACH_DIR
from llm_availability import disk_floor_mb
import maintenance

HEALTH_FILE = os.path.join(HOME, HEALTH_REL)
ATTACH_DISK_MARGIN_MB = 2048
DISK_WARN_MARGIN_MB = 2 * ATTACH_DISK_MARGIN_MB   # warn before disk_low
WAL_WARN_BYTES = 256 * 1024 * 1024
SNAPSHOT_VERIFY_WARN_RATIO = 0.6
DB_MAINT_OVERDUE_FACTOR = 2        # two missed daily runs, not one late tick
DB_MAINT_STATE_MAX_BYTES = 65536


def attachment_ocr_health(data: str, cfg: dict | None, now: float) -> dict:
    """Read bounded private OCR evidence without exposing source IDs, paths or text."""
    import attachment_ocr
    import writer_lock
    if not isinstance(cfg, dict):
        return {"state": "unknown", "reason": "attachment_ocr_config_unavailable"}
    settings = cfg.get("attachment_ocr", {})
    if isinstance(settings, dict) and settings.get("enabled", False) is False:
        return {"state": "disabled"}
    if attachment_ocr.config_error(cfg):
        return {"state": "unknown", "reason": "attachment_ocr_config_invalid"}
    path = os.path.join(data, "attachment_ocr_status.json")
    counts = ("complete", "empty", "failed", "unsupported", "deferred", "pending_failed",
              "local_done", "local_deferred")
    writer_fields = ("writer_reason", "writer_stage", "writer_job_stage")
    try:
        value = json.loads(safe_io.read_private_bytes(path, limit=4096))
        if (not isinstance(value, dict) or set(value) - {
                "at", "reason", "queue_status", "paid_recovery_pending", *counts, *writer_fields}
                or not finite_number(value.get("at")) or not 0 <= value["at"] <= now):
            raise ValueError("ocr_status_invalid")
        reason = value.get("reason")
        known = {None, "disabled", "busy", "writer_busy", "writer_blocked", "config_invalid",
                 "database_unavailable", "runtime_unavailable", "local_processing_failed",
                 "snapshot_deferred", "status_unavailable", "memory_deferred"}
        if not isinstance(reason, (str, type(None))) or reason not in known:
            raise ValueError("ocr_status_invalid")
        if any(key in value and (type(value[key]) is not int or value[key] < 0) for key in counts):
            raise ValueError("ocr_status_invalid")
        if reason is None and any(key not in value for key in counts if key not in ("pending_failed", "local_done", "local_deferred")):
            raise ValueError("ocr_status_invalid")
        if "queue_status" in value and value["queue_status"] not in ("known", "partial"):
            raise ValueError("ocr_status_invalid")
        if "paid_recovery_pending" in value and type(value["paid_recovery_pending"]) is not bool:
            raise ValueError("ocr_status_invalid")
        writer_detail = {key: value[key] for key in writer_fields if key in value}
        if writer_detail and (len(writer_detail) != len(writer_fields)
                or any(type(item) is not str for item in writer_detail.values())
                or writer_detail["writer_stage"] not in writer_lock._WRITER_REFUSAL_STAGES.get(
                    writer_detail["writer_reason"], ())
                or writer_detail["writer_job_stage"] not in ("local", "ocr")):
            raise ValueError("ocr_status_invalid")
    except FileNotFoundError:
        return {"state": "unknown", "reason": "attachment_ocr_status_missing"}
    except (OSError, ValueError, TypeError, RecursionError):
        return {"state": "unknown", "reason": "attachment_ocr_status_corrupt"}
    detail = {"at": value["at"], "age_s": round(now - value["at"], 1),
              **{key: value[key] for key in (*counts, "paid_recovery_pending") if key in value},
              **writer_detail}
    if now - value["at"] > freshness_deadline(cfg):
        return {**detail, "state": "unknown", "reason": "attachment_ocr_status_stale"}
    if value.get("failed", 0) or value.get("pending_failed", 0) or reason not in (None, "busy", "writer_busy", "snapshot_deferred", "memory_deferred"):
        return {**detail, "state": "failed", "reason": "attachment_ocr_processing_failed"}
    if value.get("paid_recovery_pending") is True:
        return {**detail, "state": "deferred", "reason": "attachment_ocr_accounting_deferred"}
    if reason == "memory_deferred":
        return {**detail, "state": "deferred", "reason": "attachment_ocr_memory_deferred"}
    if value.get("deferred", 0) or reason is not None:
        return {**detail, "state": "deferred", "reason": "attachment_ocr_processing_deferred"}
    if value.get("queue_status") != "known" or "pending_failed" not in value:
        return {**detail, "state": "unknown", "reason": "attachment_ocr_queue_unverified"}
    if value.get("local_deferred", 0):
        return {**detail, "state": "deferred", "reason": "attachment_ocr_local_inference_deferred"}
    return {**detail, "state": "ok"}

def prev_health() -> dict:
    """The previously published health block ({} when unreadable)."""
    try:
        prev = safe_io.read_json_nofollow(HEALTH_FILE, STATUS_MAX_BYTES)
    except (OSError, ValueError, RecursionError):
        return {}
    return prev if isinstance(prev, dict) else {}


def unread_at(result: dict, status: str, now: float) -> float | None:
    """When unread collection last completed. A --jobs-only deep run or
    a failed tick does not collect unread, so it carries the previous
    value forward — deep runs alone must never keep health 'fresh'
    while the unread check has stopped."""
    if not result.get("jobs_only") \
            and status not in ("failed", "session_expired"):
        return now
    prev = prev_health().get("unread_at")
    return prev if finite_number(prev) else None


def attachment_free_mb(path=None) -> float | None:
    """Free MiB on the volume attachments (and the db) live on;
    None when unmeasurable."""
    try:
        return shutil.disk_usage(ATTACH_DIR if path is None else path).free / (1024 * 1024)
    except OSError:
        return None


def attachment_disk_low(free_mb: float | None) -> bool:
    """Free space under the attachment guard (floor + margin); an
    unmeasurable volume or a disabled floor (0) never reads as low."""
    floor = disk_floor_mb()
    return bool(floor) and free_mb is not None \
        and free_mb < floor + ATTACH_DISK_MARGIN_MB


QUARANTINE_SCAN_MAX = 2048        # directory entries per health read
QUARANTINE_SCAN_S = 0.5


def quarantined_commands(data: str) -> int | None:
    """*.json.invalid files in the two command queues (data/cmd and
    data/cmd_int), each opened no-follow; None (unknown) once the scan
    passes its entry or time bound, or a queue cannot be read."""
    count, seen = 0, 0
    stop = time.monotonic() + QUARANTINE_SCAN_S
    for queue in ("cmd", "cmd_int"):
        try:
            fd = os.open(os.path.join(data, queue),
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    seen += 1
                    if seen > QUARANTINE_SCAN_MAX or time.monotonic() > stop:
                        return None
                    count += entry.name.endswith(".json.invalid")
        except OSError:
            return None
        finally:
            os.close(fd)
    return count


def early_warnings(db, result: dict, free_mb: float | None) -> dict:
    """Fixed warning codes plus counts and byte sizes only — no paths,
    names or message text. Informational: overall and state_reasons keep
    their own rules, so a warning never pages by itself."""
    data = os.path.join(HOME, "data")
    out: dict = {"warnings": []}
    out["cmd_quarantined"] = quarantined_commands(data)
    if out["cmd_quarantined"] is None:
        out["warnings"].append("cmd_quarantined_unknown")
    elif out["cmd_quarantined"]:
        out["warnings"].append("cmd_quarantined")
    wal = None
    with suppress(sqlite3.Error, OSError, TypeError, IndexError):
        main = next(r for r in db.execute("PRAGMA database_list") if r[1] == "main")[2]
        st = os.lstat(main + "-wal")
        wal = st.st_size if stat.S_ISREG(st.st_mode) else None
    out["ledger_wal_bytes"] = wal
    if wal is not None and wal > WAL_WARN_BYTES:
        out["warnings"].append("ledger_wal_large")
    # two published snapshots in a row spending most of their verify
    # budget: the next growth step fails the snapshot (and its gates)
    timing = result.get("snapshot_timing") or {}
    verify, budget = timing.get("verify_s"), timing.get("budget_s")
    prev = prev_health().get("snapshot_verify_slow_runs")
    prev = prev if type(prev) is int and prev >= 0 else 0
    if finite_number(verify) and finite_number(budget) and budget > 0:
        slow = prev + 1 if verify > SNAPSHOT_VERIFY_WARN_RATIO * budget else 0
    else:
        slow = prev                      # no publish this tick: carry forward
    out["snapshot_verify_slow_runs"] = slow
    if slow >= 2:
        out["warnings"].append("snapshot_verify_near_budget")
    floor = disk_floor_mb()
    if floor and free_mb is not None and not attachment_disk_low(free_mb) \
            and free_mb < floor + DISK_WARN_MARGIN_MB:
        out["warnings"].append("disk_space_low_soon")
    try:
        backups = maintenance.backup_bytes_by_class()
    except Exception:
        backups = None
    out["manual_backup_bytes"] = None if backups is None else backups["manual"]
    out["backup_bytes_by_class"] = backups
    out.update(ledger_capacity(db))
    deferred = db_maintenance_ages(data, time.time())
    out.update(deferred)
    daily = deferred["db_maintenance_daily_age_s"]
    if deferred["db_maintenance_state"] == "unreadable":
        out["warnings"].append("db_maintenance_unknown")
    elif daily is not None and daily > DB_MAINT_OVERDUE_FACTOR * maintenance.DB_MAINT_INTERVAL_S:
        out["warnings"].append("db_maintenance_overdue")
    return out


def ledger_capacity(db) -> dict:
    """SQLite page totals from the header (O(1)); free pages are what a
    VACUUM could return. File allocation size is not this number."""
    out = {"ledger_bytes": None, "ledger_free_bytes": None}
    with suppress(sqlite3.Error, TypeError, IndexError):
        pages, free, size = (db.execute(f"PRAGMA {name}").fetchone()[0]
                             for name in ("page_count", "freelist_count", "page_size"))
        if all(type(v) is int and v >= 0 for v in (pages, free, size)) and free <= pages:
            out.update(ledger_bytes=pages * size, ledger_free_bytes=free * size)
    return out


def db_maintenance_ages(data: str, now: float) -> dict:
    """Seconds since the last completed daily and weekly maintenance. The
    record only moves on success, so a growing age means deferral (a
    deadline or busy reader) that run errors alone would hide."""
    out = {"db_maintenance_state": "missing", "db_maintenance_daily_age_s": None,
           "db_maintenance_fts_age_s": None, "db_checkpoint_pending": None}
    try:
        value = safe_io.read_json_nofollow(os.path.join(data, "db_maintenance.json"),
                                           DB_MAINT_STATE_MAX_BYTES)
    except FileNotFoundError:
        return out
    except (OSError, ValueError, RecursionError):
        out["db_maintenance_state"] = "unreadable"
        return out
    if not isinstance(value, dict):
        out["db_maintenance_state"] = "unreadable"
        return out
    out["db_maintenance_state"] = "present"
    for key, field in (("db_maintenance_daily_age_s", "daily_at"),
                       ("db_maintenance_fts_age_s", "fts_at")):
        at = value.get(field)
        if finite_number(at) and type(at) is not bool and 0 <= at <= now:
            out[key] = round(now - at)
    # The writer drops the flag once a checkpoint succeeds, so absence is
    # False; any value other than a real bool is unknown, never False.
    flag = value.get("checkpoint_pending", False)
    out["db_checkpoint_pending"] = flag if type(flag) is bool else None
    return out


def collection(result: dict) -> dict:
    """Collection verdict: a stalled history_head or an incomplete
    unread fetch is 'incomplete'; coverage lag alone is information.
    --jobs-only runs collect nothing, so they carry the last full
    tick's verdict forward instead of resetting it to 'ok'."""
    keys = ("collection", "incomplete_projects", "coverage_lagging",
            "coverage_stalled")
    if result.get("jobs_only"):
        prev = prev_health()
        if prev.get("collection") in ("ok", "incomplete"):
            return {k: prev.get(k) for k in keys}
    gaps = result.get("coverage_gaps") or []
    stalled = [g.get("pid") for g in result.get("coverage_stalled") or []]
    return dict(zip(keys, (
        "incomplete" if result.get("incomplete") or stalled else "ok",
        result.get("incomplete") or [],
        {"count": len(gaps),
         "max_lag_s": max((g.get("lag_s") or 0 for g in gaps), default=0)},
        stalled)))


# identity marker: a run that crashed before _config() has no backup evidence
CONFIG_NOT_LOADED: dict = {}


def backup_health(cfg: dict, now: float) -> dict:
    """Read only configured backup evidence; missing proof is never success."""
    from backup_evidence import BackupError, load_policy, status
    from backup_config import validate_backup_config

    setting = cfg.get("backup", {"enabled": False})
    if validate_backup_config(setting):
        return {"state": "invalid", "reasons": ["backup_config_invalid"]}
    if not setting["enabled"]:
        return {"state": "disabled", "reasons": []}
    try:
        policy, scheduled = load_policy(setting["policy"])
        assert policy is not None
        recorded = status(os.path.join(HOME, "data"), policy)
    except (BackupError, OSError, ValueError, TypeError, sqlite3.Error, RecursionError):
        return {"state": "failed", "reasons": ["backup_io_or_policy_failed"]}
    reasons = []
    if not scheduled:
        reasons.append("backup_schedule_disabled")
    if recorded["last_attempt_failed"]:
        reasons.append("backup_failed")
    source = recorded["source_last_successful_run"]
    if source is None:
        reasons.append("backup_source_unknown")
    elif not 0 <= now - source <= policy.max_rpo_seconds:
        reasons.append("backup_rpo_exceeded")
    for field, interval in (
            ("last_verify_at", setting["verify_interval_s"]),
            ("last_drill_at", setting["drill_interval_s"])):
        at = recorded[field]
        if at is None:
            reasons.append("backup_" + field.removeprefix("last_") + "_unknown")
        elif not 0 <= now - at <= interval:
            reasons.append("backup_" + field.removeprefix("last_") + "_stale")
    return {"state": "degraded" if reasons else "ok", "reasons": reasons,
            # backup_failed stays the reason; the known fixed code says why
            "failure_code": recorded["last_failure_code"],
            **{key: recorded[key] for key in (
                "last_offsite_at", "last_verify_at", "last_drill_at",
                "last_restore_at", "source_last_successful_run", "max_rpo_seconds")}}


CARD_STALL_S = 1800        # a queued render this old on the live transport
BACKLOG_STALL_S = 6 * 3600  # untouched due work AND no progress this long
DUE_SLACK_S = 900           # next_try this close counts as due (defer cycles)
LOCAL_LLM_DOWN_S = 3600     # llm_down_for_s at or past this, with due work, alerts
COVERAGE_COUNT_S = 3600     # full extraction-coverage COUNTs at most this often


_JEV_BLOCK_KINDS = ("payment_required", "no_api_key", "budget_exceeded",
                    "auth_error")


def jev_block_reason(db) -> str | None:
    """Why TypeSafe Jev is refusing work, from the newest assessment
    record: the drainer writes one per status change, so the latest row
    is the current state. None when the last assessment went through."""
    row = db.execute(
        "SELECT json_extract(meta,'$.error_kind') FROM artifacts "
        "WHERE kind='semantic_assess' AND json_valid(meta) "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    kind = row[0] if row else None
    return kind if kind in _JEV_BLOCK_KINDS else None


def cloud_stop_reason(db, cfg) -> str | None:
    """Observe a configured provider's durable stop without probing it or reading source bodies."""
    from types import SimpleNamespace
    import cloud_policy
    # an unreadable config ({}) keeps the route off (cloud_inference.resolve_cloud_settings)
    settings, errors = cloud_policy.resolve_settings(cfg) if cfg else cloud_policy.cloud_config({})
    if (errors or settings["mode"] not in ("on", "shadow")
            or not settings["routes"] or settings["project_ids"] is not None
            and not set(settings["project_ids"]) - set(settings["exclude_project_ids"])):
        return None
    key_present = bool(cloud_policy.provider_key(settings["provider"]))
    reason = cloud_policy.global_block_reason(SimpleNamespace(db=db), model=settings["model"])
    if reason == "paused":
        return None
    if not key_present:
        return "no_api_key"
    if reason is None:
        return None
    return reason if reason in ("auth_error", "payment_required", "pricing_unverified") else "circuit_open"


def carried_since(key: str, active: bool, now: float) -> float | None:
    """Start of a condition observed on every health write since: the
    previous health block's mark while it held, else now; None when the
    condition is absent (the episode ends)."""
    if not active:
        return None
    marks = prev_health().get("stall_marks")
    at = marks.get(key) if isinstance(marks, dict) else None
    return at if finite_number(at) and 0 <= at <= now else now


def backlog_stalls(db, cfg, now: float, *, marks: dict | None = None) -> list:
    """Degraded reasons for work that silently stopped moving. Each
    check is gated on its feature being enabled and needs BOTH old
    waiting work and no completion inside the window, so a large but
    draining backlog (or an idle system) never trips it.
    - card_delivery_stalled: a queued render on the active interactive
      transport not picked up for CARD_STALL_S (worker down/denied).
    - local_llm_down: local-model work (semantic jobs or pending legacy
      extraction) is due while the LLM lane's own health record says the
      local LLM has been down for LOCAL_LLM_DOWN_S (deferred jobs never
      fail, so nothing else would say so).
    - semantic_backlog_stalled: semantic mode on, no semantic job done
      for BACKLOG_STALL_S while due jobs existed throughout that window
      (tracked across health writes, independent of updated_at: a
      drainer that only defers keeps refreshing updated_at), or a due
      pending job untouched for BACKLOG_STALL_S.
    ``marks`` receives the episode start times carried in health.json.
    - extract_backlog_stalled: legacy extraction admitting (fact_source
      legacy), an eligible message without a v4 result stored
      BACKLOG_STALL_S ago with no extract_llm artifact at all, and no
      extract_llm artifact written since. Any attempt (errored, poison,
      older body) counts as started, so permanent failures surfaced
      elsewhere never pin this on."""
    import notify_scope
    import notify_spool
    import semantic_policy
    from mcs_queries import current_v4_id
    reasons = []
    cloud_reason = cloud_stop_reason(db, cfg)
    if cloud_reason:
        reasons.append(f"cloud_llm_{cloud_reason}")
    if notify_scope.interactive_enabled(cfg):
        oldest = db.execute(
            "SELECT MIN(created_at) FROM notification_renders "
            "WHERE state='queued' AND transport=?",
            (notify_scope.active_transport(cfg),)).fetchone()[0]
        progress = db.execute(
            "SELECT MAX(updated_at) FROM notification_renders "
            "WHERE state='delivered' AND transport=?",
            (notify_scope.active_transport(cfg),)).fetchone()[0]
        part_progress = db.execute(
            "SELECT MAX(p.updated_at) FROM notification_render_parts p "
            "JOIN notification_renders r ON r.delivery_id=p.delivery_id "
            "WHERE p.state='delivered' AND r.transport=?",
            (notify_scope.active_transport(cfg),)).fetchone()[0]
        latest_progress = max((v for v in (progress, part_progress)
                               if v is not None and v <= now), default=None)
        if (oldest is not None and now - oldest > CARD_STALL_S
                and (latest_progress is None or now - latest_progress > CARD_STALL_S)):
            reasons.append("card_delivery_stalled")
            import card_worker_status
            worker = card_worker_status.diagnose(notify_spool.data_root(db), cfg)
            if worker["state"] in ("unknown", "disabled"):
                reasons.append(worker["reason"])
        if db.execute("SELECT 1 FROM notification_restore_holds "
                      "WHERE released_at IS NULL LIMIT 1").fetchone():
            reasons.append("restore_delivery_held")
    marks = {} if marks is None else marks
    window = now - BACKLOG_STALL_S
    scfg, errors = semantic_policy.runtime_config(cfg)

    def llm_due() -> bool:
        """Work that needs the local model is waiting: due semantic jobs
        (semantic mode on) or legacy extraction pending (fact_source not
        canonical — the default semantic-off install has no fetch_jobs
        for it). extract_qc is Jev-only and never counts. Evaluated
        lazily: the legacy probe walks eligible messages."""
        # due "now or within the slack": a deferring drainer keeps pushing
        # next_try a minute ahead, which must still read as waiting work
        if not errors and scfg["mode"] != "off" and db.execute(
                "SELECT 1 FROM fetch_jobs WHERE kind='semantic' "
                "AND state='pending' AND next_try<=? LIMIT 1",
                (now + DUE_SLACK_S,)).fetchone() is not None:
            return True
        if errors or scfg["fact_source"] == "canonical":
            return False
        # extract_llm's own pending predicate, minus exhausted rows
        import extract_llm
        return db.execute(
            f"SELECT 1 FROM messages m WHERE {extract_llm.pending_pred(db)} "
            "AND COALESCE((SELECT MAX(COALESCE(json_extract(e.meta,"
            f"'$.attempts'),0)) {extract_llm._ERROR_ROWS_SQL}), 0) < 5 "
            "LIMIT 1", (extract_llm.KIND, extract_llm.EXTRACT_VERSION)
        ).fetchone() is not None

    # the LLM lane's own health record (llm_health.json): None means
    # unknown or healthy and never alerts
    try:
        seconds = llm_availability.llm_down_for_s(db, now=now)
    except Exception:
        seconds = None
    marks["llm_down_since"] = None   # kept in health.json for older readers
    if (finite_number(seconds) and seconds >= LOCAL_LLM_DOWN_S
            and llm_due()):
        reasons.append("local_llm_down")
    if errors:
        return reasons   # reported as stage errors; admission is closed
    blocked = jev_block_reason(db) if scfg["mode"] != "off" else None
    if blocked:
        # the cause is known and external — name it. A live drainer keeps
        # touching due jobs while it waits, so the generic stall below
        # still only fires when the drainer itself stopped.
        reasons.append(f"semantic_jev_{blocked}")
    sem_on = scfg["mode"] != "off"
    due = sem_on and db.execute(
        "SELECT 1 FROM fetch_jobs WHERE kind='semantic' "
        "AND state='pending' AND next_try<=? LIMIT 1",
        (now + DUE_SLACK_S,)).fetchone() is not None
    due_since = carried_since("semantic_due_since", due, now)
    marks["semantic_due_since"] = due_since
    if due and not db.execute(
            "SELECT 1 FROM fetch_jobs WHERE kind='semantic' "
            "AND state='done' AND updated_at>=? LIMIT 1",
            (window,)).fetchone() and (
            (due_since is not None and due_since < window)
            or db.execute(
                "SELECT 1 FROM fetch_jobs WHERE kind='semantic' "
                "AND state='pending' AND next_try<=? AND updated_at<? LIMIT 1",
                (now, window)).fetchone()):
        reasons.append("semantic_backlog_stalled")
    if scfg["fact_source"] != "canonical":
        newest = db.execute(
            "SELECT created_at FROM artifacts WHERE kind='extract_llm' "
            "ORDER BY artifact_id DESC LIMIT 1").fetchone()
        if (newest is None or (newest[0] or 0) < window) and db.execute(
                f"SELECT 1 FROM messages m WHERE {ELIGIBLE_MESSAGE} "
                "AND m.first_seen < ? AND NOT EXISTS(SELECT 1 FROM artifacts a "
                "  WHERE a.kind='extract_llm' AND a.message_id=m.message_id) "
                f"AND {current_v4_id()} IS NULL LIMIT 1",
                (window,)).fetchone():
            reasons.append("extract_backlog_stalled")
    return reasons


HEALTH_OBSERVATION_S = 0.25
NOTIFICATION_OBSERVATION_S = 2.0
HEALTH_SQL_BUDGET_S = 30.0  # one shared window for every SQL read build_health owns
HEALTH_OBSERVATION_ITEMS = 10002  # 10,000 rows plus the two boundary checks
HEALTH_OBSERVATION_ROW_BYTES = 2 * 1024 * 1024


def bounded_health_read(db, read, seconds=None, *, observation=None):
    """Bound additive reads on the tick-owned connection; preserve its transaction."""
    seconds = HEALTH_OBSERVATION_S if seconds is None else seconds  # read at call time
    started = time.monotonic()
    deadline = started + seconds

    def sampled(at, stop=None):
        if observation is not None:
            observation.update(elapsed_s=round(max(0.0, at - started), 6),
                               remaining_s=round(max(0.0, deadline - at), 6))
            if stop is not None:
                observation["stop"] = stop

    if observation is not None:
        observation.update(phase="connection_setup", budget_s=max(0.0, seconds),
                           stop="read_unavailable")
    # a depleted window starts no read at all — the callback, the SQL and
    # the savepoint all stay untouched so the tick can finish on time
    at = time.monotonic()
    sampled(at)
    if at >= deadline:
        sampled(at, "budget_exhausted")
        raise RuntimeError("observation_limit")
    items = 0

    def checkpoint():
        nonlocal items
        items += 1
        at = time.monotonic()
        if at >= deadline or items > HEALTH_OBSERVATION_ITEMS:
            sampled(at, "time_limit" if at >= deadline else "item_limit")
            raise RuntimeError("observation_limit")

    def progress():
        at = time.monotonic()
        expired = at >= deadline
        if expired:
            sampled(at, "time_limit")
        return int(expired)

    # This connection has no long-lived progress handler. Backup and detailed
    # observer handlers belong to their own connections, not the tick db.
    previous_limit = db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    previous_wait = db.execute("PRAGMA busy_timeout").fetchone()[0]
    savepoint = False
    try:
        db.execute(f"PRAGMA busy_timeout={min(previous_wait, int(seconds * 1000))}")
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,
                    min(previous_limit, HEALTH_OBSERVATION_ROW_BYTES))
        db.execute("SAVEPOINT mcs_health_observation")
        savepoint = True
        db.set_progress_handler(progress, 1000)
        value = read(checkpoint)
        at = time.monotonic()
        sampled(at, "time_limit" if at >= deadline else "complete")
        if at >= deadline:
            raise RuntimeError("observation_limit")
        return value
    except Exception as exc:
        if observation is not None:
            try:
                # SQLite exposes stable numeric codes; never copy exception text.
                code = getattr(exc, "sqlite_errorcode", None)
                code = code & 255 if type(code) is int else None
                stop = {sqlite3.SQLITE_TOOBIG: "row_size_limit",
                        sqlite3.SQLITE_BUSY: "storage_busy",
                        sqlite3.SQLITE_LOCKED: "storage_busy"}.get(code)
                sampled(time.monotonic(), stop)
            except Exception:
                # Optional diagnostics must not replace the read failure or cleanup.
                pass
        raise
    finally:
        try:
            db.set_progress_handler(None, 0)
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous_limit)
            try:
                if savepoint:
                    db.execute("RELEASE mcs_health_observation")
            finally:
                # A failed RELEASE must not leave the tick connection's short wait behind.
                db.execute(f"PRAGMA busy_timeout={previous_wait}")
        except Exception:
            if observation is not None:
                observation.update(phase="connection_cleanup", stop="read_unavailable")
            raise


def collection_diagnostics_health(db, errors, now, seconds=None):
    """Observe collection details within the existing reader budget without changing the verdict."""
    import collection_diagnostics as diagnostics
    try:
        incomplete = bounded_health_read(
            db, lambda checkpoint: diagnostics.incomplete_counts(db, now, checkpoint),
            seconds=HEALTH_OBSERVATION_S if seconds is None else min(seconds, HEALTH_OBSERVATION_S))
    except (sqlite3.Error, RuntimeError, ValueError, TypeError):
        incomplete = diagnostics.unknown_incomplete()
    return {"observed_at": now, "errors": diagnostics.error_counts(errors), "incomplete": incomplete}


def pending_health(db, now, seconds=None):
    """Use the same streamed population for counts and reasons, or keep the old count."""
    import notify_scope
    try:
        breakdown = bounded_health_read(
            db, lambda check: notify_scope.pending_breakdown(db, now, checkpoint=check),
            seconds=None if seconds is None else min(seconds, HEALTH_OBSERVATION_S))
    except Exception:
        # Optional diagnostics must not hide a real count failure: a real
        # SQLite error still propagates through the existing health path;
        # only a bound stop reports an honest unknown.
        try:
            row = bounded_health_read(
                db, lambda _check: db.execute(
                    "SELECT COUNT(*) c,MIN(created_at) o FROM notify_outbox WHERE "
                    + notify_scope.OUTBOX_PENDING_WHERE, (now,)).fetchone(),
                seconds=seconds)
        except Exception as exc:
            if _observation_failure(exc) != "observation_limit":
                raise
            return {"pending": None, "oldest_age_s": None,
                    "pending_by_reason": {"status": "unavailable", "observed_at": now,
                                          "reason": "observation_limit"}}
        return {"pending": row["c"],
                "oldest_age_s": round(now - row["o"], 1) if row["o"] else 0,
                "pending_by_reason": {"status": "unavailable", "observed_at": now,
                                      "reason": "observation_unavailable"}}
    return {"pending": breakdown["total"],
            "oldest_age_s": (round(breakdown["oldest_age_s"], 1)
                             if breakdown["oldest_age_s"] is not None else
                             0 if breakdown["total"] == 0 else None),
            "pending_by_reason": {**breakdown, "status": "recorded", "observed_at": now}}


PATIENT_CONTEXT_S = 2.0     # one bounded count per COVERAGE_COUNT_S, like coverage
_PATIENT_CONTEXT_COUNTS = ("published", "active_patients", "extraction_complete",
                           "focus_eligible", "focus_targets")


def _observation_failure(exc) -> str:
    """A read stopped by its bound is 'observation_limit'; any other failure is unavailable."""
    limited = str(exc) == "observation_limit" or (
        isinstance(exc, sqlite3.OperationalError) and "interrupt" in str(exc))
    return "observation_limit" if limited else "observation_unavailable"


def patient_context_health(db, checked_cfg, now, source, seconds=None) -> dict:
    """Counts only, never patients: context refresh lane outcomes, extraction-complete
    and patient-focus counts, stored published contexts. Counts are reused for
    COVERAGE_COUNT_S on the same db and extraction generation; publication is
    always read from the current checked config (None when unchecked). A read
    past its bound is 'unknown', never zero."""
    import extract_llm
    import patient_context_refresh
    publication = patient_context_refresh.options(checked_cfg)["publication"] if checked_cfg is not None else None
    key = {"source": source, "extract_version": extract_llm.EXTRACT_VERSION}
    prev = prev_health().get("patient_context")
    if (isinstance(prev, dict) and prev.get("status") == "recorded"
            and all(prev.get(k) == v for k, v in key.items()) and finite_number(prev.get("observed_at"))
            and 0 <= now - prev["observed_at"] < COVERAGE_COUNT_S
            and all(type(prev.get(k)) is int and prev[k] >= 0 for k in _PATIENT_CONTEXT_COUNTS)):
        return {**prev, "publication": publication}
    try:
        counts = bounded_health_read(db, lambda _check: {
            **patient_context_refresh.lane_metrics(db), **extract_llm.patient_progress_counts(db)},
            seconds=min(PATIENT_CONTEXT_S, seconds) if seconds is not None else PATIENT_CONTEXT_S)
    except Exception as exc:
        return {"status": "unknown", "observed_at": now, "reason": _observation_failure(exc),
                "publication": publication}
    return {**counts, "publication": publication, "status": "recorded",
            "observed_at": now, **key}


def part_holds_health(db, now, seconds=None) -> dict:
    """Held/unknown notification parts by fixed reason and retained specs (counts only)."""
    import notify_recovery
    try:
        report = bounded_health_read(db, lambda _check: notify_recovery.part_hold_report(db),
                                     seconds=NOTIFICATION_OBSERVATION_S if seconds is None
                                     else min(seconds, NOTIFICATION_OBSERVATION_S))
    except Exception as exc:
        return {"status": "unknown", "observed_at": now, "reason": _observation_failure(exc)}
    return {**report, "status": "recorded", "observed_at": now}


def incomplete_parts_health(db, now, cfg=None, seconds=None) -> dict:
    """Observe unfinished notification counts within the existing reader budget."""
    import notify_scope
    try:
        report = bounded_health_read(
            db, lambda _check: notify_scope.incomplete_parts_health(db, now, cfg),
            seconds=NOTIFICATION_OBSERVATION_S if seconds is None
            else min(seconds, NOTIFICATION_OBSERVATION_S))
    except Exception as exc:
        return {"total": None, "cards": None,
                "by_state": dict.fromkeys(("pending", "unknown", "not_sent", "held")),
                "by_plan": dict.fromkeys(("current", "superseded")),
                "oldest_age_s": None, "observed_at": now,
                "status": "unknown", "reason": _observation_failure(exc)}
    return {**report, "status": "recorded"}


def build_health(db, result: dict, status: str,
            run_id: int | None = None, *, cfg: dict | None = None,
            deadline: float | None = None) -> dict:
    """Per-subsystem machine-readable state: collection completeness,
    notification completion, extract/QC backlog lag, and current
    extraction-generation coverage. Observations expose only counts,
    fixed reasons and timestamps. Notification freshness checks may read
    bounded source rows; no source body is included in the report.
    `run_id` binds this evidence to the source run that produced it.
    `deadline` (monotonic, e.g. the watchdog's hard deadline) bounds every
    observation together with the shared pool — past it each section must
    report honest unknown, never keep reading while the process is killed."""
    import extract_llm
    import notify_scope
    from mcs_queries import current_extract_pred, json_or_null
    now = time.time()
    # Every SQL read this build owns shares one window; a slow table spends
    # the pool and later sections report honest 'unknown' instead of
    # stalling the tick past the watchdog's patience. The caller's hard
    # deadline caps the same window — a read started with ten seconds left
    # never claims a fresh thirty.
    observation_end = time.monotonic() + HEALTH_SQL_BUDGET_S
    if deadline is not None:
        observation_end = min(observation_end, deadline)

    def remaining():
        return max(0.0, observation_end - time.monotonic())

    notify_res = result.get("notify") or {}
    notify_state = ("incomplete"
                    if notify_res.get("failed") or notify_res.get("skipped")
                    else "parked" if notify_res.get("parked") else "ok")
    # a signal digest scheduled interval_h ahead and never attempted is
    # waiting by design, not pending delivery — everything else counts
    # from its creation, including retries with a future next_try
    card_observation = {}
    try:
        cards_health = bounded_health_read(
            db, lambda _check: notify_scope.health_cards(db, cfg, observation=card_observation),
            seconds=min(NOTIFICATION_OBSERVATION_S, remaining()),
            observation=card_observation)
    except Exception as exc:
        cards_health = dict.fromkeys((
            "pending", "delivered", "update_failed", "delivery_unknown", "message_deleted",
            "revoked", "oldest_pending_age_s", "layout_inventory", "unknown_parts",
            "unknown_part_resolution", "renders_queued", "renders_unknown", "attempts_unsettled",
            "retired_unsettled", "retired_items", "oldest_unsettled_age_s", "unsettled",
            "last_delivered_at"))
        cards_health.update(status="unknown", observed_at=now, reason=_observation_failure(exc),
                            completion={"status": "unknown", "observed_at": now,
                                        "reason": _observation_failure(exc)})
        if card_observation.get("stop") == "complete":
            card_observation["stop"] = "read_unavailable"
    cards_health["observation"] = card_observation
    cards_unknown = cards_health.get("status") == "unknown"
    incomplete_parts = incomplete_parts_health(db, now, cfg, seconds=min(NOTIFICATION_OBSERVATION_S, remaining()))
    parts_incomplete = incomplete_parts['status'] == 'unknown' or bool(incomplete_parts['total'])
    part_holds = part_holds_health(db, now, seconds=min(NOTIFICATION_OBSERVATION_S, remaining()))
    outbox = pending_health(db, now, seconds=remaining())
    # an unmeasurable pending count can never read as delivered work
    pending_unknown = outbox["pending"] is None
    if pending_unknown:
        notify_state = "incomplete"
    retired = notify_scope.retired_transports(cfg)
    marks = ",".join("?" * len(retired))
    # Preserve retired receipts while excluding only events whose sealed
    # batches all belong to retired transports from live held work.
    on_retired = (
        "o.route='interactive' AND EXISTS(SELECT 1 FROM "
        "notification_intent_batches b WHERE b.event_id=o.event_id "
        f"AND b.transport IN ({marks})) AND NOT EXISTS(SELECT 1 FROM "
        "notification_intent_batches b WHERE b.event_id=o.event_id "
        f"AND b.transport NOT IN ({marks}))") if retired else "0"
    try:
        _held_rows = bounded_health_read(db, lambda _check: db.execute(f"""
          SELECT CASE
            WHEN code IN ('send_outcome_unknown','restore_text_unverified',
                          'payload_or_progress_invalid','delivery_changed',
                          'deferred_after_partial','policy_changed','stale_after_partial',
                          'send_usage','pre_send_failure','internal_failure',
                          'payload_invalid','resend_exhausted','dispatch_failed',
                          'restore_hold','restore_unlinked')
              THEN code
            WHEN code IS NULL THEN 'not_recorded'
            ELSE 'unknown'
          END AS reason, retired, COUNT(*) AS count
          FROM (
            SELECT json_extract({json_or_null('o.progress')},'$.hold_reason') AS code,
                   ({on_retired}) AS retired
            FROM notify_outbox o WHERE state='failed' AND next_try IS NULL
          ) GROUP BY reason, retired ORDER BY reason
        """, retired * 2).fetchall(), seconds=remaining())
    except Exception as exc:
        held = None
        retired_held = None
        held_reasons = {"status": "unknown", "observed_at": now,
                        "reason": _observation_failure(exc)}
    else:
        held_reasons = {}
        retired_held = 0
        for row in _held_rows:
            if row["retired"]:
                retired_held += row["count"]
            else:
                held_reasons[row["reason"]] = row["count"]
        held = sum(held_reasons.values())
    # an unmeasurable held queue can never read as delivered work
    if held is None:
        notify_state = "incomplete"
    # held sends keep their posts' attachments past retention; old ones
    # are counted so files kept for them never sit on disk unnoticed
    held_pins = {"count": 0, "bytes": 0}
    if held:
        notify_state = "incomplete"
        try:
            held_pins = bounded_health_read(
                db, lambda _check: maintenance.held_pinned_attachments(db, now),
                seconds=remaining())
        except Exception:
            held_pins = {"count": None, "bytes": None}
    try:
        _pending_rows = bounded_health_read(db, lambda _check: (
            db.execute(
                "SELECT COUNT(*) c, MIN(created_at) o FROM fetch_jobs "
                "WHERE kind='semantic' AND state='pending'").fetchone(),
            db.execute(
                "SELECT COUNT(*) c, MIN(created_at) o FROM fetch_jobs "
                "WHERE kind='extract_qc' AND state='pending'").fetchone()),
            seconds=remaining())
    except Exception:
        _pending_rows = None
    source = ledger_key(db)
    coverage = cached_coverage(extract_llm.EXTRACT_VERSION, now, source)
    if coverage is None:
        try:
            # join from the rare artifact side: an EXISTS per eligible
            # message scans the whole table on any data shape
            poison, total, current = bounded_health_read(db, lambda _check: (
                db.execute(
                    f"SELECT COUNT(DISTINCT bad.message_id) FROM artifacts bad "
                    "JOIN messages m ON m.message_id=bad.message_id "
                    "WHERE bad.kind='extract_llm' AND NOT json_valid(bad.meta) "
                    f"AND {ELIGIBLE_MESSAGE}").fetchone()[0],
                db.execute(
                    f"SELECT COUNT(*) FROM messages m WHERE {ELIGIBLE_MESSAGE}"
                ).fetchone()[0],
                db.execute(f"""
                  SELECT COUNT(DISTINCT m.message_id) FROM artifacts a
                    JOIN messages m ON m.message_id=a.message_id
                    WHERE a.kind='extract_llm' AND {ELIGIBLE_MESSAGE}
                      {current_extract_pred('a', 'm')}
                      AND json_extract({json_or_null('a.meta')},'$.extract_version')=?
                """, (extract_llm.EXTRACT_VERSION,)).fetchone()[0]),
                seconds=remaining())
        except Exception as exc:
            coverage = {
                "extract_version": extract_llm.EXTRACT_VERSION,
                "current": None, "eligible": None, "poison_gated": None,
                "ratio": None, "counted_at": None, "source": source,
                "status": "unknown", "reason": _observation_failure(exc)}
        else:
            coverage = {
                "extract_version": extract_llm.EXTRACT_VERSION,
                "current": current, "eligible": total,
                "poison_gated": poison,
                "ratio": round(current / total, 4) if total else None,
                "counted_at": now, "source": source, "status": "recorded"}
    coll = collection(result)
    try:
        import semantic_intake_hold
        unstored_state = 'present' if bounded_health_read(
            db, lambda _check: semantic_intake_hold.unstored_epoch(db),
            seconds=remaining()) else 'none_observed'
    except Exception:
        unstored_state = 'unverified'
    previous_intake = prev_health()
    diagnostic_unpersisted = (any(
        isinstance(error, str) and ': semantic_intake_diagnostic_unpersisted' in error
        for error in result.get('errors', ()))
        or previous_intake.get('semantic_intake_diagnostic_unpersisted') is True
        and previous_intake.get('semantic_intake_source') == source)
    if diagnostic_unpersisted:
        unstored_state = 'unverified'
    if unstored_state != 'none_observed':
        coll['collection'] = 'incomplete'
    free_mb = attachment_free_mb()
    if parts_incomplete or cards_unknown:
        notify_state = "incomplete"
    # Itemized attention separates synthetic job and delivery populations;
    # an aggregate status alone cannot identify which saved work needs review.
    # Three separate signals: fresh failures (24h), due-but-never-
    # picked rows (updated_at==created_at — attempts alone cannot tell
    # starvation from healthy rotation jobs like reconcile, which
    # progress by payload page with attempts=0), and old unsettled
    # deliveries. Informational only — 'overall' keeps its own rules.
    try:
        _failed_rows, _unstarted_rows = bounded_health_read(db, lambda _check: (
            db.execute(
                "SELECT kind,COUNT(*) c,MAX(updated_at) u FROM fetch_jobs "
                "WHERE state='failed' AND updated_at>=? GROUP BY kind",
                (now - 86400,)).fetchall(),
            db.execute(
                "SELECT kind,COUNT(*) c,MIN(created_at) o "
                "FROM fetch_jobs WHERE state='pending' "
                # extract_claim rows are leases, not work — an expired
                # one is a released claim awaiting the daily sweep
                "AND kind!='extract_claim' "
                "AND COALESCE(attempts,0)=0 AND updated_at<=created_at "
                "AND next_try<=? GROUP BY kind", (now,)).fetchall()),
            seconds=remaining())
    except Exception as exc:
        attention = {"status": "unknown", "observed_at": now,
                     "reason": _observation_failure(exc)}
    else:
        attention = {
            "failed_jobs_24h": {r["kind"]: r["c"] for r in _failed_rows},
            "failed_jobs_latest_at": max(
                (r["u"] for r in _failed_rows if r["u"] is not None),
                default=None),
            "pending_unstarted": {
                r["kind"]: {"count": r["c"],
                            "oldest_age_s": round(now - r["o"], 1)}
                for r in _unstarted_rows},
            "delivery_unsettled": {
                "attempts": cards_health["attempts_unsettled"],
                "oldest_age_s": cards_health["oldest_unsettled_age_s"]},
        }
    checked = isinstance(cfg, dict) and cfg is not CONFIG_NOT_LOADED
    # an unchecked run (no config) carries the episode marks forward
    prev_marks = prev_health().get("stall_marks") if not checked else None
    stall_marks: dict = dict(prev_marks) if isinstance(prev_marks, dict) else {}
    try:
        stalls = (bounded_health_read(
                      db, lambda _check: backlog_stalls(db, cfg, now, marks=stall_marks),
                      seconds=remaining())
                  if checked else [])
    except Exception as e:
        # a plain reason code: health_watch validates codes as snake_case
        # and would otherwise drop every reason in the list
        stalls = ["stall_check_failed"]
        result["errors"] = list(result.get("errors") or []) \
            + [f"stall_check_failed: {type(e).__name__}"]
    errors = result.get("errors") or []
    # A mid-tick mcs/ change is a deploy guard, not a fault: skipped
    # stages rerun next tick on the new code. The run row keeps
    # 'partial' + code_changed for audit, but it must not degrade
    # health or fire a monitoring alert by itself.
    from collection_diagnostics import classify
    recorded_errors = [e for e in errors if e != "code_changed"]
    real_errors = [e for e in recorded_errors if classify(e) != "deferred"]
    overall = ("failed" if status in ("failed", "session_expired")
               else "degraded"
               if (real_errors or coll["collection"] != "ok"
                   or notify_state == "incomplete" or stalls)
               else "ok")
    shadow = result.get("metadata_shadow") or {"mode": "off", "reason": "not_run"}
    shadow_health = {key: shadow[key] for key in (
        "mode", "reason", "due", "fetched", "published", "deferred", "budget_s", "elapsed_s",
        "interval_s", "backoff_s", "deferred_reasons") if key in shadow}
    shadow_health["error_count"] = len(shadow.get("errors") or [])
    run = dict(result.get("run") or {})
    from operational_status import interrupted_runs
    try:
        run["last_interrupt"] = bounded_health_read(
            db, lambda _check: interrupted_runs(db, now=now),
            seconds=min(HEALTH_OBSERVATION_S, remaining()))
    except Exception:
        run["last_interrupt"] = {"status": "unavailable", "observed_at": now,
                                 "reason": "observation_unavailable"}
    if result.get("deferred_stages"):
        run["deferred_stages"] = list(result["deferred_stages"])
    run_overdue = run.get("overshoot_s", 0) > 0
    from ledger_audit import guard_status
    try:
        guards = bounded_health_read(db, lambda _check: guard_status(db),
                                     seconds=remaining())
    except Exception:
        # guard_status already fails to this honest 'unverifiable' shape
        guards = {"state": "unknown", "relations": []}
    guard_violations = any(
        relation["existing_count"] or relation["shadow_count"]
        for relation in guards["relations"])
    # no loaded config is no backup evidence — never report it 'disabled'
    backup = (backup_health(cfg or {}, now) if cfg is not CONFIG_NOT_LOADED
              else {"state": "unknown", "reasons": ["config_not_loaded"]})
    try:
        backup["local"] = maintenance.local_backup_status(now=now)
    except Exception:
        backup["local"] = {"state": "unknown", "newest_age_s": None,
                           "observed_at": now}
    backup_bad = backup["state"] not in ("ok", "disabled")
    import notify_spool
    ocr = attachment_ocr_health(notify_spool.data_root(db), cfg if checked else None, now)
    ocr_bad = ocr["state"] not in ("ok", "disabled")
    skipped = result.get("backup_skipped")
    disk_low = attachment_disk_low(free_mb) or skipped == "disk_low"
    # M7: a backup skipped for lack of time is news only once the newest
    # daily copy is older than a day — one late tick is not an incident
    age = maintenance.newest_backup_age_s() if skipped == "deadline" else 0
    backup_deferred = skipped == "deadline" and (age is None or age > 86400)
    if (run_overdue or guard_violations or backup_bad or ocr_bad
            or backup_deferred) and overall == "ok":
        overall = "degraded"
    previous = prev_health()
    last_ok_at = (now if overall == "ok" else previous.get(
        "last_ok_at", previous.get("at") if previous.get("overall") == "ok" else None))
    if (isinstance(last_ok_at, bool)
            or not isinstance(last_ok_at, (int, float))
            or not 0 <= last_ok_at < float("inf")):
        last_ok_at = None
    state_reasons = [
        code for code, present in (
            ("run_failed", status == "failed"),
            ("session_expired", status == "session_expired"),
            ("stage_errors", bool(real_errors)),
            ("code_changed", "code_changed" in errors),
            ("run_deadline_exceeded", run_overdue),
            ("ledger_relation_violations", guard_violations),
            ("backup_not_verified", backup_bad),
            (ocr.get("reason"), ocr_bad),
            ("collection_incomplete", coll["collection"] != "ok"),
            ("notification_failed", bool(notify_res.get("failed"))),
            ("notification_deferred", bool(notify_res.get("skipped"))),
            ("notification_parked", bool(notify_res.get("parked"))),
            ("notification_held", bool(held)),
            ("notification_pending", bool(outbox["pending"])),
            ("notification_pending_unavailable", pending_unknown),
            ("notification_parts_incomplete", parts_incomplete),
            ("notification_cards_unavailable", cards_unknown),
            ("disk_low", disk_low),
            ("backup_deferred_deadline", backup_deferred),
        ) if present] + stalls
    return {
        "overall": overall, "run_status": status,
        "last_ok_at": last_ok_at,
        "state_reasons": state_reasons,
        "run_id": run_id,
        "run": run,
        "ledger_guards": guards,
        "backup": backup,
        "attachment_ocr": ocr,
        "at": now,
        "unread_at": unread_at(result, status, now),
        **coll,
        "semantic_intake_unstored": unstored_state,
        "semantic_intake_diagnostic_unpersisted": diagnostic_unpersisted,
        "semantic_intake_source": source,
        "collection_diagnostics": collection_diagnostics_health(
            db, recorded_errors, now, seconds=remaining()),
        "disk_free_mb": round(free_mb) if free_mb is not None else None,
        "disk_low": disk_low,
        "notify": {"state": notify_state,
                   **outbox,
                   "held": held,
                   "held_reasons": held_reasons,
                   "retired_held": retired_held,
                   "held_pinned_attachments": held_pins,
                   "incomplete_parts": incomplete_parts,
                   "part_holds": part_holds},
        "semantic_jobs": ({"pending": _pending_rows[0]["c"],
                           "oldest_age_s": (round(now - _pending_rows[0]["o"], 1)
                                            if _pending_rows[0]["o"] else 0)}
                          if _pending_rows is not None else
                          {"pending": None, "oldest_age_s": None,
                           "status": "unknown", "observed_at": now}),
        "extract_qc_jobs": ({"pending": _pending_rows[1]["c"],
                             "oldest_age_s": (round(now - _pending_rows[1]["o"], 1)
                                              if _pending_rows[1]["o"] else 0)}
                            if _pending_rows is not None else
                            {"pending": None, "oldest_age_s": None,
                             "status": "unknown", "observed_at": now}),
        # key kept for health.json consumers; the value is the current version
        "extract_v2_coverage": coverage,
        "cards": cards_health,
        "metadata_shadow": shadow_health,
        "attention": attention,
        "patient_context": patient_context_health(
            db, cfg if checked else None, now, source, seconds=remaining()),
        "stall_marks": stall_marks,
        **early_warnings(db, result, free_mb),
        "errors": list(result.get("errors") or []),
    }


def ledger_key(db) -> str:
    """Short opaque identity of the db file (no path in health)."""
    import hashlib
    try:
        path = db.execute("PRAGMA database_list").fetchone()[2] or ""
    except (sqlite3.Error, IndexError, TypeError):
        path = ""
    return hashlib.sha256(os.path.realpath(path).encode()).hexdigest()[:12]


def cached_coverage(version, now: float, source: str) -> dict | None:
    """The previous health block's extraction coverage while it is
    younger than COVERAGE_COUNT_S, for the same extract version and the
    same db — three full COUNTs over every eligible message ran on
    every tick and every deep run. None means count now."""
    cov = prev_health().get("extract_v2_coverage")
    if (not isinstance(cov, dict) or cov.get("extract_version") != version
            or cov.get("source") != source):
        return None
    at = cov.get("counted_at")
    if not finite_number(at) or not 0 <= now - at < COVERAGE_COUNT_S:
        return None
    if not all(type(cov.get(k)) is int and cov[k] >= 0
               for k in ("current", "eligible", "poison_gated")):
        return None
    return cov
