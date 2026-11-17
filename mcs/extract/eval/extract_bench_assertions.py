"""Score allergy and adverse-reaction assertions on subject, time, polarity and certainty for the extraction benchmark.

Evaluation only: nothing here is imported by the periodic jobs.

The mention layer (`patient_context` categories allergies / adverse_events)
keeps the source quote as written, so it cannot tell "アレルギーなし" from an
allergy, or a planned test from a history. This module scores the axis layer:
one assertion per allergen with explicit axes.

Output item shape (PROVISIONAL — the extraction generation that first emits
the field decides the real shape; `units` is the single place to adapt):

  {"allergen": str | null, "kind": "allergy" | "adverse_reaction",
   "reaction": str | null, "subject": ..., "status": ..., "negated": bool,
   "certainty": "confirmed" | "suspected", "evidence": str,
   "unverified": bool?}

Label shape (expect / forbid `allergies`): any subset of
allergen / kind / reaction / subject / status / negated / certainty.
allergen and reaction match by containment, the rest exactly; an explicit
null on allergen or reaction pins "absent". A label without allergen matches
any allergen, so {} in forbid means "no allergy assertion at all".
subject / status "unknown" is the extractor's abstention: never a gold value
in expect, but nameable in forbid, because an assertion that leaves the
subject or the time open is read downstream as the patient's own, current
one — a family member's allergy filed that way is a wrong assertion.

An expected label is satisfied only by an output whose evidence is located
uniquely in the case body and which is not marked unverified. A forbid label
is violated by any output not marked unverified, located or not: an
assertion shown as confirmed without a source is the worst case, not a
harmless one.
"""
from __future__ import annotations

from clinical_events import STATUSES, SUBJECTS
from mcs_util import locate_quote_span

FIELD = "allergies"
KINDS = ("allergy", "adverse_reaction")
CERTAINTIES = ("confirmed", "suspected")
ABSTENTION = "unknown"

_TEXT_KEYS = ("allergen", "reaction")
_ENUM_KEYS = {"kind": KINDS, "subject": SUBJECTS, "status": STATUSES, "certainty": CERTAINTIES}
_LABEL_KEYS = frozenset(_TEXT_KEYS) | frozenset(_ENUM_KEYS) | {"negated"}


def has_field(out) -> bool:
    """Whether a stored output carries the field at all (an older generation does not)."""
    return isinstance(out, dict) and FIELD in out


def units(out, body) -> list[dict]:
    """Map a stored output to scoring units, marking which are source-grounded."""
    items = out.get(FIELD) if isinstance(out, dict) else None
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        quote = item.get("evidence")
        span = (locate_quote_span(body, quote)
                if isinstance(body, str) and isinstance(quote, str) and quote else None)
        source = body[span[0]:span[1]] if span else ""
        grounded = span is not None and all(
            item.get(key) is None or (isinstance(item[key], str) and item[key] in source)
            for key in _TEXT_KEYS)
        result.append({**{key: item.get(key) for key in _LABEL_KEYS},
                       "unverified": item.get("unverified") is True,
                       "grounded": grounded})
    return result


def _axes_match(label: dict, unit: dict) -> bool:
    for key in _TEXT_KEYS:
        if key not in label:
            continue
        want, got = label[key], unit[key]
        if want is None:
            if got is not None:
                return False
        elif not isinstance(got, str) or want not in got:
            return False
    return all(key not in label or label[key] == unit[key]
               for key in (*_ENUM_KEYS, "negated"))


def match_expected(label: dict, got: list) -> bool:
    """A grounded, confirmed unit carries every axis the label pins."""
    return any(unit["grounded"] and not unit["unverified"] and _axes_match(label, unit)
               for unit in got)


def match_forbidden(label: dict, got: list) -> bool:
    """Any unit shown as confirmed carries the forbidden axes."""
    return any(not unit["unverified"] and _axes_match(label, unit) for unit in got)


def label_name(label: dict) -> str:
    """Short display name of a label for the violation list."""
    return label.get("allergen") or label.get("kind") or "*"


def labels_valid(labels, *, forbid: bool = False) -> bool:
    """Reject typos and off-vocabulary values that would make a label match nothing.

    Only a forbid label may name the abstention value: nothing is expected to abstain.
    """
    if not isinstance(labels, list):
        return False
    for label in labels:
        if not isinstance(label, dict) or set(label) - _LABEL_KEYS:
            return False
        if any(key in label and label[key] is not None
               and not (isinstance(label[key], str) and label[key].strip())
               for key in _TEXT_KEYS):
            return False
        if any(key in label and (label[key] not in allowed
                                 or (label[key] == ABSTENTION and not forbid))
               for key, allowed in _ENUM_KEYS.items()):
            return False
        if "negated" in label and type(label["negated"]) is not bool:
            return False
    return True
