"""Process one durable semantic job with guarded inference and stage commits."""
from __future__ import annotations
import semantic_facts
import mcs_util
import semantic_audit
import semantic_render
import semantic_store
import semantic_transport
from functools import wraps
import json
import hashlib
from contextlib import ExitStack, nullcontext
import math
import sqlite3
import time
from mcs_util import load_config, loads_dict, strict_json
from inference_version import (CURRENT_VERSION, VERSION_KEY, is_current)
import semantic_jev as jev
import semantic_runtime as runtime
import semantic_publication as v4
import semantic_conversion as cohort
from semantic_audit import audit_code, audit_status_for
from semantic_loops import update_loops
from semantic_policy import (BACKLOG_BUDGET_RESERVE, KIND_ASSESS, KIND_AUDIT,
                             KIND_BUNDLE, KIND_CANDIDATE, KIND_COVERAGE,
                             KIND_DRAIN_RUN, KIND_FACT_PROJ,
                             KIND_FACTS, KIND_FACTS_V2,
                             KIND_LOOP, KIND_LOOP_EVENT,
                             KIND_PLAN, KIND_REPAIR,
                             KIND_SUMMARY,
                             KIND_SUMMARY_FORMAT_REPAIR, KIND_USAGE,
                             SCHEMA_VERSION, SCHED_KIND,
                             policy_fingerprint, runtime_config)
import semantic_notice
from semantic_store import current_artifact, jev_state, jev_usage_today

from semantic_due_selection import (
    _backlog_commit_reserve,
)
from semantic_fact_stages import (
    _fact_stage,
)
from semantic_summary_stages import (
    _claim_input_binding,
    _empty_repair_candidate,
    _empty_repair_prior,
    _jev_fail_flag,
    _prior_pending_findings,
    _reused_pass_current,
    _store_empty_repair,
    _summarize_observed,
    _summary_producer,
    _summary_provenance,
    _summary_repair_evidence,
    _why,
    _write_result,
)
from semantic_store import KIND_LOOP_COMPLETION as KIND_LOOP_COMPLETION

def _fact_stop_reason(stage):
    return _fixed_stop_reason(stage.get('diagnostic_reason'), f"fact_stage_{stage['outcome']}")



def _fixed_stop_reason(reason, fallback):
    import cloud_backlog
    return (reason if isinstance(reason, str) and reason in cloud_backlog._SEMANTIC_STOP_REASONS
            else fallback)



def _assess_target(ledger, scfg, bundle, jev_client, pid, mid, fp, policy,
                   prev, deadline, *, diagnostics: dict):
    """Primary Jev proposition assessment of one target, durably recorded.

    Returns (verdicts, flag): verdicts is None when no answer arrived (the
    wait/error row is recorded once per status change); flag is the
    _jev_fail_flag of a raised JevError, else None."""
    flag = None
    state = jev_state(bundle, mid)
    meta_base = {VERSION_KEY: CURRENT_VERSION, "fingerprint": fp, "policy_fingerprint": policy, "model": jev.JEV_MODEL,
                 "registry": jev.REGISTRY_VERSION,
                 "schema": SCHEMA_VERSION}
    answers = None
    try:
        old = prev["content"].get("verdicts", {}) if prev else {}
        if semantic_store._partial_assessment(old, scfg) != old:
            raise ValueError("semantic_partial_assessment_invalid")
    except (ValueError, TypeError, KeyError):
        _why(diagnostics, "assess_partial_invalid")
        return None, "hard"
    partial = dict(old)
    req0 = jev_client.requests_made if jev_client else 0
    if jev_client is not None and state is not None and semantic_store._verified_prefilter(ledger, pid, mid):
        if prev and prev["content"].get("verdicts") != {}:
            _why(diagnostics, "assess_partial_invalid")
            return None, "hard"
        # the local prefilter already settled this body as carrying no clinical
        # signal (a lone greeting): asking twelve clinical propositions about it
        # spends a request for a known answer
        ledger.artifact_add(
            KIND_ASSESS,
            json.dumps({"target_message_id": mid, "verdicts": {}, "detail": {}}, ensure_ascii=False),
            project_id=pid, message_id=mid, model=jev.JEV_MODEL,
            meta={**meta_base, "technical_status": "complete", "jev_requests": 0,
                  "skipped": "prefilter_no_signal"})
        return {}, flag
    if jev_client is not None and state is not None:
        qs = {k: jev.noul_question(p["instructions"], p["true"],
                                   p["false"])
              for k, p in jev.active_propositions(scfg).items() if k not in old}
        if not qs:
            _why(diagnostics, "assess_partial_invalid")
            return None, "hard"
        try:
            out = jev.evaluate_chunked(
                jev_client, state, qs, deadline,
                scfg["max_questions_per_request"])
            answers = out["answers"]
        except jev.JevError as e:
            meta_base["technical_status"] = (
                "retry_wait" if e.retryable else "error")
            meta_base["error_kind"] = e.kind
            flag = _jev_fail_flag(e)
            try:
                if hasattr(e, "partial_answers"):
                    new = semantic_store._partial_assessment(e.partial_answers, scfg)
                    if set(new) - set(qs) or set(new) & set(old):
                        raise ValueError("semantic_partial_assessment_conflict")
                    partial = {**old, **new}
                    e.partial_verdicts = partial
            except (ValueError, TypeError):
                e.partial_invalid = True
                _why(diagnostics, "assess_partial_invalid")
                return None, "hard"
            _why(diagnostics, f"assess_jev_{flag}")
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
                prev["meta"].get("error_kind"), prev["content"].get("verdicts")) != (
                meta_base["technical_status"],
                meta_base.get("error_kind"), partial):
            ledger.artifact_add(
                KIND_ASSESS,
                json.dumps({"target_message_id": mid,
                            "verdicts": partial},
                           ensure_ascii=False),
                project_id=pid, message_id=mid,
                model=jev.JEV_MODEL, meta=meta_base)
        return None, flag
    target_verdicts = {k: {"noul": a["noul"],
                           "verdict": jev.verdict_for(
                               a["noul"], scfg["match_threshold"],
                               scfg["nomatch_threshold"])}
                       for k, a in answers.items()}
    try:
        if (set(target_verdicts) != set(qs) or set(target_verdicts) & set(old)
                or semantic_store._partial_assessment(target_verdicts, scfg) != target_verdicts):
            raise ValueError("semantic_partial_assessment_conflict")
        target_verdicts = {**old, **target_verdicts}
        if set(target_verdicts) != set(jev.active_propositions(scfg)):
            raise ValueError("semantic_partial_assessment_incomplete")
    except (ValueError, TypeError):
        _why(diagnostics, "assess_partial_invalid")
        return None, "hard"
    ledger.artifact_add(
        KIND_ASSESS,
        # "detail" stays in the stored shape (medication detail
        # lives in semantic_assessment rows)
        json.dumps({"target_message_id": mid,
                    "verdicts": target_verdicts,
                    "detail": {}}, ensure_ascii=False),
        project_id=pid, message_id=mid, model=jev.JEV_MODEL,
        meta={**meta_base, "technical_status": "complete"})
    return target_verdicts, flag


