"""Select pending extraction work and collect stale artifacts within scan budgets."""
import contextlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
import inference_version as inference
import cloud_policy
import cloud_inference
import inference_dependency_loss
import mcs_util
import patient_priority
from mcs_queries import (ELIGIBLE_MESSAGE, current_extract_pred,
                         current_v4_id, json_or_null)
from mcs_util import HOME, load_config, loads_dict, strict_json

from extract_llm_progress import (
    cloud_completion,
    progress_for_message,
)
from extract_llm_qc import (
    _ERROR_ROWS_SQL,
    _hold_sql,
    _human_flagged_sql,
    _qc_flagged_sql,
    _retry_budget_sql,
    _thin_pending_sql,
    pending_pred,
)
from extract_llm_source_contract import (
    EXTRACT_VERSION,
    KIND,
    _BATCH_PER_ITEM_S,
    _CLOUD_PRIORITY_PAGE,
    _INFERENCE_VERSION,
    _MIN_CALL_S,
    _STALE_GC_BUDGET_S,
    _STALE_GC_CLAIM_PAGE,
    _STALE_GC_CURSOR_RESERVE_S,
    _STALE_GC_KIND,
    _STALE_GC_PAGE,
    _STALE_GC_S,
    _writer_current,
)

def _next_retry(ledger, *, after: float | None = None) -> float | None:
    """Earliest per-message retry among retriable error artifacts, or None when
    nothing is retriable (all remaining are permanent failures). With
    `after`, only next_try later than it count: a past-due row that stays
    unselected (other shard, gate) must not hide a later due time."""
    safe_meta = json_or_null("a.meta")
    timestamp = f"json_extract({safe_meta},'$.next_try')"
    timestamp_type = f"json_type({safe_meta},'$.next_try')"
    finite = f"{timestamp} BETWEEN -{sys.float_info.max!r} AND {sys.float_info.max!r}"
    sql = (f"SELECT MIN(retry_at) FROM (SELECT MAX({timestamp}) AS retry_at "
           "FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
           "WHERE a.kind=? AND " + inference.current_meta_pred("a.meta", target=_INFERENCE_VERSION) + " AND json_valid(a.meta) "
           f"AND json_extract({safe_meta},'$.error')=1 "
           f"AND COALESCE(json_extract({safe_meta},'$.invalidated'),0)=0 "
           f"AND COALESCE(json_extract({safe_meta},'$.extract_version'),0)={EXTRACT_VERSION} "
           "AND (a.project_id IS NULL OR a.project_id=m.project_id) "
           f"AND m.content_hash=json_extract({safe_meta},'$.hash') AND m.body_state IS NOT 'deleted' "
           f"AND (json_type({safe_meta},'$.attempts') IS NULL OR "
           f"(json_type({safe_meta},'$.attempts')='integer' AND json_extract({safe_meta},'$.attempts')>=0)) "
           "GROUP BY m.message_id "
           f"HAVING MAX(COALESCE(json_extract({safe_meta},'$.attempts'),0))<5 "
           f"AND MAX(CASE WHEN {timestamp_type} IS NULL THEN 0 "
           f"WHEN {timestamp_type} IN ('integer','real') AND {finite} THEN 0 ELSE 1 END)=0) "
           "WHERE retry_at IS NOT NULL")
    params: list = [KIND]
    if after is not None:
        sql += " AND retry_at > ?"
        params.append(after)
    row = ledger.db.execute(sql, params).fetchone()
    return row[0] if row and row[0] is not None else None


_IDLE_FLOOR_S = 5.0


def _next_due_in(ledger, now: float, *, semantic: bool, context: bool) -> float | None:
    """Seconds until the earliest FUTURE retry/due time this resident can
    serve, or None. Past-due rows are excluded on purpose: a row that was
    due and still not selected is gated (shard, admission, poison, lock)
    and polling faster cannot serve it."""
    try:
        due = [t for t in (_next_retry(ledger, after=now),) if t is not None]
        if semantic:
            import semantic_policy
            row = ledger.db.execute(
                "SELECT MIN(next_try) FROM fetch_jobs WHERE state='pending' "
                "AND next_try > ? AND kind IN (?,?)",
                (now, semantic_policy.JOB_KIND, semantic_policy.QC_JOB_KIND)).fetchone()
            if row and row[0] is not None:
                due.append(row[0])
        if context:
            import patient_context_refresh
            safe = json_or_null("content")
            t = f"json_extract({safe},'$.next_try')"
            row = ledger.db.execute(
                f"SELECT MIN({t}) FROM artifacts WHERE kind IN (?,?) "
                f"AND COALESCE(json_extract({safe},'$.attempts'),0) < 5 "
                f"AND json_type({safe},'$.next_try') IN ('integer','real') AND {t} > ?",
                (patient_context_refresh.PROGRESS, patient_context_refresh.SCAN, now)).fetchone()
            if row and row[0] is not None:
                due.append(row[0])
    except Exception:
        return None   # a locked/odd DB must not kill the resident; fall back to the fixed poll
    return min(due) - now if due else None


