"""Read current extraction artifacts and feedback eligibility without adopting results."""
import hashlib
import json
import sqlite3
import sys
from types import SimpleNamespace
import semantic_store
import inference_version as inference
import cloud_backlog
import extract_quality
from extraction_provenance import (
    provider_strict_dict as _provider_strict_dict,
)
import extraction_refresh
import mcs_queries
import mcs_util
from mcs_queries import (ELIGIBLE_MESSAGE, EXTRACT_FEEDBACK_KIND, V4_PROJECTION_KIND,
                         current_extract_pred,
                         current_qc_pred,
                         current_v4_id, json_or_null, qc_source_id)
from mcs_util import load_config, loads_dict

from extract_llm_source_contract import (
    EXTRACT_VERSION,
    KIND,
    PATIENT_CONTEXT_VERSION,
    UPGRADE_KIND,
    _INFERENCE_VERSION,
    _LOADED_SOURCE_DIGESTS,
)

_AUTO_RETRY_SQL = ("(CASE WHEN json_type(meta,'$.auto_retry')='integer' "
                   "THEN json_extract(meta,'$.auto_retry') ELSE 0 END)")


def _current(ledger, mid: int, content_hash: str) -> bool:
    """True if a non-error artifact of the CURRENT schema version for
    this exact body already exists — guards per-write locking against a
    tick processing the same message between this process's LLM call and
    its write. Readers and writers require the exact public generation
    and source hash together."""
    return ledger.db.execute(
        f"SELECT 1 FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
        f"WHERE a.kind=? AND m.message_id=? {current_extract_pred()} "
        "AND m.content_hash=? "
        f"AND json_extract({json_or_null('a.meta')},'$.extract_version')=? "
        "LIMIT 1",
        (KIND, mid, content_hash, EXTRACT_VERSION)).fetchone() is not None


def _upgrade_contract() -> str:
    return hashlib.sha256(json.dumps([_INFERENCE_VERSION, EXTRACT_VERSION, PATIENT_CONTEXT_VERSION,
        _LOADED_SOURCE_DIGESTS], sort_keys=True).encode()).hexdigest()


def _prior_extract(db, r):
    return db.execute(
        "SELECT a.* FROM artifacts a JOIN messages m ON a.message_id=m.message_id "
        f"WHERE a.kind=? AND m.message_id=? AND m.project_id=? AND m.content_hash=? {mcs_queries.source_extract_pred()} "
        "AND (m.body_state IS NULL OR m.body_state='full') AND length(m.body_text)>0 "
        f"AND COALESCE(json_extract({json_or_null('a.content')},'$._error'),0)=0 "
        "ORDER BY a.artifact_id DESC LIMIT 1",
        (KIND, r["message_id"], r["project_id"], r["content_hash"])).fetchone()


def _prior_identity(prior):
    return tuple(prior[key] for key in ("artifact_id", "project_id", "message_id", "content", "model", "meta"))


def _hold_sql(*, human=False, released=False):
    """Source-bound private receipts; a fresh human report bypasses automatic holds.
    ``released`` matches the same receipt minted under an older code contract."""
    prior = ("c.artifact_id" if human else "(SELECT p.artifact_id FROM artifacts p "
        "JOIN messages pm ON pm.message_id=p.message_id "
        f"WHERE p.kind='{KIND}' AND pm.message_id=m.message_id "
        + mcs_queries.source_extract_pred("p", "pm")
        + " AND (pm.body_state IS NULL OR pm.body_state='full') AND length(pm.body_text)>0 "
        + f"AND COALESCE(json_extract({json_or_null('p.content')},'$._error'),0)=0 "
        + "ORDER BY p.artifact_id DESC LIMIT 1)")
    reason = ("json_extract(u.content,'$.reason')='human_failed' "
              "AND json_extract(u.content,'$.feedback_id')=h.artifact_id" if human else
              "json_extract(u.content,'$.reason') IN ('protected_loss','qc_failed')")
    return f"""SELECT 1 FROM artifacts u WHERE u.kind='{UPGRADE_KIND}'
        AND u.project_id=m.project_id AND u.message_id=m.message_id
        AND CASE WHEN json_valid(u.content) AND json_valid(u.meta) THEN
            json_extract(u.content,'$.state')='held_prior'
            AND json_extract(u.content,'$.hash')=m.content_hash
            AND json_type(u.content,'$.prior_artifact_id')='integer'
            AND json_extract(u.content,'$.prior_artifact_id')={prior}
            AND json_extract(u.content,'$.target_version')={EXTRACT_VERSION}
            AND json_extract(u.content,'$.contract'){'<>' if released else '='}'{_upgrade_contract()}'
            AND {inference.current_meta_pred('u.meta', target=_INFERENCE_VERSION)}
            AND COALESCE(json_extract(u.meta,'$.invalidated'),0)=0
            AND {reason} ELSE 0 END"""


