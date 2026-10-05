#!/usr/bin/env python3
"""MCS initial data import — bulk-fetch recent history for all active patients.

Enumerates GET /projects (ordered by last_message recency), keeps projects
active within --days, and walks each timeline back to the cutoff via
fetch_history. Full thread replies are fetched for every message that has
replies. Attachment metadata is queued for the scheduled --download-files
drain; binaries are not fetched here.

Safety: read-only (all GETs), no mark-as-read, NO Discord notifications —
historical imports never enter the notify_outbox. Progress prints ids/counts
only, never patient names or bodies.

Resumable: patients.history_floor records the deepest completed cutoff;
re-running skips patients already floored at/below --since.
"""
import argparse
import json
import math
import os
import sys
import time

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
from mcs_adapter import MCSAdapter, MCSError, SessionExpired
from ledger import Ledger
from job_ops import merge_full_replies
from mcs_util import (CACHE, CHROME_BIN, CHROME_PROFILE, DB, RUN_LOCK,
                      acquire_run_lock)

# canonical path constants live in mcs_util; the local aliases keep the
# module attribute names (monkeypatch surface for tests) unchanged
LOCKFILE = RUN_LOCK


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--since", type=int, default=0,
                    help="epoch cutoff (overrides --days)")
    ap.add_argument("--pages", type=int, default=40,
                    help="max timeline pages per patient per run")
    ap.add_argument("--chunk", type=int, default=10,
                    help="pages per fetch/save batch (cursor advances per chunk)")
    ap.add_argument("--delay", type=float, default=0.15,
                    help="politeness sleep between API calls")
    ap.add_argument("--deadline", type=int, default=1800)
    ap.add_argument("--project", type=int, action="append", default=[],
                    help="restrict to specific project id(s)")
    args = ap.parse_args()
    if (args.days < 1 or args.pages < 1 or args.chunk < 1
            or not math.isfinite(args.delay) or args.delay < 0 or args.deadline < 1
            or any(pid <= 0 for pid in args.project)):
        ap.error("days/pages/chunk/deadline/project must be positive; delay must be finite and >= 0")

    since = args.since or int(time.time() - args.days * 86400)
    deadline = time.monotonic() + args.deadline

    lock_fd = acquire_run_lock(LOCKFILE)
    if lock_fd is None:
        print(json.dumps({"ok": False, "error": "lock_held"}))
        return 3

    adapter = MCSAdapter(token_cache=CACHE)
    adapter.set_deadline(deadline)
    ledger = Ledger(DB)
    stats = {"errors": [], "threads": 0, "deadline": False}

    try:
        projects = adapter.list_projects()
    except SessionExpired:
        state = adapter.auto_login(profile_dir=CHROME_PROFILE,
                                   chrome_bin=CHROME_BIN)
        if state != "ok":
            print(json.dumps({"ok": False, "error": f"auto_login={state}"}))
            ledger.close()
            os.close(lock_fd)
            return 2
        try:
            projects = adapter.list_projects()
        except MCSError as e:
            print(json.dumps({"ok": False, "error": e.kind}))
            ledger.close()
            os.close(lock_fd)
            return 1
    except MCSError as e:
        print(json.dumps({"ok": False, "error": e.kind}))
        ledger.close()
        os.close(lock_fd)
        return 1

    if args.project:
        wanted = set(args.project)
        active = [p for p in projects if p.project_id in wanted]
    else:
        active = [p for p in projects
                  if p.last_activity and p.last_activity >= since]

    result = {"ok": True, "since": since,
              "projects_total": len(projects), "projects_active": len(active),
              "done": 0, "skipped_floored": 0, "messages_new": 0,
              "threads_fetched": 0, "errors": stats["errors"]}
    print(json.dumps({"plan": result["projects_active"],
                      "since": since}, ensure_ascii=False))

    for i, p in enumerate(active):
        if time.monotonic() > deadline:
            stats["errors"].append("deadline_exceeded")
            break
        ledger.ensure_patient(p.project_id)
        floor = ledger.history_floor(p.project_id)
        if floor and floor <= since:
            result["skipped_floored"] += 1
            continue
        # Resume the stored cursor for ANY cutoff: pages are newest-first
        # so a deeper --since just continues the descent and a shallower
        # one reaches its cutoff on the next fetched page. Restarting at
        # page 1 on target drift made `--days` re-runs (whose `since`
        # moves every invocation) re-walk already-saved pages forever
        # (P-3). The floor write is monotonic, so a resumed walk can
        # never regress coverage. Finished floors still skip above.
        cursor = ledger.history_cursor(p.project_id)
        if not cursor:
            ledger.reset_history_cursor(p.project_id, since)
            cursor = 1
        elif ledger.history_target(p.project_id) != since:
            ledger.set_history_target(p.project_id, since)
        pages_left = args.pages
        total_new = 0
        reached = False
        replies_pending = False
        reauthed = False  # one re-login attempt per patient (FIX-ID1)
        # incremental walk: each chunk is fetched->replies->saved->cursor
        # advances, so deadline/crash resumes from the last stored page
        while pages_left > 0 and not reached:
            if time.monotonic() > deadline:
                stats["deadline"] = True
                break
            n_pages = min(args.chunk, pages_left)
            sp = cursor if n_pages <= 1 else max(1, cursor - 1)
            try:
                batch = adapter.fetch_history(
                    p.project_id, since, max_pages=n_pages, start_page=sp)
            except Exception as e:
                # fetch_history embeds every MCSError in batch.error — a
                # bare raise would be an adapter bug; keep the JSON
                # contract instead of a traceback (FIX-ID1)
                stats["errors"].append(
                    f"history {p.project_id}: {type(e).__name__}")
                break
            hist = batch.messages
            merged = merge_full_replies(
                adapter, hist, args.delay, deadline, stats, ledger=ledger)
            try:
                ledger.upsert_patient_info(p)
                new_ids = ledger.save_messages(hist)
            except Exception:
                stats["errors"].append(f"db {p.project_id}: write_failed")
                break
            total_new += len(new_ids)
            pages_used = batch.pages
            if merged.checkpoint_safe:
                cursor = sp + pages_used
            pages_left -= pages_used
            ledger.set_history_cursor(p.project_id, cursor)
            # floor requires: full walk to cutoff AND no failed/missing
            # replies AND no mid-merge deadline hit (Oracle B08);
            # 'snippet' parent bodies already forced
            # checkpoint_safe=False inside merge (Oracle R3/F9) —
            # 'unknown' parents are body-less post types the API never
            # resolves, so they do not block
            replies_pending = ledger.pending_reply_jobs(p.project_id) > 0
            if batch.reached and not batch.error and merged.checkpoint_safe \
                    and not replies_pending and not merged.deadline:
                # contiguous-with-coverage walks extend the verified
                # upper boundary — later head syncs anchor there
                if since <= ledger.coverage_ts(p.project_id):
                    ledger.set_coverage(p.project_id,
                                        ledger.high_watermark(
                                            p.project_id))
                ledger.set_history_floor(p.project_id, since)
                reached = True
            elif batch.reached:
                reached = True  # walk done but floor withheld — replies pending
            # auth failure arrives embedded — fetch_history puts it in
            # batch.error, the reply merge in merged.error. Attempt ONE
            # re-login per patient, then resume the chunk from the
            # persisted cursor (partial pages are already saved above).
            # The old `except SessionExpired` around fetch_history could
            # never fire, so this path was previously unreachable (FIX-ID1)
            if (isinstance(batch.error, SessionExpired)
                    or isinstance(merged.error, SessionExpired)) \
                    and not reauthed:
                reauthed = True
                state = adapter.auto_login(profile_dir=CHROME_PROFILE,
                                           chrome_bin=CHROME_BIN)
                if state == "ok":
                    continue
                stats["errors"].append(
                    f"history {p.project_id}: auto_login={state}")
            if batch.error:
                stats["errors"].append(
                    f"history {p.project_id}: {batch.error.kind}")
                if isinstance(batch.error, SessionExpired):
                    stats["deadline"] = True
                break
            if merged.error:
                stats["errors"].append("session_expired")
                stats["deadline"] = True
                break
            if pages_used < n_pages:
                break  # short page = end of timeline
            time.sleep(args.delay)
        result["messages_new"] += total_new
        result["threads_fetched"] = stats["threads"]
        result["done"] += 1
        floor = ledger.history_floor(p.project_id)
        print(json.dumps({"i": i + 1, "of": len(active),
                          "pid": p.project_id, "new": total_new,
                          "cursor": cursor,
                          "floored": bool(floor and floor <= since)},
                         ensure_ascii=False))
        time.sleep(args.delay)

    result["ok"] = not stats["deadline"] and not stats["errors"]
    print(json.dumps(result, ensure_ascii=False))
    try:
        ledger.close()
    finally:
        os.close(lock_fd)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
