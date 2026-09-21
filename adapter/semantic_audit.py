#!/usr/bin/env python3
"""Phase J audit — deterministic checks plus per-claim Jev support
evaluation (spec §16). PASS is the only publishable verdict; anything
unverifiable degrades to NEEDS_REVIEW or PENDING, never silent PASS.
"""
import semantic_jev as jev


def audit_code(bundle: dict, facts: list, summary: dict) -> list:
    """Deterministic checks: reference integrity, span equality,
    structural enums, fact->claim coverage. Returns findings list —
    [] means the code side found no blocker (model check still runs)."""
    findings = []
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
                 deadline: float) -> tuple[list, bool]:
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
            "text does NOT support the claim. Judge target, value, "
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
        # the claim's support is judged against the quote PLUS its
        # surrounding原文 window — a bare quote cannot reveal a
        # negation or condition sitting next to it (§16.2, AT-028)
        if type(s) is int and type(e) is int and 0 <= s < e <= len(body):
            win = body[max(0, s - 250):min(len(body), e + 250)]
        else:
            win = ""
        ev_ctx[ev["evidence_id"]] = (quote, win)
    for c in claims:
        ctx = []
        for ev in c["evidence_refs"]:
            quote, win = ev_ctx.get(ev, ("", ""))
            ctx.append({"id": ev, "role": "evidence_quote",
                        "text": quote})
            if win and win != quote:
                ctx.append({"id": f"{ev}_ctx",
                            "role": "evidence_context", "text": win})
        state = {"target": {"id": c["claim_id"], "text": c["text"]},
                 "context": ctx}
        try:
            out = jev_client.evaluate(
                state, {c["claim_id"]: questions[c["claim_id"]]},
                deadline)
        except jev.JevError:
            return findings + [{"code": "support_unevaluated",
                                "claim": c["claim_id"]}], False
        ans = out["answers"][c["claim_id"]]
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
    if not evaluated:
        return "PENDING"
    blocking = [f for f in code_findings
                if f["code"] in ("evidence_missing",
                                 "evidence_revision_mismatch",
                                 "evidence_span_mismatch",
                                 "input_oversize")]
    if blocking:
        return "NEEDS_REVIEW"
    repairable = [f for f in code_findings + jev_findings
                  if f["code"] in ("fact_dropped", "claim_not_supported",
                                   "quantity_untraced",
                                   "claim_without_evidence")]
    if repairable and not repaired:
        return "REPAIR_REQUIRED"
    if repairable or [f for f in jev_findings
                      if f["code"] in ("claim_contradicts",
                                       "claim_ambiguous")]:
        return "NEEDS_REVIEW"
    return "PASS"
