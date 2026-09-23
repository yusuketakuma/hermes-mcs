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
import time

from mcs_requests import payload_hash
from mcs_util import env_value, load_config
import semantic_jev as jev
import semantic_runtime as runtime
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
from semantic_qc import (QC_MAX_ITEMS, _process_qc_job,  # noqa: F401
                         _qc_questions, _qc_seed)  # noqa: F401

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
        if m.get("fingerprint") == fp \
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
        project_id=pid, message_id=mid, model=semantic.LLM_MODEL,
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
    # fact_source selects the canonical path: "shadow" still feeds
    # consumers from the legacy extractor while writing the v2
    # document for comparison; "canonical" makes the v2 document the
    # fact source and refuses to proceed while it stays incomplete —
    # it never silently falls back to legacy.
    fact_source = scfg.get("fact_source", "legacy")
    v2_doc = None
    if fact_source != "legacy":
        from semantic_extraction import extract_facts_v2
        from semantic_projection import project_v2_facts
        prev_v2 = _current(ledger, KIND_FACTS_V2, mid, fp)
        if prev_v2 is not None:
            v2_doc = prev_v2["content"]
        else:
            if time.monotonic() > deadline - 5:
                return {"outcome": "incomplete", "v2_doc": None}
            v2_result = extract_facts_v2(
                llm_fn, member, deadline - 5, ledger=ledger,
                source_fingerprint=fp, project_id=pid,
                jev_client=jev_client)
            if not v2_result["extraction_complete"]:
                if fact_source == "canonical":
                    return {"outcome": "retryable"
                            if v2_result["failure_reason"] == "model"
                            else "incomplete", "v2_doc": None}
                v2_doc = None      # shadow: incomplete doc not stored
            else:
                v2_doc = v2_result["doc"]
                ledger.artifact_add(
                    KIND_FACTS_V2,
                    json.dumps(v2_doc, ensure_ascii=False,
                               allow_nan=False),
                    project_id=pid, message_id=mid,
                    model=semantic.LLM_MODEL,
                    meta={"fingerprint": fp,
                          "policy_fingerprint": policy,
                          "schema": SCHEMA_VERSION,
                          "fact_source": fact_source,
                          "coverage_status":
                              v2_doc["coverage"]["status"],
                          "facts": len(v2_doc["facts"]),
                          "open_obligations": len(
                              v2_doc["coverage"]
                              ["open_obligation_ids"])})
        if fact_source == "canonical" and v2_doc is not None \
                and v2_doc["coverage"]["status"] != "complete":
            # Adjudicated-but-incomplete canonical coverage holds the
            # generation — never degrade to the legacy projection.
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
            doc_hash = payload_hash(
                {"f": v2_doc["facts"], "e": v2_doc["evidence"]})
            prev_fa = _current(ledger, KIND_FACT_AUDIT, mid, fp)
            if prev_fa is not None \
                    and prev_fa["meta"].get("doc_hash") == doc_hash:
                fact_audit = prev_fa["content"]
            else:
                if time.monotonic() > deadline - 5:
                    break
                from semantic_audit import audit_facts_v2
                fact_audit = audit_facts_v2(
                    jev_client, v2_doc, member["body_original"],
                    deadline - 5,
                    match_threshold=scfg["match_threshold"])
                ledger.artifact_add(
                    KIND_FACT_AUDIT,
                    json.dumps(
                        {"status": fact_audit["status"],
                         "evaluated": fact_audit["evaluated"],
                         "findings": fact_audit["findings"],
                         "fact_verdicts":
                             fact_audit["fact_verdicts"]},
                        ensure_ascii=False, allow_nan=False),
                    project_id=pid, message_id=mid,
                    model=jev.JEV_MODEL,
                    meta={"fingerprint": fp,
                          "policy_fingerprint": policy,
                          "schema": SCHEMA_VERSION,
                          "doc_hash": doc_hash,
                          "audit_status": fact_audit["status"]})
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
                    break
                from semantic_extraction import repair_facts_v2
                repair = repair_facts_v2(
                    llm_fn, member, v2_doc, rejected, deadline - 5)
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
                    model=semantic.LLM_MODEL,
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
                        model=semantic.LLM_MODEL,
                        meta={"fingerprint": fp,
                              "policy_fingerprint": policy,
                              "schema": SCHEMA_VERSION,
                              "fact_source": fact_source,
                              "repaired": True,
                              "coverage_status":
                                  v2_doc["coverage"]["status"],
                              "facts": len(v2_doc["facts"])})
                    continue
            break
        if not audited:
            return {"outcome": fail_kind, "v2_doc": v2_doc}
        facts = project_v2_facts(v2_doc)
        # T12: audited canonical docs also publish a legacy-shaped
        # projection so the read side (stats/queries/rollup/signals)
        # keeps working during migration — the artifact carries the
        # message hash so "current" predicates bind it like an
        # extract_llm row.
        if _current(ledger, KIND_FACT_PROJ, mid, fp) is None:
            from semantic_projection import project_v2_doc_legacy
            ledger.artifact_add(
                KIND_FACT_PROJ,
                json.dumps(project_v2_doc_legacy(v2_doc),
                           ensure_ascii=False, allow_nan=False),
                project_id=pid, message_id=mid,
                model=semantic.LLM_MODEL,
                meta={"fingerprint": fp,
                      "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION,
                      "hash": member["revision"],
                      "doc_hash": payload_hash(
                          {"f": v2_doc["facts"],
                           "e": v2_doc["evidence"]})})
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
                project_id=pid, message_id=mid, model=semantic.LLM_MODEL,
                meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
                      "chunks_total": len(_chunks(
                          member["body_original"])),
                      "dropped_by_cap": f_dropped})
    return {"outcome": None, "facts": facts, "v2_doc": v2_doc}


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

    # The adapters below make every actual Jev/local-model boundary pass
    # through the same identity check.  A real Jev client additionally gets
    # a hook before each internal retry attempt and a durable reservation
    # immediately before the POST.
    jev_client, _ = runtime.bind_jev(jev_client, guard, reserve_fn)
    llm_fn = runtime.guarded_llm(llm_fn, guard, deadline,
                                 timeout_cap=semantic.LLM_TIMEOUT)
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
    facts_by_target: dict[int, list] = {}
    coverage_by_target: dict[int, list] = {}
    detail_findings: dict[int, list] = {}
    verdicts: dict[int, dict] = {}
    incomplete = False
    hard_fail = False   # non-retryable Jev error — bound the retries
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
                    if _jev_failure_class(e) == "resource":
                        resource_wait = True
                    elif e.retryable:
                        retryable_failure = True
                    else:
                        hard_fail = True
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
            if stage["outcome"] == "retryable":
                retryable_failure = True
            elif stage["outcome"] == "hard_fail":
                hard_fail = True
            incomplete = True
            break
        facts = stage["facts"]
        v2_doc = stage["v2_doc"]
        fact_source = scfg.get("fact_source", "legacy")
        facts_by_target[mid] = facts
        from semantic_assessment import evaluate_medication_events
        event_details = evaluate_medication_events(
            ledger, bundle, mid, facts, jev_client, scfg, deadline - 5)
        guard("medication_detail_result")
        detail_findings[mid] = event_details["findings"]
        if not event_details["complete"] and event_details["failure_reason"] != "invalid":
            incomplete = True
            error = getattr(jev_client, "last_error", None)
            if error is not None and _jev_failure_class(error) == "failed":
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
                if error is not None and _jev_failure_class(error) == "failed":
                    hard_fail = True
                elif error is not None and _jev_failure_class(error) == "retry":
                    retryable_failure = True
                elif coverage.get("failure_reason") == "model":
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
                summary, summary_reason = summarize(
                    llm_fn, bundle, mid, facts, verdicts.get(mid, {}),
                    deadline=deadline, return_reason=True)
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
                    project_id=pid, message_id=mid, model=semantic.LLM_MODEL,
                    meta={"fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
                          "technical_status": "pending"})
            incomplete = True
            continue
        # Preserve the pre-audit output for the fixed-bundle comparison (§24.1).
        # It is never eligible for publication or automatic adoption.
        if existing is None and _current(ledger, "semantic_candidate", mid, fp, policy) is None:
            guard("candidate_snapshot")
            ledger.artifact_add(
                "semantic_candidate", json.dumps(summary, ensure_ascii=False),
                project_id=pid, message_id=mid, model=semantic.LLM_MODEL,
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
                error = getattr(jev_client, "last_error", None)
                if error is not None:
                    if _jev_failure_class(error) == "resource":
                        resource_wait = True
                    elif getattr(error, "retryable", False):
                        retryable_failure = True
                    else:
                        hard_fail = True
            findings = code_f + jev_f
            status = audit_status_for(code_f, jev_f, evaluated,
                                      repaired)
            if status == "REPAIR_REQUIRED" and not repaired:
                guard("repair_reservation")
                ledger.artifact_add(
                    "semantic_repair", json.dumps({"target_message_id": mid}),
                    project_id=pid, message_id=mid, model=semantic.LLM_MODEL,
                    meta={"fingerprint": fp, "policy_fingerprint": policy, "repair_count": 1})
                repaired = True
                summary2 = summarize(
                    llm_fn, bundle, mid, facts, verdicts.get(mid, {}),
                    feedback=[f.get("code", "") + " " +
                              f.get("statement", f.get("claim", ""))
                              for f in code_f + jev_f])
                if summary2 is not None:
                    summary = summary2
                    summary["_facts"] = facts
                    continue
                status = "NEEDS_REVIEW"
                findings.append({"code": "repair_unavailable"})
            break
        if fact_source == "canonical" and v2_doc is not None:
            # T11: the adjudicated contract drives a mandatory render
            # layer — verified facts stay visible and non-terminal
            # obligations are disclosed as limitations even if the
            # model summary dropped them.
            from semantic_render import mandatory_render
            mandatory = mandatory_render(v2_doc)
            if mandatory["facts"]:
                summary["mandatory_facts"] = mandatory["facts"]
            if mandatory["limitations"]:
                existing_lims = summary.get("limitations") or []
                summary["limitations"] = existing_lims + [
                    x for x in mandatory["limitations"]
                    if x not in existing_lims]
        summary["audit_status"] = status
        results[mid] = {"summary": summary, "status": status,
                        "findings": findings, "repaired": repaired,
                        "fresh": True,
                        "jev_requests": (jev_client.requests_made - req0)
                        if jev_client else 0}
    if incomplete:
        if results:
            try:
                guard("partial_promote")
            except runtime.RuntimeGuardError:
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
        if hard_fail:
            return "failed"
        if retryable_failure:
            return "retry"
        return "deferred"

    # generation guard: the bundle, config, and complete job identity must
    # still be current before promotion.  The same guard also runs before
    # every Jev/local-model call through the wrappers above.
    stale = False
    try:
        guard("promote")
    except runtime.RuntimeGuardError:
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
            error = getattr(jev_client, "last_error", None)
            if error is not None:
                if _jev_failure_class(error) == "resource":
                    resource_wait = True
                elif getattr(error, "retryable", False):
                    retryable_failure = True
                else:
                    hard_fail = True
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
        if not stale and not pending and loops_done:
            if not runtime.transition_tx(ledger, token, "done"):
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

    try:
        return _process_job_inner(
            ledger, scfg, job, jev_client, tracked_llm, deadline,
            cfg_path=cfg_path, config_generation=config_generation,
            reserve_fn=reserve_fn)
    except runtime.RuntimeStale:
        return "stale"
    except runtime.RuntimeOff:
        return "stale"
    except runtime.RuntimeBudget:
        sent = (jev_client is not None
                and jev_client.requests_made > requests_before)
        return "retry" if sent or llm_started else "deferred"


def run_due(ledger, cfg: dict, result: dict, deadline: float,
            jev_client=None, llm_fn=None, max_jobs: int = 4,
            cfg_path: str | None = None) -> dict:
    """Drain due semantic jobs inside the tick's remaining budget.
    OFF returns immediately — no job creation, no external calls, no
    auto-drain (AT-053). The caller's shared run lock is held by
    run_check; a standalone CLI takes acquire_run_lock itself.
    cfg_path, when given, re-validates config between jobs so an OFF
    flip mid-run takes effect at the next job boundary (AT-060)."""
    import semantic
    if type(max_jobs) is not int or not 1 <= max_jobs <= 32:
        raise ValueError("semantic_max_jobs_invalid")
    scfg, errors = semantic_config(cfg)
    cfg_generation = runtime.config_generation(cfg)
    for e in errors:
        if e not in result["errors"]:
            result["errors"].append(e)
    out = {"mode": scfg["mode"], "done": 0, "deferred": 0,
           "failed": 0, "left": None, "budget_exhausted": False}
    if scfg["mode"] == "off":
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
    due = ledger.db.execute("""
      SELECT * FROM fetch_jobs
      WHERE state='pending' AND next_try <= ?
        AND kind IN ({})
      ORDER BY CASE WHEN json_valid(payload)
                    AND json_extract(payload, '$.eligible') = 1
                    THEN 0 ELSE 1 END,
               CASE WHEN kind = ? THEN 0 ELSE 1 END, job_id
      LIMIT ?
    """.format(",".join("?" * len(kinds))),
             (time.time(), *kinds, JOB_KIND, max_jobs)).fetchall()
    for job in due:
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
        if time.monotonic() > deadline - 15:
            out["deferred"] += 1
            break
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
        req0 = jev_client.requests_made if jev_client is not None else 0
        usage_before = dict(getattr(jev_client, "usage_totals", {}))
        job_started = time.perf_counter()
        job_age = max(0.0, time.time() - job["created_at"])
        try:
            if job["kind"] == QC_JOB_KIND:
                status = _process_qc_job(
                    ledger, scfg, job, jev_client, deadline,
                    reserve_fn=reserve_fn, cfg_path=cfg_path,
                    config_generation=cfg_generation)
            else:
                status = semantic._process_job(
                    ledger, scfg, job, jev_client, llm_fn, deadline,
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
        # durable per-attempt request delta — the daily budget ledger
        # (jev_usage_today) is exact and covers claim-audit and loop
        # calls, not just the primary assessment
        used = (jev_client.requests_made - req0) \
            if jev_client is not None else 0
        usage_after = getattr(jev_client, "usage_totals", {})
        usage = {key: usage_after.get(key, 0) - usage_before.get(key, 0)
                 for key in ("input_tokens", "output_tokens", "reported_requests")}
        usage["unreported_requests"] = used - usage["reported_requests"]
        out["job_metrics"].append({
            "job_id": job["job_id"], "project_id": job["project_id"],
            "kind": job["kind"],
            "generation": token.generation,
            "status": status if status is not None else "error",
            "elapsed_s": job_elapsed, "job_age_s": job_age,
            "jev_requests": used, "usage": usage,
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
        elif status == "deferred":
            out["deferred"] += 1
            runtime.transition(ledger, token, "defer", retry_in=60)
        elif status == "retry":
            runtime.transition(ledger, token, "retry", retry_in=300,
                               max_attempts=limit)
            out["deferred"] += 1
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
    out["left"] = sum(len(ledger.job_due(limit=50, kind=k))
                      for k in kinds)
    out["elapsed_s"] = time.perf_counter() - drain_started
    return out


def main() -> int:
    """Standalone drain loop (nightly catch-up window).

    ``--drain --stop-after N --max-jobs M``: each iteration takes the
    run lock for one short batch (~2 min max) so the 15-min tick is
    delayed by at most one iteration — never starved by the window.
    Set MCS_LLM_SLOT=1 to lend calls to the RT slot when it is idle."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--drain", action="store_true",
                    help="loop run_due until the queue is empty or "
                         "--stop-after expires")
    ap.add_argument("--stop-after", type=float, default=3600.0)
    ap.add_argument("--max-jobs", type=int, default=8)
    args = ap.parse_args()
    if not args.drain:
        ap.error("--drain required")
    from ledger import Ledger
    from mcs_util import acquire_run_lock, CONF_PATH, HOME
    led = Ledger(os.path.join(HOME, "data", "ledger.db"))
    cfg = load_config()
    stop = time.monotonic() + max(0.0, args.stop_after)
    totals = {"done": 0, "deferred": 0, "failed": 0, "batches": 0}
    try:
        while time.monotonic() < stop:
            lock_fd = acquire_run_lock()
            if lock_fd is None:
                time.sleep(10)   # tick owns the lock — retry shortly
                continue
            try:
                result = {"errors": []}
                out = run_due(
                    led, cfg, result,
                    deadline=min(stop, time.monotonic() + 120.0),
                    max_jobs=args.max_jobs, cfg_path=CONF_PATH)
            finally:
                os.close(lock_fd)
            totals["batches"] += 1
            for k in ("done", "deferred", "failed"):
                totals[k] += out.get(k) or 0
            totals["left"] = out.get("left")
            print(json.dumps({**out, "batches": totals["batches"]},
                             ensure_ascii=False, default=str),
                  flush=True)
            if out.get("left") == 0:
                totals["stopped"] = "queue_empty"
                break
            if not out.get("done"):
                # deferred/failed-only batches mean every claimable job
                # is backed off — hot-looping re-claims the same rows.
                time.sleep(10)
            else:
                time.sleep(1)   # yield the lock between batches
    finally:
        led.close()
    print(json.dumps(totals, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
