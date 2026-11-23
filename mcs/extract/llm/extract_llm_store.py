"""Atomically adopt extraction and checkpoint outputs under source and lease fences."""
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import inference_version as inference
import cloud_client
import cloud_policy
import cloud_inference
import provider_registry
import extract_validation
import extract_quality
from extraction_provenance import (
    provider_evidence as _provider_evidence,
    provider_strict_dict as _provider_strict_dict,
    provider_result_hash as _provider_result_hash,
    internal_proof_hash as _internal_proof_hash,
    evidence_matches_route as _evidence_matches_route,
    prior_provider_evidence as _prior_provider_evidence,
    leaf_evidence as _leaf_evidence,
)
import extraction_refresh
from clinical_chunking import PLAN_VERSION, plan_chunks
import mcs_queries
import mcs_util
import extraction_context
import semantic_qc
from mcs_queries import (current_extract_pred, json_or_null)
from mcs_util import HOME, load_config, load_config_checked, loads_dict

from extract_llm_context import (
    _chunk_context,
    _frame_meta,
    _frame_references_current,
    _frame_refs,
    _medication_reference_current,
    _thread_context,
)
from extract_llm_progress import (
    cloud_completion_any,
)
from extract_llm_prompts import (
    MAX_TOKENS,
    _CTX_TOTAL_MAX,
)
from extract_llm_proofs import (
    _CheckpointChanged,
    _checkpoint_identity,
    _checkpoint_keys,
    _checkpoint_matches,
    _checkpoint_state_matches,
    _checkpoint_write,
    _chunk_piece,
    _delete_checkpoint_keys_tx,
    _extract_inline_proof,
    _replay_repair_leaf,
    _saved_chunk_state,
    _verify_atomic_witness,
    _verify_completeness_claim,
    _verify_inline_extract_proof,
    _verify_unique_llm_current,
)
from extract_llm_provider import (
    _actual_model,
    _cloud_compare_tx,
    _comparison_extract,
    _leaf_record,
)
from extract_llm_qc import (
    _commit_rejected,
    _current,
    _open_attempt_contracts,
    _qc_attempt_notes_current,
    _qc_attempt_superseded,
    _qc_cloud_followup_sql,
    _qc_feedback,
    _qc_notes_digest,
    _prior_extract,
    _prior_matches,
    _protected_prior_current,
    _rerender_cards,
    _upgrade_contract,
    _verify_created_artifact_tx,
    source_revision_sql,
)
from extract_llm_source_contract import (
    CLOUD_ATTEMPT_KIND,
    EXTRACT_VERSION,
    KIND,
    PATIENT_CONTEXT_VERSION,
    UPGRADE_KIND,
    _CHUNK_SIZE,
    _INFERENCE_VERSION,
    _INTERNAL_LEAF_CLAIMS,
    _INTERNAL_REPAIR_PROOF,
    _STALE_GC_BUDGET_S,
    _owns_current_source,
    _source_writable,
    _unsealed_source_pred,
    _writer_current,
)
from extract_llm_selection import (
    _StaleGCDeadline,
    _stale_gc_transaction,
)

# Write transactions holding the SQLite writer lock past this are reported as
# *_tx_hold events — same stderr drain path as ledger.intake_tx_hold; behavior
# is unchanged (instrumentation for the writer-lock contention survey).
_TX_HOLD_REPORT_S = 5.0


def _report_tx_hold(event: str, started, r, guard=None) -> None:
    if started is None:
        return
    held = time.monotonic() - started
    if held >= _TX_HOLD_REPORT_S:
        payload = {"event": event, "pid": os.getpid(), "ts": time.time(),
                   "message_id": r.get("message_id") if isinstance(r, dict) else None,
                   "hold_s": round(held, 2)}
        if guard is not None:
            payload["guard_path"] = getattr(guard, "path", None)
            payload["guard_verdict"] = getattr(guard, "verdict", None)
        print(json.dumps(payload), file=sys.stderr, flush=True)


def _record_hold_tx(ledger, r, prior, reason, *, lost=None, lost_class=None, feedback_id=None, remove_chunks=True):
    if not ledger.db.in_transaction:
        ledger.db.execute('BEGIN IMMEDIATE')
    prior_row = ledger.db.execute(
        "SELECT * FROM artifacts WHERE artifact_id=? AND kind=? AND message_id=? "
        "AND (project_id IS NULL OR project_id=?)",
        (prior['artifact_id'], KIND, r['message_id'], r['project_id'])).fetchone()
    if prior_row is None:
        raise RuntimeError('extract_hold_unverified')
    # Some internal callers supply only the id; callers which already selected
    # the complete row must retain the exact prior across this transaction.
    if all(key in prior.keys() for key in ('kind', 'project_id', 'message_id', 'content', 'model', 'meta', 'created_at')):
        if any(prior[key] != prior_row[key] for key in ('kind', 'project_id', 'message_id', 'content', 'model', 'meta', 'created_at')):
            raise RuntimeError('extract_hold_unverified')
    prior_witness = dict(prior_row)
    receipt = {inference.VERSION_KEY: _INFERENCE_VERSION, "state": "held_prior", "reason": reason, "hash": r["content_hash"],
        "prior_artifact_id": prior["artifact_id"], "target_version": EXTRACT_VERSION,
        "contract": _upgrade_contract()}
    if lost:
        receipt["lost"] = lost
    if lost_class:
        receipt["lost_class"] = lost_class
    if feedback_id is not None:
        receipt["feedback_id"] = feedback_id
    raw = json.dumps(receipt, sort_keys=True)
    receipt_meta = {inference.VERSION_KEY: _INFERENCE_VERSION, 'hash': r['content_hash'],
                    'contract': receipt['contract']}
    meta_json = json.dumps(receipt_meta, ensure_ascii=False)
    saved = ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind=? AND project_id=? AND message_id=? AND content=? AND meta=? "
        "ORDER BY artifact_id DESC LIMIT 1", (UPGRADE_KIND, r['project_id'], r['message_id'], raw, meta_json)).fetchone()
    if saved is None:
        created_at = time.time()
        cur = ledger.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) VALUES(?,?,?,?,?,?,?)",
            (UPGRADE_KIND, r['project_id'], r['message_id'], raw, '', meta_json, created_at))
        if cur.rowcount != 1:
            raise RuntimeError('extract_hold_unverified')
        receipt_witness = {'artifact_id': cur.lastrowid, 'kind': UPGRADE_KIND,
            'project_id': r['project_id'], 'message_id': r['message_id'], 'content': raw,
            'model': '', 'meta': meta_json, 'created_at': created_at}
    else:
        receipt_witness = dict(saved)
    witnesses = [prior_witness, receipt_witness]
    _verify_atomic_witness(ledger.db, r, witnesses)
    # Settled rejected chunks must never become a fresh human correction's input.
    if remove_chunks:
        ledger.db.execute("DELETE FROM artifacts WHERE kind='extract_llm_chunk' AND message_id=? "
                          "AND (project_id=? OR project_id IS NULL) AND "
                          + inference.current_meta_pred("meta", target=_INFERENCE_VERSION),
                          (r["message_id"], r["project_id"]))
    _verify_atomic_witness(ledger.db, r, witnesses)
    return witnesses


