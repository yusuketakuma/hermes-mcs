"""Durable medication-event detail assessment.

The primary propositions describe a message as a whole.  Medication detail
is different: one message can report several medications with different
statuses or polarities.  This module asks the detail questions once per
extracted medication fact and stores each successful answer as an existing
``semantic_assess`` artifact, so a later attempt resumes at the first missing
fact/dimension.
"""
from __future__ import annotations

import json
import time


KIND_ASSESS = "semantic_assess"
SCHEMA_VERSION = "semantic-assessment/v2"


def _result(details: dict, findings: list, complete: bool,
            failure_reason: str | None = None) -> dict:
    return {"details": details, "complete": complete,
            "findings": findings, "failure_reason": failure_reason}


def _finite_confidence(value) -> float | None:
    if type(value) is bool or not isinstance(value, int | float):
        return None
    return float(value) if 0.0 <= value <= 1.0 else None


def _policy_fingerprint(scfg: dict) -> str | None:
    supplied = scfg.get("policy_fingerprint")
    if isinstance(supplied, str) and supplied:
        return supplied
    import semantic
    return semantic.policy_fingerprint(scfg)


def _parse_json(value):
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return None


def _cached_detail(ledger, project_id, target_id, source_fp, policy_fp,
                   fact_id, dimension, options: dict) -> dict | None:
    if ledger is None:
        return None
    rows = ledger.artifacts(KIND_ASSESS, project_id=project_id,
                            message_id=target_id)
    for row in reversed(rows):
        meta = _parse_json(row["meta"])
        if not isinstance(meta, dict):
            continue
        row_source = meta.get("source_fingerprint", meta.get("fingerprint"))
        if (meta.get("schema") != SCHEMA_VERSION
                or row_source != source_fp
                or meta.get("policy_fingerprint") != policy_fp
                or meta.get("fact_id") != fact_id
                or meta.get("dimension") != dimension
                or meta.get("technical_status") != "complete"):
            continue
        content = _parse_json(row["content"])
        if not isinstance(content, dict):
            continue
        choice = content.get("choice")
        confidence = _finite_confidence(content.get("confidence"))
        if not isinstance(choice, str) or choice not in options \
                or confidence is None:
            continue
        return {"choice": choice, "confidence": confidence}
    return None


def _persist_detail(ledger, project_id, target_id, source_fp, policy_fp,
                    fact_id, dimension, choice, confidence) -> None:
    if ledger is None:
        return
    content = {
        "schema": SCHEMA_VERSION,
        "target_message_id": target_id,
        "fact_id": fact_id,
        "dimension": dimension,
        "choice": choice,
        "confidence": confidence,
    }
    meta = {
        "schema": SCHEMA_VERSION,
        "technical_status": "complete",
        "fingerprint": source_fp,
        "source_fingerprint": source_fp,
        "policy_fingerprint": policy_fp,
        "fact_id": fact_id,
        "dimension": dimension,
    }
    import semantic_jev as jev
    ledger.artifact_add(
        KIND_ASSESS, json.dumps(content, ensure_ascii=False),
        project_id=project_id, message_id=target_id, model=jev.JEV_MODEL,
        meta=meta)


def _member_context(bundle: dict) -> tuple[list, dict[int, dict]]:
    members = bundle.get("members")
    if not isinstance(members, list):
        raise ValueError("bundle_members_invalid")
    context = []
    by_id = {}
    for member in members:
        if not isinstance(member, dict):
            raise ValueError("bundle_member_invalid")
        message_id = member.get("message_id")
        body = member.get("body_original")
        if message_id is None or not isinstance(body, str):
            raise ValueError("bundle_member_source_invalid")
        if message_id in by_id:
            raise ValueError("bundle_member_duplicate")
        by_id[message_id] = member
        context.append({
            "id": f"m{message_id}",
            "role": "context",
            "message_id": message_id,
            "posted_at": member.get("posted_at", ""),
            "sender": member.get("sender", {}),
            "text": body,
        })
    return context, by_id


