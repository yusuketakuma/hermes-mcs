#!/usr/bin/env python3
"""MCS unread check — cron/launchd entry point (orchestrator only).

Responsibility split:
- this file      : args, flock, deadline, stage ORDER, run status,
                   outermost exception boundary
- mcs_adapter.py : API/CDP/auth/download (the only network surface)
- ledger.py      : schema, migrations, writes, outbox, jobs table
- job_ops.py     : cmd ingest, fetch_jobs drains, discovery, trickle
                   deep-import seeding, thread merge
- maintenance.py : daily verified backup, log rotation, snapshot publish
- extract*/rollup/notify_flush : derived-data + delivery stages

Exit codes:
  0 = run ok (may include per-patient partial failures — see result.errors)
  1 = run failed (bootstrap/network/schema-level failure)
  2 = session expired mid-run — stages attempt one auto_login at the
      failure point and retry; exit 2 means the run still aborted
      (relogin failed or the session re-died). The notice carries the
      outcome: 'session_recovered' on success, 'session_expired' with
      auto_login=<state> on failure -> human re-login
  3 = another instance holds the lock (overlap prevented)

Privacy: stdout/stderr carries ids, counts, states only. Patient names,
bodies, tokens never leave this process except into the local ledger and
(once external send was explicitly approved) the notify channel.

Read-ack gate: --mark-read only marks a patient when its fetch_state is
'complete' AND the ledger commit succeeded, and it always sends the exact
snapshot timestamp returned by list_unread(). A response that fails to
parse records status='unknown', never 'confirmed'.
"""
import argparse
import json
import math
import os
import sys
import time
from contextlib import suppress

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
from mcs_adapter import (MCSAdapter, MCSError, SessionExpired)
from ledger import Ledger
from mcs_util import (CACHE, CHROME_BIN, CHROME_PROFILE, CONF_PATH, DB,
                      HOME, RUN_LOCK, acquire_run_lock, load_config)
import job_ops
import maintenance
import notify_flush

# canonical path constants live in mcs_util; the local aliases keep the
# module attribute names (monkeypatch surface for tests) unchanged
LOCKFILE = RUN_LOCK
ATTACH_DIR = os.path.join(HOME, "data", "attachments")
RUN_DEADLINE_S = 480          # whole-run cap; per-request timeouts are not enough
BACKFILL_MAX_PAGES = 3        # per patient, per run — newest-first walk
BACKFILL_OVERLAP_S = 120      # re-scan window; dedup handles repeats
HEALTH_FILE = os.path.join(HOME, "data", "health.json")


def _err_str(e: Exception) -> str:
    """Structured error info only — kind+status+detail, never response
    bodies. MCSError.detail is internal structured text (e.g.
    'auto_login=keychain_locked') and is safe for logs/alerts."""
    if isinstance(e, MCSError):
        base = f"{e.kind}(status={e.status})" if e.status else e.kind
        detail = getattr(e, "detail", "")
        return f"{base}: {detail}" if detail else base
    return type(e).__name__


def _config() -> dict:
    return load_config(CONF_PATH)


# ---------- health contract (F20) ----------
# exit code says whether the PROCESS survived; health.json says how the
# SUBSYSTEMS did. Monitors must read this file — a partial run exits 0
# by design (work happened), so exit-status-only monitoring hides it.

def _unread_at(result: dict, status: str, now: float) -> float | None:
    """When unread collection last completed. A --jobs-only deep run or
    a failed tick does not collect unread, so it carries the previous
    value forward — deep runs alone must never keep health 'fresh'
    while the unread check has stopped."""
    if not result.get("jobs_only") \
            and status not in ("failed", "session_expired"):
        return now
    try:
        with open(HEALTH_FILE, encoding="utf-8") as f:
            prev = json.load(f).get("unread_at")
    except (OSError, ValueError, AttributeError):
        return None
    return prev if isinstance(prev, int | float) \
        and not isinstance(prev, bool) and math.isfinite(prev) else None


def _health(ledger, result: dict, status: str,
            run_id: int | None = None) -> dict:
    """Per-subsystem machine-readable state: collection completeness,
    notification completion, extract/QC backlog lag, and current
    extraction-generation coverage. Every query is a count — never bodies.
    `run_id` binds this evidence to the source run that produced it."""
    import extract_llm
    import notify_cards
    from mcs_queries import current_extract_pred, json_or_null
    now = time.time()
    notify_res = result.get("notify") or {}
    notify_state = ("incomplete"
                    if notify_res.get("failed") or notify_res.get("skipped")
                    else "parked" if notify_res.get("parked") else "ok")
    outbox = ledger.db.execute(
        "SELECT COUNT(*) c, MIN(created_at) o FROM notify_outbox "
        "WHERE state IN ('pending','failed') AND next_try IS NOT NULL"
    ).fetchone()
    held = ledger.db.execute(
        "SELECT COUNT(*) FROM notify_outbox "
        "WHERE state='failed' AND next_try IS NULL").fetchone()[0]
    if held:
        notify_state = "incomplete"
    sem = ledger.db.execute(
        "SELECT COUNT(*) c, MIN(created_at) o FROM fetch_jobs "
        "WHERE kind='semantic' AND state='pending'").fetchone()
    qc = ledger.db.execute(
        "SELECT COUNT(*) c, MIN(created_at) o FROM fetch_jobs "
        "WHERE kind='extract_qc' AND state='pending'").fetchone()
    eligible_where = (
        "m.body_text IS NOT NULL AND m.body_text != '' "
        "AND (m.body_state IS NULL OR m.body_state='full')")
    poison = ledger.db.execute(
        f"SELECT COUNT(*) FROM messages m WHERE {eligible_where} "
        "AND EXISTS(SELECT 1 FROM artifacts bad "
        "  WHERE bad.kind='extract_llm' AND bad.message_id=m.message_id"
        "    AND NOT json_valid(bad.meta))").fetchone()[0]
    total = ledger.db.execute(
        f"SELECT COUNT(*) FROM messages m WHERE {eligible_where}"
    ).fetchone()[0]
    current = ledger.db.execute(f"""
      SELECT COUNT(*) FROM messages m WHERE {eligible_where}
        AND EXISTS(SELECT 1 FROM artifacts a
          WHERE a.kind='extract_llm' AND a.message_id=m.message_id
            {current_extract_pred('a', 'm')}
            AND json_extract({json_or_null('a.meta')},'$.extract_version')=?)
    """, (extract_llm.EXTRACT_VERSION,)).fetchone()[0]
    collection = ("incomplete"
                  if result.get("incomplete") or result.get("coverage_gaps")
                  else "ok")
    overall = ("failed" if status in ("failed", "session_expired")
               else "degraded"
               if (result.get("errors") or collection != "ok"
                   or notify_state == "incomplete")
               else "ok")
    return {
        "overall": overall, "run_status": status,
        "run_id": run_id,
        "at": now,
        "unread_at": _unread_at(result, status, now),
        "collection": collection,
        "incomplete_projects": result.get("incomplete") or [],
        "notify": {"state": notify_state,
                   "pending": outbox["c"],
                   "held": held,
                   "oldest_age_s": (round(now - outbox["o"], 1)
                                    if outbox["o"] else 0)},
        "semantic_jobs": {"pending": sem["c"],
                          "oldest_age_s": (round(now - sem["o"], 1)
                                           if sem["o"] else 0)},
        "extract_qc_jobs": {"pending": qc["c"],
                            "oldest_age_s": (round(now - qc["o"], 1)
                                             if qc["o"] else 0)},
        "extract_v2_coverage": {
            "extract_version": extract_llm.EXTRACT_VERSION,
            "current": current, "eligible": total,
            "poison_gated": poison,
            "ratio": round(current / total, 4) if total else None},
        "cards": notify_cards.health_cards(ledger),
        "errors": list(result.get("errors") or []),
    }


