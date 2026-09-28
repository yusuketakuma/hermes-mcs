"""v4 publication, stage receipts, and fail-closed legacy migration.

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
- ``v4_retire_cohort`` / ``v4_retire_item`` persist fixed conversion
  targets, scheduling reservations, and hold reasons. New inference
  stays closed until the adapters can enforce the total token ceiling.
  Physical cleanup additionally needs a separate verified recovery
  manifest; a conversion receipt never authorizes payload deletion.
"""
from __future__ import annotations

import json
import math
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
    publication boundary).

    A receipt identical (apart from ``ts``) to the latest one for the
    same generation and stage adds no information and is skipped — a
    job waiting on a durable resource outage re-walks S0–S2 every drain
    without consuming attempts, and must not append rows forever."""
    receipt = {"stage": stage, "status": status, **detail}
    prev = ledger.db.execute(
        "SELECT content FROM artifacts WHERE kind=? AND message_id=? "
        "AND json_extract(meta,'$.stage')=? "
        "AND json_extract(meta,'$.fingerprint')=? "
        "AND json_extract(meta,'$.policy_fingerprint')=? "
        "ORDER BY artifact_id DESC LIMIT 1",
        (KIND_V4_STAGE, mid, stage, fp, policy)).fetchone()
    if prev is not None:
        try:
            last = json.loads(prev["content"])
        except (json.JSONDecodeError, TypeError):
            last = None
        if isinstance(last, dict):
            last.pop("ts", None)
            if last == receipt:
                return
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
        if isinstance(meta, dict) and isinstance(content, dict) \
                and meta.get("fingerprint") == fp:
            out.append({"stage": meta.get("stage"), **content})
    return out


def current_v4(ledger, mid: int, content_hash: str):
    """Newest valid published v4 row bound to the CURRENT source hash
    — mirrors ``current_projection_pred`` (error-free, hash-current,
    not invalidated) for the v4 kind."""
    from mcs_queries import current_v4_pred
    row = ledger.db.execute(
        "SELECT a.artifact_id,a.content,a.meta FROM artifacts a "
        "JOIN messages m ON m.message_id=a.message_id "
        "AND m.project_id=a.project_id WHERE a.kind=? "
        "AND a.message_id=? AND m.content_hash=? AND "
        + current_v4_pred("a", "m.content_hash")
        + " ORDER BY a.artifact_id DESC LIMIT 1",
        (KIND_V4, mid, content_hash)).fetchone()
    return ({"artifact_id": row["artifact_id"],
             "content": json.loads(row["content"]),
             "meta": json.loads(row["meta"])} if row else None)


def publish(ledger, pid: int, mid: int, fp: str, policy: str,
            member: dict, v2_doc: dict) -> int | None:
    """Atomically mint the PASS-only v4 row (S8). MUST be called inside
    the caller's ``with ledger.db`` block so the read model switch and
    the status receipt commit together. Idempotent on doc_hash — a
    replayed PASS for the same audited document reuses the row — unless
    it was minted by another projection version, which a new row
    supersedes."""
    from semantic_projection import PROJECTION_VERSION, project_v2_doc_legacy
    doc_hash = _doc_hash(v2_doc)
    existing = current_v4(ledger, mid, member["revision"])
    if existing is not None \
            and existing["meta"].get("doc_hash") == doc_hash \
            and existing["meta"].get("projection_version") \
            == PROJECTION_VERSION:
        return existing["artifact_id"]
    import semantic
    content = project_v2_doc_legacy(v2_doc)
    ledger.artifact_add_tx(
        KIND_V4, json.dumps(content, ensure_ascii=False,
                            allow_nan=False),
        project_id=pid, message_id=mid, model=semantic.llm_model(),
        meta={"fingerprint": fp, "policy_fingerprint": policy,
              "schema": 2, "hash": member["revision"],
              "extract_version": ENGINE_VERSION,
              "engine_version": ENGINE_VERSION,
              "fact_source": "canonical",
              "doc_hash": doc_hash,
              "projection_version": PROJECTION_VERSION})
    row = ledger.db.execute(
        "SELECT MAX(artifact_id) FROM artifacts WHERE kind=? "
        "AND message_id=?", (KIND_V4, mid)).fetchone()
    return row[0] if row else None


# Rows re-projected per tick — the pass is a pure function of stored
# documents (no model call), but it still writes one artifact per row
# and must never crowd the shared run lock's deadline.
REPROJECT_LIMIT = 25


def _reproject_doc(ledger, mid: int, meta: dict):
    """The audited v2 document a current projection/v4 row was rendered
    from, or ``(None, reason)``. Selected exactly the way the drain
    selects it (the newest ``semantic_facts_v2`` row of the generation's
    fingerprint) and accepted only when it is still the document the row
    binds (``doc_hash``), its coverage is complete, and the stored fact
    audit of that generation PASSed it."""
    from semantic_policy import KIND_FACT_AUDIT, KIND_FACTS_V2
    from semantic_store import _current
    fp, policy = meta.get("fingerprint"), meta.get("policy_fingerprint")
    doc_hash = meta.get("doc_hash")
    if not (isinstance(fp, str) and isinstance(policy, str)
            and isinstance(doc_hash, str)):
        return None, "unbound_meta"
    prev_v2 = _current(ledger, KIND_FACTS_V2, mid, fp)
    doc = prev_v2["content"] if prev_v2 else None
    try:
        ok = doc is not None and _doc_hash(doc) == doc_hash
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        return None, "doc_unavailable"
    if not isinstance(doc.get("coverage"), dict) \
            or doc["coverage"].get("status") != "complete":
        return None, "coverage_incomplete"
    audit = _current(ledger, KIND_FACT_AUDIT, mid, fp, policy)
    if audit is None or audit["meta"].get("doc_hash") != doc_hash \
            or not audit["content"].get("evaluated") \
            or audit["content"].get("status") != "PASS":
        return None, "audit_not_pass"
    return doc, None


def reproject_stale(ledger, scfg: dict,
                    limit: int = REPROJECT_LIMIT) -> dict:
    """Bounded deterministic re-projection (no model call): supersede
    CURRENT ``canonical_projection`` / ``semantic_facts_v4`` rows whose
    ``meta.projection_version`` is missing or older than
    ``PROJECTION_VERSION`` with a row rendered by the current
    ``project_v2_doc_legacy`` from the same stored audited document.

    Only a row the read side would select right now is touched (the
    shared ``current_projection_id``/``current_v4_id`` predicates:
    hash-current, error-free, not invalidated, newest) on a message whose
    body is not deleted, so invalidated, superseded and non-PASS
    generations (which never have a v4 row) are never resurrected. The
    new row copies the old binding meta verbatim — fingerprint, policy,
    hash, doc_hash, engine/extract version — and only advances
    ``projection_version`` (plus a ``reprojected_from`` lineage id);
    readers pick it as the newest ``artifact_id`` and the drain's own
    reuse checks accept it. A row whose document cannot be re-derived
    safely is marked ``reproject_skipped`` (so it cannot starve the
    bound) and keeps serving until its message is drained again. Run it
    after ``invalidate_projections`` in the same tick."""
    from mcs_queries import current_projection_id, current_v4_id
    from semantic_policy import KIND_FACT_PROJ
    from semantic_projection import PROJECTION_VERSION, project_v2_doc_legacy
    out = {"reprojected": 0, "skipped": 0, "skip_reasons": {}}
    # not gated on the current fact_source: rows published while the
    # source was canonical stay readable (current_fact_pred) after a
    # switch back to shadow/legacy, and each row is re-derived only from
    # its own bound fingerprint/policy/doc_hash
    if scfg.get("mode") == "off" or limit < 1:
        return out
    rows = ledger.db.execute(
        "SELECT a.artifact_id,a.kind,a.project_id,a.message_id,a.model,"
        "a.meta FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id AND m.project_id=a.project_id "
        "WHERE a.kind IN (?,?) AND m.body_state IS NOT 'deleted' "
        "AND a.artifact_id=CASE WHEN a.kind=? "
        f"THEN {current_projection_id('m')} ELSE {current_v4_id('m')} END "
        "AND COALESCE(json_extract(a.meta,'$.projection_version'),0)<? "
        "AND COALESCE(json_extract(a.meta,'$.reproject_skipped'),0)<? "
        "ORDER BY a.artifact_id LIMIT ?",
        (KIND_FACT_PROJ, KIND_V4, KIND_FACT_PROJ, PROJECTION_VERSION,
         PROJECTION_VERSION, limit)).fetchall()
    for row in rows:
        meta = json.loads(row["meta"])
        doc, reason = _reproject_doc(ledger, row["message_id"], meta)
        with ledger.db:
            if doc is None:
                ledger.db.execute(
                    "UPDATE artifacts SET meta=json_set(meta,"
                    "'$.reproject_skipped',?) WHERE artifact_id=?",
                    (PROJECTION_VERSION, row["artifact_id"]))
                out["skipped"] += 1
                out["skip_reasons"][reason] = \
                    out["skip_reasons"].get(reason, 0) + 1
                continue
            new_meta = dict(meta)
            new_meta.pop("reproject_skipped", None)
            new_meta["projection_version"] = PROJECTION_VERSION
            new_meta["reprojected_from"] = row["artifact_id"]
            ledger.artifact_add_tx(
                row["kind"], json.dumps(project_v2_doc_legacy(doc),
                                        ensure_ascii=False,
                                        allow_nan=False),
                project_id=row["project_id"],
                message_id=row["message_id"], model=row["model"] or "",
                meta=new_meta)
            # same rollup contract as invalidate_projections: the
            # patient's rollup is rebuilt from the new current row
            ledger.db.execute(
                "DELETE FROM artifacts WHERE kind='patient_rollup' "
                "AND project_id=?", (row["project_id"],))
        out["reprojected"] += 1
    return out


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

def _time_value(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


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
    if not _time_value(doc.get("expires_at")):
        return False
    if doc.get("retire_after_at") is not None \
            and not _time_value(doc["retire_after_at"]):
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


def _begin(ledger):
    # Read decisions and reservations share a write transaction. Refuse a
    # caller-owned transaction instead of accidentally committing it.
    if ledger.db.in_transaction:
        raise ValueError("cohort_transaction_active")
    ledger.db.execute("BEGIN IMMEDIATE")


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
            bundle = thread_bundle(ledger, row["project_id"], mid, [mid])
            fixed.append({"message_id": mid, "project_id": row["project_id"],
                          "content_hash": row["content_hash"],
                          "source_fingerprint": bundle["source_fingerprint"]})
        doc["items"] = fixed
        ledger.artifact_add_tx(
            KIND_V4_COHORT, json.dumps(doc, ensure_ascii=False, allow_nan=False),
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
    """Latest item receipts, including durable scheduling reservations."""
    done = {}
    for r in ledger.db.execute(
            "SELECT content FROM artifacts WHERE kind=? "
            "ORDER BY artifact_id", (KIND_V4_ITEM,)):
        try:
            doc = json.loads(r["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(doc, dict) and doc.get("cohort") == cohort \
                and type(doc.get("message_id")) is int:
            done[doc["message_id"]] = doc
    return done


def active_legacy_admissions(ledger, now: float | None = None) -> set:
    """Conversion cohorts schedule v4 work and never authorize v3 inference."""
    return set()


def _source_current(ledger, item: dict) -> bool:
    from semantic_store import thread_bundle
    row = ledger.db.execute(
        "SELECT project_id,content_hash FROM messages WHERE message_id=?",
        (item["message_id"],)).fetchone()
    if (row is None or row["content_hash"] != item["content_hash"]
            or row["project_id"] != item.get("project_id")):
        return False
    bundle = thread_bundle(ledger, row["project_id"], item["message_id"],
                           [item["message_id"]])
    return bundle is not None and bundle["source_fingerprint"] == \
        item.get("source_fingerprint")


def _item_receipt(ledger, cohort, mid, action, **detail):
    ledger.artifact_add_tx(
        KIND_V4_ITEM,
        json.dumps({**detail, "cohort": cohort, "message_id": mid,
                    "action": action}, ensure_ascii=False, allow_nan=False),
        project_id=0, message_id=mid, meta={"cohort": cohort})


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
        from semantic_policy import JOB_KIND
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
            ledger._job_add_tx(
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
    _begin(ledger)
    with ledger.db:
        from semantic_runtime import JobToken, RuntimeStale, job_matches
        if not job_matches(ledger, JobToken.from_row(job)):
            raise RuntimeStale("cohort_admission")
        cohort = origin.get("cohort")
        doc = _cohort(ledger, cohort)
        if _valid_cohort(doc) and any(
                it["message_id"] == job["message_id"] for it in doc["items"]):
            prev = cohort_items_done(ledger, cohort).get(job["message_id"], {})
            if prev.get("reason") != "token_budget_not_enforceable":
                _item_receipt(ledger, cohort, job["message_id"],
                              "needs_review", reason="token_budget_not_enforceable")
    return True


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