def _event_state(bundle: dict, target_id, fact: dict,
                 context: list, members: dict[int, dict]) -> tuple[dict, str | None]:
    fact_id = fact.get("fact_id")
    statement = fact.get("statement")
    evidence = fact.get("_evidence")
    if not isinstance(fact_id, str) or not fact_id:
        return {}, "medication_detail_fact_invalid"
    if not isinstance(statement, str) or not statement.strip():
        return {}, "medication_detail_fact_invalid"
    if not isinstance(evidence, dict):
        return {}, "medication_detail_evidence_missing"
    evidence_id = evidence.get("evidence_id")
    message_id = evidence.get("message_id")
    quote = evidence.get("quote")
    start = evidence.get("start_codepoint")
    end = evidence.get("end_codepoint")
    source = members.get(message_id)
    revision = evidence.get("revision_id", "")
    if (not isinstance(evidence_id, str) or not evidence_id
            or source is None or not isinstance(quote, str)
            or revision != source.get("revision", "")
            or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(source["body_original"])
            or source["body_original"][start:end] != quote):
        return {}, "medication_detail_evidence_invalid"

    evidence_entry = {
        "id": evidence_id,
        "role": "evidence_quote",
        "message_id": message_id,
        "revision_id": revision,
        "start_codepoint": start,
        "end_codepoint": end,
        "text": quote,
    }
    state_context = [*context, evidence_entry]
    drug = next((fact.get(name) for name in
                 ("drug_ref", "drug", "drug_name", "medication")
                 if isinstance(fact.get(name), str) and fact.get(name)), None)
    target = {
        "id": f"fact:{fact_id}",
        "role": "target",
        "text": statement.strip(),
        "fact_id": fact_id,
        "drug": drug,
        "drug_ref": fact.get("drug_ref"),
        "time_text": fact.get("time_text"),
        "status": fact.get("status"),
        "polarity": fact.get("polarity"),
        "evidence": {
            "id": evidence_id,
            "message_id": message_id,
            "start_codepoint": start,
            "end_codepoint": end,
            "text": quote,
        },
    }
    return {"target": target, "context": state_context}, None


def _append_finding(findings: list, code: str, fact_id=None,
                    dimension=None, **extra) -> None:
    item = {"code": code}
    if fact_id is not None:
        item["fact_id"] = fact_id
    if dimension is not None:
        item["dimension"] = dimension
    item.update(extra)
    findings.append(item)


