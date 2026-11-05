"""Select due semantic work with cohort fairness and bounded cloud observation."""
from __future__ import annotations
import os
import mcs_util
import semantic_store
import semantic_transport
import patient_priority
from functools import wraps, partial
import json
from contextlib import contextmanager
import math
import heapq
import sqlite3
import threading
import time
from mcs_util import load_config, strict_json
from inference_version import (CURRENT_VERSION, is_current,
                               current_meta_pred, read_version, LEGACY_VERSION)
import semantic_runtime as runtime
from semantic_policy import (JOB_KIND, QC_JOB_KIND, runtime_config)
import semantic_notice

from semantic_reprocess import (
    _LONG_DEFERS_MAX,
    _LONG_DEFERS_SCAN_BYTES,
    _REALTIME_KEY,
    _REALTIME_ORDER,
)
from semantic_scheduler_state import (
    _bounded_job_payload,
    _held_cursor_valid,
    _reservation_sched,
    _sched_state,
    _sched_update,
    _sched_write,
)
from semantic_store import _REPROCESS_JOB_MAX_BYTES as _REPROCESS_JOB_MAX_BYTES

def _long_defers(ledger) -> dict:
    """{str(job_id): {"g": generation, "n": free_defers}} — valid entries."""
    raw = _sched_state(ledger).get("long_defers")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items()
            if isinstance(k, str) and k.isdigit() and isinstance(v, dict)
            and isinstance(v.get("g"), str) and type(v.get("n")) is int}


def _known_long_defer_generation(raw):
    if not isinstance(raw, str) or len(raw) > _REPROCESS_JOB_MAX_BYTES:
        return None
    try:
        if len(raw.encode('utf-8')) > _REPROCESS_JOB_MAX_BYTES:
            return None
        payload = strict_json(raw)
        if not isinstance(payload, dict) or read_version(payload) not in (CURRENT_VERSION, LEGACY_VERSION):
            return None
        nodes = [(payload, 0)]
        while nodes:
            node, depth = nodes.pop()
            if depth > 128 or (type(node) is int and not -(2**63) <= node < 2**63):
                return None
            if isinstance(node, dict):
                nodes.extend((value, depth + 1) for value in node.values())
            elif isinstance(node, list):
                nodes.extend((value, depth + 1) for value in node)
        if 'targets' in payload and (not isinstance(payload['targets'], list)
                or not payload['targets']
                or any(type(mid) is not int or not 0 < mid < 2**63 for mid in payload['targets'])):
            return None
        generation = payload.get('generation')
        if generation is not None and not isinstance(generation, str):
            return None
        return runtime.payload_generation(payload)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return None


def _reserve_long_defer(ledger, token):
    if ledger.db.in_transaction:
        raise ValueError('semantic_scheduler_transaction_active')
    ledger.db.execute('BEGIN IMMEDIATE')
    with ledger.db:
        sched, entries = _reservation_sched(ledger)
        import semantic_intake_hold
        try:
            semantic_intake_hold.validate_indexes(ledger.db)
            held = semantic_intake_hold.has_held_root(ledger.db, token.project_id, token.message_id)
        except sqlite3.Error:
            raise ValueError('semantic_intake_hold_unverified') from None
        if held:
            raise ValueError('semantic_intake_held')
        row = ledger.db.execute(
            'SELECT state,attempts FROM fetch_jobs '
            'WHERE job_id=? AND kind=? AND project_id=? AND message_id=?',
            (token.job_id, token.kind, token.project_id, token.message_id)).fetchone()
        raw = _bounded_job_payload(ledger.db, token.job_id, _REPROCESS_JOB_MAX_BYTES) if row else None
        if row is None or row['state'] != token.state or int(row['attempts'] or 0) != token.attempts:
            return None
        generation = _known_long_defer_generation(raw)
        if generation is None:
            raise ValueError('semantic_job_payload_unreadable')
        if raw != token.payload_raw or generation != token.generation:
            return None
        key = str(token.job_id)
        prior = entries.get(key)
        if prior is None and len(entries) >= _LONG_DEFERS_MAX:
            raise ValueError('semantic_long_defers_capacity')
        if prior and prior['g'] != token.generation:
            if semantic_store._reprocess_job_payload(ledger, {'payload': raw}, token.project_id, token.message_id) is None:
                raise ValueError('semantic_long_defer_generation_unknown')
            prior = None
        count = prior['n'] if prior else 0
        if count >= 2**63 - 2:
            raise ValueError('semantic_scheduler_invalid')
        reserved = {'g': token.generation, 'n': count + 1}
        entries[key] = reserved
        sched['long_defers'] = entries
        _sched_write(ledger, sched, commit=False)
        return prior, reserved


