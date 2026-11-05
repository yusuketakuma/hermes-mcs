"""Produce and validate semantic fact stages against current source evidence."""
from __future__ import annotations
import semantic_facts
import mcs_util
import semantic_audit
import semantic_extraction
import semantic_store
import semantic_transport
import json
import hashlib
import math
import sqlite3
import time
from mcs_util import strict_json
from inference_version import (CURRENT_VERSION, VERSION_KEY)
import semantic_jev as jev
import semantic_runtime as runtime
import semantic_publication as v4
from semantic_policy import (KIND_ASSESS, KIND_FACT_AUDIT, KIND_FACT_REPAIR, KIND_FACTS, KIND_FACTS_V2,
                             KIND_SHADOW_V2, SCHEMA_VERSION, policy_fingerprint)
from semantic_store import current_artifact

from semantic_summary_stages import (
    _park_needs_review,
    _shadow_v2_generation,
    _shadow_v2_tried,
)

def _fact_stage(ledger, scfg, member, pid, mid, fp, policy,
                jev_client, llm_fn, deadline, *, shadow_stats: dict | None = None,
                jev_skip: str | None = None, activate_skip=None, skip_state=None):
    """Fact stage for one bundle member.

    fact_source selects the canonical path: "shadow" still feeds
    consumers from the legacy extractor while writing the v2 document
    for comparison; "canonical" makes the v2 document the fact source —
    extract -> bidirectional audit -> one bounded repair -> legacy
    projection — and refuses to proceed while it stays incomplete; it
    never silently falls back to legacy.

    Returns {"outcome": None, "facts": [...], "v2_doc": doc|None} on
    success.  outcome "incomplete"|"retryable"|"hard_fail" asks the
    caller to break the member loop with the matching failure flag set.
    """
    # facts: reuse a stored set for this fingerprint, else extract.
    fact_source = scfg.get("fact_source", "legacy")
    v2_doc = None
    if fact_source != "legacy":
        early, v2_doc, doc_build = _fact_v2_document(
            ledger, member, pid, mid, fp, policy, fact_source,
            jev_client, llm_fn, deadline, shadow_stats=shadow_stats)
        if early is not None:
            return early
    if fact_source == "canonical":
        early, v2_doc, doc_hash = _fact_canonical_audit(
            ledger, scfg, member, pid, mid, fp, policy, fact_source,
            v2_doc, doc_build, jev_client, llm_fn, deadline, jev_skip=jev_skip,
            activate_skip=activate_skip, skip_state=skip_state)
        if early is not None:
            return early
        from semantic_projection import project_v2_facts
        facts = project_v2_facts(v2_doc)
        if jev_skip is None and not (skip_state and skip_state.get("reason")):
            v4.publish_fact_projection(ledger, member, pid, mid, fp, policy,
                                       v2_doc, doc_hash)
    else:
        early, facts = _fact_legacy(ledger, member, pid, mid, fp, policy,
                                    llm_fn, deadline)
        if early is not None:
            return early
    return {"outcome": None, "facts": facts, "v2_doc": v2_doc}