def _obtain_summary(ledger, scfg, bundle, llm_fn, guard, pid, mid, fp, policy,
                    facts, verdicts, existing, deadline, *, diagnostics: dict):
    """Stored, candidate, or freshly generated summary for one target, with
    the one reserved format repair.

    Returns (summary, summary_reason, summary_error, sent_error, producer): sent_error
    is the LLMNotSent/RuntimeBudget stop that left no summary, else None;
    any other runtime guard error propagates."""
    sent_error = None
    summary_reason = None
    summary_error = {}
    revision = next(m["revision"] for m in bundle["members"] if m["message_id"] == mid)
    producer = {"providers": [], "complete": False}
    if existing is not None:
        summary = existing["content"]
        producer = _summary_producer(existing, fp, policy, revision, db=ledger.db, bundle=bundle)
        if existing["meta"].get("input_oversize"):
            summary["_input_oversize"] = True
    else:
        candidate = current_artifact(ledger.db, KIND_CANDIDATE, mid, fp, policy)
        if candidate and candidate["meta"].get("publication_mode") == scfg["summary_mode"]:
            summary = candidate["content"]
            producer = _summary_producer(candidate, fp, policy, revision, db=ledger.db, bundle=bundle)
        else:
            format_repair_id = None
            try:
                (summary, summary_reason), producer = _summarize_observed(
                    llm_fn, bundle, mid, facts, verdicts.get(mid, {}),
                    deadline=deadline, return_reason=True,
                    error_out=summary_error)
                if summary is None and summary_reason == "model" \
                        and summary_error.get("code") \
                        and semantic_transport.last_llm_failure() != "length_stop" \
                        and current_artifact(ledger.db, KIND_SUMMARY_FORMAT_REPAIR,
                                     mid, fp, policy) is None:
                    if diagnostics.get("summary_validation_error") is None:
                        diagnostics["summary_validation_error"] = summary_error["code"]
                    guard("summary_format_repair_reservation")
                    format_repair_id = ledger.artifact_add(
                        KIND_SUMMARY_FORMAT_REPAIR,
                        json.dumps({"target_message_id": mid}),
                        project_id=pid, message_id=mid,
                        model=semantic_transport.llm_model(),
                        meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy,
                              "repair_count": 1})
                    (summary, summary_reason), producer = _summarize_observed(
                        llm_fn, bundle, mid, facts, verdicts.get(mid, {}),
                        format_repair=True,
                        deadline=deadline, return_reason=True,
                        error_out=summary_error)
            except runtime.RuntimeGuardError as e:
                if format_repair_id is not None and not runtime.llm_request_was_dispatched(e):
                    with ledger.db:
                        ledger.db.execute(
                            "DELETE FROM artifacts WHERE artifact_id=?",
                            (format_repair_id,))
                if not isinstance(e, (runtime.LLMNotSent, runtime.RuntimeBudget)):
                    raise
                # the local model never got (or ran out of budget
                # for) this target's request: stop here like any
                # incomplete pass, so targets already audited are
                # still committed below; re-raised for _process_job
                sent_error = e
                summary, summary_reason = None, "not_sent"
    return summary, summary_reason, summary_error, sent_error, producer


def _promote_loops(ledger, scfg, pl, pid, bundle, facts_by_target,
                   jev_client, deadline, *, diagnostics: dict):
    """Loop candidates/relation events for the live generation.

    Returns (loops_done, loops_short, resource, retryable, hard): the last
    three are failure flags the caller ORs into its own."""
    loops_short = False
    resource = retryable = hard = False
    rejections = []
    scope = pl.get("retry_command_id")
    try:
        _, loops_done = update_loops(
            ledger, pid, bundle, facts_by_target, jev_client,
            scfg, deadline,
            retry_scope=scope if isinstance(scope, str) else "",
            rejections=rejections)
    except runtime.RuntimeBudgetShort:
        # the Jev cutoff stopped the relation pass: the results below
        # still commit and the job resumes the remaining pairs next
        # tick (last_error is not this stop's cause — no flag)
        loops_done = False
        loops_short = True
    else:
        flag = _jev_fail_flag(getattr(jev_client, "last_error", None)) \
            if not loops_done and jev_client is not None else None
        if rejections:
            if diagnostics.get("loops_rejected") is None:
                diagnostics["loops_rejected"] = rejections[0]["code"]
            if flag is None:
                # Only cached rejections, or a later pair's success
                # reset last_error: the known rejection is the cause —
                # a bounded retry, never a free defer that would loop
                # forever. A later hard/resource failure keeps its own
                # handling below.
                _why(diagnostics, "loops_jev_rejected")
                retryable = True
        if not loops_done and jev_client is not None:
            _why(diagnostics, f"loops_jev_{flag}")
            resource |= flag == "resource"
            retryable |= flag == "retryable"
            hard |= flag == "hard"
    return loops_done, loops_short, resource, retryable, hard


