"""v4 engine boundary (T18): stage receipts, PASS-only publication,
diagnostics and bounded retirement of legacy-generated payloads.

The canonical chain in semantic_drain already walks S0–S7 (prep →
extract_facts_v2 → local validation + audit_facts_v2 → one reserved
repair → re-audit → projection → summary → claim audit). This module
mints that single self-correcting pipeline as engine v4:

- ``v4_stage`` receipts record every stage transition with
  ``engine_version=4``, the source fingerprint, the policy fingerprint
  and exact dependency ids (doc_hash) — the durable S0–S8 ledger a
  reviewer can replay.
- ``semantic_facts_v4`` is the PASS-only read model: v3-shaped content
  plus ``extract_version=4`` and full fact/evidence/doc_hash lineage.
  It is a DIFFERENT artifact kind, so the legacy ``_replace_current``
  DELETE (``kind='extract_llm'``) can never reach it, and
  ``current_fact_pred`` prefers it over every older generation — a
  delayed v3 writer can land but can never displace a published v4 row.
- ``v4_diagnostic`` carries non-PASS findings (PENDING/NEEDS_REVIEW/
  STALE) — a separate kind no ordinary extraction reader selects.
- ``v4_retire_cohort`` / ``v4_retire_item`` implement the finite,
  restart-safe conversion manifests: fixed item lists, item/call
  ceilings, expiry, and a persisted cursor that date changes and
  restarts cannot replenish.
"""
from __future__ import annotations

import json
import time

ENGINE_VERSION = 4
KIND_V4 = "semantic_facts_v4"
KIND_V4_STAGE = "v4_stage"
KIND_V4_DIAG = "v4_diagnostic"
KIND_V4_COHORT = "v4_retire_cohort"
KIND_V4_ITEM = "v4_retire_item"

STAGES = ("s0_prep", "s1_extract", "s2_fact_audit", "s3_repair",
          "s4_reaudit", "s5_projection", "s6_summary",
          "s7_summary_audit", "s8_publish")
FINAL_STATUSES = ("PASS", "PENDING", "NEEDS_REVIEW", "STALE")


def record_stage(ledger, pid: int, mid: int, fp: str, policy: str,
                 stage: str, status: str, tx: bool = False,
                 **detail) -> None:
    """One durable stage receipt. Fail-open by contract of the caller —
    a receipt write error must never abort the pipeline itself.
    ``tx=True`` inside a caller-held ``with ledger.db`` block (the
    non-tx variant commits immediately and would break the atomic
    publication boundary)."""
    add = ledger.artifact_add_tx if tx else ledger.artifact_add
    add(
        KIND_V4_STAGE,
        json.dumps({"stage": stage, "status": status,
                    "ts": time.time(), **detail},
                   ensure_ascii=False, allow_nan=False),
        project_id=pid, message_id=mid,
        meta={"fingerprint": fp, "policy_fingerprint": policy,
              "stage": stage, "engine_version": ENGINE_VERSION})


