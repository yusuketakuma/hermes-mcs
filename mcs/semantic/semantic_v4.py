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
from mcs_util import loads_dict

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
    from mcs_queries import json_object_or_null
    receipt = {"stage": stage, "status": status, **detail}
    # json_extract on a malformed meta row raises; the guarded document
    # is NULL for such rows, so they are skipped (fail-open).
    meta_doc = json_object_or_null("meta")
    prev = ledger.db.execute(
        "SELECT content FROM artifacts WHERE kind=? AND message_id=? "
        f"AND json_extract({meta_doc},'$.stage')=? "
        f"AND json_extract({meta_doc},'$.fingerprint')=? "
        f"AND json_extract({meta_doc},'$.policy_fingerprint')=? "
        "ORDER BY artifact_id DESC LIMIT 1",
        (KIND_V4_STAGE, mid, stage, fp, policy)).fetchone()
    if prev is not None:
        last = loads_dict(prev["content"])
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
        meta, content = loads_dict(r["meta"]), loads_dict(r["content"])
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
    if row is None:
        return None
    content, meta = loads_dict(row["content"]), loads_dict(row["meta"])
    return ({"artifact_id": row["artifact_id"], "content": content, "meta": meta}
            if content is not None and meta is not None else None)


def stage_request_following(ledger, bundle, doc, *, policy):
    """Persist a current-source candidate; never publish, change Loop IDs or mint grants."""
    import hashlib
    import semantic
    import semantic_facts as sf
    from semantic_projection import project_v2_doc_legacy, project_v2_facts

    clean = sf.validate_facts_doc(doc)
    current = semantic.thread_bundle(ledger, bundle["project_id"], bundle["root_id"])
    if current is None or current["source_fingerprint"] != bundle["source_fingerprint"]:
        raise sf.ContractError("request_following_source_stale")
    source = clean["source"]
    member = next((m for m in current["members"]
                   if str(m["message_id"]) == source["message_id"]), None)
    if member is None or member["body_state"] == "deleted" \
            or source["revision"] != member["revision"] \
            or source["source_fingerprint"] != current["source_fingerprint"] \
            or source["content_quality"] != (
                "full" if member["body_state"] == "full" else "partial") \
            or source["body_codepoints"] != len(member["body_original"]) \
            or source["content_hash"] != hashlib.sha256(
                member["body_original"].encode("utf-8")).hexdigest():
        raise sf.ContractError("request_following_source_mismatch")
    if any(member["body_original"][e["start"]:e["end"]] != e["quote"]
           for e in clean["evidence"]):
        raise sf.ContractError("request_following_evidence_mismatch")
    if not isinstance(policy, str) or not policy.strip():
        raise sf.ContractError("request_following_policy_required")
    candidate = {
        "source": clean["source"], "source_doc_hash": _doc_hash(doc),
        "doc_hash": _doc_hash(clean),
        "projection": project_v2_doc_legacy(clean, request_details=True),
        "loop_facts": project_v2_facts(clean, request_details=True),
        "promotion_ready": False,
        "required_promotion_gates": [
            "request_detail_audit", "loop_identity_owner_decision",
            "extraction_generation_owner_decision", "human_200_g6", "calibration"],
    }
    record_stage(ledger, current["project_id"], member["message_id"], current["source_fingerprint"],
                 policy, "request_following_candidate", "PENDING", candidate=candidate)
    return candidate


def extract_request_following(llm_fn, ledger, bundle, message_id, *, policy,
                              deadline=None, chunk_size=3000, retry_coverage=False):
    """Extract into a separate candidate generation and source-bound PENDING staging."""
    import semantic
    import semantic_facts as sf
    from semantic_extraction import extract_facts_v2

    if not isinstance(policy, str) or not policy.strip():
        raise sf.ContractError("request_following_policy_required")
    current = semantic.thread_bundle(ledger, bundle["project_id"], bundle["root_id"])
    if current is None or current["source_fingerprint"] != bundle["source_fingerprint"]:
        raise sf.ContractError("request_following_source_stale")
    member = next((m for m in current["members"] if m["message_id"] == message_id), None)
    if member is None or member["body_state"] == "deleted":
        raise sf.ContractError("request_following_source_mismatch")
    result = extract_facts_v2(
        llm_fn, member, deadline, ledger=ledger, project_id=current["project_id"],
        source_fingerprint=current["source_fingerprint"], chunk_size=chunk_size,
        retry_coverage=retry_coverage, request_following=True)
    candidate = (stage_request_following(ledger, current, result["doc"], policy=policy)
                 if result["extraction_complete"] else None)
    record_stage(
        ledger, current["project_id"], member["message_id"], current["source_fingerprint"],
        policy, "request_following_extraction", "PENDING",
        generation=result["generation"], extraction_complete=result["extraction_complete"],
        doc_hash=_doc_hash(result["doc"]))
    return {"extraction": result, "candidate": candidate}


