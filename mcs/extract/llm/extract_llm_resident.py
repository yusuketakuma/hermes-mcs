"""Run the resident extraction controller while preserving carry and update fences."""
import argparse
import extract_llm_call_context
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import functools
import time
import semantic_runtime
import semantic_policy
import writer_lock
import inference_version as inference
import cloud_backlog
import cloud_inference
import cloud_policy
import extract_validation
import local_llm
from ledger import Ledger, LedgerReader
from mcs_util import CONF_PATH, HOME, load_config
from writer_lock import acquire_run_lock
import llm_availability
from llm_availability import circuit_open_s, disk_floor_mb, disk_free_mb

from extract_llm_admission import (
    legacy_admissions,
)
from extract_llm_carry import (
    _settle_carry,
)
from extract_llm_errors import (
    _write_lock,
    revive_failed,
)
from extract_llm_provider import (
    _resolved_llm,
)
from extract_llm_runner import (
    run_pending,
)
from extract_llm_selection import (
    _IDLE_FLOOR_S,
    _next_due_in,
    _next_retry,
    patient_extraction_complete,
    upgrade_status,
)
from extract_llm_source_contract import (
    DB,
    EXTRACT_VERSION,
    SEMANTIC_FIRST_S,
    _ARRIVAL_CARRY_S,
    _BATCH_K,
    _INFERENCE_VERSION,
    _LOADED_SOURCE_DIGESTS,
    _LOADED_SOURCE_PATHS,
    _RESIDENT_STARTUP_POLL_S,
    _RESIDENT_STARTUP_WAIT_S,
    _RESIDENT_WRITE_WAIT_S,
    _APPROVED_FIRST_TURNS,
    _SEMANTIC_RECENT_TURNS,
    _writer_current,
    _writer_current_diagnostic,
)

_SEMANTIC_WORK = ("done", "retry", "failed", "deferred_short", "error")
# Bounded wait for the run lock on the background semantic slot. The
# slot only runs while this worker has no carry work, so waiting costs
# idle capacity. Measured production holds run ~7min between releases,
# so a 60s window almost never sees a release; 150s matches the
# run-check reacquire horizon while staying bounded by job budget.
_SEMANTIC_LOCK_WAIT_S = 150.0
# A Jev-only semantic/QC pass (no local-model stage) is its own bounded
# drain: several jobs up to a time budget, ahead of the lane's local work.
_JEV_DRAIN_MAX_JOBS = 8
_JEV_DRAIN_BUDGET_S = 300.0
# The daytime night lane has no local work: an empty drain waits this long.
_JEV_DRAIN_IDLE_S = 60.0


def _jev_only_semantic(cfg) -> bool:
    """Whether due semantic and QC jobs need Jev alone, from the resolved semantic config."""
    scfg = semantic_policy.runtime_config(cfg)[0]
    return scfg["mode"] != "off" and not semantic_policy.needs_local_model(scfg)


def _recent_arrivals(ledger) -> set:
    """Arrivals the tick's realtime slot still owns (notified recently)."""
    return {r[0] for r in ledger.db.execute(
        "SELECT message_id FROM messages WHERE notified_at >= ?",
        (time.time() - _ARRIVAL_CARRY_S,))}


def _approved_pending(ledger) -> bool:
    """Any approved local rebuild still holding an ACTIVE permit. An unreadable
    approvals table counts as none (fail-closed: prepare grants nothing either)."""
    try:
        import extract_llm_local_rebuild as local_rebuild
        units = local_rebuild.approved_units(ledger.db)
        return any(local_rebuild.permit(ledger, pid, mid, approvals=units)
                   == local_rebuild.ACTIVE for pid, mid in units)
    except sqlite3.Error:
        return False


def _background_semantic(ledger, stop=None):
    """Serve the oldest semantic/QC work: one job, or a bounded drain when it needs Jev alone."""
    import semantic_drain
    cfg = load_config()
    jev_only = _jev_only_semantic(cfg)
    # One job's room: its own budget plus the margin to retake the lock and commit.
    budget = semantic_policy.semantic_config(cfg)[0]["job_budget_seconds"] + 30
    # Called only when this worker has no carry work: a bounded wait is
    # free capacity the old non-blocking try always missed under a busy
    # lock. Keep room for one job so an acquired lock is never spent on
    # an instant defer.
    wait_s = _SEMANTIC_LOCK_WAIT_S
    if stop is not None:
        wait_s = min(wait_s, max(0.0, stop - time.monotonic() - budget))
    lock_fd = (acquire_run_lock() if wait_s <= 0 else
               writer_lock.wait_run_lock(
                   writer_lock.RUN_LOCK, wait_s=wait_s,
                   expected_code_stamp=writer_lock.code_stamp(),
                   marker_path=os.path.join(
                       HOME, "data", writer_lock.UPDATE_MARKER_NAME)))
    if lock_fd is None:
        return {"skipped": "lock_busy"}
    try:
        now = time.monotonic()
        # The drain cap only closes the window for STARTING jobs; a job
        # already in flight keeps its own budget to retake the lock and commit.
        start_by = now + _JEV_DRAIN_BUDGET_S if jev_only else None
        deadline = (start_by or now) + budget
        if stop is not None:
            deadline = min(deadline, stop)
            start_by = None if start_by is None else min(start_by, stop)
        result = {"errors": []}
        out = semantic_drain.run_due(
            ledger, cfg, result, deadline, max_jobs=_JEV_DRAIN_MAX_JOBS if jev_only else 1,
            start_by=start_by,
            cfg_path=CONF_PATH, run_lock_fd=lock_fd, lane="backlog",
            prioritize_patients=cloud_inference.resolve_cloud_settings(cfg)["mode"] == "on")
        out["errors"] = result["errors"]
        return out
    finally:
        os.close(lock_fd)


