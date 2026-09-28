"""Canonical ``semantic-facts/v2`` -> legacy read-model projection.

The canonical document is the source of truth; downstream consumers
still speak the legacy fact shape during migration.  The projection is
one-way and explicitly lossy: fields that have no v2 analogue stay
None rather than being invented. Categories with no legacy slot
(allergy, adverse events, vitals, preferences, observations) remain
in ``canonical_facts`` with their evidence and validation state.
Only VERIFIED facts project — unverified work never surfaces.
"""
from __future__ import annotations

# ---------------------------------------------------------------------

_V2_TO_V1_KIND = {
    "medication_event": "medication_event",
    "medication_exposure": "medication_event",
    "allergy_intolerance": "observation",
    "adverse_drug_event": "observation",
    "adherence_administration": "medication_event",
    "symptom_state": "symptom",
    "vital_lab": "observation",
    "care_event": "schedule",
    "request_pending": "explicit_request",
    "preference": "preference",
    "other_observation": "observation",
}
_V2_TO_V1_STATUS = {
    "considering": "considered", "planned": "planned",
    "ordered": "order_reported", "reported": "execution_reported",
    "in_progress": "execution_reported", "performed": "execution_reported",
    "done": "execution_reported", "cancelled": "cancelled",
}


def _v2_drug_ref(statement: str) -> str | None:
    """Best-effort entity token for the legacy ``drug_ref`` slot —
    longest katakana/alphanumeric run, deterministic tie-break."""
    import semantic_relations as sr
    tokens = sr._entity_tokens(statement)
    if not tokens:
        return None
    return sorted(tokens, key=lambda t: (-len(t), t))[0]


def project_v2_facts(doc: dict) -> list:
    """Project a validated ``semantic-facts/v2`` document into the
    legacy fact shape consumed by assessment/loops/render.

    Facts without verified evidence keep ``evidence_refs`` empty and
    ``validation_status`` ``unverified`` — projection never upgrades.
    """
    def _mid(v):
        # v2 doc JSON carries message ids as strings; bundle members key
        # on ints — a string id makes audit lookup fail on good evidence
        # (C01). Normalize at the projection boundary.
        try:
            return int(v)
        except (TypeError, ValueError):
            return v
    evidence_by_id = {e["evidence_id"]: e
                      for e in doc.get("evidence", [])
                      if isinstance(e, dict) and e.get("evidence_id")}
    message_id = _mid(doc.get("source", {}).get("message_id", ""))
    revision = doc.get("source", {}).get("revision", "")
    out = []
    for fact in doc.get("facts", []):
        if not isinstance(fact, dict):
            continue
        ev_ids = [ref for ref in fact.get("evidence_ids", [])
                  if ref in evidence_by_id]
        ev = evidence_by_id[ev_ids[0]] if ev_ids else None
        event_time = fact.get("event_time", "unknown")
        occurred = event_time if isinstance(event_time, str) \
            and event_time[:1].isdigit() else None
        legacy_ev = {
            "evidence_id": ev["evidence_id"],
            "message_id": _mid(ev.get("message_id", message_id)),
            "revision_id": ev.get("revision", revision),
            "start_codepoint": ev["start"],
            "end_codepoint": ev["end"],
            "quote": ev["quote"],
            "atom_id": ev.get("atom_id"),
        } if ev else None
        out.append({
            "fact_id": fact["fact_id"],
            "kind": _V2_TO_V1_KIND.get(fact.get("kind"), "other"),
            "statement": fact["statement"],
            "subject_ref": fact.get("subject", "unknown"),
            "drug_ref": _v2_drug_ref(fact["statement"])
                        if _V2_TO_V1_KIND.get(fact.get("kind"))
                        == "medication_event" else None,
            "status": _V2_TO_V1_STATUS.get(fact.get("workflow_status"),
                                           "not_stated"),
            "polarity": fact.get("polarity")
                        if fact.get("polarity") in
                        ("affirmed", "negated", "uncertain")
                        else "uncertain",
            "occurred_at": occurred,
            "time_text": None if event_time in (None, "unknown")
                         else event_time,
            "quantity": fact.get("quantity")
                        if isinstance(fact.get("quantity"), str)
                        and fact.get("quantity") != "unknown" else None,
            "evidence_refs": ev_ids,
            "validation_status": fact.get("validation_status",
                                          "unverified"),
            "_evidence": legacy_ev,
            "_v2_kind": fact.get("kind"),
            "_v2_provenance": fact.get("provenance"),
            "_v2_importance": fact.get("importance"),
        })
    return out


