"""Bounded collection stages, durable cursors, acknowledgements and session replay.

The caller owns the adapter, Ledger lifetime and run result. Each stage keeps
its existing save/job/ack transaction boundaries; no encompassing transaction
spans collection HTTP, replay, or publication. Paths bind here from mcs_util;
CLI orchestration owns ordering, run locks, generation checks and watchdogs.
"""
import errno
import json
import os
import re
import time
from types import SimpleNamespace

from mcs_contract import MCSError, SessionExpired, describe_error
from message_observations import METADATA_SHADOW_BACKOFF_S
from health_policy import COVERAGE_STALL_S
from mcs_util import ATTACH_DIR, CHROME_BIN, CHROME_PROFILE, loads_dict
import health_report
from stats_math import finite_number
import job_ops
import message_observations
import stamp_read

BACKFILL_MAX_PAGES = 3        # per patient, per run — newest-first walk
BACKFILL_OVERLAP_S = 120      # re-scan window; dedup handles repeats
KARTE_SUMMARY_TICK_CAP = 12   # 連携サマリー GETs per run; the rest carry over
KARTE_SUMMARY_FILL = 10       # missing/stale summaries filled per deep run
KARTE_SUMMARY_MARGIN_S = 60   # keep the tail of the deadline for semantic/finish
ALERT_MIN_INTERVAL_S = 6 * 3600
# Each wrapped stage tries once; the same run result bounds all attempts.
RELOGIN_MAX_PER_RUN = 3
# A collection outage makes newly stored, already-read posts notify on catchup.
OUTAGE_CATCHUP_S = 1200
_REPLAYED_LISTS = ("errors", "incomplete", "coverage_gaps",
                   "coverage_stalled", "self_probe_fetched")
_ENTRY_PID = re.compile(r"(?:project|mark|backfill|probe) (\d+): ")
THREAD_READ_WINDOW_S = 7 * 86400   # stored replies this recent qualify
THREAD_READ_MAX = 10               # threads checked per tick (MCS load)
WALK_CURSOR_KIND = "patient_walk_cursor_v1"
SELF_PROBE_PAGES = 2          # bounded history fetch per positive probe
SELF_PROBE_MARGIN_S = 60      # keep the tail of the deadline for notify
REACTION_ACTORS_TICK_CAP = 4


def queue_run_alert(ledger, kind: str, run_id: int, detail: str) -> bool:
    """Enqueue a system alert — throttled so a persistent condition does
    not re-alert on every tick: one per kind per ALERT_MIN_INTERVAL_S
    keeps it visible without flooding the channel. The run row and
    health file still record every occurrence; only the notification
    is gated."""
    last = ledger.db.execute(
        "SELECT MAX(created_at) t FROM notify_outbox"
        " WHERE kind=?", (kind,)).fetchone()["t"]
    if last is not None \
            and time.time() - float(last) < ALERT_MIN_INTERVAL_S:
        return False
    ledger.outbox_add(kind, None, {"run_id": run_id, "detail": detail})
    return True


def queue_session_expired(ledger, run_id: int, detail: str) -> bool:
    """Enqueue the session_expired alert (throttled per kind)."""
    return queue_run_alert(ledger, "session_expired", run_id, detail)


def _alert_session_recovered(ledger, run_id: int, detail: str) -> None:
    """Enqueue the session_recovered notice — NOT throttled: it resolves
    a possibly-visible session_expired alert, so suppressing it would
    leave a stale 'manual re-login required' standing in the channel.
    Volume stays bounded in practice because a session that flaps past
    RELOGIN_MAX_PER_RUN aborts the run instead of recovering again."""
    ledger.outbox_add("session_recovered", None,
                      {"run_id": run_id, "detail": detail})


