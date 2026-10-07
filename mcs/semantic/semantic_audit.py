"""Semantic audit gates (spec §16): code-level checks, per-claim Jev
support evaluation, and the audit-status decision tree.

Leaf module — deliberately independent of the semantic facade so the
audit logic can be exercised without the drain machinery."""
from __future__ import annotations

import json
from contextlib import suppress

import semantic_jev as jev
from semantic_quantities import claim_quantity_findings


def _probability(value) -> bool:
    return type(value) in (int, float) and 0 <= value <= 1


def audit_code(bundle: dict, facts: list, summary: dict) -> list:
    """Deterministic checks: reference integrity, span equality,
    structural enums, fact->claim coverage. Returns findings list —
    [] means the code side found no blocker (model check still runs)."""
    findings = []
    if bundle.get("content_quality") != "full":
        findings.append({"code": "input_incomplete"})
    if summary.get("_input_oversize"):
        findings.append({"code": "input_oversize"})
    members = {m["message_id"]: m for m in bundle["members"]}
    for f in facts:
        ev = f.get("_evidence")
        if f["evidence_refs"] and not ev:
            findings.append({"code": "evidence_missing",
                             "fact": f["fact_id"]})
            continue
        if not ev:
            continue
        src = members.get(ev["message_id"])
        if src is None or ev["revision_id"] != src["revision"]:
            findings.append({"code": "evidence_revision_mismatch",
                             "fact": f["fact_id"]})
            continue
        s, e = ev["start_codepoint"], ev["end_codepoint"]
        if type(s) is not int or type(e) is not int or s < 0 or s >= e \
                or e > len(src["body_original"]) \
                or src["body_original"][s:e] != ev["quote"]:
            findings.append({"code": "evidence_span_mismatch",
                             "fact": f["fact_id"]})
        # numbers/units in a fact must trace to ITS quote — appearing
        # somewhere else in the post is not tracing (AT-025/026)
        if f.get("quantity") and f["quantity"] not in ev["quote"]:
            findings.append({"code": "quantity_untraced",
                             "fact": f["fact_id"]})
    covered = set()
    for c in summary["claims"]:
        findings.extend(claim_quantity_findings(c, facts))
        for r in c["fact_refs"]:
            covered.add(r)
        if c["claim_kind"] == "reported_fact" and not c["evidence_refs"]:
            findings.append({"code": "claim_without_evidence",
                             "claim": c["claim_id"]})
    for i, f in enumerate(facts):
        if f["validation_status"] == "candidate" and i not in covered:
            findings.append({"code": "fact_dropped",
                             "fact": f["fact_id"],
                             "statement": f["statement"]})
    return findings