def _write_health(ledger, result: dict, status: str,
                  run_id: int | None = None) -> None:
    """Atomic health.json — monitoring consumes this file. A write
    failure must never turn committed ingest work into a crash, but it
    must not be silent either: a durable 'health_write_failed' incident
    is queued so the gap is itself visible (GAP-1)."""
    try:
        health = _health(ledger, result, status, run_id=run_id)
        result["health"] = health
        maintenance.atomic_publish_text(
            HEALTH_FILE, json.dumps(health, ensure_ascii=False))
    except Exception as e:
        result["errors"].append(
            f"health_write_failed: {type(e).__name__}")
        with suppress(Exception):
            ledger.outbox_add("health_write_failed", None,
                              {"detail": type(e).__name__,
                               "run_id": run_id})
LOCK_WAIT_S = 150
LOCK_POLL_S = 0.2


_MCS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code_stamp() -> int:
    """Newest mtime of the mcs/ sources — changes when an update merges."""
    newest = 0
    for base, _dirs, files in os.walk(_MCS_ROOT):
        for name in files:
            if name.endswith(".py"):
                with suppress(OSError):
                    newest = max(newest, os.stat(
                        os.path.join(base, name)).st_mtime_ns)
    return newest


_CODE_STAMP = _code_stamp()


def _wait_run_lock(wait_s: float) -> int | None:
    """Poll the run lock until ``wait_s`` elapses — short polls so a
    holder that releases between batches is won, not missed. A lock won
    right after an updater released it is given back: this process
    loaded the OLD code before waiting, and running it (with modules
    imported later from the new tree) would mix generations."""
    until = time.monotonic() + wait_s
    while time.monotonic() < until:
        time.sleep(LOCK_POLL_S)
        fd = acquire_run_lock(LOCKFILE)
        if fd is not None:
            marker = os.path.join(HOME, "data", "update_in_progress.marker")
            if os.path.exists(marker) or _code_stamp() != _CODE_STAMP:
                os.close(fd)
                return None
            return fd
    return None


SESSION_ALERT_MIN_INTERVAL_S = 3600
# a flapping session must not drive auto_login in a loop — each wrapped
# stage allows one attempt, and _attempt_relogin (the single entry point
# for every caller, run boundary included) bounds the per-run total
RELOGIN_MAX_PER_RUN = 3


def _alert_throttled(ledger, kind: str, run_id: int, detail: str) -> bool:
    """Enqueue a system alert — throttled so a persistent condition does
    not re-alert on every tick: one per kind per hour keeps it visible
    without flooding the channel. The run row and health file still
    record every occurrence; only the notification is gated."""
    last = ledger.db.execute(
        "SELECT MAX(created_at) t FROM notify_outbox"
        " WHERE kind=?", (kind,)).fetchone()["t"]
    if last is not None \
            and time.time() - float(last) < SESSION_ALERT_MIN_INTERVAL_S:
        return False
    ledger.outbox_add(kind, None, {"run_id": run_id, "detail": detail})
    return True


def _alert_session_expired(ledger, run_id: int, detail: str) -> bool:
    """Enqueue the session_expired alert (one per hour max)."""
    return _alert_throttled(ledger, "session_expired", run_id, detail)


def _alert_session_recovered(ledger, run_id: int, detail: str) -> None:
    """Enqueue the session_recovered notice — NOT throttled: it resolves
    a possibly-visible session_expired alert, so suppressing it would
    leave a stale 'manual re-login required' standing in the channel.
    Volume stays bounded in practice because a session that flaps past
    RELOGIN_MAX_PER_RUN aborts the run instead of recovering again."""
    ledger.outbox_add("session_recovered", None,
                      {"run_id": run_id, "detail": detail})


def _attempt_relogin(adapter, ledger, result, where: str,
                     err: "SessionExpired") -> str:
    """One bounded auto_login at the point a stage died on
    SessionExpired. This is the single entry point for every caller, so
    it owns the per-run budget: once RELOGIN_MAX_PER_RUN attempts are
    spent it returns 'budget_exhausted' without calling the adapter or
    journaling (relogin_attempts records real attempts only). Every real
    attempt is journaled in result['relogin_attempts'] for the run log;
    on success a session_recovered notice is queued so the channel sees
    the blip AND its resolution instead of silence or a false
    manual-login alert."""
    attempts = result.setdefault("relogin_attempts", [])
    if len(attempts) >= RELOGIN_MAX_PER_RUN:
        return "budget_exhausted"
    attempt = {"stage": where, "error": _err_str(err)}
    attempts.append(attempt)
    try:
        state = adapter.auto_login(profile_dir=CHROME_PROFILE,
                                   chrome_bin=CHROME_BIN)
    except Exception as exc:
        state = "failed"
        attempt["detail"] = type(exc).__name__
    attempt["state"] = state
    if state == "ok":
        _alert_session_recovered(ledger, result.get("run_id"),
                                 f"{where}: {attempt['error']}")
    return state