def stage_ledger(ledger, mid: int, fp: str) -> list:
    """Ordered S0–S8 receipts for one generation — the replayable
    evidence that repair was reserved before dispatch, audits were
    evaluated, and publication only followed an all-PASS chain."""
    out = []
    for r in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? "
            "AND message_id=? ORDER BY artifact_id",
            (KIND_V4_STAGE, mid)):
        try:
            meta = json.loads(r["meta"] or "{}")
            content = json.loads(r["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if meta.get("fingerprint") == fp:
            out.append({"stage": meta.get("stage"), **content})
    return out


def current_v4(ledger, mid: int, content_hash: str):
    """Newest valid published v4 row bound to the CURRENT source hash
    — mirrors ``current_projection_pred`` (error-free, hash-current,
    not invalidated) for the v4 kind."""
    for r in ledger.db.execute(
            "SELECT artifact_id,content,meta FROM artifacts "
            "WHERE kind=? AND message_id=? ORDER BY artifact_id DESC",
            (KIND_V4, mid)):
        try:
            meta = json.loads(r["meta"] or "{}")
            content = json.loads(r["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(meta, dict) or not isinstance(content, dict):
            continue
        if meta.get("error") in (True, 1) or content.get("_error"):
            continue
        if meta.get("invalidated"):
            continue
        if content_hash is not None and meta.get("hash") == content_hash:
            return {"artifact_id": r["artifact_id"],
                    "content": content, "meta": meta}
        break
    return None


def publish(ledger, pid: int, mid: int, fp: str, policy: str,
            member: dict, v2_doc: dict) -> int | None:
    """Atomically mint the PASS-only v4 row (S8). MUST be called inside
    the caller's ``with ledger.db`` block so the read model switch and
    the status receipt commit together. Idempotent on doc_hash — a
    replayed PASS for the same audited document reuses the row."""
    doc_hash = _doc_hash(v2_doc)
    existing = current_v4(ledger, mid, member["revision"])
    if existing is not None \
            and existing["meta"].get("doc_hash") == doc_hash:
        return existing["artifact_id"]
    from semantic_projection import project_v2_doc_legacy
    import semantic
    content = project_v2_doc_legacy(v2_doc)
    ledger.artifact_add_tx(
        KIND_V4, json.dumps(content, ensure_ascii=False,
                            allow_nan=False),
        project_id=pid, message_id=mid, model=semantic.LLM_MODEL,
        meta={"fingerprint": fp, "policy_fingerprint": policy,
              "schema": 2, "hash": member["revision"],
              "extract_version": ENGINE_VERSION,
              "engine_version": ENGINE_VERSION,
              "fact_source": "canonical",
              "doc_hash": doc_hash})
    row = ledger.db.execute(
        "SELECT MAX(artifact_id) FROM artifacts WHERE kind=? "
        "AND message_id=?", (KIND_V4, mid)).fetchone()
    return row[0] if row else None


def _doc_hash(v2_doc: dict) -> str:
    from mcs_requests import payload_hash
    return payload_hash({"f": v2_doc["facts"], "e": v2_doc["evidence"]})


def diagnostic(ledger, pid: int, mid: int, fp: str, policy: str,
               status: str, findings: list, doc_hash=None,
               tx: bool = False) -> None:
    """A non-PASS outcome lives ONLY as a diagnostic receipt — the
    separate kind guarantees no extraction reader can ever select it
    as current content. Idempotent per (fp, status, doc_hash): a
    retried drain must not stack identical rows. ``tx=True`` inside a
    caller-held ``with ledger.db`` block."""
    from semantic_store import _current
    prev = _current(ledger, KIND_V4_DIAG, mid, fp, policy)
    if prev is not None \
            and prev["content"].get("status") == status \
            and prev["content"].get("doc_hash") == doc_hash \
            and prev["content"].get("findings") == findings:
        return
    add = ledger.artifact_add_tx if tx else ledger.artifact_add
    add(
        KIND_V4_DIAG,
        json.dumps({"status": status, "findings": findings,
                    "target_message_id": mid, "doc_hash": doc_hash},
                   ensure_ascii=False, allow_nan=False),
        project_id=pid, message_id=mid,
        meta={"fingerprint": fp, "policy_fingerprint": policy,
              "engine_version": ENGINE_VERSION, "audit_status": status,
              "diagnostic": True})


# ---------- finite conversion manifests (legacy retirement) ----------

def declare_cohort(ledger, cohort: str, items: list[dict],
                   ceilings: dict, expires_at: float,
                   retire_after_at: float | None = None) -> dict:
    """Persist a FIXED, FINITE retirement cohort. `items` binds each
    entry to an exact source revision (message_id + content_hash at
    declaration) — a cohort can never silently grow, and `expires_at`
    bounds how long it stays admissible. Ceilings: items, calls,
    tokens, retries."""
    existing = _cohort(ledger, cohort)
    if existing is not None:
        return existing
    doc = {"contract": "mcs-v4-cohort/1", "cohort": cohort,
           "items": items, "ceilings": {
               "items": int(ceilings.get("items", len(items))),
               "calls": int(ceilings.get("calls", len(items) * 4)),
               "tokens": int(ceilings.get("tokens", 0)),
               "retries": int(ceilings.get("retries", 1))},
           "expires_at": expires_at,
           "retire_after_at": retire_after_at,
           "created_at": time.time()}
    ledger.artifact_add(
        KIND_V4_COHORT, json.dumps(doc, ensure_ascii=False,
                                   allow_nan=False),
        project_id=0, message_id=0,
        meta={"cohort": cohort, "engine_version": ENGINE_VERSION})
    return doc


def _cohort(ledger, cohort: str) -> dict | None:
    for r in ledger.db.execute(
            "SELECT content FROM artifacts WHERE kind=? "
            "ORDER BY artifact_id DESC", (KIND_V4_COHORT,)):
        try:
            doc = json.loads(r["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(doc, dict) and doc.get("cohort") == cohort:
            return doc
    return None


def cohort_items_done(ledger, cohort: str) -> dict:
    """Persisted per-item receipts — the restart-safe cursor. An item
    is done exactly once, on the day it is processed; a date change or
    a process restart cannot grant it another slot."""
    done = {}
    for r in ledger.db.execute(
            "SELECT content FROM artifacts WHERE kind=? "
            "ORDER BY artifact_id", (KIND_V4_ITEM,)):
        try:
            doc = json.loads(r["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(doc, dict) and doc.get("cohort") == cohort:
            done[doc.get("message_id")] = doc
    return done


def active_legacy_admissions(ledger, now: float | None = None) -> set:
    """Message ids the legacy engine may still touch: union of items
    in non-expired cohorts. Outside a manifest the default v3-engine
    new-inference admission is ZERO (T18)."""
    now = time.time() if now is None else now
    ids = set()
    for r in ledger.db.execute(
            "SELECT content FROM artifacts WHERE kind=?",
            (KIND_V4_COHORT,)):
        try:
            doc = json.loads(r["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(doc, dict) or doc.get("cohort") is None:
            continue
        if (doc.get("expires_at") or 0) <= now:
            continue
        for it in doc.get("items") or []:
            if isinstance(it, dict) and isinstance(
                    it.get("message_id"), int):
                ids.add(it["message_id"])
    return ids


def run_cohort(ledger, cohort: str, now: float | None = None) -> dict:
    """Admit the next bounded slice of a conversion cohort as semantic
    (v4) jobs — never extract_llm work, and never more than the
    manifest's item ceiling minus items already receipted."""
    now = time.time() if now is None else now
    doc = _cohort(ledger, cohort)
    if doc is None:
        return {"cohort": cohort, "error": "cohort_unknown"}
    if (doc.get("expires_at") or 0) <= now:
        return {"cohort": cohort, "error": "cohort_expired",
                "scheduled": 0, "remaining": 0}
    done = cohort_items_done(ledger, cohort)
    ceiling = doc["ceilings"]["items"]
    scheduled = 0
    pending = 0
    from semantic_policy import JOB_KIND
    for it in doc.get("items") or []:
        mid = it.get("message_id")
        if mid in done:
            continue
        row = ledger.db.execute(
            "SELECT project_id,content_hash FROM messages "
            "WHERE message_id=?", (mid,)).fetchone()
        if row is None or (it.get("content_hash")
                           and row["content_hash"] != it["content_hash"]):
            # source moved or vanished under the declared fingerprint —
            # receipt it as needs_review, never convert a shifted source
            ledger.artifact_add(
                KIND_V4_ITEM,
                json.dumps({"cohort": cohort, "message_id": mid,
                            "action": "needs_review",
                            "reason": "source_changed"}),
                project_id=0, message_id=mid,
                meta={"cohort": cohort})
            continue
        if scheduled >= ceiling:
            pending += 1
            continue
        existing = ledger.db.execute(
            "SELECT 1 FROM fetch_jobs WHERE kind=? AND project_id=? "
            "AND message_id=? AND state='pending'",
            (JOB_KIND, row["project_id"], mid)).fetchone()
        if existing is None:
            ledger.job_add(JOB_KIND, row["project_id"], mid,
                           payload={"targets": [mid],
                                    "origin": {"source": "v4_cohort",
                                               "cohort": cohort}})
            scheduled += 1
        else:
            pending += 1
    remaining = len([it for it in doc.get("items") or []
                     if it.get("message_id") not in done])
    return {"cohort": cohort, "scheduled": scheduled,
            "pending_jobs": pending, "remaining": remaining,
            "expired": False}


def mark_item_done(ledger, cohort: str, mid: int, action: str,
                   **detail) -> None:
    ledger.artifact_add(
        KIND_V4_ITEM,
        json.dumps({"cohort": cohort, "message_id": mid,
                    "action": action, **detail}, ensure_ascii=False),
        project_id=0, message_id=mid, meta={"cohort": cohort})


def retire_payloads(ledger, cohort: str, now: float | None = None) -> dict:
    """Physically overwrite OLD LLM-GENERATED payloads for cohort items
    — only after EVERY gate passes: the cohort's retire_after_at is in
    the past, the item's conversion receipted, a current v4 PASS row
    covers the source hash, no extract_qc audit and no outbox/delivery
    row still references the artifact, and the source message is
    intact (the verified recovery route). extract_v1 is rule-derived
    and NEVER touched; tombstones keep artifact_id/meta lineage so
    historical QC source ids stay resolvable."""
    now = time.time() if now is None else now
    doc = _cohort(ledger, cohort)
    if doc is None:
        return {"cohort": cohort, "error": "cohort_unknown"}
    if (doc.get("retire_after_at") or float("inf")) > now:
        return {"cohort": cohort, "retired": 0,
                "held": "retire_after_at_not_reached"}
    done = cohort_items_done(ledger, cohort)
    retired, held = [], []
    for it in doc.get("items") or []:
        mid = it.get("message_id")
        receipt = done.get(mid)
        if receipt is None or receipt.get("action") not in (
                "converted", "payload_retired"):
            held.append({"message_id": mid,
                         "reason": "conversion_not_receipted"})
            continue
        v4 = current_v4(ledger, mid, it.get("content_hash"))
        if v4 is None:
            held.append({"message_id": mid,
                         "reason": "no_current_v4"})
            continue
        item_retired = []
        for r in ledger.db.execute(
                "SELECT artifact_id,content,meta FROM artifacts "
                "WHERE message_id=? AND kind IN "
                "('extract_llm','canonical_projection') "
                "ORDER BY artifact_id", (mid,)):
            try:
                old_content = json.loads(r["content"] or "{}")
            except (json.JSONDecodeError, TypeError):
                old_content = {}
            if isinstance(old_content, dict) \
                    and old_content.get("_tombstone"):
                continue
            # a QC audit or a delivery row that still references this
            # artifact id pins the payload — held, never deleted
            ref = ledger.db.execute(
                "SELECT 1 FROM artifacts q WHERE q.kind='extract_qc' "
                "AND json_valid(q.meta) "
                "AND json_extract(q.meta,'$.source_artifact_id')=? "
                "LIMIT 1", (r["artifact_id"],)).fetchone()
            if ref is not None:
                held.append({"message_id": mid,
                             "artifact_id": r["artifact_id"],
                             "reason": "qc_reference"})
                continue
            tomb = {"_tombstone": True,
                    "retired_artifact_id": r["artifact_id"],
                    "retired_at": now, "cohort": cohort,
                    "replaced_by": v4["artifact_id"]}
            ledger.db.execute(
                "UPDATE artifacts SET content=? WHERE artifact_id=?",
                (json.dumps(tomb, ensure_ascii=False),
                 r["artifact_id"]))
            retired.append(r["artifact_id"])
            item_retired.append(r["artifact_id"])
        mark_item_done(ledger, cohort, mid, "payload_retired",
                       retired_ids=item_retired)
    ledger.db.commit()
    return {"cohort": cohort, "retired": retired, "held": held}
