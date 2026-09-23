"""Canonical ``semantic-facts/v2`` contract: enums, identity derivation,
and validators.

Leaf module for the canonical evidence-backed fact layer.  It imports the
stdlib and ``mcs_requests.payload_hash`` only; every canonical semantic
stage shares these enums and stable IDs so a restart or a changed chunk
layout never produces a different identity for the same source
statement.

Importance (``T0``–``T3``/``unknown``) is ordering and review-priority
metadata only: a lower tier must never remove, hide, or downgrade a
fact.  A mandatory-rubric fact is either represented with verified
evidence or the generation is explicitly non-PASS — no silent omission.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata

from mcs_requests import payload_hash

CONTRACT_VERSION = "semantic-facts/v2"

CANDIDATE_STATUSES = ("VALIDATED", "PENDING", "NEEDS_REVIEW", "STALE")
FACTS_STATUSES = ("VERIFIED", "PENDING", "NEEDS_REVIEW", "STALE")
PUBLICATION_STATUSES = ("PASS", "PENDING", "NEEDS_REVIEW", "STALE")

ATOM_KINDS = ("clause", "list_item", "table_row", "heading", "attachment_ref")
IMPORTANCE_TIERS = ("T0", "T1", "T2", "T3", "unknown")
IMPORTANCE_RANK = {tier: index for index, tier in enumerate(IMPORTANCE_TIERS)}

FACT_KINDS = (
    "medication_event", "medication_exposure", "allergy_intolerance",
    "adverse_drug_event", "adherence_administration", "symptom_state",
    "vital_lab", "care_event", "request_pending", "preference",
    "other_observation",
)

# Mandatory pharmacist-rubric categories.  Extraction must produce an
# obligation for every category, and ``explicit_no_fact`` is the only way
# an obligation closes without a fact.
MANDATORY_CATEGORIES = (
    "medication", "allergy_intolerance", "adverse_drug_event",
    "adherence_administration", "symptom_state", "vital_lab",
    "care_event", "request_pending", "preference", "other_observation",
)

# Fact kind -> mandatory category.  Single source of truth for the
# taxonomy: extraction links obligations, coverage counts, rendering,
# and projections all read this map.
KIND_CATEGORY = {
    "medication_event": "medication",
    "medication_exposure": "medication",
    "allergy_intolerance": "allergy_intolerance",
    "adverse_drug_event": "adverse_drug_event",
    "adherence_administration": "adherence_administration",
    "symptom_state": "symptom_state",
    "vital_lab": "vital_lab",
    "care_event": "care_event",
    "request_pending": "request_pending",
    "preference": "preference",
    "other_observation": "other_observation",
}

POLARITIES = ("affirmed", "negated", "uncertain", "unknown")
EPISTEMICS = ("asserted", "reported", "speculated", "unknown")
WORKFLOW_STATUSES = (
    "reported", "ordered", "planned", "considering", "in_progress",
    "performed", "done", "cancelled", "on_hold", "pending", "unknown",
)
MED_ACTIONS = (
    "start", "stop", "increase", "decrease", "change", "hold",
    "restart", "continue", "unchanged", "consider", "planned",
    "ordered", "administered", "cancelled", "unknown",
)

OBLIGATION_SOURCES = ("deterministic", "jev_pre")
OBLIGATION_STATUSES = ("open", "covered", "explicit_no_fact",
                       "ambiguous", "failed")

# Priority order: reconcile reports the first type that holds.
RELATION_TYPES = (
    "EXACT_DUPLICATE", "EXPLICIT_SUPERSESSION", "TRANSITION",
    "CONTRADICTION", "COMPLEMENTS", "UNRESOLVED",
)

CONTENT_QUALITIES = ("full", "partial")

UNKNOWN = "unknown"
NOT_STATED = "not_stated"

_ID_RE = re.compile(r"^[a-z]+_[0-9a-f]{16}$")


class ContractError(ValueError):
    """A semantic-facts/v2 document violated the contract."""


def _fail(reason: str) -> None:
    raise ContractError(f"contract:{reason}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _id_value(value, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        _fail(f"{field}_invalid")
    value = str(value).strip()
    if not value:
        _fail(f"{field}_missing")
    return value


def _enum(value, field: str, options) -> str:
    if not isinstance(value, str) or value not in options:
        _fail(f"{field}_invalid")
    return value


def _int_range(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{field}_invalid")
    return value


def _str_list(value, field: str) -> list:
    if not isinstance(value, list):
        _fail(f"{field}_list_required")
    return [_id_value(item, field) for item in value]


def _normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


def source_fingerprint(message_id, revision, content_hash,
                       body_codepoints, content_quality,
                       attachments_complete) -> str:
    """Bind a generation to an exact source snapshot."""
    _id_value(message_id, "message_id")
    _id_value(revision, "revision")
    _id_value(content_hash, "content_hash")
    _int_range(body_codepoints, "body_codepoints")
    _enum(content_quality, "content_quality", CONTENT_QUALITIES)
    if type(attachments_complete) is not bool:
        _fail("attachments_complete_invalid")
    return "sf_" + payload_hash({
        "message_id": str(message_id), "revision": str(revision),
        "content_hash": str(content_hash),
        "body_codepoints": body_codepoints,
        "content_quality": content_quality,
        "attachments_complete": attachments_complete,
    })[:16]


def atom_id(source_fp: str, index: int) -> str:
    _id_value(source_fp, "source_fingerprint")
    _int_range(index, "atom_index")
    return "atom_" + payload_hash({"a": source_fp, "i": index})[:16]


def chunk_id(source_fp: str, index: int) -> str:
    _id_value(source_fp, "source_fingerprint")
    _int_range(index, "chunk_index")
    return "chk_" + payload_hash({"c": source_fp, "i": index})[:16]


def obligation_id(owner_id: str, category: str, source: str) -> str:
    _id_value(owner_id, "obligation_owner")
    _enum(category, "obligation_category", MANDATORY_CATEGORIES)
    _enum(source, "obligation_source", OBLIGATION_SOURCES)
    return "obl_" + payload_hash(
        {"o": owner_id, "c": category, "s": source})[:16]


def evidence_id(message_id, revision, start: int, end: int,
                quote: str) -> str:
    _id_value(message_id, "message_id")
    _id_value(revision, "revision")
    _int_range(start, "evidence_start")
    _int_range(end, "evidence_end")
    if end < start:
        _fail("evidence_range_invalid")
    return "ev_" + payload_hash({
        "m": str(message_id), "r": str(revision),
        "s": start, "e": end, "q": _sha256(str(quote)),
    })[:16]


def fact_id(project_id, message_id, revision, kind, subject,
            statement, evidence_ids) -> str:
    """Derive a stable fact ID from source identity only.

    Chunk IDs are deliberately excluded: the same statement must keep
    the same fact ID across retries and across chunk layouts.  Two
    observations sharing evidence still differ when the statement or
    subject differs.
    """
    _id_value(project_id, "project_id")
    _id_value(message_id, "message_id")
    _id_value(revision, "revision")
    _enum(kind, "fact_kind", FACT_KINDS)
    _id_value(subject, "fact_subject")
    _id_value(statement, "fact_statement")
    ev = sorted(_str_list(list(evidence_ids), "fact_evidence_ids"))
    return "fact_" + payload_hash({
        "p": str(project_id), "m": str(message_id), "r": str(revision),
        "k": kind, "u": subject, "t": statement, "e": ev,
    })[:16]


def relation_id(left_fact_id: str, right_fact_id: str,
                relation_type: str) -> str:
    """Ordered relation identity: left is the superseded/earlier fact."""
    _id_value(left_fact_id, "relation_left")
    _id_value(right_fact_id, "relation_right")
    _enum(relation_type, "relation_type", RELATION_TYPES)
    return "rel_" + payload_hash(
        {"l": left_fact_id, "r": right_fact_id,
         "t": relation_type})[:16]


def subject_identity(project_id, *, sender_id=None, role=None,
                     name=None) -> str:
    """Project-scoped identity that never invents a patient mapping.

    ``patient:{project_id}`` for the patient; ``sender:{sender_id}`` for
    an identified author; ``person:<hash>`` for a named project-scoped
    person; ``role:<role>`` for a generic, non-linkable reference;
    ``unknown`` when nothing is stated.
    """
    _id_value(project_id, "project_id")
    if role == "patient":
        return f"patient:{project_id}"
    if sender_id is not None:
        return f"sender:{_id_value(sender_id, 'sender_id')}"
    if role is not None and name is not None:
        digest = _sha256(
            f"{project_id}:{_normalize_name(role)}:{_normalize_name(name)}")
        return f"person:{digest[:16]}"
    if role is not None:
        return f"role:{_normalize_name(role)}"
    return UNKNOWN


def importance_rank(tier: str) -> int:
    """Ordering key only — importance never filters a fact out."""
    _enum(tier, "importance", IMPORTANCE_TIERS)
    return IMPORTANCE_RANK[tier]


def validate_source_binding(binding: dict) -> dict:
    if not isinstance(binding, dict):
        _fail("source_binding_object_required")
    return {
        "message_id": _id_value(binding.get("message_id"), "message_id"),
        "revision": _id_value(binding.get("revision"), "revision"),
        "content_hash": _id_value(binding.get("content_hash"),
                                  "content_hash"),
        "body_codepoints": _int_range(binding.get("body_codepoints"),
                                      "body_codepoints"),
        "content_quality": _enum(binding.get("content_quality"),
                                 "content_quality", CONTENT_QUALITIES),
        "attachments_complete": (
            binding["attachments_complete"]
            if type(binding.get("attachments_complete")) is bool
            else _fail("attachments_complete_invalid")),
        "source_fingerprint": _id_value(
            binding.get("source_fingerprint"), "source_fingerprint"),
    }


def validate_atom(atom: dict) -> dict:
    if not isinstance(atom, dict):
        _fail("atom_object_required")
    start = _int_range(atom.get("start"), "atom_start")
    end = _int_range(atom.get("end"), "atom_end")
    if end <= start:
        _fail("atom_range_empty")
    deps = _str_list(list(atom.get("dependency_atom_ids", [])),
                     "atom_dependency_atom_ids")
    out = {
        "atom_id": _id_value(atom.get("atom_id"), "atom_id"),
        "kind": _enum(atom.get("kind"), "atom_kind", ATOM_KINDS),
        "start": start, "end": end,
        "text_hash": _id_value(atom.get("text_hash"), "atom_text_hash"),
        "section_path": _str_list(
            list(atom.get("section_path", [])), "atom_section_path"),
        "dependency_atom_ids": deps,
        "importance": _enum(atom.get("importance", "unknown"),
                            "atom_importance", IMPORTANCE_TIERS),
    }
    if out["atom_id"] in deps:
        _fail("atom_dependency_self")
    return out


def validate_chunk(chunk: dict) -> dict:
    if not isinstance(chunk, dict):
        _fail("chunk_object_required")
    core = _str_list(list(chunk.get("core_atom_ids", [])),
                     "chunk_core_atom_ids")
    if not core:
        _fail("chunk_core_empty")
    return {
        "chunk_id": _id_value(chunk.get("chunk_id"), "chunk_id"),
        "core_atom_ids": core,
        "context_atom_ids": _str_list(
            list(chunk.get("context_atom_ids", [])),
            "chunk_context_atom_ids"),
        "dependency_atom_ids": _str_list(
            list(chunk.get("dependency_atom_ids", [])),
            "chunk_dependency_atom_ids"),
        "status": _id_value(chunk.get("status", "pending"),
                            "chunk_status"),
    }


def validate_obligation(obligation: dict) -> dict:
    if not isinstance(obligation, dict):
        _fail("obligation_object_required")
    out = {
        "obligation_id": _id_value(obligation.get("obligation_id"),
                                   "obligation_id"),
        "owner_id": _id_value(obligation.get("owner_id"),
                              "obligation_owner"),
        "category": _enum(obligation.get("category"),
                          "obligation_category", MANDATORY_CATEGORIES),
        "source": _enum(obligation.get("source"), "obligation_source",
                        OBLIGATION_SOURCES),
        "importance": _enum(obligation.get("importance", "unknown"),
                            "obligation_importance", IMPORTANCE_TIERS),
        "status": _enum(obligation.get("status"), "obligation_status",
                        OBLIGATION_STATUSES),
        "fact_ids": _str_list(list(obligation.get("fact_ids", [])),
                              "obligation_fact_ids"),
    }
    if out["status"] == "covered" and not out["fact_ids"]:
        _fail("obligation_covered_without_fact")
    confidence = obligation.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(
                confidence, (int, float)) \
                or not math.isfinite(float(confidence)) \
                or not 0.0 <= float(confidence) <= 1.0:
            _fail("obligation_confidence_invalid")
        out["confidence"] = float(confidence)
    reason = obligation.get("reason")
    if reason is not None:
        out["reason"] = _id_value(reason, "obligation_reason")
    return out


def validate_evidence(evidence: dict) -> dict:
    if not isinstance(evidence, dict):
        _fail("evidence_object_required")
    start = _int_range(evidence.get("start"), "evidence_start")
    end = _int_range(evidence.get("end"), "evidence_end")
    if end <= start:
        _fail("evidence_range_empty")
    return {
        "evidence_id": _id_value(evidence.get("evidence_id"),
                                 "evidence_id"),
        "message_id": _id_value(evidence.get("message_id"),
                                "evidence_message_id"),
        "revision": _id_value(evidence.get("revision"),
                              "evidence_revision"),
        "start": start, "end": end,
        "quote": _id_value(evidence.get("quote"), "evidence_quote"),
        "atom_id": _id_value(evidence.get("atom_id"),
                             "evidence_atom_id"),
    }


def validate_fact(fact: dict) -> dict:
    if not isinstance(fact, dict):
        _fail("fact_object_required")
    out = {
        "fact_id": _id_value(fact.get("fact_id"), "fact_id"),
        "kind": _enum(fact.get("kind"), "fact_kind", FACT_KINDS),
        "subject": _id_value(fact.get("subject"), "fact_subject"),
        "actor": _id_value(fact.get("actor", UNKNOWN), "fact_actor"),
        "statement": _id_value(fact.get("statement"), "fact_statement"),
        "polarity": _enum(fact.get("polarity"), "fact_polarity",
                          POLARITIES),
        "epistemic": _enum(fact.get("epistemic", UNKNOWN),
                           "fact_epistemic", EPISTEMICS),
        "workflow_status": _enum(
            fact.get("workflow_status", UNKNOWN),
            "fact_workflow_status", WORKFLOW_STATUSES),
        "event_time": _id_value(fact.get("event_time", UNKNOWN),
                                "fact_event_time"),
        "valid_time": _id_value(fact.get("valid_time", UNKNOWN),
                                "fact_valid_time"),
        "evidence_ids": _str_list(list(fact.get("evidence_ids", [])),
                                  "fact_evidence_ids"),
        "obligation_ids": _str_list(list(fact.get("obligation_ids", [])),
                                    "fact_obligation_ids"),
        "importance": _enum(fact.get("importance", "unknown"),
                            "fact_importance", IMPORTANCE_TIERS),
        "quantity": _id_value(fact.get("quantity", UNKNOWN),
                              "fact_quantity"),
        "provenance": _id_value(fact.get("provenance", "local_llm"),
                                "fact_provenance"),
        "validation_status": _enum(
            fact.get("validation_status", "unverified"),
            "fact_validation_status",
            ("unverified", "verified", "rejected")),
    }
    if out["kind"] == "medication_event":
        _enum(fact.get("action", UNKNOWN), "fact_action", MED_ACTIONS)
        out["action"] = fact.get("action", UNKNOWN)
    return out


def validate_relation(relation: dict) -> dict:
    if not isinstance(relation, dict):
        _fail("relation_object_required")
    out = {
        "relation_id": _id_value(relation.get("relation_id"),
                                 "relation_id"),
        "left_fact_id": _id_value(relation.get("left_fact_id"),
                                  "relation_left"),
        "right_fact_id": _id_value(relation.get("right_fact_id"),
                                   "relation_right"),
        "type": _enum(relation.get("type"), "relation_type",
                      RELATION_TYPES),
        "evidence_ids": _str_list(list(relation.get("evidence_ids", [])),
                                  "relation_evidence_ids"),
        "status": _id_value(relation.get("status", "candidate"),
                            "relation_status"),
    }
    if out["left_fact_id"] == out["right_fact_id"]:
        _fail("relation_self_loop")
    confidence = relation.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(
                confidence, (int, float)) \
                or not math.isfinite(float(confidence)) \
                or not 0.0 <= float(confidence) <= 1.0:
            _fail("relation_confidence_invalid")
        out["confidence"] = float(confidence)
    reason = relation.get("reason")
    if reason is not None:
        out["reason"] = _id_value(reason, "relation_reason")
    return out


def validate_coverage(coverage: dict) -> dict:
    if not isinstance(coverage, dict):
        _fail("coverage_object_required")
    counts = coverage.get("category_counts")
    if not isinstance(counts, dict):
        _fail("coverage_category_counts_object_required")
    clean_counts = {}
    for category, count in counts.items():
        _enum(category, "coverage_category", MANDATORY_CATEGORIES)
        clean_counts[category] = _int_range(count, "coverage_count")
    limitations = coverage.get("limitations", [])
    if not isinstance(limitations, list):
        _fail("coverage_limitations_list_required")
    return {
        "category_counts": clean_counts,
        "open_obligation_ids": _str_list(
            list(coverage.get("open_obligation_ids", [])),
            "coverage_open_obligation_ids"),
        "limitations": [_id_value(item, "coverage_limitation")
                        for item in limitations],
        "status": _enum(coverage.get("status"), "coverage_status",
                        ("complete", "incomplete")),
    }


def validate_facts_doc(doc: dict) -> dict:
    """Validate a complete ``semantic-facts/v2`` artifact document.

    Cross-references are checked: fact evidence must exist, relations
    must reference known facts, and every covered obligation must name a
    real fact.  Range coverage across atoms/chunks is the extraction
    stage's job; this validator owns document-internal consistency.
    """
    if not isinstance(doc, dict):
        _fail("doc_object_required")
    if doc.get("version") != CONTRACT_VERSION:
        _fail("doc_version_mismatch")
    source = validate_source_binding(doc.get("source"))
    atoms = [validate_atom(a) for a in doc.get("atoms")] \
        if isinstance(doc.get("atoms"), list) \
        else _fail("doc_atoms_list_required")
    chunks = [validate_chunk(c) for c in doc.get("chunks")] \
        if isinstance(doc.get("chunks"), list) \
        else _fail("doc_chunks_list_required")
    obligations = [validate_obligation(o) for o in doc.get("obligations")] \
        if isinstance(doc.get("obligations"), list) \
        else _fail("doc_obligations_list_required")
    evidence = [validate_evidence(e) for e in doc.get("evidence")] \
        if isinstance(doc.get("evidence"), list) \
        else _fail("doc_evidence_list_required")
    facts = [validate_fact(f) for f in doc.get("facts")] \
        if isinstance(doc.get("facts"), list) \
        else _fail("doc_facts_list_required")
    relations = [validate_relation(r) for r in doc.get("relations")] \
        if isinstance(doc.get("relations"), list) \
        else _fail("doc_relations_list_required")
    coverage = validate_coverage(doc.get("coverage"))

    atom_ids = {a["atom_id"] for a in atoms}
    if len(atom_ids) != len(atoms):
        _fail("doc_atom_id_duplicate")
    chunk_ids = set()
    owned = set()
    for chunk in chunks:
        if chunk["chunk_id"] in chunk_ids:
            _fail("doc_chunk_id_duplicate")
        chunk_ids.add(chunk["chunk_id"])
        for ref in chunk["core_atom_ids"] + chunk["context_atom_ids"] \
                + chunk["dependency_atom_ids"]:
            if ref not in atom_ids:
                _fail("doc_chunk_atom_unknown")
        overlap = set(chunk["core_atom_ids"]) & owned
        if overlap:
            _fail("doc_chunk_core_overlap")
        owned |= set(chunk["core_atom_ids"])
    if owned != atom_ids:
        _fail("doc_core_coverage_incomplete")

    evidence_ids = {e["evidence_id"] for e in evidence}
    if len(evidence_ids) != len(evidence):
        _fail("doc_evidence_id_duplicate")
    atoms_by_id = {a["atom_id"]: a for a in atoms}
    for e in evidence:
        if e["atom_id"] not in atom_ids:
            _fail("doc_evidence_atom_unknown")
        if e["message_id"] != source["message_id"] \
                or e["revision"] != source["revision"]:
            _fail("doc_evidence_source_mismatch")
        atom = atoms_by_id[e["atom_id"]]
        # A quote may cross atoms; the atom containing its start owns it.
        if not atom["start"] <= e["start"] < atom["end"] \
                or e["end"] > source["body_codepoints"]:
            _fail("doc_evidence_range_invalid")

    fact_ids = {f["fact_id"] for f in facts}
    if len(fact_ids) != len(facts):
        _fail("doc_fact_id_duplicate")
    obligation_ids = {o["obligation_id"] for o in obligations}
    if len(obligation_ids) != len(obligations):
        _fail("doc_obligation_id_duplicate")
    for f in facts:
        if any(ref not in evidence_ids for ref in f["evidence_ids"]):
            _fail("doc_fact_evidence_unknown")
        if any(ref not in obligation_ids for ref in f["obligation_ids"]):
            _fail("doc_fact_obligation_unknown")
        if f["validation_status"] == "verified" \
                and not f["evidence_ids"]:
            _fail("doc_verified_without_evidence")
    for o in obligations:
        if o["owner_id"] not in atom_ids and o["owner_id"] not in chunk_ids:
            _fail("doc_obligation_owner_unknown")
        if any(ref not in fact_ids for ref in o["fact_ids"]):
            _fail("doc_obligation_fact_unknown")
    relation_ids = set()
    for r in relations:
        if r["relation_id"] in relation_ids:
            _fail("doc_relation_id_duplicate")
        relation_ids.add(r["relation_id"])
        for endpoint in (r["left_fact_id"], r["right_fact_id"]):
            if endpoint not in fact_ids:
                _fail("doc_relation_fact_unknown")
        if any(ref not in evidence_ids for ref in r["evidence_ids"]):
            _fail("doc_relation_evidence_unknown")
    if any(ref not in obligation_ids
           for ref in coverage["open_obligation_ids"]):
        _fail("doc_coverage_obligation_unknown")
    if coverage["status"] == "complete" \
            and coverage["open_obligation_ids"]:
        _fail("doc_coverage_complete_with_open")
    return {
        "version": CONTRACT_VERSION,
        "source": source, "atoms": atoms, "chunks": chunks,
        "obligations": obligations, "evidence": evidence,
        "facts": facts, "relations": relations,
        "coverage": coverage,
    }
