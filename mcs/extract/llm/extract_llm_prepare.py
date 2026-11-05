"""Prepare eligible source rows and frozen contexts before inference."""
import contextlib
import copy
import json
import os
import sqlite3
import time
import inference_version as inference
import cloud_policy
import cloud_inference
from clinical_chunking import plan_chunks
import inference_dependency_loss
import extraction_context
import extraction_medications
from mcs_queries import (current_extract_pred,
                         json_or_null)
from mcs_util import HOME, load_config, loads_dict

from extract_llm_context import (
    _context_contract,
    _context_contracts,
    _medication_reference_current,
    _pending_context_frame,
    _thread_context,
)
from extract_llm_prompts import (
    _CTX_TOTAL_MAX,
    _hint_coverage,
)
from extract_llm_proofs import (
    _saved_chunk_size,
    _saved_chunk_state,
)
from extract_llm_provider import (
    _cloud_mode,
    _cloud_reextract,
)
from extract_llm_qc import (
    _qc_feedback,
    _verify_created_artifact_tx,
    pending_pred,
)
from extract_llm_selection import (
    _LEFT_ESTIMATE,
    _LEFT_ESTIMATE_S,
    _left_estimate_key,
)
from extract_llm_source_contract import (
    EXTRACT_VERSION,
    KIND,
    _CHUNK_SIZE,
    _GREETING_RE,
    _INFERENCE_VERSION,
    _PREFILTER_CONTENT,
    _rule_hints,
    _source_writable,
)
from extract_llm_store import (
    _replace_current,
)

def _prefilter_enabled() -> bool:
    """config.json extract.prefilter, else the legacy MCS_EXTRACT_PREFILTER, else on."""
    ext = load_config().get("extract")
    if isinstance(ext, dict) and type(ext.get("prefilter")) is bool:
        return ext["prefilter"]
    return os.environ.get("MCS_EXTRACT_PREFILTER", "on") \
        not in ("0", "off", "false")


def _low_signal(body: str, hints: dict | None) -> bool:
    """Skip only a standalone greeting with no rule findings or failed hint parse."""
    if hints is None or len(hints) > 1:
        return False
    return isinstance(body, str) and _GREETING_RE.fullmatch(body) is not None


def _mark_prefiltered(ledger, r) -> bool:
    """Settle a no-signal row without an LLM call — same guarded write
    path as _replace_current (v4 fence, hash gate, supersede sweep)."""
    hints = _rule_hints(r)
    if not _low_signal(r["body_text"], hints):
        return False
    record = {**_hint_coverage(loads_dict(_PREFILTER_CONTENT), hints, r["body_text"]),
              "checked_hint_candidates": 0}
    return _replace_current(
        ledger, r, _PREFILTER_CONTENT,
        integrity={"validation": {"items_dropped": 0, "evidence_dropped": 0},
                   "completeness_chunks": 1, "completeness": {"0": record}},
        extra_meta={"prefilter": "no_signal"})


@contextlib.contextmanager
def _v1_write_transaction(db):
    """Reserve a writer transaction, or borrow an existing one via savepoint."""
    if not db.in_transaction:
        with db:
            db.execute("BEGIN IMMEDIATE")
            yield
        return
    db.execute("SAVEPOINT extract_v1_write")
    try:
        yield
    except BaseException:
        try:
            db.execute("ROLLBACK TO extract_v1_write")
            db.execute("RELEASE extract_v1_write")
        except sqlite3.Error:
            pass  # SQLite may already have rolled back the transaction.
        raise
    else:
        db.execute("RELEASE extract_v1_write")