# ---------------------------------------------------------------------
# v2 -> extract_llm content-shape projection (T12 consumer migration)
# ---------------------------------------------------------------------

_V2_TO_LLM_ACTION = {
    "start": "start", "restart": "start", "stop": "stop",
    "cancelled": "stop", "increase": "increase",
    "decrease": "decrease", "change": "change",
}


def _v2_llm_subject(subject: str) -> str:
    """Canonical subject identity -> legacy med ``subject`` slot.
    Only ``patient:*`` maps to ``patient`` and ``role:family`` to
    ``family``; named persons (``person:*``), senders, staff, and
    unknown references all map to ``other`` — the legacy slot cannot
    represent them and projection stays honest."""
    if isinstance(subject, str) and subject.startswith("patient:"):
        return "patient"
    if isinstance(subject, str) and subject == "role:family":
        return "family"
    return "other"
_CARE_EVENT_KEYWORDS = [
    ("discharge", ("退院", "退院予定")), ("transfer", ("転院", "転棟")),
    ("admission", ("入院",)), ("fall", ("転倒", "転落")),
    ("eol", ("看取り", "終末期")), ("exam", ("検査", "採血", "画像")),
    ("family_contact", ("家族連絡", "ご家族へ連絡", "家族へ連絡")),
    ("visit", ("訪問", "往診")),
]


def _v2_med_status(fact: dict) -> str:
    action = fact.get("action")
    workflow = fact.get("workflow_status")
    if action in ("stop", "cancelled") \
            and workflow in ("performed", "done", "reported"):
        return "past"
    if workflow in ("considering", "planned", "ordered", "pending",
                    "on_hold"):
        return "planned"
    return "current"