def _add_cloud_attempt_tx(db, r, state: str, **binding):
    """Append one cloud attempt record of the row's body unless the identical record exists."""
    content = json.dumps({"hash": r["content_hash"], "purpose": "qc" if r.get("qc_cloud") else "backlog",
                          "state": state, **binding}, sort_keys=True)
    if db.execute("SELECT 1 FROM artifacts WHERE kind=? AND project_id=? AND message_id=? AND content=? LIMIT 1",
                  (CLOUD_ATTEMPT_KIND, r["project_id"], r["message_id"], content)).fetchone() is None:
        db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                   "VALUES(?,?,?,?,'','{}',?)",
                   (CLOUD_ATTEMPT_KIND, r["project_id"], r["message_id"], content, time.time()))


def _record_cloud_attempt_tx(ledger, r, reservation) -> int:
    """Record a paid request of the row's QC second attempt in its reservation transaction -> rows written.

    The first one opens the attempt. Each record binds the request to the
    flagged extraction, the source revision, the pinned contract
    (``_cloud_pin``) and the audit notes its prompt carries (``_qc_notes``),
    so only that unfinished attempt resumes (cloud_attempt_spent_sql) and
    only under that contract and those notes. A post that no longer
    qualifies or left the QC window, a contract other than the attempt's, or
    notes a newer audit changed refuse the reservation: nothing is sent or
    recorded."""
    db, pin, notes = ledger.db, r.get("_cloud_pin"), r.get("_qc_notes")
    refused = cloud_client.CloudError("budget_exceeded", "cloud_attempt_unavailable")
    if (not db.in_transaction or type(r.get("qc_cloud")) is not int or not isinstance(pin, dict)
            or not isinstance(notes, str) or not isinstance(reservation.get("reservation_id"), str)):
        raise refused
    before = db.total_changes
    revision = db.execute(f"SELECT {source_revision_sql()} FROM messages m WHERE m.message_id=?",
                          (r["message_id"],)).fetchone()
    _add_cloud_attempt_tx(db, r, "open", source_artifact_id=r["qc_cloud"],
                          revision=revision[0] if revision else None, contract=pin, notes=notes,
                          reservation_id=reservation["reservation_id"])
    # With this record written, the lane's own predicate (QC window included) must still admit the post.
    scope, scope_params = semantic_qc.qc_scope_sql(time.time())
    row = db.execute(
        f"SELECT ({_qc_cloud_followup_sql()}) FROM messages m "
        f"WHERE m.message_id=? AND m.project_id=? AND m.content_hash=? AND {scope}",
        (r["message_id"], r["project_id"], r["content_hash"], *scope_params)).fetchone()
    current = _qc_feedback(ledger, r["qc_cloud"])   # the newest audit's notes, read in this transaction
    if (row is None or row[0] != r["qc_cloud"] or any(started != pin for started in _open_attempt_contracts(db, r))
            or _qc_notes_digest(current) != notes or not _qc_attempt_notes_current(db, {**r}, current)):
        raise refused
    return db.total_changes - before


def _close_cloud_attempt_tx(db, r):
    """Record in the caller's transaction that a cloud re-extraction of the row's body ended."""
    _add_cloud_attempt_tx(db, r, "closed")


def _close_cloud_attempt(ledger, r) -> bool:
    """Record, in its own transaction, that a paid cloud re-extraction of the row's body ended for good.

    A QC second attempt ends only once a paid request opened it: before that
    nothing was spent, and the post stays eligible."""
    if ledger.db.in_transaction:
        return False
    with ledger.db:
        ledger.db.execute("BEGIN IMMEDIATE")
        if r.get("qc_cloud") and not _open_attempt_contracts(ledger.db, r):
            return False
        _close_cloud_attempt_tx(ledger.db, r)
    return True


def _cloud_attempt_withdrawn(ledger, cfg, r) -> bool:
    """Whether the row's open QC second attempt has ended for good; it reads settings but never waits for them.

    It has when its records disagree, a readable configuration, review and
    key file no longer offer its pinned contract (cloud_policy.resolve_contract
    ``gone``), or a newer audit changed the notes it started under
    (``_qc_attempt_superseded``); a waiting or unverifiable contract has not."""
    started = _open_attempt_contracts(ledger.db, r)
    return bool(started) and (any(contract != started[0] for contract in started)
                              or _qc_attempt_superseded(ledger, r)
                              or cloud_policy.resolve_contract(cfg, started[0], route="reextract", purpose="backlog",
                                                               root=HOME, wait=False).state == "gone")


def _open_cloud_attempts(db, r=None) -> list:
    """Rows (``_close_cloud_attempt`` shape) of the open QC second attempts of current bodies; it only reads.

    With ``r`` only that post's attempts are read, so a re-check under the
    writer lock does not scan the whole attempt history."""
    content = json_or_null("ca.content")
    narrow, params = ("", ()) if r is None else (
        " AND ca.project_id=? AND ca.message_id=?", (r["project_id"], r["message_id"]))
    rows = db.execute(
        f"""SELECT DISTINCT ca.project_id, ca.message_id, m.content_hash,
                   json_extract({content},'$.source_artifact_id') FROM artifacts ca
            JOIN messages m ON m.message_id=ca.message_id AND m.project_id=ca.project_id
            WHERE ca.kind=?{narrow} AND json_extract({content},'$.state')='open'
              AND json_extract({content},'$.hash')=m.content_hash
              AND json_type({content},'$.source_artifact_id')='integer'
              AND json_type({content},'$.revision')='integer'
              AND json_extract({content},'$.revision')={source_revision_sql()}
              AND NOT EXISTS (SELECT 1 FROM artifacts cc WHERE cc.kind=ca.kind AND cc.message_id=ca.message_id
                    AND json_extract({json_or_null('cc.content')},'$.state')='closed'
                    AND json_extract({json_or_null('cc.content')},'$.hash')=m.content_hash)""",
        (CLOUD_ATTEMPT_KIND, *params)).fetchall()
    return [{"project_id": project_id, "message_id": message_id, "content_hash": content_hash, "qc_cloud": source}
            for project_id, message_id, content_hash, source in rows if type(source) is int]


