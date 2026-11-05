"""Semantic audit gates and the audit-status decision tree (spec §16).

Covers code-level checks and per-claim Jev support evaluation.

Leaf module — deliberately independent of the semantic facade so the
audit logic can be exercised without the drain machinery."""
from __future__ import annotations

import json
from contextlib import suppress

import semantic_jev as jev
from semantic_quantities import claim_quantity_findings, extract_quantities


def jev_skip_reason(error) -> str | None:
    """Classify only known Jev unavailability, never an unverified guard."""
    if not isinstance(error, jev.JevError):
        return None
    kind = getattr(error, "kind", None)
    detail = getattr(error, "detail", None)
    if kind == "no_api_key":
        return "not_configured"
    if kind == "budget_exceeded" and detail == "daily_cap":
        return "daily_budget"
    if kind == "timeout" and detail == "job_deadline_exceeded":
        return None
    if kind in ("transport", "timeout", "rate_limited", "auth_error", "payment_required"):
        return "service_unavailable"
    return None


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
        if c["claim_kind"] in ("reported_fact", "limitation") and not c["evidence_refs"]:
            findings.append({"code": "claim_without_evidence",
                             "claim": c["claim_id"]})
    for i, f in enumerate(facts):
        if f["validation_status"] == "candidate" and i not in covered:
            findings.append({"code": "fact_dropped",
                             "fact": f["fact_id"],
                             "statement": f["statement"]})
    return findings


# Claims share a request, so each question names its claim and the exact
# evidence entries it may use: another claim's sources in the same state
# never widen its scope (AT-011).
_CLAIM_QUESTION = (
    "Does the claim with id {claim} in state.target.claims follow from its "
    "cited source spans (state.context entries: {entries})? Judge only this "
    "claim and only against those entries; the other claims and entries in "
    "this request are not its evidence. Each evidence "
    "entry pairs the exact quote (role=evidence_quote) with its "
    "surrounding original-text window (role=evidence_context) — "
    "a verbatim quote negated or conditioned by neighboring "
    "text does NOT support the claim. evidence_metadata identifies "
    "the source sender, posting time, revision and parent relation; "
    "use it to check attribution and relative dates. Judge target, value, "
    "polarity and tense together — a same-topic claim with "
    "different drug/dose does not match (AT-024/025/028).")


_FACT_QUESTION = (
    "Does the fact with id {fact} in state.target.facts follow from its own "
    "evidence_quote entries ({entries}) read within the evidence_context source "
    "text (id source)? Judge only this fact and only against those quotes; the "
    "other facts and quotes in this request are not its evidence. The fact text "
    "ends with structured fields in brackets (kind/polarity/epistemic/workflow/"
    "event_time/valid_time/subject[/action]/optional patient_context category/"
    "text/details) — judge statement AND fields together: a quote negated, "
    "attributed to another subject, or placed at a different time does NOT "
    "support the fact.")