def _with_relogin(adapter, ledger, result, where: str, fn, *args,
                  **kwargs):
    """Run one adapter-bound stage with a single in-run session
    recovery. A mid-stage SessionExpired no longer abandons the whole
    tick: auto_login is tried once at the failure point, and on success
    the stage replays — every stage is idempotent (deduped saves,
    durable job cursors, snapshot-gated marks), so a replay cannot
    double-apply committed work. A stage that already ran its own
    auto_login (detail 'auto_login=…'), a run over the relogin budget,
    or a session that dies again right after recovery is not retried —
    it escalates to the run boundary."""
    try:
        return fn(*args, **kwargs)
    except SessionExpired as e:
        if "auto_login=" in (e.detail or ""):
            raise
        state = _attempt_relogin(adapter, ledger, result, where, e)
        if state == "budget_exhausted":
            # cap spent — the raw failure escalates undecided; the run
            # boundary's own attempt is declined the same way and
            # reports 'auto_login=budget_exhausted'
            raise
        if state != "ok":
            raise SessionExpired(f"auto_login={state}") from e
        try:
            return fn(*args, **kwargs)
        except SessionExpired as e2:
            raise SessionExpired(
                "auto_login=ok_then_expired") from e2


# ---------- stage: unread pipeline ----------

def stage_unread(adapter, ledger, args, result, deadline, run_id,
                 semantic: bool = False,
                 notify_max_age_s: float | None = None):
    """list_unread -> per-patient unread fetch -> reply merge -> save
    (notify intent in the same tx) -> optional gated mark-read.
    notify_max_age_s: unread messages older than this are imported
    without notification (bulk-added patients surface as unread with
    months-old history). Returns the snapshot for downstream stages."""
    try:
        snap = adapter.list_unread()
    except SessionExpired as e:
        # credential-free recovery: saved-password autofill + submit
        # click — journaled + notified like every other relogin path
        state = _attempt_relogin(adapter, ledger, result, "unread", e)
        if state == "budget_exhausted":
            raise
        if state == "ok":
            try:
                snap = adapter.list_unread()
            except SessionExpired as e2:
                # a session that dies again right after this stage's own
                # auto_login carries the marker, so _with_relogin does
                # not spend a second attempt re-running the whole stage
                raise SessionExpired(
                    "auto_login=ok_then_expired") from e2
        else:
            raise SessionExpired(f"auto_login={state}") from e
    ledger.db.execute("UPDATE runs SET snapshot_ts=? WHERE run_id=?",
                      (snap.timestamp, run_id))
    ledger.db.commit()
    result["projects"] = len(snap.patients)
    result["snapshot_ts"] = snap.timestamp

    for p in snap.patients:
        if time.monotonic() > deadline:
            result["errors"].append("deadline_exceeded")
            break
        batch = None
        session_error = None
        try:
            batch = adapter.fetch_unread_messages(p.project_id, snap.timestamp)
            p.messages = batch.messages
            for m in p.messages:
                if m.body_state == "snippet":
                    # a truncated parent body has no dedicated fetch
                    # surface — later list/history reads may upgrade it.
                    # Until then the patient must not be ACKed (F05)
                    p.fetch_state = "incomplete"
                    p.fetch_reason = "parent_body_incomplete"
                try:
                    replies = adapter.fetch_unread_replies(m)
                except SessionExpired as e:
                    p.fetch_state = "incomplete"
                    p.fetch_reason = e.kind
                    session_error = e
                    break
                except MCSError as e:
                    # thread fetch failed — durable refetch jobs for each
                    # unread reply instead of aborting the patient
                    if e.kind == "thread_incomplete":
                        ledger.job_add("thread", p.project_id, m.message_id,
                                       parent_id=m.message_id,
                                       payload={"page": 1})
                    for t in m.replies:
                        if t.is_unread:
                            ledger.job_add(
                                "reply", p.project_id, t.message_id,
                                parent_id=m.message_id)
                    p.fetch_state = "incomplete"
                    p.fetch_reason = "thread_incomplete"
                    continue
                if replies.missing:
                    for rid in replies.missing:
                        ledger.job_add(
                            "reply", p.project_id, rid,
                            parent_id=m.message_id)
                    p.fetch_state = "incomplete"
                    p.fetch_reason = "replies_missing"
            if batch.error:
                p.fetch_state = "incomplete"
                p.fetch_reason = batch.error.kind
                if batch.error.kind == "pages_exceeded" \
                        and not ledger.history_job(p.project_id):
                    # more unread than the page cap: the tail is
                    # unreachable via the unread list — hand the
                    # backlog to the durable cursor walk (F03)
                    ledger.job_add("history", p.project_id, payload={
                        "since": 0, "page": 1, "pages": 10})
            if p.fetch_state != "incomplete":
                p.fetch_state = "complete"
        except SessionExpired:
            raise  # auth failure must abort the run, not look partial
        except MCSError as e:
            p.fetch_state = "incomplete"
            p.fetch_reason = e.kind
        if p.fetch_state == "incomplete" and p.project_id \
                not in result["incomplete"]:
            result["incomplete"].append(p.project_id)
            result["errors"].append(
                f"project {p.project_id}: {p.fetch_reason}")

        # persist whatever was fetched — partial data is still durable;
        # fetch_state records it is NOT eligible for acknowledgement.
        # The notify intent lands in the SAME transaction so a crash can
        # never leave a stored message without its notification (B11).
        try:
            new_ids = ledger.save_patient(p, notify={
                "run_id": run_id, "source": "unread",
                "snapshot_ts": snap.timestamp}, semantic=semantic,
                notify_max_age_s=notify_max_age_s)
        except Exception as e:
            ledger.patient_fetch_failed(p.project_id, type(e).__name__)
            result["errors"].append(
                f"project {p.project_id}: db_write_failed")
            continue
        result["messages"] += len(p.messages)
        result["new_messages"] += len(new_ids)

        if session_error:
            raise session_error
        if isinstance(getattr(batch, "error", None), SessionExpired):
            raise batch.error

        if args.mark_read and p.fetch_state == "complete":
            if ledger.was_marked(p.project_id, snap.timestamp):
                continue
            # record the intent BEFORE sending — a crash between send
            # and record must read as unknown, never as confirmed
            ledger.mark_read(p.project_id, snap.timestamp, "unknown")
            try:
                adapter.mark_patient_read(p.project_id, snap.timestamp)
                ledger.mark_read(p.project_id, snap.timestamp, "confirmed")
                result["marked_read"].append(p.project_id)
            except MCSError as e:
                result["errors"].append(
                    f"mark {p.project_id}: {_err_str(e)}")
    return snap