def _end_withdrawn_cloud_attempts(ledger, lock, per_write_lock: bool, *, deadline) -> int:
    """Close each open QC second attempt that has ended for good -> attempts closed.

    Every extraction pass runs this, with the cloud on or off, so an attempt
    whose provider can no longer send still ends (確認候補) when no cloud
    worker runs. Contracts are judged only under verified settings read
    without waiting (``load_config_checked``): a pass whose settings are
    unavailable or mid-update closes nothing and the next pass retries. The
    write lock is taken only when one is found, and the lock, the SQLite
    writer wait and the SQL are bounded like the stale GC's, so a busy
    database never delays selection. Once the run lock is held the settings,
    review and audits are read again and each candidate is judged just
    before its own write transaction: extraction writers are serialized by
    that lock, so a provider re-approved while waiting keeps its attempt, and
    no settings or review read happens while SQLite's writer lock is held.
    Each attempt closes in its own short transaction (only a still-open check
    and the write) right after it is judged, so a pass that runs out of time
    keeps the ones it closed and the next pass continues.
    Nothing here sends or reserves."""
    if ledger.db.in_transaction:
        return 0
    cfg = load_config_checked(wait=False)
    if cfg is None or not any(_cloud_attempt_withdrawn(ledger, cfg, r) for r in _open_cloud_attempts(ledger.db)):
        return 0
    close_by = min(deadline, time.monotonic() + _STALE_GC_BUDGET_S)
    closed = 0
    with lock(per_write_lock, deadline=close_by) as held:
        if not held or not _writer_current():
            return 0
        cfg = load_config_checked(wait=False)
        for r in [] if cfg is None else _open_cloud_attempts(ledger.db):
            # judged again under the run lock, outside any write transaction, and closed at once,
            # so the budget is spent on writes as it goes rather than on judging every candidate first
            if not _cloud_attempt_withdrawn(ledger, cfg, r):
                continue
            try:
                with _stale_gc_transaction(ledger.db, close_by):
                    # this post only, so the re-check stays small however long the history
                    ended = r in _open_cloud_attempts(ledger.db, r)
                    if ended:
                        _close_cloud_attempt_tx(ledger.db, r)
            except _StaleGCDeadline:
                break   # this one rolled back; it and the rest wait for the next pass
            closed += ended
    return closed


def _replace_current(ledger, r, content: str, ctx: bool = False,
                     integrity: dict | None = None,
                     qc_fix: dict | None = None,
                     extra_meta: dict | None = None):
    """Atomically replace this public generation while retaining other generations.

    Within this generation readers must never see two current rows for one
    body (stats scan oldest-first, notify_flush reads newest-first).
    Invalid metadata remains history without supplying current facts.

    `ctx` records whether thread context was supplied: the content hash
    covers only the body, so without the flag a context-free
    extraction is indistinguishable from a context-aware one."""
    if (ledger.db.in_transaction or not isinstance(r, dict)
            or not isinstance(r.get("_source_binding"), str)):
        return False
    hold_started = None
    try:
        with ledger.db:
            if not ledger.db.in_transaction:
                ledger.db.execute("BEGIN IMMEDIATE")
                hold_started = time.monotonic()
            witness = []
            outcome = _replace_current_tx(ledger, r, content, ctx, integrity, qc_fix, extra_meta,
                                          result_witness=witness)
            if outcome in ("held", "written"):
                if any(key not in r for key in ("project_id", "parent_id", "posted_at", "posted_at_ts")):
                    source = ledger.db.execute("SELECT * FROM messages WHERE message_id=?", (r["message_id"],)).fetchone()
                    if source is None:
                        raise RuntimeError("extract_source_changed_during_write")
                    r = {**dict(source), **r}
                extraction_refresh.mark_history_change_tx(ledger.db, r["project_id"], r["message_id"])
                _verify_atomic_witness(ledger.db, r, witness)
                if outcome == "written":
                    _verify_unique_llm_current(ledger.db, r, witness[0]['artifact_id'])
    except cloud_inference.CloudObservationIncomplete:
        return False
    except cloud_inference.CloudFallback:
        if isinstance(r, dict) and r.get("_cloud_discard"):
            r["_cloud_discard"]()
        return False
    finally:
        _report_tx_hold("replace_current_tx_hold", hold_started, r)
    if outcome != "rejected":
        project_id = r.get("project_id")
        if project_id is None:
            row = ledger.db.execute("SELECT project_id FROM messages WHERE message_id=?",
                                    (r["message_id"],)).fetchone()
            project_id = row[0] if row else None
        _rerender_cards(ledger, project_id, r["message_id"])
    return outcome == "written"


