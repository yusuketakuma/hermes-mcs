"""Persist bounded extraction failures and source-bound work leases."""
import contextlib
import json
import os
import sqlite3
import time
import writer_lock
import inference_version as inference
import bounded_http
from clinical_chunking import PLAN_VERSION
import local_llm
import mcs_util
from mcs_queries import (current_extract_pred,
                         json_or_null)
from mcs_util import loads_dict
from writer_lock import acquire_run_lock

from extract_llm_call_context import (
    _opener_request,
)
from extract_llm_context import (
    _chunk_context,
    _frame_meta,
    _thread_context,
)
from extract_llm_progress import (
    cloud_completion,
)
from extract_llm_proofs import (
    _checkpoint_identity,
    _delete_checkpoint_keys_tx,
    _verify_atomic_witness,
)
from extract_llm_provider import (
    _actual_model,
    _resolved_llm,
)
from extract_llm_qc import (
    _AUTO_RETRY_SQL,
    _prior_extract,
)
from extract_llm_selection import (
    _selection_sql,
)
from extract_llm_source_contract import (
    EXTRACT_VERSION,
    KIND,
    REVIVE_COOLDOWN_S,
    REVIVE_MAX,
    REVIVE_PER_INPUT,
    TIMEOUT,
    _EXTRACT_LEASE_S,
    _INFERENCE_VERSION,
    _PREPARE_HOLD_S,
    _owns_current_source,
    _source_writable,
    _writer_current,
    _writer_current_diagnostic,
)
from extract_llm_store import (
    _record_hold_tx,
)

def _llm_up(deadline: float | None = None) -> bool:
    try:
        status, _headers, raw = _opener_request(
            local_llm.probe_urls(_resolved_llm()[0])[0], "GET", None, 3, deadline)
        return status == 200 and len(raw) <= bounded_http.MAX_RESPONSE_BYTES
    except (OSError, ValueError):
        return False


def _fail_tx(ledger, r, attempts: int, auto_retry: int = 0, *, scoped_cleanup=False, result_witness=None):
    """In-transaction error artifact insert — caller holds `with
    ledger.db` AND has already cleared prior error rows in the same tx.
    extract_version is stamped so a prior schema version's permanent
    failure never blocks the current version's retry budget."""
    if not ledger.db.in_transaction:
        ledger.db.execute("BEGIN IMMEDIATE")
    meta = {inference.VERSION_KEY: _INFERENCE_VERSION, "hash": r["content_hash"], "error": True,
            "extract_version": EXTRACT_VERSION,
            "attempts": attempts + 1,
            "next_try": time.time() + min(3600, 300 * (attempts + 1))}
    if any(key not in r.keys() for key in ("parent_id", "posted_at", "posted_at_ts")):
        source = ledger.db.execute("SELECT * FROM messages WHERE message_id=? AND content_hash=?",
                                   (r["message_id"], r["content_hash"])).fetchone()
        if source is not None:
            r = {**dict(source), **dict(r)}
    if not _source_writable(ledger.db, r):
        return
    actual_model = _actual_model(r)
    can_bind = all(key in r.keys() for key in ("parent_id", "posted_at", "posted_at_ts"))
    context = (r["_context"] if "_context" in r.keys() else _thread_context(ledger.db, r)) if can_bind else None
    meta.update(actual_model=actual_model, plan_version=PLAN_VERSION,
                context_binding=_chunk_context(r, context) if can_bind else None)
    meta.update(_frame_meta(r))
    if auto_retry:
        meta["auto_retry"] = auto_retry
    content_json = json.dumps({"_model": actual_model, "_error": True}, ensure_ascii=False)
    meta_json = json.dumps(meta, ensure_ascii=False)
    created_at = time.time()
    cur = ledger.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (KIND, r['project_id'], r['message_id'], content_json, actual_model, meta_json, created_at))
    if cur.rowcount != 1:
        raise RuntimeError('extract_insert_unverified')
    witness = {'artifact_id': cur.lastrowid, 'kind': KIND, 'project_id': r['project_id'],
               'message_id': r['message_id'], 'content': content_json, 'model': actual_model,
               'meta': meta_json, 'created_at': created_at}
    _verify_atomic_witness(ledger.db, r, [witness])
    if result_witness is not None:
        result_witness.append(witness)
    if attempts + 1 >= 5:
        prior = _prior_extract(ledger.db, r)
        if prior and (loads_dict(prior["meta"]).get("extract_version") != EXTRACT_VERSION
                      or not inference.is_current(loads_dict(prior["meta"]), target=_INFERENCE_VERSION)):
            hold_witness = _record_hold_tx(ledger, r, prior, "attempts_exhausted", remove_chunks=not scoped_cleanup)
            if result_witness is not None:
                result_witness.extend(hold_witness)
            return True
    return False