def _context_memory_available() -> bool:
    """Admit one more worker under the pool's shared memory reserve; unknown pressure defers."""
    headroom = cloud_backlog.memory_headroom()
    return headroom is not None and headroom >= 0


_CLOUD_OPERATING_LIMITS = semantic_runtime.CLOUD_OPERATING_LIMITS


def _same_but_operating_limits(current, cfg):
    """A cloud budget, rate or concurrency edit must not discard a context unit
    that is waiting for its write; the next HTTP attempt re-reads them anyway."""
    def strip(c):
        c = dict(c) if isinstance(c, dict) else c
        if isinstance(c, dict) and isinstance(c.get("cloud_llm"), dict):
            c["cloud_llm"] = {k: v for k, v in c["cloud_llm"].items() if k not in _CLOUD_OPERATING_LIMITS}
            if not c["cloud_llm"]:
                del c["cloud_llm"]
        if isinstance(c, dict) and isinstance(c.get("semantic"), dict):
            # The budget CLI updates both providers' end times together.
            c["semantic"] = {k: v for k, v in c["semantic"].items() if k != "until"}
            if not c["semantic"]:
                del c["semantic"]
        return c
    return strip(current) == strip(cfg)


# A local lane takes over the cloud patients once its probes have seen no context child for
# this long. It exceeds the pool's longest child restart backoff (600 s), so a pool restarting
# its children is not taken over; a drainer the standalone host restarts may wait longer.
CHILD_ABSENT_GRACE_S = 900
# The lane's ledger keeps (first, last) monotonic times of the probes that saw no child.
_CHILD_ABSENT = "_context_child_absent"
# The lane's ledger marks a takeover that has been recorded, so it is recorded once.
_TAKEOVER = "_context_takeover"
# The lane's ledger keeps (until, reason) of the pause after a context pass that waits.
_CONTEXT_PAUSE = "_context_pause"
# The pause after a takeover pass whose patients all wait for a context child: the wait of a
# pass that found another worker in the selection (``patient_context_selection_busy``).
_CHILD_WAIT_PAUSE_S = 20


def context_lane_active(slot=None):
    """Whether this resident lane serves patient context now: lane 3 always, lane 1 at night."""
    import llm_slot_schedule
    slot = extract_llm_call_context._SLOT_OVERRIDE if slot is None else slot
    return slot == local_llm.LANES[3] or slot == local_llm.LANES[1] and llm_slot_schedule.night()


def _context_role(ledger, *, cloud_worker, cloud_on):
    """The patients this worker serves (``patient_context_refresh.ROLES``).

    A child serves only the cloud's patients while a local lane is alive, and every patient
    otherwise. With the cloud on, a local lane serves the other patients while a child is alive
    or cannot be observed, and every patient once no child has served for the grace period."""
    import cloud_backlog
    if cloud_worker:
        return "cloud" if cloud_backlog.role_alive(HOME, cloud_backlog.ROLE_LOCAL) is True else "any"
    if not cloud_on:
        ledger.__dict__.pop(_CHILD_ABSENT, None)
        ledger.__dict__.pop(_TAKEOVER, None)
        return "any"
    alive = cloud_backlog.role_alive(HOME, cloud_backlog.ROLE_CLOUD)
    if (alive is None) != ledger.__dict__.get("_context_role_probe_unavailable", False):
        # One record when the probe becomes unavailable, not one per pass.
        ledger.__dict__["_context_role_probe_unavailable"] = alive is None
        if alive is None:
            print(json.dumps({"event": "context_role_probe_unavailable", "pid": os.getpid(), "ts": time.time()}),
                  file=sys.stderr, flush=True)
    if alive is not False:
        ledger.__dict__.pop(_CHILD_ABSENT, None)
        ledger.__dict__.pop(_TAKEOVER, None)
        return "local"
    now = time.monotonic()
    since, last = ledger.__dict__.get(_CHILD_ABSENT, (now, now))
    if now - last > CHILD_ABSENT_GRACE_S:
        # Probes this far apart do not show a continuous absence: a child may have served in
        # between (the lane was off, not admitted or extracting). The grace starts again.
        since = now
    ledger.__dict__[_CHILD_ABSENT] = (since, now)
    if now - since < CHILD_ABSENT_GRACE_S:
        ledger.__dict__.pop(_TAKEOVER, None)
        return "local"
    if not ledger.__dict__.get(_TAKEOVER):
        # One record when the lane starts pinning cloud patients' sources locally, not one per pass.
        ledger.__dict__[_TAKEOVER] = True
        print(json.dumps({"event": "context_local_takeover", "pid": os.getpid(), "ts": time.time(),
                          "absent_s": int(now - since)}), file=sys.stderr, flush=True)
    return "local_takeover"