def attempt_relogin(adapter, ledger, result, where: str,
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
    attempt = {"stage": where, "error": describe_error(err)}
    attempts.append(attempt)
    try:
        state = adapter.auto_login(profile_dir=CHROME_PROFILE,
                                   chrome_bin=CHROME_BIN)
    except Exception as exc:
        state = "failed"
        attempt["detail"] = type(exc).__name__
    attempt["state"] = state
    for key in ("healed_tabs", "chrome_restarted"):
        if getattr(adapter, key, 0):
            attempt[key] = getattr(adapter, key)
    if state == "ok":
        _alert_session_recovered(ledger, result.get("run_id"),
                                 f"{where}: {attempt['error']}")
    return state


def outage_since(now: float | None = None) -> float | None:
    """When unread collection last completed, if that is longer ago than
    OUTAGE_CATCHUP_S — else None. unread_at only advances on runs that
    collected unread, so every outage cause (and a run that recovered
    the session only at its boundary) keeps the gap open until a
    collecting run succeeds."""
    now = time.time() if now is None else now
    unread_at = health_report.prev_health().get("unread_at")
    if not finite_number(unread_at) or now - unread_at <= OUTAGE_CATCHUP_S:
        return None
    return unread_at


def _seed_notifying_head(ledger, pid: int, since: float) -> None:
    """Queue a bounded history_head walk that notifies every row it stores.

    A pending head job keeps its own `since` and only gains the flag."""
    ledger.job_add("history_head", pid, payload={
        "since": max(0, int(since)), "page": 1,
        "pages": BACKFILL_MAX_PAGES, "trickle": False, "notify": True})
    ledger.job_set_flag("history_head", pid, 0, "notify")


def seed_outage_catchup(ledger, result, since: float) -> None:
    """Queue the bounded notifying history_head walk _post_ack_gap uses
    for every active patient — it finishes what this run's backfill did
    not reach. Cheap job rows only; the fetches run in the normal job
    drains, and notify_max_age_s still bounds what is announced. A
    pending head job keeps its own `since` and only gains the flag."""
    seeded = 0
    for row in ledger.frontier_patients():
        pid = row["project_id"]
        _seed_notifying_head(ledger, pid, min(
            since, ledger.coverage_ts(pid) or ledger.high_watermark(pid)
            or since) - BACKFILL_OVERLAP_S)
        seeded += 1
    result["outage_catchup"] = seeded


def _entry_pid(entry) -> int | None:
    """The patient a walk record names — a pid, a {'pid': …} entry or a
    '<stage> <pid>: …' error — or None for a run-wide record."""
    if isinstance(entry, dict):
        return entry.get("pid")
    if isinstance(entry, int):
        return entry
    m = _ENTRY_PID.match(entry) if isinstance(entry, str) else None
    return int(m.group(1)) if m else None


def _restore_unobserved(where: str, result, aborted: dict, counted: dict,
                        replay) -> None:
    """Re-add the aborted attempt's records that the replay cannot
    observe again. unread: a patient the replay no longer lists (the
    aborted attempt acknowledged it, or its mark landed before the
    response failed) keeps its errors, incomplete entry and message
    count. backfill: the aborted attempt's job_add revived the stalled
    history_head, so its coverage_stalled record stays unless the
    replay recorded that patient's stall itself."""
    if where == "unread":
        seen = {p.project_id for p in getattr(replay, "patients", ())}
        unseen = sum(n for pid, n in counted.items() if pid not in seen)
        if unseen:
            result["messages"] += unseen

        def keep(key, entry):
            pid = _entry_pid(entry)
            return pid is not None and pid not in seen
    elif where == "backfill":
        stalled = {g["pid"] for g in result.get("coverage_stalled", ())}

        def keep(key, entry):
            return (key == "coverage_stalled"
                    or ": coverage_stalled_" in str(entry)) \
                and _entry_pid(entry) not in stalled
    elif where == 'self_probe':
        def keep(key, entry):
            return False
    else:
        return
    for key, entries in aborted.items():
        for entry in entries:
            # A later listing of the patient cannot prove that a failed
            # write's original messages were fetched again.
            unstored = (key == 'errors' and isinstance(entry, str)
                        and ': semantic_intake_' in entry)
            if (keep(key, entry) or unstored) and entry not in result.get(key, ()):
                result.setdefault(key, []).append(entry)
    for entry in aborted.get('errors', ()):
        if isinstance(entry, str) and ': semantic_intake_' in entry:
            pid = _entry_pid(entry)
            if pid is not None and pid not in result.setdefault('incomplete', []):
                result['incomplete'].append(pid)


def with_relogin(adapter, ledger, result, where: str, fn, *args,
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
    # walk-derived lists the replay records again; save-derived counters
    # (new_messages, backfilled, realtime_ids) stay — the deduped saves
    # add nothing for rows the aborted attempt already stored
    sizes = {k: len(result[k]) for k in _REPLAYED_LISTS if k in result}
    msgs = result.get("messages")
    try:
        return fn(*args, **kwargs)
    except SessionExpired as e:
        if getattr(ledger.db, "in_transaction", False):
            raise
        if "auto_login=" in (e.detail or ""):
            raise
        state = attempt_relogin(adapter, ledger, result, where, e)
        if state == "budget_exhausted":
            # cap spent — the raw failure escalates undecided; the run
            # boundary's own attempt is declined the same way and
            # reports 'auto_login=budget_exhausted'
            raise
        if state != "ok":
            raise SessionExpired(f"auto_login={state}") from e
        # these stages re-walk everything, so the replay re-records them;
        # drop the aborted attempt's walk records and messages, then put
        # back what the replay cannot observe again. Job drains are
        # excluded: a job failed or deferred by the aborted attempt is
        # not due on replay, so its recorded error must stay.
        aborted, counted = {}, result.pop("_unread_counted", {})
        if where in ("unread", "self_probe", "backfill"):
            for key in _REPLAYED_LISTS:
                if key in sizes:
                    aborted[key] = result[key][sizes[key]:]
                    del result[key][sizes[key]:]
                else:
                    aborted[key] = result.pop(key, [])
            if msgs is not None:
                result["messages"] = msgs
        try:
            replay = fn(*args, **kwargs)
        except SessionExpired as e2:
            raise SessionExpired(
                "auto_login=ok_then_expired") from e2
        _restore_unobserved(where, result, aborted, counted, replay)
        return replay
    finally:
        # stage_unread's per-patient tally is replay bookkeeping only
        result.pop("_unread_counted", None)


def _cap_cleared(adapter, ledger, p, result, top: bool = False) -> bool:
    """F-5 acknowledgement gate for an unread-capped patient: the
    history walk must have certified down to the server's oldest
    unread (ledger.unread_cap_cleared) and, when nothing this tick
    fetched the top of the range (``top``), the server's newest message
    must already be stored. Anything less keeps the patient incomplete
    and seeds the walk once — or, when the full walk already completed
    (floor -1), reports unread_cap_stuck instead of re-walking the
    whole timeline every tick."""
    oldest = adapter.oldest_unread_id(p.project_id)
    ok = oldest is not None and ledger.unread_cap_cleared(
        p.project_id, oldest)
    if ok and top:
        latest = adapter.fetch_latest(p.project_id)["message_id"]
        ok = latest is None or ledger.has_message(latest)
    if not ok:
        p.fetch_state = "incomplete"
        p.fetch_reason = "unread_capped"
        if ledger.history_floor(p.project_id) == -1:
            result["errors"].append(
                f"project {p.project_id}: unread_cap_stuck")
        elif not ledger.history_job(p.project_id):
            ledger.job_add("history", p.project_id, payload={
                "since": 0, "page": 1, "pages": 10})
    return ok


def _post_ack_gap(adapter, ledger, pid: int, result):
    """After any acknowledgement, verify the server's newest message is
    stored. The read clears the project flag, so a post that landed
    after the unread snapshot would arrive read and unnotified —
    seed a bounded history_head walk and record post_ack_gap instead of
    letting it disappear. An unanswerable probe counts as a gap."""
    try:
        latest = adapter.fetch_latest(pid)["message_id"]
        gap = latest is not None and not ledger.has_message(latest)
    except MCSError:
        gap = True
    if not gap:
        return
    # notify: the rows it finds arrive already read — the walk must
    # offer every new row, merged into an already-pending head job too
    _seed_notifying_head(ledger, pid, (ledger.coverage_ts(pid)
                                       or ledger.high_watermark(pid))
                         - BACKFILL_OVERLAP_S)
    result["errors"].append(f"mark {pid}: post_ack_gap")


def stage_unread(adapter, ledger, result, deadline, run_id, *,
                 mark_read: bool = False, semantic: bool = False,
                 notify_max_age_s: float | None = None, ack_exclusions=None):
    """list_unread -> per-patient unread fetch -> reply merge -> save
    (notify intent in the same tx) -> optional gated mark-read.
    notify_max_age_s: unread messages older than this are imported
    without notification (bulk-added patients surface as unread with
    months-old history). Returns the snapshot for downstream stages."""
    # credential-free recovery at the failure point; a re-expiry right
    # after this auto_login carries the marker, so the outer wrapper
    # does not spend a second attempt re-running the whole stage
    snap = with_relogin(adapter, ledger, result, "unread",
                         adapter.list_unread)
    ledger.db.execute("UPDATE runs SET snapshot_ts=? WHERE run_id=?",
                      (snap.timestamp, run_id))
    ledger.db.commit()
    result["projects"] = len(snap.patients)
    result["snapshot_ts"] = snap.timestamp

    # /projects already lists read rooms too; new invitations need no extra scan.
    known = {r["project_id"] for r in ledger.frontier_patients()}
    known_groups = None
    for p in snap.listed_patients:
        if p.project_id in known:
            project_type = getattr(p, "project_type", None)
            refresh_group = project_type == "group" and (
                getattr(p, "group_id_present", False) or getattr(p, "group_name_present", False))
            other_type = isinstance(project_type, str) and bool(project_type.strip()) and project_type != "group"
            if other_type and known_groups is None:
                if time.monotonic() > deadline:
                    result["errors"].append("deadline_exceeded")
                    break
                try:
                    known_groups = {r["project_id"] for r in ledger.db.execute(
                        "SELECT project_id FROM patients WHERE is_archived=0 AND project_type='group'")}
                except Exception:
                    result["errors"].append(f"project {p.project_id}: db_write_failed")
                    known_groups = set()
                    continue
            if refresh_group or other_type and p.project_id in known_groups:
                if time.monotonic() > deadline:
                    result["errors"].append("deadline_exceeded")
                    break
                try:
                    ledger.update_group_metadata(p)
                except Exception:
                    result["errors"].append(f"project {p.project_id}: db_write_failed")
            continue
        if time.monotonic() > deadline:
            result["errors"].append("deadline_exceeded")
            break
        try:
            ledger.upsert_patient_info(p, is_archived=False, notify_new=True,
                                       observation_ts=snap.timestamp)
            known.add(p.project_id)
        except Exception:
            result["errors"].append(f"project {p.project_id}: db_write_failed")

    for index, p in enumerate(snap.patients):
        if time.monotonic() > deadline:
            result["errors"].append("deadline_exceeded")
            # never fetched this tick: incomplete, not 'nothing unread'
            result["incomplete"].extend(
                q.project_id for q in snap.patients[index:]
                if q.project_id and q.project_id not in result["incomplete"])
            break
        batch = None
        session_error = None
        try:
            batch = adapter.fetch_unread_messages(p.project_id, snap.timestamp)
            p.messages = batch.messages
            for m in p.messages:
                if m.body_state not in ("full", "deleted"):
                    # An unreceived or truncated parent has no dedicated fetch
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
            if batch.error and batch.error.kind == "http_error" \
                    and 400 <= (batch.error.status or 0) < 500 \
                    and not p.messages:
                # F-5: the unread route rejects a project past the screen
                # cap. The rows still arrive through the self probe and
                # the history walk; acknowledge only once that walk has
                # certified everything down to the server's oldest
                # unread — otherwise stay incomplete and keep walking.
                # Nothing in this branch fetched the TOP of the range,
                # so the server's newest message must be stored too:
                # the plain list read clears the whole project flag and
                # a row imported later by the walk arrives read (never
                # notified).
                p.ack_fallback = _cap_cleared(adapter, ledger, p, result,
                                              top=True)
            elif batch.error:
                p.fetch_state = "incomplete"
                p.fetch_reason = batch.error.kind
                if batch.error.kind == "pages_exceeded" \
                        and ledger.history_floor(p.project_id) != -1 \
                        and not ledger.history_job(p.project_id):
                    # more unread than the page cap: the tail is
                    # unreachable via the unread list — hand the
                    # backlog to the durable cursor walk (F03)
                    ledger.job_add("history", p.project_id, payload={
                        "since": 0, "page": 1, "pages": 10})
            elif batch.capped:
                # F-5: the unread screen caps at UNREAD_SCREEN_CAP rows —
                # older unread rows are invisible here and mark-as-read
                # would clear them unfetched. Stay incomplete until the
                # history walk has stored down to the server's oldest
                # unread message; only then is acknowledgement safe.
                _cap_cleared(adapter, ledger, p, result)
            if p.fetch_state != "incomplete":
                p.fetch_state = "complete"
        except SessionExpired:
            raise  # auth failure must abort the run, not look partial
        except MCSError as e:
            p.fetch_state = "incomplete"
            p.fetch_reason = e.kind
        except Exception as exc:
            import semantic_intake_hold
            if not semantic_intake_hold.report_write_failure(
                    ledger, result, p.project_id, exc, ack_exclusions,
                    messages=getattr(p, 'messages', None)) or ledger.db.in_transaction:
                raise
            result['errors'].append(f'project {p.project_id}: db_write_failed')
            continue
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
                observation_ts=snap.timestamp,
                notify_max_age_s=notify_max_age_s)
        except Exception as e:
            if ack_exclusions is not None:
                ack_exclusions.add(p.project_id)
            if p.project_id not in result['incomplete']:
                result['incomplete'].append(p.project_id)
            import semantic_intake_hold
            hold_failed = semantic_intake_hold.report_write_failure(
                    ledger, result, p.project_id, e, ack_exclusions, messages=p.messages)
            if not hold_failed:
                ledger.patient_fetch_failed(p.project_id, type(e).__name__)
            result["errors"].append(
                f"project {p.project_id}: db_write_failed")
            if hold_failed and session_error:
                raise session_error
            continue
        result["messages"] += len(p.messages)
        # per patient, for a relogin replay that no longer lists it
        result.setdefault("_unread_counted", {})[p.project_id] = \
            len(p.messages)
        result["new_messages"] += len(new_ids)
        result.setdefault("realtime_ids", []).extend(new_ids)
        import semantic_intake_hold
        unstored = semantic_intake_hold.has_unstored(ledger.db, p.project_id)
        if unstored:
            if p.project_id not in result['incomplete']:
                result['incomplete'].append(p.project_id)
            result['errors'].append(f'project {p.project_id}: semantic_intake_unstored')
        if new_ids:
            ledger.karte_summary_mark_due(p.project_id)

        if session_error:
            raise session_error
        if isinstance(getattr(batch, "error", None), SessionExpired):
            raise batch.error

        if (mark_read and p.fetch_state == "complete"
                and not unstored
                and (ack_exclusions is None or p.project_id not in ack_exclusions)):
            if ledger.was_marked(p.project_id, snap.timestamp):
                continue
            # record the intent BEFORE sending — a crash between send
            # and record must read as unknown, never as confirmed
            ledger.mark_read(p.project_id, snap.timestamp, "unknown")
            try:
                adapter.mark_patient_read(p.project_id, snap.timestamp,
                                          fallback_plain=p.ack_fallback)
                ledger.mark_read(p.project_id, snap.timestamp, "confirmed")
                result["marked_read"].append(p.project_id)
                # strict acks too: a post landing between list_unread and
                # this GET is cleared server-side without being stored
                _post_ack_gap(adapter, ledger, p.project_id, result)
            except MCSError as e:
                result["errors"].append(
                    f"mark {p.project_id}: {describe_error(e)}")
    return snap


def stage_thread_read(adapter, ledger, result, deadline, *, exclude_projects=()):
    """Acknowledge stored thread replies, queue gaps, and confirm read status."""
    done = result.setdefault("threads_marked_read", [])
    for pid, parent, last in ledger.thread_read_candidates(
            time.time() - THREAD_READ_WINDOW_S, THREAD_READ_MAX,
            exclude_projects=exclude_projects):
        if time.monotonic() > deadline - 30:
            result["errors"].append("deadline_exceeded")
            break
        try:
            if not adapter.thread_unread(pid, parent):
                ledger.mark_thread_read(pid, parent, last, "confirmed")
                continue
            server = {m.message_id for m in adapter.fetch_thread(pid, parent)}
            missing = server - ledger.stored_reply_ids(parent, project_id=pid)
            # A reply stored under another patient cannot be re-owned by a
            # refetch (saves never move project_id): queueing it would revive
            # the done job every tick. It is not this patient's stored reply
            # either, so the thread is never cleared while it remains.
            foreign = {rid for rid, owner in ledger.message_owners(missing).items()
                       if owner != pid}
            missing -= foreign
            if foreign:
                result["errors"].append("thread_read: reply_stored_for_other_patient")
            if missing:
                # Store full replies before acknowledging the thread.
                # never revive a burnt-out job: this runs every tick and
                # would reset its attempts forever (same as save_thread_replies)
                for rid in sorted(missing):
                    ledger.job_add("reply", pid, rid, parent_id=parent,
                                   revive_failed=False)
                # a burnt-out reply never settles this thread: make it
                # visible instead of retrying silently every tick
                if any(ledger.job_state("reply", pid, rid) == "failed"
                       for rid in missing):
                    result["errors"].append("thread_read: reply_failed")
            if missing or foreign:
                continue
            ledger.mark_thread_read(pid, parent, last, "unknown")
            seen = set()
            try:
                seen = adapter.read_thread(pid, parent)
            except MCSError as error:
                seen = set(error.observed_reply_ids)
                raise
            finally:
                # Successful pages may already have cleared replies even
                # when a later page failed. Keep their observed IDs durable;
                # the thread mark remains unknown until the whole read succeeds.
                for rid in sorted(seen - server):
                    ledger.job_add("reply", pid, rid, parent_id=parent,
                                   payload={"unread": True},
                                   revive_failed=False)
                    ledger.job_set_flag("reply", pid, rid, "unread")
            if not adapter.thread_unread(pid, parent):
                ledger.mark_thread_read(pid, parent, last, "confirmed")
                done.append(parent)
        except SessionExpired:
            raise
        except MCSError as e:
            result["errors"].append(f"thread_read: {describe_error(e)}")


def walk_order(ledger, stage: str) -> list:
    """This stage's patient walk, starting where its last deadline stopped.
    Run ids are shared with deep and other runs, so they cannot order a
    walk; the stage's own stored cursor (one artifact row) does."""
    rows = list(ledger.frontier_patients())
    cur = ledger.db.execute(
        "SELECT content FROM artifacts WHERE kind=? AND json_valid(content) "
        "AND json_valid(meta) AND json_extract(meta,'$.stage')=? "
        "ORDER BY artifact_id DESC LIMIT 1", (WALK_CURSOR_KIND, stage)).fetchone()
    nxt = (loads_dict(cur[0]) or {}).get("next") if cur else None
    if not rows or type(nxt) is not int:
        return rows
    k = next((i for i, r in enumerate(rows) if r["project_id"] >= nxt), 0)
    return rows[k:] + rows[:k]


def _save_walk(ledger, stage: str, nxt) -> None:
    """Store where the next tick of this stage starts (one row per stage)."""
    if type(nxt) is not int:
        return
    content = json.dumps({"next": nxt})
    row = ledger.db.execute(
        "SELECT artifact_id, content FROM artifacts WHERE kind=? AND json_valid(meta) "
        "AND json_extract(meta,'$.stage')=? ORDER BY artifact_id DESC LIMIT 1",
        (WALK_CURSOR_KIND, stage)).fetchone()
    if row and row[1] == content:
        return
    with ledger.db:
        if row:
            ledger.db.execute("UPDATE artifacts SET content=?, created_at=? WHERE artifact_id=?",
                              (content, time.time(), row[0]))
        else:
            ledger.artifact_add_tx(WALK_CURSOR_KIND, content, meta={"stage": stage})


def stage_backfill(adapter, ledger, result, deadline, run_id,
                   semantic: bool = False,
                   notify_max_age_s: float | None = None,
                   notify_all_new: bool = False,
                   notify_after: dict | None = None, ack_exclusions=None):
    """Catch posts the unread API misses (e.g. read by another human).
    notify_all_new (the first collecting run after an outage) announces
    every newly stored row, not only unread ones. notify_after maps a
    pid to the cutoff of this run's page-limited self-post probe: new
    rows posted after it notify like the probe's own.
    Walk each patient's history down to CONFIRMED coverage — never the
    newest stored message, so storing a new unread cannot skip older
    unfetched items (Oracle B06). Coverage only advances on a complete
    walk; a gap stays visible via coverage_lag."""
    order = walk_order(ledger, "backfill")
    nxt = None
    for row in order:
        pid = row["project_id"]
        if time.monotonic() > deadline - 30:
            result["errors"].append("deadline_exceeded")
            nxt = pid   # the next tick starts at the first patient not reached
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
                f"backfill {pid}: {describe_error(e)}")
            continue
        hist = batch.messages
        try:
            merged = job_ops.merge_full_replies(
                adapter, hist, 0, deadline, result, ledger=ledger)
        except Exception as exc:
            import semantic_intake_hold
            if not semantic_intake_hold.report_write_failure(
                    ledger, result, pid, exc, ack_exclusions, messages=hist) or ledger.db.in_transaction:
                raise
            continue
        try:
            new_ids = ledger.save_messages(
                hist, project_id=pid,
                notify={"run_id": run_id, "source": "history"},
                semantic=semantic, notify_max_age_s=notify_max_age_s,
                notify_all_new=notify_all_new,
                notify_new_after_ts=(notify_after or {}).get(pid))
        except Exception as exc:
            import semantic_intake_hold
            if not semantic_intake_hold.report_write_failure(
                    ledger, result, pid, exc, ack_exclusions, messages=hist):
                raise
            if merged.error:
                raise merged.error
            if batch.error:
                result['errors'].append(f'backfill {pid}: {describe_error(batch.error)}')
                if isinstance(batch.error, SessionExpired):
                    raise batch.error
            continue
        if new_ids:
            result["backfilled"] += len(new_ids)
        if pid in (notify_after or {}) and not batch.reached \
                and not batch.error:
            # rows past this walk stay unobtained — and would be stored
            # silently by a later tick — so surface it
            result["errors"].append(f"probe {pid}: history_incomplete")
        if merged.error:
            raise merged.error
        if batch.error:
            result["errors"].append(
                f"backfill {pid}: {describe_error(batch.error)}")
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
            lag = ledger.coverage_lag(pid)
            # read BEFORE job_add — it revives a failed row
            stall = _head_stall(ledger, pid) if lag > 0 else None
            # A bounded frontier read is not a completed history walk.
            # Reserve its tail independently of the deep-import floor.
            ledger.job_add("history_head", pid, payload={
                "since": max(0, cutoff),
                "page": 1 + batch.pages if merged.checkpoint_safe else 1,
                "pages": BACKFILL_MAX_PAGES, "trickle": False})
            if lag > 0:
                # A gap between the stored head and verified coverage
                # is information, not an error: a patient with more
                # than BACKFILL_MAX_PAGES of history certifies only
                # when history_head finishes its full rescan, so a lag
                # of hours to days is normal. Only a head job that
                # failed or stopped advancing is a collection fault.
                result.setdefault("coverage_gaps", []).append(
                    {"pid": pid, "lag_s": lag})
                if stall:
                    result.setdefault("coverage_stalled", []).append(
                        {"pid": pid, "lag_s": lag, "reason": stall})
                    result["errors"].append(
                        f"backfill {pid}: coverage_stalled_{stall}")
    # only a deadline moves the cursor; a full walk or an exception leaves it as it was
    _save_walk(ledger, "backfill", nxt)