def _verify_created_artifact_tx(db, artifact_id, *, kind, project_id, message_id,
                                content, model, meta, created_at):
    """Verify the new row on the borrowed writer transaction after triggers."""
    if (not db.in_transaction or type(artifact_id) is not int or db.execute(
            "SELECT 1 FROM artifacts WHERE artifact_id=? AND kind IS ? AND project_id IS ? "
            "AND message_id IS ? AND content IS ? AND model IS ? AND meta IS ? AND created_at IS ?",
            (artifact_id, kind, project_id, message_id, content, model, meta, created_at)).fetchone() is None):
        raise RuntimeError("extract_insert_unverified")


def _commit_rejected(r, reason):
    """Return the unchanged rejection outcome while retaining only a fixed diagnostic."""
    if isinstance(r, dict):
        if reason == "completeness":
            detail = r.get("_completeness_reject_reason")
            if type(detail) is str and detail in cloud_backlog._COMPLETENESS_REJECTION_REASONS:
                reason = detail
        r["_commit_reject_reason"] = ("commit_" + reason
            if type(reason) is str and reason in cloud_backlog._COMMIT_REJECTION_REASONS else "commit_rejected")
    return "rejected"


def _rerender_cards(ledger, project_id, message_id) -> None:
    """Queue the current extraction or progress on its existing source-bound card."""
    db = getattr(ledger, "db", None)
    try:
        if semantic_store.refresh_cards_allowed(db, project_id, message_id):
            extraction_refresh.refresh_extraction_cards(ledger, project_id, message_id, cfg=load_config())
    except Exception as error:
        if db is None or getattr(db, "in_transaction", True):
            raise
        # The extraction is committed and the card sweep retries its presentation.
        print(json.dumps({"event": "card_rerender_failed", "error": type(error).__name__}), file=sys.stderr)


def _thin_fact_count(raw):
    """Count with the Python thinness rules; invalid shapes get one repair."""
    document = loads_dict(raw)
    if document is None:
        return 0
    try:
        return extract_quality.thin_fact_count(document)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        # Existing SQL treated invalid shapes as zero: request at most the
        # existing single quality repair, never claim quality passed.
        return 0


def _register_thin_fact_count(db):
    """Register once on a borrowed connection without changing its transaction."""
    try:
        db.execute("SELECT mcs_thin_fact_count(NULL)").fetchone()
    except sqlite3.OperationalError as error:
        if "no such function: mcs_thin_fact_count" not in str(error):
            raise
        db.create_function("mcs_thin_fact_count", 1, _thin_fact_count, deterministic=True)


