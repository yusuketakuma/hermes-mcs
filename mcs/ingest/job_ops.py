#!/usr/bin/env python3
"""MCS durable job operations — separated from run_check orchestration.

Responsibilities:
- cmd/*.json ingest: validate -> record a durable fetch_job -> consume the
  file. A request is never deleted before it is durably recorded, and a
  malformed request is consumed but reported, never loops (Oracle B12).
- fetch_jobs drains: 'reply' refetches (bodies the thread API missed) and
  'history' walks. History jobs carry their own page cursor so an
  unfinished deepen resumes where it stopped; a job only restarts at
  page 1 when it is NEW (Oracle B09). One page of overlap on resume
  absorbs forward-shifting page numbering.
- trickle mode: jobs whose payload sets "trickle": true get a small
  page budget per run and only execute while ample deadline remains —
  deep all-history imports advance gradually in the scheduler's spare
  capacity instead of competing with the unread pipeline.
- discovery: periodic /projects enumeration (a 'discovery' job kept
  permanently pending with a daily next_try) finds patients that have
  never produced an unread item and seeds trickle jobs for them.
- merge_full_replies: shared thread-body merge used by init_data and the
  job drains; missing bodies/failed threads become durable reply jobs.
"""
import json
import os
import time
from dataclasses import dataclass

from mcs_adapter import MCSError, SessionExpired
from ledger import TERMINAL_BODY_STATES
import mcs_requests

CMD_DIR = os.path.join(os.path.expanduser("~/.mcs"), "data", "cmd")

TRICKLE_PAGES = 3        # timeline pages per patient per run
TRICKLE_PATIENTS = 3     # patients advanced per run
TRICKLE_MIN_S = 90       # only run trickle with this much budget left
DISCOVERY_INTERVAL_S = 24 * 3600
# failed discovery retries on a bounded backoff — the job must stay
# pending forever, never accumulate attempts into a terminal 'failed'
DISCOVERY_RETRY_S = 1800
REPLY_JOB_LIMIT = 10
HISTORY_JOB_LIMIT = 4
# reconcile = the only surface that sees EDITS/DELETIONS on posts below
# the since-cutoff: a slow rotating re-walk of the full history (no
# since filter) that re-saves pages so upserts can detect changes.
RECONCILE_PAGES = 2            # history pages per drain call
RECONCILE_INTERVAL_S = 6*3600  # idle period after a full pass completes
RECONCILE_JOB_LIMIT = 2        # patients per drain call
# a history window that stays checkpoint-unsafe is re-walked on each
# defer; after this many stalls the job fails visibly instead of looping
# forever over an unresolvable blocker (e.g. a 'snippet' parent whose
# full body has no API surface)
HISTORY_STALL_LIMIT = 8

# body_state values that mean "nothing more to fetch": 'full' = complete
# body (including empty body on file-only posts); 'deleted' = tombstoned
# on the MCS side, body permanently unavailable.
# TERMINAL_BODY_STATES lives in ledger.py (imported above) — the ledger
# reconciles reply-job state against it atomically during reply saves.


@dataclass
class MergeResult:
    checkpoint_safe: bool = True
    reply_jobs: int = 0
    deadline: bool = False
    error: MCSError | None = None


# ---------- command ingest ----------

def _valid_cmd(req) -> tuple[bool, str]:
    """Strict command schema — malformed input must never raise (Oracle B12)."""
    if not isinstance(req, dict) or req.get("cmd") != "import":
        return False, "unknown_cmd"
    pid = req.get("project_id")
    if type(pid) is not int or pid <= 0:
        return False, "bad_project_id"
    for k, lo, hi in (("days", 1, 365), ("pages", 1, 40)):
        if k not in req:
            continue
        v = req[k]
        if type(v) is not int or not (lo <= v <= hi):
            return False, f"bad_{k}"
    return True, ""