# ---------- stage: coverage backfill ----------

def stage_backfill(adapter, ledger, result, deadline, run_id,
                   semantic: bool = False,
                   notify_max_age_s: float | None = None):
    """Catch posts the unread API misses (e.g. read by another human).
    Walk each patient's history down to CONFIRMED coverage — never the
    newest stored message, so storing a new unread cannot skip older
    unfetched items (Oracle B06). Coverage only advances on a complete
    walk; a gap stays visible via coverage_lag."""
    for row in ledger.frontier_patients():
        pid = row["project_id"]
        if time.monotonic() > deadline - 30:
            result["errors"].append("deadline_exceeded")
            break
        wm = ledger.high_watermark(pid)
        cov = ledger.coverage_ts(pid)
        if not wm and not cov:
            continue  # no baseline anchor yet
        cutoff = (cov or wm) - BACKFILL_OVERLAP_S
        try:
            batch = adapter.fetch_history(
                pid, cutoff, max_pages=BACKFILL_MAX_PAGES)
        except SessionExpired:
            raise
        except MCSError as e:
            result["errors"].append(
                f"backfill {pid}: {_err_str(e)}")
            continue
        hist = batch.messages
        merged = job_ops.merge_full_replies(
            adapter, hist, 0, deadline, result, ledger=ledger)
        new_ids = ledger.save_messages(
            hist, project_id=pid,
            notify={"run_id": run_id, "source": "history"},
            semantic=semantic, notify_max_age_s=notify_max_age_s)
        if new_ids:
            result["backfilled"] += len(new_ids)
        if merged.error:
            raise merged.error
        if batch.error:
            result["errors"].append(
                f"backfill {pid}: {_err_str(batch.error)}")
            if isinstance(batch.error, SessionExpired):
                raise batch.error
        if batch.reached and not batch.error and merged.checkpoint_safe \
                and ledger.pending_reply_jobs(pid) == 0:
            newest = wm or int(time.time())
            ledger.set_coverage(pid, newest)
            lag = ledger.coverage_lag(pid)
            if lag > 3600:
                result.setdefault("coverage_gaps", []).append(
                    {"pid": pid, "lag_s": lag})
        else:
            # A bounded frontier read is not a completed history walk.
            # Reserve its tail independently of the deep-import floor.
            ledger.job_add("history_head", pid, payload={
                "since": max(0, cutoff),
                "page": 1 + batch.pages if merged.checkpoint_safe else 1,
                "pages": BACKFILL_MAX_PAGES, "trickle": False})
            lag = ledger.coverage_lag(pid)
            if lag > 0:
                # Only a real gap between the stored head and verified
                # coverage is reportable — a bounded scan that cannot
                # reach the natural end (>BACKFILL_MAX_PAGES of
                # history) defers certification to history_head.
                result.setdefault("coverage_gaps", []).append(
                    {"pid": pid, "lag_s": lag})
                result["errors"].append(
                    f"backfill {pid}: coverage_incomplete")


# ---------- stage: self-post / missed-post probe ----------

SELF_PROBE_PAGES = 2          # bounded history fetch per positive probe
SELF_PROBE_MARGIN_S = 60      # keep the tail of the deadline for notify


def stage_self_probe(adapter, ledger, result, deadline, run_id,
                     semantic: bool = False,
                     notify_max_age_s: float | None = None):
    """Probe every active patient's `latest` message id against the
    stored ledger. The unread set structurally cannot contain posts the
    operator wrote (never unread for their author) or posts another
    human already read — this per-project check is the only timely
    signal for them; the deep walks catch up days later.
    A positive probe triggers a bounded fetch_history; every newly
    stored row notifies like an unread arrival (notify_all_new).
    An id that a completed fetch still cannot store is remembered
    (probe_mid) so it is not re-fetched every tick.
    """
    probed = 0
    for row in ledger.frontier_patients():
        pid = row["project_id"]
        wm = ledger.high_watermark(pid)
        if not wm:
            continue  # never imported — the durable history jobs own it
        if time.monotonic() > deadline - SELF_PROBE_MARGIN_S:
            result["errors"].append("self_probe: deadline_exceeded")
            break
        try:
            probe = adapter.fetch_latest(pid)
        except SessionExpired:
            raise
        except MCSError as e:
            result["errors"].append(f"probe {pid}: {e.kind}")
            continue
        probed += 1
        mid = probe["message_id"]
        if mid is None or ledger.has_message(mid):
            continue                     # nothing newer than stored
        if ledger.probe_marker(pid) == mid:
            continue                     # unfetchable id — already tried
        try:
            batch = adapter.fetch_history(pid, wm, max_pages=SELF_PROBE_PAGES)
        except SessionExpired:
            raise
        except MCSError as e:
            result["errors"].append(f"probe {pid}: {e.kind}")
            continue
        merged = job_ops.merge_full_replies(
            adapter, batch.messages, 0, deadline, result, ledger=ledger)
        src = "self" if probe["is_self_only"] else "probe"
        new_ids = ledger.save_messages(
            batch.messages, project_id=pid, semantic=semantic,
            notify={"run_id": run_id, "source": src},
            notify_max_age_s=notify_max_age_s, notify_all_new=True)
        if new_ids:
            result["new_messages"] += len(new_ids)
        result.setdefault("self_probe_fetched", []).append(pid)
        # A page-limited walk has not established that the latest id is
        # unfetchable. Only a completed walk may suppress later probes.
        if not batch.error and batch.reached and merged.checkpoint_safe:
            ledger.set_probe_marker(pid, mid)
        elif not batch.error and not ledger.has_message(mid):
            result["errors"].append(f"probe {pid}: history_incomplete")
        if merged.error:
            raise merged.error
        if batch.error:
            result["errors"].append(f"probe {pid}: {batch.error.kind}")
            if isinstance(batch.error, SessionExpired):
                raise batch.error
    result["self_probe"] = probed


# ---------- stage: attachments ----------