def audit_claims(jev_client, bundle: dict, summary: dict,
                 deadline: float, match_threshold: float = jev.MATCH_THRESHOLD) -> tuple[list, bool]:
    """Per-claim Jev support check (supports/contradicts/not_supported/
    ambiguous). Returns (findings, evaluated) — evaluated=False means
    the check could not run, i.e. PENDING rather than a silent PASS."""
    findings = []
    if jev_client is None or not _probability(match_threshold):
        return [{"code": "support_unevaluated"}], False
    claims = [c for c in summary["claims"]
              if c["claim_kind"] != "limitation"]
    if not claims:
        return findings, True
    # one request per claim keeps question scope unambiguous (§13.2)
    questions = {}
    for c in claims:
        questions[c["claim_id"]] = jev.choice_question(
            "Does the claim in state.target.text follow from the "
            "provided source spans in state.context? Each evidence "
            "entry pairs the exact quote (role=evidence_quote) with its "
            "surrounding original-text window (role=evidence_context) — "
            "a verbatim quote negated or conditioned by neighboring "
            "text does NOT support the claim. evidence_metadata identifies "
            "the source sender, posting time, revision and parent relation; "
            "use it to check attribution and relative dates. Judge target, value, "
            "polarity and tense together — a same-topic claim with "
            "different drug/dose does not match (AT-024/025/028).",
            jev.CLAIM_SUPPORT_OPTIONS)
    members = {m["message_id"]: m for m in bundle["members"]}
    ev_ctx = {}
    for f in summary.get("_facts", []):
        ev = f.get("_evidence")
        if not ev:
            continue
        quote = ev["quote"]
        src = members.get(ev["message_id"])
        body = src["body_original"] if src else ""
        s, e = ev["start_codepoint"], ev["end_codepoint"]
        # the claim's support is judged against the quote PLUS the whole
        # source body as context — a bare quote cannot reveal a negation
        # or condition sitting elsewhere in the same post (§16.2, AT-028).
        # The full body is deliberately sent (not a clipped window) so a
        # distant qualifier is never lost; the cost is bounded by the job
        # budget (S-3).
        if type(s) is int and type(e) is int and 0 <= s < e <= len(body):
            win = body
        else:
            win = ""
        metadata = {key: src.get(key) for key in
                    ("message_id", "parent_id", "revision", "posted_at", "sender")} if src else None
        ev_ctx[ev["evidence_id"]] = (quote, win, metadata)
    for c in claims:
        ctx = []
        for ev in c["evidence_refs"]:
            quote, win, metadata = ev_ctx.get(ev, ("", "", None))
            ctx.append({"id": ev, "role": "evidence_quote",
                        "text": quote})
            if win and win != quote:
                ctx.append({"id": f"{ev}_ctx",
                            "role": "evidence_context", "text": win})
            if metadata is not None:
                ctx.append({"id": f"{ev}_meta", "role": "evidence_metadata",
                            "text": json.dumps(metadata, ensure_ascii=False)})
        state = {"target": {"id": c["claim_id"], "text": c["text"]},
                 "context": ctx}
        try:
            out = jev_client.evaluate(
                state, {c["claim_id"]: questions[c["claim_id"]]},
                deadline)
        except jev.JevError as error:
            with suppress(Exception):
                jev_client.last_error = error
            return findings + [{"code": "support_unevaluated",
                                "claim": c["claim_id"]}], False
        ans = out["answers"][c["claim_id"]]
        if (not isinstance(ans, dict)
                or not isinstance(ans.get("choice"), str)
                or ans["choice"] not in jev.CLAIM_SUPPORT_OPTIONS
                or not _probability(ans.get("confidence"))):
            return findings + [{"code": "support_unevaluated",
                                "claim": c["claim_id"]}], False
        # raw answers ride on the summary's private channel so the
        # drain can persist them into the audit artifact's meta —
        # accumulated confidences feed threshold calibration (§22).
        raw = summary.setdefault("_claim_audit", {})
        raw[c["claim_id"]] = {"choice": ans["choice"],
                              "confidence": ans["confidence"]}
        if ans["confidence"] < match_threshold:
            findings.append({"code": "claim_low_confidence",
                             "claim": c["claim_id"],
                             "confidence": ans["confidence"]})
        if ans["choice"] in ("contradicts", "not_supported"):
            findings.append({"code": "claim_" + ans["choice"],
                             "claim": c["claim_id"],
                             "confidence": ans["confidence"]})
        elif ans["choice"] == "ambiguous":
            findings.append({"code": "claim_ambiguous",
                             "claim": c["claim_id"],
                             "confidence": ans["confidence"]})
    return findings, True


COVERAGE_OPTIONS = {
    "complete": "all important source events are represented by the facts",
    "missing": "one or more important source events are missing from the facts",
    "ambiguous": "coverage cannot be decided from the supplied source and facts",
}


def _coverage_incomplete(code: str, reason: str = "model") -> dict:
    return {"status": "INCOMPLETE", "evaluated": False, "choice": None,
            "confidence": None, "failure_reason": reason,
            "findings": [{"code": code}]}