def _background_context_refresh(ledger, initial_state, stop=None, *, cloud_worker=False):
    """Refresh one patient context in its cloud child or a local lane, with shared guards."""
    import llm_slot_schedule
    slot = extract_llm_call_context._SLOT_OVERRIDE
    if not cloud_worker and not context_lane_active(slot):
        return {"status": "off", "done": 0}
    try:
        cfg = load_config()
        import patient_context_refresh
        if not patient_context_refresh.options(cfg)["enabled"]:
            return {"status": "off", "done": 0}
    except Exception as exc:
        return {"status": "error", "done": 0, "error": type(exc).__name__}
    if not _writer_current(full=True):
        return {"status": "source_changed", "done": 0, "run_lock_lost": "updated"}
    deadline = time.monotonic() + 900
    if stop is not None:
        deadline = min(deadline, stop)
    if not cloud_worker and slot != local_llm.LANES[3]:
        # Lane 1 serves context only until 08:00; a call that cannot finish by then starts nothing.
        import patient_context_refresh_state as context_state
        # The monotonic reference is read before the wall clock, so a delay between the two
        # reads can only bring the deadline earlier than 08:00, never later.
        started = time.monotonic()
        remaining = llm_slot_schedule.seconds_until_day()
        if remaining < context_state.CALL_SECONDS + context_state.COMMIT_MARGIN + 5:
            return {"status": "deferred", "done": 0}
        deadline = min(deadline, started + remaining)
    if not cloud_worker:
        until, reason = ledger.__dict__.get(_CONTEXT_PAUSE, (0.0, None))
        if time.monotonic() < until:
            # The pause after a waiting pass: no scan and no selection lock until it ends.
            return {"status": "deferred", "done": 0, "reason": reason}

    def admit():
        # Unknown capacity is not idle: memory, disk, breaker, updater
        # and an unchanged config all have to be proven.
        if (writer_lock.updater_state() != initial_state or initial_state[0]):
            return False
        if cloud_worker and not cloud_backlog.owner_alive():
            return False
        if not _same_but_operating_limits(load_config(), cfg):
            return False
        free = disk_free_mb(ledger.db)
        return free is not None and free >= disk_floor_mb()

    def local_admit():
        return admit() and not circuit_open_s(ledger.db) and _context_memory_available()

    try:
        cloud_cfg = cloud_inference.resolve_cloud_settings(cfg)
        cloud_on = cloud_cfg["mode"] == "on"
        if cloud_worker and not cloud_on:
            return {"status": "off", "done": 0}
        # A local lane sends nothing: with the cloud on it admits like any local work.
        cloud_enabled = cloud_cfg["mode"] != "off" and not (cloud_on and not cloud_worker)
        if time.monotonic() >= deadline or not (admit() if cloud_enabled else local_admit()):
            result = {"status": "deferred", "done": 0}
        else:
            role = _context_role(ledger, cloud_worker=cloud_worker, cloud_on=cloud_on)
            with local_llm.pinned_slot(local_llm.LANES[3] if cloud_worker else slot):
                result = patient_context_refresh.run_due(
                    ledger, cfg, deadline=deadline, admit=admit if cloud_enabled else local_admit,
                    **({"local_admit": local_admit} if cloud_enabled else {}),
                    **({"role": role} if role != "any" else {}),
                    complete=functools.partial(patient_extraction_complete, ledger))
            if not cloud_worker:
                # A lane paces its context passes like the children (a cloud wait or a pass
                # that found only other-role patients waits a minute); shorter waits keep the
                # lane loop's own idle floor. A unit waiting for a child holds back only its
                # patient, which the next passes skip: the lane goes on with the others, and
                # only a pass that found nothing but such patients waits, as briefly as a pass
                # that found the selection busy.
                delay = (_CHILD_WAIT_PAUSE_S if result.get("reason") == patient_context_refresh.CHILD_WAIT_REASON
                         else cloud_backlog._context_retry_delay(result))
                if delay > _IDLE_FLOOR_S:
                    ledger.__dict__[_CONTEXT_PAUSE] = (time.monotonic() + delay, result.get("reason"))
    except Exception as exc:
        # Context refresh failures must not interrupt collection or normal extraction.
        result = {"status": "error", "done": 0, "error": type(exc).__name__}
    if writer_lock.updater_state() != initial_state:
        result["run_lock_lost"] = "updated"
    return result


