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
- extract*/rollup/notifier : derived-data + delivery stages

Exit codes:
  0 = run ok (may include per-patient partial failures — see result.errors)
  1 = run failed (bootstrap/network/schema-level failure)
  2 = session expired and auto-login could not recover -> human re-login
  3 = another instance holds the lock (overlap prevented)

Privacy: stdout/stderr carries ids, counts, states only. Patient names,
bodies, tokens never leave this process except into the local ledger and
(once external send was explicitly approved) the Discord channel.

Read-ack gate: --mark-read only marks a patient when its fetch_state is
'complete' AND the ledger commit succeeded, and it always sends the exact
snapshot timestamp returned by list_unread(). A response that fails to
parse records status='unknown', never 'confirmed'.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcs_adapter import (MCSAdapter, MCSError, SessionExpired, SchemaError)
from ledger import Ledger
from mcs_util import acquire_run_lock, load_config
import job_ops
import maintenance
import notifier

HOME = os.path.expanduser("~/.mcs")
DB = os.path.join(HOME, "data", "ledger.db")
CACHE = os.path.join(HOME, "token_cache.json")   # outside data/ (sandbox-mounted)
ATTACH_DIR = os.path.join(HOME, "data", "attachments")
LOCKFILE = os.path.join(HOME, "data", "run.lock")
CONF_PATH = os.path.join(HOME, "config.json")
CHROME_PROFILE = os.path.join(HOME, "chrome-profile")
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
RUN_DEADLINE_S = 480          # whole-run cap; per-request timeouts are not enough
BACKFILL_MAX_PAGES = 3        # per patient, per run — newest-first walk
BACKFILL_OVERLAP_S = 120      # re-scan window; dedup handles repeats


def _err_str(e: Exception) -> str:
    """Structured error info only — kind+status, never response bodies."""
    if isinstance(e, MCSError):
        return f"{e.kind}(status={e.status})" if e.status else e.kind
    return type(e).__name__


def _config() -> dict:
    return load_config(CONF_PATH)


# ---------- stage: unread pipeline ----------

def stage_unread(adapter, ledger, args, result, deadline, run_id,
                 semantic: bool = False):
    """list_unread -> per-patient unread fetch -> reply merge -> save
    (notify intent in the same tx) -> optional gated mark-read.
    Returns the snapshot for downstream stages."""
    try:
        snap = adapter.list_unread()
    except SessionExpired:
        # credential-free recovery: saved-password autofill + submit click
        state = adapter.auto_login(profile_dir=CHROME_PROFILE,
                                   chrome_bin=CHROME_BIN)
        if state == "ok":
            snap = adapter.list_unread()
        else:
            raise SessionExpired(f"auto_login={state}")
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
                try:
                    replies = adapter.fetch_unread_replies(m)
                except SessionExpired as e:
                    p.fetch_state = "incomplete"
                    p.fetch_reason = e.kind
                    session_error = e
                    break
                except MCSError:
                    # thread fetch failed — durable refetch jobs for each
                    # unread reply instead of aborting the patient
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
                "snapshot_ts": snap.timestamp}, semantic=semantic)
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
                   semantic: bool = False):
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
        new_ids = ledger.save_messages(hist, project_id=pid, notify={
            "run_id": run_id, "source": "history"}, semantic=semantic)
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


# ---------- stage: attachments ----------

def stage_attachments(adapter, ledger, result, deadline):
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
                                    info["bytes"], info["sha256"])
        except MCSError as e:
            ledger.attachment_failed(a["attachment_id"], e.kind)
            result["errors"].append(
                f"attach {a['attachment_id']}: {e.kind}")
        except OSError as e:
            ledger.attachment_failed(
                a["attachment_id"], f"fs_{type(e).__name__}")
            result["errors"].append(f"attach {a['attachment_id']}: fs")


# ---------- stage: derived data ----------

