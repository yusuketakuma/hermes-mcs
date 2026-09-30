"""Semantic drain engine (spec §14, §18): the durable-job worker —
claiming jobs, staged extraction/summary/audit, Jev budget + circuit
wiring, plan recording, and notice emission. Functions reach facade
patch points (semantic.llm_chat / audit_claims / _process_job) through
the module object so monkeypatching keeps working."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401  registers every subdir as import root

from functools import wraps
import json
from contextlib import contextmanager
import math
import time

from mcs_requests import payload_hash
from mcs_util import env_value, load_config
import semantic_jev as jev
import semantic_runtime as runtime
import semantic_v4 as v4
from semantic_audit import audit_code, audit_status_for
from semantic_llm import _chunks, extract_facts, summarize
from semantic_loops import update_loops
from semantic_policy import (JOB_KIND, KIND_ASSESS, KIND_AUDIT,
                             KIND_BUNDLE, KIND_FACT_AUDIT, KIND_FACT_PROJ,
                             KIND_FACT_REPAIR, KIND_FACTS, KIND_FACTS_V2,
                             KIND_PLAN,
                             KIND_SUMMARY, KIND_USAGE, POLICY_VERSION,
                             QC_JOB_KIND, SCHEMA_VERSION,
                             policy_fingerprint, semantic_config)
from semantic_render import (_emit_degraded, _notify_src_event,
                             _outbox_has_delivery, render_notice)
from semantic_store import _current, jev_state, jev_usage_today

# extract_qc subsystem lives in semantic_qc.py; names stay re-exported
# here so drain callers and tests keep one patch surface.
from semantic_qc import (QC_MAX_ITEMS,  # noqa: F401
                         _process_qc_job, _qc_questions,  # noqa: F401
                         _qc_seed)  # noqa: F401

def _eval_chunked(jev_client, state: dict, questions: dict,
                  deadline: float, limit: int) -> dict:
    """evaluate() with the question set split into
    max_questions_per_request-sized chunks — the configured bound is
    enforced, not just validated (§13.4)."""
    keys = list(questions)
    merged = {"answers": {}}
    for i in range(0, len(keys), max(1, limit)):
        part = {k: questions[k] for k in keys[i:i + limit]}
        out = jev_client.evaluate(state, part, deadline)
        merged["answers"].update(out["answers"])
    return merged


def _jev_failure_class(error) -> str:
    """Classify an evaluation failure for durable job scheduling.

    ``resource`` means no external request should be counted as a failed
    input (daily cap, missing key, or exhausted job budget).  All other Jev
    retryable failures use finite job retries; permanent failures stop the
    generation until explicitly reseeded.
    """
    if getattr(error, "kind", "") in {"budget_exceeded", "no_api_key"}:
        return "resource"
    return "retry" if getattr(error, "retryable", False) else "failed"


def _jev_fail_flag(error) -> str | None:
    """Map a Jev failure onto this pass's accounting flags —
    'resource' waits (never counted against the input), 'retryable'
    retried, 'hard' stops the generation.  None when no error."""
    if error is None:
        return None
    return {"resource": "resource",
            "retry": "retryable"}.get(_jev_failure_class(error), "hard")


def _plan_exists(ledger, message_id: int, fp: str,
                 status: str, policy=None) -> bool:
    """A notify_plan for THIS (generation, audit outcome) already
    recorded — replay and crash-retry must not stack duplicate plan
    rows. Keyed on status too: a plan left by a PENDING run does not
    satisfy a later PASS on the same fingerprint."""
    for r in ledger.db.execute(
            "SELECT meta FROM artifacts WHERE kind=? AND message_id=?",
            (KIND_PLAN, message_id)):
        try:
            m = json.loads(r["meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(m, dict) and m.get("fingerprint") == fp \
                and m.get("audit_status") == status \
                and (policy is None or m.get("policy_fingerprint") == policy):
            return True
    return False


def _write_result(ledger, pid: int, mid: int, r: dict, fp: str,
                  members: dict, final_status: str, policy: str, publication_mode: str) -> None:
    """Summary + audit artifact pair for one evaluated target — the
    durable record of this generation's outcome. The repair_count meta
    is the INV-10 budget: it survives restarts AND mid-job deferrals
    because it lives on this artifact, not in process memory."""
    import semantic
    summary = {k: v for k, v in r["summary"].items()
               if not k.startswith("_")}
    # a deferred/pending job re-derives the same outcome on every pass —
    # without this guard each drain appends a byte-identical
    # summary+audit pair (observed: 3 identical PENDING rows in 13 min
    # while Jev was unreachable). The durable store records an outcome
    # once, not once per attempt (FIX-SD1).
    prev_sum = _current(ledger, KIND_SUMMARY, mid, fp, policy)
    prev_aud = _current(ledger, KIND_AUDIT, mid, fp, policy)
    if (prev_sum is not None and prev_aud is not None
            and prev_sum["content"] == summary
            and prev_sum["meta"].get("audit_status") == final_status
            and prev_sum["meta"].get("publication_mode")
            == publication_mode
            and prev_aud["content"].get("status") == final_status
            and prev_aud["content"].get("findings") == r["findings"]):
        return
    ledger.artifact_add_tx(
        KIND_SUMMARY,
        json.dumps(summary, ensure_ascii=False),
        project_id=pid, message_id=mid, model=semantic.llm_model(),
        meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
              "audit_status": final_status, "publication_mode": publication_mode,
              "stale": final_status == "STALE",
              "target_revision": members[mid]["revision"],
              # the stored content drops _-keys — the oversize marker
              # must survive storage or a re-audit of a PENDING-stored
              # stub would lose its blocking finding
              "input_oversize": bool(r["summary"].get("_input_oversize"))})
    ledger.artifact_add_tx(
        KIND_AUDIT,
        json.dumps({"status": final_status,
                    "findings": r["findings"],
                    "target_message_id": mid}, ensure_ascii=False),
        project_id=pid, message_id=mid, model=jev.JEV_MODEL,
        meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
              "audit_status": final_status, "publication_mode": publication_mode,
              "repair_count": 1 if r["repaired"] else 0,
              "jev_requests": r.get("jev_requests", 0),
              # raw Jev claim verdicts — the calibration corpus for
              # match_threshold tuning (summary content strips _-keys,
              # so this private channel is the only place they persist)
              "claim_audit": r["summary"].get("_claim_audit") or {}})


def _park_needs_review(ledger, pid, mid, fp, policy, v2_doc,
                       findings: list | None = None) -> dict:
    """Terminal NEEDS_REVIEW for a canonical generation: a durable audit
    receipt carrying the (code-only) findings, then ``hard_fail`` so the
    job stops for human review instead of deferring or publishing.
    Findings default to incomplete canonical coverage."""
    if findings is None:
        findings = [{"code": "canonical_coverage_incomplete"}]
    ledger.artifact_add(
        KIND_AUDIT,
        json.dumps({"status": "NEEDS_REVIEW", "findings": findings,
                    "target_message_id": mid}, ensure_ascii=False),
        project_id=pid, message_id=mid,
        meta={"fingerprint": fp, "policy_fingerprint": policy,
              "audit_status": "NEEDS_REVIEW",
              "technical_status": "needs_review"})
    return {"outcome": "hard_fail", "v2_doc": v2_doc,
            "findings": findings}


def _fact_stage(ledger, scfg, member, pid, mid, fp, policy,
                jev_client, llm_fn, deadline):
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
    import semantic

    # facts: reuse a stored set for this fingerprint, else extract.
    fact_source = scfg.get("fact_source", "legacy")
    v2_doc = None
    if fact_source != "legacy":
        from semantic_extraction import extract_facts_v2
        from semantic_projection import project_v2_facts
        prev_v2 = _current(ledger, KIND_FACTS_V2, mid, fp)
        # C03: an adjudicated-but-incomplete stored doc must not be
        # reused forever — one bounded re-extraction resumes from the
        # missing chunks; if it still cannot complete, the generation
        # parks as needs_review instead of silently deferring forever.
        coverage_retry = (
            fact_source == "canonical" and prev_v2 is not None
            and prev_v2["meta"].get("coverage_status") not in
                (None, "complete")
            and not prev_v2["meta"].get("coverage_retry"))
        if fact_source == "canonical":
            # S0: deterministic prep (extract_v1 hints + atom/chunk
            # boundaries inside extract_facts_v2) — no model call
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s0_prep", "done")
        if prev_v2 is not None and not coverage_retry:
            v2_doc = prev_v2["content"]
            if fact_source == "canonical":
                v4.record_stage(ledger, pid, mid, fp, policy,
                                "s1_extract", "reused")
        else:
            if time.monotonic() > deadline - 5:
                return {"outcome": "incomplete", "v2_doc": None}
            if fact_source == "canonical":
                v4.record_stage(ledger, pid, mid, fp, policy,
                                "s1_extract", "start")
            v2_result = extract_facts_v2(
                llm_fn, member, deadline - 5, ledger=ledger,
                source_fingerprint=fp, project_id=pid,
                jev_client=jev_client, retry_coverage=coverage_retry)
            if not v2_result["extraction_complete"]:
                if fact_source == "canonical":
                    return {"outcome": "retryable"
                            if v2_result["failure_reason"] == "model"
                            else "incomplete", "v2_doc": None}
                v2_doc = None      # shadow: incomplete doc not stored
            else:
                v2_doc = v2_result["doc"]
                meta = {"fingerprint": fp,
                        "policy_fingerprint": policy,
                        "schema": SCHEMA_VERSION,
                        "fact_source": fact_source,
                        "coverage_status":
                            v2_doc["coverage"]["status"],
                        "facts": len(v2_doc["facts"]),
                        "open_obligations": len(
                            v2_doc["coverage"]
                            ["open_obligation_ids"])}
                if coverage_retry:
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
                    model=semantic.llm_model(),
                    meta=meta)
        if fact_source == "canonical" and v2_doc is not None \
                and v2_doc["coverage"]["status"] != "complete":
            if coverage_retry or (prev_v2 and prev_v2["meta"].get("coverage_retry")):
                return _park_needs_review(
                    ledger, pid, mid, fp, policy, v2_doc)
            return {"outcome": "incomplete", "v2_doc": v2_doc}
    if fact_source == "canonical":
        fail_kind = "incomplete"
        # POST_JEV: bidirectional audit must pass before the
        # canonical fact set feeds any consumer.  The audit artifact
        # is keyed by doc_hash, so a repaired document is always
        # re-audited; the repair receipt bounds dispatch to one
        # attempt per generation.  Findings hold the generation —
        # never promoted or silently dropped.
        audited = False
        for _attempt in range(2):
            doc_hash = v4._doc_hash(v2_doc)
            prev_fa = _current(ledger, KIND_FACT_AUDIT, mid, fp, policy)
            # C04: only a COMPLETED evaluation is a reusable audit — a
            # stored evaluated:false row (mid-audit outage) must be
            # re-run, otherwise a transient failure pins the generation
            # on a failed verdict forever and manual retry cannot clear it
            if v4.fact_audit_verdict(prev_fa, doc_hash) is not None:
                fact_audit = prev_fa["content"]
            else:
                if time.monotonic() > deadline - 5:
                    break
                from semantic_audit import audit_facts_v2
                fact_audit = audit_facts_v2(
                    jev_client, v2_doc, member["body_original"],
                    deadline - 5,
                    match_threshold=scfg["match_threshold"])
                stored = {"status": fact_audit["status"],
                          "evaluated": fact_audit["evaluated"],
                          "findings": fact_audit["findings"],
                          "fact_verdicts": fact_audit["fact_verdicts"]}
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
                        meta={"fingerprint": fp,
                              "policy_fingerprint": policy,
                              "schema": SCHEMA_VERSION,
                              "doc_hash": doc_hash,
                              "audit_status": fact_audit["status"]})
            v4.record_stage(
                ledger, pid, mid, fp, policy,
                "s4_reaudit" if _attempt else "s2_fact_audit",
                fact_audit["status"] if fact_audit["evaluated"]
                else "unevaluated",
                doc_hash=doc_hash)
            if not fact_audit["evaluated"]:
                error = getattr(jev_client, "last_error", None)
                if error is not None \
                        and _jev_failure_class(error) == "failed":
                    fail_kind = "hard_fail"
                elif error is None \
                        or _jev_failure_class(error) != "resource":
                    fail_kind = "retryable"
                break
            if fact_audit["status"] == "PASS":
                if v2_doc["coverage"]["status"] != "complete":
                    # defense in depth: never mint PASS over open
                    # obligations, whatever the audit said
                    return _park_needs_review(
                        ledger, pid, mid, fp, policy, v2_doc)
                audited = True
                break
            # NEEDS_REVIEW: one targeted repair dispatch per
            # generation (receipt-persisted), then re-audit.
            rejected = {f["fact"]: f["code"]
                        for f in fact_audit["findings"]
                        if f.get("fact")}
            if _attempt == 0 and rejected \
                    and _current(ledger, KIND_FACT_REPAIR,
                                 mid, fp) is None:
                if time.monotonic() > deadline - 5:
                    break      # repair still possible next pass
                # S3 (T18): reserve the single repair dispatch BEFORE
                # the model call — a crash leaves the 'started'
                # receipt, which permanently consumes this generation's
                # one dispatch (no second repair run)
                started = ledger.artifact_add(
                    KIND_FACT_REPAIR,
                    json.dumps({"status": "started",
                                "rejected": rejected},
                               ensure_ascii=False),
                    project_id=pid, message_id=mid,
                    model=semantic.llm_model(),
                    meta={"fingerprint": fp,
                          "policy_fingerprint": policy,
                          "schema": SCHEMA_VERSION,
                          "doc_hash": doc_hash})
                v4.record_stage(ledger, pid, mid, fp, policy,
                                "s3_repair", "reserved",
                                doc_hash=doc_hash)
                from semantic_extraction import repair_facts_v2
                sent = []

                def repair_llm(prompt):
                    try:
                        value = llm_fn(prompt)
                    except runtime.RuntimeGuardError as e:
                        if not (isinstance(e, runtime.LLMNotSent)
                                or e.stage in _PRE_DISPATCH_STAGES):
                            sent.append(True)
                        raise
                    except Exception:
                        sent.append(True)
                        raise
                    sent.append(True)
                    return value
                try:
                    repair = repair_facts_v2(
                        repair_llm, member, v2_doc, rejected, deadline - 5)
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
                ledger.artifact_add(
                    KIND_FACT_REPAIR,
                    json.dumps({"rejected": rejected,
                                "repaired": repair["repaired"],
                                "repaired_fact_ids":
                                    repair["repaired_fact_ids"],
                                "owner_chunk_ids":
                                    repair["owner_chunk_ids"]},
                               ensure_ascii=False, allow_nan=False),
                    project_id=pid, message_id=mid,
                    model=semantic.llm_model(),
                    meta={"fingerprint": fp,
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
                        model=semantic.llm_model(),
                        meta={"fingerprint": fp,
                              "policy_fingerprint": policy,
                              "schema": SCHEMA_VERSION,
                              "fact_source": fact_source,
                              "repaired": True,
                              "coverage_status":
                                  v2_doc["coverage"]["status"],
                              "facts": len(v2_doc["facts"])})
                    # the repair drops every rejected fact and reopens
                    # the obligations they covered — a repaired doc with
                    # open obligations must never reach PASS (rollout
                    # "Nothing degrades to PASS"); the one repair is
                    # spent, so the generation parks for review
                    if v2_doc["coverage"]["status"] != "complete":
                        return _park_needs_review(
                            ledger, pid, mid, fp, policy, v2_doc)
                    continue
            # an evaluated NEEDS_REVIEW with no repair left (no per-fact
            # finding to repair, repair already spent, or still failing
            # after it) is a clinical verdict, not a technical wait —
            # park it for review instead of deferring forever
            return _park_needs_review(
                ledger, pid, mid, fp, policy, v2_doc,
                fact_audit["findings"])
        if not audited:
            return {"outcome": fail_kind, "v2_doc": v2_doc}
        facts = project_v2_facts(v2_doc)
        # T12: audited canonical docs also publish a legacy-shaped
        # projection so the read side (stats/queries/rollup/signals)
        # keeps working during migration — the artifact carries the
        # message hash so "current" predicates bind it like an
        # extract_llm row.
        # audited is only set by a break right after doc_hash was taken
        # for this v2_doc, so doc_hash is the audited document's hash
        from semantic_projection import (PROJECTION_VERSION,
                                         project_v2_doc_legacy,
                                         projection_current)
        prev_proj = _current(ledger, KIND_FACT_PROJ, mid, fp, policy)
        # a row minted by an older projection version (or from another
        # audited document) is stale — supersede it with a fresh row
        if prev_proj is None \
                or not projection_current(prev_proj["meta"], doc_hash):
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s5_projection", "done",
                            doc_hash=doc_hash)
            ledger.artifact_add(
                KIND_FACT_PROJ,
                json.dumps(project_v2_doc_legacy(v2_doc),
                           ensure_ascii=False, allow_nan=False),
                project_id=pid, message_id=mid,
                model=semantic.llm_model(),
                meta={"fingerprint": fp,
                      "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION,
                      "hash": member["revision"],
                      "doc_hash": doc_hash,
                      "projection_version": PROJECTION_VERSION})
        else:
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s5_projection", "reused",
                            doc_hash=doc_hash)
    else:
        prev_f = _current(ledger, KIND_FACTS, mid, fp)
        if prev_f is not None:
            facts = prev_f["content"].get("facts", [])
        else:
            if time.monotonic() > deadline - 5:
                return {"outcome": "incomplete", "v2_doc": None}
            facts, f_complete, f_dropped, f_reason = extract_facts(
                llm_fn, member, deadline - 5, return_reason=True,
                ledger=ledger, source_fingerprint=fp, project_id=pid)
            if not f_complete:
                return {"outcome": "retryable" if f_reason == "model"
                        else "incomplete", "v2_doc": None}
            ledger.artifact_add(
                KIND_FACTS,
                json.dumps({"facts": facts,
                            "evidence": {f["_evidence"]["evidence_id"]:
                                         f["_evidence"] for f in facts
                                         if f.get("_evidence")}},
                           ensure_ascii=False),
                project_id=pid, message_id=mid, model=semantic.llm_model(),
                meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
                      "chunks_total": len(_chunks(
                          member["body_original"])),
                      "dropped_by_cap": f_dropped})
    return {"outcome": None, "facts": facts, "v2_doc": v2_doc}


def _publish_v4(ledger, pid: int, mid: int, fp: str, policy: str,
                members: dict, v2_docs_by_target: dict,
                final_status: str, findings: list,
                fact_source: str) -> None:
    """T18 S8 — called INSIDE the caller's ``with ledger.db`` so the
    read-model switch (or its diagnostic) commits atomically with the
    status receipt. Only an all-PASS chain mints the ``semantic_facts_v4``
    read model; anything else leaves a ``v4_diagnostic`` receipt that no
    extraction reader can select."""
    if fact_source != "canonical":
        return
    doc = v2_docs_by_target.get(mid)
    if doc is None:
        prev_v2 = _current(ledger, KIND_FACTS_V2, mid, fp)
        doc = prev_v2["content"] if prev_v2 else None
    doc_hash = v4._doc_hash(doc) if doc else None
    if final_status == "PASS" and doc is not None:
        vid = v4.publish(ledger, pid, mid, fp, policy,
                         members[mid], doc)
        v4.record_stage(ledger, pid, mid, fp, policy, "s8_publish",
                        "PASS", tx=True, artifact_id=vid,
                        doc_hash=doc_hash)
    else:
        v4.diagnostic(ledger, pid, mid, fp, policy, final_status,
                      findings, doc_hash=doc_hash, tx=True)
        v4.record_stage(ledger, pid, mid, fp, policy, "s8_publish",
                        final_status, tx=True, doc_hash=doc_hash)


def _process_job_inner(ledger, scfg, job, jev_client, llm_fn, deadline,
                       cfg_path: str | None = None,
                       config_generation: str | None = None,
                       reserve_fn=None) -> str:
    """One semantic job -> durable artifacts + job state transition.
    Returns 'done'|'deferred'|'retry'|'failed'."""
    import semantic
    pid, root = job["project_id"], job["message_id"]
    token = runtime.JobToken.from_row(job)
    deadline = runtime.job_deadline(scfg, deadline)
    try:
        pl = json.loads(job["payload"])
    except (json.JSONDecodeError, TypeError):
        return "failed"
    if not isinstance(pl, dict):
        return "failed"
    targets = pl.get("targets", [root])
    if targets is None:
        return "failed"
    try:
        bundle = semantic.thread_bundle(ledger, pid, root, targets)
    except ValueError:
        return "failed"
    if bundle is None:
        # source vanished — nothing to preserve for it; mark done so
        # the row does not re-run as a no-op on every drain
        return "done" if runtime.transition(ledger, token, "done") \
            else "stale"
    fp = bundle["source_fingerprint"]
    policy = policy_fingerprint(scfg)

    def current_source_fp():
        fresh = semantic.thread_bundle(ledger, pid, root, targets or [root])
        return fresh["source_fingerprint"] if fresh else None

    def guard(stage):
        try:
            runtime.guard(
                ledger, token, deadline=deadline,
                expected_config_generation=config_generation,
                expected_mode=scfg["mode"], cfg_path=cfg_path,
                load_cfg=load_config, parse_cfg=semantic_config,
                source_fingerprint=fp, current_source=current_source_fp,
                stage=stage)
        except runtime.RuntimeStale as error:
            # Keep the stale marker bound to the worker's original
            # fingerprint.  A fresh bundle must never be used to label an
            # old worker's result.
            if error.stage.endswith(":source"):
                ledger.artifact_add(
                    KIND_AUDIT,
                    json.dumps({"status": "STALE", "findings": [],
                                "target_message_id": root},
                               ensure_ascii=False),
                    project_id=pid, message_id=root, model=jev.JEV_MODEL,
                    meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
                          "audit_status": "STALE",
                          "technical_status": "stale"})
            raise

    guard("admission")
    if v4.hold_unbounded_job(ledger, job):
        return "failed"

    # The adapters below make every actual Jev/local-model boundary pass
    # through the same identity check.  A real Jev client additionally gets
    # a hook before each internal retry attempt and a durable reservation
    # immediately before the POST.
    jev_client, _ = runtime.bind_jev(jev_client, guard, reserve_fn)
    llm_fn = runtime.guarded_llm(
        llm_fn, guard, deadline, timeout_cap=semantic.LLM_TIMEOUT,
        # one long canonical generation (~300 s) or two thirds of the
        # per-job budget, whichever is smaller — a follow-up call that
        # cannot fit defers the pass instead of overrunning it
        call_reserve=min(runtime.LLM_CALL_RESERVE_S,
                         float(scfg["job_budget_seconds"]) * 2 / 3))
    # provenance on the recorded bundle (spec §12.1): which stored
    # origin event / capture path this evaluation descends from
    origin = pl.get("origin") if isinstance(pl.get("origin"), dict) \
        else {}
    bundle["origin_event_id"] = origin.get("event_id")
    bundle["capture_origin"] = origin.get("source")
    if _current(ledger, KIND_BUNDLE, root, fp, policy) is None:
        ledger.artifact_add(
            KIND_BUNDLE, json.dumps(bundle, ensure_ascii=False),
            project_id=pid, message_id=root, model=jev.JEV_MODEL,
            meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION})
    all_targets = [t for t in (targets or [root])
                   if any(m["message_id"] == t
                          for m in bundle["members"])]
    members = {m["message_id"]: m for m in bundle["members"]}
    fact_source = scfg.get("fact_source", "legacy")
    facts_by_target: dict[int, list] = {}
    v2_docs_by_target: dict[int, dict] = {}
    coverage_by_target: dict[int, list] = {}
    detail_findings: dict[int, list] = {}
    verdicts: dict[int, dict] = {}
    incomplete = False
    hard_fail = False   # non-retryable Jev error — bound the retries
    not_sent = None     # LLMNotSent from a target summary (see below)
    retryable_failure = False
    resource_wait = False
    for mid in all_targets:
        member = members[mid]
        # restart-safe: a complete assessment for THIS fingerprint is
        # reused; retry_wait/error/pending ones are re-attempted
        prev = _current(ledger, KIND_ASSESS, mid, fp, policy)
        if prev and prev["meta"].get("technical_status") == "complete":
            verdicts[mid] = prev["content"].get("verdicts", {})
        else:
            if time.monotonic() > deadline - 5:
                incomplete = True
                break
            state = jev_state(bundle, mid)
            meta_base = {"fingerprint": fp, "policy_fingerprint": policy, "model": jev.JEV_MODEL,
                         "registry": jev.REGISTRY_VERSION,
                         "schema": SCHEMA_VERSION}
            answers = None
            req0 = jev_client.requests_made if jev_client else 0
            if jev_client is not None and state is not None:
                qs = {k: jev.noul_question(p["instructions"], p["true"],
                                           p["false"])
                      for k, p in jev.PROPOSITIONS.items()}
                try:
                    out = _eval_chunked(
                        jev_client, state, qs, deadline,
                        scfg["max_questions_per_request"])
                    answers = out["answers"]
                except jev.JevError as e:
                    meta_base["technical_status"] = (
                        "retry_wait" if e.retryable else "error")
                    meta_base["error_kind"] = e.kind
                    flag = _jev_fail_flag(e)
                    resource_wait |= flag == "resource"
                    retryable_failure |= flag == "retryable"
                    hard_fail |= flag == "hard"
            meta_base["jev_requests"] = (
                jev_client.requests_made - req0) if jev_client else 0
            if answers is None:
                meta_base.setdefault("technical_status", "pending")
                meta_base.setdefault("error_kind", "jev_unavailable")
                # an identical wait/error record adds no information —
                # only a status CHANGE earns a new row, so a durable
                # resource wait (Jev down, no key, budget hold) does not
                # append one artifact per drain forever
                if prev is None or (
                        prev["meta"].get("technical_status"),
                        prev["meta"].get("error_kind")) != (
                        meta_base["technical_status"],
                        meta_base.get("error_kind")):
                    ledger.artifact_add(
                        KIND_ASSESS,
                        json.dumps({"target_message_id": mid,
                                    "verdicts": {}},
                                   ensure_ascii=False),
                        project_id=pid, message_id=mid,
                        model=jev.JEV_MODEL, meta=meta_base)
                incomplete = True
                continue
            verdicts[mid] = {k: {"noul": a["noul"],
                                 "verdict": jev.verdict_for(
                                     a["noul"], scfg["match_threshold"],
                                     scfg["nomatch_threshold"])}
                             for k, a in answers.items()}
            detail = {}
            meta_base["jev_requests"] = (
                jev_client.requests_made - req0) if jev_client else 0
            ledger.artifact_add(
                KIND_ASSESS,
                json.dumps({"target_message_id": mid,
                            "verdicts": verdicts[mid],
                            "detail": detail}, ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={**meta_base, "technical_status": "complete"})
        if scfg["summary_mode"] == "off" and scfg["loop_mode"] == "off":
            continue
        stage = _fact_stage(ledger, scfg, member, pid, mid, fp, policy,
                            jev_client, llm_fn, deadline)
        if stage["outcome"] is not None:
            if fact_source == "canonical":
                # T18: a fact-stage failure still leaves a durable
                # diagnostic receipt — never an invisible stop
                doc = stage["v2_doc"]
                v4.diagnostic(
                    ledger, pid, mid, fp, policy,
                    "NEEDS_REVIEW" if stage["outcome"] == "hard_fail"
                    else "PENDING",
                    [{"code": f"fact_stage_{stage['outcome']}"}]
                    + list(stage.get("findings") or []),
                    doc_hash=(v4._doc_hash(doc) if doc else None))
            if stage["outcome"] == "retryable":
                retryable_failure = True
            elif stage["outcome"] == "hard_fail":
                hard_fail = True
            incomplete = True
            break
        facts = stage["facts"]
        if stage["v2_doc"] is not None:
            v2_docs_by_target[mid] = stage["v2_doc"]
        facts_by_target[mid] = facts
        from semantic_assessment import evaluate_medication_events
        event_details = evaluate_medication_events(
            ledger, bundle, mid, facts, jev_client, scfg, deadline - 5)
        guard("medication_detail_result")
        detail_findings[mid] = event_details["findings"]
        if not event_details["complete"] and event_details["failure_reason"] != "invalid":
            incomplete = True
            error = getattr(jev_client, "last_error", None)
            if error is not None and _jev_failure_class(error) == "failed" \
                    and not _malformed_answer(error):
                hard_fail = True
            elif event_details["failure_reason"] != "deadline" and (
                    error is None or _jev_failure_class(error) != "resource"):
                retryable_failure = True
            break
        if scfg["loop_mode"] != "off":
            guard("loop_candidates")
            update_loops(ledger, pid, bundle, {mid: facts}, None, scfg, deadline)
        if scfg["summary_mode"] != "off":
            previous = _current(ledger, "semantic_coverage", mid, fp, policy)
            coverage = previous["content"] if previous else None
            if not coverage or not coverage.get("evaluated"):
                from semantic_audit import evaluate_source_fact_coverage
                coverage = evaluate_source_fact_coverage(
                    jev_client, member["body_original"], facts, deadline,
                    target_id=f"m{mid}", match_threshold=scfg["match_threshold"])
                guard("coverage_result")
                ledger.artifact_add(
                    "semantic_coverage", json.dumps(coverage, ensure_ascii=False),
                    project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                    meta={"fingerprint": fp, "policy_fingerprint": policy,
                          "technical_status": "complete" if coverage["evaluated"] else "pending"})
            coverage_by_target[mid] = coverage["findings"]
            if not coverage["evaluated"]:
                incomplete = True
                error = getattr(jev_client, "last_error", None)
                if error is not None and _jev_failure_class(error) == "failed" \
                        and not _malformed_answer(error):
                    hard_fail = True
                elif (error is not None and _jev_failure_class(error) == "retry"
                      or coverage.get("failure_reason") == "model"):
                    retryable_failure = True
                break
    if incomplete:
        if hard_fail:
            return "failed"
        if retryable_failure:
            # deterministic failure (e.g. protocol_error, oversized
            # payload) — deferring forever would burn a Jev call every
            # tick on an input that can never pass; bounded retry ends
            # 'failed' where status_report can surface it
            return "retry"
        return "deferred"

    # summary + audit. A TERMINAL audit (PASS/NEEDS_REVIEW) for THIS
    # fingerprint is reused — its notification intent was already
    # committed atomically below. A PENDING one is NOT final: the
    # stored summary is re-audited so a mid-audit outage can never
    # wedge the thread on a stale PENDING marker (AT-058).
    results = {}
    for mid, facts in facts_by_target.items():
        if scfg["summary_mode"] == "off":
            continue
        existing = _current(ledger, KIND_SUMMARY, mid, fp, policy)
        prev_audit = _current(ledger, KIND_AUDIT, mid, fp, policy)
        if existing and existing["meta"].get("publication_mode") != scfg["summary_mode"]:
            existing = None
            prev_audit = None
        prev_status = (prev_audit["meta"].get("audit_status")
                       if prev_audit else None)
        if existing is not None and prev_status in ("PASS",
                                                    "NEEDS_REVIEW"):
            results[mid] = {"summary": existing["content"],
                            "status": prev_status, "findings": [],
                            "repaired": False, "fresh": False}
            continue
        if time.monotonic() > deadline - 5:
            incomplete = True
            break
        summary_reason = None
        if existing is not None:
            summary = existing["content"]
            if existing["meta"].get("input_oversize"):
                summary["_input_oversize"] = True
        else:
            candidate = _current(ledger, "semantic_candidate", mid, fp, policy)
            if candidate and candidate["meta"].get("publication_mode") == scfg["summary_mode"]:
                summary = candidate["content"]
            else:
                try:
                    summary, summary_reason = summarize(
                        llm_fn, bundle, mid, facts, verdicts.get(mid, {}),
                        deadline=deadline, return_reason=True)
                except runtime.LLMNotSent as e:
                    # the local model never got this target's request:
                    # stop here like any incomplete pass, so targets
                    # already audited are still committed below
                    not_sent = e
                    summary, summary_reason = None, "not_sent"
        if summary is None:
            # same dedup as the assess wait-state above: a persistent
            # local-LLM outage defers without stacking identical
            # PENDING rows on every drain
            if summary_reason == "model":
                retryable_failure = True
            prev_a = _current(ledger, KIND_AUDIT, mid, fp, policy)
            if not (prev_a
                    and prev_a["meta"].get("technical_status")
                    == "pending"
                    and prev_a["content"].get("status") == "PENDING"):
                ledger.artifact_add(
                    KIND_AUDIT,
                    json.dumps({"status": "PENDING",
                                "findings":
                                    [{"code": "summary_unavailable"}],
                                "target_message_id": mid},
                               ensure_ascii=False),
                    project_id=pid, message_id=mid, model=semantic.llm_model(),
                    meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
                          "technical_status": "pending"})
            incomplete = True
            if not_sent is not None:
                break         # the model is held — later targets too
            continue
        if fact_source == "canonical":
            v4.record_stage(ledger, pid, mid, fp, policy, "s6_summary",
                            "reused" if existing is not None
                            else "done")
        # Preserve the pre-audit output for the fixed-bundle comparison (§24.1).
        # It is never eligible for publication or automatic adoption.
        if existing is None and _current(ledger, "semantic_candidate", mid, fp, policy) is None:
            guard("candidate_snapshot")
            ledger.artifact_add(
                "semantic_candidate", json.dumps(summary, ensure_ascii=False),
                project_id=pid, message_id=mid, model=semantic.llm_model(),
                meta={"fingerprint": fp, "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION, "stage": "pre_audit",
                      "publication_mode": scfg["summary_mode"],
                      "target_revision": members[mid]["revision"]})
        # Reserve before dispatch: a crash cannot refund the repair (INV-10).
        repaired = bool(_current(ledger, "semantic_repair", mid, fp)
                        or (prev_audit
                            and prev_audit["meta"].get("repair_count", 0) >= 1))
        summary["_facts"] = facts   # evidence context for the Jev audit
        req0 = jev_client.requests_made if jev_client else 0
        findings = []
        status = "PENDING"
        for _attempt in range(2):      # initial + at most one repair
            code_f = (audit_code(bundle, facts, summary) + coverage_by_target.get(mid, [])
                      + detail_findings.get(mid, []))
            jev_f, evaluated = semantic.audit_claims(jev_client, bundle,
                                            summary, deadline, scfg["match_threshold"])
            if not evaluated and jev_client is not None:
                flag = _jev_fail_flag(getattr(jev_client, "last_error",
                                              None))
                resource_wait |= flag == "resource"
                retryable_failure |= flag == "retryable"
                hard_fail |= flag == "hard"
            findings = code_f + jev_f
            status = audit_status_for(code_f, jev_f, evaluated,
                                      repaired)
            if status == "REPAIR_REQUIRED" and not repaired:
                guard("repair_reservation")
                ledger.artifact_add(
                    "semantic_repair", json.dumps({"target_message_id": mid}),
                    project_id=pid, message_id=mid, model=semantic.llm_model(),
                    meta={"fingerprint": fp, "policy_fingerprint": policy, "repair_count": 1})
                repaired = True
                try:
                    summary2 = summarize(
                        llm_fn, bundle, mid, facts, verdicts.get(mid, {}),
                        feedback=[f.get("code", "") + " " +
                                  f.get("statement", f.get("claim", ""))
                                  for f in code_f + jev_f])
                except runtime.LLMNotSent:
                    # same outcome as an unavailable repair (the one-shot
                    # reservation is already durable) — never let it
                    # discard sibling targets audited in this pass
                    summary2 = None
                if summary2 is not None:
                    summary = summary2
                    summary["_facts"] = facts
                    continue
                status = "NEEDS_REVIEW"
                findings.append({"code": "repair_unavailable"})
            break
        v2_doc = v2_docs_by_target.get(mid)
        if fact_source == "canonical" and v2_doc is not None:
            # T11: the adjudicated contract drives a mandatory render
            # layer — verified facts stay visible and non-terminal
            # obligations are disclosed as limitations even if the
            # model summary dropped them. Per-target doc: the LAST
            # member's doc must never leak into another target's
            # summary (C02).
            from semantic_render import mandatory_render
            mandatory = mandatory_render(v2_doc)
            if mandatory["facts"]:
                summary["mandatory_facts"] = mandatory["facts"]
                summary["mandatory_fact_ids"] = mandatory["fact_ids"]
                summary["mandatory_pages"] = mandatory["pages"]
                summary["mandatory_overview"] = mandatory["overview"]
            if mandatory["limitations"]:
                existing_lims = summary.get("limitations") or []
                summary["limitations"] = existing_lims + [
                    x for x in mandatory["limitations"]
                    if x not in existing_lims]
            # A verified fact that cannot reach any page (oversized
            # single line, or a packing gap) is publication-incomplete —
            # flag it instead of letting a partial list pass as done.
            if not mandatory["complete"]:
                findings.append({"code": "mandatory_render_incomplete"})
                status = "NEEDS_REVIEW"
        summary["audit_status"] = status
        if fact_source == "canonical":
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s7_summary_audit", status)
        results[mid] = {"summary": summary, "status": status,
                        "findings": findings, "repaired": repaired,
                        "fresh": True,
                        "jev_requests": (jev_client.requests_made - req0)
                        if jev_client else 0}
    if incomplete:
        if results:
            try:
                guard("partial_promote")
            except runtime.RuntimeStale:
                # generation moved — budget/off re-raise so _process_job
                # maps them like every other boundary (bounded retry /
                # pause), never as a silent uncounted stale
                return "stale"
            # commit each completed target's outcome NOW — dropping it
            # would re-spend Jev calls on an identical input next run
            # AND silently reset the one-shot repair budget, whose
            # counter lives on these artifacts (INV-10, §18.2)
            with ledger.db:
                for mid, r in results.items():
                    if r["fresh"]:
                        _write_result(ledger, pid, mid, r, fp, members,
                                      r["status"], policy, scfg["summary_mode"])
                    _publish_v4(ledger, pid, mid, fp, policy, members,
                                v2_docs_by_target, r["status"],
                                r.get("findings") or [],
                                fact_source)
        if hard_fail:
            return "failed"
        if retryable_failure:
            return "retry"
        if not_sent is not None:
            raise not_sent    # _process_job maps it (Jev-spend backoff)
        return "deferred"

    # generation guard: the bundle, config, and complete job identity must
    # still be current before promotion.  The same guard also runs before
    # every Jev/local-model call through the wrappers above.
    stale = False
    try:
        guard("promote")
    except runtime.RuntimeStale:
        # only a generation change labels the results STALE; an
        # exhausted budget or a pause/OFF propagates to _process_job
        # (retry/deferred/stale) and writes nothing after the boundary
        stale = True
    pending = not stale and any(r["status"] == "PENDING"
                                for r in results.values())
    loops_done = True
    if not stale and scfg["loop_mode"] != "off":
        # loop candidates/relation events for the LIVE generation —
        # kept OUT of the commit tx because matching makes Jev calls
        # (no network inside a DB transaction, §6.2); replay-safe via
        # candidate_fp and pair dedup.
        _, loops_done = update_loops(ledger, pid, bundle,
                                     facts_by_target, jev_client,
                                     scfg, deadline)
        if not loops_done and jev_client is not None:
            flag = _jev_fail_flag(getattr(jev_client, "last_error", None))
            resource_wait |= flag == "resource"
            retryable_failure |= flag == "retryable"
            hard_fail |= flag == "hard"
    # notification eligibility is derived from the stored origin event,
    # not from this job's seed provenance (INV-20, §20.3) — evaluated
    # per target below: a covering new_messages intent must exist for
    # the message a notice reports on.
    with ledger.db:
        for mid, r in results.items():
            final_status = "STALE" if stale else r["status"]
            if r["fresh"]:
                _write_result(ledger, pid, mid, r, fp, members,
                              final_status, policy, scfg["summary_mode"])
            # T18 S8: v4 publication/diagnostic commits in the SAME
            # transaction as the status receipt — a crash cannot leave
            # a PASS result without its read-model row, nor a non-PASS
            # without its diagnostic. STALE is a status too: the
            # generation receipt persists even when the source moved
            _publish_v4(ledger, pid, mid, fp, policy, members,
                        v2_docs_by_target, final_status,
                        r.get("findings") or [], fact_source)
            if stale:
                continue
            # notification plan + outbox intent commit in the SAME
            # transaction as the analysis result — a crash can never
            # leave a saved summary without its notification intent
            # (§18.3). Reused results re-enter here so an intent lost
            # by an older crash is recreated on the next run. The two
            # dedupes are independent: the plan row keys on
            # (fingerprint, audit_status), the outbox row on its
            # delivery_key — a plan written by an earlier PENDING/shadow
            # run must not block a now-PASS enforce enqueue.
            summary_clean = {k: v for k, v in r["summary"].items()
                             if not k.startswith("_")}
            text = render_notice(ledger, pid, root, summary_clean,
                                 final_status, targets=all_targets,
                                 quality=bundle["content_quality"],
                                 focus_mid=mid)
            enqueued = False
            if (scfg["mode"] == "enforce" and scfg["summary_mode"] == "enforce"
                    and final_status == "PASS" and not pl.get("notification_free")):
                # per-target delivery key — one generation emits one
                # notice PER audited target; a shared (root, fp) key
                # would dedup every later target's claims out of the
                # notice while the header still claims them
                dkey = payload_hash({"kind": "semantic_notice",
                                     "root": root, "fp": fp,
                                     "mid": mid, "policy": policy})
                # per-target eligibility: THIS message must be covered
                # by a stored new_messages intent — a target that only
                # ever arrived via import/replay produces artifacts but
                # no notice, even when a sibling target was notified
                # (INV-20, AT-055)
                src_mid = _notify_src_event(ledger, pid, [mid])
                if src_mid is not None \
                        and not _outbox_has_delivery(ledger, dkey):
                    ledger.outbox_add_tx("semantic_notice", pid, {
                        "delivery_key": dkey, "root_id": root,
                        "target_message_id": mid,
                        "target_revision": members[mid]["revision"],
                        "src_event_id": src_mid, "text": text,
                        "fingerprint": fp,
                        "policy_version": POLICY_VERSION,
                        "policy_fingerprint": policy})
                    enqueued = True
            if not _plan_exists(ledger, mid, fp, final_status, policy):
                plan_meta = {"fingerprint": fp, "policy_fingerprint": policy,
                             "audit_status": final_status, "publication_mode": scfg["summary_mode"],
                             "mode": scfg["mode"],
                             "origin": pl.get("origin")}
                if enqueued:
                    plan_meta["enqueued"] = True
                ledger.artifact_add_tx(KIND_PLAN, json.dumps(
                    {"root_id": root, "target_message_id": mid,
                     "text": text}, ensure_ascii=False),
                    project_id=pid, message_id=mid,
                    meta=plan_meta)
        if (not stale and not pending and loops_done
                and not runtime.transition_tx(ledger, token, "done")):
            raise runtime.RuntimeStale("promote")
    if stale:
        return "stale"
    if hard_fail:
        return "failed"
    if pending:
        # evaluation incomplete (e.g. Jev outage mid-audit) — the job
        # must not sit 'done' on an unfinished audit; consume a bounded
        # retry attempt so a persistent outage eventually fails rather
        # than busy-loops (AT-058)
        return "deferred" if resource_wait and not (
            hard_fail or retryable_failure) else "retry"
    if not loops_done:
        # relation pass truncated by the pair budget/deadline — the
        # results committed above stand; the job re-runs to evaluate
        # the remaining (candidate, target) pairs (§17.2 持ち越し)
        return "retry" if hard_fail or retryable_failure else "deferred"
    return "done"


NOT_SENT_BACKOFF_S = 3600


def _process_job(ledger, scfg, job, jev_client, llm_fn, deadline,
                 cfg_path: str | None = None,
                 config_generation: str | None = None,
                 reserve_fn=None) -> str:
    """Preserve stale workers while bounding timeouts after dispatched work."""
    requests_before = jev_client.requests_made if jev_client is not None else 0
    llm_started = False

    @wraps(llm_fn)
    def tracked_llm(*args, **kwargs):
        nonlocal llm_started
        llm_started = True
        return llm_fn(*args, **kwargs)

    def jev_sent():
        return (jev_client is not None
                and jev_client.requests_made > requests_before)

    try:
        return _process_job_inner(
            ledger, scfg, job, jev_client, tracked_llm, deadline,
            cfg_path=cfg_path, config_generation=config_generation,
            reserve_fn=reserve_fn)
    except (runtime.RuntimeStale, runtime.RuntimeOff):
        return "stale"
    except runtime.LLMNotSent:
        # admission hold / refused connection: the LLM request never
        # left, so no attempt is consumed — but Jev calls this pass
        # already made (e.g. the per-chunk preflight) are real spend:
        # back off hourly so a held LLM cannot re-spend the Jev budget
        # every minute
        return "deferred_backoff" if jev_sent() else "deferred"
    except runtime.RuntimeBudgetShort:
        # earlier calls of this pass completed and persisted; the next
        # one could not start in the remaining budget — resume next
        # tick from the durable stages without charging an attempt.
        # Distinct from "deferred" so run_due stops the pass: the
        # remaining budget cannot fit another long generation either
        return "deferred_short"
    except runtime.RuntimeBudget:
        return "retry" if jev_sent() or llm_started else "deferred"


class _TimedClient:
    """Per-job wall-time accounting proxy for the Jev client: every
    attribute (including hookable markers and mutable budget fields)
    forwards to the real client, while each method call's seconds
    accumulate for the drain's per-job phase metrics (T14)."""

    def __init__(self, client):
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "elapsed_s", 0.0)

    def __getattr__(self, name):
        value = getattr(self._client, name)
        if not callable(value):
            return value

        def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return value(*args, **kwargs)
            finally:
                object.__setattr__(
                    self, "elapsed_s",
                    self.elapsed_s + time.perf_counter() - started)
        return timed

    def __setattr__(self, name, value):
        setattr(self._client, name, value)