def publish(ledger, pid: int, mid: int, fp: str, policy: str,
            member: dict, v2_doc: dict) -> int | None:
    """Atomically mint the PASS-only v4 row (S8). MUST be called inside
    the caller's ``with ledger.db`` block so the read model switch and
    the status receipt commit together. Idempotent on doc_hash — a
    replayed PASS for the same audited document reuses the row — unless
    it was minted by another projection version, which a new row
    supersedes."""
    from semantic_projection import (PROJECTION_VERSION,
                                      project_v2_doc_legacy,
                                      projection_current)
    from semantic_audit import _published_quantity_findings
    if _published_quantity_findings(v2_doc):
        return None
    doc_hash = _doc_hash(v2_doc)
    existing = current_v4(ledger, mid, member["revision"])
    if existing is not None and projection_current(existing["meta"], doc_hash):
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


def reproject_doc(ledger, mid: int, meta: dict):
    """The audited v2 document a current projection/v4 row was rendered
    from, or ``(None, reason)``. Selected exactly the way the drain
    selects it (the newest ``semantic_facts_v2`` row of the generation's
    fingerprint) and accepted only when it is still the document the row
    binds (``doc_hash``), its coverage is complete, and the stored fact
    audit of that generation PASSed it (``fact_audit_verdict``)."""
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
    from semantic_audit import _published_quantity_findings
    if _published_quantity_findings(doc):
        return None, "quantity_unverified"
    if fact_audit_verdict(_current(ledger, KIND_FACT_AUDIT, mid, fp, policy),
                          doc_hash) != "PASS":
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
    bound); an old-version row stays outside the current read model. Run it
    after ``invalidate_projections`` in the same tick."""
    from mcs_queries import current_projection_id, current_v4_id
    from semantic_policy import KIND_FACT_PROJ
    from semantic_projection import PROJECTION_VERSION, project_v2_doc_legacy
    out = {"reprojected": 0, "skipped": 0, "skip_reasons": {}}
    # run_due first invalidates rows while fact_source is noncanonical,
    # hiding them from readers and this pass. On canonical return it
    # revives only current, PASS rows expired for that switch; this pass
    # then re-derives them from their own fingerprint/policy/doc_hash.
    if scfg.get("mode") == "off" or limit < 1:
        return out
    rows = ledger.db.execute(
        "SELECT a.artifact_id,a.kind,a.project_id,a.message_id,a.model,"
        "a.meta FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id AND m.project_id=a.project_id "
        "WHERE a.kind IN (?,?) AND m.body_state IS NOT 'deleted' "
        "AND a.artifact_id=CASE WHEN a.kind=? "
        f"THEN {current_projection_id('m', require_version=False)} ELSE {current_v4_id('m', require_version=False)} END "
        # CASE, not AND: the subquery term above is evaluated last, so a
        # bare json_extract here would raise on a malformed meta row.
        "AND CASE WHEN json_valid(a.meta) AND json_type(a.meta)='object' "
        "THEN COALESCE(json_extract(a.meta,'$.projection_version'),0)<? "
        "AND COALESCE(json_extract(a.meta,'$.reproject_skipped'),0)<? END "
        "ORDER BY a.artifact_id LIMIT ?",
        (KIND_FACT_PROJ, KIND_V4, KIND_FACT_PROJ, PROJECTION_VERSION,
         PROJECTION_VERSION, limit)).fetchall()
    reinterpreted = {}
    for row in rows:
        meta = loads_dict(row["meta"])
        content, doc = None, None
        try:
            doc, reason = (reproject_doc(ledger, row["message_id"], meta)
                           if meta is not None
                           else (None, "metadata_unreadable"))
            if doc is not None:
                content = json.dumps(project_v2_doc_legacy(doc),
                                     ensure_ascii=False, allow_nan=False)
        except Exception:
            # a stored v2 doc of the wrong shape (KeyError/unhashable)
            # must be marked skipped, or it stays first in the ORDER BY
            # and blocks every later row forever
            reason = "projection_failed"
        with ledger.db:
            if content is None:
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
                row["kind"], content,
                project_id=row["project_id"],
                message_id=row["message_id"], model=row["model"] or "",
                meta=new_meta)
            # same rollup contract as invalidate_projections: the
            # patient's rollup is rebuilt from the new current row
            ledger.db.execute(
                "DELETE FROM artifacts WHERE kind='patient_rollup' "
                "AND project_id=?", (row["project_id"],))
        out["reprojected"] += 1
        if doc is not None:
            reinterpreted.setdefault(
                (row["project_id"], row["message_id"]), (doc, meta))
    for (pid, mid), (doc, meta) in reinterpreted.items():
        _reinterpret_loops(ledger, scfg, pid, mid, doc, meta, out)
    used = _rebuild_hint_docs(ledger, scfg, limit - len(rows), out)
    _hold_upgraded_repairs(ledger, scfg, limit - len(rows) - used, out)
    return out


# A fact entry's provenance text; a non-object entry or non-text value
# reads as '' (same operand guard as mcs_queries.JSON_OBJECT_SQL).
_FACT_OBJECT = "CASE WHEN f.type='object' THEN f.value ELSE '{}' END"
_PROVENANCE = (f"CASE WHEN json_type({_FACT_OBJECT},'$.provenance')='text' "
               f"THEN json_extract({_FACT_OBJECT},'$.provenance') ELSE '' END")

_HELD_KINDS = ("semantic_summary", "semantic_audit", "semantic_coverage",
               "semantic_facts_audit", "loop_candidate")


def _count(out: dict, key: str) -> None:
    out["skip_reasons"][key] = out["skip_reasons"].get(key, 0) + 1


def _member_bundle(ledger, pid: int, mid: int, fp):
    """(bundle, member) of the message's live thread, or (None, None)
    when the stored generation ``fp`` is no longer the live one."""
    from semantic_store import thread_bundle
    row = ledger.db.execute(
        "SELECT COALESCE(parent_id,message_id) r FROM messages "
        "WHERE project_id=? AND message_id=?", (pid, mid)).fetchone()
    bundle = thread_bundle(ledger, pid, row["r"]) if row else None
    if bundle is None or bundle["source_fingerprint"] != fp:
        return None, None
    member = next((m for m in bundle["members"] if m["message_id"] == mid), None)
    return (bundle, member) if member is not None else (None, None)


def _reinterpret_loops(ledger, scfg, pid, mid, doc, meta, out) -> None:
    """Loop candidates follow the current projection of the same audited
    document: items it no longer yields stop being current (no model)."""
    from semantic_policy import policy_fingerprint
    from semantic_projection import project_v2_facts
    if scfg.get("loop_mode", "off") == "off" \
            or meta.get("policy_fingerprint") != policy_fingerprint(scfg):
        return
    bundle, _ = _member_bundle(ledger, pid, mid, meta.get("fingerprint"))
    if bundle is None:
        return
    from semantic_loops import update_loops
    try:
        update_loops(ledger, pid, bundle, {mid: project_v2_facts(doc)}, None,
                     scfg, time.monotonic() + 30)
    except Exception:
        _count(out, "loop_reinterpret_failed")


def _hold_derivatives(ledger, pid: int, mid: int, fp: str,
                      reason: str) -> None:
    """Stop everything derived from a stored canonical document that is no
    longer trusted from being current: every projection of the message's
    generation, and the generation's summary/audit/coverage/fact audit and Loop
    candidates. Rows stay as history; a summary is also marked stale so
    the send gate holds any queued notice. Caller holds the transaction."""
    from semantic_policy import KIND_FACT_PROJ
    # Every publication of this message's generation, not only the one bound
    # to the held document: readers pick the newest non-invalidated row, so
    # an older document's row of the same generation would otherwise become
    # current again. Other generations, messages and patients stay as they are.
    ledger.db.execute(
        "UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true'),"
        "'$.invalidated_reason',?) WHERE project_id=? AND message_id=? "
        "AND kind IN (?,?) AND CASE WHEN json_valid(meta) "
        "AND json_type(meta)='object' THEN json_extract(meta,'$.fingerprint')=? "
        "AND json_extract(meta,'$.invalidated') IS NOT 1 END",
        (reason, pid, mid, KIND_FACT_PROJ, KIND_V4, fp))
    held = ("WHERE project_id=? AND message_id=? AND kind IN ({}) "
            "AND CASE WHEN json_valid(meta) AND json_type(meta)='object' "
            "THEN json_extract(meta,'$.fingerprint')=? "
            "AND json_extract(meta,'$.invalidated') IS NOT 1 END")
    # the summary first: its stale mark is what the send gate reads
    ledger.db.execute(
        "UPDATE artifacts SET meta=json_set(meta,'$.stale',json('true')) "
        + held.format("?"), (pid, mid, "semantic_summary", fp))
    ledger.db.execute(
        "UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true'),"
        "'$.invalidated_reason',?) " + held.format(",".join("?" * len(_HELD_KINDS))),
        (reason, pid, mid, *_HELD_KINDS, fp))
    ledger.db.execute(
        "DELETE FROM artifacts WHERE kind='patient_rollup' AND project_id=?",
        (pid,))


def _seed_rebuild(ledger, scfg, pid: int, mid: int, meta: dict):
    """Queue one notification-free re-run of the message's thread unless
    the generation is parked/failed (no new retry budget) or queued.
    Runs inside the caller's transaction."""
    if meta.get("needs_review"):
        return "needs_review"
    if meta.get("repaired"):
        # re-running would re-audit the unrepaired base under a spent
        # repair: the held thread waits for an explicit manual retry
        return "repaired"
    scope = scfg.get("project_ids")
    if scope is not None and pid not in scope:
        return "out_of_scope"
    row = ledger.db.execute(
        "SELECT COALESCE(parent_id,message_id) r FROM messages "
        "WHERE project_id=? AND message_id=?", (pid, mid)).fetchone()
    state = ledger.job_state("semantic", pid, row["r"]) if row else None
    if state in ("failed", "pending"):
        return f"job_{state}"
    ledger._semantic_seed_tx(pid, [mid], {"source": "hint_rebuild",
                                          "notification_free": True})
    return None