def patient_extraction_complete(ledger, project_id) -> bool:
    """True when no stored post of the patient still needs the current
    LLM extraction (EXTRACT_VERSION) — the same pending predicate the
    drainers select by, so patient context never reads a half-done
    patient (2026-10-18 owner rule). Posts past the retry ceiling can
    never be selected, so they do not hold the patient back."""
    return ledger.db.execute(
        f"SELECT 1 FROM messages m WHERE m.project_id=? AND {pending_pred(ledger.db)}"
        f" AND ({_retry_budget_sql()}) LIMIT 1",
        (project_id,)).fetchone() is None


_stale_gc_at = 0.0


def _batch_need_s(n: int) -> float:
    """Estimated wall time a batch call needs to actually finish —
    a floor proportional to items because decode dominates and scales
    with output size, not with the shared prompt."""
    return 60.0 + _BATCH_PER_ITEM_S * n


_LEFT_ESTIMATE: dict = {}


_LEFT_ESTIMATE_S = 600.0


def _left_estimate_key(ledger) -> str:
    try:
        return ledger.db.execute("PRAGMA database_list").fetchone()[2] or ""
    except Exception:
        return ""


_FRESH_SQL = ("NOT EXISTS (SELECT 1 FROM artifacts x WHERE x.kind='" + KIND
              + "' AND x.message_id=m.message_id AND (x.project_id IS NULL OR x.project_id=m.project_id))")


def _rehold_sql() -> str:
    """An automatic hold of this body at this generation that a code update
    released (pending_pred keeps only current-contract holds out). These
    re-runs mostly end held again, so they wait behind work that can land;
    human/QC re-extracts and invalidated holds are never pushed back."""
    return f"""(EXISTS ({_hold_sql(released=True)})
        AND ({_qc_flagged_sql()}) IS NULL AND ({_human_flagged_sql()}) IS NULL)"""


def upgrade_status(db, *, model: str | None) -> dict:
    """Count every full-source upgrade outcome once, without exposing source or identifiers."""
    out = dict.fromkeys(("done_clean", "held_prior", "needs_review", "failed", "pending",
                         "projection_shadowed"), 0)
    rows = db.execute(f"""SELECT m.*, ({current_v4_id()}) AS projection,
        (SELECT a.meta FROM artifacts a WHERE a.kind='{KIND}' AND a.message_id=m.message_id
            {current_extract_pred()} ORDER BY a.artifact_id DESC LIMIT 1) AS latest_meta,
        ({pending_pred(db)}) AS pending, ({_retry_budget_sql()}) AS retryable,
        EXISTS ({_hold_sql()}) AS held,
        EXISTS (SELECT 1 {_ERROR_ROWS_SQL}
            AND COALESCE(json_extract({json_or_null('e.meta')},'$.attempts'),0)>=5) AS failed
        FROM messages m WHERE {ELIGIBLE_MESSAGE}""", (KIND, EXTRACT_VERSION))
    for row in rows:
        integrity = (loads_dict(row["latest_meta"]) or {}).get("integrity")
        validation = integrity.get("validation") if isinstance(integrity, dict) else None
        dropped = isinstance(validation, dict) and any(
            type(validation.get(key)) is int and validation[key] > 0
            for key in ("items_dropped", "evidence_dropped"))
        state = ("projection_shadowed" if row["projection"] is not None else
                 "failed" if row["failed"] else "held_prior" if row["held"] else
                 "pending" if row["pending"] and row["retryable"] else
                 "done_clean" if not dropped and progress_for_message(db, row, model=model)["state"] == "complete" else
                 "needs_review")
        out[state] += 1
    return {"target_version": EXTRACT_VERSION, **out, "total": sum(out.values())}