def stage_attachments(adapter, ledger, result, deadline, semantic=False):
    # attachments referenced by queued notify events jump the queue —
    # otherwise a deep backlog leaves new-message files undownloaded
    # when flush() posts the event, and accepted events never re-send
    priority = ledger.pending_notify_message_ids()
    for a in ledger.attachments_due(limit=30, priority_mids=priority):
        if time.monotonic() > deadline - 20:
            break
        dest = os.path.join(ATTACH_DIR, str(a["attachment_id"]))
        try:
            info = adapter.download(a["url"], dest)
            ledger.attachment_saved(a["attachment_id"], dest,
                                    info["bytes"], info["sha256"], semantic=semantic)
        except MCSError as e:
            # keep the HTTP status in the recorded kind — the failure
            # class (permanent 4xx vs transient) and triage need it (F11)
            kind = (f"http_{e.status}" if e.kind == "http_error"
                    and getattr(e, "status", None) else e.kind)
            ledger.attachment_failed(a["attachment_id"], kind,
                                     semantic=semantic)
            result["errors"].append(
                f"attach {a['attachment_id']}: {kind}")
        except OSError as e:
            ledger.attachment_failed(
                a["attachment_id"], f"fs_{type(e).__name__}", semantic=semantic)
            result["errors"].append(f"attach {a['attachment_id']}: fs")


# ---------- stage: derived data ----------

def stage_derive(ledger, result, deadline, cfg=None,
                 llm_budget_cap: float = 90):
    """extract_v1 (instant rules) -> extract_llm (v3) -> rollups.

    Two lanes, one lineage: the rule pass is pure-pattern and instant —
    it keeps real-time analysis at ingest speed so notifications and
    signals never wait on the LLM queue. The v3 pass then folds the
    same rule output in as prompt hints and mints the extract_v1
    artifact itself, so v1+v2 work happens inside the v3 pass too."""
    try:
        import extract
        ex = extract.run_pending(ledger)
        result["extracted"] = ex["done"]
        result["extracted_pids"] = ex["pids"]
    except Exception as e:
        result["errors"].append(f"extract: {type(e).__name__}")

    try:
        import extract_llm
        # T18: under the v4 engine (fact_source=canonical) the legacy
        # v3 extractor admits NOTHING new. Conversion manifests schedule
        # v4 jobs; they never reopen v3. Outside canonical mode admission
        # is unchanged.
        admitted = None
        try:
            import semantic_policy
            scfg = semantic_policy.semantic_config(cfg or {})[0]
            canonical_mode = scfg.get("fact_source") == "canonical"
        except Exception:
            canonical_mode = False  # config unreadable → legacy behavior
        if canonical_mode:
            try:
                import semantic_v4
                admitted = semantic_v4.active_legacy_admissions(ledger)
            except Exception:
                # canonical mode defaults v3 admission to ZERO — a
                # manifest read error must fail CLOSED, not open the
                # legacy engine to unrestricted new inference
                admitted = set()
        remain = (deadline - time.monotonic()) - 45
        result["extract_llm"] = (
            extract_llm.run_pending(
                ledger, limit=15, budget_s=min(llm_budget_cap,
                                               max(0, remain)),
                admitted_ids=admitted,
                # drainers take the newest rows (DESC); the tick walks
                # the tail so the two never re-process the same rows.
                # batch_k amortizes the per-call cost over context-free
                # backlog rows — singles stay the path for context rows.
                oldest_first=True, batch_k=extract_llm._BATCH_K)
            if remain > 10 else {"done": 0, "failed": 0, "left": -1,
                                 "pids": []})
    except Exception as e:
        result["errors"].append(f"extract_llm: {type(e).__name__}")

    # rebuild rollups for patients whose underlying data changed —
    # union of touched-this-run + dirty detection (Oracle B23)
    try:
        import rollup
        touched = set(result.get("extracted_pids", []))
        touched |= set(result.get("extract_llm", {}).get("pids", []))
        for k in ("cmd_imports", "trickle_imports"):
            touched |= {c["pid"] for c in result.get(k, [])}
        touched |= set(rollup.dirty_projects(ledger))
        result["rollups"] = rollup.rebuild_many(ledger, touched)
    except Exception as e:
        result["errors"].append(f"rollup: {type(e).__name__}")

    # prospective review candidates — lifecycle-persisted signal_v1
    # artifacts; notify intents only when config signals.notify is set
    try:
        import mcs_signals
        result["signals"] = mcs_signals.evaluate(
            ledger, cfg or _config(), deadline=deadline)
    except Exception as e:
        result["errors"].append(f"signals: {type(e).__name__}")


# ---------- main ----------

def _consent_only(lock_fd) -> int:
    """Tick while a schema-bump DB replace awaits human consent: drain
    ONLY the ops.restore_approve channel (drain_commands defers every
    other file while the marker stands). No adapter, no ingest, no
    reconcile — nothing may write a table the loss report counts."""
    try:
        ledger = Ledger(DB)
    except Exception:
        os.close(lock_fd)
        print(json.dumps({"ok": False, "error": "ledger_init_failed"}))
        return 1
    result = {"ok": True, "restore_consent_hold": True,
              "commands": 0, "errors": []}
    try:
        job_ops.drain_commands(ledger, result)
        result["commands"] = result.get("command_commands", 0)
        # The receipt may predate this tick (an earlier spawn lost the
        # run.lock race). Re-check the bound consent directly — when a
        # matching receipt exists, relaunch the updater so the held
        # restore converges without waiting for the periodic check.
        import notify_cards
        marker = notify_cards.restore_awaiting_consent(
            os.path.join(HOME, "data"))
        if marker and marker.get("backup_path"):
            try:
                import mcs_update
                report = mcs_update._restore_loss_report(
                    marker["backup_path"])
                if mcs_update._restore_consent(report) is not None:
                    mcs_update.spawn_detached()
            except Exception:
                result["errors"].append("consent_respawn_failed")
    except Exception as e:
        result["ok"] = False
        result["errors"].append(f"cmd:{type(e).__name__}")
    print(json.dumps(result, ensure_ascii=False))
    try:
        ledger.close()
    finally:
        os.close(lock_fd)
    return 0 if result["ok"] else 1