def _process_job_inner(ledger, scfg, job, jev_client, llm_fn, deadline,
                       cfg_path: str | None = None,
                       config_generation: str | None = None,
                       reserve_fn=None, lane: str | None = None, *,
                       jev_transport=None, jev_timing=None,
                       diagnostics: dict, shadow_stats: dict | None = None,
                       jev_skip: str | None = None) -> str:
    """One semantic job -> durable artifacts + job state transition.
    Returns 'done'|'deferred'|'deferred_short'|'retry'|'failed'|'stale'."""
    pid, root = job["project_id"], job["message_id"]
    if jev_skip is not None:
        diagnostics["jev_skip_reason"] = jev_skip
    token = runtime.JobToken.from_row(job)
    deadline = runtime.job_deadline(scfg, deadline)
    # Stages and their result guards keep the job deadline. In the backlog
    # lane every unlocked transport (local model and Jev) must end a commit
    # reserve earlier, so its result can wait out a tick's hold on the run
    # lock and still be written in time.
    transport_deadline = deadline - (_backlog_commit_reserve(scfg)
                                     if lane == "backlog" else 0.0)
    pl = loads_dict(job["payload"])
    if pl is None or not is_current(pl):
        return "failed"
    targets = pl.get("targets", [root])
    if targets is None:
        return "failed"
    try:
        bundle = semantic_store.thread_bundle(ledger.db, pid, root, targets)
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
        fresh = semantic_store.thread_bundle(ledger.db, pid, root, targets or [root])
        return fresh["source_fingerprint"] if fresh else None

    def guard(stage):
        try:
            if jev_skip is not None:
                # Even a skipped call must not bypass a malformed/expired
                # authorization window or an unreadable usage ledger.
                try:
                    runtime._jev_window_check(config_path=cfg_path)
                    used = jev_usage_today(ledger)
                    cap = scfg["daily_request_budget"]
                    if lane == "backlog":
                        cap -= math.ceil(cap * BACKLOG_BUDGET_RESERVE)
                    if ((jev_skip in ("daily_budget", "backlog_reserved") and used < cap)
                            or (jev_skip == "disabled" and scfg["daily_request_budget"] > 0)
                            or (jev_skip == "not_configured" and jev.api_key())
                            or (jev_skip == "circuit_open" and (not runtime.circuit_open(ledger)
                                or runtime._circuit_state(ledger)["failure_class"]
                                   not in runtime._CIRCUIT_FAILURE_CLASSES))):
                        raise ValueError("semantic_skip_reason_stale")
                except (jev.JevError, ValueError, sqlite3.Error):
                    raise runtime.RuntimeOff(stage + ':jev_unverified') from None
            runtime.guard(
                ledger, token, deadline=deadline,
                expected_config_generation=config_generation,
                expected_execution_generation=runtime.execution_generation(scfg),
                expected_mode=scfg["mode"], cfg_path=cfg_path,
                load_cfg=load_config, parse_cfg=runtime_config,
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
                    meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
                          "audit_status": "STALE",
                          "technical_status": "stale"})
            raise

    guard("admission")
    if cohort.hold_unbounded_job(ledger, job):
        return "failed"

    # The adapters below make every actual Jev/local-model boundary pass
    # through the same identity check.  A real Jev client additionally gets
    # a hook before each internal retry attempt and a durable reservation
    # immediately before the POST.
    jev_client = runtime.bind_jev(
        jev_client, guard, reserve_fn,
        cutoff=transport_deadline if lane == "backlog" else None,
        transport=jev_transport, timing=jev_timing, config_path=cfg_path)
    llm_fn = runtime.guarded_llm(
        # a backlog worker owns its slot: one long-output call may use the
        # transport deadline instead of burning 600 s per attempt (2026-10-10)
        llm_fn, guard, transport_deadline,
        timeout_cap=None if lane == "backlog" else semantic_transport.LLM_TIMEOUT,
        # the shared follow-up reserve or two thirds of the per-job budget,
        # whichever is smaller — a call that cannot fit defers the pass
        # instead of overrunning it
        call_reserve=min(runtime.LLM_CALL_RESERVE_S,
                         float(scfg["job_budget_seconds"]) * 2 / 3))
    # provenance on the recorded bundle (spec §12.1): which stored
    # origin event / capture path this evaluation descends from
    origin = pl.get("origin") if isinstance(pl.get("origin"), dict) \
        else {}
    bundle["origin_event_id"] = origin.get("event_id")
    bundle["capture_origin"] = origin.get("source")
    if current_artifact(ledger.db, KIND_BUNDLE, root, fp, policy) is None:
        ledger.artifact_add(
            KIND_BUNDLE, json.dumps(bundle, ensure_ascii=False, separators=(',', ':')),
            project_id=pid, message_id=root, model=jev.JEV_MODEL,
            meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION})
    all_targets = [t for t in (targets or [root])
                   if any(m["message_id"] == t
                          and m['body_state'] == 'full' and m['body_original']
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
    not_sent = None     # a summary/repair could not dispatch (see below)
    retryable_failure = False
    resource_wait = False

    def record_skipped_assessment(mid, previous, reason, partial=None):
        partial = partial or {}
        guard("assessment_skip")
        if previous is None or (previous["meta"].get("technical_status") != "skipped"
                                or previous["meta"].get("jev_skip_reason") != reason
                                or previous["content"].get("verdicts") != partial):
            ledger.artifact_add(
                KIND_ASSESS, json.dumps({"target_message_id": mid, "verdicts": partial}, ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={VERSION_KEY: CURRENT_VERSION, "fingerprint": fp, "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION, "model": jev.JEV_MODEL, "registry": jev.REGISTRY_VERSION,
                      "technical_status": "skipped", "jev_skip_reason": reason})

    def activate_skip(reason):
        nonlocal jev_skip, jev_client
        jev_skip = reason
        jev_client = None
        diagnostics["jev_skip_reason"] = reason
        guard("jev_skip")

    for mid in all_targets:
        semantic_transport.set_inference_target(mid)
        member = members[mid]
        # restart-safe: a complete assessment for THIS fingerprint is
        # reused; retry_wait/error/pending ones are re-attempted
        try:
            prev, previous = semantic_store._primary_assessment_history(ledger.db, pid, mid, fp, policy)
            valid = (prev is None or (semantic_store._valid_primary_assessment(
                prev["content"], prev["meta"], prev["model"], mid, fp, policy, scfg,
                prefiltered=(prev["meta"].get("skipped") == "prefilter_no_signal"
                             and semantic_store._verified_prefilter(ledger, pid, mid)))
                and semantic_store._valid_primary_history(prev["content"], prev["meta"], previous, mid, fp, policy, scfg)))
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            valid = False
        if not valid:
            _why(diagnostics, "assess_scope_invalid")
            hard_fail = incomplete = True
            break
        if prev and prev["meta"].get("technical_status") == "complete":
            verdicts[mid] = prev["content"].get("verdicts", {})
        elif jev_skip is not None:
            partial = prev["content"].get("verdicts", {}) if prev else {}
            try:
                if semantic_store._partial_assessment(partial, scfg) != partial:
                    raise ValueError("semantic_partial_assessment_invalid")
            except (ValueError, TypeError):
                hard_fail = incomplete = True
                break
            record_skipped_assessment(mid, prev, jev_skip, partial)
            verdicts[mid] = partial
        else:
            if time.monotonic() > deadline - 5:
                incomplete = True
                break
            target_verdicts, flag = _assess_target(
                ledger, scfg, bundle, jev_client, pid, mid, fp, policy,
                prev, deadline, diagnostics=diagnostics)
            if target_verdicts is None:
                error = getattr(jev_client, "last_error", None)
                reason = semantic_audit.jev_skip_reason(error)
                if not hasattr(error, "partial_verdicts") or getattr(error, "partial_invalid", False):
                    reason = None
                if reason is None:
                    resource_wait |= flag == "resource"
                    retryable_failure |= flag == "retryable"
                    hard_fail |= flag == "hard"
                    incomplete = True
                    continue
                jev_skip = reason
                diagnostics["jev_skip_reason"] = reason
                partial = error.partial_verdicts
                jev_client = None
                record_skipped_assessment(mid, prev, jev_skip, partial)
                verdicts[mid] = partial
            else:
                verdicts[mid] = target_verdicts
        if scfg["summary_mode"] == "off" and scfg["loop_mode"] == "off":
            continue
        skip_state = {"reason": jev_skip}
        try:
            stage = _fact_stage(ledger, scfg, member, pid, mid, fp, policy,
                                jev_client, llm_fn, deadline, shadow_stats=shadow_stats, jev_skip=jev_skip,
                                activate_skip=activate_skip, skip_state=skip_state)
        except runtime.LLMNotSent as error:
            if error.stage not in semantic_transport.MEMBER_HOLD_STAGES:
                raise
            # The member's own paid unit or provenance held — never a local
            # result and never a job-level stop: other targets still run.
            _why(diagnostics, error.stage)
            incomplete = resource_wait = True
            continue
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
                    doc_hash=(semantic_facts.doc_hash(doc) if doc else None))
            _why(diagnostics, _fact_stop_reason(stage))
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
            ledger, bundle, mid, facts, jev_client, scfg, deadline - 5,
            skip_unavailable=jev_skip is not None)
        if not event_details["complete"] and event_details["failure_reason"] == "technical" and jev_skip is None:
            reason = semantic_audit.jev_skip_reason(getattr(jev_client, "last_error", None))
            if reason is not None:
                jev_skip = reason
                diagnostics["jev_skip_reason"] = reason
                jev_client = None
                guard("medication_detail_skip")
                event_details = evaluate_medication_events(
                    ledger, bundle, mid, facts, None, scfg, deadline - 5, skip_unavailable=True)
        guard("medication_detail_result")
        detail_findings[mid] = [finding for finding in event_details["findings"]
                                if finding.get("code") != "medication_detail_unevaluated"]
        if (not event_details["complete"] and event_details["failure_reason"] != "invalid"
                and not (jev_skip is not None and event_details["failure_reason"] == "skipped")):
            incomplete = _why(
                diagnostics, f"medication_detail_{event_details['failure_reason']}")
            error = getattr(jev_client, "last_error", None)
            if error is not None and jev.failure_class(error) == "failed":
                hard_fail = True
            elif event_details["failure_reason"] != "deadline" and (
                    error is None or jev.failure_class(error) != "resource"):
                retryable_failure = True
            break
        if scfg["loop_mode"] != "off":
            guard("loop_candidates")
            update_loops(ledger, pid, bundle, {mid: facts}, None, scfg, deadline)
        if scfg["summary_mode"] != "off":
            previous = current_artifact(ledger.db, KIND_COVERAGE, mid, fp, policy)
            coverage = previous["content"] if previous else None
            if not coverage or not coverage.get("evaluated"):
                from semantic_audit import evaluate_source_fact_coverage
                if jev_skip is not None:
                    coverage = {"status": "SKIPPED", "evaluated": False, "choice": None,
                                "confidence": None, "failure_reason": jev_skip,
                                "findings": [{"code": "source_fact_coverage_unevaluated"}]}
                else:
                    coverage = evaluate_source_fact_coverage(
                        jev_client, member["body_original"], facts, deadline,
                        target_id=f"m{mid}", match_threshold=scfg["match_threshold"])
                    reason = semantic_audit.jev_skip_reason(getattr(jev_client, "last_error", None))
                    if not coverage["evaluated"] and reason is not None:
                        jev_skip = reason
                        diagnostics["jev_skip_reason"] = reason
                        jev_client = None
                        guard("coverage_skip")
                        coverage = {"status": "SKIPPED", "evaluated": False, "choice": None,
                                    "confidence": None, "failure_reason": reason,
                                    "findings": [{"code": "source_fact_coverage_unevaluated"}]}
                guard("coverage_result")
                # an identical unevaluated re-run is a durable wait, not new
                # information — record the outcome once (FIX-SD1)
                if previous is None or previous["content"] != coverage:
                    ledger.artifact_add(
                        KIND_COVERAGE, json.dumps(coverage, ensure_ascii=False),
                        project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                        meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy,
                              "technical_status": "complete" if coverage["evaluated"] else "pending"})
            coverage_by_target[mid] = ([] if coverage.get("status") == "SKIPPED"
                                       else coverage["findings"])
            if not coverage["evaluated"] and coverage.get("status") != "SKIPPED":
                incomplete = _why(diagnostics, f"coverage_{coverage.get('failure_reason')}")
                error = getattr(jev_client, "last_error", None)
                if error is not None and jev.failure_class(error) == "failed":
                    hard_fail = True
                elif (error is not None and jev.failure_class(error) == "retry"
                      or coverage.get("failure_reason") in ("model", "retry")):
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
    held_empty_targets = set()
    for mid, facts in facts_by_target.items():
        semantic_transport.set_inference_target(mid, "summary")
        if scfg["summary_mode"] == "off":
            continue
        existing = current_artifact(ledger.db, KIND_SUMMARY, mid, fp, policy)
        prev_audit = current_artifact(ledger.db, KIND_AUDIT, mid, fp, policy)
        if existing and existing["meta"].get("publication_mode") != scfg["summary_mode"]:
            existing = None
            prev_audit = None
        prev_status = (prev_audit["meta"].get("audit_status")
                       if prev_audit else None)
        if existing is not None and prev_status == "PASS":
            import semantic_send_gate
            if not semantic_send_gate.passed_summary_current(
                    ledger.db, pid, mid, fp, policy, members[mid]["revision"],
                    scfg["summary_mode"], bundle=bundle, fact_source=fact_source):
                _why(diagnostics, "prior_pass_unverified")
                incomplete = resource_wait = True
                continue
        if existing is not None and prev_status in ("PASS",
                                                    "NEEDS_REVIEW"):
            results[mid] = {"summary": existing["content"],
                            "status": prev_status,
                            # stored findings keep v4.diagnostic's
                            # (status, doc_hash, findings) dedup intact
                            "findings": list(prev_audit["content"].get(
                                "findings") or []),
                            "repaired": False, "fresh": False}
            continue
        if time.monotonic() > deadline - 5:
            incomplete = True
            break
        empty_reason = {}
        recovered, empty_reserved = _empty_repair_candidate(
            ledger.db, pid, mid, fp, policy, members[mid]['revision'], scfg['summary_mode'], facts,
            reason_out=empty_reason)
        if empty_reserved and recovered is None:
            _why(diagnostics, _fixed_stop_reason(empty_reason.get('code'), 'empty_repair_unverified'))
            held_empty_targets.add(mid)
            incomplete = resource_wait = True
            continue
        if recovered is not None:
            summary, producer = recovered['content'], recovered['producer']
            summary_reason, summary_error, sent_error = None, {}, None
        else:
            summary, summary_reason, summary_error, sent_error, producer = _obtain_summary(
                ledger, scfg, bundle, llm_fn, guard, pid, mid, fp, policy,
                facts, verdicts, existing, deadline, diagnostics=diagnostics)
        if sent_error is not None:
            if getattr(sent_error, "stage", None) in semantic_transport.MEMBER_HOLD_STAGES:
                # member-bound hold (own paid unit/provenance): this target
                # waits without a summary or PENDING write — later targets run
                _why(diagnostics, sent_error.stage)
                incomplete = resource_wait = True
                continue
            not_sent = sent_error
        if summary is None:
            _why(diagnostics, f"summary_{summary_reason}")
            if diagnostics.get("summary_validation_error") is None:
                diagnostics["summary_validation_error"] = summary_error.get("code")
            # same dedup as the assess wait-state above: a persistent
            # local-LLM outage defers without stacking identical
            # PENDING rows on every drain
            if summary_reason == "model":
                retryable_failure = True
            prev_a = current_artifact(ledger.db, KIND_AUDIT, mid, fp, policy)
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
                    project_id=pid, message_id=mid, model=semantic_transport.llm_model(),
                    meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy, "schema": SCHEMA_VERSION,
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
        if existing is None and current_artifact(ledger.db, KIND_CANDIDATE, mid, fp, policy) is None:
            guard("candidate_snapshot")
            ledger.artifact_add(
                KIND_CANDIDATE, json.dumps(summary, ensure_ascii=False),
                project_id=pid, message_id=mid, model=semantic_transport.llm_model(),
                meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION, "stage": "pre_audit",
                      "publication_mode": scfg["summary_mode"],
                      "target_revision": members[mid]["revision"],
                      "provider_provenance": _summary_provenance(producer, fp, policy,
                                                                  members[mid]["revision"], summary)})
        # Reserve before dispatch: a crash cannot refund the repair (INV-10).
        repaired = bool(current_artifact(ledger.db, KIND_REPAIR, mid, fp)
                        or (prev_audit
                            and prev_audit["meta"].get("repair_count", 0) >= 1))
        summary["_facts"] = facts   # evidence context for the Jev audit
        audit_client = jev_client
        req0 = audit_client.requests_made if audit_client else 0
        findings = []
        status = "PENDING"
        claim_input_binding = None
        jev_outcome = "unknown"
        empty_unverified = False
        held_error = None
        empty_repair = recovered is not None
        empty_prior = recovered['meta']['prior'] if recovered else None
        empty_repair_id = recovered['meta']['repair_artifact_id'] if recovered else None
        for _attempt in range(2):      # initial + at most one repair
            if summary.get("_input_oversize") is True:
                findings = audit_code(bundle, facts, summary)
                status = "NEEDS_REVIEW"
                break
            if summary.get("claims") == []:
                _why(diagnostics, "audit_empty_unverified")
                if repaired or summary.get("_input_oversize"):
                    held_empty_targets.add(mid)
                    empty_unverified = incomplete = resource_wait = True
                    break
                # A malformed or otherwise unbound prior audit is unknown;
                # do not let a fresh empty candidate replace that evidence.
                prior_row = ledger.db.execute(
                    "SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND message_id=? "
                    "ORDER BY artifact_id DESC LIMIT 1", (KIND_AUDIT, pid, mid)).fetchone()
                if prior_row is not None:
                    try:
                        prior_bound = (prev_audit is not None
                                       and strict_json(prior_row['content']) == prev_audit['content']
                                       and strict_json(prior_row['meta']) == prev_audit['meta'])
                    except (ValueError, TypeError, RecursionError):
                        prior_bound = False
                    if not prior_bound:
                        held_empty_targets.add(mid)
                        empty_unverified = incomplete = resource_wait = True
                        break
                empty_repair = True
                empty_prior = _empty_repair_prior(ledger.db, pid, mid)
                code_f = [{"code": "summary_empty_unverified"}]
                jev_f = []
                findings = code_f
                status = "REPAIR_REQUIRED"
                claim_input_binding = _claim_input_binding(
                    pid, mid, fp, policy, members[mid]["revision"], summary, facts)
            else:
                if not isinstance(summary.get("claims"), list):
                    _why(diagnostics, "audit_claims_invalid")
                    if empty_repair:
                        held_empty_targets.add(mid)
                        empty_unverified = incomplete = resource_wait = True
                    else:
                        hard_fail = incomplete = True
                    break
                code_f = (audit_code(bundle, facts, summary) + coverage_by_target.get(mid, [])
                          + detail_findings.get(mid, []))
                claim_input_binding = _claim_input_binding(pid, mid, fp, policy, members[mid]["revision"], summary, facts)
                before_audit = getattr(jev_client, "requests_made", None)
                if empty_repair and jev_client is not None:
                    _store_empty_repair(ledger, pid, mid, fp, policy, members[mid]['revision'],
                        scfg['summary_mode'], facts, summary, producer, empty_repair_id, empty_prior,
                        guard, audit_state='inflight')
                try:
                    jev_f, evaluated = semantic_audit.audit_claims(
                        jev_client, bundle, summary, deadline,
                        scfg["match_threshold"], scfg["max_questions_per_request"])
                except (runtime.LLMNotSent, runtime.RuntimeBudget) as error:
                    # Jev cutoff/budget: earlier targets commit below. A repair
                    # already applied this pass is kept as a PENDING result, so
                    # the next pass re-audits the repaired text (INV-10: the
                    # one repair is spent, never refunded).
                    not_sent = error
                    incomplete = True
                    if empty_repair and jev_client is not None:
                        partial = summary.get('_claim_audit') or {}
                        _store_empty_repair(ledger, pid, mid, fp, policy, members[mid]['revision'],
                            scfg['summary_mode'], facts, summary, producer, empty_repair_id, empty_prior,
                            guard, partial_answers=partial, audit_state='held' if partial else 'ready')
                    if _attempt and not empty_repair:
                        summary["audit_status"] = "PENDING"
                        if fact_source == 'canonical' and producer.get('repair_evidence'):
                            producer = {**producer, 'repair_render_phase': 'pending'}
                        results[mid] = {
                            "summary": summary, "status": "PENDING",
                            "findings": code_f + [{"code": "support_unevaluated"}],
                            "repaired": True, "fresh": True,
                            "producer": producer, "jev_outcome": "unknown",
                            "claim_input_binding": claim_input_binding,
                            "jev_requests": (jev_client.requests_made - req0)
                            if jev_client else 0}
                    break
                if empty_repair and jev_client is not None:
                    partial = summary.get('_claim_audit') or {} if not evaluated else {}
                    if not evaluated:
                        adverse = [item for item in jev_f if not isinstance(item, dict)
                                   or item.get('code') != 'support_unevaluated']
                        if adverse:
                            partial = {'answers': partial, 'findings': adverse}
                    _store_empty_repair(ledger, pid, mid, fp, policy, members[mid]['revision'],
                        scfg['summary_mode'], facts, summary, producer, empty_repair_id, empty_prior,
                        guard, partial_answers=partial, audit_state='held' if partial else 'ready')
                if not evaluated and jev_skip is None:
                    skip = semantic_audit.jev_skip_reason(getattr(jev_client, "last_error", None))
                    if skip is not None:
                        jev_skip = skip
                        diagnostics["jev_skip_reason"] = skip
                        jev_client = None
                        guard("claim_audit_skip")
                after_audit = getattr(audit_client, "requests_made", None)
                sent = (type(before_audit) is int and type(after_audit) is int
                        and after_audit > before_audit)
                observed = summary.get("_claim_audit", {})
                claim_ids = [claim.get("claim_id") for claim in summary["claims"] if isinstance(claim, dict)]
                valid_ids = (len(claim_ids) == len(summary["claims"])
                             and all(isinstance(claim_id, str) for claim_id in claim_ids))
                if (sent and evaluated and valid_ids and claim_ids and isinstance(observed, dict)
                        and set(observed) == set(claim_ids)):
                    jev_outcome = "done"
                elif sent and isinstance(observed, dict) and observed:
                    jev_outcome = "partial"
                elif jev_skip is not None and prev_audit is None and not sent and not observed:
                    jev_outcome = "skipped"
                else:
                    jev_outcome = "unknown"
                if not evaluated and jev_skip is None:
                    reason = summary.pop("_claim_audit_reason", None)
                    if reason in ("claims_empty", "claims_invalid", "client_unavailable", "options_invalid", "response_invalid"):
                        _why(diagnostics, f"audit_{reason}")
                        # A saved empty/invalid claim list is reused unchanged on each retry.
                        hard_fail |= reason in ("claims_empty", "claims_invalid")
                        resource_wait |= reason == "client_unavailable"
                        retryable_failure |= reason == "response_invalid"
                    else:
                        flag = _jev_fail_flag(getattr(jev_client, "last_error", None))
                        _why(diagnostics, f"audit_jev_{flag}" if flag else "audit_unevaluated")
                        resource_wait |= flag == "resource"
                        retryable_failure |= flag == "retryable"
                        hard_fail |= flag == "hard"
                prior_blocked = False
                if not evaluated and jev_skip is not None:
                    known, unbound = _prior_pending_findings(ledger.db, pid, mid, fp, policy,
                                                             members[mid]["revision"], scfg["summary_mode"], repaired, prev_audit,
                                                             summary, claim_input_binding)
                    prior_blocked = bool(known) or unbound or _attempt > 0
                    if known:
                        jev_f.extend(f for f in known if f not in jev_f)
                    if unbound or _attempt > 0:
                        jev_f.append({"code": "prior_audit_unverified"})
                    if any(f.get("code") != "support_unevaluated" for f in jev_f):
                        prior_blocked = True
                findings = code_f + jev_f
                status = (semantic_audit.optional_audit_status(code_f, jev_f, jev_skip, repaired)
                          if not evaluated and jev_skip is not None
                          else audit_status_for(code_f, jev_f, evaluated, repaired))
                if prior_blocked:
                    # A skipped re-audit cannot clear an earlier negative result
                    # or certify a repair that has not itself been re-evaluated.
                    status = "PENDING"
                    resource_wait = True
            if status == "REPAIR_REQUIRED" and not repaired:
                guard("repair_reservation")
                # Preserve the rejected original and its exact audit before
                # dispatch: a crash or empty repair must not erase a finding.
                repair_pending = {"summary": {**summary, "audit_status": "PENDING"},
                                  "status": "PENDING", "findings": findings, "repaired": True,
                                  "producer": producer, "jev_outcome": jev_outcome,
                                  "claim_input_binding": claim_input_binding,
                                  "jev_requests": (audit_client.requests_made - req0) if audit_client else 0}
                with ledger.db:
                    if not ledger.db.in_transaction:
                        ledger.db.execute('BEGIN IMMEDIATE')
                    guard("repair_reservation_tx")
                    if not empty_repair:
                        _write_result(ledger, pid, mid, repair_pending, fp, members,
                                      "PENDING", policy, scfg["summary_mode"])
                    repair_id = ledger.artifact_add_tx(
                        KIND_REPAIR, json.dumps({"target_message_id": mid}),
                        project_id=pid, message_id=mid, model=semantic_transport.llm_model(),
                        meta={VERSION_KEY: CURRENT_VERSION, "inference_requests": semantic_transport.inference_requests(), "fingerprint": fp, "policy_fingerprint": policy, "repair_count": 1,
                              **({'empty_repair': True} if empty_repair else {})})
                repaired = True
                if empty_repair:
                    empty_repair_id = repair_id
                repair_answers = []

                def summary_repair_llm(prompt):
                    previous_requests = set(semantic_transport.inference_requests())
                    value = llm_fn(prompt)
                    receipt = semantic_transport.inference_receipt()
                    contract = semantic_transport.inference_contract()
                    if (not empty_repair and isinstance(value, str) and receipt is not None and contract is not None
                            and receipt.get('reservation_id') not in previous_requests):
                        import bounded_http
                        try:
                            if len(value.encode('utf-8')) <= bounded_http.MAX_RESPONSE_BYTES:
                                repair_answers.append({'text': value,
                                    'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                                    'reservation_id': receipt.get('reservation_id'), 'contract': contract,
                                    'unit_key': receipt.get('unit_key'), 'owner': receipt.get('owner')})
                        except UnicodeError:
                            pass
                    return value

                try:
                    with (semantic_transport.quality_repair() if not empty_repair else nullcontext()):
                        summary2, repair_producer = _summarize_observed(
                            summary_repair_llm, bundle, mid, facts, verdicts.get(mid, {}),
                            feedback=[f.get("code", "") + " " +
                                      f.get("statement", f.get("claim", ""))
                                      for f in code_f + jev_f])
                except runtime.RuntimeGuardError as error:
                    if not runtime.llm_request_was_dispatched(error):
                        # A proven pre-dispatch stop did not spend the one
                        # repair. A crash or post-dispatch stop still does.
                        with ledger.db:
                            ledger.db.execute(
                                "DELETE FROM artifacts WHERE artifact_id=?",
                                (repair_id,))
                            if not empty_repair:
                                repair_pending["repaired"] = False
                                _write_result(ledger, pid, mid, repair_pending, fp, members,
                                              "PENDING", policy, scfg["summary_mode"])
                    if isinstance(error, (runtime.LLMNotSent,
                                          runtime.RuntimeBudget)):
                        if getattr(error, "stage", None) in semantic_transport.MEMBER_HOLD_STAGES:
                            held_error = error
                        else:
                            not_sent = error
                        incomplete = True
                        break
                    raise
                if summary2 is not None and summary2.get("_input_oversize") is not True:
                    if not empty_repair and repair_producer.get('providers') in (['openai'], ['anthropic']):
                        fact_kind = KIND_FACTS_V2 if fact_source == 'canonical' else KIND_FACTS
                        refs = {}
                        for key, kind in (('facts_id', fact_kind), ('assessment_id', KIND_ASSESS),
                                          ('original_summary_id', KIND_SUMMARY), ('audit_id', KIND_AUDIT)):
                            row = ledger.db.execute("SELECT artifact_id FROM artifacts WHERE kind=? AND project_id=? "
                                "AND message_id=? AND artifact_id<? AND CASE WHEN json_valid(meta) THEN "
                                "json_extract(meta,'$.fingerprint')=? AND json_extract(meta,'$.policy_fingerprint')=? "
                                "AND (?!=? OR json_extract(meta,'$.fact_id') IS NULL) ELSE 0 END "
                                "ORDER BY artifact_id DESC LIMIT 1",
                                (kind, pid, mid, repair_id, fp, policy, kind, KIND_ASSESS)).fetchone()
                            if row is not None:
                                refs[key] = row['artifact_id']
                        if len(repair_answers) == 1 and len(refs) == 4:
                            repair_evidence = {'version': 1, 'project_id': pid, 'message_id': mid,
                                'fact_kind': fact_kind, **refs, 'repair_id': repair_id, 'answer': repair_answers[0]}
                            if _summary_repair_evidence(ledger.db, bundle, summary2, repair_evidence,
                                                        fp, policy, members[mid]['revision'], render_phase='pre_render'):
                                guard('summary_repair_evidence')
                                proof_id = ledger.artifact_add(
                                    KIND_REPAIR, json.dumps({'target_message_id': mid,
                                        '__mcs_internal_proof_v1': repair_evidence}, ensure_ascii=False),
                                    project_id=pid, message_id=mid, model=semantic_transport.llm_model(),
                                    meta={VERSION_KEY: CURRENT_VERSION, 'fingerprint': fp,
                                          'policy_fingerprint': policy, 'repair_count': 1})
                                repair_producer['repair_evidence'] = {
                                    'artifact_id': proof_id, 'hash': mcs_util.payload_hash(repair_evidence)}
                            else:
                                repair_producer = {'providers': [], 'complete': False}
                        else:
                            repair_producer = {'providers': [], 'complete': False}
                    summary = summary2
                    producer = repair_producer
                    if empty_repair and isinstance(summary.get('claims'), list) and summary['claims']:
                        _store_empty_repair(ledger, pid, mid, fp, policy, members[mid]['revision'],
                            scfg['summary_mode'], facts, summary, producer, repair_id, empty_prior, guard)
                    summary["_facts"] = facts
                    continue
                if empty_repair:
                    held_empty_targets.add(mid)
                    empty_unverified = incomplete = resource_wait = True
                    break
                status = "NEEDS_REVIEW"
                findings.append({"code": "repair_unavailable"})
            break
        if not_sent is not None:
            break
        if held_error is not None:
            # member-bound hold in the reserved repair path: this target
            # waits without consuming audit state — later targets still run
            _why(diagnostics, held_error.stage)
            incomplete = resource_wait = True
            continue
        if empty_repair and status == "PENDING":
            held_empty_targets.add(mid)
            empty_unverified = incomplete = resource_wait = True
        if empty_unverified:
            continue
        v2_doc = v2_docs_by_target.get(mid)
        if fact_source == "canonical" and v2_doc is not None:
            # T11: the adjudicated contract drives a mandatory render
            # layer — verified facts stay visible and non-terminal
            # obligations are disclosed as limitations even if the
            # model summary dropped them. Per-target doc: the LAST
            # member's doc must never leak into another target's
            # summary (C02).
            status = semantic_render.apply_mandatory_render(summary, findings, status,
                                             v2_doc)
            if producer.get('repair_evidence'):
                producer = {**producer, 'repair_render_phase': 'rendered'}
        summary["audit_status"] = status
        if fact_source == "canonical":
            v4.record_stage(ledger, pid, mid, fp, policy,
                            "s7_summary_audit", status)
        results[mid] = {"summary": summary, "status": status,
                        "findings": findings, "repaired": repaired,
                        "fresh": True,
                        "producer": producer, "jev_outcome": jev_outcome,
                        "claim_input_binding": claim_input_binding,
                        "jev_skip_reason": jev_skip if status == "SKIPPED" else None,
                        "jev_requests": (audit_client.requests_made - req0)
                        if audit_client else 0}
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
                if not ledger.db.in_transaction:
                    ledger.db.execute('BEGIN IMMEDIATE')
                guard('partial_publish_tx')
                only_empty_held = (bool(held_empty_targets)
                                   and set(results) | held_empty_targets == set(facts_by_target)
                                   and all(r["status"] in ("PASS", "SKIPPED") for r in results.values())
                                   and not hard_fail and not retryable_failure and not_sent is None)
                for mid, r in results.items():
                    if r["fresh"]:
                        _write_result(ledger, pid, mid, r, fp, members,
                                      r["status"], policy, scfg["summary_mode"])
                    v4.publish_target_tx(ledger, pid, mid, fp, policy, members,
                                v2_docs_by_target, r["status"],
                                r.get("findings") or [],
                                fact_source)
                    reusable = (not r["fresh"] and r["status"] == "PASS"
                                and _reused_pass_current(ledger.db, pid, mid, fp, policy,
                                                         members[mid]["revision"], scfg["summary_mode"],
                                                         r, facts_by_target[mid]))
                    if (only_empty_held and fact_source != "canonical" and (r["fresh"] or reusable)):
                        import semantic_intake_hold
                        semantic_notice.record_notice_plan_tx(
                            ledger, pid, root, mid, fp, policy, r["summary"], r["status"],
                            members[mid], all_targets, mode=scfg["mode"],
                            summary_mode=scfg["summary_mode"],
                            notification_free=(pl.get("notification_free") or not
                                semantic_intake_hold.notification_allowed(ledger, pl, pid, mid)),
                            origin=pl.get("origin"), content_quality=bundle["content_quality"])
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
    loops_short = False
    if not stale and scfg["loop_mode"] != "off" and jev_skip is not None:
        guard("loop_skip")
        prior = current_artifact(ledger.db, "semantic_loop_skipped", root, fp, policy)
        if prior is None or prior["content"].get("reason") != jev_skip:
            ledger.artifact_add(
                "semantic_loop_skipped", json.dumps({"status": "SKIPPED", "reason": jev_skip}),
                project_id=pid, message_id=root,
                meta={VERSION_KEY: CURRENT_VERSION, "fingerprint": fp, "policy_fingerprint": policy,
                      "schema": SCHEMA_VERSION})
    if not stale and scfg["loop_mode"] != "off" and jev_skip is None:
        # loop candidates/relation events for the LIVE generation —
        # kept OUT of the commit tx because matching makes Jev calls
        # (no network inside a DB transaction, §6.2); replay-safe via
        # candidate_fp and pair dedup.
        loops_done, loops_short, resource, retryable, hard = _promote_loops(
            ledger, scfg, pl, pid, bundle, facts_by_target, jev_client,
            deadline, diagnostics=diagnostics)
        resource_wait |= resource
        retryable_failure |= retryable
        hard_fail |= hard
    # notification eligibility is derived from the stored origin event,
    # not from this job's seed provenance (INV-20, §20.3) — evaluated
    # per target below: a covering new_messages intent must exist for
    # the message a notice reports on.
    with ledger.db:
        if not ledger.db.in_transaction:
            ledger.db.execute('BEGIN IMMEDIATE')
        if not stale:
            guard('publish_tx')
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
            v4.publish_target_tx(ledger, pid, mid, fp, policy, members,
                        v2_docs_by_target, final_status,
                        r.get("findings") or [], fact_source)
            if stale:
                continue
            import semantic_intake_hold
            semantic_notice.record_notice_plan_tx(
                ledger, pid, root, mid, fp, policy, r["summary"], final_status,
                members[mid], all_targets, mode=scfg["mode"],
                summary_mode=scfg["summary_mode"],
                notification_free=(pl.get("notification_free") or not
                    semantic_intake_hold.notification_allowed(ledger, pl, pid, mid)),
                origin=pl.get("origin"), content_quality=bundle["content_quality"])
        if not stale and not pending and loops_done:
            if (scfg['summary_mode'] == 'off' and scfg['loop_mode'] != 'off'
                    and not hard_fail and not retryable_failure and not resource_wait
                    and isinstance(targets, list) and targets and len(targets) == len(set(targets))
                    and set(targets) == set(facts_by_target) and not any(detail_findings.values())
                    and (jev_skip is not None or jev_client is not None)):
                try:
                    manifest = semantic_store._loop_completion_manifest(ledger.db, pid, bundle, targets, scfg,
                        facts_by_target=facts_by_target, skipped=jev_skip is not None, deadline=deadline)
                except sqlite3.Error as error:
                    raise runtime.RuntimeBudgetShort('loop_completion_read') from error
                except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
                    manifest = None
                skip_ref = (semantic_store._loop_skip_ref(ledger.db, pid, root, fp, policy, jev_skip)
                            if jev_skip is not None else None)
                if jev_skip is not None and skip_ref is None:
                    manifest = None
                guard('loop_completion')
                if manifest is not None:
                    status = 'SKIPPED' if jev_skip is not None else 'DONE'
                    content = {'status': status, 'target_ids': sorted(targets),
                               'loop_mode': scfg['loop_mode'], 'semantic_mode': scfg['mode'], 'manifest': manifest,
                               'skip_reason': jev_skip if status == 'SKIPPED' else None, 'skip_ref': skip_ref}
                    for mid in targets:
                        ledger.artifact_add_tx(KIND_LOOP_COMPLETION, json.dumps(content, ensure_ascii=False),
                            project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                            meta={VERSION_KEY: CURRENT_VERSION, 'fingerprint': fp,
                                  'policy_fingerprint': policy, 'schema': SCHEMA_VERSION,
                                  'target_revision': members[mid]['revision'],
                                  'execution_generation': runtime.execution_generation(scfg),
                                  'loop_status': status, 'skip_reason': content['skip_reason'],
                                  'content_hash': mcs_util.payload_hash(content)})
            if not runtime.transition_tx(ledger, token, "done"):
                raise runtime.RuntimeStale("promote")
    if stale:
        return "stale"
    if hard_fail:
        return "failed"
    if loops_short:
        # same mapping as a cutoff raised anywhere else in the pass
        _why(diagnostics, "budget_short")
        return "deferred_short"
    if pending:
        # evaluation incomplete (e.g. Jev outage mid-audit) — the job
        # must not sit 'done' on an unfinished audit; consume a bounded
        # retry attempt so a persistent outage eventually fails rather
        # than busy-loops (AT-058)
        _why(diagnostics, "audit_pending")
        return "deferred" if resource_wait and not retryable_failure \
            else "retry"
    if not loops_done:
        # relation pass truncated by the pair budget/deadline — the
        # results committed above stand; the job re-runs to evaluate
        # the remaining (candidate, target) pairs (§17.2 持ち越し)
        _why(diagnostics, "loops_incomplete")
        return "retry" if retryable_failure else "deferred"
    return "done"