def _selection_sql(db, order: str, adm_sql: str, fresh: bool | None = None,
                   archived: bool | None = None, rehold: bool | None = None,
                   prioritize_patients: bool = False, bounded: bool = True,
                   cloud_followup: bool = False) -> str:
    """run_pending's due-row selection.

    Retry state is read through correlated subqueries instead of a
    LEFT JOIN + GROUP BY over every eligible message. Without patient
    priority, rows can stream in posted_at order and a SQL LIMIT can stop
    the indexed walk. Cloud selection filters receipts before its Python
    limit; patient priority adds a sort. The cheap shard/claim tests precede
    the per-message JSON work. The row set equals the former grouped
    query (attempts: MAX(COALESCE(attempts,0)) or 0 without error rows;
    due: COALESCE(MAX(next_try),0) <= now); message_id breaks posted_at
    ties, which were unordered before (M5). ``fresh`` True/False keeps
    only never-extracted / previously-extracted messages (M12).
    ``archived`` partitions patients without adding a sort before LIMIT;
    ``rehold`` likewise splits off update-released automatic holds."""
    fresh_sql = ("" if fresh is None else
                 f" AND {_FRESH_SQL}" if fresh else f" AND NOT {_FRESH_SQL}")
    rehold_sql = ("" if rehold is None else
                  f" AND {_rehold_sql()}" if rehold else f" AND NOT {_rehold_sql()}")
    archived_sql = ""
    if archived is not None:
        archived_sql = (" AND " + ("" if archived else "NOT ")
                        + "EXISTS (SELECT 1 FROM patients p WHERE p.project_id=m.project_id"
                          " AND COALESCE(p.is_archived,0)!=0)")
    priority = patient_priority.activity_order_sql('m.project_id') + ', ' if prioritize_patients else ''
    pending = pending_pred(db)
    if cloud_followup:
        # A sealed (v4-adopted) source still needs its required cloud pass:
        # the sealed-source fence lives on the write side, not on selection.
        # A canonical raw source (v4 row, no extract artifact) is the same
        # case — it matches through the sealed arm instead of local pending.
        pending = (f"({pending}) OR ({ELIGIBLE_MESSAGE} AND ({current_v4_id()} IS NOT NULL OR EXISTS ("
                   f"SELECT 1 FROM artifacts a WHERE a.kind='{KIND}' AND a.message_id=m.message_id "
                   + current_extract_pred() + f" AND json_extract({json_or_null('a.meta')},'$.extract_version')={EXTRACT_VERSION})))")
        pending = f"({pending}) AND NOT EXISTS ({_hold_sql()})"
    return f"""
      SELECT m.message_id, m.project_id, {inference_dependency_loss.SOURCE_BINDING_COLUMNS},
             ({_qc_flagged_sql()}) AS qc_src,
             ({_thin_pending_sql(db)}) AS thin_src,
             ({_human_flagged_sql()}) AS human_src,
             COALESCE((SELECT MAX(COALESCE(json_extract(e.meta,'$.attempts'),0))
                       {_ERROR_ROWS_SQL}), 0) AS attempts
      FROM messages m
      WHERE {ELIGIBLE_MESSAGE}{adm_sql}{archived_sql}{fresh_sql}{rehold_sql}
        AND (? IS NULL OR m.message_id % ? = ?)
        AND NOT EXISTS (SELECT 1 FROM fetch_jobs claim
                        WHERE claim.kind='extract_claim'
                          AND claim.project_id=m.project_id
                          AND claim.message_id=m.message_id
                          AND claim.next_try > ?)
        AND ({_retry_budget_sql()})
        AND ({pending})
        AND COALESCE((SELECT MAX(json_extract(e.meta,'$.next_try'))
                      {_ERROR_ROWS_SQL}), 0) <= ?
      ORDER BY {priority}m.posted_at_ts {order}, m.message_id {order}
      {'LIMIT ?' if bounded else ''}
    """


def _validate_run_args(limit, budget_s) -> None:
    """Reject a non-positive limit or a non-finite budget."""
    if type(limit) is not int or limit < 1:
        raise ValueError("extract_limit_invalid")
    try:
        valid_budget = type(budget_s) in (int, float) and math.isfinite(budget_s)
    except OverflowError:
        valid_budget = False
    if not valid_budget:
        raise ValueError("extract_budget_invalid")


class _StaleGCDeadline(Exception):
    pass