def _head_stall(ledger, pid: int) -> str | None:
    """Stall evidence for a patient's history_head: 'failed' (attempts
    exhausted / window stalled) or 'not_advancing' (pending but neither
    worked nor deferred — every checkpoint bumps updated_at — for
    COVERAGE_STALL_S). None while it is making progress."""
    row = ledger.db.execute(
        "SELECT state,updated_at FROM fetch_jobs WHERE kind='history_head'"
        " AND project_id=? AND message_id=0", (pid,)).fetchone()
    if row is None:
        return None
    if row["state"] == "failed":
        return "failed"
    if row["state"] == "pending" \
            and time.time() - (row["updated_at"] or 0) > COVERAGE_STALL_S:
        return "not_advancing"
    return None


def stage_self_probe(adapter, ledger, result, deadline, run_id,
                     semantic: bool = False,
                     notify_max_age_s: float | None = None, ack_exclusions=None):
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
    order = walk_order(ledger, "self_probe")
    nxt = None
    for row in order:
        pid = row["project_id"]
        wm = ledger.high_watermark(pid)
        if not wm:
            continue  # never imported — the durable history jobs own it
        if time.monotonic() > deadline - SELF_PROBE_MARGIN_S:
            result["errors"].append("self_probe: deadline_exceeded")
            nxt = pid
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
        # overlap the watermark: fetch_history drops items with ts <=
        # since, and a same-second post would be skipped then marked
        since = max(1, wm - BACKFILL_OVERLAP_S)
        try:
            batch = adapter.fetch_history(
                pid, since, max_pages=SELF_PROBE_PAGES)
        except SessionExpired:
            raise
        except MCSError as e:
            result["errors"].append(f"probe {pid}: {e.kind}")
            continue
        try:
            merged = job_ops.merge_full_replies(
                adapter, batch.messages, 0, deadline, result, ledger=ledger)
        except Exception as exc:
            import semantic_intake_hold
            if not semantic_intake_hold.report_write_failure(
                    ledger, result, pid, exc, ack_exclusions, messages=batch.messages) or ledger.db.in_transaction:
                raise
            result.setdefault('self_probe_after', {})[pid] = since
            continue
        src = "self" if probe["is_self_only"] else "probe"
        try:
            new_ids = ledger.save_messages(
                batch.messages, project_id=pid, semantic=semantic,
                notify={"run_id": run_id, "source": src},
                notify_max_age_s=notify_max_age_s, notify_all_new=True)
        except Exception as exc:
            import semantic_intake_hold
            if not semantic_intake_hold.report_write_failure(
                    ledger, result, pid, exc, ack_exclusions, messages=batch.messages):
                raise
            # Preserve the probe's notification policy if backfill retries.
            result.setdefault('self_probe_after', {})[pid] = since
            if merged.error:
                raise merged.error
            if batch.error:
                result['errors'].append(f'probe {pid}: {batch.error.kind}')
                if isinstance(batch.error, SessionExpired):
                    raise batch.error
            continue
        if new_ids:
            result["new_messages"] += len(new_ids)
            result.setdefault("realtime_ids", []).extend(new_ids)
            ledger.karte_summary_mark_due(pid)
        result.setdefault("self_probe_fetched", []).append(pid)
        if not batch.reached:
            # page-limited: this run's backfill stores the rest and must
            # keep the probe's notify policy, or the next tick sees the
            # latest id stored and the remainder is never announced
            result.setdefault("self_probe_after", {})[pid] = since
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
    _save_walk(ledger, "self_probe", nxt)
    result["self_probe"] = probed