def _fact_v2_document(ledger, member, pid, mid, fp, policy, fact_source,
                      jev_client, llm_fn, deadline, *, shadow_stats: dict | None = None):
    """Reuse or extract the v2 fact document for a non-legacy fact source.

    Returns (early, v2_doc, doc_build): early is the _fact_stage result to
    return as-is, else None with the document (None when not stored) and
    the hint-merge build it carries."""
    from semantic_extraction import extract_facts_v2
    import semantic_projection  # noqa: F401  loaded before any stage write
    v2_doc = None
    prev_v2 = current_artifact(ledger.db, KIND_FACTS_V2, mid, fp)
    prev_v2, spent, coverage_retry = semantic_facts.cached_facts_plan(
        prev_v2, member, mid, fp, fact_source)
    execution_contract = semantic_transport.inference_contract()
    if prev_v2 is not None and not coverage_retry and execution_contract is not None:
        if prev_v2['meta'].get('repaired'):
            previous = v4._repaired_projection_producer(
                ledger.db, pid, mid, fp, policy, member, prev_v2['content'], prev_v2['meta'])
            if (previous is None or any(item.get('contract') != execution_contract for item in previous['base'])):
                # Historical publication stays intact; do not revive a spent repair.
                return {"outcome": "incomplete", "v2_doc": None}, None, None
        else:
            from semantic_extraction import rebuild_facts_v2_cached
            producer, contracts = {}, []
            rebuilt, reason = rebuild_facts_v2_cached(member, ledger.db, fp, pid, prev_v2['content'],
                prev_v2['meta'], producer_out=producer, contracts_out=contracts)
            if (reason is not None or rebuilt is None or not producer.get('complete')
                    or mcs_util.payload_hash(rebuilt) != mcs_util.payload_hash(prev_v2['content'])):
                return {"outcome": "incomplete", "v2_doc": None}, None, None
            if any(item.get('contract') != execution_contract for item in contracts):
                prev_v2 = None
    if fact_source == "canonical":
        # S0: deterministic prep (extract_v1 hints + atom/chunk
        # boundaries inside extract_facts_v2) — no model call
        v4.record_stage(ledger, pid, mid, fp, policy,
                        "s0_prep", "done")
    # the hint-merge build of the document this run continues from
    doc_build = None
    if prev_v2 is not None and not coverage_retry:
        v2_doc = prev_v2["content"]
        doc_build = prev_v2["meta"].get("build")
        if fact_source == "canonical":
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s1_extract", "reused")
    elif fact_source == "shadow" and _shadow_v2_tried(
            ledger, mid, fp, policy):
        # this source + policy already ran its shadow comparison and it
        # stayed incomplete (receipt below) — a retry does not re-run it
        pass
    else:
        if time.monotonic() > deadline - 5:
            return {"outcome": "incomplete", "v2_doc": None}, None, None
        if fact_source == "canonical":
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s1_extract", "start")
        started = time.monotonic()
        shadow = shadow_stats if fact_source == "shadow" else None
        complete = False
        try:
            v2_result = extract_facts_v2(
                llm_fn, member, deadline - 5, ledger=ledger,
                source_fingerprint=fp, project_id=pid,
                jev_client=jev_client, retry_coverage=coverage_retry or spent)
            complete = v2_result["extraction_complete"]
        finally:
            if shadow is not None:      # a raised call ended incomplete too
                shadow["calls"] += 1
                shadow["incomplete"] += 0 if complete else 1
                shadow["s"] += time.monotonic() - started
        if not v2_result["extraction_complete"]:
            if fact_source == "canonical":
                return {"outcome": "retryable"
                        if v2_result["failure_reason"] == "model"
                        else "incomplete", "v2_doc": None}, None, None
            v2_doc = None      # shadow: incomplete doc not stored
            if v2_result["failure_reason"] != "deadline":
                # Without a durable mark every retry re-extracted each
                # finished target until a multi-target job could no
                # longer reach the next one (2026-10-10). The receipt
                # says "incomplete", never complete; a time-out instead
                # resumes from the persisted chunks next pass.
                ledger.artifact_add(
                    KIND_SHADOW_V2,
                    json.dumps({"status": "incomplete",
                                "failure_reason":
                                    v2_result["failure_reason"],
                                # codes/counts only: which "model" stop
                                # it was (LLM failure code, or facts
                                # dropped by the chunk's validation)
                                "llm_failure": semantic_transport.last_llm_failure(),
                                "dropped_in_failed_chunk":
                                    v2_result.get("dropped_in_failed_chunk"),
                                "dropped_reasons":
                                    v2_result.get("dropped_reasons"),
                                "null_contexts_omitted":
                                    v2_result.get("null_contexts_omitted"),
                                "chunks_completed":
                                    v2_result["chunks_completed"],
                                "chunks_total":
                                    v2_result["chunks_total"]}),
                    project_id=pid, message_id=mid,
                    model=semantic_transport.llm_model(),
                    meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp,
                          "policy_fingerprint": policy,
                          "generation": _shadow_v2_generation()})
        else:
            v2_doc = v2_result["doc"]
            from semantic_extraction import FACTS_V2_BUILD
            doc_build = FACTS_V2_BUILD
            meta = {VERSION_KEY: CURRENT_VERSION, "fingerprint": fp,
                    "policy_fingerprint": policy,
                    "schema": SCHEMA_VERSION,
                    "fact_source": fact_source,
                    "coverage_status":
                        v2_doc["coverage"]["status"],
                    "facts": len(v2_doc["facts"]),
                    "open_obligations": len(
                        v2_doc["coverage"]
                        ["open_obligation_ids"]),
                    "build": FACTS_V2_BUILD,
                    # newly normalised this run (reused chunks excluded)
                    "null_contexts_omitted":
                        v2_result.get("null_contexts_omitted", 0)}
            producer = v2_result.get("producer_evidence", {})
            meta["provider_provenance"] = {
                "version": 1, "stage": "facts", "providers": producer.get("providers", []),
                "complete": producer.get("complete") is True,
                "result_hash": mcs_util.payload_hash(v2_doc), "source_fingerprint": fp,
                "policy_fingerprint": policy, "target_revision": member["revision"]}
            if coverage_retry or spent:
                # the bounded retry already ran — a still-incomplete
                # doc is flagged for human review and never
                # re-extracted again (C03)
                meta["coverage_retry"] = True
                if v2_doc["coverage"]["status"] != "complete":
                    meta["needs_review"] = True
            if fact_source == "canonical":
                v4.record_stage(ledger, pid, mid, fp, policy,
                                "s1_extract", "done",
                                facts=len(v2_doc["facts"]))
            ledger.artifact_add(
                KIND_FACTS_V2,
                json.dumps(v2_doc, ensure_ascii=False,
                           allow_nan=False),
                project_id=pid, message_id=mid,
                model=semantic_transport.llm_model(),
                meta={**meta, "inference_requests": semantic_transport.inference_requests()})
    if fact_source == "canonical" and v2_doc is not None \
            and v2_doc["coverage"]["status"] != "complete":
        if coverage_retry or spent \
                or (prev_v2 and prev_v2["meta"].get("coverage_retry")):
            return _park_needs_review(
                ledger, pid, mid, fp, policy, v2_doc), None, None
        return {"outcome": "incomplete", "v2_doc": v2_doc}, None, None
    return None, v2_doc, doc_build