def _gc_stale_artifacts(ledger, lock, per_write_lock: bool, *, deadline=None) -> None:
    """Reconcile a bounded stale-artifact page, resuming from durable progress."""
    global _stale_gc_at
    if ledger.db.in_transaction or time.time() - _stale_gc_at < _STALE_GC_S:
        return
    started = time.monotonic()
    local_deadline = started + _STALE_GC_BUDGET_S
    deadline = min(deadline, local_deadline) if deadline is not None else local_deadline
    if started >= deadline:
        return
    work_deadline = deadline - min(_STALE_GC_CURSOR_RESERVE_S, (deadline - started) / 4)
    completed = False
    try:
        with lock(per_write_lock, deadline=deadline) as held:
            if not held or not _writer_current() or time.monotonic() >= deadline:
                return
            with ledger.db:
                ledger.db.execute('BEGIN IMMEDIATE')
                if not _writer_current() or time.monotonic() >= deadline:
                    raise _StaleGCDeadline()
                state_row = ledger.db.execute(
                    "SELECT CASE WHEN typeof(payload)='text' AND length(payload)<=1024 "
                    "AND instr(payload,char(0))=0 THEN payload END FROM fetch_jobs "
                    "WHERE kind=? AND project_id=0 AND message_id=0", (_STALE_GC_KIND,)).fetchone()
                old_payload = state_row[0] if state_row is not None else None
                if state_row is None:
                    after, highwater = 0, 0
                else:
                    try:
                        state = strict_json(old_payload)
                    except (TypeError, ValueError, RecursionError) as exc:
                        raise ValueError('extract_gc_cursor_invalid') from exc
                    if (not isinstance(state, dict) or set(state) != {'version', 'cursor', 'highwater'}
                            or type(state['version']) is not int or state['version'] != 1
                            or type(state['cursor']) is not int or type(state['highwater']) is not int
                            or not 0 <= state['cursor'] <= state['highwater'] < 2**63):
                        raise ValueError('extract_gc_cursor_invalid')
                    after, highwater = state['cursor'], state['highwater']
                if highwater == 0:
                    highwater = ledger.db.execute("SELECT COALESCE(MAX(artifact_id),0) FROM artifacts WHERE kind=?",
                                                  (KIND,)).fetchone()[0]
                import inference_dependency_loss as dependency_loss
                rows = ledger.db.execute(
                    "SELECT a.artifact_id,a.project_id,a.message_id,a.content,a.model,a.meta,a.created_at,"
                    "m.project_id AS source_project_id,m.content_hash AS source_hash FROM artifacts a "
                    "JOIN messages m ON m.message_id=a.message_id WHERE a.kind=? "
                    "AND a.artifact_id>? AND a.artifact_id<=? "
                    "AND (a.project_id IS NULL OR a.project_id=m.project_id) "
                    "AND typeof(a.content)='text' AND length(a.content)<=262144 "
                    "AND (a.model IS NULL OR (typeof(a.model)='text' AND length(a.model)<=32768)) "
                    "AND typeof(a.created_at) IN ('integer','real','null') "
                    "AND (m.content_hash IS NULL OR (typeof(m.content_hash)='text' AND length(m.content_hash)<=256)) "
                    "AND CASE WHEN typeof(a.meta)='text' AND length(a.meta)<=32768 THEN ("
                    + inference.current_meta_pred('a.meta', target=_INFERENCE_VERSION)
                    + " AND CASE WHEN json_valid(a.meta) THEN json_extract(a.meta,'$.hash') IS NOT m.content_hash END) "
                    "ELSE 0 END ORDER BY a.artifact_id LIMIT ?", (KIND, after, highwater, _STALE_GC_PAGE)).fetchall()
                exhausted = True
                for row in rows:
                    after = row['artifact_id']
                    if time.monotonic() >= work_deadline:
                        # No mutation for this source: defer it for the next
                        # fixed pass, while committing the completed prefix.
                        exhausted = False
                        break
                    args = (row['artifact_id'], KIND, row['project_id'], row['message_id'], row['content'],
                            row['model'], row['meta'], row['created_at'], row['source_project_id'], row['source_hash'])
                    witness = ("artifact_id=? AND kind IS ? AND project_id IS ? AND message_id IS ? "
                               "AND content IS ? AND model IS ? AND meta IS ? AND created_at IS ? "
                               "AND EXISTS (SELECT 1 FROM messages m WHERE m.message_id=artifacts.message_id "
                               "AND m.project_id IS ? AND m.content_hash IS ? "
                               "AND (artifacts.project_id IS NULL OR artifacts.project_id=m.project_id))")
                    if ledger.db.execute('SELECT 1 FROM artifacts WHERE ' + witness, args).fetchone() is None:
                        continue
                    ledger.db.execute('SAVEPOINT stale_gc_candidate')
                    pid = row['source_project_id']
                    binding = dependency_loss.source_binding(ledger.db, pid, row['message_id'])
                    footprint = dependency_loss.capture_tx(ledger.db, pid, row['message_id'],
                                                          artifact_ids=[row['artifact_id']])
                    added = dependency_loss.record_tx(ledger.db, footprint)
                    deleted = ledger.db.execute('DELETE FROM artifacts WHERE ' + witness, args).rowcount
                    if (deleted != 1 or ledger.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?',
                                                          (row['artifact_id'],)).fetchone() is not None
                            or dependency_loss.source_binding(ledger.db, pid, row['message_id']) != binding):
                        raise RuntimeError('extract_stale_gc_unverified')
                    if added:
                        ledger.bump_inference_analysis_epoch_tx(pid)
                        if dependency_loss.source_binding(ledger.db, pid, row['message_id']) != binding:
                            raise RuntimeError('extract_stale_gc_unverified')
                    if time.monotonic() >= work_deadline:
                        # A slow candidate must not block later ids. Restore its
                        # own changes; the next full pass will revisit it.
                        ledger.db.execute('ROLLBACK TO stale_gc_candidate')
                        ledger.db.execute('RELEASE stale_gc_candidate')
                        exhausted = False
                        break
                    ledger.db.execute('RELEASE stale_gc_candidate')
                completed = exhausted and (len(rows) < _STALE_GC_PAGE or after == highwater)
                next_payload = json.dumps({'version': 1, 'cursor': 0 if completed else after,
                                           'highwater': 0 if completed else highwater}, separators=(',', ':'))
                if time.monotonic() >= deadline or not _writer_current():
                    raise _StaleGCDeadline()
                if state_row is None:
                    cur = ledger.db.execute(
                        "INSERT OR IGNORE INTO fetch_jobs(kind,project_id,message_id,payload,state,next_try,created_at,updated_at) "
                        "VALUES(?,0,0,?,'done',0,?,?)", (_STALE_GC_KIND, next_payload, time.time(), time.time()))
                else:
                    cur = ledger.db.execute('UPDATE fetch_jobs SET payload=?,updated_at=? '
                        'WHERE kind=? AND project_id=0 AND message_id=0 AND payload=?',
                        (next_payload, time.time(), _STALE_GC_KIND, old_payload))
                if cur.rowcount != 1 or ledger.db.execute(
                        'SELECT 1 FROM fetch_jobs WHERE kind=? AND project_id=0 AND message_id=0 AND payload=?',
                        (_STALE_GC_KIND, next_payload)).fetchone() is None:
                    raise RuntimeError('extract_gc_cursor_unverified')
                if completed:
                    ledger.db.execute("DELETE FROM fetch_jobs WHERE job_id IN (SELECT job_id FROM fetch_jobs "
                                      "WHERE kind='extract_claim' AND next_try < ? LIMIT ?)",
                                      (time.time() - 86400, _STALE_GC_CLAIM_PAGE))
                if time.monotonic() >= deadline or not _writer_current():
                    raise _StaleGCDeadline()
    except _StaleGCDeadline:
        return  # the context manager rolls back the whole page and cursor
    if completed:
        _stale_gc_at = time.time()