def revive_failed(ledger, now: float | None = None) -> dict:
    """Re-open exhausted current-version error rows for one more try."""
    now = time.time() if now is None else now
    out = {"revived": 0, "skipped_cap": 0}
    if not _writer_current():
        return out
    # capped rows are excluded BEFORE the LIMIT — selecting them first
    # let a capped prefix starve every eligible row behind it
    rows = ledger.db.execute(
        "SELECT artifact_id,meta,kind,project_id,message_id,created_at,content,model FROM artifacts WHERE kind=? "
        "AND EXISTS (SELECT 1 FROM messages m WHERE m.message_id=artifacts.message_id "
        "AND (artifacts.project_id IS NULL OR artifacts.project_id=m.project_id)) "
        "AND typeof(content)='text' AND length(content)<=262144 "
        "AND (model IS NULL OR (typeof(model)='text' AND length(model)<=32768)) "
        "AND " + inference.current_meta_pred("meta", target=_INFERENCE_VERSION) + " AND json_valid(meta) AND json_extract(meta,'$.error')=1 "
        "AND COALESCE(json_extract(meta,'$.invalidated'),0)=0 "
        "AND COALESCE(json_extract(meta,'$.extract_version'),0)=? "
        "AND COALESCE(json_extract(meta,'$.attempts'),0)>=5 "
        "AND created_at<=? AND " + _AUTO_RETRY_SQL + "<? "
        "ORDER BY created_at LIMIT ?",
        (KIND, EXTRACT_VERSION, now - REVIVE_COOLDOWN_S,
         REVIVE_PER_INPUT, REVIVE_MAX * 4)).fetchall()
    out["skipped_cap"] = ledger.db.execute(
        "SELECT COUNT(*) FROM artifacts WHERE kind=? "
        "AND EXISTS (SELECT 1 FROM messages m WHERE m.message_id=artifacts.message_id "
        "AND (artifacts.project_id IS NULL OR artifacts.project_id=m.project_id)) "
        "AND typeof(content)='text' AND length(content)<=262144 "
        "AND (model IS NULL OR (typeof(model)='text' AND length(model)<=32768)) "
        "AND " + inference.current_meta_pred("meta", target=_INFERENCE_VERSION) + " AND json_valid(meta) AND json_extract(meta,'$.error')=1 "
        "AND COALESCE(json_extract(meta,'$.invalidated'),0)=0 "
        "AND COALESCE(json_extract(meta,'$.extract_version'),0)=? "
        "AND COALESCE(json_extract(meta,'$.attempts'),0)>=5 "
        "AND created_at<=? AND " + _AUTO_RETRY_SQL + ">=?",
        (KIND, EXTRACT_VERSION, now - REVIVE_COOLDOWN_S,
         REVIVE_PER_INPUT)).fetchone()[0]
    for row in rows:
        if out["revived"] >= REVIVE_MAX:
            break
        try:
            meta = json.loads(row["meta"])
        except (ValueError, TypeError, RecursionError):
            continue
        n = meta.get("auto_retry")
        n = n if type(n) is int and n >= 0 else 0
        if n >= REVIVE_PER_INPUT:
            out["skipped_cap"] += 1
            continue
        meta.update({"attempts": 4, "next_try": now, "auto_retry": n + 1})
        updated_meta = json.dumps(meta, ensure_ascii=False)
        with ledger.db:
            if not _writer_current(full=True):
                ledger.db.rollback()
                return out
            cur = ledger.db.execute(
                "UPDATE artifacts SET meta=? WHERE artifact_id=? AND meta=? AND kind IS ? "
                "AND project_id IS ? AND message_id IS ? AND created_at IS ? AND content IS ? AND model IS ? "
                "AND EXISTS (SELECT 1 FROM messages m WHERE m.message_id=artifacts.message_id "
                "AND (artifacts.project_id IS NULL OR artifacts.project_id=m.project_id))",
                (updated_meta, row["artifact_id"], row["meta"], row["kind"], row["project_id"],
                 row["message_id"], row["created_at"], row["content"], row["model"]))
            if cur.rowcount and ledger.db.execute(
                "SELECT 1 FROM artifacts WHERE artifact_id=? AND meta IS ? AND kind IS ? "
                "AND project_id IS ? AND message_id IS ? AND created_at IS ? AND content IS ? AND model IS ? "
                "AND EXISTS (SELECT 1 FROM messages m WHERE m.message_id=artifacts.message_id "
                "AND (artifacts.project_id IS NULL OR artifacts.project_id=m.project_id))",
                (row["artifact_id"], updated_meta, row["kind"], row["project_id"],
                 row["message_id"], row["created_at"], row["content"], row["model"])).fetchone() is None:
                raise RuntimeError("extract_revive_unverified")
            if not _writer_current(full=True):
                ledger.db.rollback()
                return out
        # Another drainer/revival may have changed or removed the snapshot.
        out["revived"] += cur.rowcount
    return out