def _malformed_answer(error) -> bool:
    """A protocol_error raised while validating ONE answer of a
    per-fact/per-dimension question (2026-09-30: two jobs died on a
    single malformed medication-detail reply after ~100 s of local
    generation). Such a reply is a per-request defect of the remote
    model, not a contract break like auth/model_mismatch: the job
    retries within its bounded attempts and the already-persisted
    detail answers are reused. Response-shape errors on the whole
    envelope (answers/usage missing) stay hard failures. Every
    per-answer code is ``{qid}:<reason>`` and no envelope code carries
    a colon (semantic_jev.validate_answers), so the colon is the
    discriminator."""
    return getattr(error, "kind", "") == "protocol_error" \
        and ":" in str(getattr(error, "detail", "") or "")


def _jev_error_brief(error) -> dict | None:
    """Kind/class of a JevError for run-record diagnostics (no bodies)."""
    if error is None:
        return None
    return {"kind": str(getattr(error, "kind", "") or "")[:40],
            # JevError.detail is a code (never a response body) — the
            # protocol_error variant is only diagnosable from it
            "detail": str(getattr(error, "detail", "") or "")[:60],
            "status": getattr(error, "status", 0) or 0,
            "class": _jev_failure_class(error)}


# Artifact kinds a semantic pass persists as durable stage results.
# Scoped (kind + project) so a concurrent drainer's extract_llm rows or
# another project's work never read as this job's progress (2026-09-30:
# a job looped 19 passes 'progressing' on foreign writes).
# guard stages raised before a local-model request leaves (runtime)
_PRE_DISPATCH_STAGES = ("llm_attempt", "llm_next_call", "llm")