def _replace_current_tx(ledger, r, content: str, ctx: bool = False,
                     integrity: dict | None = None,
                     qc_fix: dict | None = None,
                     extra_meta: dict | None = None, *, scoped_cleanup=False, result_witness=None):
    diagnostic_row = r
    if isinstance(r, dict):
        r.pop("_commit_reject_reason", None)
    if not isinstance(r, dict) or not isinstance(r.get("_source_binding"), str):
        return _commit_rejected(diagnostic_row, "source_or_owner")
    meta = {inference.VERSION_KEY: _INFERENCE_VERSION, "hash": r["content_hash"],
            "extract_version": EXTRACT_VERSION,
            "patient_context_version": PATIENT_CONTEXT_VERSION}
    if any(key not in r.keys() for key in ("project_id", "parent_id", "posted_at", "posted_at_ts")):
        source = ledger.db.execute(
            "SELECT * FROM messages m WHERE message_id=? AND content_hash=? "
            f"AND {_unsealed_source_pred(r)}", (r["message_id"], r["content_hash"])).fetchone()
        if source is None:
            return _commit_rejected(diagnostic_row, "source_or_owner")                # fence before any missing-row context read
        r = {**dict(source), **dict(r)}
    actual_model = _actual_model(r)
    bound_context = r["_context"] if "_context" in r.keys() else _thread_context(ledger.db, r)
    meta.update(actual_model=actual_model, plan_version=PLAN_VERSION,
                context_binding=_chunk_context(r, bound_context))
    meta.update(_frame_meta(r))
    if isinstance(r, dict) and r.get("_provider_contract"):
        meta.update(r["_provider_contract"])
        if r.get("_cloud_generation"):
            meta["config_generation"] = r["_cloud_generation"]
    if ctx:
        meta["ctx"] = True
    if integrity:
        meta["integrity"] = integrity
    if qc_fix is not None:
        meta["qc_fix"] = qc_fix
    if extra_meta:
        meta.update(extra_meta)
    meta[inference.VERSION_KEY] = _INFERENCE_VERSION
    if isinstance(r, dict) and r.get('_source_binding') is not None:
        meta['source_binding'] = r['_source_binding']
    held_prior = False
    if not _source_writable(ledger.db, r):
        return _commit_rejected(diagnostic_row, "source_or_owner")
    if isinstance(r, dict) and r.get("_cloud_guard"):
        try:
            r["_cloud_guard"]()
        except cloud_inference.CloudFallback:
            raise
    source = ledger.db.execute(
        "SELECT m.* FROM messages m WHERE message_id=? AND content_hash=? AND project_id=? "
        "AND (body_state IS NULL OR body_state='full') "
        f"AND {_unsealed_source_pred(r)}",
        (r["message_id"], r["content_hash"], r["project_id"])).fetchone()
    if source is None:
        return _commit_rejected(diagnostic_row, "source_or_owner")
    if "_input_context" in r and _thread_context(ledger.db, r,
            budget=r.get("_input_context_budget", _CTX_TOTAL_MAX)) != r["_input_context"]:
        return _commit_rejected(diagnostic_row, "context")
    frame = r.get("_frame")
    if frame is not None:
        current_frame = extraction_context.load(ledger.db, frame.get("artifact_id"), dict(source))
        if current_frame is None or current_frame["digest"] != frame.get("digest"):
            return _commit_rejected(diagnostic_row, "context")
        if not _frame_references_current(ledger.db, dict(source), current_frame):
            return _commit_rejected(diagnostic_row, "reference")
        if not _medication_reference_current(ledger.db, r):
            return _commit_rejected(diagnostic_row, "medication")
    meta['source_binding'] = r['_source_binding']
    prior = _prior_extract(ledger.db, r)
    human = extra_meta.get("human_fix") if isinstance(extra_meta, dict) else None
    old = loads_dict(prior["content"]) if prior else None
    lost = extract_quality.protected_lost(old, loads_dict(content), source["body_text"]) if old else {}
    prior_meta = loads_dict(prior["meta"]) if prior else None
    human_failed = isinstance(human, dict) and human.get("applied") is False
    qc_failed = isinstance(qc_fix, dict) and qc_fix.get("applied") is False
    old_carry = (prior_meta and (prior_meta.get("extract_version") != EXTRACT_VERSION or not inference.is_current(prior_meta, target=_INFERENCE_VERSION))
                 and (human_failed or qc_failed))
    if old_carry or (lost and not (isinstance(human, dict) and human.get("applied") is True)):
        reason = ("human_failed" if human_failed else "qc_failed") if old_carry else "protected_loss"
        # Fixed codes and counts only: no text, label or quote enters the receipt.
        # shortcut: "unlocated" uses the root chunk layout, not length resplits; read the saved splits if they matter.
        size = r.get("_chunk_size")
        lost_class = extract_quality.protected_loss_classes(
            old, loads_dict(content), source["body_text"],
            pieces=plan_chunks(source["body_text"], size if type(size) is int and size > 0 else _CHUNK_SIZE)) if lost else None
        hold_witness = _record_hold_tx(ledger, r, prior, reason,
                        lost=lost, lost_class=lost_class, feedback_id=human.get("feedback_id") if human else None,
                        remove_chunks=not scoped_cleanup)
        held_prior = True
    else:
        retaining_prior = bool(human_failed or qc_failed or r.get("_kept_prior"))
        if retaining_prior and prior is not None and prior_meta:
            for key in ("actual_model", "plan_version", "context_binding", "context_snapshot",
                        "context_digest", "patient_state_context", "ctx", "patient_context_version", "integrity",
                        "backend", "model", "prompt", "config_generation"):
                if key in prior_meta:
                    meta[key] = prior_meta[key]
                else:
                    meta.pop(key, None)
            actual_model = prior["model"]
            retained_document = _provider_strict_dict(content)
            retained_proof = retained_document.get(_INTERNAL_REPAIR_PROOF) if retained_document is not None else None
            marked = "internal_proof_version" in prior_meta or "internal_proof_hash" in prior_meta
            if marked or retained_proof is not None:
                retained = {**r, "_target": ("", prior["model"]), "_provider_contract":
                    {key: prior_meta.get(key) for key in ("backend", "model", "prompt")}}
                retained.pop("_frame", None)
                retained.pop("qc_src", None)
                if "context_snapshot" in prior_meta:
                    retained_frame = extraction_context.load(ledger.db, prior_meta["context_snapshot"], dict(source))
                    if retained_frame is None:
                        return _commit_rejected(diagnostic_row, "retained_proof")
                    retained["_frame"] = retained_frame
                    retained_context = retained_frame["text"]
                else:
                    retained_context = _thread_context(ledger.db, retained)
                if (prior["content"] != content or (type(prior_meta.get("internal_proof_version")) is not int or prior_meta["internal_proof_version"] != 1)
                        or not isinstance(retained_proof, dict)
                        or not mcs_util.valid_hash(prior_meta.get("internal_proof_hash"))
                        or prior_meta.get("internal_proof_hash") != _internal_proof_hash(retained_proof)
                        or prior_meta.get("context_binding") != _chunk_context(retained, retained_context)
                        or not _verify_inline_extract_proof(ledger.db, retained, retained_context,
                            {key: value for key, value in retained_document.items()
                             if key not in (_INTERNAL_REPAIR_PROOF, _INTERNAL_LEAF_CLAIMS)},
                            retained_proof)):
                    return _commit_rejected(diagnostic_row, "retained_proof")
                meta["internal_proof_version"] = 1
                meta["internal_proof_hash"] = prior_meta["internal_proof_hash"]
        inline_proof = r.get("_inline_extract_proof") if not retaining_prior else None
        if inline_proof is not None:
            clinical = _provider_strict_dict(content)
            if (clinical is None or _INTERNAL_REPAIR_PROOF in clinical
                    or not _verify_inline_extract_proof(ledger.db, r, bound_context, clinical, inline_proof)):
                return _commit_rejected(diagnostic_row, "result_seal")
            clinical[_INTERNAL_REPAIR_PROOF] = inline_proof
            content = json.dumps(clinical, ensure_ascii=False)
            meta["internal_proof_version"] = 1
            meta["internal_proof_hash"] = _internal_proof_hash(inline_proof)
            if meta["internal_proof_hash"] is None:
                return _commit_rejected(diagnostic_row, "result_seal")
        claims = meta["integrity"].get("completeness") if isinstance(meta.get("integrity"), dict) else None
        if isinstance(claims, dict):
            # Persist the verified leaf claim inside freshly written content:
            # the claim travels with every retained/replayed result, so a
            # meta-only edit cannot mint resolved over stored execution
            # history. A retained prior is never re-sealed — rewriting its
            # content would void the stored provider evidence.
            clinical = _provider_strict_dict(content)
            if clinical is None:
                return _commit_rejected(diagnostic_row, "result_seal")
            existing = clinical.get(_INTERNAL_LEAF_CLAIMS)
            if existing is not None:
                if existing != claims:
                    return _commit_rejected(diagnostic_row, "result_seal")
            elif not retaining_prior:
                clinical[_INTERNAL_LEAF_CLAIMS] = claims
                content = json.dumps(clinical, ensure_ascii=False)
        proof = (_prior_provider_evidence(prior, content, r["_source_binding"])
                 if retaining_prior else r.get("_provider_evidence"))
        if (not isinstance(proof, dict) or type(proof.get("complete")) is not bool
                or not isinstance(proof.get("providers"), list)
                or any(name not in provider_registry.INFERENCE_PROVIDERS for name in proof["providers"])
                or (proof["complete"] and not proof["providers"])):
            proof = _provider_evidence()
        contract = ({key: meta.get(key) for key in ("backend", "model", "prompt")}
                    if any(key in meta for key in ("backend", "model", "prompt")) else None)
        if not _evidence_matches_route(proof, actual_model, contract):
            proof = _provider_evidence()
        digest = _provider_result_hash(content)
        if digest is not None:
            meta["provider_provenance"] = {
                "version": 1, "stage": "extract", **_provider_evidence(proof["providers"], proof["complete"]),
                "result_hash": digest, "source_binding": r["_source_binding"]}
        else:
            meta.pop("provider_provenance", None)
        if prior_meta and (prior_meta.get("extract_version") != EXTRACT_VERSION or not inference.is_current(prior_meta, target=_INFERENCE_VERSION)):
            meta["upgrade"] = {"prior_artifact_id": prior["artifact_id"],
                "prior_version": prior_meta.get("extract_version"),
                "facts_lost": len(extract_quality.facts(extract_validation.validate(old, source["body_text"]) or {})
                                  - extract_quality.facts(extract_validation.validate(loads_dict(content) or {}, source["body_text"]) or {}))}
        previous = ledger.db.execute(
            "SELECT a.content,a.meta,a.model FROM artifacts a JOIN messages m "
            "ON m.message_id=a.message_id WHERE a.kind=? AND a.message_id=? "
            "AND a.project_id=? " + current_extract_pred()
            + " ORDER BY a.artifact_id DESC LIMIT 1",
            (KIND, r["message_id"], r["project_id"])).fetchone()
        current_signature = mcs_queries.extraction_input_signature(content, meta, actual_model)
        try:
            previous_signature = mcs_queries.extraction_input_signature(
                previous["content"], previous["meta"], previous["model"]) if previous else None
        except ValueError:
            # Unreadable stored history cannot veto a validated replacement.
            # Its unknown meaning still invalidates derived analysis proofs.
            previous_signature = None
        changed = previous_signature != current_signature
        # Keep the INSERT fence even though the prior check owns the write lock.
        meta_json = json.dumps(meta)
        created_at = time.time()
        cur = ledger.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
            "SELECT ?,project_id,message_id,?,?,?,? FROM messages m "
            "WHERE message_id=? AND content_hash=? AND project_id=? "
            "AND (body_state IS NULL OR body_state='full') "
            f"AND {_unsealed_source_pred(r)}",
            (KIND, content, actual_model, meta_json, created_at,
             r["message_id"], r["content_hash"], r["project_id"]))
        if not cur.rowcount:
            return _commit_rejected(diagnostic_row, "insert_fence")
        # A trigger can change the occurrence even under our writer reservation.
        # Returning normally here would commit the newly inserted stale result.
        if not _source_writable(ledger.db, r):
            raise RuntimeError("extract_source_changed_during_write")
        _verify_created_artifact_tx(ledger.db, cur.lastrowid, kind=KIND,
                                    project_id=r["project_id"], message_id=r["message_id"],
                                    content=content, model=actual_model, meta=meta_json, created_at=created_at)
        import inference_dependency_loss as dependency_loss
        replaced_ids = [row[0] for row in ledger.db.execute(
            "SELECT artifact_id FROM artifacts WHERE kind=? AND message_id=? "
            "AND (project_id IS NULL OR project_id=?) AND "
            + inference.current_meta_pred("meta", target=_INFERENCE_VERSION) + " AND artifact_id != ?",
            (KIND, r['message_id'], r['project_id'], cur.lastrowid))]
        footprint = dependency_loss.capture_tx(ledger.db, r['project_id'], r['message_id'], artifact_ids=replaced_ids)
        dependency_loss.record_tx(ledger.db, footprint, replacement_id=cur.lastrowid)
        ledger.db.execute(
            "DELETE FROM artifacts WHERE kind=? AND message_id=? "
            "AND (project_id IS NULL OR project_id=?) AND "
            + inference.current_meta_pred("meta", target=_INFERENCE_VERSION) + " AND artifact_id != ?",
            (KIND, r["message_id"], r["project_id"], cur.lastrowid))
        if not _source_writable(ledger.db, r):
            raise RuntimeError("extract_source_changed_during_write")
        if changed:
            ledger.bump_inference_analysis_epoch_tx(r["project_id"])
        if isinstance(r, dict) and r.get("_cloud_mode") == "dry_run":
            selection = r.get("_cloud_selection")
            if selection is None:
                selection, errors = cloud_policy.resolve_profile(
                    r.get("_cloud_config") or load_config(), route="reextract",
                    purpose="backlog", root=HOME, ledger=ledger, ordered=True)
                if selection is None:
                    raise cloud_inference.CloudFallback("cloud_profile_invalid")
            cloud_policy.dryrun_tx(ledger, route="reextract", project_id=r["project_id"],
                unit_key=str(r["message_id"]), input_chars=len(r["body_text"] or ""),
                max_tokens=selection["max_tokens"] or MAX_TOKENS, model=selection["model"])
        if isinstance(r, dict) and r.get("_cloud_mode") == "shadow":
            # the comparison unit commits (pending) with the local result or not at all
            r["_shadow_unit"] = os.urandom(16).hex()
            _cloud_compare_tx(ledger, route="reextract", project_id=r["project_id"],
                left=set(_comparison_extract(r, loads_dict(content))), right=None,
                rejected=None, held=None, repairs=None, unit=r["_shadow_unit"], pending=True)
        if not _source_writable(ledger.db, r):
            raise RuntimeError("extract_source_changed_during_write")
        _verify_created_artifact_tx(ledger.db, cur.lastrowid, kind=KIND,
                                    project_id=r["project_id"], message_id=r["message_id"],
                                    content=content, model=actual_model, meta=meta_json, created_at=created_at)
        _verify_unique_llm_current(ledger.db, r, cur.lastrowid)
        if result_witness is not None:
            result_witness.append({'artifact_id': cur.lastrowid, 'kind': KIND,
                'project_id': r['project_id'], 'message_id': r['message_id'],
                'content': content, 'model': actual_model, 'meta': meta_json, 'created_at': created_at})
    if held_prior and result_witness is not None:
        result_witness.extend(hold_witness)
        _verify_atomic_witness(ledger.db, r, result_witness)
    return "held" if held_prior else "written"