def _ensure_v1(ledger, r, hints) -> None:
    """extract_v1 artifact for this body, written inside the v3 pass —
    v1+v2 work happens simultaneously with v3, keeping instant-analysis
    fields (med_periods etc.) covered even on paths that bypass the
    tick. A current artifact (same hash + rule_version) is left alone;
    a stale one is atomically replaced."""
    if hints is None:
        return
    import extract
    with _v1_write_transaction(ledger.db):
        if not _source_writable(ledger.db, r):
            return
        # Distinguish a real existing current artifact from an INSERT ignored
        # by a trigger; an ignored replacement must never authorize deletion.
        existing = ledger.db.execute(
            "SELECT 1 FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
            "WHERE a.kind='extract_v1' AND m.message_id=? AND m.project_id=? "
            + current_extract_pred() +
            f" AND json_extract({json_or_null('a.meta')},'$.rule_version')=? LIMIT 1",
            (r["message_id"], r["project_id"], extract.RULE_VERSION)).fetchone()
        if existing:
            return
        intervention = extract.intervention_source_payload_tx(ledger.db, r, r["_source_binding"])
        saved_hints = {**hints, "intervention_schedule": intervention} if intervention is not None else hints
        saved_meta = {inference.VERSION_KEY: _INFERENCE_VERSION, "hash": r["content_hash"],
                      "rule_version": extract.RULE_VERSION, "source_binding": r["_source_binding"]}
        if intervention is None:
            saved_meta["intervention_zero"] = 1
        content_json = json.dumps(saved_hints, ensure_ascii=False)
        meta_json = json.dumps(saved_meta, ensure_ascii=False)
        created_at = time.time()
        cur = ledger.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
            "SELECT 'extract_v1',m.project_id,m.message_id,?,'rules-v1',?,? "
            "FROM messages m WHERE m.message_id=? AND m.content_hash=? AND m.project_id=? "
            "AND (m.body_state IS NULL OR m.body_state='full') "
            "AND NOT EXISTS (SELECT 1 FROM artifacts a "
            "WHERE a.kind='extract_v1' AND a.message_id=m.message_id "
            f"{current_extract_pred()} "
            f"AND json_extract({json_or_null('a.meta')},'$.rule_version')=?)",
            (content_json, meta_json, created_at,
             r["message_id"], r["content_hash"], r["project_id"], extract.RULE_VERSION))
        extract.verify_v1_insert_tx(ledger.db, r, cur.lastrowid, cur.rowcount, content_json, meta_json)
        if not _source_writable(ledger.db, r):
            raise RuntimeError("extract_source_changed_during_write")
        _verify_created_artifact_tx(ledger.db, cur.lastrowid, kind='extract_v1',
                                    project_id=r['project_id'], message_id=r['message_id'],
                                    content=content_json, model='rules-v1', meta=meta_json, created_at=created_at)
        if cur.rowcount:
            old_ids = [row[0] for row in ledger.db.execute(
                "SELECT artifact_id FROM artifacts WHERE kind='extract_v1' AND message_id=? "
                "AND (project_id IS NULL OR project_id=?) AND "
                + inference.current_meta_pred("meta", target=_INFERENCE_VERSION) + " AND artifact_id!=?",
                (r["message_id"], r["project_id"], cur.lastrowid))]
            footprint = inference_dependency_loss.capture_tx(
                ledger.db, r["project_id"], r["message_id"], artifact_ids=old_ids)
            inference_dependency_loss.record_tx(ledger.db, footprint, replacement_id=cur.lastrowid)
            ledger.db.execute(
                "DELETE FROM artifacts WHERE kind='extract_v1' "
                "AND message_id=? AND (project_id IS NULL OR project_id=?) AND "
                + inference.current_meta_pred("meta", target=_INFERENCE_VERSION) + " AND artifact_id!=?",
                (r["message_id"], r["project_id"], cur.lastrowid))
            ledger.bump_inference_analysis_epoch_tx(r["project_id"])
            if not _source_writable(ledger.db, r):
                raise RuntimeError("extract_source_changed_during_write")
            _verify_created_artifact_tx(ledger.db, cur.lastrowid, kind='extract_v1',
                                        project_id=r['project_id'], message_id=r['message_id'],
                                        content=content_json, model='rules-v1', meta=meta_json, created_at=created_at)


_PREPARE_CONTEXT_REASONS = {
    ("load", "pending_context_unverifiable"): "prepare_context_load_unverifiable",
    ("load", "pending_context_reference_changed"): "prepare_context_load_reference_changed",
    ("medication", "medication_reference_input_invalid"): "prepare_context_medication_input_invalid",
    ("medication", "medication_reference_snapshot_invalid"): "prepare_context_medication_snapshot_invalid",
    ("prepare", "context_transaction_owned"): "prepare_context_prepare_transaction_owned",
    ("prepare", "invalid_context_options"): "prepare_context_prepare_options_invalid",
    ("prepare", "context_budget_exceeded"): "prepare_context_prepare_budget_exceeded",
    ("prepare", "context_medication_hints_invalid"): "prepare_context_prepare_medication_invalid",
    ("prepare", "context_source_changed"): "prepare_context_prepare_source_changed",
    ("prepare", "context_snapshot_invalid"): "prepare_context_prepare_snapshot_invalid",
}