def _refund_long_defer(ledger, token, reservation):
    if ledger.db.in_transaction:
        raise ValueError('semantic_scheduler_transaction_active')
    ledger.db.execute('BEGIN IMMEDIATE')
    with ledger.db:
        sched, entries = _reservation_sched(ledger)
        import semantic_intake_hold
        try:
            semantic_intake_hold.validate_indexes(ledger.db)
            held = semantic_intake_hold.has_held_root(ledger.db, token.project_id, token.message_id)
        except sqlite3.Error:
            raise ValueError('semantic_intake_hold_unverified') from None
        if held:
            return
        key = str(token.job_id)
        row = ledger.db.execute(
            'SELECT job_id FROM fetch_jobs WHERE job_id=? AND kind=? AND project_id=? AND message_id=?',
            (token.job_id, token.kind, token.project_id, token.message_id)).fetchone()
        raw = _bounded_job_payload(ledger.db, token.job_id, _REPROCESS_JOB_MAX_BYTES) if row else None
        if (row is None or raw != token.payload_raw
                or _known_long_defer_generation(raw) != token.generation
                or entries.get(key) != reservation[1]):
            return
        if reservation[0] is None:
            entries.pop(key)
        else:
            entries[key] = reservation[0]
        if entries:
            sched['long_defers'] = entries
        else:
            sched.pop('long_defers', None)
        _sched_write(ledger, sched, commit=False)


def _merge_long_defers(ledger, sched: dict, seen: dict) -> None:
    """Fold this pass's long defers into the persisted map and prune
    entries whose job left 'pending' or moved to a new generation."""
    previous = _long_defers(ledger)
    current = dict(previous)
    current.update(seen)
    if not current:
        sched.pop("long_defers", None)
        return
    alive = {}
    budget = _LONG_DEFERS_SCAN_BYTES
    cursor = sched.get('long_defers_scan_cursor', 0)
    if type(cursor) is not int or not 0 <= cursor < 2**63:
        cursor = 0
    last_scanned = None
    for row in ledger.db.execute(
            "SELECT job_id,state,project_id,message_id FROM fetch_jobs WHERE kind=? "
            "AND job_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY CASE WHEN job_id>? THEN 0 ELSE 1 END,job_id",
            (JOB_KIND, json.dumps(sorted(int(k) for k in current)), cursor)):
        key = str(row["job_id"])
        generation = None
        if budget > 0:
            last_scanned = row['job_id']
            limit = min(budget, _REPROCESS_JOB_MAX_BYTES)
            raw = _bounded_job_payload(ledger.db, row['job_id'], limit)
            if isinstance(raw, str):
                try:
                    size = len(raw.encode('utf-8'))
                except UnicodeError:
                    size = limit + 1
                if size <= limit:
                    budget -= size
                    generation = _known_long_defer_generation(raw)
                    if (generation is not None
                            and (row['state'] != 'pending' or current[key]['g'] != generation)
                            and semantic_store._reprocess_job_payload(ledger, {'payload': raw},
                                                       row['project_id'], row['message_id']) is None):
                        generation = None
                else:
                    budget -= limit
            else:
                budget -= limit
        if generation is None:
            alive[key] = previous.get(key, current[key])
        elif row['state'] == 'pending' and current[key]["g"] == generation:
            alive[key] = current[key]
    if last_scanned is not None:
        sched['long_defers_scan_cursor'] = last_scanned
    if len(alive) > _LONG_DEFERS_MAX:
        raise ValueError('semantic_long_defers_capacity')
    if alive:
        sched["long_defers"] = alive
    else:
        sched.pop("long_defers", None)


_ARCHIVED_JOB = ("EXISTS (SELECT 1 FROM patients p "
                 "WHERE p.project_id=fetch_jobs.project_id AND p.is_archived)")


_ELIGIBLE_SQL = "json_valid(payload) AND COALESCE(json_extract(payload,'$.eligible'),0)=1"


_MALFORMED_JOB_SQL = (
    "(CASE WHEN kind='semantic' AND typeof(payload)='text' "
    "AND length(CAST(payload AS BLOB))<=1048576 THEN CASE "
    "WHEN NOT json_valid(payload) THEN 1 WHEN json_type(payload)!='object' THEN 1 "
    "WHEN json_type(payload,'$.inference_engine_version') IS NULL AND ("
    "(json_type(payload,'$.targets') IS NOT NULL AND (json_type(payload,'$.targets')!='array' "
    "OR json_array_length(payload,'$.targets')=0)) OR "
    "(json_type(payload,'$.generation') IS NOT NULL AND json_type(payload,'$.generation')!='text')) "
    "THEN 1 ELSE 0 END ELSE 0 END)")