def _commit_extraction_atomic(ledger, r, content, context, integrity, qc_fix, extra_meta,
                              used_keys, *, check_current, mark_context=None, expected_prior=None,
                              adoption_guard=None, before_replace_tx=None):
    if isinstance(r, dict):
        r.pop("_commit_reject_reason", None)
        r.pop("_completeness_reject_reason", None)
    if ledger.db.in_transaction:
        return _commit_rejected(r, "transaction")
    r.pop("_inline_extract_proof", None)
    hold_started = None
    try:
        if adoption_guard is not None and hasattr(adoption_guard, "prepare"):
            # Off-transaction evaluation: the guard's verdict and artifact
            # fence are captured before BEGIN IMMEDIATE so the in-transaction
            # calls only re-prove the fence instead of rescanning accounting.
            adoption_guard.prepare()
        with ledger.db:
            ledger.db.execute("BEGIN IMMEDIATE")
            hold_started = time.monotonic()
            if r.get("_checkpoint_conflict"):
                return _commit_rejected(r, "checkpoint")
            if (not _source_writable(ledger.db, r)
                    or not _owns_current_source(ledger, r, r.get("_current_lease"))):
                return _commit_rejected(r, "source_or_owner")
            if adoption_guard is not None and not adoption_guard():
                # The migrated row's financial boundary re-verification ran
                # inside this write transaction: a hold rejects the adoption
                # and the row stays pending for the next guarded attempt.
                return _commit_rejected(r, "superseded_guard")
            expected = r.get("_checkpoint_identity")
            if expected is None:
                return _commit_rejected(r, "checkpoint")
            state = _checkpoint_identity(ledger.db, r, context, r["_chunk_size"])
            if not _checkpoint_state_matches(r, expected, used_keys, state):
                return _commit_rejected(r, "checkpoint")
            if check_current:
                required = r.get("_required_cloud_contract")
                already_current = (cloud_completion_any(ledger.db, r)[0] is not None if required is not None
                                   else _current(ledger, r["message_id"], r["content_hash"]))
                if already_current:
                    return "current"
            pinned_prior = r.get("_expected_prior")
            if ((pinned_prior is not None and expected_prior is not None and pinned_prior != expected_prior)
                    or not _prior_matches(ledger.db, r, pinned_prior if expected_prior is None else expected_prior)):
                return _commit_rejected(r, "prior_changed")
            if not _protected_prior_current(ledger.db, r):
                return _commit_rejected(r, "prior_changed")
            _, _, current = state
            # The existing checkpoint cleanup is safe only after every adopted
            # repaired leaf has been copied into the protected final artifact.
            repaired = False
            for key in (() if r.get("_kept_prior") else
                        {part for key in used_keys for part in _checkpoint_keys(key)}):
                identity = current.get(key)
                payload = _provider_strict_dict(identity[2]) if identity is not None else None
                repaired = repaired or isinstance(payload, dict) and "__quality_repair_v1" in payload
            if repaired:
                try:
                    inline = _extract_inline_proof(ledger.db, r, context, used_keys)
                except (ValueError, TypeError, KeyError, sqlite3.Error):
                    return _commit_rejected(r, "quality_proof")
                clinical = _provider_strict_dict(content)
                if clinical is None or not _verify_inline_extract_proof(ledger.db, r, context, clinical, inline):
                    return _commit_rejected(r, "quality_proof")
                r["_inline_extract_proof"] = inline
            if (not r.get("_kept_prior") and isinstance(integrity, dict)
                    and ("completeness" in integrity or "completeness_chunks" in integrity)
                    and not _verify_completeness_claim(
                        ledger.db, r, context, content, integrity, used_keys)):
                # The stored completeness claim must reproduce from the
                # adopted leaves' content — run-time metadata alone cannot
                # mint a resolved result over unresolved leaves.
                return _commit_rejected(r, "completeness")
            if before_replace_tx is not None:
                # Auxiliary SQL writes (e.g. rule coverage) commit with the adoption, never alone.
                before_replace_tx()
                if (not _source_writable(ledger.db, r)
                        or not _owns_current_source(ledger, r, r.get("_current_lease"))
                        or not _prior_matches(ledger.db, r, pinned_prior if expected_prior is None else expected_prior)
                        or not _protected_prior_current(ledger.db, r)
                        or not _checkpoint_state_matches(r, expected, used_keys,
                            _checkpoint_identity(ledger.db, r, context, r["_chunk_size"]))
                        or adoption_guard is not None and not adoption_guard()):
                    raise _CheckpointChanged()
            witness = []
            outcome = _replace_current_tx(ledger, r, content,
                                          ctx=context is not None if mark_context is None else mark_context,
                                          integrity=integrity, qc_fix=qc_fix,
                                          extra_meta=extra_meta, scoped_cleanup=True, result_witness=witness)
            if before_replace_tx is not None:
                if outcome not in ("held", "written"):
                    ledger.db.rollback()
                    return outcome
                if (not _source_writable(ledger.db, r)
                        or not _owns_current_source(ledger, r, r.get("_current_lease"))
                        or not _checkpoint_state_matches(r, expected, used_keys,
                            _checkpoint_identity(ledger.db, r, context, r["_chunk_size"]))
                        or adoption_guard is not None and not adoption_guard()):
                    raise _CheckpointChanged()
            if outcome == "held":
                if current != r["_checkpoint_identity"]:
                    raise _CheckpointChanged()
                _delete_checkpoint_keys_tx(ledger.db, r, context, current.keys())
                _verify_atomic_witness(ledger.db, r, witness)
            elif outcome == "written":
                _delete_checkpoint_keys_tx(ledger.db, r, context, used_keys)
                _verify_atomic_witness(ledger.db, r, witness)
                if repaired:
                    stored = _provider_strict_dict(witness[0]["content"])
                    stored_proof = stored.pop(_INTERNAL_REPAIR_PROOF, None) if isinstance(stored, dict) else None
                    stored_claims = stored.pop(_INTERNAL_LEAF_CLAIMS, None) if isinstance(stored, dict) else None
                    if (not isinstance(stored, dict) or stored_proof != inline
                            or (isinstance(integrity, dict) and integrity.get("completeness") is not None
                                and stored_claims != integrity["completeness"])
                            or not _verify_inline_extract_proof(ledger.db, r, context, stored, inline)):
                        raise RuntimeError("extract_proof_readback_failed")
                _verify_unique_llm_current(ledger.db, r, witness[0]['artifact_id'])
            if outcome in ("held", "written"):
                extraction_refresh.mark_history_change_tx(ledger.db, r["project_id"], r["message_id"])
                _verify_atomic_witness(ledger.db, r, witness)
                if outcome == "written":
                    _verify_unique_llm_current(ledger.db, r, witness[0]['artifact_id'])
    except _CheckpointChanged:
        return _commit_rejected(r, "checkpoint")
    except cloud_inference.CloudObservationIncomplete:
        return _commit_rejected(r, "cloud_guard")
    except cloud_inference.CloudFallback:
        if r.get("_cloud_discard"):
            r["_cloud_discard"]()
        return _commit_rejected(r, "cloud_guard")
    finally:
        _report_tx_hold("extract_commit_tx_hold", hold_started, r, adoption_guard)
    if outcome != "rejected":
        _rerender_cards(ledger, r["project_id"], r["message_id"])
    return outcome