def _fail(ledger, r, attempts: int, *, lease=None):
    """Record an error artifact with retry metadata — a transient failure
    must retry later, a permanent one must stop (Oracle B21). Prior
    error rows are folded in, not stacked."""
    if ledger.db.in_transaction:
        return
    with ledger.db:
        if not ledger.db.in_transaction:
            ledger.db.execute("BEGIN IMMEDIATE")
        if not _source_writable(ledger.db, r):
            return
        if lease is not None and not _owns_current_source(ledger, r, lease):
            return
        if lease is not None:
            size, layout, identities = _checkpoint_identity(
                ledger.db, r, r.get("_context"), r["_chunk_size"])
            if (size != r["_chunk_size"] or layout != r.get("_checkpoint_layout")
                    or identities != r.get("_checkpoint_identity") or r.get("_checkpoint_conflict")):
                return
        prev = _error_attempts(ledger, r["message_id"], r["content_hash"])
        revived = _error_auto_retry(ledger, r["message_id"],
                                    r["content_hash"])
        _clear_error_tx(ledger, r["message_id"], r["project_id"])
        if not _source_writable(ledger.db, r):
            raise RuntimeError("extract_source_changed_during_write")
        witness = []
        held = _fail_tx(ledger, r, max(attempts, prev), revived, scoped_cleanup=lease is not None,
                        result_witness=witness)
        if held and lease is not None:
            _delete_checkpoint_keys_tx(ledger.db, r, r.get("_context"), identities.keys())
        if not witness:
            raise RuntimeError("extract_insert_unverified")
        _verify_atomic_witness(ledger.db, r, witness)


def _error_auto_retry(ledger, mid: int, content_hash) -> int:
    """Periodic revivals already spent on this body hash under the CURRENT
    extract version (0 for a new body or version) — carried so a refail
    keeps the per-input cap without an older version's spent revivals
    blocking the current version's budget."""
    safe = json_or_null("a.meta")
    return ledger.db.execute(
        f"SELECT COALESCE(MAX(CASE WHEN json_type({safe},'$.auto_retry')='integer' "
        f"THEN json_extract({safe},'$.auto_retry') ELSE 0 END),0) "
        "FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
        "WHERE a.kind=? AND a.message_id=? "
        + current_extract_pred("a", "m", content=False, error_check=False, inference_target=_INFERENCE_VERSION)
        + f" AND json_extract({safe},'$.error')=1 "
        f"AND COALESCE(json_extract({safe},'$.extract_version'),0)=? "
        f"AND json_extract({safe},'$.hash')=?",
        (KIND, mid, EXTRACT_VERSION, content_hash)).fetchone()[0] or 0


def _error_attempts(ledger, mid: int, content_hash) -> int:
    """Highest attempts count on the message's CURRENT-version error
    rows for THIS body — read inside the write tx so a concurrent
    writer's row isn't rolled back to a stale count. Stale rows of an
    older body hash may linger until the next sweep; they must not
    fold into the fresh body's budget (mirrors _error_auto_retry)."""
    safe = json_or_null("a.meta")
    return ledger.db.execute(
        f"SELECT COALESCE(MAX(CASE WHEN json_type({safe},'$.attempts')='integer' "
        f"AND json_extract({safe},'$.attempts')>=0 THEN json_extract({safe},'$.attempts') ELSE 0 END),0) "
        "FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
        "WHERE a.kind=? AND a.message_id=? "
        + current_extract_pred("a", "m", content=False, error_check=False, inference_target=_INFERENCE_VERSION)
        + f" AND json_extract({safe},'$.error')=1 "
        f"AND COALESCE(json_extract({safe},'$.extract_version'),0)=? "
        f"AND json_extract({safe},'$.hash')=?",
        (KIND, mid, EXTRACT_VERSION, content_hash)).fetchone()[0]