def _prepare_context_rejection(stage, error):
    """Classify only one exact known code at its fixed preparation stage."""
    args = error.args
    if len(args) == 1 and type(args[0]) is str:
        return _PREPARE_CONTEXT_REASONS.get((stage, args[0]), "prepare_context_other")
    return "prepare_context_other"


def _prepare_jobs(ledger, rows, target, prefilter: bool, *, allow_cloud=True,
                  background=False, cloud_only=False, load_checkpoints=True, deadline=None,
                  blocked_reasons=None, blocked_rows=None) -> tuple[list, int, int]:
    """Load contexts/checkpoints on the calling thread; settle prefiltered rows."""
    jobs = []
    skipped = 0
    blocked = 0

    def block(reason):
        nonlocal blocked
        blocked += 1
        if blocked_reasons is not None:
            blocked_reasons[reason] = blocked_reasons.get(reason, 0) + 1
        if blocked_rows is not None:
            blocked_rows.append((dict(r), reason))

    cfg = load_config()
    extract_cfg = cfg.get("extract") if isinstance(cfg, dict) else None
    enabled = isinstance(extract_cfg, dict) and extract_cfg.get("patient_state_context") is True
    for r in rows:
        if deadline is not None and time.monotonic() >= deadline:
            block("prepare_deadline")
            continue
        r = dict(r)
        binding = inference_dependency_loss.source_binding_from_row(r)
        if (not isinstance(binding, str) or not binding
                or r.get('_source_binding', binding) != binding
                or inference_dependency_loss.source_binding(ledger.db, r['project_id'], r['message_id']) != binding):
            block("prepare_source_changed")
            continue
        r['_source_binding'] = binding
        r["_target"] = target
        mode = _cloud_mode(ledger, cfg, "reextract", r["project_id"]) if allow_cloud and _cloud_reextract(
            ledger, r, background=background) else "off"
        if background and mode in ('on', 'shadow'):
            r['_cloud_history_snapshot'] = cloud_policy.history_thread_snapshot(
                ledger.db, r['project_id'], r['message_id'])
            if r['_cloud_history_snapshot'] is None:
                mode = 'off'
        r["_cloud_background"] = background and mode in ("on", "shadow")
        if cloud_only and mode != "on":
            block("prepare_cloud_disabled")
            continue
        r["_cloud_mode"] = mode
        if mode in ("on", "shadow", "dry_run"):
            r["_cloud_config"] = copy.deepcopy(cfg)
            r["_cloud_settings"] = cloud_inference.resolve_cloud_settings(cfg)
            selection, errors = cloud_policy.resolve_profile(cfg, route="reextract", purpose="backlog", root=HOME)
            if errors or selection is None:
                block("prepare_profile_unavailable")
                continue
            r["_cloud_selection"] = selection
        if mode == "on":
            r["_provider_contract"] = cloud_inference.cloud_provider_contract(selection=selection)
            if cloud_only:
                r["_required_cloud_contract"] = r["_provider_contract"]
            r["_target"] = (target[0], r["_provider_contract"]["model"])
        qc = _qc_feedback(ledger, r["qc_src"]) if r["qc_src"] else None
        if r["qc_src"] and qc is None:
            continue   # flagged in SQL but the audit is gone or clean
        hints = _rule_hints(r)
        if prefilter and not cloud_only and qc is None and not r["thin_src"] \
                and not r["human_src"] \
                and _low_signal(r["body_text"] or "", hints):
            # no clinical signal on either net — settle with a durable
            # marker + v1 coverage instead of burning an LLM call
            _ensure_v1(ledger, r, hints)
            if _mark_prefiltered(ledger, r):
                skipped += 1
            continue
        base = _thread_context(ledger.db, r, budget=600) if enabled else _thread_context(ledger.db, r)
        r['_input_context'] = base
        r['_input_context_budget'] = 600 if enabled else _CTX_TOTAL_MAX
        retry = ledger.db.execute("SELECT meta FROM artifacts WHERE kind=? AND message_id=? "
            "AND " + inference.current_meta_pred("meta", target=_INFERENCE_VERSION) + " "
            "AND json_extract(" + json_or_null("meta") + ",'$.error')=1 "
            "AND json_extract(" + json_or_null("meta") + ",'$.hash')=? ORDER BY artifact_id DESC LIMIT 1",
            (KIND, r["message_id"], r["content_hash"])).fetchone()
        retry_meta = (loads_dict(retry[0]) or {}) if retry else {}
        auto = retry_meta.get("auto_retry")
        attempt = {key: r.get(key) for key in ("qc_src", "human_src", "thin_src")}
        attempt["auto_retry"] = auto if type(auto) is int and auto >= 0 else 0
        stage = "load"
        try:
            frame = _pending_context_frame(ledger.db, r, attempt=attempt, enabled=enabled)
            if frame is None:
                hint_deadline = time.monotonic() + 1.0
                if deadline is not None:
                    hint_deadline = min(hint_deadline, deadline)
                stage = "medication"
                medication_hints = extraction_medications.build(ledger.db, cfg, r["body_text"],
                    private=mode != "on", deadline=hint_deadline)
                stage = "prepare"
                frame = extraction_context.prepare(ledger, r, model=r["_target"][1],
                    contract=_context_contract(), attempt=attempt, enabled=enabled, base_context=base,
                    version=EXTRACT_VERSION, medication_hints=medication_hints,
                    compatible_contracts=tuple(_context_contracts() - {_context_contract()}))
            r["_frame"] = frame
            if not _medication_reference_current(ledger.db, r, cfg=cfg, deadline=deadline):
                block("prepare_medication_changed")
                continue
        except sqlite3.Error:
            block("prepare_database_unavailable")
            continue
        except ValueError as error:
            block(_prepare_context_rejection(stage, error))
            continue
        context = r["_context"] = r["_frame"]["text"]
        saved = {}
        if load_checkpoints:
            r["_chunk_size"] = _saved_chunk_size(ledger.db, r, context)
            r["_splits"], saved = _saved_chunk_state(ledger.db, r, context, chunk_size=r["_chunk_size"])
        jobs.append((r, context, saved, hints, qc))
    return jobs, skipped, blocked