_FOCUS: dict = {}


_FOCUS_PATIENTS = 3


def _focus_patients(ledger) -> list:
    """Active patients with the fewest selectable pending posts (cached).
    Rows waiting out a retry backoff are not counted, so a patient whose
    rest is not due yet does not hold a focus place."""
    key = _left_estimate_key(ledger)
    hit = _FOCUS.get(key)
    if hit is None or time.monotonic() - hit["at"] >= _LEFT_ESTIMATE_S:
        pids = [r[0] for r in ledger.db.execute(
            "SELECT m.project_id FROM messages m WHERE m.project_id IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM patients p WHERE p.project_id=m.project_id"
            " AND COALESCE(p.is_archived,0)!=0)"
            f" AND {pending_pred(ledger.db)} AND ({_retry_budget_sql()})"
            f" AND COALESCE((SELECT MAX(json_extract(e.meta,'$.next_try')) {_ERROR_ROWS_SQL}), 0) <= ?"
            # focus selects re-extraction only, so a patient whose rest is all
            # new arrivals would hold a place it cannot use
            f" GROUP BY m.project_id HAVING SUM(CASE WHEN {_FRESH_SQL} THEN 0 ELSE 1 END) > 0"
            " ORDER BY COUNT(*), m.project_id LIMIT ?",
            (KIND, EXTRACT_VERSION, time.time(), _FOCUS_PATIENTS))]
        hit = _FOCUS[key] = {"at": time.monotonic(), "pids": pids}
    return hit["pids"]


def patient_progress_counts(db) -> dict:
    """Counts only: active patients, those with every stored post extracted, and focus candidates.

    Completion counts pending posts even after retries are exhausted;
    only focus eligibility follows the worker's retry budget. The caller bounds time."""
    active = db.execute("SELECT COUNT(*) FROM patients WHERE COALESCE(is_archived,0)=0").fetchone()[0]
    pending = pending_pred(db)
    remaining, focus = db.execute(
        f"SELECT COUNT(*), COALESCE(SUM(EXISTS(SELECT 1 FROM messages m WHERE m.project_id=p.project_id "
        f"AND {pending} AND ({_retry_budget_sql()}) AND COALESCE((SELECT MAX(json_extract(e.meta,'$.next_try')) "
        f"{_ERROR_ROWS_SQL}), 0) <= ? AND NOT {_FRESH_SQL})), 0) "
        "FROM patients p WHERE COALESCE(p.is_archived,0)=0 "
        f"AND EXISTS(SELECT 1 FROM messages m WHERE m.project_id=p.project_id AND {pending})",
        (KIND, EXTRACT_VERSION, time.time())).fetchone()
    return {"active_patients": active, "extraction_complete": max(0, active - remaining),
            "focus_eligible": focus, "focus_targets": min(focus, _FOCUS_PATIENTS)}


class _CloudSelectionIncomplete(Exception):
    """A cloud selection could not finish safely within the current pass."""


def _cloud_priority_identity(db, cfg, admitted_ids, shard, oldest_first, archived_first, fair_oldest):
    """Identify one non-authoritative ordering walk without retaining receipt contents."""
    scope = db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind='cloud_history_scope' "
                       "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    control = db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind='cloud_control' "
                         "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    receipt = scoped_control = None
    if scope is not None:
        document = cloud_policy._history_document(scope['content'])
        command_id = document.get('command_id') if isinstance(document, dict) else None
        if isinstance(command_id, str):
            receipt = db.execute("SELECT outcome,receipt_json FROM command_receipts WHERE command_id=?",
                                 (command_id,)).fetchone()
            scoped_control = db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind='cloud_control' "
                "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.command_id') END=? "
                "ORDER BY artifact_id DESC LIMIT 1", (command_id,)).fetchone()
    return mcs_util.payload_hash({'scope': tuple(scope) if scope is not None else None,
        'control': tuple(control) if control is not None else None,
        'scoped_control': tuple(scoped_control) if scoped_control is not None else None,
        'receipt': tuple(receipt) if receipt is not None else None,
        'config': cfg, 'admitted': sorted(admitted_ids) if admitted_ids is not None else None,
        'shard': shard, 'oldest': oldest_first, 'archived': archived_first, 'fair': fair_oldest})


def _cloud_priority_discard(ledger, mid):
    """Move past a candidate that could not be prepared or claimed this pass."""
    state = getattr(ledger, '_cloud_priority_progress', None)
    if isinstance(state, dict):
        state['ready'] = [item for item in state['ready'] if item[0] != mid]


