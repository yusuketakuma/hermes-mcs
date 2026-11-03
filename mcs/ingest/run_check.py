#!/usr/bin/env python3
"""MCS unread check — cron/launchd entry point (orchestrator only).

Responsibility split:
- this file      : args, flock, deadline, stage ORDER, run status,
                   outermost exception boundary
- collection_stages.py : bounded collection, cursor/ack state, session replay
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
# registers every subdir holding .py files, at any depth, as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
import safe_io  # noqa: E402
import extract_validation
import semantic_drain
import semantic_policy
from mcs_adapter import MCSAdapter
from mcs_contract import MCSError, SessionExpired, describe_error
from ledger import Ledger
from mcs_util import CACHE, CONF_PATH, DB, HOME, load_config
import writer_lock
from writer_lock import RUN_LOCK, UPDATE_MARKER_NAME, RunLockLost, acquire_run_lock
import job_ops
import collection_stages
import health_report
import maintenance
import notify_flush

# canonical run-lock path lives in writer_lock; the local alias keeps the
# module attribute names (monkeypatch surface for tests) unchanged
LOCKFILE = RUN_LOCK
RUN_DEADLINE_S = 480          # whole-run cap; per-request timeouts are not enough
REALTIME_CARRY_S = 86400      # notified arrivals stay on the realtime slot this long
# the armed watchdog's exit time (monotonic); backup and DB upkeep start
# only when they fit before it, less this margin for the tail stages
_WATCHDOG_EXIT_AT: float | None = None
HARD_DEADLINE_MARGIN_S = 10


def _config() -> dict:
    return load_config(CONF_PATH)


# ---------- health contract (F20) ----------
# exit code says whether the PROCESS survived; health.json says how the
# SUBSYSTEMS did. Monitors must read this file — a partial run exits 0
# by design (work happened), so exit-status-only monitoring hides it.

def _write_health(ledger, result: dict, status: str,
                  run_id: int | None = None, *, cfg: dict | None = None) -> None:
    """Atomic health.json — monitoring consumes this file. A write
    failure must never turn committed ingest work into a crash, but it
    must not be silent either: a durable 'health_write_failed' incident
    is queued so the gap is itself visible (GAP-1)."""
    try:
        health = health_report.build_health(
            ledger.db, result, status, run_id=run_id, cfg=cfg,
            deadline=_hard_deadline())
        if ledger.db.in_transaction:
            result["errors"].append("tick: transaction_open")
            return
        result["health"] = health
        safe_io.atomic_publish_text(
            health_report.HEALTH_FILE, json.dumps(health, ensure_ascii=False))
    except Exception as e:
        result["errors"].append(
            f"health_write_failed: {type(e).__name__}")
        if not ledger.db.in_transaction:
            with suppress(Exception):
                ledger.outbox_add("health_write_failed", None,
                                  {"detail": type(e).__name__,
                                   "run_id": run_id})
LOCK_WAIT_S = 150


try:
    _CODE_STAMP = writer_lock.code_stamp_checked()
except OSError:
    _CODE_STAMP = None


def _entry_code_current() -> bool:
    """Refuse a tick when its loaded source identity is uncertain."""
    try:
        return _CODE_STAMP is not None and writer_lock.code_stamp_checked() == _CODE_STAMP
    except OSError:
        return False


def _entry_update_pending() -> bool:
    """Check the marker without interpreting an inaccessible marker as absent."""
    try:
        os.lstat(os.path.join(HOME, 'data', UPDATE_MARKER_NAME))
    except FileNotFoundError:
        return False
    return True


def _crash_site(e: BaseException) -> str:
    """Type and file:line of the innermost frame — never the message,
    which can embed paths or user data; basename only (no home dir)."""
    tb = e.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    where = (f"{os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}"
             if tb is not None else "unknown")
    return f"{type(e).__name__}@{where}"


def _code_changed(result: dict) -> bool:
    """True once the mcs/ tree changed under this tick; records 'code_changed' once.

    Stages that import modules lazily (derive, semantic) are skipped from
    then on, so a tick overlapping a deploy never mixes code generations."""
    if "code_changed" in result["errors"]:
        return True
    if writer_lock.code_stamp() == _CODE_STAMP:
        return False
    result["errors"].append("code_changed")
    return True




# ---------- metadata orchestration ----------

def _run_metadata_shadow(adapter, ledger, result, deadline, cfg, *, manual=False):
    """Select off, storage-only shadow, or publish mode for scheduled and manual runs."""
    enabled = cfg.get("metadata_shadow", False)
    reason = ("config_invalid" if type(enabled) is not bool else
              "code_changed" if _code_changed(result) else
              "disabled" if not (enabled or manual) else None)
    if reason:
        result["metadata_shadow"] = {"mode": "off", "reason": reason}
        return
    # a non-bool switch is off: publishing or actor walks need an explicit true
    collection_stages.stage_metadata_shadow(adapter, ledger, result, deadline,
                          publish=cfg.get("metadata_refresh_publish") is True,
                          actors=cfg.get("metadata_actors") is True, cfg=cfg)

class TickTransactionOpen(RuntimeError):
    """A tick stage left a transaction open that later stages must not commit."""


def _require_closed_transaction(ledger, result):
    if ledger.db.in_transaction:
        if "tick: transaction_open" not in result["errors"]:
            result["errors"].append("tick: transaction_open")
        raise TickTransactionOpen("tick_transaction_open")


def _report_open_transaction(ledger, result):
    if not ledger.db.in_transaction:
        return False
    if "tick: transaction_open" not in result["errors"]:
        result["errors"].append("tick: transaction_open")
    result["ok"] = False
    print(json.dumps(result, ensure_ascii=False))
    return True


def stage_derive(ledger, result, deadline, cfg=None,
                 llm_budget_cap: float = 90, run_lock_fd=None):
    """Run bounded rule/LLM extraction, medication mapping, rollups and signals.

    The rule pass supplies pattern-based hints before LLM extraction.
    Each phase leaves time for downstream clinical work. Medication
    mapping uses this tick's latest extraction before rollups and signals.
    The LLM pass also mints extract_v1 when the rule pass has not."""
    def transaction_pending():
        _require_closed_transaction(ledger, result)

    transaction_pending()
    import snapshots
    # Keep the existing run cap and verification policy. Derived work yields
    # early enough for size-based snapshot validation and the existing tail.
    source_db = ledger.db.execute("PRAGMA database_list").fetchone()[2]
    work_deadline = deadline - snapshots.snapshot_verify_timeout_s(source_db) - 30
    try:
        import extract
        # Leave a fair share for medication mapping, rollups and signals.
        rule_deadline = time.monotonic() + max(0, work_deadline - time.monotonic()) / 2
        ex = _run_stage(result, "derive_rules", work_deadline,
                        lambda: extract.run_pending(ledger, deadline=rule_deadline), required=True)
        result["extracted"] = ex["done"]
        result["extracted_pids"] = ex["pids"]
        if ex.get("reconcile_error") is not None:
            result["errors"].append("intervention_reconcile_failed")
    except Exception as e:
        result["errors"].append(f"extract: {type(e).__name__}")

    transaction_pending()
    try:
        import extract_llm
        remain = work_deadline - time.monotonic()
        budget = min(llm_budget_cap, max(0, remain) / 2)
        if budget < extract_llm._MIN_CALL_S:
            # Keep a complete call viable while reserving clinical follow-up time.
            budget = min(llm_budget_cap, extract_llm._MIN_CALL_S, max(0, remain) * 0.9)
        idle = {"done": 0, "failed": 0, "left": -1, "pids": []}
        # This tick's arrivals plus earlier notified arrivals a busy or
        # one-call tick left behind — the realtime slot reaches them
        # without waiting for a background lane to free up.
        realtime = set(result.get("realtime_ids", [])) | {
            r[0] for r in ledger.db.execute(
                "SELECT message_id FROM messages WHERE notified_at >= ?",
                (time.time() - REALTIME_CARRY_S,))}
        if budget < extract_llm._MIN_CALL_S or not realtime:
            result["extract_llm"] = idle
        elif extract_llm.pinned_slot_busy(time.monotonic() + budget):
            # Hermes is using realtime; the arrival stays durable for
            # another pass while ingestion and background keep moving.
            result["extract_llm"] = {**idle, "skipped_busy": True}
        else:
            # T18: under the v4 engine (fact_source=canonical) the legacy
            # v3 extractor admits NOTHING new; one helper with the drainer,
            # fail-closed on an errored semantic config. No cfg (direct
            # callers) keeps the per-key defaults, as before.
            admitted = extract_llm.legacy_admissions(ledger, cfg or {})
            # A fragmented batch can contain one row and retry it as a single.
            per_row_budget = (extract_llm._batch_need_s(1) + extract_llm._MIN_CALL_S
                              if extract_llm._BATCH_K >= 2 else extract_llm._MIN_CALL_S)
            result["extract_llm"] = extract_llm.run_pending(
                ledger, limit=max(1, int(budget // per_row_budget)),
                budget_s=budget, admitted_ids=(
                    realtime if admitted is None else realtime & admitted),
                # Only realtime arrivals use this slot, newest first.
                # Existing claim leases exclude in-flight work.
                oldest_first=False, batch_k=extract_llm._BATCH_K, allow_cloud=False,
                vital_policy=extract_validation.vital_threshold_policy(cfg), run_lock_fd=run_lock_fd)
            if result["extract_llm"].get("gc_error") == "extract_stale_gc_failed":
                result["errors"].append("extract_llm: extract_stale_gc_failed")
    except Exception as e:
        result["errors"].append(f"extract_llm: {type(e).__name__}")

    transaction_pending()
    try:
        import drug_map
        dictionary, error = drug_map.configured(cfg)
        invalid = error is not None
        if invalid:
            result["errors"].append(f"drug_map: {error}")
        map_deadline = time.monotonic() + max(0, work_deadline - time.monotonic()) / 3
        result["drug_map"] = _run_stage(
            result, "derive_medications", work_deadline,
            lambda: drug_map.derive(ledger, dictionary, deadline=map_deadline), required=True)
        if invalid:
            result["drug_map"]["status"] = "invalid"
        elif result["drug_map"]["status"] == "partial":
            result["errors"].append("drug_map: partial")
    except Exception as e:
        result["errors"].append(f"drug_map: {type(e).__name__}")

    transaction_pending()
    # rebuild rollups for patients whose underlying data changed —
    # union of touched-this-run + dirty detection (Oracle B23)
    try:
        import rollup
        touched = set(result.get("extracted_pids", []))
        touched |= set(result.get("extract_llm", {}).get("pids", []))
        touched |= set(result.get("drug_map", {}).get("pids", []))
        for k in ("cmd_imports", "trickle_imports"):
            touched |= {c["pid"] for c in result.get(k, [])}
        scan_start = time.monotonic()
        # Rollups yield before the remaining signal evaluation slice.
        rollup_deadline = scan_start + max(0, work_deadline - scan_start) * 2 / 3
        scan_deadline = scan_start + max(0, rollup_deadline - scan_start) / 2
        scan = rollup.scan_fair_dirty_projects(ledger, cfg=cfg, deadline=scan_deadline,
                                               commit_deadline=rollup_deadline)
        touched.difference_update(scan.unknown)
        rebuild_ids = list(dict.fromkeys([*scan.dirty, *sorted(touched)]))
        result["rollup_scan"] = {"state": "unknown" if scan.incomplete else "checked",
                                 "unknown_observed": len(scan.unknown),
                                 "unknown_reasons": dict(scan.unknown_reasons),
                                 "rechecked": scan.rechecked}
        if scan.incomplete:
            if scan.unknown and dict(scan.unknown_reasons) == {"deadline": len(scan.unknown)}:
                _defer_stage(result, "rollup_scan")
            else:
                result["errors"].append("rollup: scan_unknown")
        source_changed = []
        source_unknown = []
        source_unknown_reasons = {}
        result["rollups"] = rollup.rebuild_many(
            ledger, rebuild_ids, deadline=rollup_deadline, source_changed=source_changed,
            source_unknown=source_unknown, source_unknown_reasons=source_unknown_reasons, cfg=cfg)
        if source_changed:
            result["rollup_source_changed"] = len(source_changed)
            result["errors"].append("rollup: source_changed")
        if source_unknown:
            result["rollup_source_unknown"] = len(source_unknown)
            if (not set(source_unknown_reasons) <= {"deadline", "source_unverified"}
                    or any(type(count) is not int or count <= 0
                           for count in source_unknown_reasons.values())
                    or sum(source_unknown_reasons.values()) != len(source_unknown)):
                source_unknown_reasons = {"source_unverified": len(source_unknown)}
            result["rollup_source_unknown_reasons"] = source_unknown_reasons
            if source_unknown_reasons == {"deadline": len(source_unknown)}:
                _defer_stage(result, "rollup_rebuild")
            else:
                result["errors"].append("rollup: source_unknown")
    except Exception as e:
        result["errors"].append(f"rollup: {type(e).__name__}")

    transaction_pending()
    # prospective review candidates — lifecycle-persisted signal_v1
    # artifacts; notify intents only when config signals.notify is set
    try:
        import mcs_signals
        result["signals"] = mcs_signals.evaluate(
            ledger, cfg or _config(), deadline=work_deadline)
        result["errors"].extend(
            f"signals: {e}" for e in result["signals"].get("errors") or [])
        if result["signals"].get("detectors_deferred"):
            _defer_stage(result, "signals")
    except Exception as e:
        result["errors"].append(f"signals: {type(e).__name__}")
    transaction_pending()


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
        job_ops.drain_commands(ledger, result, consent_only=True)
        result["commands"] = result.get("command_commands", 0)
        # The receipt may predate this tick (an earlier spawn lost the
        # run.lock race). Re-check the bound consent directly — when a
        # matching receipt exists, relaunch the updater so the held
        # restore converges without waiting for the periodic check.
        import notify_spool
        marker = notify_spool.restore_awaiting_consent(
            os.path.join(HOME, "data"))
        if marker and marker.get("backup_path"):
            try:
                import restore_evidence
                report = restore_evidence.create_loss_report(
                    live_path=DB, backup_path=marker["backup_path"],
                    report_path=os.path.join(HOME, "data", "restore_report.json"))
                if restore_evidence.find_restore_consent(report, live_path=DB) is not None:
                    import update_launch
                    update_launch.spawn_detached(
                        root=HOME, data_dir=os.path.join(HOME, "data"),
                        hermes_wrapper=update_launch.HERMES_WRAPPER,
                        environment=os.environ)
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
    import notify_spool
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
        notify_spool.ensure_dirs(root)
        if notify_spool.restore_awaiting_consent(root) is not None:
            # schema-bump DB replace held for human consent — freeze
            # every writer except the consent drain so the approved
            # loss report cannot drift (drain_commands itself is
            # consent-only even if the marker changes during the drain)
            job_ops.drain_commands(ledger, result, consent_only=True)
            result["restore_consent_hold"] = True
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["ok"] else 1
        # startup/periodic recovery: missing spec files, flags,
        # stale-claim visibility — before any command is applied
        notify_spool.recover(ledger, cfg, result)
        # a DB restore holds all send grants until the journal-vs-DB
        # reconcile finishes — run it before draining new commands
        if notify_spool.restore_pending(root) is not None:
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
        notify_spool.publish_flags(cfg, root)
        notify_cards.gc(ledger, cfg)
        dirty = notify_cards.snapshot_dirty(ledger)
        if dirty:
            result["snapshot"] = maintenance.publish_snapshot(
                DB, deadline=deadline)
            if result["snapshot"]:
                notify_cards.clear_snapshot_dirty(ledger, dirty)
            else:
                result["ok"] = False
                result["errors"].append("snapshot_publish_failed")
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    except Exception as e:
        # exception text can embed paths/user data — type name only,
        # matching the tick's outermost boundary
        print(json.dumps({"ok": False,
                          "error": f"crash:{type(e).__name__}",
                          "crash_site": _crash_site(e)}))
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
        from semantic_publication import invalidate_projections
        scfg, errors = semantic_policy.runtime_config(cfg)
        # an invalid block also closes legacy extraction admission
        # (extract_llm.legacy_admissions) — surface it every tick
        for e in errors:
            if e not in result["errors"]:
                result["errors"].append(e)
        invalidate_projections(ledger.db, scfg)
        return scfg["mode"] != "off"
    except Exception as e:
        result["errors"].append(f"semantic_init: {type(e).__name__}")
        return False


def _stage_fetch(adapter, ledger, args, cfg, result, deadline, run_id,
                 sem_on, notify_max_age_s, ack_exclusions=None):
    """Priority fetch work — unread, optional self-post probes, then
    backfill — skipped entirely by --jobs-only runs."""
    if args.jobs_only:
        result["jobs_only"] = True
        return
    outage_since = collection_stages.outage_since()
    if outage_since is not None:
        result["outage_since"] = outage_since
    collection_stages.with_relogin(adapter, ledger, result, "unread",
                  collection_stages.stage_unread, adapter, ledger, result, deadline,
                  run_id, mark_read=args.mark_read, semantic=sem_on,
                  notify_max_age_s=notify_max_age_s, ack_exclusions=ack_exclusions)
    _require_closed_transaction(ledger, result)
    self_posts = cfg.get("self_posts", False)
    if type(self_posts) is not bool:
        result["errors"].append("config: self_posts_invalid")
        self_posts = False
    # probe BEFORE backfill: backfill notifies unread rows only, so an
    # own / other-read post on its first pages would be stored silently
    # and the probe would then see the latest id as already stored
    if self_posts:
        collection_stages.with_relogin(adapter, ledger, result, "self_probe",
                      collection_stages.stage_self_probe, adapter, ledger, result,
                      deadline, run_id, semantic=sem_on,
                      notify_max_age_s=notify_max_age_s, ack_exclusions=ack_exclusions)
        _require_closed_transaction(ledger, result)
    probe_after = result.pop("self_probe_after", None)
    if args.no_backfill:
        # no backfill to finish the probe's walk this run
        result["errors"].extend(f"probe {pid}: history_incomplete"
                                for pid in probe_after or {})
    else:
        collection_stages.with_relogin(adapter, ledger, result, "backfill",
                      collection_stages.stage_backfill, adapter, ledger, result, deadline,
                      run_id, semantic=sem_on,
                      notify_max_age_s=notify_max_age_s,
                      notify_all_new=outage_since is not None,
                      notify_after=probe_after, ack_exclusions=ack_exclusions)
        _require_closed_transaction(ledger, result)
    if outage_since is not None:
        collection_stages.seed_outage_catchup(ledger, result, outage_since)


def _run_jobs(adapter, ledger, args, cfg, result, deadline, sem_on,
              notify_max_age_s, ack_exclusions=()):
    """Durable job machinery — command ingest, discovery, reply and
    history drains, trickle seeding, reconcile and self profile."""
    # cmd ingest first so requests are due THIS run; discovery and
    # trickle seeding only enqueue — the drains below execute them.
    job_ops.drain_commands(ledger, result)
    _require_closed_transaction(ledger, result)
    try:
        # interactive-notification command channel: plugin receipts
        # and card actions apply mid-tick, not just via the watcher
        import notify_spool
        import notify_cmds
        notify_spool.ensure_dirs(os.path.join(HOME, "data"))
        # a DB restore holds send grants until the journal-vs-DB
        # reconcile lands — run it before draining cmd_int traffic
        if notify_spool.restore_pending(
                os.path.join(HOME, "data")) is not None:
            import notify_reconcile
            rep = notify_reconcile.reconcile_after_restore(
                ledger, cfg)
            result["restore_reconcile"] = rep["counts"]
            _require_closed_transaction(ledger, result)
        notify_cmds.drain_int_commands(
            ledger, result, cfg, os.path.join(HOME, "data"),
            deadline=deadline)
    except TickTransactionOpen:
        raise
    except Exception as e:
        result["errors"].append(f"cmd_int: {type(e).__name__}")
    _require_closed_transaction(ledger, result)
    # Human intent was committed above; external mutations never run in that TX.
    try:
        import stamp_write
        if stamp_write.enabled(cfg):
            result["stamps"] = stamp_write.drain(
                ledger, adapter, cfg, os.path.join(HOME, "data"), deadline)
    except Exception as e:
        result["errors"].append(f"stamps: {type(e).__name__}")
    _require_closed_transaction(ledger, result)
    job_ops.seed_discovery(ledger)
    _require_closed_transaction(ledger, result)
    discover_archived = cfg.get("discover_archived", False)
    if type(discover_archived) is not bool:
        result["errors"].append("config: discover_archived_invalid")
        discover_archived = False
    collection_stages.with_relogin(adapter, ledger, result, "discovery",
                  job_ops.run_discovery, adapter, ledger, result,
                  deadline, include_archived=discover_archived)
    _require_closed_transaction(ledger, result)
    collection_stages.with_relogin(adapter, ledger, result, "reply_jobs",
                  job_ops.run_reply_jobs, adapter, ledger, result,
                  deadline, semantic=sem_on,
                  notify_max_age_s=notify_max_age_s, ack_exclusions=ack_exclusions)
    _require_closed_transaction(ledger, result)
    collection_stages.with_relogin(adapter, ledger, result, "history_jobs",
                  job_ops.run_history_jobs, adapter, ledger, result,
                  deadline, trickle=False, semantic=sem_on,
                  notify_max_age_s=notify_max_age_s, ack_exclusions=ack_exclusions)
    _require_closed_transaction(ledger, result)
    if getattr(args, "mark_read", False):
        # after every import path of this tick has stored its replies
        collection_stages.with_relogin(adapter, ledger, result, "thread_read",
                      collection_stages.stage_thread_read, adapter, ledger, result, deadline,
                      exclude_projects=ack_exclusions)
        _require_closed_transaction(ledger, result)

    if args.download_files:
        collection_stages.with_relogin(adapter, ledger, result, "attachments",
                      collection_stages.stage_attachments, adapter, ledger, result, deadline,
                      semantic=sem_on)
        _require_closed_transaction(ledger, result)

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
        _require_closed_transaction(ledger, result)
        if seeded:
            result["trickle_seeded"] = seeded
    # --jobs-only runs exist FOR this work: bigger slice of the
    # window, smaller safety margin than the priority tick
    collection_stages.with_relogin(adapter, ledger, result, "trickle_jobs",
                  job_ops.run_history_jobs,
                  adapter, ledger, result, deadline, trickle=True,
                  trickle_pages=trickle_pages,
                  max_jobs=8 if args.jobs_only else None,
                  min_margin=30 if args.jobs_only else None,
                  semantic=sem_on, ack_exclusions=ack_exclusions)
    _require_closed_transaction(ledger, result)

    # -- post-import reconcile: edits/deletions below the cutoff ---
    collection_stages.with_relogin(adapter, ledger, result, "reconcile",
                  job_ops.run_reconcile_jobs, adapter, ledger, result,
                  deadline, semantic=sem_on, ack_exclusions=ack_exclusions)
    _require_closed_transaction(ledger, result)

    # own identity: name/professions/stations from MCS, persisted
    # as the signal engine's default self (config overrides). Never
    # fails the run — an unusable profile is a logged warning. The
    # profile rarely changes: refresh it on deep (--jobs-only) runs, and
    # on a tick only until the first artifact exists.
    try:
        import self_identity
        if args.jobs_only \
                or not self_identity.latest_self_profile(ledger.db):
            prof = adapter.self_profile()
            _require_closed_transaction(ledger, result)
            with ledger.db:
                if self_identity.record_self_profile(ledger.db, prof):
                    result["self_profile"] = "updated"
            _require_closed_transaction(ledger, result)
            _refresh_station_staff(adapter, ledger, prof, result)
    except TickTransactionOpen:
        raise
    except Exception as e:
        result["errors"].append(f"self_profile: {type(e).__name__}")
    _require_closed_transaction(ledger, result)


def _refresh_station_staff(adapter, ledger, prof, result):
    """The own station roster (task assignee choices) follows the
    profile's cadence. Optional data: a failure keeps the stored roster
    and is reported as ``result["station_staff"]`` — never in
    ``errors``, so it cannot degrade health on every jobs-only run."""
    import self_identity
    try:
        staff = adapter.station_staffs(prof.get("stations") or [])
        _require_closed_transaction(ledger, result)
        if staff:
            with ledger.db:
                if self_identity.record_station_staff(ledger.db, staff):
                    result["station_staff"] = "updated"
    except TickTransactionOpen:
        raise
    except Exception as e:
        result["station_staff"] = f"failed: {type(e).__name__}"


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
    import notify_cards
    import notify_digest
    try:
        # 🌅 queued even under --no-notify; the next flush sends it
        if notify_digest.maybe_enqueue(ledger, cfg):
            result["daily_digest"] = 1
    except Exception as e:
        result["errors"].append(f"daily_digest: {type(e).__name__}")
    _require_closed_transaction(ledger, result)
    try:
        import notify_urgent
        result["urgency_followups"] = notify_urgent.maybe_enqueue(ledger, cfg)
    except Exception as e:
        result["errors"].append(f"urgency_followups: {type(e).__name__}")
    _require_closed_transaction(ledger, result)
    if not args.no_notify:
        try:
            # ⏰ due/overdue task reminders join this tick's flush
            n = notify_cards.task_reminders(ledger, cfg)
            if n:
                result["task_reminders"] = n
        except Exception as e:
            result["errors"].append(f"task_reminders: {type(e).__name__}")
        _require_closed_transaction(ledger, result)
        try:
            result["notify"] = notify_flush.flush(ledger, deadline=deadline)
        except Exception as e:
            result["errors"].append(f"notify: {type(e).__name__}")
        _require_closed_transaction(ledger, result)
    try:
        # live-card sweep: edits/deletes, signal resolves, archive
        # revokes, deferral expiry, stuck spec repair — bounded
        result["card_sweep"] = notify_cards.sweep(
            ledger, cfg, limit=50)
    except Exception as e:
        result["errors"].append(f"card_sweep: {type(e).__name__}")
    _require_closed_transaction(ledger, result)
    try:
        # expired tokens + settled spec payloads — bounded per tick
        result["card_gc"] = notify_cards.gc(ledger, cfg)
    except Exception as e:
        result["errors"].append(f"card_gc: {type(e).__name__}")
    _require_closed_transaction(ledger, result)


# Realtime never waits for background slots or pauses on a night schedule.
SEMANTIC_TAIL_RESERVE_S = 60


def _semantic_skipped(cfg, reason: str, **extra) -> dict:
    mode = semantic_policy.runtime_config(cfg)[0]["mode"]
    return {"mode": mode, "skipped": reason, "done": 0, "deferred": 0,
            "failed": 0, "elapsed_s": 0.0, "job_metrics": [],
            "oldest_pending_job_age_s": None, **extra}


def _run_semantic(ledger, args, cfg, result, deadline, sem_on,
                  run_lock_fd=None):
    """Serve arrival jobs on realtime while residents drain the backlog."""
    if not sem_on:
        return
    if args.jobs_only:
        result["semantic"] = _semantic_skipped(cfg, "background_worker")
        return
    if deadline - time.monotonic() <= SEMANTIC_TAIL_RESERVE_S:
        result["semantic"] = _semantic_skipped(cfg, "no_budget")
        return
    import extract_llm
    if extract_llm.pinned_slot_busy(deadline):
        result["semantic"] = _semantic_skipped(cfg, "slot_busy")
        return
    try:
        result["semantic"] = semantic_drain.run_due(
            ledger, cfg, result, deadline - SEMANTIC_TAIL_RESERVE_S,
            cfg_path=CONF_PATH, max_jobs=4, run_lock_fd=run_lock_fd,
            lane="realtime")
    except Exception as e:
        result["errors"].append(f"semantic: {type(e).__name__}")
        return
    lost = result["semantic"].get("run_lock_lost")
    if lost:
        # the drain stopped without touching its job; end the tick
        raise RunLockLost(held=lost == "held")


def _hard_deadline() -> float | None:
    """When this process is killed (monotonic): the armed watchdog's
    exit time less a margin, or None when no watchdog is armed."""
    if _WATCHDOG_EXIT_AT is None:
        return None
    return _WATCHDOG_EXIT_AT - HARD_DEADLINE_MARGIN_S


def _housekeeping(result):
    """Daily backup, attachment pruning and the daily
    database upkeep — each failure lands in errors without failing the
    run. Backup and upkeep start only when the watchdog leaves them time
    (M7); a skipped backup is recorded, not silently dropped."""
    hard = _hard_deadline()
    try:
        skipped = (maintenance.daily_backup(DB) if hard is None
                   else maintenance.daily_backup(DB, deadline=hard))
        if skipped == "skipped_disk_low":
            result["backup_skipped"] = "disk_low"
        elif skipped == "skipped_deadline":
            result["backup_skipped"] = "deadline"
    except maintenance.MaintenanceError as e:
        result["errors"].append(f"backup: {e}")
    except Exception as e:
        result["errors"].append(f"backup: {type(e).__name__}")
    try:
        upkeep = maintenance.db_maintenance(DB, deadline=hard)
        if upkeep:
            result["db_maintenance"] = upkeep
    except maintenance.MaintenanceError as e:
        result["errors"].append(f"db_maintenance: {e}")
    except Exception as e:
        result["errors"].append(f"db_maintenance: {type(e).__name__}")
    try:
        pruned = maintenance.prune_attachments(DB)
        if pruned:
            result["attachments_pruned"] = pruned
    except Exception as e:
        result["errors"].append(f"prune: {type(e).__name__}")
    try:
        maintenance.prune_leftovers()
    except Exception as e:
        result["errors"].append(f"prune_leftovers: {type(e).__name__}")


def _rotate_logs(result) -> None:
    """Every tick: housekeeping may be deferred for days by slow ticks,
    and rotation is one lstat per log unless a log is over the cap."""
    try:
        maintenance.rotate_log()
    except maintenance.MaintenanceError as e:
        result["errors"].append(f"log_rotate: {e}")  # fixed reason token
    except Exception as e:
        result["errors"].append(f"log_rotate: {type(e).__name__}")


def _overdue_housekeeping(result) -> None:
    """An MCS failure or crash ends the tick before housekeeping; a long outage or
    manual re-login wait must not also stop the local daily backup. Once
    it is overdue (no backup yet is not overdue), run the local-only
    housekeeping — no MCS request, no send — and keep its errors."""
    _rotate_logs(result)
    try:
        if maintenance.backup_overdue():
            _housekeeping(result)
    except Exception as e:
        result["errors"].append(f"housekeeping: {type(e).__name__}")


def _finish_run(ledger, cfg, result, run_id, deadline) -> str:
    """Tail drain, run record and snapshot publish — returns the final
    run status for the health write."""
    if result.get("notify", {}).get("failed") \
            or result.get("notify", {}).get("skipped"):
        result["errors"].append("notify_incomplete")

    result["ok"] = True
    import notify_cards
    import notify_spool
    import notify_cmds
    try:
        # last-chance drain: interactions queued while this tick ran
        _run_stage(result, "finish_commands", deadline,
                   lambda: notify_cmds.drain_int_commands(
                       ledger, result, cfg, os.path.join(HOME, "data"),
                       deadline=deadline, limit=16), required=True)
        _require_closed_transaction(ledger, result)
        notify_spool.publish_flags(cfg, os.path.join(HOME, "data"))
    except TickTransactionOpen:
        raise
    except Exception as e:
        result["errors"].append(f"cmd_int_tail: {type(e).__name__}")
    _require_closed_transaction(ledger, result)
    # Tail command failures must be included in the recorded run
    # before its read-only snapshot is published.
    status = "ok" if not result["errors"] and not result["incomplete"] \
        else "partial"
    ledger.finish_run(run_id, status,
                      "; ".join(result["errors"][:8]))
    try:
        # H4: a full copy + quick_check only when the ledger changed, the
        # notification flag asks for it, or the copy is an hour old
        dirty = notify_cards.snapshot_dirty(ledger)
        state = _run_stage(result, "finish_snapshot", deadline,
                           lambda: maintenance.refresh_snapshot(
                               DB, force=dirty, deadline=_hard_deadline()), required=True)
        result["snapshot"] = state is not None
        result["snapshot_state"] = state
        if state is None:
            import snapshots
            result["snapshot_failure"] = snapshots.last_failure()
            raise RuntimeError("snapshot_verify_failed")
        if state == "published":
            import snapshots
            result["snapshot_timing"] = snapshots.last_timing()
            notify_cards.clear_snapshot_dirty(ledger, dirty)
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
    result["errors"].append(detail)
    try:
        ledger.finish_run(run_id, status, detail)
    except Exception as e:  # health + exit code must still report the failure
        result["errors"].append(f"run_record_failed: {type(e).__name__}")
        if ledger.db.in_transaction:
            return
    if ledger.db.in_transaction:
        return
    try:  # operational alert — silent death is worse than noise
        if alert == "session":
            collection_stages.queue_session_expired(ledger, run_id, detail)
        elif alert:
            # an unthrottled run_failed could alert every 10-minute tick
            collection_stages.queue_run_alert(ledger, "run_failed", run_id, detail)
        if ledger.db.in_transaction:
            return
        # alert=None: the session_recovered notice was already queued by
        # the relogin attempt — the flush below still delivers it
        if not args.no_notify:  # --no-notify suppresses ALL sends;
            result["notify"] = notify_flush.flush(ledger, deadline=deadline)
    except Exception as e:  # queued for a later flush — but visible
        result["errors"].append(f"alert_enqueue_failed: {type(e).__name__}")


def _arm_watchdog(deadline: float, grace_s: int) -> None:
    """Exit a wedged tick after its deadline and explicit operator grace."""
    import faulthandler
    global _WATCHDOG_EXIT_AT
    delay = max(0.001, deadline - time.monotonic() + grace_s)
    _WATCHDOG_EXIT_AT = time.monotonic() + delay
    faulthandler.dump_traceback_later(delay, exit=True)


def _disarm_watchdog() -> None:
    import faulthandler
    global _WATCHDOG_EXIT_AT
    faulthandler.cancel_dump_traceback_later()
    _WATCHDOG_EXIT_AT = None


def _defer_stage(result, name):
    deferred = result.setdefault("deferred_stages", [])
    deferred.append(name)
    # skipped work is not success: the run record must be partial.
    # One compact entry so later errors survive the runs.error [:8] cut.
    errors = result.setdefault("errors", [])
    prev = "deadline_deferred:" + ",".join(deferred[:-1])
    entry = "deadline_deferred:" + ",".join(deferred)
    if prev in errors:
        errors[errors.index(prev)] = entry
    else:
        errors.append(entry)


def _run_stage(result, name, deadline, function, *args, required=False, **kwargs):
    """Measure a stage and defer optional work once the run budget is spent."""
    started = time.monotonic()
    if started >= deadline and not required:
        _defer_stage(result, name)
        return None
    try:
        return function(*args, **kwargs)
    finally:
        finished = time.monotonic()
        durations = result.setdefault("stage_elapsed_s", {})
        durations[name] = round(max(0, finished - started), 3)
        result["run"] = {
            "elapsed_s": round(max(0, finished - (deadline - RUN_DEADLINE_S)), 3),
            "overshoot_s": round(max(0, finished - deadline), 3),
            "slowest_stage": max(durations, key=durations.get),
        }


def main() -> int:
    import local_llm
    with local_llm.pinned_slot(local_llm.REALTIME_SLOT):
        return _main()


def _main() -> int:
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
    ap.add_argument("--metadata-shadow", action="store_true",
                    help="opt-in bounded metadata GETs after contract verification; storage only")
    ap.add_argument("--watchdog-grace", type=int,
                    help="opt-in process watchdog grace seconds (1..3600)")
    ap.add_argument("--commands-only", action="store_true",
                    help="drain cmd/cmd_int command traffic only — no "
                         "adapter, network, ingest, derive or notify; "
                         "the interactive-card watcher job uses this")
    args = ap.parse_args()
    if args.watchdog_grace is not None and not 1 <= args.watchdog_grace <= 3600:
        ap.error("--watchdog-grace must be 1..3600")

    os.makedirs(os.path.join(HOME, "data"), exist_ok=True)

    deadline = time.monotonic() + RUN_DEADLINE_S if args.commands_only else None
    # Command receipts also wait for short writer windows. The existing
    # generation/update fence applies. Only commands count waiting against
    # their run budget; scheduled collection retains its full work budget.
    lock_fd = acquire_run_lock(LOCKFILE)
    if lock_fd is None:
        lock_fd = writer_lock.wait_run_lock(LOCKFILE, wait_s=(LOCK_WAIT_S if deadline is None else min(
                                                LOCK_WAIT_S, max(0, deadline - time.monotonic()))),
                                                expected_code_stamp=_CODE_STAMP,
                                                marker_path=os.path.join(HOME, "data", UPDATE_MARKER_NAME))
    if lock_fd is None:
        print(json.dumps({"ok": False, "error": "lock_held"}))
        return 3

    try:
        code_current = _entry_code_current()
        consent_only = False
        if code_current:
            try:
                update_pending = _entry_update_pending()
            except OSError:
                code_current = False
            else:
                if update_pending:
                    import notify_spool
                    try:
                        hold = notify_spool.restore_awaiting_consent(
                            os.path.join(HOME, 'data'))
                        consent_only = (isinstance(hold, dict)
                                        and hold.get('phase') == 'awaiting_consent'
                                        and not hold.get('unreadable'))
                    except (OSError, ValueError, TypeError, RecursionError):
                        pass
                    code_current = False
        if code_current:
            os.makedirs(collection_stages.ATTACH_DIR, mode=0o700, exist_ok=True)
            os.chmod(collection_stages.ATTACH_DIR, 0o700)
    except BaseException:
        os.close(lock_fd)
        raise
    if consent_only:
        return _consent_only(lock_fd)
    if not code_current:
        os.close(lock_fd)
        print(json.dumps({"ok": False, "error": "code_changed_or_update_pending"}))
        return 3

    if deadline is None:
        deadline = time.monotonic() + RUN_DEADLINE_S
    elif time.monotonic() >= deadline:
        os.close(lock_fd)
        print(json.dumps({"ok": False, "error": "run_deadline_exceeded"}))
        return 3
    if args.commands_only:
        return _commands_only(lock_fd, deadline)
    import notify_spool
    if notify_spool.restore_awaiting_consent(
            os.path.join(HOME, "data")) is not None:
        # schema-bump DB replace held for human consent — ingest,
        # derive, reconcile and sends all stay frozen so the approved
        # loss report cannot drift; only the consent command drains
        return _consent_only(lock_fd)
    adapter = MCSAdapter(token_cache=CACHE)
    adapter.set_deadline(deadline)
    watchdog_armed = args.watchdog_grace is not None
    if args.watchdog_grace is not None:
        _arm_watchdog(deadline, args.watchdog_grace)
    try:
        ledger = Ledger(
            DB, notify_all_replies=(not args.jobs_only
                                    and _config().get("notify_all_replies") is True))
    except Exception:
        if watchdog_armed:
            _disarm_watchdog()
        os.close(lock_fd)
        print(json.dumps({"ok": False, "error": "ledger_init_failed"}))
        return 1
    started = time.time()
    run_id = None
    result = {"run_id": run_id, "ok": False, "projects": 0, "messages": 0,
              "new_messages": 0, "backfilled": 0, "incomplete": [],
              "marked_read": [], "notify": {}, "errors": []}
    cfg = health_report.CONFIG_NOT_LOADED
    try:
        run_id = ledger.begin_run(
            None, kind="deep" if args.jobs_only else "tick")
        result["run_id"] = run_id
        cfg = _config()
        sem_on = _semantic_enabled(ledger, cfg, result)
        notify_max_age_s = _notify_max_age_s(cfg, result)
        ack_exclusions = set()
        _run_stage(result, "fetch", deadline, _stage_fetch,
                   adapter, ledger, args, cfg, result, deadline,
                   run_id, sem_on, notify_max_age_s, ack_exclusions)
        _require_closed_transaction(ledger, result)
        _run_stage(result, "jobs", deadline, _run_jobs,
                   adapter, ledger, args, cfg, result, deadline, sem_on,
                   notify_max_age_s, ack_exclusions)
        _require_closed_transaction(ledger, result)

        # -- derived data ----------------------------------------------
        # jobs-only runs give the realtime LLM slice no budget: the
        # resident backlog workers own background extraction.
        if not _code_changed(result):
            _run_stage(result, "derive", deadline, stage_derive,
                       ledger, result, deadline, cfg,
                       llm_budget_cap=0 if args.jobs_only else 90, run_lock_fd=lock_fd)
        _require_closed_transaction(ledger, result)

        _run_stage(result, "deliver", deadline, _deliver,
                   ledger, args, cfg, result, deadline)
        _require_closed_transaction(ledger, result)
        # 連携サマリー GETs sit behind unread collection and notify (spec:
        # 取得段は unread 収集・通知の後) so a late tick spends its tail
        # on sends, not on summaries
        _run_stage(result, "karte_summary", deadline, collection_stages.with_relogin,
                   adapter, ledger, result, "karte_summary",
                   collection_stages.stage_karte_summary, adapter, ledger, result,
                   deadline, jobs_only=args.jobs_only)
        _require_closed_transaction(ledger, result)
        if not _code_changed(result):
            _run_stage(result, "semantic", deadline, _run_semantic,
                       ledger, args, cfg, result, deadline, sem_on,
                       run_lock_fd=lock_fd)
        _require_closed_transaction(ledger, result)
        _run_stage(result, "metadata_shadow", deadline, _run_metadata_shadow,
                   adapter, ledger, result, deadline, cfg,
                   manual=args.metadata_shadow)
        _require_closed_transaction(ledger, result)
        if args.jobs_only and cfg.get("clinical_metadata") is True and not _code_changed(result):
            import project_metadata
            result["clinical_metadata"] = project_metadata.sync_clinical_metadata(
                ledger, adapter, enabled=True, deadline=deadline)
        _require_closed_transaction(ledger, result)
        # optional, but a slow MCS can spend every tick's budget on unread
        # — once the daily backup is overdue it runs past the deadline
        _rotate_logs(result)
        _run_stage(result, "housekeeping", deadline, _housekeeping, result,
                   required=maintenance.backup_overdue())
        status = _run_stage(result, "finish", deadline, _finish_run,
                            ledger, cfg, result, run_id, deadline, required=True)
        assert isinstance(status, str)
        _require_closed_transaction(ledger, result)
        _write_health(ledger, result, status, run_id=run_id, cfg=cfg)
        _require_closed_transaction(ledger, result)
    except RunLockLost as e:
        if _report_open_transaction(ledger, result):
            return 1
        # An updater won the lock during LLM transport. Held: record
        # the run as partial and stop — no further stage runs old code.
        # Not held: no DB write at all; the 'running' row is closed as
        # 'crashed' by the next begin_run (the crashed-run path).
        result["errors"].append("run_lock_lost")
        if e.held:
            try:
                ledger.finish_run(run_id, "partial",
                                  "; ".join(result["errors"][:8]))
            except Exception as record_error:
                result["errors"].append(
                    f"run_record_failed: {type(record_error).__name__}")
            if _report_open_transaction(ledger, result):
                return 1
            _write_health(ledger, result, "partial", run_id=run_id, cfg=cfg)
            if _report_open_transaction(ledger, result):
                return 1
        print(json.dumps(result, ensure_ascii=False))
        return 1
    except TickTransactionOpen:
        # The tick owns this connection; close will roll back the open work.
        # Do not record a run or send an alert using the uncommitted connection.
        result["ok"] = False
        print(json.dumps(result, ensure_ascii=False))
        return 1
    except SessionExpired as e:
        if _report_open_transaction(ledger, result):
            return 1
        detail = describe_error(e)
        recovered = False
        if "auto_login=" not in detail:
            # an expiry escaping an unwrapped path still earns one
            # recovery attempt before a manual-login alert goes out;
            # the wrapped stages already tried (their detail carries
            # 'auto_login=<state>'), so don't double-attempt them.
            # collection_stages.attempt_relogin enforces the run budget, so a capped run
            # gets 'auto_login=budget_exhausted' with no adapter call.
            state = collection_stages.attempt_relogin(adapter, ledger, result, "run", e)
            detail = f"{detail} auto_login={state}"
            recovered = state == "ok"
        _fail_run(ledger, args, result, run_id, "session_expired",
                  detail, deadline,
                  alert=None if recovered else "session")
        if _report_open_transaction(ledger, result):
            return 1
        _overdue_housekeeping(result)
        _write_health(ledger, result, "session_expired", run_id=run_id, cfg=cfg)
        if _report_open_transaction(ledger, result):
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 2
    except MCSError as e:
        if _report_open_transaction(ledger, result):
            return 1
        _fail_run(ledger, args, result, run_id, "failed", describe_error(e),
                  deadline, alert="run_failed")
        if _report_open_transaction(ledger, result):
            return 1
        _overdue_housekeeping(result)
        _write_health(ledger, result, "failed", run_id=run_id, cfg=cfg)
        if _report_open_transaction(ledger, result):
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 1
    except Exception as e:
        if _report_open_transaction(ledger, result):
            return 1
        # outermost boundary — non-MCSError crashes (AttributeError, sqlite,
        # ...) must still record a failed run and alert, not die silently
        with suppress(Exception):
            _fail_run(ledger, args, result, run_id, "failed",
                      f"crash: {type(e).__name__}", deadline,
                      alert="run_failed")
        if _report_open_transaction(ledger, result):
            return 1
        result["errors"].append(f"crash_site: {_crash_site(e)}")
        _overdue_housekeeping(result)
        _write_health(ledger, result, "failed", run_id=run_id, cfg=cfg)
        if _report_open_transaction(ledger, result):
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 1
    finally:
        if watchdog_armed:
            _disarm_watchdog()
        try:
            ledger.close()
        finally:
            os.close(lock_fd)

    result["elapsed_s"] = round(time.time() - started, 1)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
