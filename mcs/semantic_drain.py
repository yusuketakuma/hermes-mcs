"""Semantic drain engine (spec §14, §18): the durable-job worker —
claiming jobs, staged extraction/summary/audit, Jev budget + circuit
wiring, plan recording, and notice emission. Functions reach facade
patch points (semantic.llm_chat / audit_claims / _process_job) through
the module object so monkeypatching keeps working."""
from __future__ import annotations

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
                             KIND_BUNDLE, KIND_FACTS, KIND_PLAN,
                             KIND_SUMMARY, KIND_USAGE, POLICY_VERSION,
                             QC_ARTIFACT, QC_JOB_KIND, SCHEMA_VERSION,
                             policy_fingerprint, semantic_config)
from semantic_render import (_emit_degraded, _notify_src_event,
                             _outbox_has_delivery, render_notice)
from semantic_store import _current, jev_state, jev_usage_today

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
    ledger.artifact_add_tx(
        KIND_SUMMARY,
        json.dumps({k: v for k, v in r["summary"].items()
                    if not k.startswith("_")}, ensure_ascii=False),
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
        # facts: reuse a stored set for this fingerprint, else extract
        prev_f = _current(ledger, KIND_FACTS, mid, fp)
        if prev_f is not None:
            facts = prev_f["content"].get("facts", [])
        else:
            if time.monotonic() > deadline - 5:
                incomplete = True
                break
            facts, f_complete, f_dropped, f_reason = extract_facts(
                llm_fn, member, deadline - 5, return_reason=True,
                ledger=ledger, source_fingerprint=fp, project_id=pid)
            if not f_complete:
                if f_reason == "model":
                    retryable_failure = True
                incomplete = True
                break
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
                from semantic_extraction import evaluate_source_fact_coverage
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


# ---------- extract_qc: Jev quality control over extract_llm artifacts ----------
# Annotate-only audit of the fully-local extraction. The drain's existing
# guards (OFF mode, circuit, paused, project_ids, daily budget, usage
# reservation, mid-run config recheck) apply verbatim — the job never
# mutates the extraction artifact and never suppresses an item.

QC_MAX_ITEMS = 16


def _qc_seed(ledger, now: float, limit: int = 32) -> int:
    """Queue extract_qc jobs for current extract_llm artifacts lacking a
    QC artifact for the same content hash. Re-extraction (hash change)
    re-pends the row; a pending/failed row for the SAME hash is left
    alone (failed inputs stay failed until an explicit retry).
    json_valid guards keep poisoned meta rows from aborting the scan."""
    import extract_llm
    with ledger.db:
        cur = ledger.db.execute("""
          INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,
            payload,state,next_try,created_at,updated_at)
          SELECT ?, m.project_id, a.message_id, NULL,
            json_object('hash', json_extract(a.meta,'$.hash')),
            'pending', ?, ?, ?
          FROM artifacts a JOIN messages m ON m.message_id=a.message_id
          WHERE a.kind='extract_llm' AND json_valid(a.meta)
            AND json_extract(a.meta,'$.hash')=m.content_hash
            AND json_extract(a.meta,'$.extract_version')=?
            AND json_extract(a.meta,'$.error') IS NULL
            AND NOT EXISTS(SELECT 1 FROM artifacts q
                           WHERE q.kind=? AND q.message_id=a.message_id
                             AND json_valid(q.meta)
                             AND json_extract(q.meta,'$.hash')
                                 =json_extract(a.meta,'$.hash'))
          ORDER BY a.artifact_id DESC LIMIT ?
          ON CONFLICT(kind,project_id,message_id) DO UPDATE SET
            payload=excluded.payload,state='pending',attempts=0,
            next_try=excluded.next_try,updated_at=excluded.updated_at
          WHERE fetch_jobs.state IN ('done','failed')
            AND coalesce(json_extract(fetch_jobs.payload,'$.hash'),'')
                !=json_extract(excluded.payload,'$.hash')
        """, (QC_JOB_KIND, now, now, now,
              extract_llm.EXTRACT_VERSION, QC_ARTIFACT, limit))
    return cur.rowcount


def _qc_questions(ex: dict) -> tuple[dict, list, dict]:
    """Per-item support questions (noul) plus classification audits
    (choice). Returns (questions, layout, context_items): layout maps
    qid -> (section, index); context_items carries the extracted items
    as quoted DATA for state.context — never in the instruction channel
    (an evidence span is a verbatim quote of the message body, i.e.
    attacker-controlled text)."""
    questions, layout, ctx_items = {}, [], {}
    n = QC_MAX_ITEMS
    for section in ("meds", "symptoms", "events"):
        for i, item in enumerate((ex.get(section) or [])[:n]):
            label = (item if section == "events"
                     else json.dumps(item, ensure_ascii=False))
            qid = f"{section[0]}{i}"
            ctx_items[qid] = str(label)[:400]
            questions[qid] = jev.noul_question(
                f"state.context の id={qid} の抽出項目は、"
                "対象の投稿本文に裏付けられているか",
                "本文にこの項目を裏付ける記述がある",
                "本文にこの項目を裏付ける記述がない")
            layout.append((qid, section, i))
            n -= 1
            if n <= 0:
                break
        if n <= 0:
            break
    urg = ex.get("urgency")
    if urg in ("high", "routine"):
        questions["urg"] = jev.choice_question(
            "対象の投稿本文の緊急度として最も妥当なものを選ぶ"
            f"（抽出側の分類: {urg}）",
            {"high": "早急な対応が必要な内容",
             "routine": "通常対応で足りる内容",
             "unclear": "本文だけでは判断できない"})
        layout.append(("urg", "urgency", -1))
    return questions, layout, ctx_items


def _process_qc_job(ledger, scfg: dict, job, jev_client,
                    deadline: float, reserve_fn=None,
                    cfg_path: str | None = None,
                    config_generation: str | None = None) -> str:
    """One extract_qc job -> extract_qc artifact (annotate only).
    The job row transitions to 'done' inside the artifact commit tx —
    a claimed row must never be left re-claimable after its result
    landed. Returns 'done'|'deferred'|'retry'|'stale'."""
    import extract_llm
    if jev_client is None or scfg.get("extract_qc") != "annotate":
        return "deferred"
    token = runtime.JobToken.from_row(job)
    deadline = runtime.job_deadline(scfg, deadline)
    pid, mid = job["project_id"], job["message_id"]

    def done():
        return "done" if runtime.transition(ledger, token, "done") \
            else "stale"

    msg = ledger.db.execute(
        "SELECT content_hash,body_text FROM messages WHERE message_id=?",
        (mid,)).fetchone()
    if msg is None:
        return done()
    art = None
    for r in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind='extract_llm'"
            " AND message_id=? ORDER BY artifact_id DESC", (mid,)):
        try:
            m = json.loads(r["meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if m.get("hash") == msg["content_hash"] \
                and m.get("extract_version") == extract_llm.EXTRACT_VERSION \
                and not m.get("error"):
            art = (r["content"], m["hash"])
            break
    if art is None:
        return done()
    try:
        ex = json.loads(art[0] or "{}")
    except (json.JSONDecodeError, TypeError):
        return done()
    if not isinstance(ex, dict):
        return done()
    questions, layout, ctx_items = _qc_questions(ex)
    state = {"target": {"id": f"m{mid}", "role": "target",
                        "text": msg["body_text"]},
             "context": [{"id": qid, "role": "extracted_item",
                          "text": text}
                         for qid, text in ctx_items.items()]}

    def guard(stage):
        runtime.guard(
            ledger, token, deadline=deadline,
            expected_config_generation=config_generation,
            expected_mode=scfg["mode"], cfg_path=cfg_path,
            load_cfg=load_config, parse_cfg=semantic_config,
            stage=stage)

    req0 = jev_client.requests_made
    client, _cleanup = runtime.bind_jev(jev_client, guard, reserve_fn)
    try:
        out = (_eval_chunked(client, state, questions, deadline,
                             scfg["max_questions_per_request"])
               if questions else {"answers": {}})
    except jev.JevError as error:
        try:
            jev_client.last_error = error
        except Exception:
            pass
        cls = _jev_failure_class(error)
        if cls == "resource":
            return "deferred"
        if cls == "retry":
            return "retry"
        # permanent failure: record why QC could not run instead of
        # retrying forever — the extraction artifact itself is untouched
        with ledger.db:
            ledger.db.execute(
                "DELETE FROM artifacts WHERE kind=? AND message_id=?"
                " AND json_valid(meta)"
                " AND json_extract(meta,'$.hash') != ?",
                (QC_ARTIFACT, mid, art[1]))
            ledger.artifact_add_tx(
                QC_ARTIFACT, json.dumps({"qc": "unevaluated",
                                         "reason": error.kind},
                                        ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={"hash": art[1],
                      "extract_version": extract_llm.EXTRACT_VERSION,
                      "qc": "unevaluated"})
            if not runtime.transition_tx(ledger, token, "done"):
                raise runtime.RuntimeStale("qc:write")
        return "done"
    except runtime.RuntimeStale:
        return "stale"
    except (runtime.RuntimeOff, runtime.RuntimeBudget):
        return "deferred"
    answers = out["answers"]
    items, urgency = [], None
    for qid, section, i in layout:
        ans = answers.get(qid)
        if ans is None:
            continue
        if section == "urgency":
            urgency = {"extracted": ex.get("urgency"),
                       "jev": ans.get("choice"),
                       "confidence": ans.get("confidence")}
        else:
            items.append({"section": section, "index": i,
                          "item": (ex.get(section) or [])[i],
                          # NO_MATCH means 'not supported by this
                          # text', never 'the fact does not exist'
                          "verdict": jev.verdict_for(
                              ans.get("noul", 0.0),
                              scfg["match_threshold"],
                              scfg["nomatch_threshold"]),
                          "noul": ans.get("noul")})
    content = {"qc": "done", "items": items}
    if urgency is not None:
        content["urgency"] = urgency
    try:
        with ledger.db:
            # stale QC rows for a superseded hash are replaced in the
            # same tx; the job transition lands here too so the row is
            # never left claimable after its result
            ledger.db.execute(
                "DELETE FROM artifacts WHERE kind=? AND message_id=?"
                " AND json_valid(meta)"
                " AND json_extract(meta,'$.hash') != ?",
                (QC_ARTIFACT, mid, art[1]))
            ledger.artifact_add_tx(
                QC_ARTIFACT, json.dumps(content, ensure_ascii=False),
                project_id=pid, message_id=mid, model=jev.JEV_MODEL,
                meta={"hash": art[1],
                      "extract_version": extract_llm.EXTRACT_VERSION,
                      "qc": "done",
                      "jev_requests": jev_client.requests_made - req0})
            if not runtime.transition_tx(ledger, token, "done"):
                raise runtime.RuntimeStale("qc:write")
    except runtime.RuntimeStale:
        return "stale"
    return "done"


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
                    THEN 0 ELSE 1 END, job_id
      LIMIT ?
    """.format(",".join("?" * len(kinds))),
             (time.time(), *kinds, max_jobs)).fetchall()
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
            "generation": token.generation,
            "status": status if status is not None else "error",
            "elapsed_s": job_elapsed, "job_age_s": job_age,
            "jev_requests": used, "usage": usage,
        })
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