def _held_cursor_for(job, lane):
    rank = None
    if lane == 'realtime':
        next_try, updated, created = job['next_try'], job['updated_at'], job['created_at']
        values = (next_try, updated, created)
        if any(value is not None and (type(value) not in (int, float)
                or abs(value) >= 2**63 or not math.isfinite(value)) for value in values):
            return None
        rank = updated if next_try is not None and updated is not None and next_try <= updated else created
    cursor = {'job_id': job['job_id'], 'rank': rank}
    return cursor if _held_cursor_valid(cursor, lane) else None


def _class_scope(archived: bool, excluded, *, lane=None, held_after=None) -> tuple[str, str]:
    """(patient-class + excluded-job SQL predicate, its json_each parameter)."""
    scope = (("" if archived else "NOT ") + _ARCHIVED_JOB
             + ' AND (' + current_meta_pred('fetch_jobs.payload')
             + ' OR (' + current_meta_pred('fetch_jobs.payload', target=LEGACY_VERSION)
             + " AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.eligible')=1 END)"
             + ' OR ' + _MALFORMED_JOB_SQL + ')'
             + " AND job_id NOT IN (SELECT value FROM json_each(?))")
    if _held_cursor_valid(held_after, lane):
        job_id = held_after['job_id']
        if lane == 'realtime':
            rank = held_after['rank']
            predicate = (f"({_REALTIME_KEY} IS NULL AND job_id<{job_id})" if rank is None else
                         f"({_REALTIME_KEY} IS NULL OR {_REALTIME_KEY}<{rank!r} OR "
                         f"({_REALTIME_KEY}={rank!r} AND job_id<{job_id}))")
        else:
            predicate = f'job_id>{job_id}'
        scope += f" AND (kind!='{JOB_KIND}' OR {predicate})"
    return scope, json.dumps(sorted(excluded))


def _adopt_pending_arrival(ledger, job):
    """Upgrade a selected legacy arrival from its actual unconsumed intent, without a corpus scan."""
    payload = runtime.parse_payload(job)
    if is_current(payload):
        return job
    if (read_version(payload) != LEGACY_VERSION or job['state'] != 'pending'
            or not payload.get('eligible') or payload.get('notification_free')):
        return None
    targets = payload.get('targets', [job['message_id']])
    if not isinstance(targets, list) or not targets or any(type(mid) is not int for mid in targets):
        return None
    version = ledger.db.execute('PRAGMA data_version').fetchone()[0]
    source = ledger._semantic_source_generation(job['project_id'], job['message_id'])
    origin = semantic_notice.origin_event_id(ledger.db, job['project_id'], targets)
    if ledger.db.in_transaction:
        raise ValueError('semantic_arrival_adoption_transaction_active')
    ledger.db.execute('BEGIN IMMEDIATE')
    with ledger.db:
        current = ledger.db.execute('SELECT * FROM fetch_jobs WHERE job_id=?', (job['job_id'],)).fetchone()
        import semantic_intake_hold
        try:
            semantic_intake_hold.validate_indexes(ledger.db)
            held = semantic_intake_hold.has_held_root(ledger.db, job['project_id'], job['message_id'])
        except sqlite3.Error:
            raise ValueError('semantic_intake_hold_unverified') from None
        if held:
            return None
        if (current is None or current['payload'] != job['payload'] or current['state'] != 'pending'
                or ledger.db.execute('PRAGMA data_version').fetchone()[0] != version):
            return None
        if origin is None:
            ledger.db.execute('UPDATE fetch_jobs SET next_try=? WHERE job_id=?',
                              (time.time() + 300, job['job_id']))
            return None
        ledger.semantic_seed_tx(job['project_id'], targets,
            {'source': 'unread', 'event_id': origin}, _source_generation=source)
    return ledger.db.execute('SELECT * FROM fetch_jobs WHERE job_id=?', (job['job_id'],)).fetchone()