def _select_pending(ledger, limit: int, admitted_ids: set | None,
                    shard: tuple[int, int] | None,
                    oldest_first: bool, *, archived_first: bool = False,
                    fair_oldest: bool = False, prioritize_patients: bool = False,
                    cloud_only: bool = False, deadline: float | None = None,
                    exclude_ids=None) -> tuple[list, int | None]:
    deadline = deadline if cloud_only else None
    reader = None
    owner = ledger
    if deadline is not None:
        if time.monotonic() >= deadline:
            raise _CloudSelectionIncomplete('deadline')
        if ledger.db.in_transaction:
            raise _CloudSelectionIncomplete('active_transaction')
        # A caller-owned cancellation handler remains authoritative on this read.
        path = ledger.db.execute('PRAGMA database_list').fetchone()[2]
        if not path:
            raise _CloudSelectionIncomplete('reader_unavailable')
    try:
        if deadline is not None:
            before = os.stat(path)
            reader = sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro', uri=True, timeout=0)
            after = os.stat(path)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise _CloudSelectionIncomplete('reader_changed')
            reader.row_factory = sqlite3.Row
            reader.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            ledger = SimpleNamespace(db=reader, _cloud_state_owner=owner,
                                     _cloud_db_identity=(before.st_dev, before.st_ino))
            if prioritize_patients:
                # Keep the ordinary inference floor available whenever the
                # pass is long enough, while still allowing short passes to scan.
                deadline -= min(_MIN_CALL_S, max(0.0, deadline - time.monotonic()) / 2.0)
        check = (cloud_policy.history_selection_check(ledger.db) if cloud_only
                 else contextlib.nullcontext(None))
        with check as receipt_check:
            result = _select_pending_impl(ledger, limit, admitted_ids, shard, oldest_first,
                archived_first=archived_first, fair_oldest=fair_oldest,
                prioritize_patients=prioritize_patients, cloud_only=cloud_only,
                receipt_check=receipt_check, deadline=deadline,
                **({"exclude_ids": exclude_ids} if exclude_ids else {}))
        if deadline is not None and time.monotonic() >= deadline:
            raise _CloudSelectionIncomplete('deadline')
    except MemoryError as error:
        if reader is not None:
            with contextlib.suppress(sqlite3.Error):
                reader.close()
        if hasattr(owner, '_cloud_priority_progress'):
            owner._cloud_priority_progress = None
        raise _CloudSelectionIncomplete('memory') from error
    except BaseException as error:
        if reader is not None:
            with contextlib.suppress(sqlite3.Error):
                reader.close()
        if deadline is not None and isinstance(error, (sqlite3.OperationalError, OSError)):
            reason = 'deadline' if time.monotonic() >= deadline else 'reader_unavailable'
            raise _CloudSelectionIncomplete(reason) from error
        raise
    else:
        if reader is not None:
            try:
                reader.close()
            except sqlite3.Error as error:
                raise _CloudSelectionIncomplete('reader_close_failed') from error
        if cloud_only and prioritize_patients and deadline is not None:
            state = getattr(owner, '_cloud_priority_progress', None)
            if (isinstance(state, dict) and not state['ready']
                    and state['phase'] >= state.get('phase_count', 4)):
                owner._cloud_priority_progress = None
        return result