def _thin_pending_sql(db) -> str:
    """A current, valid, same-version artifact that is under-extracted
    (<2 clinical assertions on a >=300-char body) and not retried
    yet — re-pends for ONE quality re-extract. Thinness is recomputed
    from content shape so artifacts written before meta.thin existed
    qualify without a backfill. Prefilter markers are excluded: a
    no-signal row is honest, not thin. The new row carries
    meta.thin_retried so a still-thin retry settles permanently."""
    _register_thin_fact_count(db)
    meta = json_or_null("t.meta")
    return f"""SELECT t.artifact_id FROM artifacts t
        WHERE t.kind='{KIND}' AND t.message_id=m.message_id
          {current_extract_pred('t')}
          AND json_extract({meta},'$.extract_version')={EXTRACT_VERSION}
          AND json_extract({meta},'$.prefilter') IS NULL
          AND json_extract({meta},'$.thin_retried') IS NULL
          AND json_extract({meta},'$.human_fix') IS NULL
          AND length(m.body_text) >= {extract_quality._THIN_BODY_MIN}
          AND mcs_thin_fact_count(t.content) < {extract_quality._THIN_FACTS_MIN}
          ORDER BY t.artifact_id DESC LIMIT 1"""


def _thin_retry_content(ledger, row) -> str | None:
    """Read the pinned thin source under its message, project and hash scope."""
    source = ledger.db.execute(
        "SELECT content FROM artifacts WHERE artifact_id=? "
        "AND kind=? AND message_id=? "
        "AND (project_id=? OR project_id IS NULL) "
        "AND " + inference.current_meta_pred("meta", target=_INFERENCE_VERSION)
        + " AND json_valid(meta) AND json_extract(meta,'$.hash')=?",
        (row["thin_src"], KIND, row["message_id"], row["project_id"],
         row["content_hash"])).fetchone()
    return source["content"] if source is not None else None


def _human_flagged_sql(val: str = "h.artifact_id") -> str:
    """A human ⚠ report (extract_feedback_v1, card button) pinned to the
    message's CURRENT extract artifact re-pends it for exactly one
    re-extract, bypassing the no-signal prefilter. Success or failure
    replaces the current row (meta.human_fix: feedback_id, applied), so
    the report no longer pins the current row and the loop ends; a
    human_fix row is never thin-retried. Parallel to the QC
    single-retry, never gated by it."""
    return f"""SELECT {val} FROM artifacts h
        JOIN artifacts c ON c.artifact_id=json_extract(
               {json_or_null('h.content')},'$.artifact_id')
        WHERE h.kind='{EXTRACT_FEEDBACK_KIND}'
          AND h.message_id=m.message_id
          AND c.kind='{KIND}' AND c.message_id=m.message_id
          {current_extract_pred('c')}
          AND NOT EXISTS ({_hold_sql(human=True)})
        ORDER BY h.artifact_id DESC LIMIT 1"""


def _has_kind(kind: str, alias: str, project: bool = False) -> str:
    """Index-only (kind, message_id) existence probe — no JSON parsed."""
    scope = f" AND {alias}.project_id=m.project_id" if project else ""
    return (f"EXISTS (SELECT 1 FROM artifacts {alias} WHERE {alias}.kind='{kind}'"
            f" AND {alias}.message_id=m.message_id{scope})")


def pending_pred(db) -> str:
    """Shared body-current extraction and quality-retry eligibility.

    Evaluated per stored message, so the JSON-heavy subqueries sit behind
    exact, index-only guards (M5): each guarded subquery itself requires
    an artifact of the probed kind for the message (or, for thin, the
    same body-length bound), so a missing guard row already decides it.
    The qc_fix test runs after the settle test, which is false for most
    of a mature corpus. Boolean results are identical to the unguarded
    composition (tests/extract/llm/test_stab_pending_pred.py)."""
    return f"""{ELIGIBLE_MESSAGE}
        AND (NOT {_has_kind(V4_PROJECTION_KIND, 'v0', project=True)}
             OR {current_v4_id()} IS NULL)
        AND (((NOT EXISTS (SELECT 1 FROM artifacts a
                        WHERE a.kind='{KIND}' AND a.message_id=m.message_id
                          {current_extract_pred()}
                          AND json_extract({json_or_null('a.meta')},'$.extract_version')={EXTRACT_VERSION})
             OR ({_has_kind('extract_qc', 'q0')}
                 AND EXISTS ({_qc_flagged_sql(val="1")}))
             OR (length(m.body_text) >= {extract_quality._THIN_BODY_MIN}
                 AND EXISTS ({_thin_pending_sql(db)})))
        AND NOT EXISTS (SELECT 1 FROM artifacts f
                        WHERE f.kind='{KIND}' AND f.message_id=m.message_id
                          {current_extract_pred('f')}
                          AND json_extract({json_or_null('f.meta')},'$.extract_version')={EXTRACT_VERSION}
                          AND json_extract({json_or_null('f.meta')},'$.qc_fix') IS NOT NULL)
        AND (NOT {_has_kind(UPGRADE_KIND, 'u0', project=True)} OR NOT EXISTS ({_hold_sql()})))
             OR ({_has_kind(EXTRACT_FEEDBACK_KIND, 'h0')}
                 AND EXISTS ({_human_flagged_sql(val="1")})))"""