def _commands_only(lock_fd, deadline) -> int:
    """Interactive-notification command worker: drains data/cmd (shared
    queue — operator card_resolve lives there) and data/cmd_int, repairs
    published state, and refreshes the snapshot when notification
    receipts/renders dirtied it. Deliberately no adapter, no auth, no
    ingest, no derive, no notify_flush run, no health write — it exists so
    a card interaction never waits a whole tick (RC05/RC20)."""
    import notify_cards
    import notify_cmds
    try:
        ledger = Ledger(DB)
    except Exception:
        os.close(lock_fd)
        print(json.dumps({"ok": False, "error": "ledger_init_failed"}))
        return 1
    result = {"ok": True, "commands": 0, "errors": []}
    cfg = _config()
    root = os.path.join(HOME, "data")
    try:
        notify_cards.ensure_dirs(root)
        if notify_cards.restore_awaiting_consent(root) is not None:
            # schema-bump DB replace held for human consent — freeze
            # every writer except the consent drain so the approved
            # loss report cannot drift (drain_commands itself is
            # consent-only while the marker stands)
            job_ops.drain_commands(ledger, result)
            result["restore_consent_hold"] = True
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["ok"] else 1
        # startup/periodic recovery: missing spec files, flags,
        # stale-claim visibility — before any command is applied
        notify_cards.recover(ledger, cfg, result)
        # a DB restore holds all send grants until the journal-vs-DB
        # reconcile finishes — run it before draining new commands
        if notify_cards.restore_pending(root) is not None:
            try:
                import notify_reconcile
                rep = notify_reconcile.reconcile_after_restore(
                    ledger, cfg)
                result["restore_reconcile"] = rep["counts"]
            except Exception as e:
                result["errors"].append(
                    f"restore_reconcile:{type(e).__name__}")
        # dependency order: existing data/cmd traffic first (a resolve
        # may settle an attempt a receipt then reports), then cmd_int
        cmds_before = result.get("command_commands", 0)
        job_ops.drain_commands(ledger, result)
        if result.get("command_commands", 0) > cmds_before:
            # drain_int sweeps after its own applies; data/cmd applies
            # (card_resolve, request.create) drift cards the same way —
            # re-render here or the change waits for the next tick
            try:
                notify_cards.sweep(ledger, cfg)
            except Exception as e:
                result["errors"].append(f"cmd_sweep:{type(e).__name__}")
        notify_cmds.drain_int_commands(
            ledger, result, cfg, root,
            deadline=min(deadline, time.monotonic() + min(120, max(
                5, deadline - time.monotonic() - 10))))
        # a second drain: commands queued by the first pass's receipts
        notify_cmds.drain_int_commands(
            ledger, result, cfg, root, deadline=min(deadline, time.monotonic() + 30))
        notify_cards.publish_flags(cfg, root)
        notify_cards.gc(ledger, cfg)
        if notify_cards.snapshot_dirty(ledger):
            result["snapshot"] = maintenance.publish_snapshot(DB)
            if result["snapshot"]:
                notify_cards.clear_snapshot_dirty(ledger)
            else:
                result["ok"] = False
                result["errors"].append("snapshot_publish_failed")
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    except Exception as e:
        # exception text can embed paths/user data — type name only,
        # matching the tick's outermost boundary
        print(json.dumps({"ok": False,
                          "error": f"crash:{type(e).__name__}"}))
        return 1
    finally:
        try:
            ledger.close()
        finally:
            os.close(lock_fd)


def _semantic_enabled(ledger, cfg, result) -> bool:
    """Resolve the semantic layer's mode and refresh stale projections.
    Config/module trouble forces semantic OFF — but it must be visible:
    an enforced pipeline silently disabled is a missed evaluation, not
    a clean "no work" tick."""
    try:
        import semantic as _sem
        from semantic_store import invalidate_projections
        scfg = _sem.semantic_config(cfg)[0]
        invalidate_projections(ledger, scfg)
        return scfg["mode"] != "off"
    except Exception as e:
        result["errors"].append(f"semantic_init: {type(e).__name__}")
        return False


def _stage_fetch(adapter, ledger, args, cfg, result, deadline, run_id,
                 sem_on, notify_max_age_s):
    """Priority fetch work — unread, optional backfill and self-post
    probes — skipped entirely by --jobs-only runs."""
    if args.jobs_only:
        result["jobs_only"] = True
        return
    _with_relogin(adapter, ledger, result, "unread",
                  stage_unread, adapter, ledger, args, result, deadline,
                  run_id, semantic=sem_on,
                  notify_max_age_s=notify_max_age_s)
    if not args.no_backfill:
        _with_relogin(adapter, ledger, result, "backfill",
                      stage_backfill, adapter, ledger, result, deadline,
                      run_id, semantic=sem_on,
                      notify_max_age_s=notify_max_age_s)
    self_posts = cfg.get("self_posts", False)
    if type(self_posts) is not bool:
        result["errors"].append("config: self_posts_invalid")
        self_posts = False
    if self_posts:
        _with_relogin(adapter, ledger, result, "self_probe",
                      stage_self_probe, adapter, ledger, result,
                      deadline, run_id, semantic=sem_on,
                      notify_max_age_s=notify_max_age_s)