def evaluate_source_fact_coverage(jev_client, source_text: str, facts: list,
                                 deadline: float, *, target_id="source",
                                 match_threshold: float | None = None) -> dict:
    """Ask one Jev Choice over the whole source and all extracted facts.

    A technical/model failure is incomplete.  ``missing`` and ``ambiguous``
    are evaluated findings and never a PASS.  Findings contain codes only;
    the source and fact text stay inside the Jev state and are not returned.
    """
    if not isinstance(source_text, str) or not isinstance(facts, list):
        return _coverage_incomplete("source_fact_coverage_input_invalid",
                                    "invalid")
    threshold = jev.MATCH_THRESHOLD if match_threshold is None else match_threshold
    if not _probability(threshold):
        return _coverage_incomplete("source_fact_coverage_input_invalid", "invalid")
    if jev_client is None:
        return _coverage_incomplete("source_fact_coverage_unevaluated",
                                    "resource")
    from semantic_runtime import RuntimeGuardError
    try:
        question_id = "source_fact_coverage"
        question = jev.choice_question(
            "Do the candidate facts in state.context cover every important "
            "event in the original source at state.target.text? Judge the "
            "whole target text, not a shortened span. Choose complete only "
            "when no important event is missing; use missing or ambiguous "
            "when coverage fails or cannot be decided.", COVERAGE_OPTIONS)
        context = []
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict):
                continue
            fact_id = fact.get("fact_id", f"fact_{index}")
            statement = fact.get("statement")
            if isinstance(statement, str):
                context.append({"id": str(fact_id), "role": "fact_candidate",
                                "text": statement})
        state = {"target": {"id": str(target_id), "text": source_text},
                 "context": context}
        response = jev_client.evaluate(
            state, {question_id: question}, deadline)
        answer = response.get("answers", {}).get(question_id)
        choice = answer.get("choice") if isinstance(answer, dict) else None
        confidence = answer.get("confidence") if isinstance(answer, dict) else None
        if (not isinstance(choice, str) or choice not in COVERAGE_OPTIONS
                or not _probability(confidence)):
            return _coverage_incomplete("source_fact_coverage_unevaluated")
    except RuntimeGuardError:
        raise
    except Exception as error:
        reason = ("resource" if getattr(error, "retryable", False)
                  or getattr(error, "kind", "") in {
                      "no_api_key", "budget_exceeded", "transport"}
                  else "model")
        return _coverage_incomplete("source_fact_coverage_unevaluated", reason)

    confidence = float(confidence)
    if choice == "complete" and confidence >= float(threshold):
        return {"status": "PASS", "evaluated": True, "choice": choice,
                "confidence": confidence, "failure_reason": None,
                "findings": []}
    code = ("source_fact_coverage_missing" if choice == "missing"
            else "source_fact_coverage_ambiguous" if choice == "ambiguous"
            else "source_fact_coverage_low_confidence")
    return {"status": "NEEDS_REVIEW", "evaluated": True,
            "choice": choice, "confidence": confidence,
            "failure_reason": None,
            "findings": [{"code": code}]}


FACT_SUPPORT_OPTIONS = {
    "supports": "the evidence fully supports the stated fact",
    "contradicts": "the evidence contradicts the stated fact",
    "not_supported": "the evidence does not establish the fact",
    "ambiguous": "support cannot be decided",
}


def _fact_audit_target(fact: dict) -> str:
    """Deterministic one-line rendering of a v2 fact for the audit
    target — the verifier judges the claim text exactly as stored."""
    fields = [f"kind:{fact.get('kind')}",
              f"polarity:{fact.get('polarity')}",
              f"epistemic:{fact.get('epistemic')}",
              f"workflow:{fact.get('workflow_status')}",
              f"event_time:{fact.get('event_time')}",
              f"subject:{fact.get('subject')}"]
    if fact.get("kind") == "medication_event":
        fields.append(f"action:{fact.get('action')}")
    if "patient_context" in fact:
        from semantic_facts import validate_patient_context
        fields.append("patient_context:" + json.dumps(
            validate_patient_context(fact), ensure_ascii=False, sort_keys=True))
    return f"{fact.get('statement')} [{' '.join(fields)}]"