def _fact_canonical_audit(ledger, scfg, member, pid, mid, fp, policy,
                          fact_source, v2_doc, doc_build, jev_client,
                          llm_fn, deadline, *, jev_skip=None, activate_skip=None, skip_state=None):
    """POST_JEV bidirectional fact audit with its one bounded repair.

    Returns (early, v2_doc, doc_hash): early is the _fact_stage result to
    return as-is, else None with the audited document and its hash."""
    fail_kind = "incomplete"
    # POST_JEV: bidirectional audit must pass before the
    # canonical fact set feeds any consumer.  The audit artifact
    # is keyed by doc_hash, so a repaired document is always
    # re-audited; the repair receipt bounds dispatch to one
    # attempt per generation.  Findings hold the generation —
    # never promoted or silently dropped.
    audited = False
    for _attempt in range(2):
        doc_hash = semantic_facts.doc_hash(v2_doc)
        prev_fa = current_artifact(ledger.db, KIND_FACT_AUDIT, mid, fp, policy)
        # C04: only a COMPLETED evaluation is a reusable audit — a
        # stored evaluated:false row (mid-audit outage) must be
        # re-run, otherwise a transient failure pins the generation
        # on a failed verdict forever and manual retry cannot clear it
        fact_audit = semantic_audit.cached_fact_audit(prev_fa, v2_doc, doc_hash)
        if fact_audit is None:
            if time.monotonic() > deadline - 5:
                break
            from semantic_audit import audit_facts_v2
            jev_outcome = "unknown"
            if jev_skip is not None:
                if prev_fa is None:
                    jev_outcome = "skipped"
                fact_audit = semantic_audit.optional_fact_audit(
                    v2_doc, member, fp, jev_skip, prev_fa["content"] if prev_fa else None,
                    match_threshold=scfg["match_threshold"])
            else:
                before_requests = getattr(jev_client, "requests_made", None)
                fact_audit = audit_facts_v2(
                    jev_client, v2_doc, member["body_original"],
                    deadline - 5,
                    match_threshold=scfg["match_threshold"],
                    per_request=scfg.get("max_questions_per_request", 12))
                after_requests = getattr(jev_client, "requests_made", None)
                sent = (type(before_requests) is int and type(after_requests) is int
                        and after_requests > before_requests)
                if sent and fact_audit.get("evaluated") is True:
                    jev_outcome = "done"
                elif sent and fact_audit.get("fact_verdicts"):
                    jev_outcome = "partial"
                reason = semantic_audit.jev_skip_reason(getattr(jev_client, "last_error", None))
                if not fact_audit["evaluated"] and reason is not None and activate_skip is not None:
                    if not sent and prev_fa is None:
                        jev_outcome = "skipped"
                    activate_skip(reason)
                    jev_skip = reason
                    if skip_state is not None:
                        skip_state["reason"] = reason
                    fact_audit = semantic_audit.optional_fact_audit(
                        v2_doc, member, fp, reason, fact_audit, match_threshold=scfg["match_threshold"])
            stored = {"status": fact_audit["status"],
                      "evaluated": fact_audit["evaluated"],
                      "findings": fact_audit["findings"],
                      "fact_verdicts": fact_audit["fact_verdicts"]}
            if fact_audit["status"] == "SKIPPED":
                stored.update(jev_skip_reason=jev_skip, code_checks="passed",
                              document_hash=mcs_util.payload_hash(v2_doc))
            # an unevaluated re-run identical to the stored row (a
            # durable resource wait) adds no information — record
            # an outcome once, not once per drain (FIX-SD1)
            if not (prev_fa is not None
                    and prev_fa["meta"].get("doc_hash") == doc_hash
                    and prev_fa["content"] == stored):
                ledger.artifact_add(
                    KIND_FACT_AUDIT,
                    json.dumps(stored, ensure_ascii=False,
                               allow_nan=False),
                    project_id=pid, message_id=mid,
                    model=jev.JEV_MODEL,
                    meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp,
                          "policy_fingerprint": policy,
                          "schema": SCHEMA_VERSION,
                          "doc_hash": doc_hash,
                          "audit_status": fact_audit["status"],
                          "jev_provenance": {"version": 1, "stage": "fact_audit", "outcome": jev_outcome,
                                             "result_hash": mcs_util.payload_hash(stored), "doc_hash": doc_hash,
                                             "document_hash": mcs_util.payload_hash(v2_doc)}})
        v4.record_stage(
            ledger, pid, mid, fp, policy,
            "s4_reaudit" if _attempt else "s2_fact_audit",
            fact_audit["status"] if fact_audit["evaluated"] or fact_audit["status"] == "SKIPPED"
            else "unevaluated",
            doc_hash=doc_hash)
        if jev_skip is not None and fact_audit["status"] == "SKIPPED":
            audited = True
            break
        if jev_skip is not None and fact_audit["status"] == "NEEDS_REVIEW" and not fact_audit["evaluated"]:
            return _park_needs_review(ledger, pid, mid, fp, policy, v2_doc, fact_audit["findings"]), None, None
        disposition = semantic_audit.canonical_audit_disposition(fact_audit, v2_doc)
        if disposition == "wait":
            error = getattr(jev_client, "last_error", None)
            if error is not None \
                    and jev.failure_class(error) == "failed":
                fail_kind = "hard_fail"
            elif error is None \
                    or jev.failure_class(error) != "resource":
                fail_kind = "retryable"
            break
        if disposition in ("pass", "coverage_incomplete"):
            if disposition == "coverage_incomplete":
                # defense in depth: never mint PASS over open
                # obligations, whatever the audit said
                return _park_needs_review(
                    ledger, pid, mid, fp, policy, v2_doc), None, None
            audited = True
            break
        # NEEDS_REVIEW: one targeted repair dispatch per
        # generation (receipt-persisted), then re-audit.
        rejected, coverage_missing, repair_candidate = semantic_audit.fact_repair_plan(
            fact_audit["findings"], _attempt)
        if repair_candidate \
                and current_artifact(ledger.db, KIND_FACT_REPAIR,
                             mid, fp) is None:
            if time.monotonic() > deadline - 5:
                break      # repair still possible next pass
            # S3 (T18): reserve the single repair dispatch BEFORE
            # the model call — a crash leaves the 'started'
            # receipt, which permanently consumes this generation's
            # one dispatch (no second repair run)
            original_row = ledger.db.execute(
                "SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? "
                "AND message_id=? ORDER BY artifact_id DESC LIMIT 1", (KIND_FACTS_V2, pid, mid)).fetchone()
            audit_row = ledger.db.execute(
                "SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? "
                "AND message_id=? ORDER BY artifact_id DESC LIMIT 1", (KIND_FACT_AUDIT, pid, mid)).fetchone()
            started = ledger.artifact_add(
                KIND_FACT_REPAIR,
                json.dumps({"status": "started",
                            "rejected": rejected},
                           ensure_ascii=False),
                project_id=pid, message_id=mid,
                model=semantic_transport.llm_model(),
                meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp,
                      "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION,
                      "doc_hash": doc_hash})
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s3_repair", "reserved",
                            doc_hash=doc_hash)
            from semantic_extraction import repair_facts_v2
            sent, answers = [], []

            def repair_llm(prompt):
                previous_requests = set(semantic_transport.inference_requests())
                try:
                    value = llm_fn(prompt)
                except runtime.RuntimeGuardError as e:
                    if runtime.llm_request_was_dispatched(e):
                        sent.append(True)
                    raise
                except Exception:
                    sent.append(True)
                    raise
                sent.append(True)
                receipt = semantic_transport.inference_receipt()
                contract = semantic_transport.inference_contract()
                if (isinstance(value, str) and receipt is not None and contract is not None
                        and receipt.get('reservation_id') not in previous_requests):
                    import bounded_http
                    try:
                        if len(value.encode('utf-8')) <= bounded_http.MAX_RESPONSE_BYTES:
                            answers.append({'text': value, 'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                                            'reservation_id': receipt.get('reservation_id'), 'contract': contract,
                                            'unit_key': receipt.get('unit_key'), 'owner': receipt.get('owner')})
                    except UnicodeError:
                        pass
                return value
            try:
                with semantic_transport.quality_repair():
                    repair = repair_facts_v2(
                        repair_llm, member, v2_doc, rejected, deadline - 5,
                        **({"coverage_missing": True} if coverage_missing else {}))
            except runtime.RuntimeGuardError:
                if not sent:
                    # stopped before any dispatch — the one repair
                    # was never spent, so release its reservation
                    with ledger.db:
                        ledger.db.execute(
                            "DELETE FROM artifacts WHERE artifact_id=?",
                            (started,))
                raise
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s3_repair", "completed",
                            repaired=repair["repaired"])
            evidence = None
            if repair['repaired'] and len(answers) == len(sent) and answers and original_row and audit_row:
                from inference_dependency_loss import source_binding
                try:
                    original_meta = strict_json(original_row['meta'])
                    audit_meta = strict_json(audit_row['meta'])
                    if (strict_json(original_row['content']) == v2_doc
                            and original_meta.get('fingerprint') == fp
                            and original_meta.get('policy_fingerprint') == policy
                            and audit_meta.get('doc_hash') == doc_hash
                            and audit_meta.get('fingerprint') == fp
                            and audit_meta.get('policy_fingerprint') == policy):
                        evidence = {'version': 1, 'original_artifact_id': original_row['artifact_id'],
                                    'audit_artifact_id': audit_row['artifact_id'], 'started_artifact_id': started,
                                    'source_binding': source_binding(ledger.db, pid, mid),
                                    'target_revision': member['revision'], 'original_doc_hash': doc_hash,
                                    'repaired_doc_hash': semantic_facts.doc_hash(repair['doc']),
                                    'coverage_missing': coverage_missing, 'answers': answers}
                except (TypeError, ValueError, KeyError, RecursionError):
                    pass
            completed_id = ledger.artifact_add(
                KIND_FACT_REPAIR,
                json.dumps({"rejected": rejected,
                            "repaired": repair["repaired"],
                            "repaired_fact_ids":
                                repair["repaired_fact_ids"],
                            "owner_chunk_ids":
                                repair["owner_chunk_ids"],
                            **({'__mcs_internal_proof_v1': evidence} if evidence is not None else {}),
                            # pair scan cut by the deadline: the
                            # repaired doc was not kept and the
                            # spent repair parks for review below
                            **({"relation_pairs_skipped":
                                repair["relation_pairs_skipped"]}
                               if repair.get("relation_pairs_skipped")
                               else {})},
                           ensure_ascii=False, allow_nan=False),
                project_id=pid, message_id=mid,
                model=semantic_transport.llm_model(),
                meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp,
                      "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION,
                      "doc_hash": doc_hash,
                      "repaired": repair["repaired"]})
            if repair["repaired"]:
                v2_doc = repair["doc"]
                ledger.artifact_add(
                    KIND_FACTS_V2,
                    json.dumps(v2_doc, ensure_ascii=False,
                               allow_nan=False),
                    project_id=pid, message_id=mid,
                    model=semantic_transport.llm_model(),
                    meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp,
                          "policy_fingerprint": policy,
                          "schema": SCHEMA_VERSION,
                          "fact_source": fact_source,
                          "repaired": True,
                          **({'repair_artifact_id': completed_id} if evidence is not None else {}),
                          "coverage_status":
                              v2_doc["coverage"]["status"],
                          "facts": len(v2_doc["facts"]),
                          **({"build": doc_build} if doc_build else {})})
                # the repair drops every rejected fact and reopens
                # the obligations they covered — a repaired doc with
                # open obligations must never reach PASS (rollout
                # "Nothing degrades to PASS"); the one repair is
                # spent, so the generation parks for review
                if v2_doc["coverage"]["status"] != "complete":
                    return _park_needs_review(
                        ledger, pid, mid, fp, policy, v2_doc), None, None
                continue
        # an evaluated NEEDS_REVIEW with no repair left (no per-fact
        # finding to repair, repair already spent, or still failing
        # after it) is a clinical verdict, not a technical wait —
        # park it for review instead of deferring forever
        return _park_needs_review(
            ledger, pid, mid, fp, policy, v2_doc,
            fact_audit["findings"]), None, None
    if not audited:
        return {"outcome": fail_kind, "v2_doc": v2_doc}, None, None
    return None, v2_doc, doc_hash