NOT_SENT_BACKOFF_S = 3600


_LLM_DOWN_REASONS = frozenset({"llm_unreachable", "llm_unavailable"})


def _process_job(ledger, scfg, job, jev_client, llm_fn, deadline,
                 cfg_path: str | None = None,
                 config_generation: str | None = None,
                 reserve_fn=None, lane: str | None = None, *,
                       jev_transport=None, jev_timing=None, cloud_transport=None, llm_timing=None,
                       cloud_only=False,
                 diagnostics: dict, shadow_stats: dict | None = None,
                 jev_skip: str | None = None) -> str:
    """Preserve stale workers while bounding timeouts after dispatched work."""
    requests_before = jev_client.requests_made if jev_client is not None else 0
    llm_started = False
    local_llm_fn = llm_fn

    @wraps(llm_fn)
    def tracked_llm(*args, **kwargs):
        nonlocal llm_started
        llm_started = True
        return llm_fn(*args, **kwargs)

    def jev_sent():
        return (jev_client is not None
                and jev_client.requests_made > requests_before)

    try:
        with ExitStack() as stack:
            stack.enter_context(semantic_transport.inference_targets(runtime.execution_generation(scfg)))
            if cloud_transport is not None and lane == "backlog":
                token = runtime.JobToken.from_row(job)
                payload = runtime.parse_payload(job)
                targets = payload.get("targets", [job["message_id"]])

                def source():
                    bundle = semantic_store.thread_bundle(
                        ledger.db, job["project_id"], job["message_id"], targets)
                    return bundle["source_fingerprint"] if bundle else None

                fingerprint = source()
                def fallback_current(mid):
                    # Never join a member's local result to a previously
                    # persisted cloud prefix, or to a chunk whose producer
                    # cannot be proven — decided per actual target, so one
                    # member's cloud chunks never forbid another's local.
                    import semantic_extraction
                    rows = ledger.db.execute("SELECT * FROM artifacts WHERE project_id=? AND kind IN (?,?) "
                        "AND message_id IS ? AND (NOT json_valid(meta) OR json_extract(meta,'$.source_fingerprint')=?)",
                        (job['project_id'], semantic_extraction.KIND_CHUNK,
                         semantic_extraction.KIND_CHUNK_V2, mid, fingerprint)).fetchall()
                    return all(semantic_extraction._cached_provider(
                        ledger.db, row, semantic_transport.llm_model()) == 'local' for row in rows)

                def current():
                    try:
                        runtime.guard(ledger, token, deadline=deadline,
                            expected_config_generation=config_generation,
                            expected_execution_generation=runtime.execution_generation(scfg),
                            expected_mode=scfg["mode"], cfg_path=cfg_path,
                            load_cfg=load_config, parse_cfg=runtime_config,
                            source_fingerprint=fingerprint, current_source=source, stage="cloud")
                    except runtime.RuntimeGuardError:
                        return False
                    return True

                cloud = stack.enter_context(semantic_transport.backlog_inference(
                    ledger, load_config() if cfg_path is None else load_config(cfg_path),
                    job, runtime.job_deadline(scfg, deadline), current, cloud_transport,
                    local_fallback=local_llm_fn if not cloud_only else None,
                    fallback_current=fallback_current if not cloud_only else None))
                if cloud_only and cloud is None:
                    raise runtime.LLMNotSent("cloud_scope_or_budget")
                if cloud is not None:
                    def cloud_call(prompt, timeout=None):
                        started = time.perf_counter()
                        before = llm_timing["s"] if llm_timing is not None else 0.0
                        try:
                            return cloud(prompt, timeout=timeout)
                        finally:
                            if llm_timing is not None:
                                elapsed = time.perf_counter() - started
                                # A fallback can already have charged this same accumulator.
                                llm_timing["s"] += max(0.0, elapsed - (llm_timing["s"] - before))
                    llm_fn = cloud_call
            status = _process_job_inner(
                ledger, scfg, job, jev_client, tracked_llm, deadline,
                cfg_path=cfg_path, config_generation=config_generation,
                reserve_fn=reserve_fn, lane=lane, diagnostics=diagnostics, shadow_stats=shadow_stats,
                jev_transport=jev_transport, jev_timing=jev_timing, jev_skip=jev_skip)
        from extraction_refresh import refresh_extraction_cards
        if semantic_store.refresh_cards_allowed(ledger.db, job['project_id'], job['message_id']):
            refresh_extraction_cards(ledger, job["project_id"], job["message_id"])
        return status
    except runtime.RuntimeOff as error:
        _why(diagnostics, 'permission_refused' if error.stage.endswith(':permission') else 'off_or_scope')
        return "stale"
    except runtime.RuntimeStale as error:
        _why(diagnostics, 'source_changed' if error.stage.endswith(':source') else 'generation_changed')
        return "stale"
    except runtime.LLMNotSent as e:
        diagnostics["not_sent"] = getattr(e, "stage", None)
        if diagnostics["not_sent"] == "llm_turn_busy":
            # the other lane held the GPU turn: the endpoint is healthy
            # and the next pass normally gets it, so no hourly backoff
            return "deferred"
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
        _why(diagnostics, "budget_short")
        return "deferred_short"
    except runtime.RuntimeBudget as e:
        _why(diagnostics, f"budget_{e.stage}")
        return "retry" if jev_sent() or llm_started else "deferred"


def _jev_error_brief(error) -> dict | None:
    """Kind/class of a JevError for run-record diagnostics (no bodies)."""
    if error is None:
        return None
    return {"kind": str(getattr(error, "kind", "") or "")[:40],
            # JevError.detail is a code (never a response body) — the
            # protocol_error variant is only diagnosable from it
            "detail": str(getattr(error, "detail", "") or "")[:60],
            "status": getattr(error, "status", 0) or 0,
            "class": jev.failure_class(error)}


_STAGE_KINDS = (KIND_FACT_PROJ, v4.KIND_V4_STAGE, KIND_LOOP,
                KIND_LOOP_EVENT, KIND_PLAN)


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
        (project_id, *_STAGE_KINDS, KIND_USAGE, KIND_DRAIN_RUN,
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