def _plan_units(claimed: list, batch_k: int) -> list:
    """Group claimed rows into ("single", tup) and ("batch", [tup...]) units."""
    units: list = []
    group: list = []
    for tup in claimed:
        _index, r, ctx, saved, _h, qc, _l = tup
        # QC-flagged/thin-retry rows take the single lane — their
        # feedback/repair prompts differ from the shared batch
        # envelope.
        if batch_k >= 2 and r.get("_cloud_mode") != "on" and qc is None and not r["thin_src"] \
                and ctx is None and not saved \
                and r["_chunk_size"] == _CHUNK_SIZE \
                and len(plan_chunks(r["body_text"], _CHUNK_SIZE)) <= 1:
            group.append(tup)
            if len(group) >= batch_k:
                units.append(("batch", group))
                group = []
        else:
            if group:
                units.append(("batch", group))
                group = []
            units.append(("single", tup))
    if group:
        units.append(("batch", group))
    return units


def _backlog_report(ledger, selected: int, limit: int,
                    settled: int) -> tuple:
    """Return (left, oldest ts, newest ts, estimate) — left is None when
    the selection was full; estimate is the per-process pending count."""
    left = age_min = age_max = None
    estimate_key = _left_estimate_key(ledger)
    estimate = _LEFT_ESTIMATE.get(estimate_key)
    if selected < (limit or 20) or estimate is None \
            or time.monotonic() - estimate["at"] >= _LEFT_ESTIMATE_S:
        # A short selection means the backlog is nearly drained, so the
        # full pending scan is cheap enough to report. A full selection
        # (the resident --all --semantic lane runs limit=1 per job)
        # reports left=None = unknown instead of an O(backlog) scan per
        # job, but refreshes a per-process estimate at most every
        # _LEFT_ESTIMATE_S so a large backlog (a version bump re-pends
        # the corpus) is still visible in the log (M12).
        count, age_min, age_max = ledger.db.execute(
            "SELECT COUNT(*), MIN(m.posted_at_ts), MAX(m.posted_at_ts) "
            f"FROM messages m WHERE {pending_pred(ledger.db)}").fetchone()
        estimate = _LEFT_ESTIMATE[estimate_key] = {
            "at": time.monotonic(), "left": count, "done": 0}
        if selected < (limit or 20):
            left = count
    else:
        estimate["done"] += settled
    return left, age_min, age_max, estimate