_QC_ACTIONABLE_SQL = """CASE WHEN json_valid(q.content) THEN
           (json_type(q.content,'$.items')='array'
            AND EXISTS (SELECT 1 FROM json_each(q.content,'$.items') qi
                        WHERE qi.type='object'
                          AND json_extract(qi.value,'$.verdict')
                              ='NO_MATCH'))
           OR (json_type(q.content,'$.urgency')='object'
               AND json_extract(q.content,'$.urgency.jev') IS NOT NULL
               AND json_extract(q.content,'$.urgency.jev') IS NOT
                   json_extract(q.content,'$.urgency.extracted'))
           ELSE 0 END"""


def _qc_flagged_sql(msg: str = "m", val: str = "a.artifact_id") -> str:
    """Scalar subquery: the extract artifact whose current Jev QC audit
    flagged unsupported items (NO_MATCH verdicts) or an urgency
    mismatch. The source pin lands on the NEWEST current extraction —
    a verdict on a superseded artifact never flags."""
    src = qc_source_id(msg, version=EXTRACT_VERSION)
    return f"""SELECT {val} FROM artifacts a
        JOIN artifacts q ON q.message_id=a.message_id
         AND q.kind='extract_qc'
         {current_qc_pred('q', msg, version=EXTRACT_VERSION)}
         AND q.artifact_id=(SELECT MAX(q2.artifact_id) FROM artifacts q2
             WHERE q2.kind='extract_qc'
               AND q2.message_id={msg}.message_id
               {current_qc_pred('q2', msg, version=EXTRACT_VERSION)})
         AND json_extract({json_or_null('q.meta')},'$.source_artifact_id')
             =a.artifact_id
         AND json_extract({json_or_null('q.content')},'$.qc')='done'
         AND ({_QC_ACTIONABLE_SQL})
        WHERE a.kind='{KIND}' AND a.artifact_id={src}
        ORDER BY q.artifact_id DESC LIMIT 1"""


def _qc_feedback(ledger, src_artifact_id):
    """Flagged QC audit for a current extraction -> {src, qc, notes}
    for a feedback re-extract, or None when nothing actionable remains
    (the artifact vanished, or the newest audit is clean)."""
    # Same audit row _qc_flagged_sql pinned: the newest CURRENT QC of
    # this extraction generation — never an older/stale audit.
    row = ledger.db.execute(
        f"""SELECT q.artifact_id, q.content FROM artifacts q
           JOIN artifacts a ON a.artifact_id=?
           JOIN messages m ON m.message_id=a.message_id
           WHERE q.kind='extract_qc' AND q.message_id=a.message_id
             AND (q.project_id IS NULL OR q.project_id=a.project_id)
             {current_qc_pred('q', 'm', version=EXTRACT_VERSION)}
             AND CASE WHEN json_valid(q.meta) AND json_valid(q.content)
                 THEN json_type(q.content)='object'
                  AND json_extract(q.meta,'$.source_artifact_id')=a.artifact_id
                  AND json_extract(q.content,'$.qc')='done' ELSE 0 END
           ORDER BY q.artifact_id DESC LIMIT 1""",
        (src_artifact_id,)).fetchone()
    if row is None:
        return None
    try:
        c = json.loads(row["content"] or "{}")
    except (ValueError, TypeError, RecursionError):
        return None
    notes = _qc_notes(c)
    if not notes:
        return None
    return {"src": src_artifact_id, "qc": row["artifact_id"], "notes": notes}