def stage_attachments(adapter, ledger, result, deadline, semantic=False):
    # attachments referenced by queued notify events jump the queue —
    # otherwise a deep backlog leaves new-message files undownloaded
    # when flush() posts the event, and accepted events never re-send
    priority = ledger.pending_notify_message_ids()
    due = ledger.attachments_due(limit=30, priority_mids=priority)
    # disk guard: under pressure downloads stay pending (no attempt
    # spent) instead of burning into a permanent 'failed' state
    if due and health_report.attachment_disk_low(health_report.attachment_free_mb(ATTACH_DIR)):
        result["attachments_deferred"] = len(due)
        return
    for i, a in enumerate(due):
        if time.monotonic() > deadline - 20:
            break
        dest = os.path.join(ATTACH_DIR, str(a["attachment_id"]))
        try:
            info = adapter.download(a["url"], dest)
            ledger.attachment_saved(a["attachment_id"], dest,
                                    info["bytes"], info["sha256"], semantic=semantic)
        except (MCSError, OSError) as e:
            if getattr(e, "kind", None) == "disk_full" or getattr(
                    e, "errno", None) in (errno.ENOSPC, errno.EDQUOT):
                result["attachments_deferred"] = len(due) - i
                break
            if not isinstance(e, MCSError):
                ledger.attachment_failed(
                    a["attachment_id"], f"fs_{type(e).__name__}",
                    semantic=semantic)
                result["errors"].append(
                    f"attach {a['attachment_id']}: fs")
                continue
            # downloads carry the session bearer but surface an expired
            # session as a plain 401/403 — re-login instead of burning
            # the row into a permanent 'failed' (same rule as _request)
            if e.kind == "http_error" and getattr(e, "status", None) in (
                    401, 403) and not adapter._probe_session_ok():
                raise SessionExpired("attachments", status=e.status) from e
            # keep the HTTP status in the recorded kind — the failure
            # class (permanent 4xx vs transient) and triage need it (F11)
            kind = (f"http_{e.status}" if e.kind == "http_error"
                    and getattr(e, "status", None) else e.kind)
            ledger.attachment_failed(a["attachment_id"], kind,
                                     semantic=semantic)
            result["errors"].append(
                f"attach {a['attachment_id']}: {kind}")