def _select_pending_impl(ledger, limit: int, admitted_ids: set | None,
                         shard: tuple[int, int] | None,
                         oldest_first: bool, *, archived_first: bool = False,
                         fair_oldest: bool = False, prioritize_patients: bool = False,
                         cloud_only: bool = False, receipt_check=None,
                         deadline: float | None = None, exclude_ids=None) -> tuple[list, int | None]:
    """Select the claimable pending rows for one run_pending pass ->
    (rows, how many of them are new arrivals; None for oldest_first)."""
    # pending = no current-version artifact, plus retriable CURRENT-
    # version error artifacts whose backoff expired (attempts < 5,
    # next_try <= now). Error rows of older schema versions neither
    # count toward attempts nor gate the retry — a v1 permanent failure
    # gets a fresh v2 budget. Every caller takes DESC (newest first) so
    # a missed arrival is not stuck behind the whole backlog; claim
    # leases keep concurrent callers off each other's rows. oldest_first
    # (ASC) remains for callers that must walk the tail.
    order = "ASC" if oldest_first else "DESC"
    # Canonical fact source closes legacy inference; conversion cohorts
    # never grant it an exception. Explicit admission sets from callers
    # stay inside the query so a large backlog cannot starve an admitted
    # message behind the LIMIT.
    adm_sql = ""
    adm_params: list = []
    if admitted_ids is not None:
        if len(admitted_ids) <= 30000:
            marks = ",".join("?" * len(admitted_ids)) or "NULL"
            adm_sql = f" AND m.message_id IN ({marks})"
            adm_params = sorted(admitted_ids)
        else:
            # One JSON parameter keeps the admission gate before LIMIT
            # even when the manifest exceeds SQLite's variable limit.
            adm_sql = " AND m.message_id IN (SELECT value FROM json_each(?))"
            adm_params = [json.dumps(sorted(admitted_ids))]
    if exclude_ids:
        if cloud_only:
            raise ValueError("cloud_selection_exclusion_invalid")
        # Only pass-local refusals are excluded; admission, source, shard and
        # quality gates remain in this same query before its SQL limit.
        adm_sql += " AND m.message_id NOT IN (SELECT value FROM json_each(?))"
        adm_params.append(json.dumps(sorted(exclude_ids)))
    required_contract = None
    if cloud_only:
        raw_cfg = load_config()
        cfg = cloud_inference.resolve_cloud_settings(raw_cfg)
        selection, errors = cloud_policy.resolve_profile(raw_cfg, route="reextract", purpose="backlog", root=HOME)
        if not errors and selection is not None:
            required_contract = cloud_inference.cloud_provider_contract(selection=selection)
        if cfg["mode"] != "on" or "reextract" not in cfg["routes"]:
            owner = getattr(ledger, '_cloud_state_owner', None)
            if owner is not None and prioritize_patients:
                owner._cloud_priority_progress = None
            return [], 0
        # Filter unpaid local-only work before LIMIT so it cannot starve cloud history.
        adm_sql += (" AND COALESCE(qc_src,0)=0 AND COALESCE(thin_src,0)=0 "
                    "AND COALESCE(human_src,0)=0 AND attempts=0 "
                    f"AND NOT EXISTS ({_hold_sql()})")
        adm_sql += ' AND ' + cloud_policy.history_scope_target_sql('m.message_id')
        for key, exclude in (("project_ids", False), ("exclude_project_ids", True)):
            if cfg[key] is not None and (cfg[key] or not exclude):
                adm_sql += " AND m.project_id " + ("NOT " if exclude else "") + "IN (SELECT value FROM json_each(?))"
                adm_params.append(json.dumps(cfg[key]))
    # QC-flagged rows keep their current artifact but re-enter pending
    # for exactly one feedback re-extract — a meta.qc_fix artifact
    # (applied or declined) ends the loop. Thin artifacts re-pend once
    # under the same settle-or-replace contract (meta.thin_retried).
    # next_try (claim lease and error backoff) is written from
    # time.time(), so it is compared against the same Python clock —
    # never SQLite's unixepoch(), which can disagree with it.
    now = time.time()
    def _select(fresh, n, archived, rehold=None, scope="", scope_params=()):
        if deadline is not None and time.monotonic() >= deadline:
            raise _CloudSelectionIncomplete('deadline')
        cursor = ledger.db.execute(_selection_sql(ledger.db, order, adm_sql + scope,
                                               fresh, archived, rehold, bounded=not cloud_only,
                                               prioritize_patients=prioritize_patients, cloud_followup=cloud_only), (
            KIND, EXTRACT_VERSION,
            *adm_params, *scope_params,
            shard[1] if shard else None,
            shard[1] if shard else 1,
            shard[0] if shard else 0,
            now,
            KIND, EXTRACT_VERSION,
            now, *((n,) if not cloud_only else ())))
        try:
            if not cloud_only:
                return cursor.fetchall()
            selected = []
            for row in cursor:
                if deadline is not None and time.monotonic() >= deadline:
                    raise _CloudSelectionIncomplete('deadline')
                if (cloud_policy.history_target_ready(ledger.db, row["project_id"], row["message_id"])
                        and receipt_check(row["project_id"], row["message_id"])
                        and required_contract is not None
                        and cloud_completion(ledger.db, row, required_contract) is None):
                    selected.append(row)
                    if len(selected) >= n:
                        break
            if deadline is not None and time.monotonic() >= deadline:
                raise _CloudSelectionIncomplete('deadline')
            return selected
        finally:
            cursor.close()

    if cloud_only and prioritize_patients and deadline is not None:
        owner = ledger._cloud_state_owner
        identity = (ledger._cloud_db_identity,
                    _cloud_priority_identity(ledger.db, cfg, admitted_ids, shard,
                                             oldest_first, archived_first, fair_oldest))
        state = getattr(owner, '_cloud_priority_progress', None)
        if not isinstance(state, dict) or state.get('identity') != identity:
            upper = ledger.db.execute('SELECT message_id FROM messages ORDER BY message_id DESC LIMIT 1').fetchone()
            state = {'identity': identity, 'upper': upper[0] if upper else None, 'after': None,
                     'maxima': {}, 'ordered': None, 'phase': 0, 'position': 0, 'message_after': None, 'ready': []}
            owner._cloud_priority_progress = state
        while state['ordered'] is None:
            if time.monotonic() >= deadline:
                raise _CloudSelectionIncomplete('deadline')
            if state['upper'] is None:
                page = []
            elif state['after'] is None:
                page = ledger.db.execute('SELECT message_id,project_id,posted_at_ts FROM messages '
                    'WHERE message_id<=? ORDER BY message_id LIMIT ?',
                    (state['upper'], _CLOUD_PRIORITY_PAGE)).fetchall()
            else:
                page = ledger.db.execute('SELECT message_id,project_id,posted_at_ts FROM messages '
                    'WHERE message_id>? AND message_id<=? ORDER BY message_id LIMIT ?',
                    (state['after'], state['upper'], _CLOUD_PRIORITY_PAGE)).fetchall()
            for mid, pid, posted in page:
                if time.monotonic() >= deadline:
                    raise _CloudSelectionIncomplete('deadline')
                if type(mid) is not int or (pid is not None and type(pid) is not int) \
                        or (posted is not None and (type(posted) not in (int, float) or not math.isfinite(posted))):
                    raise _CloudSelectionIncomplete('ordering_unverified')
                state['after'] = mid
                if pid is None:
                    continue  # history_target_ready cannot authorize NULL project IDs
                previous = state['maxima'].setdefault(pid, None)
                if posted is not None and (previous is None or posted > previous):
                    state['maxima'][pid] = posted
            if len(page) < _CLOUD_PRIORITY_PAGE:
                maxima = state.pop('maxima')
                state['ordered'] = sorted(maxima, key=lambda project_id:
                    patient_priority.priority_key(project_id, maxima[project_id]))
                if time.monotonic() >= deadline:
                    raise _CloudSelectionIncomplete('deadline')

        phases = [(archived, rehold) for archived in ((True, False) if archived_first else (False, True))
                  for rehold in ((None,) if oldest_first and fair_oldest else (False, True))]
        state['phase_count'] = len(phases)
        selected, ready = [], []
        # Re-read every previously selected ID under this pass's fresh receipt snapshot.
        old_ready = state['ready']
        for offset, (mid, archived, rehold) in enumerate(old_ready):
            if selected and deadline - time.monotonic() <= 1.0:
                ready.extend(old_ready[offset:])
                break
            found = _select(None, 1, archived, rehold, scope=' AND m.message_id=?', scope_params=(mid,))
            if found:
                selected.extend(found)
                ready.append((mid, archived, rehold))
        state['ready'] = ready
        selected = selected[:limit]
        while len(selected) < limit and state['phase'] < len(phases):
            if selected and deadline - time.monotonic() <= 1.0:
                break
            if time.monotonic() >= deadline:
                raise _CloudSelectionIncomplete('deadline')
            if state['position'] >= len(state['ordered']):
                state['phase'] += 1
                state['position'] = 0
                state['message_after'] = None
                continue
            pid = state['ordered'][state['position']]
            scope, params = '', [pid, state['upper']]
            if state['message_after'] is not None:
                posted, mid = state['message_after']
                comparison = '>' if oldest_first else '<'
                if posted is None:
                    scope = f' AND ((posted_at_ts IS NULL AND message_id{comparison}?)' + (
                        ' OR posted_at_ts IS NOT NULL)' if oldest_first else ')')
                    params.append(mid)
                else:
                    scope = f' AND ((posted_at_ts,message_id){comparison}(?,?)' + (
                        ')' if oldest_first else ' OR posted_at_ts IS NULL)')
                    params.extend((posted, mid))
            # Rank patients once; walk each patient's existing time index in bounded pages.
            page = ledger.db.execute('SELECT message_id,posted_at_ts FROM messages '
                'WHERE project_id=? AND message_id<=?' + scope +
                f' ORDER BY posted_at_ts {order},message_id {order} LIMIT ?',
                (*params, _CLOUD_PRIORITY_PAGE)).fetchall()
            archived, rehold = phases[state['phase']]
            for mid, posted in page:
                if selected and deadline - time.monotonic() <= 1.0:
                    break
                if posted is not None and (type(posted) not in (int, float) or not math.isfinite(posted)):
                    raise _CloudSelectionIncomplete('ordering_unverified')
                found = _select(None, 1, archived, rehold, scope=' AND m.message_id=?', scope_params=(mid,))
                state['message_after'] = (posted, mid)
                if found:
                    state['ready'].append((mid, archived, rehold))
                    selected.extend(found)
                if len(selected) >= limit:
                    break
            else:
                if len(page) < _CLOUD_PRIORITY_PAGE:
                    state['position'] += 1
                    state['message_after'] = None
        if time.monotonic() >= deadline:
            raise _CloudSelectionIncomplete('deadline')
        return selected, None if oldest_first else 0

    rows, fresh_selected = [], None if oldest_first else 0
    # an admission set (canonical engine) already decides what may run
    focus = [] if oldest_first or admitted_ids is not None or prioritize_patients else _focus_patients(ledger)
    # NULL project rows must survive the exclusion (NULL NOT IN is NULL)
    rest = (" AND (m.project_id IS NULL OR m.project_id NOT IN ("
            + ",".join("?" * len(focus)) + "))") if focus else ""
    # Active patients normally precede archived ones; the resident's
    # archive fairness turn reverses those classes. Each class keeps its
    # usual fresh/re-extraction order, except that a fair oldest turn
    # interleaves update-released holds by source time.
    for archived in ((True, False) if archived_first else (False, True)):
        for fresh in ((None,) if oldest_first or prioritize_patients else (True, False)):
            if fresh is False and not archived:
                # the patients nearest completion re-extract first, one by one;
                # their released holds too, since the patient completes only
                # once those re-runs settle under the current contract
                for pid in focus:
                    for rehold in (False, True):
                        if len(rows) >= limit:
                            return rows, fresh_selected
                        rows += _select(False, limit - len(rows), False, rehold,
                                        scope=" AND m.project_id=?", scope_params=(pid,))
            # a never-extracted body has no hold, so fresh needs no split
            for rehold in ((None,) if fresh or (oldest_first and fair_oldest)
                           else (False, True)):
                if len(rows) >= limit:
                    return rows, fresh_selected
                scoped = fresh is False and not archived and focus
                selected = _select(fresh, limit - len(rows), archived, rehold,
                                   scope=rest if scoped else "",
                                   scope_params=focus if scoped else ())
                rows += selected
                if fresh_selected is not None and fresh:
                    fresh_selected += len(selected)
    return rows, fresh_selected