def _clear_error_tx(ledger, mid: int, project_id: int):
    ledger.db.execute(
        "DELETE FROM artifacts WHERE kind=? AND message_id=? AND (project_id IS NULL OR project_id=?) "
        "AND " + inference.current_meta_pred("meta", target=_INFERENCE_VERSION) + " AND json_valid(meta) AND json_extract(meta,'$.error')=1",
        (KIND, mid, project_id))


@contextlib.contextmanager
def _write_lock(enabled: bool, wait_s: float = 60, *, deadline=None):
    """Hold the run lock only across a DB write when `enabled`, waiting
    up to ``wait_s`` seconds for it.

    The tick holds the lock outside local transport, so it passes False.
    The standalone --all backlog drainer passes True so it
    never holds the lock longer than a single write — holding it across
    the whole backlog would starve the 10-min tick for days.

    Only result writes go through this lock: ``_commit``, ``_fail`` and
    the QC/thin/human re-mints. The drainer's other writes are lockless
    on purpose — claim leases (a cross-process fence of their own),
    rule-only ``extract_v1``/prefilter markers and chunk checkpoints.
    Claim renewal checks the owner token, lease and source hash; release
    checks the owner token and lease. Other writes use their respective
    source and generation guards. Taking the lock per row (up to 60 s
    each) while leases are held would stall the drainer for no proven gain."""
    fd = None
    if enabled and deadline is not None:
        until = min(deadline, time.monotonic() + max(0.0, wait_s))
        while time.monotonic() < deadline:
            fd = acquire_run_lock()
            if fd is not None:
                break
            remaining = until - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))
    elif enabled:
        for _ in range(max(1, int(wait_s))):
            fd = acquire_run_lock()
            if fd is not None:
                break
            time.sleep(1)
    try:
        if enabled and fd is None:
            writer_lock.record_writer_refusal("run_lock_not_acquired", "write_lock")
        yield (fd is not None or not enabled) and _writer_current_diagnostic(full=enabled)
    finally:
        if fd is not None:
            os.close(fd)


def _claim(ledger, r, lease_s: float = _EXTRACT_LEASE_S) -> float | None:
    """Atomically claim a pending row via a fetch_jobs lease (F14).
    Returns this worker's lease expiry on success, None when another
    worker's live lease already covers the row — the ON CONFLICT
    conditional update is a single statement, so the check-and-set is
    race-free."""
    if ledger.db.in_transaction:
        return None
    ledger.db.execute("BEGIN IMMEDIATE")
    try:
        if not _source_writable(ledger.db, r):
            ledger.db.commit()
            return None
        if isinstance(r, dict) and r.get('_cloud_mode') in ('on', 'shadow'):
            now = time.time()
            current = ledger.db.execute(
                _selection_sql(ledger.db, 'DESC', ' AND m.message_id=?',
                               cloud_followup=bool(r.get('_required_cloud_contract'))),
                (KIND, EXTRACT_VERSION, r['message_id'], None, 1, 0, now,
                 KIND, EXTRACT_VERSION, now, 1)).fetchone()
            if (r.get('_required_cloud_contract') and cloud_completion(ledger.db, r, r['_required_cloud_contract']) is not None):
                ledger.db.commit()
                return None
            if current is None or any(current[key] != r.get(key)
                                      for key in ('qc_src', 'thin_src', 'human_src', 'attempts')):
                ledger.db.commit()
                return None
        if isinstance(r, dict):
            from inference_dependency_loss import source_binding
            r['_source_binding'] = source_binding(ledger.db, r['project_id'], r['message_id'])
        now = time.time()
        lease = now + lease_s
        token = os.urandom(16).hex() if isinstance(r, dict) else None
        cur = ledger.db.execute("""
      INSERT INTO fetch_jobs(kind,project_id,message_id,
        parent_id,payload,state,next_try,created_at,updated_at)
      VALUES('extract_claim',?,?,NULL,?,'pending',?,?,?)
      ON CONFLICT(kind,project_id,message_id) DO UPDATE SET
        payload=excluded.payload,state='pending',
        next_try=excluded.next_try,updated_at=excluded.updated_at
      WHERE fetch_jobs.next_try <= ?
        """, (r["project_id"], r["message_id"],
              json.dumps({inference.VERSION_KEY: _INFERENCE_VERSION, "hash": r["content_hash"],
                          "ver": EXTRACT_VERSION, "owner_token": token}),
              lease, now, now, now))
        ledger.db.commit()
    except BaseException:
        try:
            ledger.db.rollback()
        except sqlite3.Error:
            pass
        raise
    if cur.rowcount > 0 and isinstance(r, dict):
        r["_claim_token"] = token
        r["_current_lease"] = lease
    return lease if cur.rowcount > 0 else None