def _valid_history_payload(pl) -> bool:
    return (isinstance(pl, dict)
            and type(pl.get("since")) is int and pl["since"] >= 0
            and type(pl.get("page", 1)) is int and pl.get("page", 1) >= 1
            and ("pages" not in pl
                 or (type(pl["pages"]) is int and 1 <= pl["pages"] <= 40))
            and ("trickle" not in pl or type(pl["trickle"]) is bool)
            and ("stalls" not in pl
                 or (type(pl["stalls"]) is int and pl["stalls"] >= 0)))


def drain_commands(ledger, result, cmd_dir: str = CMD_DIR):
    """Consume queued bot requests into durable fetch_jobs FIRST. Files may
    arrive mid-write (producer writes non-atomically): a parse failure is
    consumed but recorded, never loops."""
    consent_only = False
    try:
        import notify_cards
        consent_only = notify_cards.restore_awaiting_consent(
            os.path.dirname(cmd_dir)) is not None
    except Exception:
        # an unreadable marker must not swallow commands — treat as no
        # hold; the restore path itself stays fail-closed
        consent_only = False
    try:
        names = sorted(os.listdir(cmd_dir))
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(cmd_dir, name)
        try:
            req = mcs_requests.read_command(path)
        except (OSError, ValueError, RecursionError):
            # WatchPaths can fire while a producer is still writing. Never
            # consume a request until it is complete enough to validate.
            continue
        if isinstance(req, dict) and isinstance(req.get("cmd"), str) \
                and (req["cmd"].startswith("request.")
                     or req["cmd"].startswith("ops.")):
            if consent_only and req["cmd"] != "ops.restore_approve":
                # A schema-bump DB replace is held for human consent:
                # any other command's writes would be silently wiped by
                # the pending swap, so every file stays queued until
                # the consent lands and the restore completes.
                continue
            try:
                if not mcs_requests.valid_uuid(req.get("command_id")):
                    raise ValueError("bad_command_id")
                mcs_requests.payload_hash(req)
            except (ValueError, UnicodeError, RecursionError):
                os.replace(path, path + ".invalid")
                result["errors"].append("cmd_invalid: bad_request_identity")
                continue
            # Storage exceptions propagate: do not consume a command whose commit failed.
            # ops.card_resolve carries no normal project_id — its scope is
            # derived from the stored render/coverage — so it must branch
            # before the common apply_command validation would reject it.
            if req["cmd"] == "ops.card_resolve":
                import notify_transport
                receipt = notify_transport.apply_card_resolve(ledger, req)
            else:
                receipt = mcs_requests.apply_command(ledger, req)
            if receipt["outcome"] == "rejected":
                result["errors"].append("cmd_invalid: " + receipt["error"])
            elif receipt.get("scheduled") is True \
                    and receipt.get("cmd") in (
                        "ops.update_apply", "ops.update_rollback",
                        "ops.restore_approve"):
                # The committed receipt is the approval boundary — spawn
                # the detached updater only AFTER commit, never inside
                # apply_tx (S9). The spawned process re-verifies via
                # receipt scan; argv/loop state is never trusted. A
                # restore consent re-enters the held rollback at once
                # instead of waiting for the next scheduled check.
                try:
                    import mcs_update
                    mcs_update.spawn_detached()
                except Exception:
                    result["errors"].append("update_spawn_failed")
            try:
                os.unlink(path)
            except OSError:
                pass  # Receipt makes the next drain idempotent.
            result["command_commands"] = result.get("command_commands", 0) + 1
            bucket = ("request_commands" if req["cmd"].startswith("request.")
                      else "ops_commands")
            result[bucket] = result.get(bucket, 0) + 1
            continue
        ok, reason = _valid_cmd(req)
        if ok:
            ledger.ensure_patient(req["project_id"])
            since = int(time.time() - req.get("days", 14) * 86400)
            floor = ledger.history_floor(req["project_id"])
            if floor and floor <= since:
                ok, reason = False, "already_floored"
            else:
                # an in-flight walk should DEEPEN on a newer request rather
                # than drop it — pages are newest-first so a bigger cutoff
                # just extends the same walk (page cursor stays valid)
                existing = ledger.history_job(req["project_id"])
                if existing:
                    try:
                        pl = json.loads(existing["payload"] or "{}")
                        if not _valid_history_payload(pl):
                            raise ValueError
                    except (json.JSONDecodeError, TypeError, ValueError):
                        ledger.job_fail(existing["job_id"])
                        existing = None
                if existing:
                    pl["since"] = min(since, pl.get("since", since))
                    pl["pages"] = max(pl.get("pages", 10),
                                      req.get("pages", 10))
                    pl["trickle"] = False
                    ledger.job_defer(existing["job_id"], 0, payload=pl)
                else:
                    ledger.job_add("history", req["project_id"], payload={
                        "since": since, "page": 1,
                        "pages": req.get("pages", 10), "cmd": name})
        if ok:
            try:
                os.unlink(path)
            except OSError:
                pass
        else:
            try:
                os.replace(path, path + ".invalid")
            except OSError:
                pass
        if not ok:
            result["errors"].append(f"cmd_invalid: {reason}")