def _rebuild_hint_docs(ledger, scfg: dict, limit: int, out: dict) -> None:
    """Bounded pass over current semantic_facts_v2 documents whose hint
    merge predates the evidence-span rule (``FACTS_V2_BUILD``).

    Each is re-derived from its cached chunks with no model/Jev call and
    marked once (``hint_rebuild``) so it never re-enters the pass:
    ``same`` keeps every derived row as is; ``replaced`` stores the new
    document and holds the old one's derivatives; ``skipped:<reason>``
    (unrebuildable) holds them too. A rebuilt or unrebuildable thread is
    re-queued at most once, never when failed/parked/repaired. Only for
    ``fact_source=canonical``, where the document feeds every consumer."""
    from semantic_extraction import FACTS_V2_BUILD, rebuild_facts_v2_cached
    from semantic_facts import ContractError, validate_facts_doc
    from semantic_policy import KIND_FACTS_V2
    stats = {"same": 0, "replaced": 0, "skipped": 0, "history": 0, "seeded": 0}
    # shadow/legacy summaries and Loops come from legacy facts, not from
    # this document: holding them by fingerprint would touch unrelated rows
    if limit < 1 or scfg.get("fact_source") != "canonical":
        return 0
    rows = ledger.db.execute(
        "SELECT a.artifact_id,a.project_id,a.message_id,a.model,a.content,"
        "a.meta FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id AND m.project_id=a.project_id "
        "WHERE a.kind=? AND m.body_state IS NOT 'deleted' "
        "AND a.artifact_id=(SELECT MAX(b.artifact_id) FROM artifacts b "
        "WHERE b.kind=a.kind AND b.message_id=a.message_id) "
        # nested CASE: every JSON operand is proven parseable before it is
        # read, whatever order SQLite evaluates AND terms in
        "AND CASE WHEN json_valid(a.meta) AND json_valid(a.content) THEN "
        "CASE WHEN json_type(a.meta)='object' "
        "AND json_type(a.content,'$.facts')='array' "
        "THEN json_extract(a.meta,'$.build') IS NULL "
        "AND json_extract(a.meta,'$.hint_rebuild') IS NULL "
        f"AND EXISTS(SELECT 1 FROM json_each(a.content,'$.facts') f "
        f"WHERE instr({_PROVENANCE},'extract_v1')>0 "
        f"AND instr({_PROVENANCE},'+')>0) END END "
        "ORDER BY a.artifact_id LIMIT ?", (KIND_FACTS_V2, limit)).fetchall()

    def mark(row, value):
        ledger.db.execute(
            "UPDATE artifacts SET meta=json_set(meta,'$.hint_rebuild',?) "
            "WHERE artifact_id=?", (value, row["artifact_id"]))

    for row in rows:
        pid, mid = row["project_id"], row["message_id"]
        meta, doc = loads_dict(row["meta"]) or {}, loads_dict(row["content"])
        fp = meta.get("fingerprint")
        new, reason = None, None
        try:
            _, member = _member_bundle(ledger, pid, mid, fp)
            if member is None:
                reason = "history"   # not the live generation: never reused
            elif meta.get("repaired"):
                reason = "repaired"  # cannot be re-derived from cached chunks
            else:
                old = json.dumps(validate_facts_doc(doc), sort_keys=True)
                new, reason = rebuild_facts_v2_cached(member, ledger, fp, pid,
                                                      doc, meta)
                if new is not None and old == json.dumps(
                        validate_facts_doc(new), sort_keys=True):
                    new, reason = None, "same"
        except (ContractError, KeyError, TypeError, ValueError):
            new, reason = None, "document_invalid"
        except Exception:
            # anything else still marks the row, or it would stay first in
            # the ORDER BY and block every later document every tick
            new, reason = None, "rebuild_failed"
        if reason in ("history", "same"):
            with ledger.db:
                if reason == "same":
                    ledger.db.execute(
                        "UPDATE artifacts SET meta=json_set(meta,'$.build',?) "
                        "WHERE artifact_id=?", (FACTS_V2_BUILD, row["artifact_id"]))
                mark(row, reason)
            stats[reason] += 1
            continue
        # The new document, the mark, the hold and the re-queue commit
        # together. Loop candidates are re-derived by the drain once the
        # rebuilt document passes its audit, never from an unaudited one.
        try:
            with ledger.db:
                if new is not None:
                    new_meta = {k: v for k, v in meta.items()
                                if k != "hint_rebuild"}
                    new_meta.update(
                        build=FACTS_V2_BUILD, rebuilt_from=row["artifact_id"],
                        coverage_status=new["coverage"]["status"],
                        facts=len(new["facts"]),
                        open_obligations=len(new["coverage"]["open_obligation_ids"]))
                    ledger.artifact_add_tx(
                        KIND_FACTS_V2, json.dumps(new, ensure_ascii=False,
                                                  allow_nan=False),
                        project_id=pid, message_id=mid,
                        model=row["model"] or "", meta=new_meta)
                    mark(row, "replaced")
                else:
                    mark(row, f"skipped:{reason}")
                _hold_derivatives(ledger, pid, mid, fp, "hint_rebuild")
                held = _seed_rebuild(ledger, scfg, pid, mid, meta)
        except Exception:
            # nothing above is half-applied; still hold the old document's
            # derivatives and mark it so it cannot block later rows
            new, reason, held = None, "write_failed", "write_failed"
            with ledger.db:
                mark(row, "skipped:write_failed")
                _hold_derivatives(ledger, pid, mid, fp, "hint_rebuild")
        if new is not None:
            stats["replaced"] += 1
        else:
            stats["skipped"] += 1
            _count(out, f"hint_rebuild:{reason}")
        if held is None:
            stats["seeded"] += 1
        else:
            _count(out, f"hint_rebuild_seed:{held}")
    if any(stats.values()):
        # reported only when the pass did something: an idle tick keeps
        # the reproject result unchanged
        out["hint_rebuild"] = stats
    return len(rows)