def _release(ledger, r, lease: float | None):
    """Release our lease or briefly retain a rejected save under its exact owner fence.
    The next_try equality guard keeps a replacement claim alive."""
    if lease is None or ledger.db.in_transaction:
        return
    token = r.get("_claim_token") if isinstance(r, dict) else None
    if isinstance(r, dict) and r.pop("_commit_hold", False):
        # Preserve this owner's claim briefly after a rejected save; checking
        # the occurrence and updating its lease share one writer transaction.
        ledger.db.execute("BEGIN IMMEDIATE")
        try:
            if (mcs_util.valid_hash(r.get("_source_binding")) and type(token) is str and token
                    and _source_writable(ledger.db, r) and _owns_current_source(ledger, r, lease)):
                now = time.time()
                held = ledger.db.execute(
                    "UPDATE fetch_jobs SET next_try=?,updated_at=? WHERE kind='extract_claim' "
                    "AND project_id=? AND message_id=? AND next_try=? AND next_try>? "
                    "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.owner_token') END IS ?",
                    (now + _PREPARE_HOLD_S, now, r["project_id"], r["message_id"], lease, now, token))
                if held.rowcount:
                    ledger.db.commit()
                    return
        except BaseException:
            ledger.db.rollback()
            raise
        # A changed source or expired claim follows the existing exact-owner
        # cleanup, without holding the replacement occurrence or another owner.
    try:
        ledger.db.execute(
            "DELETE FROM fetch_jobs WHERE kind='extract_claim' "
            "AND project_id=? AND message_id=? AND next_try=? "
            "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.owner_token') END IS ?",
            (r["project_id"], r["message_id"], lease, token))
        ledger.db.commit()
    except BaseException:
        try:
            ledger.db.rollback()
        except sqlite3.Error:
            pass
        raise


def _renew(ledger, r, lease: float) -> float | None:
    """Extend OUR lease on a carried result so the context units it
    waits through cannot outlive the claim; None when the lease was
    lost (lapsed and re-claimed) or the body changed underneath — the
    result is then dropped so the live claim stops hiding the new body
    from selection."""
    if ledger.db.in_transaction:
        return None
    now = time.time()
    # must outlast one 900 s context unit plus _CARRY_MARGIN_S, or the
    # next settle drops it; same length as a fresh claim
    new = max(lease, now + _EXTRACT_LEASE_S + TIMEOUT + 30)
    token = r.get("_claim_token") if isinstance(r, dict) else None
    try:
        cur = ledger.db.execute(
            "UPDATE fetch_jobs SET next_try=?, updated_at=? "
            "WHERE kind='extract_claim' AND project_id=? AND message_id=? "
            "AND next_try=? AND next_try>? "
            "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.owner_token') END IS ? "
            "AND EXISTS (SELECT 1 FROM messages m "
            "WHERE m.message_id=fetch_jobs.message_id "
            "AND m.project_id=fetch_jobs.project_id AND m.content_hash=?)",
            (new, now, r["project_id"], r["message_id"], lease, now, token, r["content_hash"]))
        ledger.db.commit()
    except BaseException:
        try:
            ledger.db.rollback()
        except sqlite3.Error:
            pass
        raise

    if cur.rowcount > 0 and isinstance(r, dict):
        r["_current_lease"] = new
        if "_cloud_lease" in r:
            r["_cloud_lease"] = new
    return new if cur.rowcount > 0 else None