def _fact_legacy(ledger, member, pid, mid, fp, policy, llm_fn, deadline):
    """Reuse or extract the legacy fact set.

    Returns (early, facts): early is the _fact_stage result to return
    as-is, else None with the facts."""
    prev_f = current_artifact(ledger.db, KIND_FACTS, mid, fp)
    if prev_f is not None:
        facts = prev_f["content"].get("facts", [])
    else:
        if time.monotonic() > deadline - 5:
            return {"outcome": "incomplete", "v2_doc": None}, None
        facts, f_complete, f_dropped, f_reason = semantic_extraction.extract_facts(
            llm_fn, member, deadline - 5, return_reason=True,
            ledger=ledger, source_fingerprint=fp, project_id=pid)
        if not f_complete:
            return {"outcome": "retryable" if f_reason == "model"
                    else "incomplete", "v2_doc": None}, None
        ledger.artifact_add(
            KIND_FACTS,
            json.dumps({"facts": facts,
                        "evidence": {f["_evidence"]["evidence_id"]:
                                     f["_evidence"] for f in facts
                                     if f.get("_evidence")}},
                       ensure_ascii=False),
            project_id=pid, message_id=mid, model=semantic_transport.llm_model(),
            meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
                  "chunks_total": len(mcs_util.text_chunks(
                      member["body_original"])),
                  "dropped_by_cap": f_dropped})
    return None, facts