# ---------- thread merge (shared with init_data) ----------

def merge_full_replies(adapter, msgs, delay, deadline, stats, ledger=None):
    """Fill full bodies for every message that has thread replies.
    Replies the thread response doesn't return — or threads that fail —
    become durable 'reply' fetch_jobs when a ledger is passed, so an
    incomplete thread can never be recorded as fully imported
    (Oracle B05/B08)."""
    stats.setdefault("reply_jobs", 0)
    stats.setdefault("threads", 0)
    stats.setdefault("deadline", False)
    result = MergeResult()
    for m in msgs:
        if time.monotonic() > deadline:
            stats["deadline"] = True
            result.deadline = True
            result.checkpoint_safe = False
            return result
        # a parent whose own body is 'snippet' (truncated by the list
        # API) must block cursor advancement AND the floor — not just
        # this batch's completion flag — otherwise earlier pages
        # certify around it (Oracle R3). 'unknown' parents do NOT
        # block: the list response is the only body-bearing surface
        # for parents (threads return replies only — verified against
        # the live API), so 'unknown' means "this post has no text
        # body" (file/stamp/system posts), i.e. terminal-in-effect —
        # blocking on it would defer the job forever with no recovery
        # path. A later fetch that DOES return a body upgrades the
        # stored row via upsert regardless.
        if (m.body_state not in TERMINAL_BODY_STATES
                and m.body_state != "unknown"):
            result.checkpoint_safe = False
        if not m.replies and not m.reply_count:
            continue
        if (m.replies
                and all(t.body_state in TERMINAL_BODY_STATES
                        for t in m.replies)
                and len({t.message_id for t in m.replies}) >= m.reply_count):
            continue
        try:
            kwargs = ({"max_pages": (m.reply_count + 9) // 10}
                      if m.reply_count > 100 else {})
            full = adapter.fetch_thread(m.project_id, m.message_id, **kwargs)
        except SessionExpired as e:
            result.error = e
            result.checkpoint_safe = False
            return result
        except MCSError as e:
            stats["errors"].append(
                f"thread {m.project_id}/{m.message_id}: {e.kind}")
            if ledger:
                if e.kind == "thread_incomplete":
                    ledger.job_add("thread", m.project_id, m.message_id,
                                   parent_id=m.message_id, payload={"page": 1})
                for t in m.replies:
                    ledger.job_add("reply", m.project_id, t.message_id,
                                   parent_id=m.message_id)
                    stats["reply_jobs"] += 1
                    result.reply_jobs += 1
            # reaching the fetch means embedded replies were incomplete;
            # a failed fetch leaves them unverified — never floor over
            # this thread even when SOME replies were embedded (Oracle F3)
            result.checkpoint_safe = False
            continue
        got = {f.message_id for f in full
               if f.body_state in TERMINAL_BODY_STATES}
        merged = {t.message_id: t for t in m.replies}
        for f in full:
            cur = merged.get(f.message_id)
            if cur is None or cur.body_state not in TERMINAL_BODY_STATES:
                merged[f.message_id] = f
        m.replies = list(merged.values())
        if len(got) < m.reply_count:
            result.checkpoint_safe = False
            if ledger:
                # Missing reply ids cannot be queued individually; retry
                # the thread until its advertised count is accounted for.
                ledger.job_add("thread", m.project_id, message_id=m.message_id,
                               parent_id=m.message_id, payload={"page": 1})
        if ledger:
            for t in merged.values():
                if (t.message_id not in got
                        and t.body_state not in TERMINAL_BODY_STATES):
                    ledger.job_add("reply", m.project_id, t.message_id,
                                   parent_id=m.message_id)
                    stats["reply_jobs"] += 1
                    result.reply_jobs += 1
                elif t.message_id not in got:
                    result.checkpoint_safe = False
        stats["threads"] += 1
        time.sleep(delay)
    return result


# ---------- job drains ----------

def run_reply_jobs(adapter, ledger, result, deadline,
                   semantic: bool = False,
                   notify_max_age_s: float | None = None):
    """Retry fetching full bodies for replies the thread API missed."""
    # self-heal: replies persisted outside a merge (unread path,
    # sibling saves) carry no retry reservation and would stay
    # 'snippet'/'unknown' forever — seed a durable job for each
    # never-queued one (bounded; resolved rows are never re-seeded)
    for r in ledger.replies_without_job():
        ledger.job_add("reply", r["project_id"], r["message_id"],
                       parent_id=r["parent_id"])
    jobs = ledger.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind IN ('reply','thread') "
        "AND state='pending' AND next_try <= ? "
        "ORDER BY updated_at,job_id LIMIT ?",
        (time.time(), REPLY_JOB_LIMIT)).fetchall()
    for job in jobs:
        if time.monotonic() > deadline - 20:
            break
        try:
            pl = json.loads(job["payload"] or "{}")
            if not isinstance(pl, dict):
                raise ValueError
            page = pl.get("page", 1)
            if type(page) is not int or page < 1:
                raise ValueError
        except (json.JSONDecodeError, TypeError, ValueError):
            ledger.job_fail(job["job_id"])
            result["errors"].append("reply: invalid_payload")
            continue
        try:
            window_error = None
            win = getattr(adapter, "fetch_thread_window", None)
            if win is not None:
                window = win(
                    job["project_id"], job["parent_id"] or 0,
                    start_page=page)
                full = window.messages
                next_page = None if window.reached else page + window.pages
                window_error = window.error
            else:
                # stubs/simpler adapters: whole thread in one call
                full = adapter.fetch_thread(
                    job["project_id"], job["parent_id"] or 0)
                next_page = None
        except SessionExpired:
            raise  # auth failure aborts the run — never consumed as job retry
        except MCSError as e:
            ledger.job_retry(job["job_id"])
            result["errors"].append(
                f"reply {job['message_id']}: {e.kind}")
            continue
        # persist THIS window's replies even mid-walk — partial progress
        # is durable and save_thread_replies reconciles every sibling's
        # reply-job state in the same commit (Oracle F3/F05). The thread
        # root itself is excluded from the save set (C2).
        replies = [m for m in full if m.message_id != job["parent_id"]]
        for m in replies:
            m.parent_id = job["parent_id"]
        if replies:
            ledger.save_thread_replies(
                replies, job["project_id"],
                notify={"source": "reply_job"}, semantic=semantic,
                notify_max_age_s=notify_max_age_s)
        if window_error:
            # Preserve the unwalked thread even if a target in an earlier
            # page already retired its individual reply job.
            ledger.job_add("thread", job["project_id"], job["parent_id"],
                           parent_id=job["parent_id"],
                           payload={"page": next_page})
            if job["kind"] == "thread" or ledger.job_state(
                    "reply", job["project_id"], job["message_id"]) == "pending":
                pl["page"] = next_page
                ledger.job_defer(job["job_id"], 300, payload=pl)
                if not isinstance(window_error, SessionExpired):
                    ledger.job_retry(job["job_id"])
            result["errors"].append(f"thread {job['parent_id']}: {window_error.kind}")
            if isinstance(window_error, SessionExpired):
                raise window_error
            continue
        target_done = (job["kind"] == "reply" and ledger.job_state(
            "reply", job["project_id"], job["message_id"]) != "pending")
        if target_done:
            if next_page is not None:
                ledger.job_add("thread", job["project_id"], job["parent_id"],
                               parent_id=job["parent_id"],
                               payload={"page": next_page})
            continue
        if next_page is not None:
            pl["page"] = next_page
            ledger.job_defer(job["job_id"], 0, payload=pl)
            result.setdefault("reply_windows", []).append(
                {"mid": job["message_id"], "next_page": next_page})
        elif job["kind"] == "thread":
            parent = ledger.db.execute(
                "SELECT reply_count FROM messages "
                "WHERE project_id=? AND message_id=?",
                (job["project_id"], job["parent_id"])).fetchone()
            complete = ledger.db.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE project_id=? AND parent_id=? "
                "AND body_state IN ('full','deleted')",
                (job["project_id"], job["parent_id"])).fetchone()[0]
            if parent and complete < (parent["reply_count"] or 0):
                pl["page"] = 1
                ledger.job_defer(job["job_id"], 0, payload=pl)
                ledger.job_retry(job["job_id"])
                result["errors"].append(
                    f"thread {job['parent_id']}: replies_missing")
            else:
                ledger.job_done(job["job_id"])
        else:
            # An incomplete target on an earlier page must be revisited
            # on the next attempt, not retried forever at the tail.
            pl["page"] = 1
            ledger.job_defer(job["job_id"], 0, payload=pl)
            ledger.job_retry(job["job_id"])