_STAGE_KINDS = ("canonical_projection", "v4_stage", "loop_candidate",
                "loop_event", "notify_plan")


def _last_stage_artifact(ledger, project_id) -> int:
    """Highest id of a semantic stage artifact for this project (Jev
    usage receipts and drain bookkeeping excluded) — a pass that raised
    it made durable progress."""
    ph = ",".join("?" * len(_STAGE_KINDS))
    return ledger.db.execute(
        "SELECT COALESCE(MAX(artifact_id),0) FROM artifacts "
        # GLOB, not LIKE: a case-insensitive LIKE cannot seek
        # idx_artifacts_lookup, and its OR branch forced a full
        # artifacts scan for every job, twice per pass
        "WHERE project_id=? AND (kind GLOB 'semantic_*' "
        f"OR kind IN ({ph})) AND kind NOT IN (?,?,?)",
        (project_id, *_STAGE_KINDS, KIND_USAGE, "semantic_drain_run",
         SCHED_KIND)).fetchone()[0]


def _timed_llm(fn):
    """Wrap an injected llm_fn so wall time accumulates without
    changing its signature contract — ``timeout`` is forwarded only
    when the inner callable accepts it (``llm_call`` inspects the
    signature the same way)."""
    acc = {"s": 0.0}
    if runtime.accepts_timeout(fn):
        def timed(prompt, timeout=None):
            started = time.perf_counter()
            try:
                return fn(prompt, timeout=timeout)
            finally:
                acc["s"] += time.perf_counter() - started
    else:
        def timed(prompt):
            started = time.perf_counter()
            try:
                return fn(prompt)
            finally:
                acc["s"] += time.perf_counter() - started
    return timed, acc


