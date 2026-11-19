"""Count earlier-registry semantic work independently of patient-context completion."""
from __future__ import annotations

import sqlite3

import mcs_util


_STATE_BUCKETS = {
    "pending": "runnable", "repair_required": "runnable",
    "held": "held", "failed": "failed", "paused": "paused",
    "scope": "excluded", "permission": "excluded", "deleted": "excluded",
    "empty_body": "excluded", "body_unavailable": "excluded",
}


def latest_post_assessment(db, project_id, message_id):
    """Return the latest post row and parsed metadata, keeping malformed rows visible."""
    from semantic_policy import KIND_ASSESS
    for stored in db.execute(
            "SELECT artifact_id,meta FROM artifacts WHERE kind=? AND project_id=? "
            "AND message_id=? ORDER BY artifact_id DESC", (KIND_ASSESS, project_id, message_id)):
        meta = mcs_util.loads_dict(stored["meta"])
        if meta is None or meta.get("fact_id") is None:
            return stored, meta
    return None, None


def prior_registry_remaining(ledger, cfg) -> dict:
    """Count old-registry posts by the existing semantic result classification.

    Posts with no post-level assessment are outside the earlier-registry cohort.
    Existing undecodable or unversioned assessments are reported as unassessed,
    never replaced by an older row or treated as permission to rebuild.
    """
    import semantic_jev
    import semantic_policy
    import semantic_store
    import semantic_transport

    if not isinstance(cfg, dict):
        raise ValueError("semantic_prior_registry_config_invalid")
    try:
        scfg, errors = semantic_policy.runtime_config(cfg)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError, OSError):
        raise ValueError("semantic_prior_registry_config_invalid") from None
    if errors:
        raise ValueError("semantic_prior_registry_config_invalid")
    counts = dict.fromkeys(("runnable", "held", "failed", "paused", "excluded", "unassessed"), 0)
    db = ledger.db
    try:
        own_transaction = not db.in_transaction
        with semantic_transport.config_model_scope(cfg):
            if own_transaction:
                db.execute("BEGIN")
            try:
                rows = db.execute("SELECT message_id,project_id FROM messages ORDER BY message_id")
                for row in rows:
                    stored, meta = latest_post_assessment(db, row["project_id"], row["message_id"])
                    if stored is None:
                        continue
                    registry = meta.get("registry") if meta is not None else None
                    if type(registry) is not str:
                        counts["unassessed"] += 1
                        continue
                    if registry == semantic_jev.REGISTRY_VERSION:
                        continue
                    source = db.execute(
                        "SELECT m.message_id,m.project_id,m.parent_id,m.body_state,m.body_text,"
                        "p.project_id AS patient_id,p.fetch_reason FROM messages m "
                        "LEFT JOIN patients p ON p.project_id=m.project_id WHERE m.message_id=?",
                        (row["message_id"],)).fetchone()
                    if source is None:
                        raise ValueError("semantic_prior_registry_source_unverified")
                    state = semantic_store.reprocess_target_state(ledger, source, scfg, cfg=cfg)
                    if state in ("current", "current_skipped"):
                        continue
                    bucket = _STATE_BUCKETS.get(state)
                    if bucket is None:
                        raise ValueError("semantic_prior_registry_state_unverified")
                    counts[bucket] += 1
                return counts
            finally:
                if own_transaction:
                    db.rollback()
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError,
            RecursionError, OverflowError, OSError):
        raise ValueError("semantic_prior_registry_unavailable") from None
