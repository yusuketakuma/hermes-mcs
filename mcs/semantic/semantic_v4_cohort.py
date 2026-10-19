"""Own bounded conversion manifests, reservations, receipts and fail-closed admission.

The operator entry points and runtime pre-dispatch hold use the same durable
cohort contract. Conversion never grants legacy inference or authorizes payload
cleanup; total token enforcement and verified recovery remain prerequisites.
Readers borrow SQLite connections; mutations use the caller's actual writer
and retain one transaction for decisions, jobs and receipt reservations.
"""
from __future__ import annotations

import json
import time

from mcs_util import loads_dict
from semantic_policy import JOB_KIND
from semantic_v4 import ENGINE_VERSION
from stats_math import finite_number

KIND_V4_COHORT = "v4_retire_cohort"
KIND_V4_ITEM = "v4_retire_item"


def _valid_cohort(doc) -> bool:
    if not isinstance(doc, dict) or doc.get("contract") != "mcs-v4-cohort/1":
        return False
    name, items, caps = (doc.get(k) for k in ("cohort", "items", "ceilings"))
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        return False
    if not isinstance(items, list) or not isinstance(caps, dict):
        return False
    if any(type(caps.get(k)) is not int or not 0 <= caps[k] <= 2**63 - 1
           for k in ("items", "calls", "tokens", "retries")):
        return False
    if not finite_number(doc.get("expires_at")):
        return False
    if doc.get("retire_after_at") is not None \
            and not finite_number(doc["retire_after_at"]):
        return False
    mids = set()
    for item in items:
        if (not isinstance(item, dict)
                or type(item.get("message_id")) is not int
                or item["message_id"] <= 0
                or item["message_id"] in mids
                or not isinstance(item.get("content_hash"), str)
                or not item["content_hash"]):
            return False
        mids.add(item["message_id"])
    return True


def _begin(db):
    # Read decisions and reservations share a write transaction. Refuse a
    # caller-owned transaction instead of accidentally committing it.
    if db.in_transaction:
        raise ValueError("cohort_transaction_active")
    db.execute("BEGIN IMMEDIATE")


def cohort_document(db, cohort: str) -> dict | None:
    for r in db.execute(
            "SELECT content FROM artifacts WHERE kind=? "
            "ORDER BY artifact_id DESC", (KIND_V4_COHORT,)):
        doc = loads_dict(r["content"])
        if isinstance(doc, dict) and doc.get("cohort") == cohort:
            return doc
    return None


def cohort_items_done(db, cohort: str) -> dict:
    """Latest item receipts, including durable scheduling reservations."""
    done = {}
    for r in db.execute(
            "SELECT content FROM artifacts WHERE kind=? "
            "ORDER BY artifact_id", (KIND_V4_ITEM,)):
        doc = loads_dict(r["content"])
        if isinstance(doc, dict) and doc.get("cohort") == cohort \
                and type(doc.get("message_id")) is int:
            done[doc["message_id"]] = doc
    return done


def active_legacy_admissions(ledger, now: float | None = None) -> set:
    """Conversion cohorts schedule v4 work and never authorize v3 inference."""
    return set()


def _item_receipt(ledger, cohort, mid, action, **detail):
    source = ledger.db.execute(
        "SELECT project_id FROM messages WHERE message_id=?", (mid,)).fetchone()
    ledger.artifact_add_tx(
        KIND_V4_ITEM,
        json.dumps({**detail, "cohort": cohort, "message_id": mid,
                    "action": action}, ensure_ascii=False, allow_nan=False),
        project_id=source["project_id"] if source is not None else None,
        message_id=mid if source is not None else None, meta={"cohort": cohort})


def hold_unbounded_job(ledger, job) -> bool:
    """Fail closed before model work when total token use cannot be bounded.

    The current Jev request contract has no pre-dispatch token cap. Stored
    numbers alone cannot enforce a lifetime token budget. Preserve the item
    and explicit reason until a bounded adapter can satisfy that contract.
    Ordinary semantic jobs are outside this migration-only gate.
    """
    from semantic_runtime import parse_payload
    payload = parse_payload(job)
    origin = payload.get("origin")
    if not isinstance(origin, dict) or origin.get("source") != "v4_cohort":
        return False
    _begin(ledger.db)
    with ledger.db:
        from semantic_runtime import JobToken, RuntimeStale, job_matches
        if not job_matches(ledger, JobToken.from_row(job)):
            raise RuntimeStale("cohort_admission")
        cohort = origin.get("cohort")
        doc = cohort_document(ledger.db, cohort)
        if _valid_cohort(doc) and any(
                it["message_id"] == job["message_id"] for it in doc["items"]):
            prev = cohort_items_done(ledger.db, cohort).get(job["message_id"], {})
            if prev.get("reason") != "token_budget_not_enforceable":
                _item_receipt(ledger, cohort, job["message_id"],
                              "needs_review", reason="token_budget_not_enforceable")
    return True


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
    _begin(ledger.db)
    with ledger.db:
        existing = cohort_document(ledger.db, cohort)
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
    if not finite_number(now):
        raise ValueError("cohort_time_invalid")
    _begin(ledger.db)
    with ledger.db:
        doc = cohort_document(ledger.db, cohort)
        if doc is None:
            return {"cohort": cohort, "error": "cohort_unknown"}
        if not _valid_cohort(doc):
            return {"cohort": cohort, "error": "cohort_invalid"}
        if doc["expires_at"] <= now:
            return {"cohort": cohort, "error": "cohort_expired",
                    "scheduled": 0, "remaining": 0}
        done = cohort_items_done(ledger.db, cohort)
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
    _begin(ledger.db)
    with ledger.db:
        doc = cohort_document(ledger.db, cohort)
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
    if not finite_number(now):
        raise ValueError("cohort_time_invalid")
    doc = cohort_document(ledger.db, cohort)
    if doc is None:
        return {"cohort": cohort, "error": "cohort_unknown"}
    if not _valid_cohort(doc):
        return {"cohort": cohort, "error": "cohort_invalid"}
    if doc.get("retire_after_at") is None or doc["retire_after_at"] > now:
        return {"cohort": cohort, "retired": 0,
                "held": "retire_after_at_not_reached"}
    return {"cohort": cohort, "retired": [],
            "held": "cleanup_manifest_and_recovery_required"}
