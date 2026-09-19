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
import mcs_requests

CMD_DIR = os.path.join(os.path.expanduser("~/.mcs"), "data", "cmd")

TRICKLE_PAGES = 3        # timeline pages per patient per run
TRICKLE_PATIENTS = 3     # patients advanced per run
TRICKLE_MIN_S = 90       # only run trickle with this much budget left
DISCOVERY_INTERVAL_S = 24 * 3600
REPLY_JOB_LIMIT = 10
HISTORY_JOB_LIMIT = 4

# body_state values that mean "nothing more to fetch": 'full' = complete
# body (including empty body on file-only posts); 'deleted' = tombstoned
# on the MCS side, body permanently unavailable.
TERMINAL_BODY_STATES = ("full", "deleted")


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
            and ("trickle" not in pl or type(pl["trickle"]) is bool))


def drain_commands(ledger, result, cmd_dir: str = CMD_DIR):
    """Consume queued bot requests into durable fetch_jobs FIRST. Files may
    arrive mid-write (producer writes non-atomically): a parse failure is
    consumed but recorded, never loops."""
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
                and req["cmd"].startswith("request."):
            try:
                if not mcs_requests.valid_uuid(req.get("command_id")):
                    raise ValueError("bad_command_id")
                mcs_requests.payload_hash(req)
            except (ValueError, UnicodeError, RecursionError):
                os.replace(path, path + ".invalid")
                result["errors"].append("cmd_invalid: bad_request_identity")
                continue
            # Storage exceptions propagate: do not consume a command whose commit failed.
            receipt = mcs_requests.apply_command(ledger, req)
            if receipt["outcome"] == "rejected":
                result["errors"].append("cmd_invalid: " + receipt["error"])
            try:
                os.unlink(path)
            except OSError:
                pass  # Receipt makes the next drain idempotent.
            result["request_commands"] = result.get("request_commands", 0) + 1
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
        if not m.replies and not m.reply_count:
            continue
        if (m.replies
                and all(t.body_state in TERMINAL_BODY_STATES
                        for t in m.replies)
                and len({t.message_id for t in m.replies}) >= m.reply_count):
            continue
        try:
            full = adapter.fetch_thread(m.project_id, m.message_id)
        except SessionExpired as e:
            result.error = e
            result.checkpoint_safe = False
            return result
        except MCSError as e:
            stats["errors"].append(
                f"thread {m.project_id}/{m.message_id}: {e.kind}")
            if ledger:
                for t in m.replies:
                    ledger.job_add("reply", m.project_id, t.message_id,
                                   parent_id=m.message_id)
                    stats["reply_jobs"] += 1
                    result.reply_jobs += 1
            if not m.replies:
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

def run_reply_jobs(adapter, ledger, result, deadline):
    """Retry fetching full bodies for replies the thread API missed."""
    for job in ledger.job_due(limit=REPLY_JOB_LIMIT, kind="reply"):
        if time.monotonic() > deadline - 20:
            break
        try:
            full = adapter.fetch_thread(job["project_id"],
                                        job["parent_id"] or 0)
        except SessionExpired:
            raise  # auth failure aborts the run — never consumed as job retry
        except MCSError as e:
            ledger.job_retry(job["job_id"])
            result["errors"].append(
                f"reply {job['message_id']}: {e.kind}")
            continue
        got = {m.message_id for m in full}
        if job["message_id"] in got:
            target = next(m for m in full
                          if m.message_id == job["message_id"])
            if target.body_state not in TERMINAL_BODY_STATES:
                ledger.job_retry(job["job_id"])
                result["errors"].append(
                    f"reply {job['message_id']}: body_incomplete")
                continue
            target.parent_id = job["parent_id"]
            ledger.save_messages([target], project_id=job["project_id"])
            ledger.job_done(job["job_id"])
        else:
            ledger.job_retry(job["job_id"])


def _due_history_jobs(ledger):
    """Walk the whole due history queue in small keyset pages."""
    after = 0
    while True:
        rows = ledger.job_due(limit=40, kind="history", after_job_id=after)
        if not rows:
            return
        for row in rows:
            after = row["job_id"]
            yield row


def run_history_jobs(adapter, ledger, result, deadline, trickle: bool = False,
                     trickle_pages: int = TRICKLE_PAGES,
                     max_jobs: int | None = None,
                     min_margin: float | None = None):
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
        new_ids = ledger.save_messages(hist, project_id=pid)
        if batch.pages and merged.checkpoint_safe:
            pl["page"] = sp + batch.pages
        floored = False
        if batch.reached and not batch.error and merged.checkpoint_safe \
                and ledger.pending_reply_jobs(pid) == 0:
            # floor only when the root walk AND all required reply bodies
            # completed (Oracle B08)
            ledger.set_history_floor(pid, since)
            ledger.job_done(job["job_id"])
            floored = True
        elif batch.error:
            ledger.job_defer(job["job_id"], 300, payload=pl)
        elif batch.reached:
            # replies still pending — keep job alive to re-check floor
            ledger.job_defer(job["job_id"], 600)
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


# ---------- discovery + trickle seeding ----------

def seed_discovery(ledger):
    """Ensure the periodic discovery job exists (kept permanently pending
    with a daily next_try — never 'done', so it reschedules itself)."""
    if not ledger.job_exists("discovery", 0):
        ledger.job_add("discovery", 0, payload={})


def run_discovery(adapter, ledger, result, deadline):
    """Enumerate all projects; register unknown patients and seed trickle
    deep-imports. Runs at most once per DISCOVERY_INTERVAL_S."""
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
        ledger.job_retry(job["job_id"])
        result["errors"].append(f"discovery: {e.kind}")
        return 0
    new_n = 0
    write_failed = False
    for p in projects:
        if ledger.ensure_patient(p.project_id):
            new_n += 1
        try:
            ledger.upsert_patient_info(p)
        except Exception:
            write_failed = True
    if write_failed:
        ledger.job_retry(job["job_id"])
        result["errors"].append("discovery: patient_write_failed")
        return 0
    seeded = seed_trickle(ledger,
                          [p.project_id for p in projects])
    ledger.job_defer(job["job_id"], DISCOVERY_INTERVAL_S)
    result["discovery"] = {"projects": len(projects),
                           "new": new_n, "seeded": seeded}
    return len(projects)


def seed_trickle(ledger, pids=None, since: int = 0) -> int:
    """Queue a trickle deep-import for every patient not already covered
    (no floor at/below `since`) and not already queued. since=0 = the
    whole timeline — the walk ends when the API reports no next page."""
    rows = pids if pids is not None else [
        r["project_id"] for r in ledger.known_patients()]
    n = 0
    for pid in rows:
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