def _upgraded_obligations(ledger, row, meta, doc) -> tuple[str, list]:
    """Prove which obligations a pre-fix repair turned from an adjudicated
    absence into coverage: ``("held", ids)``, ``("clear", [])`` or
    ``("unproven:<why>", [])``. The proof is the generation's completed
    repair receipt and the stored pre-repair document it names, both
    earlier than the repaired document; both documents must satisfy the
    canonical contract and bind this message, revision and generation.
    Anything missing, invalid or ambiguous is never asserted — a broken
    candidate is never skipped in favour of another one."""
    from semantic_facts import ContractError, validate_facts_doc
    from semantic_policy import KIND_FACT_REPAIR, KIND_FACTS_V2
    fp, mid, rid = meta.get("fingerprint"), row["message_id"], row["artifact_id"]

    def bound(candidate):
        """The contract-normalised document when it binds this generation."""
        clean = validate_facts_doc(candidate)
        source = clean["source"]
        if source["message_id"] != str(mid) or source["source_fingerprint"] != fp:
            raise ContractError("contract:repair_proof_unbound")
        return clean

    try:
        repaired = bound(doc)
    except (ContractError, KeyError, TypeError, ValueError):
        return ("unproven:repaired_document", [])
    hashes = set()
    for r in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? AND message_id=? "
            "AND project_id=? AND artifact_id<?",
            (KIND_FACT_REPAIR, mid, row["project_id"], rid)):
        rmeta, content = loads_dict(r["meta"]), loads_dict(r["content"])
        if rmeta and rmeta.get("fingerprint") == fp and content \
                and content.get("repaired") is True:
            hashes.add(rmeta.get("doc_hash"))
    if len(hashes) != 1 or not isinstance(next(iter(hashes)), str):
        return ("unproven:receipt", [])
    base_hash = hashes.pop()
    bases = []
    for r in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? AND message_id=? "
            "AND project_id=? AND artifact_id<?",
            (KIND_FACTS_V2, mid, row["project_id"], rid)):
        bmeta, base = loads_dict(r["meta"]), loads_dict(r["content"])
        if not bmeta or bmeta.get("fingerprint") != fp or bmeta.get("repaired"):
            continue
        try:
            if _doc_hash(base) != base_hash:
                continue
        except (KeyError, TypeError):
            continue
        try:
            clean = bound(base)
        except (ContractError, KeyError, TypeError, ValueError):
            return ("unproven:base_document", [])
        if (clean["source"]["revision"], clean["source"]["content_hash"]) != \
                (repaired["source"]["revision"], repaired["source"]["content_hash"]):
            return ("unproven:base_document", [])
        bases.append({o["obligation_id"]: o["status"] for o in clean["obligations"]})
    if not bases or any(b != bases[0] for b in bases):
        return ("unproven:base_document", [])
    upgraded = [o["obligation_id"] for o in repaired["obligations"]
                if o["status"] == "covered"
                and bases[0].get(o["obligation_id"]) == "explicit_no_fact"]
    return ("held", upgraded) if upgraded else ("clear", [])


