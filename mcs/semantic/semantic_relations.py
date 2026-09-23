"""Candidate relation reconciliation for semantic-facts/v2.

``reconcile_facts`` compares newly extracted facts with the active fact
set and emits *candidate* relations only.  Nothing here mutates the
active patient state: contradictory, temporal, and repeated facts all
survive side-by-side and the relation records how they relate.  The only
place relations may activate is the ATOMIC_FACT_PROMOTION stage, which
checks ``relation_set_fingerprint`` against the persisted value before
committing.

Classification is deterministic — given the same inputs it always picks
the same type, in RELATION_TYPES priority order:
EXACT_DUPLICATE > EXPLICIT_SUPERSESSION > TRANSITION > CONTRADICTION >
COMPLEMENTS > UNRESOLVED.
"""
from __future__ import annotations

import re

import semantic_facts as sf
from mcs_requests import payload_hash


WORKFLOW_ORDER = (
    "reported", "considering", "planned", "ordered", "in_progress",
    "performed", "done",
)

# Medication action pairs forming a recognized temporal sequence for the
# same subject+entity.  When the event-time order is known the earlier
# fact is superseded; without an order the pair is a CONTRADICTION —
# never a silent latest-wins.
_ACTION_SUPERSESSION = {
    ("start", "stop"), ("stop", "start"), ("start", "cancelled"),
    ("ordered", "cancelled"), ("planned", "cancelled"),
    ("consider", "cancelled"), ("restart", "stop"),
    ("increase", "decrease"), ("decrease", "increase"),
    ("hold", "restart"), ("start", "hold"), ("ordered", "administered"),
    ("planned", "ordered"), ("consider", "planned"), ("consider", "start"),
    ("planned", "start"), ("ordered", "start"), ("stop", "restart"),
}

_ENTITY_TOKEN = re.compile(r"[ァ-ヶー]{2,}|[A-Za-z0-9]{2,}|[一-龥々]{2,}")


def relation_set_fingerprint(relations: list) -> str:
    """Canonical fingerprint of a relation set — persisted before
    promotion so a changed candidate set can never commit under a stale
    receipt."""
    rows = []
    for rel in relations or []:
        if not isinstance(rel, dict):
            continue
        rows.append({
            "l": rel.get("left_fact_id"), "r": rel.get("right_fact_id"),
            "t": rel.get("type"),
            "e": sorted(rel.get("evidence_ids") or []),
        })
    rows.sort(key=lambda x: (x["l"] or "", x["r"] or "", x["t"] or ""))
    return "relset_" + payload_hash(rows)[:16]


def _norm(text) -> str:
    return sf._normalize_name(text or "")


def _entity_tokens(statement: str) -> set:
    """Significant tokens standing in for entity identity without NLP —
    katakana runs (drug names), alphanumerics, and CJK compounds."""
    return {_norm(m.group(0)) for m in _ENTITY_TOKEN.finditer(statement or "")}


def _entity_overlap(left: dict, right: dict) -> set:
    return _entity_tokens(left.get("statement")) \
        & _entity_tokens(right.get("statement"))


def _iso(value) -> str | None:
    return value if isinstance(value, str) \
        and re.match(r"^\d{4}-\d{2}-\d{2}", value) else None


def _ordered_before(left: dict, right: dict) -> bool:
    """True when left strictly precedes right on an explicit time axis."""
    lt, rt = _iso(left.get("event_time")), _iso(right.get("event_time"))
    if lt and rt:
        return lt < rt
    lv, rv = _iso(left.get("valid_time")), _iso(right.get("valid_time"))
    return bool(lv and rv and lv < rv)