def audit_claims(jev_client, bundle: dict, summary: dict,
                 deadline: float, match_threshold: float = jev.MATCH_THRESHOLD,
                 per_request: int = 12) -> tuple[list, bool]:
    """Per-claim Jev support check (supports/contradicts/not_supported/
    ambiguous), up to per_request claims in one request. Returns
    (findings, evaluated) — evaluated=False means the check could not
    run, i.e. PENDING rather than a silent PASS."""
    findings = []
    summary.pop("_claim_audit_reason", None)
    claims = summary.get("claims")
    if not isinstance(claims, list):
        summary["_claim_audit_reason"] = "claims_invalid"
        return [{"code": "support_unevaluated"}], False
    if not claims:
        summary["_claim_audit_reason"] = "claims_empty"
        return [{"code": "support_unevaluated"}], False
    if jev_client is None:
        summary["_claim_audit_reason"] = "client_unavailable"
        return [{"code": "support_unevaluated"}], False
    # per_request has semantic_policy's max_questions_per_request bound.
    if not _probability(match_threshold) or type(per_request) is not int or not 1 <= per_request <= 48:
        summary["_claim_audit_reason"] = "options_invalid"
        return [{"code": "support_unevaluated"}], False
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
    for start in range(0, len(claims), per_request):
        chunk = claims[start:start + per_request]
        ctx, questions = {}, {}
        for c in chunk:
            cited = []
            for ev in c["evidence_refs"]:
                quote, win, metadata = ev_ctx.get(ev, ("", "", None))
                entries = [{"id": ev, "role": "evidence_quote",
                            "text": quote}]
                if win and win != quote:
                    entries.append({"id": f"{ev}_ctx",
                                    "role": "evidence_context", "text": win})
                if metadata is not None:
                    entries.append({"id": f"{ev}_meta", "role": "evidence_metadata",
                                    "text": json.dumps(metadata, ensure_ascii=False)})
                for entry in entries:
                    ctx[entry["id"]] = entry      # a shared source is sent once
                    cited.append(entry["id"])
            questions[c["claim_id"]] = jev.choice_question(
                _CLAIM_QUESTION.format(claim=c["claim_id"],
                                       entries=", ".join(cited) or "none"),
                jev.CLAIM_SUPPORT_OPTIONS)
        state = {"target": {"id": "claims",
                            "claims": [{"id": c["claim_id"], "text": c["text"]}
                                       for c in chunk]},
                 "context": list(ctx.values())}
        partial = False
        try:
            out = jev.evaluate_once_more(jev_client, state, questions, deadline)
        except jev.JevError as error:
            summary["_claim_audit_reason"] = "service_error"
            with suppress(Exception):
                jev_client.last_error = error
            out = {"answers": getattr(error, "partial_answers", {})}
            partial = True
        answers = out.get("answers") if isinstance(out, dict) else None
        for c in chunk:
            ans = answers.get(c["claim_id"]) if isinstance(answers, dict) else None
            if partial and ans is None:
                continue
            if (not isinstance(ans, dict)
                    or not isinstance(ans.get("choice"), str)
                    or ans["choice"] not in jev.CLAIM_SUPPORT_OPTIONS
                    or not _probability(ans.get("confidence"))):
                summary["_claim_audit_reason"] = "response_invalid"
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
        if partial:
            return findings + [{"code": "support_unevaluated",
                                "claim": chunk[0]["claim_id"]}], False
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
        response = jev.evaluate_once_more(
            jev_client, state, {question_id: question}, deadline)
        answer = response.get("answers", {}).get(question_id)
        choice = answer.get("choice") if isinstance(answer, dict) else None
        confidence = answer.get("confidence") if isinstance(answer, dict) else None
        if (not isinstance(choice, str) or choice not in COVERAGE_OPTIONS
                or not _probability(confidence)):
            return _coverage_incomplete("source_fact_coverage_unevaluated")
    except RuntimeGuardError:
        raise
    except Exception as error:
        # the drain's scheduling classes; a permanent failure reads 'model'
        reason = {"resource": "resource", "retry": "retry"}.get(
            jev.failure_class(error), "model")
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
              f"valid_time:{fact.get('valid_time')}",
               f"subject:{fact.get('subject')}",
               f"quantity:{fact.get('quantity', 'unknown')}"]
    if fact.get("kind") == "medication_event":
        fields.append(f"action:{fact.get('action')}")
    if "patient_context" in fact:
        from semantic_facts import validate_patient_context
        fields.append("patient_context:" + json.dumps(
            validate_patient_context(fact), ensure_ascii=False, sort_keys=True))
    return f"{fact.get('statement')} [{' '.join(fields)}]"


def published_quantity_findings(doc: dict) -> list:
    """Check known medication quantities against only their own evidence quotes."""
    evidence = {item["evidence_id"]: item for item in doc.get("evidence", [])
                if isinstance(item, dict) and item.get("evidence_id")}
    findings = []
    for fact in doc.get("facts", []):
        if (not isinstance(fact, dict)
                or fact.get("validation_status") != "verified"
                or fact.get("kind") not in ("medication_event", "medication_exposure")
                or fact.get("quantity", "unknown") in (None, "unknown")):
            continue
        quantity = fact["quantity"]
        fid = fact.get("fact_id")
        if not extract_quantities(quantity):
            findings.append({"code": "fact_quantity_unverified", "fact": fid})
            continue
        quotes = [{"_evidence": {"quote": evidence[ref].get("quote")}}
                  for ref in fact.get("evidence_ids", []) if ref in evidence]
        checked = claim_quantity_findings(
            {"claim_id": fid, "text": f"{fact.get('statement', '')} [quantity:{quantity}]",
             "fact_refs": list(range(len(quotes)))}, quotes)
        findings.extend({"code": item["code"].replace("claim_", "fact_", 1), "fact": fid}
                        for item in checked)
    return findings