def stage_derive(ledger, result, deadline, llm_budget_cap: float = 90):
    """extract_v1 (instant rules) -> extract_llm (bounded local LLM)
    -> rollups for dirty patients."""
    try:
        import extract
        ex = extract.run_pending(ledger)
        result["extracted"] = ex["done"]
        result["extracted_pids"] = ex["pids"]
    except Exception as e:
        result["errors"].append(f"extract: {type(e).__name__}")

    try:
        import extract_llm
        remain = (deadline - time.monotonic()) - 45
        result["extract_llm"] = (
            extract_llm.run_pending(
                ledger, limit=15, budget_s=min(llm_budget_cap,
                                               max(0, remain)))
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


# ---------- main ----------

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
    args = ap.parse_args()

    os.makedirs(os.path.join(HOME, "data"), exist_ok=True)
    os.makedirs(ATTACH_DIR, exist_ok=True)

    lock_fd = acquire_run_lock(LOCKFILE)
    if lock_fd is None:
        print(json.dumps({"ok": False, "error": "lock_held"}))
        return 3

    deadline = time.monotonic() + RUN_DEADLINE_S
    adapter = MCSAdapter(token_cache=CACHE)
    try:
        ledger = Ledger(DB)
    except Exception:
        os.close(lock_fd)
        print(json.dumps({"ok": False, "error": "ledger_init_failed"}))
        return 1
    started = time.time()
    run_id = ledger.begin_run(
        None, kind="deep" if args.jobs_only else "tick")
    result = {"run_id": run_id, "ok": False, "projects": 0, "messages": 0,
              "new_messages": 0, "backfilled": 0, "incomplete": [],
              "marked_read": [], "notify": {}, "errors": []}
    cfg = _config()
    try:
        import semantic as _sem
        sem_on = _sem.semantic_config(cfg)[0]["mode"] != "off"
    except Exception:
        sem_on = False   # config/module trouble -> semantic stays OFF

    try:
        # -- priority fetch work -------------------------------------
        if not args.jobs_only:
            stage_unread(adapter, ledger, args, result, deadline, run_id,
                         semantic=sem_on)
            if not args.no_backfill:
                stage_backfill(adapter, ledger, result, deadline, run_id,
                               semantic=sem_on)
        else:
            result["jobs_only"] = True

        # -- durable job machinery ------------------------------------
        # cmd ingest first so requests are due THIS run; discovery and
        # trickle seeding only enqueue — the drains below execute them.
        job_ops.drain_commands(ledger, result)
        job_ops.seed_discovery(ledger)
        discover_archived = cfg.get("discover_archived", False)
        if type(discover_archived) is not bool:
            result["errors"].append("config: discover_archived_invalid")
            discover_archived = False
        job_ops.run_discovery(adapter, ledger, result, deadline,
                              include_archived=discover_archived)
        job_ops.run_reply_jobs(adapter, ledger, result, deadline,
                               semantic=sem_on)
        job_ops.run_history_jobs(adapter, ledger, result, deadline,
                                 trickle=False, semantic=sem_on)

        if args.download_files:
            stage_attachments(adapter, ledger, result, deadline)

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
        job_ops.run_history_jobs(
            adapter, ledger, result, deadline, trickle=True,
            trickle_pages=trickle_pages,
            max_jobs=8 if args.jobs_only else None,
            min_margin=30 if args.jobs_only else None,
            semantic=sem_on)

        # -- derived data ----------------------------------------------
        stage_derive(ledger, result, deadline)

        # -- delivery ----------------------------------------------------
        # existing notification sends run BEFORE the semantic drain —
        # §19.1 prioritizes committed work over new analysis, and an
        # enforce-mode semantic_notice enqueued below simply sends on a
        # later tick
        if not args.no_notify:
            try:
                result["notify"] = notifier.flush(ledger, deadline=deadline)
            except Exception as e:
                result["errors"].append(f"notify: {type(e).__name__}")

        # -- semantic layer (Phase J, feature-gated) --------------------
        # drains durable 'semantic' jobs on the same lock + remaining
        # deadline; OFF is a no-op here AND disables seeding above, so
        # the flag truly stops communication rather than only hiding
        # output
        if sem_on:
            try:
                import semantic
                result["semantic"] = semantic.run_due(
                    ledger, cfg, result, deadline, cfg_path=CONF_PATH)
            except Exception as e:
                result["errors"].append(
                    f"semantic: {type(e).__name__}")

        # -- housekeeping ------------------------------------------------
        try:
            maintenance.daily_backup(DB)
        except Exception as e:
            result["errors"].append(f"backup: {type(e).__name__}")
        try:
            maintenance.rotate_log()
        except Exception as e:
            result["errors"].append(f"log_rotate: {type(e).__name__}")

        if result.get("notify", {}).get("failed") \
                or result.get("notify", {}).get("skipped"):
            result["errors"].append("notify_incomplete")

        # stage-level status: ANY recorded error or unfilled stage means the
        # run was not clean — silent ok on partial work is the failure mode
        # being removed (Oracle B18)
        status = "ok" if not result["errors"] and not result["incomplete"] \
            else "partial"
        result["ok"] = True
        ledger.finish_run(run_id, status,
                          "; ".join(result["errors"][:8]))
        try:
            result["snapshot"] = maintenance.publish_snapshot(DB)
            if not result["snapshot"]:
                raise RuntimeError("snapshot_verify_failed")
        except Exception as e:
            result["errors"].append(f"snapshot: {type(e).__name__}")
            ledger.finish_run(run_id, "partial",
                              "; ".join(result["errors"][:8]))
    except SessionExpired as e:
        ledger.finish_run(run_id, "session_expired", _err_str(e))
        result["errors"].append(_err_str(e))
        try:  # operational alert — contains no patient data
            ledger.outbox_add("session_expired", None,
                              {"run_id": run_id, "detail": _err_str(e)})
            if not args.no_notify:  # --no-notify suppresses ALL sends;
                result["notify"] = notifier.flush(ledger, deadline=deadline)
        except Exception:                                    # queued for a
            pass                                             # later flush
        print(json.dumps(result, ensure_ascii=False))
        return 2
    except MCSError as e:
        ledger.finish_run(run_id, "failed", _err_str(e))
        result["errors"].append(_err_str(e))
        try:  # operational alert — silent death is worse than noise
            ledger.outbox_add("run_failed", None,
                              {"run_id": run_id, "detail": _err_str(e)})
            if not args.no_notify:
                result["notify"] = notifier.flush(ledger, deadline=deadline)
        except Exception:
            pass
        print(json.dumps(result, ensure_ascii=False))
        return 1
    except Exception as e:
        # outermost boundary — non-MCSError crashes (AttributeError, sqlite,
        # ...) must still record a failed run and alert, not die silently
        try:
            ledger.finish_run(run_id, "failed", type(e).__name__)
            result["errors"].append(f"crash: {type(e).__name__}")
            ledger.outbox_add("run_failed", None,
                              {"run_id": run_id,
                               "detail": type(e).__name__})
            if not args.no_notify:
                result["notify"] = notifier.flush(ledger, deadline=deadline)
        except Exception:
            pass
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