def _run_jobs(adapter, ledger, args, cfg, result, deadline, sem_on,
              notify_max_age_s):
    """Durable job machinery — command ingest, discovery, reply and
    history drains, trickle seeding, reconcile and self profile."""
    # cmd ingest first so requests are due THIS run; discovery and
    # trickle seeding only enqueue — the drains below execute them.
    job_ops.drain_commands(ledger, result)
    try:
        # interactive-notification command channel: plugin receipts
        # and card actions apply mid-tick, not just via the watcher
        import notify_cards
        import notify_cmds
        notify_cards.ensure_dirs(os.path.join(HOME, "data"))
        # a DB restore holds send grants until the journal-vs-DB
        # reconcile lands — run it before draining cmd_int traffic
        if notify_cards.restore_pending(
                os.path.join(HOME, "data")) is not None:
            import notify_reconcile
            rep = notify_reconcile.reconcile_after_restore(
                ledger, cfg)
            result["restore_reconcile"] = rep["counts"]
        notify_cmds.drain_int_commands(
            ledger, result, cfg, os.path.join(HOME, "data"),
            deadline=deadline)
    except Exception as e:
        result["errors"].append(f"cmd_int: {type(e).__name__}")
    job_ops.seed_discovery(ledger)
    discover_archived = cfg.get("discover_archived", False)
    if type(discover_archived) is not bool:
        result["errors"].append("config: discover_archived_invalid")
        discover_archived = False
    _with_relogin(adapter, ledger, result, "discovery",
                  job_ops.run_discovery, adapter, ledger, result,
                  deadline, include_archived=discover_archived)
    _with_relogin(adapter, ledger, result, "reply_jobs",
                  job_ops.run_reply_jobs, adapter, ledger, result,
                  deadline, semantic=sem_on,
                  notify_max_age_s=notify_max_age_s)
    _with_relogin(adapter, ledger, result, "history_jobs",
                  job_ops.run_history_jobs, adapter, ledger, result,
                  deadline, trickle=False, semantic=sem_on)

    if args.download_files:
        stage_attachments(adapter, ledger, result, deadline, semantic=sem_on)

    # -- idle-capacity deep history (trickle) ----------------------
    deep_history = cfg.get("deep_history", True)
    trickle_pages = cfg.get("trickle_pages", 3)
    if type(deep_history) is not bool:
        result["errors"].append("config: deep_history_invalid")
        deep_history = True
    if type(trickle_pages) is not int or not 1 <= trickle_pages <= 40:
        result["errors"].append("config: trickle_pages_invalid")
        trickle_pages = 3
    # deep_history gates NEW seeding only — already-pending jobs
    # (including archived patients' history_head final syncs) still
    # drain on idle capacity. Same contract as discover_archived:
    # a switch stops new work, never abandons committed work.
    if deep_history:
        seeded = job_ops.seed_trickle(ledger)
        if seeded:
            result["trickle_seeded"] = seeded
    # --jobs-only runs exist FOR this work: bigger slice of the
    # window, smaller safety margin than the priority tick
    _with_relogin(adapter, ledger, result, "trickle_jobs",
                  job_ops.run_history_jobs,
                  adapter, ledger, result, deadline, trickle=True,
                  trickle_pages=trickle_pages,
                  max_jobs=8 if args.jobs_only else None,
                  min_margin=30 if args.jobs_only else None,
                  semantic=sem_on)

    # -- post-import reconcile: edits/deletions below the cutoff ---
    _with_relogin(adapter, ledger, result, "reconcile",
                  job_ops.run_reconcile_jobs, adapter, ledger, result,
                  deadline, semantic=sem_on)

    # own identity: name/professions/stations from MCS, persisted
    # as the signal engine's default self (config overrides). Never
    # fails the run — an unusable profile is a logged warning.
    try:
        import mcs_signals
        prof = adapter.self_profile()
        with ledger.db:
            if mcs_signals.record_self_profile(ledger.db, prof):
                result["self_profile"] = "updated"
    except Exception as e:
        result["errors"].append(f"self_profile: {type(e).__name__}")


def _notify_max_age_s(cfg, result):
    """Validated notify_max_age_h config as seconds, or None."""
    mah = cfg.get("notify_max_age_h")
    if mah is None:
        return None
    try:
        seconds = float(mah) * 3600 if type(mah) in (int, float) else 0
    except (ValueError, OverflowError):
        seconds = 0
    if not math.isfinite(seconds) or seconds <= 0:
        result["errors"].append("config: notify_max_age_h_invalid")
        return None
    return seconds


def _deliver(ledger, args, cfg, result, deadline):
    """Committed sends first — notify outbox flush, live-card sweep and
    card gc — ahead of any new semantic analysis (§19.1)."""
    if not args.no_notify:
        try:
            result["notify"] = notify_flush.flush(ledger, deadline=deadline)
        except Exception as e:
            result["errors"].append(f"notify: {type(e).__name__}")
    import notify_cards
    try:
        # live-card sweep: edits/deletes, signal resolves, archive
        # revokes, deferral expiry, stuck spec repair — bounded
        result["card_sweep"] = notify_cards.sweep(
            ledger, cfg, limit=50)
    except Exception as e:
        result["errors"].append(f"card_sweep: {type(e).__name__}")
    try:
        # expired tokens + settled spec payloads — bounded per tick
        result["card_gc"] = notify_cards.gc(ledger, cfg)
    except Exception as e:
        result["errors"].append(f"card_gc: {type(e).__name__}")


def _run_semantic(ledger, args, cfg, result, deadline, sem_on):
    """Semantic layer drain (Phase J, feature-gated) — durable
    'semantic' jobs on the same lock + remaining deadline. OFF is a
    no-op here AND disables seeding above, so the flag truly stops
    communication rather than only hiding output."""
    if not sem_on:
        return
    try:
        import semantic
        result["semantic"] = semantic.run_due(
            ledger, cfg, result, deadline, cfg_path=CONF_PATH,
            max_jobs=12 if args.jobs_only else 4)
    except Exception as e:
        result["errors"].append(
            f"semantic: {type(e).__name__}")


def _housekeeping(result):
    """Daily backup, log rotation and attachment pruning — each failure
    lands in errors without failing the run."""
    try:
        maintenance.daily_backup(DB)
    except maintenance.MaintenanceError as e:
        result["errors"].append(f"backup: {e}")
    except Exception as e:
        result["errors"].append(f"backup: {type(e).__name__}")
    try:
        maintenance.rotate_log()
    except Exception as e:
        result["errors"].append(f"log_rotate: {type(e).__name__}")
    try:
        pruned = maintenance.prune_attachments(DB)
        if pruned:
            result["attachments_pruned"] = pruned
    except Exception as e:
        result["errors"].append(f"prune: {type(e).__name__}")


def _finish_run(ledger, cfg, result, run_id, deadline) -> str:
    """Tail drain, run record and snapshot publish — returns the final
    run status for the health write."""
    if result.get("notify", {}).get("failed") \
            or result.get("notify", {}).get("skipped"):
        result["errors"].append("notify_incomplete")

    result["ok"] = True
    import notify_cards
    import notify_cmds
    try:
        # last-chance drain: interactions queued while this tick ran
        notify_cmds.drain_int_commands(
            ledger, result, cfg, os.path.join(HOME, "data"),
            deadline=deadline, limit=16)
        notify_cards.publish_flags(cfg, os.path.join(HOME, "data"))
    except Exception as e:
        result["errors"].append(f"cmd_int_tail: {type(e).__name__}")
    # Tail command failures must be included in the recorded run
    # before its read-only snapshot is published.
    status = "ok" if not result["errors"] and not result["incomplete"] \
        else "partial"
    ledger.finish_run(run_id, status,
                      "; ".join(result["errors"][:8]))
    try:
        result["snapshot"] = maintenance.publish_snapshot(DB)
        if result["snapshot"]:
            notify_cards.clear_snapshot_dirty(ledger)
        else:
            raise RuntimeError("snapshot_verify_failed")
    except Exception as e:
        result["errors"].append(f"snapshot: {type(e).__name__}")
        ledger.finish_run(run_id, "partial",
                          "; ".join(result["errors"][:8]))
        status = "partial"
    return status