def _due_history_jobs(ledger):
    """All due history jobs in fair (least-recently-touched) order. The
    snapshot is taken once — processing bumps updated_at, which already
    rotates the job to the back for the NEXT drain (Oracle F8)."""
    yield from ledger.history_jobs_due()


def run_history_jobs(adapter, ledger, result, deadline, trickle: bool = False,
                     trickle_pages: int = TRICKLE_PAGES,
                     max_jobs: int | None = None,
                     min_margin: float | None = None,
                     semantic: bool = False):
    """Work the durable history-import queue.

    trickle=False drains user-requested/cmd jobs (payload pages cap).
    trickle=True drains deep-import jobs at TRICKLE_PAGES per patient and
    only while `min_margin` of deadline remains — idle-capacity work.
    """
    limit = max_jobs or (TRICKLE_PATIENTS if trickle else HISTORY_JOB_LIMIT)
    done_n = 0
    for job in _due_history_jobs(ledger):
        try:
            pl = json.loads(job["payload"] or "{}")
            if not _valid_history_payload(pl):
                raise ValueError
        except (json.JSONDecodeError, TypeError, ValueError):
            ledger.job_fail(job["job_id"])
            result["errors"].append("import job: invalid_payload")
            continue
        if bool(pl.get("trickle")) != trickle:
            continue
        budget = min_margin if min_margin is not None else (
            TRICKLE_MIN_S if trickle else 30)
        if time.monotonic() > deadline - budget:
            break
        pid = job["project_id"]
        since = pl["since"]
        pages = trickle_pages if trickle else pl.get("pages", 10)
        cursor = pl.get("page", 1)
        sp = cursor if pages <= 1 else max(1, cursor - 1)
        try:
            batch = adapter.fetch_history(
                pid, since, max_pages=pages, start_page=sp)
        except SessionExpired:
            raise
        except MCSError as e:
            ledger.job_retry(job["job_id"])
            result["errors"].append(f"import {pid}: {e.kind}")
            continue
        hist = batch.messages
        stats = {"errors": result["errors"], "threads": 0,
                 "deadline": False, "reply_jobs": 0}
        try:
            merged = merge_full_replies(adapter, hist, 0.1, deadline, stats,
                                        ledger=ledger)
        except Exception as e:
            result["errors"].append(
                f"import {pid} replies: {type(e).__name__}")
            merged = MergeResult(checkpoint_safe=False)
        new_ids = ledger.save_messages(hist, project_id=pid,
                                       semantic=semantic)
        if batch.pages and merged.checkpoint_safe:
            pl["page"] = sp + batch.pages
            pl["stalls"] = 0
        elif not batch.error:
            # same window re-walked with checkpoint unsafe — track stalls
            # so a permanently unverifiable blocker surfaces instead of
            # re-fetching identical pages forever
            pl["stalls"] = pl.get("stalls", 0) + 1
        floored = False
        is_head = job["kind"] == "history_head"
        if batch.reached and not batch.error and merged.checkpoint_safe \
                and ledger.pending_reply_jobs(pid) == 0:
            # floor only when the root walk AND all required reply bodies
            # completed (Oracle B08); non-terminal parent bodies already
            # forced checkpoint_safe=False inside merge (R3/F9).
            # The walk verified everything up to the newest stored
            # message — record it as coverage so a later head sync can
            # anchor there instead of re-walking the full timeline.
            # Only when the walked range is contiguous with existing
            # coverage (since <= cov): a bounded walk starting ABOVE
            # coverage leaves an unverified gap it must not paper over.
            # 'history_head' is a bounded final reconciliation — it must
            # never rewrite the deep-walk floor (R5)
            if since <= ledger.coverage_ts(pid):
                ledger.set_coverage(pid, ledger.high_watermark(pid))
            if is_head:
                ledger.job_done(job["job_id"])
            else:
                ledger.set_history_floor(pid, since)
                ledger.job_done(job["job_id"])
                floored = True
        elif batch.error:
            # a walk aborted mid-way must consume an attempt like a
            # raised MCSError does — deferring without one re-walks the
            # same pages every retry interval forever on a permanent
            # error (gone project, lost permission), never surfacing
            # 'failed' (AUDIT-J03). P-2 revival via re-seed still applies.
            # SessionExpired stays attempt-free (auth aborts the run below,
            # matching the raised path).
            if isinstance(batch.error, SessionExpired):
                ledger.job_defer(job["job_id"], 300, payload=pl)
            else:
                ledger.job_retry(job["job_id"], 300, payload=pl)
        elif pl.get("stalls", 0) >= HISTORY_STALL_LIMIT:
            # window can never certify (e.g. 'snippet' parent whose full
            # body has no API surface) — fail visibly; the job is still
            # revivable by a new import request (P-2)
            ledger.job_fail(job["job_id"])
            result["errors"].append(f"import {pid}: window_stalled")
        elif batch.reached:
            # replies still pending — keep job alive to re-check floor,
            # backing off as stalls accumulate
            ledger.job_defer(job["job_id"],
                             min(3600, 600 * pl.get("stalls", 1)),
                             payload=pl)
        else:
            # progress checkpoint: resume from this page next tick without
            # consuming attempts (only real failures do)
            ledger.job_defer(job["job_id"], 0, payload=pl)
        if batch.error:
            result["errors"].append(f"import {pid}: {batch.error.kind}")
            if isinstance(batch.error, SessionExpired):
                raise batch.error
        if merged.error:
            raise merged.error
        key = "trickle_imports" if trickle else "cmd_imports"
        result.setdefault(key, []).append(
            {"pid": pid, "new": len(new_ids),
             "floored": floored})
        done_n += 1
        if done_n >= limit:
            break
    return done_n