SCHED_KIND = "semantic_sched"


def _sched_state(ledger) -> dict:
    """Persisted fair-schedule accounting — one fetch_jobs row
    (kind='semantic_sched', project_id/message_id 0, state='done' so no
    drain ever claims it) keeps the cohort share and progress counters
    restart-safe instead of living in process memory (T14)."""
    row = ledger.db.execute(
        "SELECT payload FROM fetch_jobs WHERE kind=? "
        "AND project_id=0 AND message_id=0", (SCHED_KIND,)).fetchone()
    try:
        pl = json.loads(row["payload"]) if row else {}
    except (json.JSONDecodeError, TypeError):
        pl = {}
    return pl if isinstance(pl, dict) else {}


def _sched_write(ledger, state: dict) -> None:
    now = time.time()
    ledger.db.execute("""
      INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,
        payload,state,next_try,created_at,updated_at)
      VALUES(?,0,0,NULL,?,'done',0,?,?)
      ON CONFLICT(kind,project_id,message_id) DO UPDATE SET
        payload=excluded.payload,updated_at=excluded.updated_at
    """, (SCHED_KIND, json.dumps(state, sort_keys=True), now, now))
    ledger.db.commit()


def _due_lanes(ledger, kinds: tuple, max_jobs: int) -> tuple[list, list, dict]:
    """Two-lane due selection with a persisted fairness share.

    Arrival-seeded jobs (payload.eligible=1 — a real notification event
    spawned them) keep priority, but the backfill cohort always gets a
    guaranteed share of this drain's window, so a continuous arrival
    stream can never starve runnable backlog (GAP-6). At max_jobs < 4
    a single share slot would otherwise displace the only arrival slot:
    the persisted 'turn' marker alternates cohorts instead of
    permanently starving either.

    Returns (arrival_jobs, backfill_jobs, sched_mutation) — the mutation
    is written by the caller once the batch is consumed."""
    now = time.time()
    kind_ph = ",".join("?" * len(kinds))
    eligible = ("json_valid(payload) "
                "AND COALESCE(json_extract(payload,'$.eligible'),0)=1")
    arrivals_due = ledger.db.execute(f"""
      SELECT COUNT(*) FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind IN ({kind_ph})
        AND {eligible}
    """, (now, *kinds)).fetchone()[0]
    backlog_due = ledger.db.execute(f"""
      SELECT COUNT(*) FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind IN ({kind_ph})
        AND NOT ({eligible})
    """, (now, *kinds)).fetchone()[0]
    share = 0
    turn_next = None
    if backlog_due:
        share = min(backlog_due, max(1, max_jobs // 4))
        if arrivals_due and share >= max_jobs:
            if max_jobs == 1:
                # one-slot drains alternate cohorts via the persisted
                # turn marker — neither starves permanently. The marker
                # records the cohort that won the last contested slot;
                # an absent marker keeps arrival first (its ordinary
                # priority), so backfill wins the SECOND contention.
                turn_next = ("arrival"
                             if _sched_state(ledger).get("turn")
                             != "arrival" else "backfill")
                share = 1 if turn_next == "backfill" else 0
            else:
                share = max_jobs - 1
        # Unused arrival capacity can serve due backfill within the same
        # max_jobs/deadline/request budget. Preserve contested-slot turns.
        if arrivals_due < max_jobs - share:
            share = min(backlog_due, max_jobs - arrivals_due)
    order = "CASE WHEN kind=? THEN 0 ELSE 1 END, job_id"
    arrivals = ledger.db.execute(f"""
      SELECT * FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind IN ({kind_ph})
        AND {eligible}
      ORDER BY {order} LIMIT ?
    """, (now, *kinds, JOB_KIND, max_jobs - share)).fetchall() \
        if max_jobs - share > 0 else []
    backfill = ledger.db.execute(f"""
      SELECT * FROM fetch_jobs
      WHERE state='pending' AND next_try <= ? AND kind IN ({kind_ph})
        AND NOT ({eligible})
      ORDER BY {order} LIMIT ?
    """, (now, *kinds, JOB_KIND, share)).fetchall() if share else []
    return list(arrivals), list(backfill), {"turn": turn_next}


@contextmanager
def _job_lock(ledger, job_id):
    """Exclude duplicate inference for a job while ingestion keeps writing."""
    from mcs_util import acquire_run_lock
    db_path = ledger.db.execute("PRAGMA database_list").fetchone()[2]
    path = os.path.join(os.path.dirname(db_path), "semantic_locks",
                        str(job_id) + ".lock")
    fd = acquire_run_lock(path)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)
    # Keep the inode: unlinking a flock file can create two lock owners.


def run_due(ledger, cfg: dict, result: dict, deadline: float,
            jev_client=None, llm_fn=None, max_jobs: int = 4,
            cfg_path: str | None = None, run_lock_fd=None,
            lane: str | None = None) -> dict:
    """Drain due semantic jobs inside the tick's remaining budget.
    OFF returns immediately — no job creation, no external calls, no
    auto-drain (AT-053). The caller's shared run lock is held by
    run_check; a standalone CLI takes acquire_run_lock itself.
    cfg_path, when given, re-validates config between jobs so an OFF
    flip mid-run takes effect at the next job boundary (AT-060)."""
    import semantic
    if lane not in (None, "realtime", "backlog"):
        raise ValueError("semantic_lane_invalid")
    if type(max_jobs) is not int or not 1 <= max_jobs <= 32:
        raise ValueError("semantic_max_jobs_invalid")
    scfg, errors = semantic_config(cfg)
    cfg_generation = runtime.config_generation(cfg)
    for e in errors:
        if e not in result["errors"]:
            result["errors"].append(e)
    out = {"mode": scfg["mode"], "done": 0, "deferred": 0,
           "failed": 0, "left": None, "budget_exhausted": False,
           # jobs whose pass persisted at least one new stage artifact
           # (usage receipts excluded) — "no completion" is not "no
           # progress" for the tick's starvation guard (2026-09-30)
           "progressed": 0}
    from semantic_store import invalidate_projections
    invalidate_projections(ledger, scfg)
    if scfg["mode"] == "off":
        return out
    # Stability guards shared with the extract lane: a nearly-full
    # volume turns per-job artifact writes into an I/O error storm, and
    # an open endpoint breaker means every llm_fn call pays a dead
    # timeout — defer the whole window instead of churning jobs.
    import mcs_util
    free_mb = mcs_util.disk_free_mb(ledger)
    if free_mb is not None \
            and free_mb < mcs_util.disk_floor_mb():
        out["disk_free_mb"] = round(free_mb)
        return out
    # Deterministic maintenance (no model, no Jev — not gated by the
    # endpoint breaker or LLM admission): bring current projection/v4
    # rows minted by an older projection version up to date, a bounded
    # slice per tick, right after this tick's invalidation pass.
    try:
        out["reproject"] = v4.reproject_stale(ledger, scfg)
    except Exception as e:
        result["errors"].append(f"semantic_reproject: {type(e).__name__}")
    circuit_s = mcs_util.circuit_open_s(ledger)
    if circuit_s:
        out["circuit_open_s"] = round(circuit_s)
        return out
    drain_started = time.perf_counter()
    out["job_metrics"] = []
    oldest = ledger.db.execute(
        "SELECT MIN(created_at) FROM fetch_jobs "
        "WHERE kind IN (?,?) AND state='pending'",
        (JOB_KIND, QC_JOB_KIND)).fetchone()[0]
    out["oldest_pending_job_age_s"] = (
        max(0.0, time.time() - oldest) if oldest is not None else None)
    out["pending_by_kind"] = {
        r["kind"]: r["n"] for r in ledger.db.execute(
            "SELECT kind, COUNT(*) AS n FROM fetch_jobs "
            "WHERE kind IN (?,?) AND state='pending' GROUP BY kind",
            (JOB_KIND, QC_JOB_KIND)).fetchall()}
    out["done_by_kind"] = {}
    policy = policy_fingerprint(scfg)
    previous_policy = ledger.db.execute(
        "SELECT content FROM artifacts WHERE kind='semantic_policy' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    if previous_policy is None or previous_policy["content"] != policy:
        ledger.artifact_add("semantic_policy", policy)
    if llm_fn is None:
        llm_fn = semantic.llm_chat
    if jev_client is None and scfg["daily_request_budget"] > 0:
        jev_client = jev.JevClient(
            api_key=env_value("TYPESAFE_API_KEY"), model=scfg["model"],
            attempt_timeout=scfg["attempt_timeout_seconds"],
            job_budget=scfg["job_budget_seconds"],
            max_attempts=scfg["max_attempts_per_try"])
    from mcs_util import unlocked_transport
    original_llm = llm_fn

    @wraps(original_llm)
    def unlocked_llm(*args, **kwargs):
        with unlocked_transport(ledger, run_lock_fd):
            return original_llm(*args, **kwargs)

    llm_fn = unlocked_llm
    original_http = None
    if (run_lock_fd is not None and jev_client is not None
            and getattr(jev_client, "_mcs_jev_hookable", False)):
        original_http = jev_client._http_request

        def unlocked_http(*args, **kwargs):
            with unlocked_transport(ledger, run_lock_fd):
                return original_http(*args, **kwargs)

        jev_client._http_request = unlocked_http
    try:
        # extract_qc jobs derive from extract_llm artifacts — seeded lazily
        # here so the drain remains the single queue and enabling the
        # feature also backfills artifacts written before it existed.
        if scfg["mode"] != "off" and scfg["extract_qc"] == "annotate" \
                and jev_client is not None:
            try:
                _qc_seed(ledger, time.time())
            except Exception:
                result["errors"].append("semantic: qc_seed_failed")
        qc_active = (scfg["extract_qc"] == "annotate"
                     and jev_client is not None)
        kinds = (JOB_KIND, QC_JOB_KIND) if qc_active else (JOB_KIND,)
        # arrival-descended seeds outrank import/replay seeds: a deep
        # backfill must never starve a fresh notification's evaluation
        # (§19.1's existing-work-first ordering applied inside the queue
        # too). 'eligible' is set at seed time and survives payload merges,
        # so a merged arrival+history job keeps its priority.
        # T14: the backfill cohort additionally gets a guaranteed share of
        # every window — a continuous arrival stream cannot starve runnable
        # backlog (persisted accounting row keeps it restart-safe).
        # QC dedicated frame (observed 2026-09-30): kind ordering puts every
        # semantic row ahead of QC in both cohorts, and a ~240 s generation
        # consumes the whole window — QC rows sat pending for hours at
        # attempts=0. QC gets a bounded share of the window, served FIRST
        # (each pass is one bounded chunked eval), expanding into whatever
        # the semantic lanes leave unused. A 1-job window cannot be split —
        # QC falls back to the persisted cohort alternation inside the
        # non-eligible lane.
        qc_rows = []
        if qc_active and max_jobs > 1:
            qc_rows = ledger.db.execute(
                "SELECT * FROM fetch_jobs WHERE state='pending' "
                "AND kind=? AND next_try<=? ORDER BY job_id LIMIT ?",
                (QC_JOB_KIND, time.time(), max_jobs)).fetchall()
        qc_share = min(len(qc_rows), max(1, max_jobs // 4), max_jobs - 1)
        qc_jobs = list(qc_rows[:qc_share])
        arrivals, backfill, sched_mut = _due_lanes(
            ledger, (JOB_KIND,) if qc_jobs else kinds,
            max_jobs - len(qc_jobs))
        # unused lane capacity flows back to QC — same rule the backfill
        # cohort uses against an idle arrival lane
        spare = max_jobs - len(qc_jobs) - len(arrivals) - len(backfill)
        if spare > 0:
            qc_jobs += list(qc_rows[qc_share:qc_share + spare])
        if lane is not None:
            eligible = ("json_valid(payload) AND "
                        "COALESCE(json_extract(payload,'$.eligible'),0)=1")
            # Fresh arrivals use realtime. Background workers may also finish
            # older arrival-seeded jobs; a per-job flock excludes double work.
            where = f"AND kind=? AND {eligible}" if lane == "realtime" else ""
            params = (JOB_KIND,) if lane == "realtime" else ()
            order = "updated_at DESC, job_id DESC" if lane == "realtime" else "job_id"
            rows = ledger.db.execute(
                f"SELECT * FROM fetch_jobs WHERE state='pending' AND next_try<=? "
                f"AND kind IN ({','.join('?' * len(kinds))}) {where} "
                f"ORDER BY {order} LIMIT ?",
                (time.time(), *kinds, *params, max_jobs + 8)).fetchall()
            # Include extra candidates so another worker's current job cannot
            # hide the next runnable item. Work remains bounded by max_jobs.
            qc_jobs = [j for j in rows if j['kind'] == QC_JOB_KIND]
            arrivals = [j for j in rows if j['kind'] == JOB_KIND
                        and runtime.parse_payload(j).get('eligible')]
            backfill = [j for j in rows if j['kind'] == JOB_KIND
                        and not runtime.parse_payload(j).get('eligible')]
            sched_mut = {"turn": None}
        due = (list(rows) if lane is not None else
               qc_jobs + list(arrivals) + list(backfill))
        cohort_of = {id(job): "qc" for job in qc_jobs}
        cohort_of.update({id(job): "arrival" for job in arrivals})
        cohort_of.update({id(job): "backfill" for job in backfill})
        out["lanes"] = {"arrival": len(arrivals), "backfill": len(backfill)}
        if qc_active:
            out["lanes"]["qc"] = len(qc_jobs)
        selected = {"qc": 0, "arrival": 0, "backfill": 0}
        for job in due:
            if sum(selected.values()) >= max_jobs:
                break
            with _job_lock(ledger, job["job_id"]) as held:
                if not held:
                    continue
                token = runtime.JobToken.from_row(job)
                payload = runtime.parse_payload(job)
                limit = runtime.attempt_limit(payload)
                if runtime.circuit_open(ledger):
                    out["circuit_open"] = True
                    break
                from mcs_operations import paused
                if paused(ledger.db, job["project_id"]):
                    runtime.transition(ledger, token, "defer", retry_in=300)
                    out["deferred"] += 1
                    continue
                if cfg_path is not None:
                    current_cfg = load_config(cfg_path)
                    scfg, errs = semantic_config(current_cfg)
                    cfg_generation = runtime.config_generation(current_cfg)
                    for e in errs:
                        if e not in result["errors"]:
                            result["errors"].append(e)
                    if scfg["mode"] == "off":
                        runtime.transition(ledger, token, "defer", retry_in=300)
                        out["deferred"] += 1
                        out["mode"] = "off"
                        break
                # A semantic job's first call is exempt from the in-call reserve
                # gate — dispatching one under the call reserve burns a doomed
                # generation and a retry attempt (2026-10-01: revived jobs
                # failed at the attempt ceiling doing exactly this in 120 s
                # batches). Only start one when a call can plausibly fit — the
                # same reserve the gate applies to follow-up calls; further
                # stages then defer via BudgetShort for free. Trailing QC rows
                # need only ~15 s.
                floor = deadline - (15.0 if job["kind"] == QC_JOB_KIND else
                                    min(runtime.LLM_CALL_RESERVE_S,
                                        float(scfg["job_budget_seconds"]) * 2 / 3))
                if time.monotonic() > floor:
                    if job["kind"] == QC_JOB_KIND:
                        out["deferred"] += 1
                        break
                    continue
                if scfg["project_ids"] is not None \
                        and job["project_id"] not in scfg["project_ids"]:
                    # outside the rollout scope — defer instead of leaving it
                    # due-now, so a backlog of out-of-scope rows cannot fill
                    # the whole max_jobs window and starve in-scope work. The
                    # row stays pending: a later project_ids change resumes it.
                    runtime.transition(ledger, token, "defer", retry_in=300)
                    out["deferred"] += 1
                    continue
                if token.attempts < 0 or token.attempts >= limit:
                    # A row can be re-seeded or manually edited while it is waiting
                    # in this due-list snapshot.  Close an exhausted row with the
                    # complete token CAS before any client, budget, or network path.
                    # Keep OFF/paused/out-of-scope rows untouched; they retain the
                    # existing queue hold contract until the feature can run again.
                    if runtime.transition(ledger, token, "failed"):
                        out["failed"] += 1
                    else:
                        out["deferred"] += 1
                    continue
                if jev_client is not None:
                    # the daily cap binds at REQUEST granularity, not just job
                    # granularity — one job's claim-audit + loop-relation calls
                    # must not overshoot it mid-flight (§13.5)
                    remaining = (scfg["daily_request_budget"]
                                 - jev_usage_today(ledger))
                    if remaining <= 0:
                        result["errors"].append(
                            "semantic: daily_budget_exhausted")
                        out["budget_exhausted"] = True
                        break
                    jev_client.request_cap = (jev_client.requests_made
                                              + remaining)
                    if hasattr(jev_client, "attempt_timeout"):
                        jev_client.attempt_timeout = scfg["attempt_timeout_seconds"]
                    if hasattr(jev_client, "job_budget"):
                        jev_client.job_budget = scfg["job_budget_seconds"]
                reserve_fn = None
                if (jev_client is not None
                        and getattr(jev_client, "_mcs_jev_hookable", False)):
                    reserve_fn = runtime.usage_reserver(
                        ledger, token, kind=KIND_USAGE, model=jev.JEV_MODEL,
                        project_id=job["project_id"], message_id=job["message_id"])
                # only now has the job cleared every gate — the persisted
                # fairness counters count jobs that were actually served, not
                # selections lost to circuit/budget/pause breaks (T14)
                selected[cohort_of[id(job)]] += 1
                req0 = jev_client.requests_made if jev_client is not None else 0
                usage_before = dict(getattr(jev_client, "usage_totals", {}))
                job_started = time.perf_counter()
                job_age = max(0.0, time.time() - job["created_at"])
                # T14 phase split: queue wait (job_age_s) | model calls | Jev
                # evaluation | post-processing (validate→facts→render). The
                # wrappers only time calls — all attribution stays honest, and
                # the residual post_s covers the work between them.
                timed_jev = _TimedClient(jev_client) \
                    if jev_client is not None else None
                timed_llm, llm_acc = _timed_llm(llm_fn)
                art0 = _last_stage_artifact(ledger, job["project_id"])
                try:
                    if job["kind"] == QC_JOB_KIND:
                        status = _process_qc_job(
                            ledger, scfg, job, timed_jev, deadline,
                            reserve_fn=reserve_fn, cfg_path=cfg_path,
                            config_generation=cfg_generation)
                    else:
                        status = semantic._process_job(
                            ledger, scfg, job, timed_jev, timed_llm, deadline,
                            cfg_path=cfg_path,
                            config_generation=cfg_generation,
                            reserve_fn=reserve_fn)
                except Exception as e:
                    runtime.transition(ledger, token, "retry", retry_in=300,
                                       max_attempts=limit)
                    result["errors"].append(
                        f"semantic {job['message_id']}: {type(e).__name__}")
                    out["failed"] += 1
                    status = None
                job_elapsed = time.perf_counter() - job_started
                job_progressed = _last_stage_artifact(ledger, job["project_id"]) > art0
                if job_progressed:
                    out["progressed"] += 1
                budget_short = status == "deferred_short"
                if budget_short and not job_progressed:
                    # nothing persisted this pass: a free defer would loop
                    # forever (and re-spend Jev every minute) — charge a bounded
                    # attempt like any other overrun
                    status = "retry"
                # durable per-attempt request delta — the daily budget ledger
                # (jev_usage_today) is exact and covers claim-audit and loop
                # calls, not just the primary assessment
                used = (jev_client.requests_made - req0) \
                    if jev_client is not None else 0
                usage_after = getattr(jev_client, "usage_totals", {})
                usage = {key: usage_after.get(key, 0) - usage_before.get(key, 0)
                         for key in ("input_tokens", "output_tokens", "reported_requests")}
                usage["unreported_requests"] = used - usage["reported_requests"]
                llm_s = llm_acc["s"]
                jev_s = timed_jev.elapsed_s if timed_jev is not None else 0.0
                out["job_metrics"].append({
                    "job_id": job["job_id"], "project_id": job["project_id"],
                    "kind": job["kind"],
                    "cohort": cohort_of.get(id(job), "backfill"),
                    "generation": token.generation,
                    "status": status if status is not None else "error",
                    # phase split — queue wait vs model call vs Jev evaluation
                    # vs the residual validate/render phase (T14)
                    "queue_wait_s": job_age,
                    "llm_s": llm_s,
                    "jev_s": jev_s,
                    "post_s": max(0.0, job_elapsed - llm_s - jev_s),
                    "elapsed_s": job_elapsed, "job_age_s": job_age,
                    "jev_requests": used, "usage": usage,
                    # last Jev error class/kind of this pass — a job that ends
                    # "failed" after a few Jev calls was previously
                    # undiagnosable from the run record (2026-09-30)
                    "jev_error": _jev_error_brief(
                        getattr(jev_client, "last_error", None))
                    if status not in (None, "done") else None,
                })
                if status == "done":
                    out["done_by_kind"][job["kind"]] = \
                        out["done_by_kind"].get(job["kind"], 0) + 1
                if used:
                    runtime.record_circuit_result(ledger, getattr(jev_client, "last_error", None))
                # Real Jev calls reserve one usage row before POST.  A crash after
                # that commit therefore still spends the daily cap; adding the old
                # post-job delta would double count it.  Injected fake clients keep
                # the delta row for compatibility with offline tests.
                if used and reserve_fn is None:
                    ledger.artifact_add(
                        KIND_USAGE, json.dumps({"job_id": job["job_id"]}),
                        project_id=job["project_id"],
                        message_id=job["message_id"], model=jev.JEV_MODEL,
                        meta={"jev_requests": used})
                if status is None:
                    continue
                if status == "done":
                    out["done"] += 1
                elif status in ("deferred", "deferred_short"):
                    out["deferred"] += 1
                    runtime.transition(ledger, token, "defer", retry_in=60)
                    if status == "deferred_short":
                        # less than one call reserve is left: the next job's
                        # first call is exempt from the reserve gate and would
                        # be dispatched only to time out at the deadline
                        break
                elif status == "deferred_backoff":
                    out["deferred"] += 1
                    runtime.transition(ledger, token, "defer",
                                       retry_in=NOT_SENT_BACKOFF_S)
                elif status == "retry":
                    runtime.transition(ledger, token, "retry", retry_in=300,
                                       max_attempts=limit)
                    out["deferred"] += 1
                    if budget_short:
                        break      # same reason as deferred_short: no call fits
                elif status == "failed":
                    runtime.transition(ledger, token, "retry", max_attempts=1)
                    out["failed"] += 1
                elif status == "stale":
                    # The worker that observed this row no longer owns it.  Do not
                    # defer/retry/done the replacement generation by ID alone.
                    out["deferred"] += 1
                else:
                    out["failed"] += 1
        if scfg["mode"] == "enforce" and scfg["summary_mode"] == "enforce":
            try:
                out["degraded_notices"] = _emit_degraded(ledger, scfg)
            except Exception:
                out["degraded_notices"] = 0
                result["errors"].append("semantic: degraded_scan_failed")
        kind_ph = ",".join("?" * len(kinds))
        out["left"] = ledger.db.execute(
            f"SELECT COUNT(*) FROM fetch_jobs WHERE kind IN ({kind_ph}) "
            "AND state='pending' AND next_try<=?", (*kinds, time.time())).fetchone()[0]
        out["elapsed_s"] = time.perf_counter() - drain_started
        if due:
            # persisted fairness accounting — survives restarts, feeds
            # semantic_observe's scheduler section (T14)
            try:
                sched = _sched_state(ledger)
                for cohort in ("arrival", "backfill"):
                    sched[cohort + "_selected"] = \
                        sched.get(cohort + "_selected", 0) + selected[cohort]
                if qc_active:
                    sched["qc_selected"] = \
                        sched.get("qc_selected", 0) + selected["qc"]
                if selected["backfill"]:
                    sched["backfill_last_served_at"] = time.time()
                if selected["qc"]:
                    sched["qc_last_served_at"] = time.time()
                if sched_mut.get("turn"):
                    sched["turn"] = sched_mut["turn"]
                _sched_write(ledger, sched)
            except Exception:
                # accounting must never break the drain itself
                result["errors"].append("semantic: sched_persist_failed")
            # Bounded per-run metrics artifact — observe() aggregates the
            # recent ones; a drain that attempted work leaves durable
            # evidence instead of an in-memory-only result (T14).
            try:
                ledger.artifact_add(
                    "semantic_drain_run",
                    json.dumps({
                        "v": 1, "mode": scfg["mode"],
                        "done": out["done"], "deferred": out["deferred"],
                        "failed": out["failed"],
                        "budget_exhausted": out["budget_exhausted"],
                        "left": out["left"], "elapsed_s": out["elapsed_s"],
                        "oldest_pending_age_s":
                            out["oldest_pending_job_age_s"],
                        "lanes": out["lanes"],
                        "job_metrics": out["job_metrics"][:64]},
                        ensure_ascii=False),
                    model="semantic_drain")
            except Exception:
                result["errors"].append("semantic: metrics_persist_failed")
        return out
    finally:
        if original_http is not None:
            jev_client._http_request = original_http





# Bounded automatic retry of exhausted semantic jobs (owner request
# 2026-09-30). Bounded three ways so a permanently broken input cannot
# burn the Jev budget: at most REVIVE_MAX jobs per pass, each
# job at most REVIVE_PER_INPUT times for the same input
# generation, and only after REVIVE_COOLDOWN_S since it failed.
# Each revival grants exactly one more attempt (manual_attempt_limit =
# attempts + 1); an input change still resets everything as before.
REVIVE_MAX = 20
REVIVE_PER_INPUT = 3
REVIVE_COOLDOWN_S = 6 * 3600


def revive_failed(ledger, now: float | None = None) -> dict:
    """Give exhausted 'failed' semantic jobs one more bounded attempt.

    Oldest failures first. The job keeps its generation and accumulated
    attempts; ``auto_retry`` counts revivals of the current input so the
    per-input cap survives restarts, and ``retry_command_id`` fences a
    worker that captured the pre-revival payload (same mechanism as the
    human retry command)."""
    now = time.time() if now is None else now
    out = {"revived": 0, "skipped_cap": 0}
    # capped jobs are excluded BEFORE the LIMIT — selecting them first
    # let a capped prefix starve every eligible job behind it
    capped = ("(CASE WHEN json_valid(payload) AND "
              "json_type(payload,'$.auto_retry')='integer' "
              "THEN json_extract(payload,'$.auto_retry') ELSE 0 END)")
    where = "WHERE kind=? AND state='failed' AND updated_at<=? AND "
    rows = ledger.db.execute(
        "SELECT job_id,attempts,payload FROM fetch_jobs " + where
        + capped + "<? ORDER BY updated_at LIMIT ?",
        (JOB_KIND, now - REVIVE_COOLDOWN_S, REVIVE_PER_INPUT,
         REVIVE_MAX * 4)).fetchall()
    out["skipped_cap"] = ledger.db.execute(
        "SELECT COUNT(*) FROM fetch_jobs " + where + capped + ">=?",
        (JOB_KIND, now - REVIVE_COOLDOWN_S,
         REVIVE_PER_INPUT)).fetchone()[0]
    for row in rows:
        if out["revived"] >= REVIVE_MAX:
            break
        try:
            pl = json.loads(row["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(pl, dict):
            continue
        n = pl.get("auto_retry")
        n = n if type(n) is int and n >= 0 else 0
        if n >= REVIVE_PER_INPUT:
            out["skipped_cap"] += 1
            continue
        attempts = int(row["attempts"] or 0)
        pl["auto_retry"] = n + 1
        pl["manual_attempt_limit"] = max(
            attempts + 1, runtime.attempt_limit(pl))
        pl["retry_command_id"] = f"auto-{int(now)}-{row['job_id']}"
        with ledger.db:
            ledger.db.execute(
                "UPDATE fetch_jobs SET state='pending',payload=?,next_try=?,"
                "updated_at=? WHERE job_id=? AND state='failed'",
                (json.dumps(pl, ensure_ascii=False, sort_keys=True),
                 now, now, row["job_id"]))
        out["revived"] += 1
    return out


def main() -> int:
    """Bounded background drain; transport releases the ingestion lock."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--drain", action="store_true",
                    help="loop run_due until the queue is empty or "
                         "--stop-after expires")
    ap.add_argument("--stop-after", type=float, default=3600.0)
    ap.add_argument("--max-jobs", type=int, default=8)
    ap.add_argument("--revive-failed", action="store_true",
                    help="before draining, give exhausted failed jobs one "
                         "more bounded attempt")
    args = ap.parse_args()
    if not args.drain:
        if not args.revive_failed:
            ap.error("--drain or --revive-failed required")
        from ledger import Ledger
        from mcs_util import DB, acquire_run_lock
        fd = acquire_run_lock()
        if fd is None:
            return 3
        try:
            led = Ledger(DB)
            try:
                print(json.dumps(revive_failed(led)))
            finally:
                led.close()
        finally:
            os.close(fd)
        return 0
    if not 1 <= args.max_jobs <= 32:
        ap.error("max-jobs must be between 1 and 32")
    if not math.isfinite(args.stop_after) or args.stop_after < 0:
        ap.error("stop-after must be a finite nonnegative number")
    from ledger import Ledger
    from mcs_util import acquire_run_lock, CONF_PATH, HOME
    led = None
    stop = time.monotonic() + args.stop_after
    totals = {"done": 0, "deferred": 0, "failed": 0, "batches": 0}
    try:
        while time.monotonic() < stop:
            lock_fd = acquire_run_lock()
            if lock_fd is None:
                time.sleep(min(10, max(0.0, stop - time.monotonic())))
                continue
            try:
                if led is None:
                    led = Ledger(os.path.join(HOME, "data", "ledger.db"))
                    if args.revive_failed:
                        totals["revive"] = revive_failed(led)
                cfg = load_config()
                # the batch must fit one full job budget or a ~300 s
                # canonical generation can never complete inside it —
                # 120 s batches burned a doomed call + an attempt on
                # every revived job (2026-10-01: 11 jobs failed at the
                # attempt ceiling with llm_s≈118 each pass). Still
                # bounded: the tick waits at most job_budget+30 s.
                try:
                    batch_s = max(
                        120.0, float(semantic_config(cfg)[0]
                                     ["job_budget_seconds"]) + 30.0)
                except Exception:
                    batch_s = 480.0
                result = {"errors": []}
                out = run_due(
                    led, cfg, result,
                    deadline=min(stop, time.monotonic() + batch_s),
                    max_jobs=args.max_jobs, cfg_path=CONF_PATH,
                    run_lock_fd=lock_fd, lane="backlog")
            finally:
                os.close(lock_fd)
            totals["batches"] += 1
            for k in ("done", "deferred", "failed"):
                totals[k] += out.get(k) or 0
            totals["left"] = out.get("left")
            # ts/pid: a window cut short (e.g. gateway restart kills
            # the catchup) is judged later from semantic_drain.log
            print(json.dumps({**out, "batches": totals["batches"],
                              "ts": time.time(), "pid": os.getpid()},
                             ensure_ascii=False, default=str),
                  flush=True)
            if out.get("left") == 0:
                totals["stopped"] = "queue_empty"
                break
            if not out.get("done"):
                # deferred/failed-only batches mean every claimable job
                # is backed off — hot-looping re-claims the same rows.
                time.sleep(min(10, max(0.0, stop - time.monotonic())))
            else:
                time.sleep(min(1, max(0.0, stop - time.monotonic())))
    finally:
        if led is not None:
            led.close()
    print(json.dumps({**totals, "ts": time.time(), "pid": os.getpid()},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