def classify_pair(left: dict, right: dict) -> dict | None:
    """Classify the relation between two facts, or ``None`` when they
    are unrelated.  ``left`` is the earlier/active fact, ``right`` the
    newer one for directed types."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return None
    lf_id, rf_id = left.get("fact_id"), right.get("fact_id")
    if not lf_id or not rf_id or lf_id == rf_id:
        return None
    rel_type = None
    reason = None
    if _norm(left.get("statement")) == _norm(right.get("statement")) \
            and left.get("kind") == right.get("kind"):
        rel_type, reason = "EXACT_DUPLICATE", "same_statement"
    else:
        shared = _entity_overlap(left, right)
        if not shared:
            return None
        same_subject = left.get("subject") == right.get("subject")
        same_kind = left.get("kind") == right.get("kind")
        la = left.get("action", "unknown")
        ra = right.get("action", "unknown")
        action_pair = same_kind and (
            (la, ra) in _ACTION_SUPERSESSION
            or (ra, la) in _ACTION_SUPERSESSION)
        polarity_conflict = (
            {left.get("polarity"), right.get("polarity")}
            == {"affirmed", "negated"})
        if same_subject and action_pair:
            if _ordered_before(left, right):
                rel_type = "EXPLICIT_SUPERSESSION"
                reason = f"action_{la}_then_{ra}"
            elif _ordered_before(right, left):
                # Older fact wins the left slot deterministically.
                return _relation(right, left, "EXPLICIT_SUPERSESSION",
                                 f"action_{ra}_then_{la}")
            else:
                rel_type, reason = "CONTRADICTION", "unordered_action_pair"
        elif same_subject and polarity_conflict:
            if _ordered_before(left, right):
                rel_type, reason = "EXPLICIT_SUPERSESSION", "polarity_flip"
            elif _ordered_before(right, left):
                return _relation(right, left, "EXPLICIT_SUPERSESSION",
                                 "polarity_flip")
            else:
                rel_type, reason = "CONTRADICTION", "polarity_conflict"
        elif same_subject and same_kind \
                and left.get("kind") != "other_observation":
            lw = left.get("workflow_status")
            rw = right.get("workflow_status")
            if lw in WORKFLOW_ORDER and rw in WORKFLOW_ORDER \
                    and WORKFLOW_ORDER.index(lw) < WORKFLOW_ORDER.index(rw):
                rel_type, reason = "TRANSITION", "workflow_progression"
            elif _norm(left.get("statement")) != _norm(right.get("statement")):
                rel_type, reason = "COMPLEMENTS", "shared_entity"
            else:
                rel_type, reason = "EXACT_DUPLICATE", "same_entity"
        elif not same_subject:
            rel_type, reason = "COMPLEMENTS", "shared_entity_other_subject"
        else:
            rel_type, reason = "UNRESOLVED", "shared_entity_unclassified"
    return _relation(left, right, rel_type, reason)


def _relation(left: dict, right: dict, rel_type: str, reason: str) -> dict:
    evidence = sorted(set(left.get("evidence_ids") or [])
                      | set(right.get("evidence_ids") or []))
    return {
        "relation_id": sf.relation_id(left["fact_id"], right["fact_id"],
                                      rel_type),
        "left_fact_id": left["fact_id"],
        "right_fact_id": right["fact_id"],
        "type": rel_type,
        "evidence_ids": evidence,
        "status": "candidate",
        "reason": reason,
    }


def reconcile_facts(active_facts: list, new_facts: list,
                    *, project_id=None) -> dict:
    """Compare new facts against the active set and return candidate
    relations plus the relation-set fingerprint.

    Pure and non-destructive: inputs are read-only, every input fact
    remains in place, and nothing is deactivated.  A CONTRADICTION is a
    recorded relation between two surviving facts — never a latest-wins
    overwrite.
    """
    active = [f for f in (active_facts or []) if isinstance(f, dict)]
    new = [f for f in (new_facts or []) if isinstance(f, dict)]
    relations = {}
    # New-vs-active is the reconciliation boundary; new-vs-new catches
    # intra-batch contradictions the same way (earlier index is "left").
    pairs = [(a, n) for a in active for n in new]
    pairs += [(new[i], new[j])
              for i in range(len(new)) for j in range(i + 1, len(new))]
    for left, right in pairs:
        rel = classify_pair(left, right)
        if rel is not None:
            relations[rel["relation_id"]] = rel
    out = sorted(relations.values(), key=lambda r: r["relation_id"])
    return {
        "relations": out,
        "fingerprint": relation_set_fingerprint(out),
        "counts": {t: sum(1 for r in out if r["type"] == t)
                   for t in sf.RELATION_TYPES},
        "active_preserved": len(active),
        "new_preserved": len(new),
    }