# ---------- reconcile (post-import edit/delete coverage) ----------

def seed_reconcile(ledger):
    """Ensure one durable reconcile job per floored patient. Only a
    completed deep import gets a re-walk — mid-import patients are
    still covered by their own history cursor."""
    for r in ledger.db.execute(
            "SELECT project_id FROM patients"
            " WHERE history_floor IS NOT NULL AND history_floor != 0"):
        pid = r["project_id"]
        if not ledger.job_pending("reconcile", pid):
            # stagger first runs so a fleet doesn't refetch together
            ledger.job_add("reconcile", pid, payload={"page": 1},
                           next_try=time.time()
                           + (pid * 977) % RECONCILE_INTERVAL_S)


def run_reconcile_jobs(adapter, ledger, result, deadline,
                       semantic: bool = False):
    """Re-walk floored patients' history in small page windows, with NO
    since cutoff — the only path where an edit/deletion/tombstone on an
    old post is observed (F04). Re-saving lets the upsert detect the
    changed content_hash and retire derived artifacts as usual."""
    done_n = 0
    jobs = ledger.db.execute(
        "SELECT * FROM fetch_jobs WHERE kind='reconcile' "
        "AND state='pending' AND next_try <= ? "
        "ORDER BY updated_at,job_id LIMIT ?",
        (time.time(), RECONCILE_JOB_LIMIT)).fetchall()
    for job in jobs:
        if time.monotonic() > deadline - 30:
            break
        try:
            pl = json.loads(job["payload"] or "{}")
            if not isinstance(pl, dict):
                raise ValueError
            page = pl.get("page")
            if type(page) is not int or page < 1:
                raise ValueError
        except (json.JSONDecodeError, TypeError, ValueError):
            ledger.job_fail(job["job_id"])
            result["errors"].append("reconcile: invalid_payload")
            continue
        pid = job["project_id"]
        try:
            batch = adapter.fetch_history(
                pid, 0, max_pages=RECONCILE_PAGES, start_page=page)
        except SessionExpired:
            raise
        except MCSError as e:
            ledger.job_retry(job["job_id"])
            result["errors"].append(f"reconcile {pid}: {e.kind}")
            continue
        merged = merge_full_replies(adapter, batch.messages, 0, deadline,
                                    result, ledger=ledger)
        new_ids = ledger.save_messages(batch.messages, project_id=pid,
                                       semantic=semantic)
        if merged.error:
            raise merged.error
        if batch.pages and merged.checkpoint_safe:
            pl["page"] = page + batch.pages
        if batch.reached and not batch.error and merged.checkpoint_safe:
            # full pass complete — restart the rotation after a pause
            pl["page"] = 1
            ledger.job_defer(job["job_id"], RECONCILE_INTERVAL_S,
                             payload=pl)
        elif batch.error:
            if isinstance(batch.error, SessionExpired):
                ledger.job_defer(job["job_id"], 300, payload=pl)
                raise batch.error
            ledger.job_defer(job["job_id"], 0, payload=pl)
            ledger.job_retry(job["job_id"], 300)
            result["errors"].append(f"reconcile {pid}: {batch.error.kind}")
        else:
            ledger.job_defer(job["job_id"],
                             0 if merged.checkpoint_safe else 300, payload=pl)
        result.setdefault("reconcile", []).append(
            {"pid": pid, "new": len(new_ids), "page": pl["page"]})
        done_n += 1
    return done_n