def _due_lanes(ledger, kinds: tuple, max_jobs: int,
               archived: bool = False, *, excluded=(), turns=None, held_after=None) -> tuple[list, list, dict]:
    """Two-lane due selection with a persisted fairness share.

    Arrival-seeded jobs (payload.eligible=1 — a real notification event
    spawned them) keep priority, but the backfill cohort always gets a
    guaranteed share of this drain's window, so a continuous arrival
    stream can never starve runnable backlog within this patient class
    (GAP-6). At max_jobs < 4
    a single share slot would otherwise displace the only arrival slot:
    the persisted 'turn' marker alternates cohorts instead of
    permanently starving either.

    Returns (arrival_jobs, backfill_jobs, sched_mutation) — the mutation
    is written by the caller once the batch is consumed."""
    now = time.time()
    scope, excluded_json = _class_scope(archived, excluded, held_after=held_after)
    turn_key = "archived_turn" if archived else "turn"
    kind_ph = ",".join("?" * len(kinds))
    eligible = _ELIGIBLE_SQL
    arrivals_due = ledger.db.execute(f"""
      SELECT COUNT(*) FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind IN ({kind_ph})
        AND {eligible} AND {scope}
    """, (now, *kinds, excluded_json)).fetchone()[0]
    backlog_due = ledger.db.execute(f"""
      SELECT COUNT(*) FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind IN ({kind_ph})
        AND NOT ({eligible}) AND {scope}
    """, (now, *kinds, excluded_json)).fetchone()[0]
    share = 0
    turn_next = None
    if backlog_due:
        share = min(backlog_due, max(1, max_jobs // 4))
        if arrivals_due and share >= max_jobs:
            if max_jobs == 1:
                # one-slot drains alternate cohorts via the persisted
                # turn marker — neither starves permanently. The marker
                # records the cohort that won the last contested slot;
                # an absent marker keeps arrival first (its ordinary
                # priority), so backfill wins the SECOND contention.
                turn_next = ("arrival"
                             if (turns or _sched_state(ledger)).get(turn_key)
                             != "arrival" else "backfill")
                share = 1 if turn_next == "backfill" else 0
            else:
                share = max_jobs - 1
        # Unused arrival capacity can serve due backfill within the same
        # max_jobs/deadline/request budget. Preserve contested-slot turns.
        if arrivals_due < max_jobs - share:
            share = min(backlog_due, max_jobs - arrivals_due)
    order = "CASE WHEN kind=? THEN 0 ELSE 1 END, job_id"
    arrivals = ledger.db.execute(f"""
      SELECT * FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind IN ({kind_ph})
        AND {eligible} AND {scope}
      ORDER BY {order} LIMIT ?
    """, (now, *kinds, excluded_json, JOB_KIND, max_jobs - share)).fetchall() \
        if max_jobs - share > 0 else []
    backfill = ledger.db.execute(f"""
      SELECT * FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind IN ({kind_ph})
        AND NOT ({eligible}) AND {scope}
      ORDER BY {order} LIMIT ?
    """, (now, *kinds, excluded_json, JOB_KIND, share)).fetchall() if share else []
    mutation = {"turn": turn_next, "turn_key": turn_key}
    contested = arrivals or backfill
    if turn_next and contested:
        mutation["turn_job_id"] = contested[0]["job_id"]
    return list(arrivals), list(backfill), mutation


BACKLOG_COMMIT_RESERVE_S = 540.0


_LONG_START_S = 60.0


def _backlog_commit_reserve(scfg: dict) -> float:
    """The commit reserve the job budget affords while a long-output call
    still fits the work window: full from a ~1220 s budget, partial below
    (220 s at 900 s), never squeezing the long call out."""
    budget = float(scfg["job_budget_seconds"])
    return min(BACKLOG_COMMIT_RESERVE_S,
               max(0.0, budget - semantic_transport.LONG_MIN_CALL_S - _LONG_START_S))


_BACKLOG_TURN = {"kind": None}


_JOB_LOCK_BUCKETS = 1024


class _SchedulerUnavailable(Exception):
    pass


@contextmanager
def _job_lock(ledger, job_id):
    """Exclude duplicate inference for a job while ingestion keeps writing."""
    from writer_lock import acquire_run_lock
    db_path = ledger.db.execute("PRAGMA database_list").fetchone()[2]
    # Bucketed so the directory stays bounded (inodes are never unlinked,
    # see below). A bucket collision only makes the other job skip this
    # pass — the same path as a duplicate holder.
    path = os.path.join(os.path.dirname(db_path), "semantic_locks",
                        str(job_id % _JOB_LOCK_BUCKETS) + ".lock")
    fd = acquire_run_lock(path)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)


def _select_due(ledger, lane, kinds: tuple, qc_active: bool,
                max_jobs: int, long_defers: dict | None = None, *,
                excluded=(), turns=None, held_after=None, prioritize_patients=False) -> tuple:
    """Select active patients first, retaining each cohort's bounded fairness."""
    select_class = (partial(_select_due_class, prioritize_patients=True)
                    if prioritize_patients else _select_due_class)
    if lane == 'backlog' and not prioritize_patients:
        # Two resident lanes share this durable turn. Realtime arrivals keep
        # their existing active-first policy; an archive backlog cannot starve.
        try:
            with _sched_update(ledger) as state:
                archive_first = bool(state.get('reprocess_archive_turn'))
                state['reprocess_archive_turn'] = not archive_first
        except Exception:
            raise _SchedulerUnavailable from None
        def select(after):
            first = select_class(ledger, lane, kinds, qc_active, max_jobs,
                                      long_defers, archive_first,
                                      excluded=excluded, turns=turns, held_after=after)
            if first[0]:
                return first
            return select_class(ledger, lane, kinds, qc_active,
                                     max_jobs, long_defers, not archive_first,
                                     excluded=excluded, turns=turns, held_after=after)
    else:
        def select(after):
            active = select_class(
                ledger, lane, kinds, qc_active, max_jobs, long_defers, False,
                excluded=excluded, turns=turns, held_after=after)
            remaining = max_jobs - len(active[0])
            if remaining <= 0:
                return active
            archived = select_class(
                ledger, lane, kinds, qc_active, remaining, long_defers, True,
                excluded=excluded, turns=turns, held_after=after)
            due, qc_jobs, arrivals, backfill = [
                a + b for a, b in zip(active[:4], archived[:4])]
            return (due, qc_jobs, arrivals, backfill,
                    active[4] if active[4].get("turn") else archived[4])
    result = select(held_after)
    return select(None) if held_after and not result[0] else result


def _select_due_class(ledger, lane, kinds: tuple, qc_active: bool,
                      max_jobs: int, long_defers: dict | None,
                      archived: bool, *, excluded=(), turns=None, held_after=None,
                      prioritize_patients=False) -> tuple:
    """Due-job selection for one patient class in a drain pass.

    Returns (due, qc_jobs, arrivals, backfill, sched_mut): the ordered
    candidate list, its per-cohort split, and the fairness turn to persist.
    ``long_defers`` ({str(job_id): {"g", "n"}}) keeps jobs known to need
    the long-output budget out of the realtime lane.
    """
    scope, excluded_json = _class_scope(archived, excluded, lane=lane, held_after=held_after)
    if lane is None:
        qc_rows = []
        if qc_active and max_jobs > 1:
            qc_rows = ledger.db.execute(
                "SELECT * FROM fetch_jobs WHERE state='pending' "
                f"AND kind=? AND next_try<=? AND {scope} ORDER BY job_id LIMIT ?",
                (QC_JOB_KIND, time.time(), excluded_json, max_jobs)).fetchall()
        qc_share = min(len(qc_rows), max(1, max_jobs // 4), max_jobs - 1)
        qc_jobs = list(qc_rows[:qc_share])
        arrivals, backfill, sched_mut = _due_lanes(
            ledger, (JOB_KIND,) if qc_jobs else kinds,
            max_jobs - len(qc_jobs), archived, excluded=excluded, turns=turns, held_after=held_after)
        # unused lane capacity flows back to QC — same rule the backfill
        # cohort uses against an idle arrival lane
        spare = max_jobs - len(qc_jobs) - len(arrivals) - len(backfill)
        if spare > 0:
            qc_jobs += list(qc_rows[qc_share:qc_share + spare])
    else:
        # explicit lane: the fairness/QC selection is never computed
        # (it would be discarded), saving its queries per tick
        eligible = _ELIGIBLE_SQL
        # Fresh arrivals use realtime. Background workers may also finish
        # older arrival-seeded jobs; a per-job flock excludes double work.
        if lane == "realtime":
            # Newest SEED first (M6): a seed/re-seed/revival writes
            # next_try=updated_at, while every defer/retry pushes
            # next_try past updated_at — so a job that already took a
            # free defer ranks by its creation time instead of jumping
            # back to the head on each defer. Jobs known to need the
            # long-output budget never fit this lane and are left to
            # the backlog workers.
            # (same input generation only: a re-seed gets a new try)
            rows = ledger.db.execute(
                f"SELECT * FROM fetch_jobs WHERE state='pending' AND next_try<=? "
                f"AND kind=? AND {eligible} AND {scope} "
                "AND NOT EXISTS (SELECT 1 FROM json_each(?) s"
                " WHERE s.key=CAST(fetch_jobs.job_id AS TEXT)"
                " AND json_extract(s.value,'$.g')=CASE WHEN json_valid("
                "fetch_jobs.payload) THEN json_extract(fetch_jobs.payload,"
                "'$.generation') END) "
                f"ORDER BY {_REALTIME_ORDER} LIMIT ?",
                (time.time(), JOB_KIND, excluded_json, json.dumps(long_defers or {}),
                 max_jobs + 8 + len(long_defers or {}))).fetchall()
            # a payload from before the generation field: its generation
            # is the payload digest, which SQL cannot compute
            rows = [r for r in rows
                    if (long_defers or {}).get(str(r["job_id"]), {}).get("g")
                    != runtime.payload_generation(runtime.parse_payload(r))
                    ][:max_jobs + 8]
        else:
            # Re-extraction re-pends QC rows under their old (low) job_id;
            # one job_id order let them win every pass and no semantic job
            # ran for hours (2026-10-10). Each kind keeps oldest-first, and
            # the kind this worker served last goes second.
            # QC re-seeding refreshes next_try: its new generation must
            # queue behind QC that was already waiting, despite an old id.
            priority = patient_priority.activity_order_sql('fetch_jobs.project_id') + ', ' if prioritize_patients else ''
            rows = [row for kind in sorted(
                        kinds, key=lambda k: k == _BACKLOG_TURN["kind"])
                    for row in ledger.db.execute(
                        "SELECT * FROM fetch_jobs WHERE state='pending' "
                        f"AND next_try<=? AND kind=? AND {scope} ORDER BY "
                        + priority + f"{'next_try, job_id' if kind == QC_JOB_KIND else 'job_id'} LIMIT ?",
                        (time.time(), kind, excluded_json, max_jobs + 8)).fetchall()]
        # Include extra candidates so another worker's current job cannot
        # hide the next runnable item. Work remains bounded by max_jobs.
        qc_jobs = [j for j in rows if j['kind'] == QC_JOB_KIND]
        arrivals = [j for j in rows if j['kind'] == JOB_KIND
                    and runtime.parse_payload(j).get('eligible')]
        backfill = [j for j in rows if j['kind'] == JOB_KIND
                    and not runtime.parse_payload(j).get('eligible')]
        sched_mut = {"turn": None}
    due = (list(rows) if lane is not None else
           qc_jobs + list(arrivals) + list(backfill))
    return due, qc_jobs, arrivals, backfill, sched_mut


class _CloudSelectionIncomplete(Exception):
    """A cloud selection could not finish safely within the current pass."""


def _cloud_selection_identity(db, project_id, message_id):
    """Bind the receipt, source occurrences and attachments to one snapshot."""
    import cloud_policy
    anchor = db.execute('SELECT COALESCE(parent_id,message_id) FROM messages '
                        'WHERE project_id=? AND message_id=? '
                        'UNION ALL SELECT NULL UNION ALL SELECT NULL', (project_id, message_id))
    try:
        root = anchor.fetchone()[0]
        if root is None:
            return None
        scope = cloud_policy.history_thread_snapshot(db, project_id, message_id)
        bundle = semantic_store.thread_bundle(db, project_id, root, [message_id])
        if scope is None or bundle is None:
            return None
        return mcs_util.payload_hash({'scope': scope, 'bundle': bundle['source_fingerprint']})
    finally:
        anchor.close()


def _cloud_selection_memory_guard(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        global _cloud_selection_progress
        try:
            return fn(*args, **kwargs)
        except MemoryError as error:
            _cloud_selection_progress = None
            raise _CloudSelectionIncomplete('memory') from error
    return guarded


@_cloud_selection_memory_guard
def _select_cloud_due(ledger, lane, kinds, qc_active, max_jobs, long_defers=None, *,
                      excluded=(), turns=None, held_after=None, deadline=None, isolated=False):
    """Choose active patients and their newest activity before archived cloud work."""
    import cloud_inference
    import cloud_policy
    global _cloud_selection_progress
    if deadline is not None and time.monotonic() >= deadline:
        raise _CloudSelectionIncomplete('deadline')
    if isolated:
        # The outer connection may have a caller-owned cancellation handler.
        # Only the separate, read-only connection owns our deadline handler.
        from pathlib import Path
        from types import SimpleNamespace
        if ledger.db.in_transaction:
            raise _CloudSelectionIncomplete('active_transaction')
        try:
            path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
            if not path:
                raise _CloudSelectionIncomplete('reader_unavailable')
            before = os.stat(path)
            reader = sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro',
                                     uri=True, timeout=0)
            try:
                after = os.stat(path)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise _CloudSelectionIncomplete('reader_changed')
                reader.row_factory = sqlite3.Row
                if deadline is not None:
                    reader.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                return _select_cloud_due(SimpleNamespace(db=reader), lane, kinds, qc_active,
                    max_jobs, long_defers, excluded=excluded, turns=turns, held_after=held_after,
                    deadline=deadline)
            finally:
                reader.close()
        except sqlite3.OperationalError as error:
            if deadline is not None and time.monotonic() >= deadline:
                raise _CloudSelectionIncomplete('deadline') from error
            raise _CloudSelectionIncomplete('reader_unavailable') from error
        except OSError as error:
            raise _CloudSelectionIncomplete('reader_unavailable') from error
    cfg = load_config()
    settings = cloud_inference.resolve_cloud_settings(cfg)
    if (lane != "backlog" or settings["mode"] != "on" or "semantic_backlog" not in settings["routes"]
            or not cloud_policy.selected_key()):
        _cloud_selection_progress = None
        return [], [], [], [], {"turn": None}
    semantic_settings = runtime_config(cfg)[0]
    semantic_scope = semantic_settings["project_ids"]
    # Caller-owned transactions may include uncommitted rows. Never retain a
    # cursor derived from them beyond this invocation.
    durable = not ledger.db.in_transaction
    identity = None
    if durable:
        path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
        if path:
            try:
                stat = os.stat(path)
                identity = (os.path.realpath(path), stat.st_dev, stat.st_ino)
            except OSError:
                raise _CloudSelectionIncomplete('reader_unavailable')
    scope_row = ledger.db.execute(
        "SELECT artifact_id,content,meta FROM artifacts WHERE kind='cloud_history_scope' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    scope_identity = mcs_util.payload_hash(tuple(scope_row)) if scope_row else None
    binding = (identity, scope_identity, lane, tuple(kinds),
               mcs_util.payload_hash({'cloud': settings, 'semantic': semantic_settings}))
    if not _cloud_selection_lock.acquire(blocking=False):
        raise _CloudSelectionIncomplete('selection_busy')
    try:
        return _cloud_selection_page(ledger.db, kinds, max_jobs, settings, semantic_scope,
                                     excluded, deadline, binding if identity else None)
    finally:
        _cloud_selection_lock.release()


_cloud_selection_lock = threading.Lock()


_cloud_selection_progress = None


_CLOUD_SELECTION_PAGE = 128


def _cloud_selection_page(db, kinds, max_jobs, settings, semantic_scope, excluded, deadline, binding):
    import cloud_policy
    global _cloud_selection_progress

    def check_deadline():
        if deadline is not None and time.monotonic() >= deadline:
            raise _CloudSelectionIncomplete('deadline')

    check_deadline()
    progress = _cloud_selection_progress if binding is not None else None
    if progress is None or progress['binding'] != binding:
        upper = db.execute('SELECT COALESCE(MAX(job_id), 0) FROM fetch_jobs').fetchone()[0]
        progress = {'binding': binding, 'upper': upper, 'cursor': 0,
                    'rows': [], 'pending': [], 'collecting': True,
                    'yield_early': False}
        if binding is not None:
            _cloud_selection_progress = progress
    # PK range scan: the correlated MAX uses idx_messages_project_time.
    # A page is adopted only after its statement has fully completed.
    marks = ','.join('?' for _ in kinds)
    while progress['collecting']:
        check_deadline()
        rows = db.execute(
            'SELECT j.job_id,j.kind,j.project_id,COALESCE(p.is_archived,0), '
            + patient_priority.latest_activity_sql('j.project_id') + ' '
            'FROM fetch_jobs j NOT INDEXED LEFT JOIN patients p ON p.project_id=j.project_id '
            'WHERE j.job_id>? AND j.job_id<=? '
            'ORDER BY j.job_id LIMIT ?',
            (progress['cursor'], progress['upper'], _CLOUD_SELECTION_PAGE)).fetchall()
        if rows:
            for job_id, kind, project_id, archived, latest in rows:
                if kind in kinds:
                    # SQLite orders NULL timestamps last for DESC and NULL
                    # project IDs first for ASC.
                    heapq.heappush(progress['rows'], (
                        *patient_priority.priority_key(project_id, latest, archived=bool(archived)), job_id))
            progress['cursor'] = rows[-1][0]
        if len(rows) < _CLOUD_SELECTION_PAGE:
            progress['collecting'] = False
    check_deadline()
    selected, snapshots = [], {}
    pending = progress['pending']
    pending_pos = 0
    try:
        with cloud_policy.history_selection_check(db) as receipt_check:
            while pending_pos < len(pending) or progress['rows']:
                check_deadline()
                from_pending = pending_pos < len(pending)
                rank = pending[pending_pos] if from_pending else progress['rows'][0]
                job_id = rank[-1]
                if job_id in excluded:
                    if from_pending:
                        pending.pop(pending_pos)
                    else:
                        heapq.heappop(progress['rows'])
                    continue
                row = db.execute(
                    'SELECT j.* FROM fetch_jobs j WHERE j.job_id=? '
                    f"AND j.kind IN ({marks}) AND j.state='pending' AND j.next_try<=? "
                    'AND json_valid(j.payload) AND ' + current_meta_pred('j.payload') + ' '
                    'AND (? IS NULL OR j.project_id IN (SELECT value FROM json_each(?))) '
                    'AND ' + cloud_policy.history_scope_target_sql('j.message_id') + ' '
                    'AND j.project_id NOT IN (SELECT value FROM json_each(?)) '
                    'AND (? IS NULL OR j.project_id IN (SELECT value FROM json_each(?)))',
                    (job_id, *kinds, time.time(),
                     json.dumps(settings['project_ids']) if settings['project_ids'] is not None else None,
                     json.dumps(settings['project_ids']), json.dumps(settings['exclude_project_ids']),
                     json.dumps(semantic_scope) if semantic_scope is not None else None,
                     json.dumps(semantic_scope))).fetchone()
                eligible = False
                if (row is not None and cloud_policy.history_target_ready(db, row['project_id'], row['message_id'])
                        and receipt_check(row['project_id'], row['message_id'])):
                    snapshot = _cloud_selection_identity(db, row['project_id'], row['message_id'])
                    check_deadline()
                    if snapshot is not None:
                        eligible = True
                        snapshots[row['job_id']] = snapshot
                        selected.append(row)
                if from_pending:
                    if eligible:
                        pending_pos += 1
                    else:
                        pending.pop(pending_pos)
                else:
                    heapq.heappop(progress['rows'])
                    if eligible:
                        pending.append(rank)
                        pending_pos += 1
                if progress['yield_early'] and len(selected) >= max(1, max_jobs):
                    break
                if len(selected) >= min(512, max_jobs + max(8, settings['max_concurrency'] * 2)):
                    break
            check_deadline()
    except BaseException:
        # Retain only IDs/ranks, and do not rescan an already rejected suffix.
        # On the next tick revalidate accepted IDs and return promptly once
        # a useful batch is ready, even if the lookahead could not finish.
        if pending:
            progress['yield_early'] = True
        raise
    progress['yield_early'] = False
    if not progress['rows'] and not pending and binding is not None:
        _cloud_selection_progress = None
    semantic = [r for r in selected if r['kind'] == JOB_KIND]
    arrivals = [r for r in semantic if runtime.parse_payload(r).get('eligible')]
    backfill = [r for r in semantic if not runtime.parse_payload(r).get('eligible')]
    return (selected, [r for r in selected if r['kind'] == QC_JOB_KIND], arrivals, backfill,
            {'turn': None, 'cloud_snapshots': snapshots})


def _refill_due(ledger, lane, kinds, qc_active, max_jobs, long_defers,
                first_batch, selected, out, deadline, consumed_turns, held_after=None,
                selector=None):
    """Refill skipped slots without revisiting jobs or scanning an unbounded queue."""
    inspected = set()
    batch = first_batch
    scan_limit = max_jobs * (64 if selector is not None else 16)
    select = selector or _select_due
    while len(inspected) < scan_limit and sum(selected.values()) < max_jobs:
        before = len(inspected)
        due, qc_jobs, arrivals, backfill, mutation = batch
        if not due:
            return
        cohort_of = {row["job_id"]: name for name, rows in (
            ("qc", qc_jobs), ("arrival", arrivals), ("backfill", backfill))
            for row in rows}
        for job in due:
            if (len(inspected) >= scan_limit or sum(selected.values()) >= max_jobs
                    or time.monotonic() >= deadline):
                return
            job_id = job["job_id"]
            if job_id in inspected:
                continue
            inspected.add(job_id)
            cohort_name = cohort_of[job_id]
            out["lanes"][cohort_name] += 1
            yield job, cohort_name, mutation
        if len(inspected) == before or time.monotonic() >= deadline:
            return
        remaining = max_jobs - sum(selected.values())
        if remaining <= 0 or len(inspected) >= scan_limit:
            return
        turns = {**_sched_state(ledger), **consumed_turns}
        try:
            batch = select(ledger, lane, kinds, qc_active, remaining,
                                long_defers, excluded=inspected, turns=turns, held_after=held_after)
        except _CloudSelectionIncomplete as error:
            out['cloud_selection_incomplete'] = True
            out['cloud_selection_reason'] = str(error)
            return
        except _SchedulerUnavailable:
            out['long_defer_reservation_failed'] = True
            return