def _qc_notes(c):
    """Derive the same bounded feedback from a checked QC document."""
    notes = []
    items = c.get("items")
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or it.get("verdict") != "NO_MATCH":
            continue
        item = it.get("item")
        if isinstance(item, dict):
            label = item.get("name") or item.get("text")
            if label is None and isinstance(item.get("vitals"), dict):
                label = ",".join(f"{vk}={vv}"
                                 for vk, vv in item["vitals"].items())
            if label is None:
                label = json.dumps(item, ensure_ascii=False)
        else:
            label = str(item)
        notes.append(f"{it.get('section')}項目「{str(label)[:80]}」"
                     "は本文に裏付けがありません")
    urg = c.get("urgency")
    if isinstance(urg, dict) and urg.get("jev") is not None \
            and urg.get("jev") != urg.get("extracted"):
        notes.append(f"緊急度は「{urg.get('jev')}」が妥当と監査されています"
                     f"(前回の抽出: {urg.get('extracted')})")
    return notes[:6]


def _compact_qc_feedback(content):
    """Keep only the audit elements that generated the bounded feedback."""
    items = []
    for item in content.get("items", []) if isinstance(content.get("items"), list) else []:
        if not isinstance(item, dict) or item.get("verdict") != "NO_MATCH":
            continue
        # Use the existing formatter to preserve its exact label semantics
        # without retaining unrelated audit fields or an unbounded item.
        note = _qc_notes({"items": [item]})[0]
        section = str(item.get("section"))
        prefix = section + "項目「"
        suffix = "」は本文に裏付けがありません"
        if not note.startswith(prefix) or not note.endswith(suffix):
            raise ValueError("extract_proof_qc_invalid")
        items.append({"section": section, "verdict": "NO_MATCH", "item": note[len(prefix):-len(suffix)]})
        if len(items) == 6:
            break
    compact = {"qc": "done", "items": items}
    urgency = content.get("urgency")
    if len(items) < 6 and isinstance(urgency, dict) and urgency.get("jev") is not None:
        compact["urgency"] = {"jev": urgency.get("jev"), "extracted": urgency.get("extracted")}
    if _qc_notes(compact) != _qc_notes(content):
        raise ValueError("extract_proof_qc_invalid")
    return compact


def _snapshot_repair_qc(db, r):
    """Freeze the selected current audit before its source is replaced."""
    src = r.get("qc_src")
    if src is None:
        return None
    prior = _prior_extract(db, r)
    qc = _qc_feedback(SimpleNamespace(db=db), src)
    if prior is None or prior["artifact_id"] != src or qc is None:
        raise ValueError("extract_proof_qc_changed")
    row = db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND kind='extract_qc'",
                     (qc["qc"],)).fetchone()
    if row is None or row["message_id"] != r["message_id"] or row["project_id"] not in (None, r["project_id"]):
        raise ValueError("extract_proof_qc_changed")
    source_meta, audit_meta, audit = (_provider_strict_dict(prior["meta"]),
                                      _provider_strict_dict(row["meta"]), _provider_strict_dict(row["content"]))
    if source_meta is None or audit_meta is None or audit is None:
        raise ValueError("extract_proof_qc_invalid")
    result = {"source_id": src, "source_binding": r["_source_binding"], "source_project_id": prior["project_id"],
              "source_message_id": prior["message_id"], "source_model": prior["model"],
              "source_content_hash": mcs_util.payload_hash(prior["content"]),
              "source_meta": {key: source_meta.get(key) for key in ("hash", "extract_version", inference.VERSION_KEY)},
              "audit_id": row["artifact_id"], "audit_project_id": row["project_id"],
              "audit_message_id": row["message_id"], "audit_content": _compact_qc_feedback(audit),
              "audit_meta": {key: audit_meta.get(key) for key in
                             ("hash", "extract_version", inference.VERSION_KEY, "source_artifact_id")}}
    if _repair_qc_snapshot_feedback(r, result) != qc:
        raise ValueError("extract_proof_qc_invalid")
    return result