def main() -> int:
    previous = extract_llm_call_context._SLOT_OVERRIDE
    turn = os.environ.get(local_llm.TURN_LOCK_ENV)
    try:
        with local_llm.pinned_slot(local_llm.BACKGROUND_SLOT), local_llm.scheduling_scope(
                os.path.join(HOME, "data")):
            return _main()
    finally:
        extract_llm_call_context._SLOT_OVERRIDE = previous
        if turn is None:
            os.environ.pop(local_llm.TURN_LOCK_ENV, None)
        else:
            os.environ[local_llm.TURN_LOCK_ENV] = turn


_RESIDENT_FENCE_LOST = object()


_RESIDENT_SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resident_source_state():
    def fail(error):
        raise error

    files = []
    for base, _dirs, names in os.walk(_RESIDENT_SOURCE_ROOT, onerror=fail):
        for name in names:
            if name.endswith(".py"):
                path = os.path.join(base, name)
                st = os.stat(path)
                files.append((os.path.relpath(path, _RESIDENT_SOURCE_ROOT), st.st_dev, st.st_ino,
                              st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
    if not files:
        raise OSError("resident_source_unavailable")
    return tuple(sorted(files))


def _resident_loaded_sources_current():
    if _LOADED_SOURCE_PATHS.keys() != _LOADED_SOURCE_DIGESTS.keys():
        return False
    try:
        return all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == _LOADED_SOURCE_DIGESTS[name]
                   for name, path in _LOADED_SOURCE_PATHS.items())
    except (OSError, ValueError, TypeError):
        return False


def _open_current_ledger(*, deadline=None, distinguish_fence=False, source_state=None):
    fd = writer_lock.acquire_run_lock()
    if fd is None:
        writer_lock.record_writer_refusal("run_lock_not_acquired", "ledger_lock")
        return None
    try:
        if not _writer_current_diagnostic(full=True):
            return _RESIDENT_FENCE_LOST if distinguish_fence else None
        if source_state is not None:
            try:
                if _resident_source_state() != source_state:
                    writer_lock.record_writer_refusal("resident_sources_changed", "startup_source_fence")
                    return _RESIDENT_FENCE_LOST if distinguish_fence else None
                if not _resident_loaded_sources_current():
                    writer_lock.record_writer_refusal("loaded_sources_unverified", "startup_source_fence")
                    return _RESIDENT_FENCE_LOST if distinguish_fence else None
                if not _writer_current_diagnostic(full=True):
                    return _RESIDENT_FENCE_LOST if distinguish_fence else None
            except OSError:
                writer_lock.record_writer_refusal("resident_sources_unavailable", "startup_source_fence")
                return _RESIDENT_FENCE_LOST if distinguish_fence else None
        if deadline is not None and time.monotonic() >= deadline:
            writer_lock.record_writer_refusal("startup_deadline_reached", "ledger_startup")
            return None
        return Ledger(DB)
    finally:
        os.close(fd)


def _open_resident_ledger(deadline):
    waiting = False
    try:
        source_state = _resident_source_state()
    except OSError:
        writer_lock.record_writer_refusal("resident_sources_unavailable", "startup_source_fence")
        return None
    while _writer_current_diagnostic(full=True):
        if time.monotonic() >= deadline:
            writer_lock.record_writer_refusal("startup_deadline_reached", "resident_startup")
            return None
        led = _open_current_ledger(deadline=deadline, distinguish_fence=True, source_state=source_state)
        if led is _RESIDENT_FENCE_LOST:
            return None
        if led is not None:
            print(json.dumps({"event": "extractor_ledger_opened", "pid": os.getpid()}),
                  file=sys.stderr, flush=True)
            return led
        if not _writer_current_diagnostic(full=True):
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            writer_lock.record_writer_refusal("startup_deadline_reached", "resident_startup")
            return None
        if not waiting:
            print(json.dumps({"event": "extractor_waiting_for_writer", "pid": os.getpid()}),
                  file=sys.stderr, flush=True)
            waiting = True
        time.sleep(min(_RESIDENT_STARTUP_POLL_S, remaining))
    return None


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upgrade-status", action="store_true", help="read-only aggregate upgrade status; no LLM calls")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--budget", type=float, default=180)
    ap.add_argument("--revive-failed", action="store_true",
                    help="give exhausted error rows one more bounded "
                         "attempt, then exit")
    ap.add_argument("--all", action="store_true",
                    help="drain the whole backlog, then stay resident "
                         "polling for new work — use --stop-after for a "
                         "bounded run (ignores budget pacing)")
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent worker tasks; shared admission schedules physical "
                         "slots by day/night and realtime priority; extra workers "
                         "do not add physical LLM slots")
    ap.add_argument("--stop-after", type=float, default=0,
                    help="with --all: stop after N seconds (bounded run)")
    ap.add_argument("--lane", type=int, default=None,
                    help="with --all: preferred backlog lane 1/2/3; lane 1 works "
                         "only 20:00–08:00 JST, yielding to realtime requests")
    ap.add_argument("--shard", default=None, metavar="I/N",
                    help="with --all: process only message_id %% N == I "
                         "— disjoint shards let two drainers share the "
                         "backlog without re-doing each other's rows")
    ap.add_argument("--semantic", action="store_true",
                    help="with --all: also serve the semantic/QC backlog "
                         "(a bounded drain when it needs Jev alone)")
    ap.add_argument("--batch", type=int, default=_BATCH_K,
                    help="context-free single-chunk messages per "
                         "batched LLM call (0-8; 0 disables — every "
                         "message gets its own call)")
    args = ap.parse_args()
    if args.upgrade_status:
        led = LedgerReader(DB)
        try:
            print(json.dumps(upgrade_status(led.db, model=_resolved_llm()[1])))
        finally:
            led.close()
        return 0
    if args.revive_failed:
        # Keep the constructor and its bookkeeping in the same writer lifetime.
        with writer_lock.capture_writer_refusal() as diagnostic, _write_lock(True, wait_s=0) as locked:
            if not locked or not _writer_current_diagnostic(full=True):
                print(json.dumps({"ok": False, "error": "writer_unavailable", **diagnostic}))
                return 1
            led = Ledger(DB)
            try:
                print(json.dumps(revive_failed(led), ensure_ascii=False))
            finally:
                led.close()
        return 0
    if (not math.isfinite(args.budget) or not math.isfinite(args.stop_after)
            or args.stop_after < 0):
        print(json.dumps({"ok": False, "error": "bad_budget"}))
        return 2
    if not (0 <= args.batch <= 8):
        print(json.dumps({"ok": False, "error": "bad_batch"}))
        return 2
    if not args.all and (args.shard or args.lane is not None
                         or args.stop_after or args.semantic):
        print(json.dumps({"ok": False, "error": "resident options require --all"}))
        return 2
    shard = None
    if args.shard:
        try:
            i_s, n_s = args.shard.split("/", 1)
            i, n = int(i_s), int(n_s)
            if not (0 <= i < n and n >= 2):
                raise ValueError
            shard = (i, n)
        except ValueError:
            print(json.dumps({"ok": False, "error": "bad_shard"}))
            return 2
    if args.all and args.lane is None:
        args.lane = 2
    if args.lane is not None:
        if args.lane not in local_llm.LANES:
            print(json.dumps({"ok": False, "error": "bad_lane"}))
            return 2
        extract_llm_call_context._SLOT_OVERRIDE = local_llm.lane_slot(args.lane)
        os.environ["MCS_LLM_SLOT"] = str(extract_llm_call_context._SLOT_OVERRIDE)
        os.makedirs(os.path.join(HOME, "data"), exist_ok=True)
        os.environ[local_llm.TURN_LOCK_ENV] = os.path.join(
            HOME, "data", "llm_turn.lock")
    print(json.dumps({"event": "extractor_started", "pid": os.getpid(), "ts": time.time(),
                      "extract_version": EXTRACT_VERSION, inference.VERSION_KEY: _INFERENCE_VERSION,
                      "source_digests": _LOADED_SOURCE_DIGESTS}),
          file=sys.stderr, flush=True)
    stop = (time.monotonic() + args.stop_after
            if args.all and args.stop_after > 0 else None)
    with writer_lock.capture_writer_refusal() as diagnostic:
        if args.all:
            startup_deadline = time.monotonic() + _RESIDENT_STARTUP_WAIT_S
            if stop is not None:
                startup_deadline = min(startup_deadline, stop)
            led = _open_resident_ledger(startup_deadline)
        else:
            led = _open_current_ledger()
        if led is None:
            print(json.dumps({"ok": False, "error": "writer_unavailable", **diagnostic}))
            return 1
    # Canonical mode admits only explicitly active legacy conversions;
    # the tick and resident share the same fail-closed admission reader.
    def _admitted():
        # One config read per call serves both the admission gate and the
        # vital-threshold policy, so a resident loop picks up edits to
        # either on the same snapshot.
        try:
            cfg = load_config()
        except Exception:
            return set(), None
        try:
            return legacy_admissions(led, cfg), extract_validation.vital_threshold_policy(cfg)
        except Exception:
            return set(), None
    cloud_pool = cloud_backlog.CloudBacklogPool(HOME, context_only=True) if args.all and args.lane == 2 else None
    # This lane's ROLE_LOCAL hold while it serves patient context; context children read it.
    local_role = [None]
    try:
        if args.all:
            # Backlog drainer: per-write locking only — holding the run
            # lock across the whole backlog would starve the 10-min tick
            def pause(seconds):
                remaining = seconds if stop is None else min(seconds, stop - time.monotonic())
                if remaining > 0:
                    time.sleep(remaining)

            def idle(seconds):
                # wake for the earliest future due job, or at the floor while
                # a finished result is carried (保存待ち), instead of the fixed poll
                due = 0.0 if carry else _next_due_in(
                    led, time.time(), semantic=args.semantic, context=context_lane)
                if due is not None:
                    seconds = min(seconds, max(_IDLE_FLOOR_S, due))
                print(json.dumps({"idle_s": round(seconds, 1),
                                  "due_in_s": None if due is None else round(due, 1),
                                  "carried": len(carry), "lane": args.lane, "pid": os.getpid(),
                                  "ts": time.time()}), flush=True)
                pause(seconds)

            total = {"done": 0, "failed": 0, "left": 0}
            context_lane = context_lane_active()
            semantic_first_until = 0.0
            # Start fairness only after a semantic pass has actually worked.
            # Waiting or an expired priority window must not reset it.
            semantic_has_worked = False
            semantic_recent_turns = 0
            approved_recent_turns = 0
            semantic_archive_turn = False
            context_state = writer_lock.updater_state()
            carry: list = []   # finished results waiting for the run lock
            idle_delay = _IDLE_FLOOR_S
            pool_error = None   # one record per failure streak of the cloud pool sync

            def retire():
                try:
                    _settle_carry(led, carry, write=False)
                except Exception as error:
                    # Remaining claims stay protected by their leases.
                    print(json.dumps({"event": "extract_carry_release_error",
                                      "error": type(error).__name__, "ts": time.time()}),
                          file=sys.stderr, flush=True)
                return 0

            def carry_waiting():
                # The lane 1/2 resume record has the same shape; carry rows are never logged.
                print(json.dumps({"carry_waiting": True, "carried": len(carry), "lane": args.lane,
                                  "pid": os.getpid(), "ts": time.time()}), flush=True)

            def semantic_pass():
                # One background semantic/QC pass, logged; it reports whether semantic work ran.
                nonlocal semantic_has_worked, semantic_first_until
                started = time.monotonic()
                sem = _background_semantic(led, stop)
                llm_availability.record_semantic_turn(
                    os.path.join(HOME, "data", llm_availability.SEMANTIC_TURNS_NAME),
                    sem.get("skipped") == "lock_busy")
                print(json.dumps({"semantic": sem, "lane": args.lane, "pid": os.getpid(),
                                  "elapsed_s": round(time.monotonic() - started, 1),
                                  "ts": time.time()},
                                 ensure_ascii=False), flush=True)
                ran = any(m.get("kind") == "semantic" and m.get("status") in _SEMANTIC_WORK
                          for m in sem.get("job_metrics") or [])
                if ran:
                    semantic_has_worked = True
                    semantic_first_until = time.monotonic() + SEMANTIC_FIRST_S
                return sem, ran

            while True:
                if not _writer_current(full=True):
                    return retire()
                if stop is not None and time.monotonic() >= stop:
                    total["stopped"] = "stop_after"
                    break
                try:
                    # Lane 3 serves patient context always, lane 1 only at night (context first,
                    # extraction when no context is due). The role hold follows it, so that a
                    # daytime lane 1 never hides local patients from the context children.
                    context_lane = context_lane_active()
                    if context_lane and local_role[0] is None:
                        local_role[0] = cloud_backlog.hold_role(HOME, cloud_backlog.ROLE_LOCAL)
                    elif not context_lane and local_role[0] is not None:
                        os.close(local_role[0])
                        local_role[0] = None
                    sem, ran_semantic = None, False
                    jev_drain = args.semantic and _jev_only_semantic(load_config())
                    if jev_drain:
                        # Jev-only semantic/QC work takes no local-model turn or
                        # slot, so neither the extraction nor a carry waits on it.
                        sem, ran_semantic = semantic_pass()
                        if sem.get("run_lock_lost") in ("held", "updated"):
                            _settle_carry(led, carry, write=False)
                            return 0
                    if args.lane == 1:
                        import llm_slot_schedule
                        if not llm_slot_schedule.night():
                            # A busy database here is the loop's transient error, not an exit.
                            _settle_carry(led, carry, final=False, fence=context_state)
                            if carry:
                                carry_waiting()
                            pause(1 if sem and (sem.get("done") or sem.get("progressed"))
                                  else _JEV_DRAIN_IDLE_S if jev_drain else _IDLE_FLOOR_S)
                            continue
                    if carry and not context_lane:
                        resume_budget = _RESIDENT_WRITE_WAIT_S + 1
                        if stop is not None:
                            resume_budget = min(resume_budget, max(0.0, stop - time.monotonic()))
                        if resume_budget <= 0:
                            total["stopped"] = "stop_after"
                            break
                        started = time.monotonic()
                        resumed = run_pending(led, limit=1, budget_s=resume_budget,
                            per_write_lock=True, write_wait_s=_RESIDENT_WRITE_WAIT_S, carry=carry)
                        elapsed = time.monotonic() - started
                        carry = resumed.pop("carry", carry)
                        if resumed.get("updated"):
                            return retire()
                        total["done"] += resumed["done"]
                        total["failed"] += resumed["failed"]
                        total["left"] = resumed["left"]
                        print(json.dumps({**resumed, "lane": args.lane, "pid": os.getpid(),
                                          "elapsed_s": round(elapsed, 1), "turn": "carry",
                                          "ts": time.time()}, ensure_ascii=False), flush=True)
                        if carry:
                            pause(2)
                            continue
                    if cloud_pool is not None:
                        try:
                            if cloud_pool.sync(load_config(), ledger=led):
                                pool_error = None   # a throttled or still-failing pool decided nothing
                        except Exception as error:
                            # The pool is extra capacity: its failure must not
                            # skip this lane's own extraction and semantic work.
                            if pool_error != type(error).__name__:
                                pool_error = type(error).__name__
                                print(json.dumps({"event": "cloud_pool_sync_error",
                                                  **cloud_backlog._exception_observation(error),
                                                  "pid": os.getpid(), "ts": time.time()}),
                                      file=sys.stderr, flush=True)
                    if context_lane:
                        # lane 3 serves patient context first; it yields to
                        # semantic/extraction only when nothing is due. A
                        # carried result gets its lock attempt first and its
                        # claim lease renewed, so the context units it waits
                        # through cannot outlive the claim.
                        if writer_lock.updater_state() != context_state:
                            # an updater ran since the last pass: old code
                            # must not write the carried result either
                            _settle_carry(led, carry, write=False)
                            return 0
                        _settle_carry(led, carry, final=False, fence=context_state)
                        if carry:
                            carry_waiting()
                            pause(2)
                            continue
                        context_refresh = _background_context_refresh(led, context_state, stop)
                        if context_refresh.get("status") != "off":
                            print(json.dumps({"patient_context": context_refresh, "ts": time.time()},
                                             ensure_ascii=False), flush=True)
                        if context_refresh.get("run_lock_lost") == "updated":
                            _settle_carry(led, carry, write=False)
                            return 0
                        if context_refresh.get("done"):
                            pause(1)
                            continue
                    # the inner batch must also honor --stop-after: its
                    # budget is the remaining allowance, not a fresh 3600 s
                    # (F13)
                    budget = 3600
                    if stop is not None:
                        budget = min(budget, stop - time.monotonic())
                        if budget <= 0:
                            total["stopped"] = "stop_after"
                            break
                    # One extraction per lane before semantic/QC; a bounded
                    # oldest-first turn gives admitted history a selection
                    # opportunity during continuous semantic and new work.
                    # The existing admission and lease gates apply.
                    adm, vpol = _admitted()
                    semantic_priority = (args.semantic
                                         and time.monotonic() < semantic_first_until)
                    oldest_turn = (args.semantic and semantic_has_worked
                                   and semantic_recent_turns >= _SEMANTIC_RECENT_TURNS)
                    recent_window = semantic_priority and not oldest_turn
                    # Continuous semantic success keeps extending the arrival
                    # window, so a recent-only turn can run forever and an
                    # approval added after startup would never get a slot.
                    # Every _APPROVED_FIRST_TURNS windowed turns one normal
                    # turn runs instead; arrivals lose at most that one turn.
                    approved_turn = False
                    if recent_window:
                        if approved_recent_turns >= _APPROVED_FIRST_TURNS:
                            approved_recent_turns = 0
                            approved_turn = _approved_pending(led)
                        else:
                            approved_recent_turns += 1
                        recent_window = not approved_turn
                    window = None
                    if recent_window:
                        recent = _recent_arrivals(led)
                        window = recent if adm is None else adm & recent
                    cloud_turn = oldest_turn or not args.semantic
                    cloud_cfg = cloud_inference.resolve_cloud_settings(load_config())
                    extract_limit = (cloud_cfg["max_concurrency"] if cloud_turn
                                     and cloud_cfg["mode"] == "on"
                                     and cloud_policy.send_route("reextract") else 1)
                    turn = ("oldest" if oldest_turn else "recent_only" if recent_window
                            else "approved" if approved_turn else "normal")
                    started = time.monotonic()
                    r = run_pending(led, limit=extract_limit if args.semantic else 8,
                                    budget_s=min(budget, 900),
                                    oldest_first=oldest_turn,
                                    archived_first=oldest_turn and semantic_archive_turn and cloud_cfg["mode"] != "on",
                                    fair_oldest=oldest_turn,
                                    prioritize_patients=cloud_turn and cloud_cfg["mode"] == "on",
                                    per_write_lock=True,
                                    vital_policy=vpol,
                                    workers=max(1, min(args.workers, 8)),
                                    shard=shard, batch_k=args.batch,
                                    admitted_ids=adm if window is None else window,
                                    approved_first=not oldest_turn and not recent_window,
                                    approved_fill=recent_window,
                                    approved_adm_ids=adm if recent_window else None,
                                    allow_cloud=True, background=True,
                                    write_wait_s=_RESIDENT_WRITE_WAIT_S,
                                    carry=carry)
                    elapsed = time.monotonic() - started
                    if oldest_turn:
                        semantic_archive_turn = not semantic_archive_turn
                    if oldest_turn:
                        semantic_recent_turns = 0
                    elif args.semantic and semantic_has_worked:
                        semantic_recent_turns += 1
                    if r.get("updated"):
                        # Preserve an incoming carry when an early fence check
                        # returned before run_pending could include it.
                        if "carry" in r:
                            carry = r.pop("carry") or []
                        return retire()
                    carry = r.pop("carry", None) or []  # internal; never logged
                    if args.semantic and not jev_drain and not carry:
                        # Work with a local-model stage keeps one job per extraction.
                        sem, ran_semantic = semantic_pass()
                        if sem.get("run_lock_lost") in ("held", "updated"):
                            # an updater ran mid-transport: exit so launchd /
                            # the standalone host respawns this resident on
                            # the new code instead of re-locking. A plain
                            # reacquire timeout under unchanged code left the
                            # job pending — stay resident.
                            _settle_carry(led, carry, write=False)
                            return 0
                except Exception as e:
                    # a transient failure ('database is locked') must not
                    # kill the resident worker — log, back off, retry
                    print(json.dumps({"event": "extract_loop_error",
                                      **cloud_backlog._exception_observation(e),
                                      "error": type(e).__name__, "pid": os.getpid(),
                                      "ts": time.time()}),
                          file=sys.stderr, flush=True)
                    pause(30)
                    continue
                total["done"] += r["done"]
                total["failed"] += r["failed"]
                total["left"] = r["left"]
                print(json.dumps({**r, "lane": args.lane, "pid": os.getpid(),
                                  "elapsed_s": round(elapsed, 1), "turn": turn,
                                  "ts": time.time()}, ensure_ascii=False),
                      flush=True)
                # a semantic-first pass extracts little: do not idle on it
                if sem and (sem.get("done") or sem.get("progressed")
                            or ran_semantic):
                    pause(1)
                    continue
                if r["left"] == 0 or (
                        r["done"] == 0 and r["failed"] == 0
                        and r.get("selected", 0) == 0):
                    # Queue drained (or only poison-gated/permanent-failure
                    # rows remain — they count in `left` but can never be
                    # selected). Stay resident and poll instead of exiting:
                    # under launchd KeepAlive an exit just means a respawn
                    # every 30 s re-running the full scan forever, and the
                    # poll interval still beats the 10-min tick for picking up newly fetched messages.
                    idle(idle_delay)
                    idle_delay = min(30, idle_delay * 2)
                    continue
                idle_delay = _IDLE_FLOOR_S
                if r["done"] == 0 and (r.get("circuit_open_s")
                                       or r.get("disk_low")):
                    # the LLM lane is gated: re-scanning every few seconds
                    # cannot claim anything until the breaker/disk clears
                    pause(min(120, r.get("circuit_open_s") or 120))
                    continue
                if r["done"] == 0 and r["failed"] == 0:
                    # rows were selected but nothing committed — either
                    # every write was skipped (lock/_current race → short
                    # yield) or all calls deferred on an unreachable
                    # endpoint (server down → longer backoff instead of
                    # re-firing refused connections every few seconds)
                    pause(30 if r.get("deferred") else 5)
                elif r["done"] == 0:
                    # Everything selectable failed or is backed off —
                    # wait for the earliest retry instead of quitting.
                    nt = _next_retry(led, after=time.time())
                    if nt is None:
                        # no retriable error row (e.g. endpoint-down
                        # failures write none): stay resident and poll
                        idle(120)
                        continue
                    idle(min(300.0, max(5.0, nt - time.time())))
                else:
                    pause(1)  # yield the write window between batches
            _settle_carry(led, carry, fence=context_state)
            print(json.dumps(total, ensure_ascii=False))
        else:
            # Bounded single batch: claims lease per row and commits land
            # per-write, so holding the whole-run lock only starved ticks
            # without buying atomicity (the carry-resume call above uses
            # the same per-write configuration).
            adm, vpol = _admitted()
            print(json.dumps(
                run_pending(led, args.limit, args.budget,
                            batch_k=args.batch,
                            vital_policy=vpol,
                            admitted_ids=adm,
                            per_write_lock=True,
                            write_wait_s=_RESIDENT_WRITE_WAIT_S),
                ensure_ascii=False))
    finally:
        if local_role[0] is not None:
            os.close(local_role[0])
        if cloud_pool is not None:
            cloud_pool.close()
        led.close()
    return 0