def evaluate_medication_events(ledger, bundle: dict, target_id, facts: list,
                               jev_client, scfg: dict,
                               deadline: float | None) -> dict:
    """Evaluate every extracted medication event independently.

    A complete detail result is durable at ``(source fingerprint, policy
    fingerprint, fact_id, dimension)``.  The primary message-level
    proposition is intentionally not consulted: an extracted medication fact
    remains eligible even when a broad primary result was ``NO_MATCH``.
    Runtime guard errors are control-flow signals and are re-raised for the
    worker; Jev/model failures return an incomplete result.
    """
    import semantic_jev as jev
    from semantic_runtime import RuntimeGuardError

    details = {}
    findings = []
    if not isinstance(bundle, dict) or not isinstance(facts, list):
        return _result(details, [{"code": "medication_detail_input_invalid"}],
                       False, "invalid")
    if not isinstance(scfg, dict):
        return _result(details, [{"code": "medication_detail_input_invalid"}],
                       False, "invalid")
    source_fp = bundle.get("source_fingerprint")
    project_id = bundle.get("project_id")
    policy_fp = _policy_fingerprint(scfg)
    if not isinstance(source_fp, str) or not source_fp or not policy_fp:
        return _result(details, [{"code": "medication_detail_fingerprint_invalid"}],
                       False, "invalid")
    threshold = scfg.get("match_threshold", jev.MATCH_THRESHOLD)
    if (_finite_confidence(threshold) is None):
        return _result(details, [{"code": "medication_detail_threshold_invalid"}],
                       False, "invalid")
    threshold = float(threshold)

    try:
        context, members = _member_context(bundle)
    except ValueError as error:
        return _result(details, [{"code": str(error)}], False, "invalid")
    if target_id not in members:
        return _result(details, [{"code": "medication_detail_target_invalid"}],
                       False, "invalid")

    events = []
    seen_ids = set()
    for fact in facts:
        if not isinstance(fact, dict) or fact.get("kind") != "medication_event":
            continue
        fact_id = fact.get("fact_id")
        if not isinstance(fact_id, str) or not fact_id or fact_id in seen_ids:
            _append_finding(findings, "medication_detail_fact_invalid", fact_id)
            continue
        seen_ids.add(fact_id)
        events.append(fact)

    if findings:
        return _result(details, findings, False, "invalid")
    if not events:
        return _result(details, findings, True)
    if jev_client is None:
        _append_finding(findings, "medication_detail_technical_error")
        return _result(details, findings, False, "technical")

    for fact in events:
        fact_id = fact["fact_id"]
        state, state_error = _event_state(bundle, target_id, fact,
                                          context, members)
        if state_error:
            _append_finding(findings, state_error, fact_id)
            return _result(details, findings, False, "invalid")
        details.setdefault(fact_id, {})
        for dimension, (instructions, options) in \
                jev.MED_DETAIL_QUESTIONS.items():
            cached = _cached_detail(
                ledger, project_id, target_id, source_fp, policy_fp,
                fact_id, dimension, options)
            if cached is not None:
                choice = cached["choice"]
                confidence = cached["confidence"]
            else:
                if deadline is not None and time.monotonic() > deadline:
                    _append_finding(findings, "medication_detail_deadline",
                                    fact_id, dimension)
                    return _result(details, findings, False, "deadline")
                try:
                    question = jev.choice_question(
                        instructions + " Determine this from the original evidence "
                        "quote and same-thread context. The target statement and "
                        "its status/polarity are unverified extraction candidates, "
                        "not authoritative source facts.", options)
                    response = jev_client.evaluate(
                        state, {dimension: question}, deadline)
                except RuntimeGuardError:
                    raise
                except Exception as error:
                    finding = {"code": "medication_detail_technical_error",
                               "fact_id": fact_id, "dimension": dimension}
                    if isinstance(error, jev.JevError):
                        finding["kind"] = error.kind
                    findings.append(finding)
                    return _result(details, findings, False, "technical")
                if deadline is not None and time.monotonic() > deadline:
                    _append_finding(findings, "medication_detail_deadline",
                                    fact_id, dimension)
                    return _result(details, findings, False, "deadline")
                try:
                    answer = response["answers"][dimension]
                    choice = answer["choice"]
                    confidence = _finite_confidence(answer["confidence"])
                except (KeyError, TypeError):
                    choice, confidence = None, None
                if (not isinstance(choice, str) or choice not in options
                        or confidence is None):
                    _append_finding(findings,
                                    "medication_detail_technical_error",
                                    fact_id, dimension, kind="protocol_error")
                    return _result(details, findings, False, "technical")
                _persist_detail(ledger, project_id, target_id, source_fp,
                                policy_fp, fact_id, dimension, choice,
                                confidence)

            details[fact_id][dimension] = choice
            if (confidence >= threshold and dimension in ("status", "polarity")
                    and fact.get(dimension) != choice):
                _append_finding(findings, "medication_detail_mismatch",
                                fact_id, dimension, extracted=fact.get(dimension),
                                assessed=choice)
            if confidence < threshold:
                _append_finding(findings, "medication_detail_low_confidence",
                                fact_id, dimension, confidence=confidence)

    return _result(details, findings, True)