def _hold_upgraded_repairs(ledger, scfg: dict, limit: int, out: dict) -> None:
    """Bounded check of current repaired canonical documents stored before
    a repair stopped turning an adjudicated absence into coverage.

    Each is checked once (``repair_checked``). A proven upgrade holds the
    generation exactly like an unrebuildable hint document — derivatives
    stop being current and the document is never reused, with no model or
    Jev call, no re-queue and no new repair; it waits for a manual retry.
    A plain repaired document, or one whose proof is missing or
    contradictory, is left as it is."""
    if limit < 1 or scfg.get("fact_source") != "canonical":
        return
    from semantic_policy import KIND_FACTS_V2
    rows = ledger.db.execute(
        "SELECT a.artifact_id,a.project_id,a.message_id,a.content,a.meta "
        "FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id AND m.project_id=a.project_id "
        "WHERE a.kind=? AND m.body_state IS NOT 'deleted' "
        "AND a.artifact_id=(SELECT MAX(b.artifact_id) FROM artifacts b "
        "WHERE b.kind=a.kind AND b.message_id=a.message_id) "
        "AND CASE WHEN json_valid(a.meta) THEN CASE WHEN json_type(a.meta)='object' "
        "THEN json_extract(a.meta,'$.repaired') IS 1 "
        "AND json_extract(a.meta,'$.repair_checked') IS NULL "
        "AND json_extract(a.meta,'$.hint_rebuild') IS NULL END END "
        "ORDER BY a.artifact_id LIMIT ?", (KIND_FACTS_V2, limit)).fetchall()
    for row in rows:
        meta, doc = loads_dict(row["meta"]) or {}, loads_dict(row["content"]) or {}
        try:
            verdict, _ = _upgraded_obligations(ledger, row, meta, doc)
        except Exception:
            verdict = "unproven:check_failed"
        with ledger.db:
            ledger.db.execute(
                "UPDATE artifacts SET meta=json_set(meta,'$.repair_checked',?) "
                "WHERE artifact_id=?", (verdict, row["artifact_id"]))
            if verdict == "held":
                # the drain refuses a skipped document: never reused again
                ledger.db.execute(
                    "UPDATE artifacts SET meta=json_set(meta,'$.hint_rebuild',"
                    "'skipped:repair_absence') WHERE artifact_id=?",
                    (row["artifact_id"],))
                _hold_derivatives(ledger, row["project_id"], row["message_id"],
                                  meta.get("fingerprint"), "repair_absence")
        _count(out, f"repair_absence:{verdict}")