# ---------- discovery + trickle seeding ----------

def seed_discovery(ledger):
    """Ensure the periodic discovery job exists (kept permanently pending
    with a daily next_try — never 'done', so it reschedules itself).
    A previously failed discovery row is revived — job_add's conflict
    path resets attempts and state (Oracle F7). Floored patients also
    keep a durable reconcile job for post-import edits/deletions."""
    if not ledger.job_pending("discovery", 0):
        ledger.job_add("discovery", 0, payload={})
    seed_reconcile(ledger)


def run_discovery(adapter, ledger, result, deadline,
                  include_archived: bool = False):
    """Enumerate all projects; register unknown patients and seed trickle
    deep-imports. Runs at most once per DISCOVERY_INTERVAL_S.

    include_archived also enumerates /kartes?is_archived=1 and registers
    each linked project with is_archived=1 in ONE atomic upsert (F1).
    A patient that transitions live -> archived gets a final-delta
    history job so messages posted after its last completed walk are
    still imported (F5); a patient reappearing in the live list is
    unarchived (F4). Failures defer the job with a bounded retry —
    discovery must stay pending forever, never burn out (F7)."""
    job = ledger.job_pending("discovery", 0)
    if not job or job["next_try"] > time.time():
        return 0
    if time.monotonic() > deadline - 60:
        return 0
    try:
        projects = adapter.list_projects()
    except SessionExpired:
        raise
    except MCSError as e:
        ledger.job_defer(job["job_id"], DISCOVERY_RETRY_S)
        result["errors"].append(f"discovery: {e.kind}")
        return 0
    new_n = 0
    write_failed = False
    active_ids = {p.project_id for p in projects}
    for p in projects:
        try:
            created, _ = ledger.upsert_patient_info(p, is_archived=False)
            new_n += created
        except Exception:
            write_failed = True
    # the archived sweep is a SEPARATE failure domain: its error must not
    # discard already-fetched active registrations or unarchive-on-
    # reappearance work (Oracle R7)
    archived_n = 0
    archived_err = None
    if include_archived:
        try:
            archived = adapter.list_archived_kartes()
        except SessionExpired:
            raise
        except MCSError as e:
            archived_err = e.kind
        else:
            for p in archived:
                if p.project_id in active_ids:
                    continue  # live listing wins — never archive it
                try:
                    ledger.upsert_patient_info(p, is_archived=True)
                    archived_n += 1
                    # liveness repair for already-archived patients —
                    # the atomic transition reservation only fires on
                    # 0->1 flips, so jobs lost under the pre-atomic code
                    # or burnt out need this sweep (Oracle F06/R7).
                    # A pending/finished deep walk or a completed floor
                    # already covers the patient — never re-reserve those.
                    hist_state = ledger.job_state("history", p.project_id)
                    if hist_state == "failed":
                        ledger.job_add("history", p.project_id, payload={
                            "since": 0, "page": 1, "trickle": True})
                        hist_state = "pending"
                    if (ledger.job_state("history_head", p.project_id)
                            not in ("pending", "done")
                            and hist_state != "pending"):
                        # archived row with no in-flight work — its head
                        # reservation was lost under the pre-atomic code
                        # or it predates the reservation entirely (F06).
                        # An uncertified floor means deep gaps -> since=0
                        # full re-walk; a certified floor only leaves a
                        # possible post-floor gap -> bounded head anchor.
                        since = (ledger.archive_head_since(p.project_id)
                                 if ledger.history_floor(p.project_id) == -1
                                 else 0)
                        # a failed head's deeper anchor must be kept —
                        # recomputing from an advanced boundary would
                        # shrink the covered range each retry (F01)
                        old = ledger.job_payload("history_head",
                                                 p.project_id)
                        if old and type(old.get("since")) is int:
                            since = min(since, old["since"])
                        ledger.job_add("history_head", p.project_id,
                                       payload={"since": since, "page": 1,
                                                "trickle": True})
                except Exception:
                    write_failed = True
    if write_failed or archived_err:
        ledger.job_defer(job["job_id"], DISCOVERY_RETRY_S)
        cause = " ".join(
            x for x in ("patient_write_failed" if write_failed else "",
                        f"archived:{archived_err}" if archived_err else "")
            if x)
        result["errors"].append(f"discovery: {cause}")
        return 0
    seeded = seed_trickle(ledger, [p.project_id for p in projects])
    ledger.job_defer(job["job_id"], DISCOVERY_INTERVAL_S)
    result["discovery"] = {"projects": len(projects),
                           "archived": archived_n,
                           "new": new_n, "seeded": seeded}
    return len(projects)


def seed_trickle(ledger, pids=None, since: int = 0) -> int:
    """Queue a trickle deep-import for every patient not already covered
    (no floor at/below `since`) and not already queued. since=0 = the
    whole timeline — the walk ends when the API reports no next page.

    Archived patients are never seeded here — their import vehicle is the
    'history_head' job reserved atomically at archive time (Oracle R2)."""
    rows = pids if pids is not None else [
        r["project_id"] for r in ledger.known_patients()]
    n = 0
    for pid in rows:
        if ledger.is_archived(pid):
            continue
        floor = ledger.history_floor(pid)
        # floor==0 means "never floored" (NULL), floor==-1 means the whole
        # timeline was already walked — only a positive floor can deepen
        if floor and floor <= since:
            continue
        if ledger.job_state("history", pid) in ("pending", "failed"):
            continue
        ledger.job_add("history", pid, payload={
            "since": since, "page": 1, "trickle": True})
        n += 1
    return n