def _repair_qc_snapshot_feedback(r, snapshot):
    try:
        keys = {"source_id", "source_binding", "source_project_id", "source_message_id", "source_model", "source_content_hash",
                "source_meta", "audit_id", "audit_project_id", "audit_message_id", "audit_content", "audit_meta"}
        if not isinstance(snapshot, dict) or set(snapshot) != keys:
            return None
        sm, qm, content = snapshot["source_meta"], snapshot["audit_meta"], snapshot["audit_content"]
        if (type(snapshot["source_id"]) is not int or type(snapshot["audit_id"]) is not int
                or snapshot["source_id"] <= 0 or snapshot["audit_id"] <= snapshot["source_id"]
                or not isinstance(r.get("_source_binding"), str) or snapshot["source_binding"] != r["_source_binding"]
                or snapshot["source_project_id"] not in (None, r["project_id"])
                or snapshot["audit_project_id"] not in (None, r["project_id"])
                or snapshot["source_message_id"] != r["message_id"] or snapshot["audit_message_id"] != r["message_id"]
                or not isinstance(snapshot["source_model"], str) or not mcs_util.valid_hash(snapshot["source_content_hash"])
                or not isinstance(sm, dict) or not isinstance(qm, dict) or not isinstance(content, dict)
                or sm.get("hash") != r["content_hash"] or qm.get("hash") != r["content_hash"]
                or sm.get("extract_version") != EXTRACT_VERSION or qm.get("extract_version") != EXTRACT_VERSION
                or not inference.is_current(sm, target=_INFERENCE_VERSION)
                or not inference.is_current(qm, target=_INFERENCE_VERSION)
                or qm.get("source_artifact_id") != snapshot["source_id"] or content.get("qc") != "done"):
            return None
        notes = _qc_notes(content)
        return {"src": snapshot["source_id"], "qc": snapshot["audit_id"], "notes": notes} if notes else None
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


_ERROR_ROWS_SQL = f"""FROM artifacts e WHERE e.message_id=m.message_id AND e.kind=?
        AND {inference.current_meta_pred("e.meta", target=_INFERENCE_VERSION)}
        AND CASE WHEN json_valid(e.meta)
                 THEN json_extract(e.meta,'$.error')=1
                  AND COALESCE(json_extract(e.meta,'$.invalidated'),0)=0
                  AND COALESCE(json_extract(e.meta,'$.extract_version'),0)=?
                  AND json_extract(e.meta,'$.hash')=m.content_hash
                  AND (e.project_id IS NULL OR e.project_id=m.project_id)
                  AND m.body_state IS NOT 'deleted'
                  AND (json_type(e.meta,'$.attempts') IS NULL OR
                    (json_type(e.meta,'$.attempts')='integer' AND json_extract(e.meta,'$.attempts')>=0))
                 ELSE 0 END"""


def _retry_budget_sql() -> str:
    """Shared selection/progress boundary; waiting does not mean exhausted."""
    return f"""NOT EXISTS (SELECT 1 FROM artifacts e WHERE e.kind='{KIND}' AND e.message_id=m.message_id
          AND {inference.current_meta_pred('e.meta', target=_INFERENCE_VERSION)}
          AND CASE WHEN json_valid(e.meta) THEN json_extract(e.meta,'$.error')=1
            AND COALESCE(json_extract(e.meta,'$.invalidated'),0)=0
            AND COALESCE(json_extract(e.meta,'$.extract_version'),0)={EXTRACT_VERSION}
            AND json_extract(e.meta,'$.hash')=m.content_hash
            AND (e.project_id IS NULL OR e.project_id=m.project_id)
            AND m.body_state IS NOT 'deleted'
            AND json_type(e.meta,'$.attempts')='integer'
            AND json_extract(e.meta,'$.attempts')>=5 ELSE 0 END)"""