def _doc_hash(v2_doc: dict) -> str:
    from mcs_requests import payload_hash
    return payload_hash({"f": v2_doc["facts"], "e": v2_doc["evidence"]})


def fact_audit_verdict(audit, doc_hash: str) -> str | None:
    """Completed fact-audit status for the v2 document ``doc_hash``, or
    None when it has none. ``audit`` is the generation's NEWEST
    ``semantic_facts_audit`` row for the policy (``semantic_store.
    _current(..., KIND_FACT_AUDIT, mid, fp, policy)``), exactly the row
    the drain reuses (C04) — never an older one: a newer row bound to
    another document or recording an unevaluated run (``evaluated``
    not ``True``) means the document is not audited as it stands, and
    the drain re-audits it rather than resurrecting an earlier verdict.
    The drain never appends an audit after a completed one for the same
    document, so in its own ledger the newest row is the only verdict.
    ``content.status`` is the verdict the drain acted on
    (``meta.audit_status`` is its denormalized copy for status reads).
    Shared by the drain's reuse check, the re-projection gate and the
    evaluation lifecycle's ``verified`` stage."""
    if audit is None or audit["meta"].get("doc_hash") != doc_hash \
            or audit["content"].get("evaluated") is not True:
        return None
    return audit["content"].get("status")


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
            project_id=None, message_id=None,
            meta={"cohort": cohort, "engine_version": ENGINE_VERSION})
    return doc


def _cohort(ledger, cohort: str) -> dict | None:
    for r in ledger.db.execute(
            "SELECT content FROM artifacts WHERE kind=? "
            "ORDER BY artifact_id DESC", (KIND_V4_COHORT,)):
        doc = loads_dict(r["content"])
        if isinstance(doc, dict) and doc.get("cohort") == cohort:
            return doc
    return None


def cohort_items_done(ledger, cohort: str) -> dict:
    """Latest item receipts, including durable scheduling reservations."""
    done = {}
    for r in ledger.db.execute(
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
    source = ledger.db.execute(
        "SELECT project_id FROM messages WHERE message_id=?", (mid,)).fetchone()
    ledger.artifact_add_tx(
        KIND_V4_ITEM,
        json.dumps({**detail, "cohort": cohort, "message_id": mid,
                    "action": action}, ensure_ascii=False, allow_nan=False),
        project_id=source["project_id"] if source is not None else None,
        message_id=mid if source is not None else None, meta={"cohort": cohort})


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
