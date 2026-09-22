"""Semantic audit gates (spec §16): code-level checks, per-claim Jev
support evaluation, and the audit-status decision tree.

Leaf module — deliberately independent of the semantic facade so the
audit logic can be exercised without the drain machinery."""
from __future__ import annotations

import json

import semantic_jev as jev
from semantic_quantities import claim_quantity_findings

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
    if jev_client is None:
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
            try:
                jev_client.last_error = error
            except Exception:
                pass
            return findings + [{"code": "support_unevaluated",
                                "claim": c["claim_id"]}], False
        ans = out["answers"][c["claim_id"]]
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