def _fail_run(ledger, args, result, run_id, status, detail,
              deadline, alert):
    """Shared failed-run epilogue — record the run, append the detail
    to errors, then best-effort alert + pending flush. A failed alert
    must never mask the original failure; the caller still writes
    health and prints the result."""
    ledger.finish_run(run_id, status, detail)
    result["errors"].append(detail)
    try:  # operational alert — silent death is worse than noise
        if alert == "session":
            _alert_session_expired(ledger, run_id, detail)
        elif alert:
            ledger.outbox_add("run_failed", None,
                              {"run_id": run_id, "detail": detail})
        # alert=None: the session_recovered notice was already queued by
        # the relogin attempt — the flush below still delivers it
        if not args.no_notify:  # --no-notify suppresses ALL sends;
            result["notify"] = notify_flush.flush(ledger, deadline=deadline)
    except Exception:                                    # queued for a
        pass                                             # later flush


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--mark-read", action="store_true",
                    help="acknowledge fetched unreads (snapshot-gated)")
    ap.add_argument("--download-files", action="store_true")
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--jobs-only", action="store_true",
                    help="skip unread/backfill; only drain durable jobs "
                         "(used by the idle-time deep-import agent)")
    ap.add_argument("--commands-only", action="store_true",
                    help="drain cmd/cmd_int command traffic only — no "
                         "adapter, network, ingest, derive or notify; "
                         "the interactive-card watcher job uses this")
    args = ap.parse_args()

    os.makedirs(os.path.join(HOME, "data"), exist_ok=True)
    os.makedirs(ATTACH_DIR, mode=0o700, exist_ok=True)
    os.chmod(ATTACH_DIR, 0o700)

    # a scheduled tick waits briefly for the lock: the nightly semantic
    # drain releases it between batches, and a single lost night tick
    # already spans 40 min — past the 30-min session expiry. The
    # command watcher stays non-blocking (it runs every few seconds).
    lock_fd = acquire_run_lock(LOCKFILE)
    if lock_fd is None and not args.commands_only:
        lock_fd = _wait_run_lock(LOCK_WAIT_S)
    if lock_fd is None:
        print(json.dumps({"ok": False, "error": "lock_held"}))
        return 3

    deadline = time.monotonic() + RUN_DEADLINE_S
    if args.commands_only:
        return _commands_only(lock_fd, deadline)
    import notify_cards as _notify_cards
    if _notify_cards.restore_awaiting_consent(
            os.path.join(HOME, "data")) is not None:
        # schema-bump DB replace held for human consent — ingest,
        # derive, reconcile and sends all stay frozen so the approved
        # loss report cannot drift; only the consent command drains
        return _consent_only(lock_fd)
    adapter = MCSAdapter(token_cache=CACHE)
    adapter.set_deadline(deadline)
    try:
        ledger = Ledger(DB)
    except Exception:
        os.close(lock_fd)
        print(json.dumps({"ok": False, "error": "ledger_init_failed"}))
        return 1
    started = time.time()
    run_id = None
    result = {"run_id": run_id, "ok": False, "projects": 0, "messages": 0,
              "new_messages": 0, "backfilled": 0, "incomplete": [],
              "marked_read": [], "notify": {}, "errors": []}
    try:
        run_id = ledger.begin_run(
            None, kind="deep" if args.jobs_only else "tick")
        result["run_id"] = run_id
        cfg = _config()
        sem_on = _semantic_enabled(ledger, cfg, result)
        notify_max_age_s = _notify_max_age_s(cfg, result)
        _stage_fetch(adapter, ledger, args, cfg, result, deadline,
                     run_id, sem_on, notify_max_age_s)
        _run_jobs(adapter, ledger, args, cfg, result, deadline, sem_on,
                  notify_max_age_s)

        # -- derived data ----------------------------------------------
        # jobs-only runs skip fetch entirely, so the LLM extract slice
        # can be wider than the 15-min tick's — still capped well under
        # RUN_DEADLINE_S so history/trickle stages keep their share.
        stage_derive(ledger, result, deadline, cfg,
                     llm_budget_cap=240 if args.jobs_only else 90)

        _deliver(ledger, args, cfg, result, deadline)
        _run_semantic(ledger, args, cfg, result, deadline, sem_on)
        _housekeeping(result)
        status = _finish_run(ledger, cfg, result, run_id, deadline)
        _write_health(ledger, result, status, run_id=run_id)
    except SessionExpired as e:
        detail = _err_str(e)
        recovered = False
        if "auto_login=" not in detail:
            # an expiry escaping an unwrapped path still earns one
            # recovery attempt before a manual-login alert goes out;
            # the wrapped stages already tried (their detail carries
            # 'auto_login=<state>'), so don't double-attempt them.
            # _attempt_relogin enforces the run budget, so a capped run
            # gets 'auto_login=budget_exhausted' with no adapter call.
            state = _attempt_relogin(adapter, ledger, result, "run", e)
            detail = f"{detail} auto_login={state}"
            recovered = state == "ok"
        _fail_run(ledger, args, result, run_id, "session_expired",
                  detail, deadline,
                  alert=None if recovered else "session")
        _write_health(ledger, result, "session_expired", run_id=run_id)
        print(json.dumps(result, ensure_ascii=False))
        return 2
    except MCSError as e:
        _fail_run(ledger, args, result, run_id, "failed", _err_str(e),
                  deadline, alert="run_failed")
        _write_health(ledger, result, "failed", run_id=run_id)
        print(json.dumps(result, ensure_ascii=False))
        return 1
    except Exception as e:
        # outermost boundary — non-MCSError crashes (AttributeError, sqlite,
        # ...) must still record a failed run and alert, not die silently
        with suppress(Exception):
            _fail_run(ledger, args, result, run_id, "failed",
                      f"crash: {type(e).__name__}", deadline,
                      alert="run_failed")
        _write_health(ledger, result, "failed", run_id=run_id)
        print(json.dumps(result, ensure_ascii=False))
        return 1
    finally:
        try:
            ledger.close()
        finally:
            os.close(lock_fd)

    result["elapsed_s"] = round(time.time() - started, 1)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