def stage_karte_summary(adapter, ledger, result, deadline,
                        jobs_only: bool = False):
    """Read-only 連携サマリー refresh for projects that stored new chat.

    Targets: projects flagged karte_summary_due (newest last_seen first,
    so fresh chat wins the cap) — the save paths set it when they store
    new chat (unread root, reply job or self-probe import); a deferred
    (cap/margin) or retryably failed GET keeps it, a non-retryable one
    (4xx, schema, or a per-karte expiry while the session probe is
    fine) clears the immediate due flag; deep refresh can retry after backoff.
    Any failure keeps
    the project out of selection for 6 h (KARTE_SUMMARY_BACKOFF_S), so
    a dead karte cannot starve the cap — and on deep runs up to KARTE_SUMMARY_FILL
    live projects, prioritizing never-fetched summaries then summaries last
    fetched at least 24 h ago (oldest first). At most
    KARTE_SUMMARY_TICK_CAP GETs per run, stopping KARTE_SUMMARY_MARGIN_S
    before the deadline; runs after notify. Per-project MCSErrors are
    recorded under result["karte_summary"] and never make the run
    partial. No mark-as-read, no POST."""
    stats = {"fetched": 0, "stored": 0, "empty": 0, "skipped": 0,
             "deferred": 0, "errors": []}
    result["karte_summary"] = stats
    due = ledger.karte_summary_due()
    targets = list(due)
    if jobs_only:
        missing = ledger.karte_summary_missing(KARTE_SUMMARY_FILL)
        targets += missing + ledger.karte_summary_stale(KARTE_SUMMARY_FILL - len(missing))
    due_set = set(due)
    gets = 0
    for pid in dict.fromkeys(targets):
        karte_id = ledger.karte_id(pid)
        if not karte_id:
            continue
        if (gets >= KARTE_SUMMARY_TICK_CAP
                or time.monotonic() > deadline - KARTE_SUMMARY_MARGIN_S):
            stats["deferred"] += 1
            if pid not in due_set:              # already flagged rows need no rewrite
                ledger.karte_summary_mark_due(pid)
            continue
        gets += 1
        try:
            payload = adapter.fetch_memo_summary(karte_id)
        except SessionExpired as e:
            # a 403 on one karte reads as expiry; a live session means
            # the karte itself is refused — fail it, keep the run going
            if not adapter._probe_session_ok():
                raise
            stats["errors"].append({"project": pid, "kind": e.kind})
            ledger.karte_summary_mark_due(pid, due=False)
            ledger.karte_summary_failed(pid)
            continue
        except MCSError as e:
            stats["errors"].append({"project": pid, "kind": e.kind})
            ledger.karte_summary_mark_due(pid, due=e.retryable)
            if e.kind != "rate_limited":  # never sent: no 6 h backoff
                ledger.karte_summary_failed(pid)
            continue
        stats["fetched"] += 1
        if payload is None:
            stats["empty"] += 1
        if ledger.karte_summary_store(pid, karte_id, payload):
            stats["stored"] += 1
        else:
            stats["skipped"] += 1