def project_v2_doc_legacy(doc: dict) -> dict:
    """Project an audited semantic-facts/v2 document into the
    extract_llm content shape consumed by the read side.  Only
    ``verified`` facts project; quotes come from the document's own
    evidence records."""
    evidence_by_id = {e["evidence_id"]: e
                      for e in doc.get("evidence", [])
                      if isinstance(e, dict) and e.get("evidence_id")}

    def quote_of(fact):
        for ref in fact.get("evidence_ids", []):
            ev = evidence_by_id.get(ref)
            if ev is not None:
                return ev["quote"]
        return None

    out: dict = {}
    for fact in doc.get("facts", []):
        if not isinstance(fact, dict) \
                or fact.get("validation_status") != "verified":
            continue
        kind = fact.get("kind")
        quote = quote_of(fact)
        negated = fact.get("polarity") == "negated"
        uncertain = (fact.get("polarity") not in ("affirmed", "negated")
                     or fact.get("epistemic") not in ("asserted", "reported")
                     or fact.get("workflow_status") in (None, "unknown"))
        if kind in ("medication_event", "medication_exposure",
                    "adherence_administration"):
            name = _v2_drug_ref(fact.get("statement") or "")
            if not name:
                name = "処方薬"
            quantity = fact.get("quantity")
            item = {"name": name,
                    "dose": quantity
                    if isinstance(quantity, str)
                    and quantity != "unknown" else None,
                    "action": _V2_TO_LLM_ACTION.get(fact.get("action"),
                                                   "none"),
                    "status": _v2_med_status(fact),
                    "subject": _v2_llm_subject(fact.get("subject")),
                    "negated": negated}
            if uncertain:
                item["unverified"] = True
            if quote:
                item["evidence"] = quote
            out.setdefault("meds", []).append(item)
        elif kind == "symptom_state":
            workflow = fact.get("workflow_status")
            status = "resolved" if workflow in ("done", "cancelled") \
                else "new" if workflow == "reported" else "ongoing"
            item = {"text": fact.get("statement") or "",
                    "negated": negated, "status": status,
                    "subject": _v2_llm_subject(fact.get("subject"))}
            if uncertain:
                item["unverified"] = True
            if quote:
                item["evidence"] = quote
            out.setdefault("symptoms", []).append(item)
        elif kind == "care_event":
            # Legacy events have no subject, uncertainty, or plan fields.
            # Their consumers count completed patient care transitions.
            if fact.get("polarity") != "affirmed" \
                    or fact.get("epistemic") not in ("asserted", "reported") \
                    or _v2_llm_subject(fact.get("subject")) != "patient" \
                    or fact.get("workflow_status") not in ("performed", "done"):
                continue
            statement = fact.get("statement") or ""
            for event, needles in _CARE_EVENT_KEYWORDS:
                if any(n in statement for n in needles):
                    out.setdefault("events", [])
                    if event not in out["events"]:
                        out["events"].append(event)
                    break
        elif kind == "request_pending":
            due = fact.get("event_time")
            due = due if isinstance(due, str) and due[:1].isdigit() \
                else None
            out.setdefault("requests", []).append(
                {"to": "不明", "from": None,
                 "action": (fact.get("statement") or "")[:15],
                 "due": due})
    _carry_canonical(doc, out, evidence_by_id)
    return out


# Canonical carriers — the legacy slots above cannot represent every
# verified fact (allergy, adverse events, vitals, preferences,
# observations have no slot).  ``canonical_facts`` carries every
# verified fact verbatim with its ids/evidence so no reader ever loses
# a canonical fact to a missing slot; ``canonical_relations`` keeps the
# doc's fact graph restricted to carried endpoints; ``canonical_quality``
# keeps the coverage state visible next to the facts.


def _carry_canonical(doc: dict, out: dict, evidence_by_id: dict) -> None:
    carried: list = []
    seen: set = set()
    for fact in doc.get("facts", []):
        if not isinstance(fact, dict) \
                or fact.get("validation_status") != "verified" \
                or not fact.get("fact_id"):
            continue
        quote = None
        bound = []
        for ref in fact.get("evidence_ids", []):
            ev = evidence_by_id.get(ref)
            if ev is not None:
                bound.append(ref)
                if quote is None:
                    quote = ev.get("quote")
        carried.append({
            "fact_id": fact["fact_id"],
            "validation_status": fact["validation_status"],
            "kind": fact.get("kind"),
            "statement": fact.get("statement") or "",
            "subject": fact.get("subject"),
            "polarity": fact.get("polarity"),
            "epistemic": fact.get("epistemic"),
            "workflow_status": fact.get("workflow_status"),
            "event_time": fact.get("event_time"),
            "importance": fact.get("importance"),
            "evidence_ids": bound,
            "evidence_quote": quote})
        seen.add(fact["fact_id"])
    out["canonical_facts"] = carried
    out["canonical_relations"] = [
        rel for rel in doc.get("relations", [])
        if isinstance(rel, dict)
        and rel.get("left_fact_id") in seen
        and rel.get("right_fact_id") in seen]
    coverage = doc.get("coverage", {})
    coverage = coverage if isinstance(coverage, dict) else {}
    out["canonical_quality"] = {
        "coverage_status": coverage.get("status"),
        "open_obligation_ids": coverage.get("open_obligation_ids") or [],
        "limitations": coverage.get("limitations") or [],
        "source_fingerprint":
            (doc.get("source") or {}).get("source_fingerprint")}