def audit_facts_v2(jev_client, doc: dict, source_text: str,
                   deadline: float, *,
                   match_threshold: float = jev.MATCH_THRESHOLD,
                   per_request: int = 12) -> dict:
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
    findings.extend(published_quantity_findings(doc))
    coverage_doc = doc.get("coverage")
    if not isinstance(coverage_doc, dict) \
            or coverage_doc.get("status") != "complete":
        # open obligations (e.g. reopened by a repair that dropped the
        # facts covering them) are a deterministic blocker: the audited
        # facts may all be supported, yet the doc is not complete
        findings.append({"code": "canonical_coverage_incomplete"})
    # an invalid batch size must not skip every fact (range() runs empty)
    if type(per_request) is not int or not 1 <= per_request <= 48:
        findings.append({"code": "fact_audit_unevaluated"})
        return {"status": "INCOMPLETE", "evaluated": False,
                "findings": findings, "fact_verdicts": {}}

    from semantic_runtime import RuntimeGuardError
    verdicts = {}
    pending = []
    for fact in facts:
        if fact.get("validation_status") != "verified":
            continue
        fid = fact["fact_id"]
        quotes = []
        for ref in fact.get("evidence_ids", []):
            ev = evidence.get(ref)
            if ev is not None and isinstance(ev.get("quote"), str):
                quotes.append((ref, ev["quote"]))
        if "patient_context" in fact:
            from semantic_facts import ContractError, validate_patient_context
            try:
                validate_patient_context(fact, [q for _, q in quotes])
            except ContractError:
                findings.append({"code": "fact_patient_context_invalid", "fact": fid})
                continue
        pending.append((fact, quotes))
    if jev_client is None:
        findings.append({"code": "fact_audit_unevaluated"})
        return {"status": "INCOMPLETE", "evaluated": False,
                "findings": findings, "fact_verdicts": {}}
    # Facts share one source: up to per_request facts ride in one request and
    # the source text is sent once, instead of one request (and one full
    # source copy) per fact.
    for start in range(0, len(pending), per_request):
        chunk = pending[start:start + per_request]
        # context ids are code-owned aliases: an evidence id may itself be
        # "source" (or collide with another fact's alias), so quotes never
        # share the id space of the one source context
        ctx = {"source": {"id": "source", "role": "evidence_context", "text": source_text}}
        aliases = {}
        questions = {}
        for fact, quotes in chunk:
            cited = []
            for ref, quote in quotes:
                alias = aliases.setdefault(ref, f"quote_{len(aliases) + 1}")
                ctx[alias] = {"id": alias, "role": "evidence_quote", "text": quote}
                cited.append(alias)
            questions[fact["fact_id"]] = jev.choice_question(
                _FACT_QUESTION.format(fact=fact["fact_id"], entries=", ".join(cited) or "none"),
                FACT_SUPPORT_OPTIONS)
        state = {"target": {"id": "facts", "facts": [
                     {"id": fact["fact_id"], "text": _fact_audit_target(fact)} for fact, _ in chunk]},
                 "context": list(ctx.values())}
        partial = False
        try:
            out = jev.evaluate_once_more(jev_client, state, questions, deadline)
        except RuntimeGuardError:
            raise
        except Exception as error:
            with suppress(Exception):
                jev_client.last_error = error
            out = {"answers": getattr(error, "partial_answers", {})}
            partial = True
        answers = out.get("answers") if isinstance(out, dict) else None
        for fact, _ in chunk:
            fid = fact["fact_id"]
            answer = answers.get(fid) if isinstance(answers, dict) else None
            if partial and answer is None:
                continue
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
        if partial:
            findings.append({"code": "fact_audit_unevaluated", "fact": chunk[0][0]["fact_id"]})
            return {"status": "INCOMPLETE", "evaluated": False,
                    "findings": findings, "fact_verdicts": verdicts}

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
    if any(f["code"] == "input_oversize" for f in code_findings):
        return "NEEDS_REVIEW"
    if not evaluated or any(f["code"] == "input_incomplete"
                            for f in code_findings):
        return "PENDING"
    blocking = [f for f in code_findings
                if f["code"] in ("evidence_missing",
                                 "evidence_revision_mismatch",
                                 "evidence_span_mismatch",
                                 "source_fact_coverage_missing",
                                 "source_fact_coverage_ambiguous",
                                 "source_fact_coverage_low_confidence")
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


def optional_audit_status(code_findings: list, jev_findings: list,
                          skip_reason: str | None, repaired: bool) -> str:
    """Keep concrete findings when Jev could not finish its evaluation."""
    if skip_reason not in {"not_configured", "daily_budget", "backlog_reserved",
                           "disabled", "service_unavailable", "circuit_open"}:
        return audit_status_for(code_findings, jev_findings, False, repaired)
    permitted = {"support_unevaluated", "claim_not_supported", "claim_contradicts",
                 "claim_ambiguous", "claim_low_confidence"}
    if any(not isinstance(finding, dict) or finding.get("code") not in permitted
           for finding in jev_findings):
        return "NEEDS_REVIEW"
    known = [finding for finding in jev_findings
             if finding["code"] != "support_unevaluated"]
    result = audit_status_for(code_findings, known, True, repaired)
    if result == "PASS" and (code_findings or known):
        return "NEEDS_REVIEW"
    return "SKIPPED" if result == "PASS" else result


def fact_audit_verdict(audit, doc_hash: str) -> str | None:
    """Completed fact-audit status for the v2 document ``doc_hash``, or
    None when it has none. ``audit`` is the generation's NEWEST
    ``semantic_facts_audit`` row for the policy (``semantic_store.
    current_artifact(..., KIND_FACT_AUDIT, mid, fp, policy)``), exactly the row
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



def cached_fact_audit(audit, doc: dict, doc_hash: str):
    """Reuse only a completed document-bound audit whose PASS still verifies quantities."""
    verdict = fact_audit_verdict(audit, doc_hash)
    if verdict is not None and (verdict != "PASS" or not published_quantity_findings(doc)):
        return audit["content"]
    return None


def canonical_audit_disposition(audit: dict, doc: dict) -> str:
    """Keep incomplete coverage non-PASS even when the evaluated audit says PASS."""
    if not audit["evaluated"]:
        return "wait"
    if audit["status"] == "PASS":
        return "pass" if doc["coverage"]["status"] == "complete" else "coverage_incomplete"
    return "review"


def optional_fact_audit(doc, member, source_fingerprint, reason, previous=None,
                        match_threshold=jev.MATCH_THRESHOLD):
    """Check the full source-bound fact contract without fabricating a Jev verdict."""
    import semantic_assembly
    import semantic_facts
    allowed = {"not_configured", "disabled", "daily_budget", "backlog_reserved",
               "service_unavailable", "circuit_open"}
    result = {"status": "NEEDS_REVIEW", "evaluated": False,
              "findings": [{"code": "fact_optional_validation_failed"}], "fact_verdicts": {}}
    if reason not in allowed or not isinstance(member, dict) or not _probability(match_threshold):
        return result
    try:
        body = member["body_original"]
        if (not isinstance(body, str) or member.get("body_state") != "full"
                or member.get("attachments_complete", True) is not True
                or semantic_assembly.source_binding(member, source_fingerprint) != doc.get("source")):
            return result
        semantic_facts.validate_facts_doc(doc)
        for item in doc["evidence"]:
            if body[item["start"]:item["end"]] != item["quote"]:
                return result
        if previous is not None:
            if not isinstance(previous, dict) or not isinstance(previous.get("findings"), list):
                return result
            permitted = {"fact_audit_unevaluated", "source_fact_coverage_unevaluated"}
            if any(not isinstance(item, dict) or item.get("code") not in permitted
                   for item in previous["findings"]):
                return {**result, "findings": previous["findings"]}
            verdicts = previous.get("fact_verdicts", {})
            if not isinstance(verdicts, dict) or any(
                    not isinstance(value, dict) or value.get("choice") != "supports"
                    or not _probability(value.get("confidence"))
                    or value["confidence"] < match_threshold
                    for value in verdicts.values()):
                return result
        checked = audit_facts_v2(None, doc, body, deadline=0)
        remaining = [item for item in checked["findings"] if item.get("code") != "fact_audit_unevaluated"]
        if remaining:
            return {**result, "findings": remaining}
        return {"status": "SKIPPED", "evaluated": False,
                "findings": [{"code": "fact_audit_unevaluated"}], "fact_verdicts": {},
                "jev_skip_reason": reason, "code_checks": "passed"}
    except (KeyError, TypeError, ValueError, RecursionError, OverflowError):
        return result


def fact_repair_plan(findings: list, attempt: int) -> tuple[dict, bool, bool]:
    """Select targeted rejection/coverage repair only on the original first audit."""
    rejected = {f["fact"]: f["code"] for f in findings if f.get("fact")}
    coverage_missing = any(f.get("code") == "source_fact_coverage_missing" for f in findings)
    return rejected, coverage_missing, bool(attempt == 0 and (rejected or coverage_missing))


_PATIENT_SUMMARY_CHUNK_LIMIT = 12
_PATIENT_SUMMARY_OMISSION_OPTIONS = {
    "omission_ok": "the fact may be omitted from a short overview while detailed records remain available",
    "important_missing": "the omitted fact is important to current state, change, background, plans or unresolved issues",
    "ambiguous": "the importance of this omission cannot be decided from the supplied source and overview",
}
_PATIENT_SOURCE_CONTEXT_FIELDS = ("project_id", "message_id", "source", "content_hash",
    "body_state", "posted_at", "posted_at_ts", "sender_id", "sender_name", "parent_id")


def _patient_quality_sources(sources):
    """Index exact source occurrences without granting publication or clinical validity."""
    import original_source_keys
    from mcs_util import valid_hash

    indexed, project_id = {}, None
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("patient_quality_source_invalid")
        pid, mid = source.get("project_id"), source.get("message_id")
        kind, key, body = source.get("source"), source.get("source_key", mid), source.get("body_text")
        if (type(pid) is not int or pid <= 0 or type(mid) is not int or mid < 0
                or kind not in ("chat", "karte_summary", "attachment_ocr")
                or (kind == "chat" and (mid <= 0 or type(key) is not int or key != mid))
                or (kind != "chat" and (mid != 0 or not original_source_keys.valid(key)
                    or original_source_keys.parse(key)[0] != ("karte" if kind == "karte_summary" else kind)))
                or not valid_hash(source.get("content_hash"))
                or source.get("body_state") not in (None, "full")
                or not isinstance(body, str) or not body.strip() or "\x00" in body):
            raise ValueError("patient_quality_source_invalid")
        if project_id is None:
            project_id = pid
        if project_id != pid or (kind, key) in indexed:
            raise ValueError("patient_quality_source_invalid")
        indexed[(kind, key)] = source
    return indexed


def _patient_quality_facts(candidates, indexed):
    """Bind every supplied fact and both quotations to its own source occurrence."""
    from mcs_util import valid_hash

    checked, seen = [], set()
    for fact in candidates:
        if not isinstance(fact, dict) or not valid_hash(fact.get("id")) or fact["id"] in seen:
            raise ValueError("patient_quality_fact_invalid")
        seen.add(fact["id"])
        kind, key = fact.get("source"), fact.get("source_key")
        if not isinstance(kind, str) or kind not in ("chat", "karte_summary", "attachment_ocr") or type(key) not in (int, str):
            raise ValueError("patient_quality_fact_invalid")
        source = indexed.get((kind, key))
        if (source is None or type(fact.get("project_id")) is not int
                or fact.get("project_id") != source["project_id"]
                or type(fact.get("message_id")) is not int or fact.get("message_id") != source["message_id"]
                or fact.get("content_hash") != source["content_hash"]):
            raise ValueError("patient_quality_fact_invalid")
        body = source["body_text"]
        for span_key, text_key in (("evidence_span", "evidence"), ("quote_span", "quote")):
            span, text = fact.get(span_key), fact.get(text_key)
            if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                    or not 0 <= span[0] < span[1] <= len(body) or not isinstance(text, str)
                    or body[span[0]:span[1]] != text):
                raise ValueError("patient_quality_fact_invalid")
        checked.append((fact, source))
    return checked


def patient_source_question_keys(body, *, source_input_key, assessment):
    """Bind requested source questions independently of candidate grouping and aliases."""
    from mcs_util import canonical, payload_hash, valid_hash

    if (not valid_hash(source_input_key) or not isinstance(assessment, dict)
            or set(assessment) != {"code", "policy"}
            or any(not valid_hash(value) for value in assessment.values())):
        raise ValueError("patient_source_question_invalid")
    try:
        if not isinstance(body, dict) or body.get("model") != jev.JEV_MODEL:
            raise ValueError
        state, questions = body["state"], body["questions"]
        contexts, target = state["context"], state["target"]
        if (not isinstance(contexts, list) or len(contexts) != 1
                or not isinstance(contexts[0], dict) or contexts[0].get("id") != "s0"
                or contexts[0].get("role") != "original_source"
                or not isinstance(questions, dict) or not 1 <= len(questions) <= 12
                or any(qid not in {f"q{i}" for i in range(12)} for qid in questions)):
            raise ValueError
        source_context = contexts[0]
        if set(source_context) != {"id", "role", "text", "source_key", "body_hash", *_PATIENT_SOURCE_CONTEXT_FIELDS}:
            raise ValueError
        source = {**source_context, "body_text": source_context["text"]}
        if source.get("body_hash") != payload_hash(source["body_text"]):
            raise ValueError
        sources = _patient_quality_sources([source])
        targets = target["facts"]
        if not isinstance(targets, list) or not 1 <= len(targets) <= 12:
            raise ValueError
        indexed = {}
        for entry in targets:
            qid, fact = entry["id"], entry["fact"]
            if qid not in {f"q{i}" for i in range(12)} or qid in indexed or not valid_hash(fact.get("id")):
                raise ValueError
            indexed[qid] = fact
        if not set(questions) <= set(indexed):
            raise ValueError
        kind = target["id"]
        if kind == "source_facts":
            _patient_quality_facts(list(indexed.values()), sources)
            passing = "supports"
        elif kind == "source_coverage":
            if len(indexed) != 1 or not isinstance(target.get("candidate_facts"), list):
                raise ValueError
            _patient_quality_facts(target["candidate_facts"], sources)
            all_meaning = sorted(({key: value for key, value in item.items()
                                   if key not in ("found_at", "found_night_id", "discovery")}
                                  for item in target["candidate_facts"]), key=lambda item: item["id"])
            source_ref = {key: value for key, value in source_context.items() if key not in ("id", "role", "text")}
            [coverage] = indexed.values()
            if coverage != {"id": payload_hash(["patient_source_coverage", source_ref, all_meaning]),
                            "kind": "source_coverage"}:
                raise ValueError
            passing = "complete"
        else:
            raise ValueError
        canonical(body)
        output = {}
        for qid, question in questions.items():
            if (not isinstance(question, dict) or question.get("type") != "choice"
                    or not isinstance(question.get("instructions"), str) or not question["instructions"].strip()
                    or not isinstance(question.get("criteria"), dict) or passing not in question["criteria"]
                    or any(not isinstance(option, str) or not isinstance(text, str)
                           for option, text in question["criteria"].items())):
                raise ValueError
            fact = indexed[qid]
            candidates = target["candidate_facts"] if kind == "source_coverage" else [fact]
            meaning = sorted(({key: value for key, value in item.items()
                               if key not in ("found_at", "found_night_id", "discovery")}
                              for item in candidates), key=lambda item: item["id"])
            output[fact["id"]] = payload_hash(["patient-source-question/v1", source_input_key,
                source_context, kind, meaning, question, passing, assessment, body["model"]])
        if len(output) != len(questions):
            raise ValueError
        return output
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("patient_source_question_invalid") from None


def patient_source_quality_pages(source, facts, *, source_input_key, assessment):
    """Plan all context-fact support pages and one whole-source coverage question."""
    from copy import deepcopy
    from mcs_util import canonical, payload_hash
    from patient_context import CATEGORIES

    if not isinstance(facts, list):
        raise ValueError("patient_source_page_invalid")
    indexed = _patient_quality_sources([source])
    checked = _patient_quality_facts(facts, indexed)
    # Discovery bookkeeping is not interpretation. Reordering or rescanning the
    # same assertions must not give a normal negative answer a new logical ID.
    candidates = sorted((deepcopy({k: v for k, v in fact.items()
                                  if k not in ("found_at", "found_night_id", "discovery")})
                         for fact, _ in checked), key=lambda fact: fact["id"])
    source_ref = {k: source.get(k) for k in _PATIENT_SOURCE_CONTEXT_FIELDS}
    source_ref["source_key"] = source.get("source_key", source["message_id"])
    source_ref["body_hash"] = payload_hash(source["body_text"])
    context = [{"id": "s0", "role": "original_source", "text": source["body_text"], **source_ref}]
    canonical({"context": context, "facts": candidates})
    for start in range(0, len(candidates), _PATIENT_SUMMARY_CHUNK_LIMIT):
        chunk = candidates[start:start + _PATIENT_SUMMARY_CHUNK_LIMIT]
        targets, questions = [], {}
        for index, fact in enumerate(chunk):
            qid = f"q{index}"
            targets.append({"id": qid, "source_id": "s0", "fact": fact})
            questions[qid] = jev.choice_question(
                f"Judge the context assertion with stable fact ID {fact['id']} against only context source s0. "
                "Does its exact evidence occurrence support the entire context assertion, category and details, "
                "including subject, negation, uncertainty, conditions, past/current/planned time and relations? "
                "Unknown or unspecified attributes must stay unknown; other sources are not evidence.",
                FACT_SUPPORT_OPTIONS)
        ids = [fact["id"] for fact in chunk]
        page = {"purpose": "source", "state": {"target": {"id": "source_facts", "facts": targets},
                "context": context}, "questions": questions, "fact_ids": ids,
                "passing_choices": dict.fromkeys(ids, "supports")}
        page["question_keys"] = patient_source_question_keys(
            {"model": jev.JEV_MODEL, "state": page["state"], "questions": questions},
            source_input_key=source_input_key, assessment=assessment)
        yield page
    coverage_id = payload_hash(["patient_source_coverage", source_ref, candidates])
    question = jev.choice_question(
        "Do all candidate context facts in state.target.candidate_facts cover every important patient-context "
        "assertion in the entire original source at context s0, across these context categories: "
        + ", ".join(sorted(CATEGORIES)) + "? Judge the complete source and all candidates, including negation, "
        "other people, uncertainty, past/current/planned time and changed states. Choose complete only when "
        "no important context assertion is missing; zero candidates do not prove absence. This is context "
        "extraction coverage, not canonical clinical-fact coverage or publication approval.", COVERAGE_OPTIONS)
    page = {"purpose": "source", "state": {"target": {"id": "source_coverage",
            "facts": [{"id": "q0", "fact": {"id": coverage_id, "kind": "source_coverage"}}],
            "candidate_facts": candidates}, "context": context}, "questions": {"q0": question},
            "fact_ids": [coverage_id], "passing_choices": {coverage_id: "complete"}}
    page["question_keys"] = patient_source_question_keys(
        {"model": jev.JEV_MODEL, "state": page["state"], "questions": page["questions"]},
        source_input_key=source_input_key, assessment=assessment)
    yield page


def audit_patient_quality_page(jev_client, page, deadline, threshold, *,
                               prior_answers=None, on_valid_answers=None, on_attempt_result=None):
    """Evaluate one owner-bound page, persisting answers before a finite malformed retry."""
    import time
    from mcs_util import canonical, valid_hash

    purpose = page.get("purpose") if isinstance(page, dict) else None
    prefix = "patient_summary" if purpose == "summary" else "patient_source"

    def invalid(code=None):
        code = code or prefix + ("_chunk_invalid" if purpose == "summary" else "_page_invalid")
        return {"evaluated": False, "findings": [{"code": code}], "answers": {},
                "failure_class": "resource" if code == prefix + "_audit_deadline" else "failed"}

    if (purpose not in ("summary", "source") or jev_client is None
            or (not callable(on_valid_answers) and not callable(on_attempt_result))
            or (on_valid_answers is not None and on_attempt_result is not None)
            or not _probability(threshold)
            or type(deadline) not in (int, float) or not float('-inf') < deadline < float('inf')):
        return invalid()
    if time.monotonic() >= deadline:
        return invalid(prefix + "_audit_deadline")
    try:
        state, questions, ids, passing = (page[k] for k in ("state", "questions", "fact_ids", "passing_choices"))
        targets = state["target"]["facts"]
        if (not isinstance(questions, dict) or not isinstance(ids, list)
                or not 1 <= len(ids) <= _PATIENT_SUMMARY_CHUNK_LIMIT or len(set(ids)) != len(ids)
                or any(not valid_hash(fid) for fid in ids) or not isinstance(targets, list)
                or len(targets) != len(ids) or not isinstance(passing, dict) or set(passing) != set(ids)
                or any(value not in ({"supports", "omission_ok"} if purpose == "summary"
                                     else {"supports", "complete"}) for value in passing.values())):
            return invalid()
        facts_by_qid = {f"q{index}": fid for index, fid in enumerate(ids)}
        if set(questions) != set(facts_by_qid) or any(
                target["id"] != qid or target["fact"]["id"] != fid
                or not isinstance(questions[qid]["criteria"], dict)
                or passing[fid] not in questions[qid]["criteria"]
                for target, (qid, fid) in zip(targets, facts_by_qid.items())):
            return invalid()
        canonical({"state": state, "questions": questions})
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        return invalid()
    questions_by_fact = {facts_by_qid[qid]: question for qid, question in questions.items()}
    if prior_answers is None:
        prior_answers = {}
    if not isinstance(prior_answers, dict) or set(prior_answers) - set(questions_by_fact):
        return invalid()

    def normalize(fact_id, answer):
        options = questions_by_fact[fact_id]["criteria"]
        if (not isinstance(answer, dict) or not isinstance(answer.get("choice"), str)
                or answer["choice"] not in options or not _probability(answer.get("confidence"))):
            return None
        return {"choice": answer["choice"], "confidence": answer["confidence"]}

    saved = {}
    for fact_id, answer in prior_answers.items():
        normalized = normalize(fact_id, answer)
        if normalized is None:
            return invalid()
        saved[fact_id] = normalized
    pending = {qid: question for qid, question in questions.items() if facts_by_qid[qid] not in saved}
    callback_failed = False

    def persist(raw, invalid_ids=None):
        nonlocal callback_failed
        if not isinstance(raw, dict) or set(raw) - set(pending):
            raise ValueError(prefix + "_answer_invalid")
        mapped = {}
        for qid, answer in raw.items():
            fact_id = facts_by_qid[qid]
            if fact_id in saved:
                raise ValueError(prefix + "_answer_duplicate")
            normalized = normalize(fact_id, answer)
            if normalized is None:
                raise ValueError(prefix + "_answer_invalid")
            mapped[fact_id] = normalized
        try:
            from copy import deepcopy
            if on_attempt_result is None:
                result = on_valid_answers(deepcopy(mapped))
            else:
                if not isinstance(invalid_ids, set) or not invalid_ids <= set(pending):
                    raise ValueError(prefix + "_answer_invalid")
                result = on_attempt_result(deepcopy(mapped), {facts_by_qid[qid] for qid in invalid_ids})
            if result is False:
                raise RuntimeError(prefix + "_answer_persistence_failed")
        except BaseException:
            callback_failed = True
            raise
        saved.update(mapped)

    failure = None
    if pending:
        try:
            jev.evaluate_once_more(jev_client, state, pending, deadline,
                **({"on_attempt_result": persist} if on_attempt_result is not None
                   else {"on_valid_answers": persist}))
        except jev.JevError as error:
            if callback_failed:
                raise
            failure = jev.failure_class(error)
    answers, findings = {}, []
    for fact_id in ids:
        answer = saved.get(fact_id)
        if answer is None:
            continue
        choice, confidence = answer["choice"], answer["confidence"]
        answers[fact_id] = answer
        if confidence < threshold:
            findings.append({"code": prefix + "_low_confidence", "fact_id": fact_id})
        if choice != passing[fact_id]:
            findings.append({"code": prefix + "_" + choice, "fact_id": fact_id})
    complete = len(answers) == len(ids)
    if not complete:
        findings.append({"code": prefix + "_audit_unevaluated"})
    return {"evaluated": complete, "findings": findings, "answers": answers,
            **({"failure_class": failure} if failure is not None else {})}


def audit_patient_summary_chunk(jev_client, summary, candidates, sources, deadline, threshold, *,
                                prior_answers=None, on_valid_answers=None, on_attempt_result=None):
    """Judge one finite source-backed chunk; the owner proves scope and currentness.

    Before calling, the owner validates facts and source bodies on the same
    authorized snapshot, including the source hash and occurrence, and binds
    answers to its source, facts, summary and assessment contract. The required
    callback durably stores validated answers or raises; prior_answers must be
    loaded under that same binding and exclusive claim. This helper verifies
    only source identity and exact quote spans; it does not prove
    clinical attributes, whole-patient coverage or publication eligibility.
    """
    from mcs_util import valid_hash

    def invalid():
        return {"evaluated": False, "findings": [{"code": "patient_summary_chunk_invalid"}],
                "answers": {}, "failure_class": "failed"}

    if (not isinstance(summary, list) or not 1 <= len(summary) <= 12
            or not isinstance(candidates, list) or not 1 <= len(candidates) <= _PATIENT_SUMMARY_CHUNK_LIMIT
            or not isinstance(sources, list) or not 1 <= len(sources) <= _PATIENT_SUMMARY_CHUNK_LIMIT):
        return invalid()
    selected = {}
    for item in summary:
        if (not isinstance(item, dict) or set(item) != {"text", "fact_ids"}
                or not isinstance(item["text"], str) or not item["text"].strip()
                or not isinstance(item["fact_ids"], list) or len(item["fact_ids"]) != 1
                or not valid_hash(item["fact_ids"][0]) or item["fact_ids"][0] in selected):
            return invalid()
        selected[item["fact_ids"][0]] = item["text"]
    try:
        checked = _patient_quality_facts(candidates, _patient_quality_sources(sources))
    except ValueError:
        return invalid()
    if any(fact["id"] in selected and selected[fact["id"]] != (fact.get("quote") or fact.get("text"))
           for fact, _ in checked):
        return invalid()
    source_ids, context, facts, questions = {}, [], [], {}
    for index, (fact, source) in enumerate(checked):
        source_key = (source["source"], source.get("source_key", source["message_id"]))
        if source_key not in source_ids:
            source_id = f"s{len(source_ids)}"
            source_ids[source_key] = source_id
            context.append({"id": source_id, "role": "original_source", "text": source["body_text"],
                            "message_id": source["message_id"], "source_key": source_key[1],
                            "posted_at": source.get("posted_at"), "sender": source.get("sender_name")})
        source_id = source_ids[source_key]
        qid = f"q{index}"
        fact_data = {k: fact.get(k) for k in ("id", "category", "text", "evidence", "evidence_span",
                     "reported_subject", "temporality", "temporal", "details", "view_bucket", "relations")}
        facts.append({"id": qid, "source_id": source_id, "fact": fact_data,
                      "selected_text": selected.get(fact["id"])})
        included = fact["id"] in selected
        instruction = (f"Judge target.facts entry {qid} against only context source {source_id}; "
                       "other context sources are not its evidence. "
                       + ("Does the selected overview statement preserve the source's subject, time and polarity?"
                          if included else
                          "Can this fact be omitted from the short overview while its detail remains available? "
                          "Consider current state, changes, important background, plans and unresolved issues."))
        options = jev.CLAIM_SUPPORT_OPTIONS if included else _PATIENT_SUMMARY_OMISSION_OPTIONS
        questions[qid] = jev.choice_question(instruction, options)
    ids = [fact["id"] for fact, _ in checked]
    page = {"purpose": "summary", "state": {"target": {"id": "overview", "summary": summary, "facts": facts},
            "context": context}, "questions": questions, "fact_ids": ids,
            "passing_choices": {fid: "supports" if fid in selected else "omission_ok" for fid in ids}}
    return audit_patient_quality_page(jev_client, page, deadline, threshold, prior_answers=prior_answers,
        on_valid_answers=on_valid_answers, on_attempt_result=on_attempt_result)


def patient_source_quality_identity(source, facts, occurrence, contract, chunks_sig, assessment):
    """Build source QA identity from source and the selected, strictly validated facts."""
    import hashlib
    from mcs_util import html_to_text, payload_hash, valid_hash

    if (not isinstance(source, dict) or not isinstance(facts, list)
            or not isinstance(occurrence, dict) or set(occurrence) != {'source_binding', 'first_seen', 'scope'}
            or not valid_hash(occurrence['source_binding']) or not isinstance(occurrence['scope'], dict)
            or set(occurrence['scope']) != {'project_id', 'project_type', 'karte_id', 'is_archived'}
            or occurrence['scope']['project_id'] != source.get('project_id')
            or not isinstance(contract, dict) or not valid_hash(chunks_sig)
            or not isinstance(assessment, dict) or set(assessment) != {'code', 'policy'}
            or any(not valid_hash(value) for value in assessment.values())):
        raise ValueError('patient_source_identity_invalid')
    body = source.get('body_text')
    if not isinstance(body, str):
        raise ValueError('patient_source_identity_invalid')
    if source.get('source') == 'chat':
        html = source.get('body_html')
        if (not isinstance(html, str) or hashlib.sha256(html.encode('utf-8')).hexdigest() != source.get('content_hash')
                or html_to_text(html) != body):
            raise ValueError('patient_source_identity_invalid')
    source_key = source.get('source_key', source.get('message_id'))
    source_input_key = payload_hash(['patient-source-input/1', source['project_id'], occurrence['scope'],
        source_key, occurrence['source_binding'], occurrence['first_seen'], source.get('content_hash'),
        hashlib.sha256(body.encode('utf-8')).hexdigest(), contract])
    pages = list(patient_source_quality_pages(source, facts, source_input_key=source_input_key, assessment=assessment))
    ids = [fid for page in pages for fid in page['fact_ids']]
    passing = {fid: choice for page in pages for fid, choice in page['passing_choices'].items()}
    question_keys = {fid: key for page in pages for fid, key in page['question_keys'].items()}
    if not ids or len(ids) != len(set(ids)) or set(ids) != set(passing) or set(ids) != set(question_keys):
        raise ValueError('patient_source_identity_invalid')
    input_key = payload_hash(['source', source_input_key])
    candidate_key = payload_hash(sorted(payload_hash([{key:value for key,value in fact.items()
        if key not in ('found_at','found_night_id','discovery')}]) for fact in facts))
    material = [[page['fact_ids'], page['state'], page['questions'], page['passing_choices']] for page in pages]
    failure_key = payload_hash(['source', input_key, candidate_key, assessment])
    identity = {'purpose':'source', 'input_key':input_key, 'candidate_key':candidate_key,
        'failure_key':failure_key, 'audit_key':payload_hash([failure_key, chunks_sig, material]),
        'fact_ids':ids, 'passing_choices':passing, 'source_fact_count':len(facts),
        'assessment':assessment, 'source_input_key':source_input_key, 'question_keys':question_keys}
    return identity, pages