def stage_metadata_shadow(adapter, ledger, result, deadline, *, publish=False,
                          actors=False, cfg=None):
    """Bounded opt-in metadata refresh after delivery; never changes message state.

    publish=True also writes successful refreshes to capture (22-E);
    actors=True then walks reaction actors in the remaining budget (22-F)."""
    started = time.monotonic()
    shadow_deadline = min(deadline - 30, started + 25)
    stats = {"mode": "publish" if publish else "shadow", "fetched": 0,
             "published": 0, "deferred": 0, "errors": [],
             "budget_s": round(max(0, shadow_deadline - started), 3),
             "elapsed_s": 0, "deferred_reasons": {},
             "interval_s": "tiered",
             "backoff_s": METADATA_SHADOW_BACKOFF_S}
    result["metadata_shadow"] = stats
    targets = message_observations.metadata_watch_targets(ledger.db)
    stats["due"] = targets[0]["due_total"] if targets else 0
    stats["deferred"] = max(0, stats["due"] - len(targets))
    if stats["deferred"]:
        stats["deferred_reasons"]["cap"] = stats["deferred"]
    stopped = False
    try:
        adapter.set_deadline(shadow_deadline)
        for index, target in enumerate(targets):
            if time.monotonic() >= shadow_deadline:
                remaining = len(targets) - index
                stats["deferred"] += remaining
                reason = "deadline_margin" if shadow_deadline == deadline - 30 else "budget_exhausted"
                stats["deferred_reasons"][reason] = remaining
                stopped = True
                break
            try:
                parent = {"parent_id": target["parent_id"]} if target["parent_id"] is not None else {}
                m = adapter.fetch_message_metadata(target["project_id"], target["message_id"], **parent)
            except MCSError as e:
                if e.kind == "rate_limited":
                    # refused locally after the run's 429 stop: nothing was observed
                    remaining = len(targets) - index
                    stats["deferred"] += remaining
                    stats["deferred_reasons"]["rate_limited"] = remaining
                    stopped = True
                    break
                ledger.save_metadata_shadow(SimpleNamespace(**dict(target)), error=e.kind)
                stats["errors"].append({"message_id": target["message_id"], "kind": e.kind})
                if isinstance(e, SessionExpired) or e.kind == "deadline_exceeded":
                    remaining = len(targets) - index - 1
                    stats["deferred"] += remaining
                    reason = "session_expired" if isinstance(e, SessionExpired) else "budget_exhausted"
                    if remaining:
                        stats["deferred_reasons"][reason] = remaining
                    stopped = True
                    break
                continue
            ledger.save_metadata_shadow(m, publish=publish)
            stats["fetched"] += 1
            stats["published"] += bool(publish and not m.metadata_errors)
            if m.metadata_errors:
                stats["errors"].append({"message_id": m.message_id, "kind": "schema_error"})
        if actors:
            stats["actors"] = _stage_reaction_actors(adapter, ledger, shadow_deadline,
                                                     skip=stopped, cfg=cfg)
    finally:
        adapter.set_deadline(deadline)
        stats["elapsed_s"] = round(max(0, time.monotonic() - started), 3)


