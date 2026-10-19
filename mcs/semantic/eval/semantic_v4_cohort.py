"""Bounded v4 conversion cohorts: declare, schedule, record, hold cleanup.

Not wired into the scheduled pipeline yet (docs/specs/semantic-evaluation.md
"実装上の保留条件"): only tests drive these entry points. The pieces the
running drainer needs — the cohort reader, item receipts and
hold_unbounded_job — stay in semantic_v4; this module only adds the
operator-side entry points on top of them.
"""
from __future__ import annotations

import json
import time

from semantic_policy import JOB_KIND
from semantic_v4 import (ENGINE_VERSION, KIND_V4_COHORT, _begin, _cohort,
                         _item_receipt, _time_value, _valid_cohort,
                         cohort_items_done)


def declare_cohort(ledger, cohort: str, items: list[dict],
                   ceilings: dict, expires_at: float,
                   retire_after_at: float | None = None) -> dict:
    """Persist fixed source/dependency generations and lifetime ceilings."""
    if not isinstance(ceilings, dict) or not isinstance(items, list):
        raise ValueError("cohort_invalid")
    doc = {"contract": "mcs-v4-cohort/1", "cohort": cohort,
           "items": items, "ceilings": {
               "items": ceilings.get("items", len(items)),
               "calls": ceilings.get("calls", len(items) * 4),
               "tokens": ceilings.get("tokens", 0),
               "retries": ceilings.get("retries", 1)},
           "expires_at": expires_at, "retire_after_at": retire_after_at,
           "created_at": time.time()}
    if not _valid_cohort(doc):
        raise ValueError("cohort_invalid")
    _begin(ledger)
    with ledger.db:
        existing = _cohort(ledger, cohort)
        if existing is not None:
            if not _valid_cohort(existing):
                raise ValueError("cohort_invalid")
            return existing
        from semantic_store import thread_bundle
        fixed = []
        for item in items:
            mid = item["message_id"]
            row = ledger.db.execute(
                "SELECT project_id,content_hash FROM messages WHERE message_id=?",
                (mid,)).fetchone()
            if row is None or row["content_hash"] != item["content_hash"]:
                raise ValueError("cohort_source_changed")
            bundle = thread_bundle(ledger.db, row["project_id"], mid, [mid])
            fixed.append({"message_id": mid, "project_id": row["project_id"],
                          "content_hash": row["content_hash"],
                          "source_fingerprint": bundle["source_fingerprint"]})
        doc["items"] = fixed
        ledger.artifact_add_tx(
            KIND_V4_COHORT, json.dumps(doc, ensure_ascii=False, allow_nan=False),
            project_id=None, message_id=None,
            meta={"cohort": cohort, "engine_version": ENGINE_VERSION})
    return doc


def _source_current(ledger, item: dict) -> bool:
    from semantic_store import thread_bundle
    row = ledger.db.execute(
        "SELECT project_id,content_hash FROM messages WHERE message_id=?",
        (item["message_id"],)).fetchone()
    if (row is None or row["content_hash"] != item["content_hash"]
            or row["project_id"] != item.get("project_id")):
        return False
    bundle = thread_bundle(ledger.db, row["project_id"], item["message_id"],
                           [item["message_id"]])
    return bundle is not None and bundle["source_fingerprint"] == \
        item.get("source_fingerprint")


def run_cohort(ledger, cohort: str, now: float | None = None) -> dict:
    """Reserve each conversion job once within the lifetime item ceiling."""
    now = time.time() if now is None else now
    if not _time_value(now):
        raise ValueError("cohort_time_invalid")
    _begin(ledger)
    with ledger.db:
        doc = _cohort(ledger, cohort)
        if doc is None:
            return {"cohort": cohort, "error": "cohort_unknown"}
        if not _valid_cohort(doc):
            return {"cohort": cohort, "error": "cohort_invalid"}
        if doc["expires_at"] <= now:
            return {"cohort": cohort, "error": "cohort_expired",
                    "scheduled": 0, "remaining": 0}
        done = cohort_items_done(ledger, cohort)
        scheduled = pending = 0
        for item in doc["items"]:
            mid = item["message_id"]
            if mid in done or len(done) >= doc["ceilings"]["items"]:
                continue
            if not _source_current(ledger, item):
                _item_receipt(ledger, cohort, mid, "needs_review",
                              reason="source_changed")
                done[mid] = {"action": "needs_review"}
                continue
            existing = ledger.db.execute(
                "SELECT 1 FROM fetch_jobs WHERE kind=? AND project_id=? "
                "AND message_id=? AND state='pending'",
                (JOB_KIND, item["project_id"], mid)).fetchone()
            if existing is not None:
                pending += 1
                continue
            ledger.job_add_tx(
                JOB_KIND, item["project_id"], mid,
                payload={"targets": [mid], "notification_free": True,
                         "source_generation": item["source_fingerprint"],
                         "origin": {"source": "v4_cohort", "cohort": cohort}})
            _item_receipt(ledger, cohort, mid, "scheduled")
            done[mid] = {"action": "scheduled"}
            scheduled += 1
        remaining = sum(done.get(it["message_id"], {}).get("action")
                        not in {"converted", "payload_retired", "needs_review"}
                        for it in doc["items"])
    return {"cohort": cohort, "scheduled": scheduled,
            "pending_jobs": pending, "remaining": remaining,
            "admitted": len(done), "expired": False}


def mark_item_done(ledger, cohort: str, mid: int, action: str,
                   **detail) -> None:
    """Record a bounded conversion outcome without changing manifest scope."""
    if action not in {"converted", "needs_review"}:
        raise ValueError("cohort_action_invalid")
    _begin(ledger)
    with ledger.db:
        doc = _cohort(ledger, cohort)
        if not _valid_cohort(doc) or type(mid) is not int \
                or not any(it["message_id"] == mid for it in doc["items"]):
            raise ValueError("cohort_item_invalid")
        _item_receipt(ledger, cohort, mid, action, **detail)


def retire_payloads(ledger, cohort: str, now: float | None = None) -> dict:
    """Hold physical cleanup until a separate verified recovery manifest exists.

    A conversion cohort names messages, not the exact generated artifact ids
    that may be destroyed. The source row and a v4 PASS receipt also do not
    prove that old QC/delivery references were migrated or recovery tested.
    No current caller supplies that separate cleanup contract, so this entry
    point preserves all payloads and reports the missing prerequisite.
    """
    now = time.time() if now is None else now
    if not _time_value(now):
        raise ValueError("cohort_time_invalid")
    doc = _cohort(ledger, cohort)
    if doc is None:
        return {"cohort": cohort, "error": "cohort_unknown"}
    if not _valid_cohort(doc):
        return {"cohort": cohort, "error": "cohort_invalid"}
    if doc.get("retire_after_at") is None or doc["retire_after_at"] > now:
        return {"cohort": cohort, "retired": 0,
                "held": "retire_after_at_not_reached"}
    return {"cohort": cohort, "retired": [],
            "held": "cleanup_manifest_and_recovery_required"}