def _persist_chunks(ledger, r, chunks_out: dict, context: str | None = None, *,
                    chunk_size: int | None = None, splits=None, split_size=None,
                    replay=False, lease=None, guard=None):
    """Durable root/leaf checkpoints and local split plans, written by the caller."""
    if len(chunks_out) != 1:
        return False
    if "_source_binding" not in r.keys() or not isinstance(r["_source_binding"], str) or not r["_source_binding"]:
        return False
    if not replay and ledger.db.in_transaction:
        return False
    if not replay and (r.get("_resume_refused") or r.get("_checkpoint_conflict")):
        # A sticky resume-admission or checkpoint conflict refuses every
        # later checkpoint write, split and compensation from this job.
        return False
    chunk_size = _CHUNK_SIZE if chunk_size is None else chunk_size
    inserted_witness = None
    with _checkpoint_write(ledger.db, replay=replay):
        if not ledger.db.in_transaction:
            ledger.db.execute("BEGIN IMMEDIATE")
        protected_current = _protected_prior_current(ledger.db, r)
        if (not _source_writable(ledger.db, r)
                or not _prior_matches(ledger.db, r, r.get("_expected_prior"))
                or not protected_current):
            if (guard is not None and guard is r.get("_checkpoint_guard")
                    and not protected_current):
                r["_checkpoint_conflict"] = True
            return False
        if (replay and lease is None) or (lease is not None and not _owns_current_source(ledger, r, lease)):
            return False
        rebuild = r.get("_local_rebuild")
        if rebuild is not None:
            # The first and every later checkpoint of an approved local rebuild re-prove the
            # permit inside this writer transaction, so a new reservation stops it here too.
            import extract_llm_local_rebuild as local_rebuild
            if local_rebuild.permit_for(ledger, rebuild) not in local_rebuild.RUNNABLE:
                return False
        for key, value in chunks_out.items():
            guarded = guard is not None and guard is r.get("_checkpoint_guard")
            if guarded:
                snapshot = r.get("_checkpoint_replay_snapshot")
                if (not isinstance(snapshot, dict) or chunk_size != r["_chunk_size"]
                        or snapshot["context"] != _chunk_context(r, context, checkpoint=True)):
                    r["_checkpoint_conflict"] = True
                    return False
            if guarded and not _checkpoint_matches(ledger.db, r, context, [key]):
                r["_checkpoint_conflict"] = True
                return False
            if replay:
                # Use the reader's own selection rules; an unrelated newer
                # chunk is not evidence that this key changed.
                if not (type(key) is int and key >= 0 or type(key) is str and
                        re.fullmatch(r"(?:0|[1-9][0-9]*)(?::(?:0|[1-9][0-9]*))+", key)):
                    return False
                path = str(key).split(":")
                parents = [int(path[0]) if depth == 1 else ":".join(path[:depth])
                           for depth in range(1, len(path))]
                snapshot = r.get("_checkpoint_replay_snapshot") if guarded else None
                fast = (isinstance(snapshot, dict) and snapshot["size"] == chunk_size
                        and snapshot["layout"] == r.get("_checkpoint_layout")
                        and snapshot["body"] == r["body_text"]
                        and snapshot["context"] == _chunk_context(r, context, checkpoint=True)
                        and snapshot["refs"] == _frame_refs(r)
                        and snapshot["saved"].get(key) == value
                        and all(snapshot["splits"].get(parent) == (splits or {}).get(parent)
                                for parent in parents)
                        and all(snapshot["identities"].get(part) == r["_checkpoint_identity"].get(part)
                                for part in _checkpoint_keys(key)))
                if not fast:
                    current_splits, current_saved = _saved_chunk_state(ledger.db, r, context, chunk_size=chunk_size)
                    if (current_saved.get(key) != value or
                            any((splits or {}).get(parent) != current_splits.get(parent) for parent in parents)):
                        return False
                continue
            piece = _chunk_piece(r["body_text"], chunk_size, key, splits or {})
            if piece is None:
                raise ValueError("extract_checkpoint_path_invalid")
            meta = {inference.VERSION_KEY: _INFERENCE_VERSION, "hash": r["content_hash"], "ver": EXTRACT_VERSION, "chunk": key,
                    "context": _chunk_context(r, context, checkpoint=True), "chunk_size": chunk_size,
                    "piece_sha256": hashlib.sha256(piece.encode()).hexdigest(),
                    "actual_model": _actual_model(r),
                    "plan_version": PLAN_VERSION}
            if rebuild is not None:
                meta["local_rebuild"] = local_rebuild.tag(rebuild)
            if split_size is not None:
                meta["split_size"] = split_size
                meta["checkpoint_provider_contract"] = r.get("_provider_contract")
                meta.update(_frame_meta(r))
            prior_ref = r.get("_protected_prior_ref")
            if isinstance(prior_ref, dict):
                meta["protected_prior_ref"] = prior_ref
            evidence = _leaf_evidence(value)
            quality_status = value.get("_quality_status")
            quality_origin = getattr(value, "quality_origin", None)
            quality_origin_id = getattr(value, "quality_origin_id", None)
            quality_prior_kind = getattr(value, "quality_prior_kind", None)
            quality_repair = (getattr(split_size, "repair_proof", None) if split_size is not None
                              else getattr(value, "repair_proof", None))
            if quality_status in ("pending", "started"):
                meta.update(quality_pass=extract_quality._QUALITY_CHECKPOINT, quality_status=quality_status)
                value = {key: item for key, item in value.items() if key != "_quality_status"}
            if isinstance(value.get("_quality_result"), dict):
                quality = value["_quality_result"]
                meta.update(quality_pass=extract_quality._QUALITY_CHECKPOINT, quality_status=quality["state"],
                            quality_remaining=quality["remaining_hint_candidates"])
                value = {key: item for key, item in value.items() if key != "_quality_result"}
            value = {key: item for key, item in value.items() if key != "_provider_leaf"}
            if quality_repair is not None:
                if not _replay_repair_leaf(ledger.db, r, context, key, chunk_size, splits or {}, quality_repair,
                                           None if split_size is not None else value,
                                           finish_reason="length" if split_size is not None else "stop"):
                    return False
                value["__quality_repair_v1"] = quality_repair
            if quality_status in ("pending", "started") and quality_origin_id is not None:
                value["__quality_origin_ref_v1"] = quality_origin_id
            elif quality_status == "pending" and quality_origin is not None:
                value["__quality_origin_v1"] = quality_origin
            if quality_status == "started" and quality_origin_id is not None:
                if (quality_prior_kind not in ("raw", "cached") or type(lease) not in (int, float)
                        or not math.isfinite(lease) or lease <= 0):
                    raise ValueError("extract_repair_origin_invalid")
                value["__quality_prior_kind_v1"] = quality_prior_kind
                meta["quality_lease"] = lease
            content = json.dumps(value, ensure_ascii=False)
            if split_size is None:
                proof = _leaf_record(meta, r, evidence, content)
                if proof is not None:
                    meta["provider_leaf"] = proof
            meta_json = json.dumps(meta, ensure_ascii=False)
            if (split_size is None and (
                    quality_status is None and meta.get("quality_status") == "hint_gaps_resolved"
                    or quality_status == "pending" and meta.get("quality_status") == "pending"
                    and lease is not None and guarded)):
                # Only the newest row for this message can prove that the
                # reader will see the same checkpoint. Do not look past a
                # later row, including an invalid or differently scoped one.
                latest = ledger.db.execute(
                    "SELECT project_id,content,model,meta FROM artifacts "
                    "WHERE kind=? AND message_id=? ORDER BY artifact_id DESC LIMIT 1",
                    ("extract_llm_chunk", r["message_id"])).fetchone()
                if (latest is not None and latest["project_id"] == r["project_id"]
                        and latest["model"] == meta["actual_model"] and latest["content"] == content
                        and latest["meta"] == meta_json):
                    continue
            inserted_id = ledger.artifact_add_tx("extract_llm_chunk", content,
                                                project_id=r["project_id"], message_id=r["message_id"],
                                                model=meta["actual_model"], meta=meta)
            if ledger.db.execute("SELECT changes()").fetchone()[0] != 1:
                ledger.db.rollback()
                if guarded:
                    r["_checkpoint_conflict"] = True
                return False
            inserted_row = ledger.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (inserted_id,)).fetchone()
            if (inserted_row is None or inserted_row['kind'] != 'extract_llm_chunk'
                    or inserted_row['project_id'] != r['project_id']
                    or inserted_row['message_id'] != r['message_id']
                    or inserted_row['content'] != content or inserted_row['model'] != meta['actual_model']
                    or inserted_row['meta'] != meta_json):
                raise RuntimeError('extract_insert_unverified')
            inserted_witness = dict(inserted_row)
            if guarded:
                latest = ledger.db.execute(
                    "SELECT artifact_id,project_id,content,model,meta FROM artifacts "
                    "WHERE kind='extract_llm_chunk' AND message_id=? ORDER BY artifact_id DESC LIMIT 1",
                    (r["message_id"],)).fetchone()
                inserted = (inserted_id, r["project_id"], content, meta["actual_model"],
                            meta_json)
                if latest is not None and tuple(latest) == inserted:
                    r["_checkpoint_identity"][key] = inserted
                else:
                    _, _, identities = _checkpoint_identity(ledger.db, r, context, chunk_size)
                    if identities.get(key) != inserted:
                        ledger.db.rollback()
                        r["_checkpoint_conflict"] = True
                        return False
                    r["_checkpoint_identity"][key] = inserted
        if inserted_witness is not None:
            extraction_refresh.mark_history_change_tx(ledger.db, r['project_id'], r['message_id'])
            _verify_atomic_witness(ledger.db, r, [inserted_witness])
            if lease is not None and not _owns_current_source(ledger, r, lease):
                raise RuntimeError('extract_source_changed_during_write')
    _rerender_cards(ledger, r["project_id"], r["message_id"])
    return True