def _stage_reaction_actors(adapter, ledger, until, *, skip, cfg=None):
    """Walk up to REACTION_ACTORS_TICK_CAP actor lists; incomplete walks keep the old set.

    A complete walk re-renders the live thread card holding the message
    (with cfg), so new names reach Slack/Discord without waiting for the
    bounded card sweep to rotate back to that card."""
    targets = stamp_read.reaction_actor_targets(ledger.db, limit=REACTION_ACTORS_TICK_CAP)
    stats = {"due": len(targets), "fetched": 0, "complete": 0,
             "deferred": len(targets) if skip else 0, "errors": []}
    for index, target in enumerate([] if skip else targets):
        if time.monotonic() >= until:
            stats["deferred"] += len(targets) - index
            break
        walk = adapter.fetch_reaction_actors(target["project_id"], target["message_id"])
        if walk.get("error") == "rate_limited":
            # This GET was refused before transport; earlier pages may exist.
            # Retain the last complete set, not a partial observation, and
            # retry next run without imposing a failed-fetch backoff.
            stats["deferred"] += len(targets) - index
            break
        stats["fetched"] += 1
        ledger.save_reaction_actors(target["message_id"], walk["actors"], walk["complete"],
                                    error=walk.get("error"))
        if walk["complete"]:
            stats["complete"] += 1
            if cfg is not None:
                try:
                    import notify_cards
                    notify_cards.rerender_message_cards(
                        ledger, cfg, target["project_id"], target["message_id"])
                except Exception as e:
                    stats["errors"].append({"message_id": target["message_id"],
                                            "kind": f"card_rerender:{type(e).__name__}"})
            continue
        stats["errors"].append({"message_id": target["message_id"], "kind": walk.get("error")})
        if walk.get("error") in ("session_expired", "deadline_exceeded"):
            stats["deferred"] += len(targets) - index - 1
            break
    return stats