def _prefiltered(ledger, mid) -> bool:
    """The message's current extraction is the local no-signal prefilter marker."""
    from mcs_queries import current_extract_pred, json_or_null
    return ledger.db.execute(
        "SELECT 1 FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
        f"WHERE a.kind='extract_llm' AND a.message_id=? {current_extract_pred()} "
        f"AND json_extract({json_or_null('a.meta')},'$.prefilter')='no_signal' LIMIT 1",
        (mid,)).fetchone() is not None


def proposition_quality_report(ledger, project_id: int, scfg: dict) -> dict:
    """Per-proposition noul/verdict distribution over current assessment rows.

    For threshold/registry tuning, each message's newest row contributes
    if it is COMPLETE and matches the ACTIVE proposition set; valid
    no-signal rows count as prefiltered."""
    active = jev.active_propositions(scfg)
    latest, seen, superseded, prefiltered_count = {}, set(), 0, 0
    policy = policy_fingerprint(scfg)
    for stored in ledger.db.execute(
            'SELECT message_id,content,meta,model FROM artifacts WHERE kind=? AND project_id=? '
            'ORDER BY artifact_id DESC', (KIND_ASSESS, project_id)):
        try:
            content, meta = strict_json(stored['content']), strict_json(stored['meta'])
        except (ValueError, TypeError, RecursionError, OverflowError):
            content, meta = None, None
        if isinstance(meta, dict) and meta.get('fact_id') is not None:
            continue
        mid = stored['message_id']
        if type(mid) is not int:
            continue
        if mid in seen:
            continue
        seen.add(mid)
        if not isinstance(content, dict) or not isinstance(meta, dict):
            superseded += 1
            continue
        verdicts = content.get('verdicts')
        row = ledger.db.execute('SELECT parent_id FROM messages WHERE project_id=? AND message_id=?',
                                (project_id, mid)).fetchone()
        try:
            bundle = (semantic_store.thread_bundle(ledger.db, project_id, row['parent_id'] or mid, [mid])
                      if row is not None else None)
            fp = bundle['source_fingerprint'] if bundle is not None else None
            prefiltered = (meta.get('skipped') == 'prefilter_no_signal'
                           and semantic_store._verified_prefilter(ledger, project_id, mid))
            assessment, previous = (semantic_store._primary_assessment_history(ledger.db, project_id, mid, fp, policy)
                                    if fp is not None else (None, []))
            valid = (assessment is not None and meta.get('technical_status') == 'complete'
                     and assessment == {'content': content, 'meta': meta, 'model': stored['model']}
                     and semantic_store._valid_primary_assessment(content, meta, stored['model'], mid, fp, policy, scfg,
                                                   prefiltered=prefiltered)
                     and semantic_store._valid_primary_history(content, meta, previous, mid, fp, policy, scfg))
        except (ValueError, TypeError, KeyError, sqlite3.Error, RecursionError, OverflowError):
            valid = False
        if not valid:
            superseded += 1
            continue
        if verdicts == {} and prefiltered:
            prefiltered_count += 1
            continue
        latest[mid] = verdicts
    props = {}
    for pid, prop in active.items():
        buckets, nouls = {"MATCH": 0, "NO_MATCH": 0, "UNDETERMINED": 0}, []
        invalid = 0
        for verdicts in latest.values():
            entry = verdicts.get(pid)
            verdict = entry.get('verdict') if isinstance(entry, dict) else None
            noul = entry.get('noul') if isinstance(entry, dict) else None
            if verdict not in buckets:
                invalid += 1
                continue
            buckets[verdict] += 1
            if type(noul) in (int, float) and math.isfinite(noul):
                nouls.append(float(noul))
        props[pid] = {"label_ja": prop.get("label_ja", pid),
                      "verdicts": {**buckets, "invalid": invalid},
                      "noul_mean": sum(nouls) / len(nouls) if nouls else None,
                      "noul_min": min(nouls) if nouls else None,
                      "noul_max": max(nouls) if nouls else None}
    return {"project_id": project_id, "messages": len(latest),
            "prefiltered": prefiltered_count, "superseded": superseded, "propositions": props}