def audit_facts_v2(jev_client, doc: dict, source_text: str,
                   deadline: float, *,
                   match_threshold: float = jev.MATCH_THRESHOLD) -> dict:
    """Bidirectional post-generation audit of a semantic-facts/v2 doc.

    fact -> evidence: every verified fact must be supported by its own
    evidence quote in source context (negation/subject/time together).
    source -> facts: the shared coverage Choice checks nothing
    important was missed.  Unverifiable work returns INCOMPLETE —
    never a silent PASS.
    """
    findings = []
    if (not isinstance(doc, dict) or not isinstance(source_text, str)
            or not _probability(match_threshold)):
        return {"status": "INCOMPLETE", "evaluated": False,
                "findings": [{"code": "fact_audit_input_invalid"}],
                "fact_verdicts": {}}
    evidence = {e["evidence_id"]: e for e in doc.get("evidence", [])
                if isinstance(e, dict) and e.get("evidence_id")}
    facts = [f for f in doc.get("facts", []) if isinstance(f, dict)]
    unverified = [f for f in facts
                  if f.get("validation_status") != "verified"]
    if unverified:
        findings.append({"code": "unverified_facts",
                         "count": len(unverified)})
    coverage_doc = doc.get("coverage")
    if not isinstance(coverage_doc, dict) \
            or coverage_doc.get("status") != "complete":
        # open obligations (e.g. reopened by a repair that dropped the
        # facts covering them) are a deterministic blocker: the audited
        # facts may all be supported, yet the doc is not complete
        findings.append({"code": "canonical_coverage_incomplete"})
    if jev_client is None:
        findings.append({"code": "fact_audit_unevaluated"})
        return {"status": "INCOMPLETE", "evaluated": False,
                "findings": findings, "fact_verdicts": {}}

    from semantic_runtime import RuntimeGuardError
    verdicts = {}
    for fact in facts:
        if fact.get("validation_status") != "verified":
            continue
        fid = fact["fact_id"]
        ctx = []
        for ref in fact.get("evidence_ids", []):
            ev = evidence.get(ref)
            if ev is None or not isinstance(ev.get("quote"), str):
                continue
            ctx.append({"id": ref, "role": "evidence_quote",
                        "text": ev["quote"]})
            ctx.append({"id": f"{ref}_ctx", "role": "evidence_context",
                        "text": source_text})
        if "patient_context" in fact:
            from semantic_facts import ContractError, validate_patient_context
            try:
                validate_patient_context(fact, [item["text"] for item in ctx
                                                if item["role"] == "evidence_quote"])
            except ContractError:
                findings.append({"code": "fact_patient_context_invalid", "fact": fid})
                continue
        question = jev.choice_question(
            "Does the claim in state.target.text follow from the "
            "evidence_quote entries within the evidence_context source "
            "text? The target ends with structured fields in brackets "
            "(kind/polarity/epistemic/workflow/event_time/subject[/"
            "action]/optional patient_context category/text/details) — judge statement AND fields together: a quote "
            "negated, attributed to another subject, or placed at a "
            "different time does NOT support the claim.",
            FACT_SUPPORT_OPTIONS)
        state = {"target": {"id": fid, "text": _fact_audit_target(fact)},
                 "context": ctx}
        try:
            out = jev_client.evaluate(state, {fid: question}, deadline)
        except RuntimeGuardError:
            raise
        except Exception as error:
            with suppress(Exception):
                jev_client.last_error = error
            findings.append({"code": "fact_audit_unevaluated",
                             "fact": fid})
            return {"status": "INCOMPLETE", "evaluated": False,
                    "findings": findings, "fact_verdicts": verdicts}
        answers = out.get("answers") if isinstance(out, dict) else None
        answer = answers.get(fid) if isinstance(answers, dict) else None
        choice = answer.get("choice") if isinstance(answer, dict) else None
        confidence = answer.get("confidence") \
            if isinstance(answer, dict) else None
        if (not isinstance(choice, str) or choice not in FACT_SUPPORT_OPTIONS
                or not _probability(confidence)):
            findings.append({"code": "fact_audit_unevaluated",
                             "fact": fid})
            return {"status": "INCOMPLETE", "evaluated": False,
                    "findings": findings, "fact_verdicts": verdicts}
        verdicts[fid] = {"choice": choice, "confidence": confidence}
        if confidence < match_threshold:
            findings.append({"code": "fact_low_confidence", "fact": fid})
        if choice in ("contradicts", "not_supported"):
            findings.append({"code": f"fact_{choice}", "fact": fid})
        elif choice == "ambiguous":
            findings.append({"code": "fact_ambiguous", "fact": fid})

    # source -> facts coverage (shared Choice, identical semantics to
    # the legacy audit path).
    coverage = evaluate_source_fact_coverage(
        jev_client, source_text,
        [{"fact_id": f["fact_id"], "statement": f["statement"]}
         for f in facts], deadline, target_id="source",
        match_threshold=match_threshold)
    findings.extend(coverage["findings"])
    if not coverage["evaluated"]:
        return {"status": "INCOMPLETE", "evaluated": False,
                "findings": findings, "fact_verdicts": verdicts}
    status = "PASS" if not findings else "NEEDS_REVIEW"
    return {"status": status, "evaluated": True, "findings": findings,
            "fact_verdicts": verdicts}


def audit_status_for(code_findings: list, jev_findings: list,
                     evaluated: bool, repaired: bool) -> str:
    if not evaluated or any(f["code"] == "input_incomplete"
                            for f in code_findings):
        return "PENDING"
    blocking = [f for f in code_findings
                if f["code"] in ("evidence_missing",
                                 "evidence_revision_mismatch",
                                 "evidence_span_mismatch",
                                 "input_oversize", "source_fact_coverage_missing",
                                 "source_fact_coverage_ambiguous",
                                 "source_fact_coverage_low_confidence",
                                 "medication_detail_low_confidence")
                or f["code"].startswith("medication_detail_")]
    if blocking:
        return "NEEDS_REVIEW"
    repairable = [f for f in code_findings + jev_findings
                  if f["code"] in ("fact_dropped", "claim_not_supported",
                                   "quantity_untraced",
                                   "claim_without_evidence")
                  or f["code"].startswith("claim_quantity_")]
    if repairable and not repaired:
        return "REPAIR_REQUIRED"
    if repairable or [f for f in jev_findings
                      if f["code"] in ("claim_contradicts",
                                       "claim_ambiguous",
                                       "claim_low_confidence")]:
        return "NEEDS_REVIEW"
    return "PASS"
