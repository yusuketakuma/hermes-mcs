"""Deterministic quantity checks for semantic claims.

The model may decide whether a claim is supported, but it cannot make a
different unit or an untraceable dose safe.  This module only compares the
claim with the exact evidence quotes of the facts named by that claim.  It
deliberately does not read the fact's convenience ``quantity`` field or
numbers from unrelated facts.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import unicodedata


# Longest units must be matched first: ``mg/mL`` otherwise becomes ``mg``.
_AMOUNT_RE = re.compile(
    r"(?<![0-9A-Za-z_.,+\-−＋])"
    r"(?P<value>(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+))\s*"
    r"(?P<unit>(?:mg\s*/\s*mL|μg|µg|ug|mcg|mg|mL|g|%)(?:\s*/\s*(?:日|回|錠|包))?)"
    # Do not truncate an unknown compound unit such as mg/kg to a bare mg.
    r"(?!\s*[／/]\s*[0-9]*[A-Za-zμµ一-龥々ヶ]+)",
    re.IGNORECASE,
)
_UNKNOWN_AMOUNT_RE = re.compile(
    r"(?<![A-Za-z_])"
    r"(?P<value>[+\-−＋]?(?:[0-9][0-9,]*(?:\.[0-9]+)?|\.[0-9]+))\s*"
    r"(?P<unit>[A-Za-zμµ]+(?:\s*[／/]\s*[0-9]*[A-Za-zμµ一-龥々ヶ]+)+"
    r"|[A-Za-zμµ]+|[一-龥々ヶ]+)"
)
_PER_DAY_RE = re.compile(
    r"(?P<days>[0-9]+)\s*日\s*(?P<count>[0-9]+)\s*回(?:量)?"
)
_SLASH_DAY_RE = re.compile(
    r"(?P<count>[0-9]+)\s*回\s*[／/]\s*日"
)
_COUNT_RE = re.compile(
    r"(?:[×x＊*]\s*)?(?P<count>[0-9]+)\s*回(?P<dose>量|あたり)?"
)
_DAY_AMOUNT_SCOPE_RE = re.compile(
    r"(?P<days>[0-9]+)\s*日\s*(?=[0-9]+(?:\.[0-9]+)?\s*"
    r"(?:mg\s*/\s*mL|mg／mL|μg|µg|ug|mcg|mg|mL|g|%))",
    re.IGNORECASE,
)


def _number(value: object) -> str | None:
    """Return a stable decimal spelling without performing unit conversion."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite() or number < 0:
        return None
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _unit(value: str) -> str:
    normalized = value.replace("／", "/").replace(" ", "")
    lowered = normalized.lower()
    if lowered in {"μg", "µg", "ug", "mcg"}:
        return "ug"
    if lowered == "ml":
        return "mL"
    if lowered == "mg/ml":
        return "mg/mL"
    if lowered == "mg":
        return "mg"
    if lowered == "g":
        return "g"
    if lowered == "%":
        return "%"
    return normalized


def _amount(value: object, unit: object, *, raw: str | None = None) -> dict | None:
    number = _number(value)
    if number is None or not isinstance(unit, str) or not unit.strip():
        return None
    return {"kind": "amount", "value": number, "unit": _unit(unit),
            **({"raw": raw} if raw is not None else {})}


def _unknown_amount(value: object, unit: object, *,
                    raw: str | None = None) -> dict | None:
    number = _number(value)
    if number is None:
        # Keep malformed sign/thousands forms as an unverified quantity so a
        # suffix such as ``-300mg`` cannot be shortened to a safe ``300mg``.
        number = str(value).strip()
        if not any(char.isdigit() for char in number):
            return None
    if not isinstance(unit, str) or not unit.strip():
        return None
    return {"kind": "amount", "value": number,
            "unit": f"unknown:{unit.replace('／', '/')}",
            "recognized": False,
            **({"raw": raw} if raw is not None else {})}


def _frequency(count: object, unit: str = "count", *, raw: str | None = None,
               period_days: object | None = None) -> dict | None:
    number = _number(count)
    if number is None:
        return None
    item = {"kind": "frequency", "value": number, "unit": unit,
            **({"raw": raw} if raw is not None else {})}
    if period_days is not None:
        day_number = _number(period_days)
        if day_number is not None:
            item["period_days"] = day_number
    return item


def extract_quantities(text: object) -> list[dict]:
    """Extract medication amount and dosing-frequency tokens from *text*.

    The records are intentionally syntactic.  ``300mg`` and ``0.3g`` stay
    different records; this function never converts between units.  Frequency
    shorthand such as ``×3回`` keeps an unspecified period; comparison may
    match it to an explicit once-per-day source without inventing a period.
    """
    if not isinstance(text, str) or not text:
        return []
    # NFKC makes full-width digits and multiplication signs comparable while
    # leaving the original evidence quote untouched in the caller's record.
    value = unicodedata.normalize("NFKC", text)
    found: list[tuple[int, int, dict]] = []

    known_amount_spans: list[tuple[int, int]] = []
    for match in _AMOUNT_RE.finditer(value):
        prefix = value[:match.start()]
        if re.search(r"[A-Za-zμµ]+\s*[／/]\s*$", prefix):
            # The denominator of an unsupported compound unit (e.g. the
            # ``5mL`` in ``300mg/5mL``) must not become a second safe token.
            continue
        item = _amount(match.group("value"), match.group("unit"),
                       raw=match.group(0))
        if item is not None:
            known_amount_spans.append(match.span())
            found.append((match.start(), match.end(), item))
    for match in _UNKNOWN_AMOUNT_RE.finditer(value):
        start, end = match.span()
        if any(start < known_end and end > known_start
               for known_start, known_end in known_amount_spans):
            continue
        # 回/日 are frequency tokens, not unsupported amount units.
        if match.group("unit") in {"回", "日"}:
            continue
        item = _unknown_amount(match.group("value"), match.group("unit"),
                               raw=match.group(0))
        if item is not None:
            found.append((start, end, item))

    # Prefer the most specific frequency forms.  The generic count matcher is
    # filtered below so ``1日3回`` contributes only one record.
    for match in _PER_DAY_RE.finditer(value):
        item = _frequency(match.group("count"), unit="per_day",
                          period_days=match.group("days"),
                          raw=match.group(0))
        if item is not None:
            found.append((match.start(), match.end(), item))
    for match in _SLASH_DAY_RE.finditer(value):
        item = _frequency(match.group("count"), unit="per_day",
                          period_days="1", raw=match.group(0))
        if item is not None:
            found.append((match.start(), match.end(), item))
    specific_spans = [(start, end) for start, end, item in found
                      if item["kind"] == "frequency"]
    for match in _COUNT_RE.finditer(value):
        start, end = match.span()
        if any(s <= start and end <= e for s, e in specific_spans):
            continue
        # Bare counts do not establish a timebase. ``1回量`` is per-dose.
        unit = "per_dose" if match.group("dose") else "count"
        item = _frequency(match.group("count"), unit=unit,
                           raw=match.group(0))
        if item is not None:
            found.append((start, end, item))

    # Preserve a total-per-day expression even when no ``回`` follows it,
    # e.g. ``1日300mg``.  This span is deliberately separate from the amount
    # token, so ``1回300mg`` and ``1日300mg`` cannot collapse to one number.
    specific_spans = [(start, end) for start, end, item in found
                      if item["kind"] == "frequency"]
    for match in _DAY_AMOUNT_SCOPE_RE.finditer(value):
        start, end = match.span()
        if any(s <= start and end <= e for s, e in specific_spans):
            continue
        item = _frequency(match.group("days"), unit="per_day_total",
                           period_days=match.group("days"),
                           raw=match.group(0))
        if item is not None:
            found.append((start, end, item))

    # Amount scope is retained when it is written immediately before/after
    # the amount.  An unqualified amount remains unqualified: omission of a
    # scope is not treated as proof of a new scope.
    for start, end, item in found:
        if item["kind"] != "amount":
            continue
        prefix = value[max(0, start - 24):start]
        suffix = value[end:end + 8]
        if re.search(r"[0-9]+\s*日\s*[0-9]+\s*回\s*$", prefix):
            item["scope"] = "per_day"
        elif re.search(r"[0-9]+\s*回(?:量|あたり)?\s*$", prefix):
            item["scope"] = "per_dose"
        elif (re.search(r"[0-9]+\s*日\s*$", prefix)
              or re.match(r"\s*[／/]\s*日", suffix)):
            item["scope"] = "per_day_total"

    # Stable source order, with amount before a same-position frequency only
    # as a deterministic tie-breaker.  ``raw`` and spans are implementation
    # aids and are not used for the comparison itself.
    found.sort(key=lambda row: (row[0], row[1], row[2]["kind"]))
    return [item for _, __, item in found]


def _claim_quantities(claim: dict) -> list[dict]:
    # Claims are currently persisted as free text.  Do not synthesize a
    # quantity from an optional model field: the guard's source of truth is
    # the text that the reviewer will see beside the evidence quote.
    return extract_quantities(claim.get("text"))


def _evidence_quantities(
        facts: list, refs: list[int]
) -> tuple[list[dict], list[int], dict[int, dict[str, int]]]:
    quantities: list[dict] = []
    evidence_facts: list[int] = []
    counts_by_fact: dict[int, dict[str, int]] = {}
    for index in refs:
        if type(index) is not int or index < 0 or index >= len(facts):
            continue
        fact = facts[index]
        evidence = fact.get("_evidence") if isinstance(fact, dict) else None
        quote = evidence.get("quote") if isinstance(evidence, dict) else None
        if not isinstance(quote, str):
            continue
        evidence_facts.append(index)
        extracted = extract_quantities(quote)
        quantities.extend(extracted)
        counts_by_fact[index] = {
            "amount": sum(q["kind"] == "amount" for q in extracted),
            "frequency": sum(q["kind"] == "frequency" for q in extracted),
        }
    return quantities, evidence_facts, counts_by_fact


def _finding(code: str, claim_id: object, *, quantity: dict | None = None,
             fact_refs: list[int] | None = None) -> dict:
    finding = {"code": code, "claim": claim_id}
    if quantity is not None:
        finding["quantity"] = {k: v for k, v in quantity.items()
                                if k in ("kind", "value", "unit", "scope",
                                         "period_days", "recognized")}
    if fact_refs is not None:
        finding["fact_refs"] = list(fact_refs)
    return finding


def _compatible(wanted: dict, candidate: dict) -> bool:
    """Compare one expression without converting units or scopes."""
    if wanted.get("recognized") is False or candidate.get("recognized") is False:
        return False
    if wanted.get("kind") != candidate.get("kind"):
        return False
    if wanted.get("value") != candidate.get("value"):
        return False
    if wanted.get("kind") == "amount":
        if wanted.get("unit") != candidate.get("unit"):
            return False
        wanted_scope = wanted.get("scope")
        candidate_scope = candidate.get("scope")
        # A total daily dose and an unqualified amount are not interchangeable.
        return wanted_scope == candidate_scope

    wanted_unit = wanted.get("unit")
    candidate_unit = candidate.get("unit")
    if wanted_unit == candidate_unit:
        if wanted_unit == "per_day":
            return wanted.get("period_days") == candidate.get("period_days")
        return True
    # ``×3回`` is an intentionally incomplete shorthand.  It can be
    # accepted only when the evidence explicitly says the same count once per
    # day; the inverse would add a timebase absent from the evidence.
    return (wanted_unit == "count" and candidate_unit == "per_day"
            and candidate.get("period_days") == "1")


def claim_quantity_findings(claim: dict, facts: list) -> list[dict]:
    """Return deterministic findings for quantities in one claim.

    Only ``claim.text`` and the
    ``_evidence.quote`` of the claim's ``fact_refs`` are considered.  A
    quantity-free claim must not drop quantities from its evidence.  A quantity with no
    usable quote, a changed value/unit, an unsupported conversion, or an
    ambiguous multi-fact relation is held for review.
    """
    if not isinstance(claim, dict) or not isinstance(facts, list):
        return []
    claim_quantities = _claim_quantities(claim)
    refs = claim.get("fact_refs")
    refs = refs if isinstance(refs, list) else []
    claim_id = claim.get("claim_id")
    evidence_quantities, evidence_facts, quantity_counts_by_fact = \
        _evidence_quantities(facts, refs)
    if not claim_quantities:
        # A repair must not evade the deterministic check by deleting the
        # number while still citing a fact whose exact quote contains one.
        if evidence_quantities:
            return [_finding("claim_quantity_unverified", claim_id,
                             fact_refs=refs)]
        return []
    if not evidence_facts or not evidence_quantities:
        return [_finding("claim_quantity_unverified", claim_id,
                         fact_refs=refs)]

    findings: list[dict] = []
    # A quote containing several amounts can describe different drugs or
    # times.  Without structured drug/time links, token-set equality cannot
    # establish which amount belongs to which claim.  Keep the relation
    # explicitly unverified rather than claiming the values were swapped
    # safely (spec §16.1).
    claim_amounts = [q for q in claim_quantities if q["kind"] == "amount"]
    if claim_quantities and (
            len(evidence_facts) > 1
            or any(any(count > 1 for count in counts.values())
                   for counts in quantity_counts_by_fact.values())
            and (claim_amounts or any(q["kind"] == "frequency"
                                      for q in claim_quantities))):
        return [_finding("claim_quantity_relation_unverified", claim_id,
                         fact_refs=refs)]

    for wanted in claim_quantities:
        if wanted.get("recognized") is False:
            findings.append(_finding("claim_quantity_unverified", claim_id,
                                     quantity=wanted, fact_refs=refs))
            continue
        same = [candidate for candidate in evidence_quantities
                if candidate["kind"] == wanted["kind"]]
        if not same:
            findings.append(_finding("claim_quantity_unverified", claim_id,
                                     quantity=wanted, fact_refs=refs))
            continue
        exact = [candidate for candidate in same
                 if _compatible(wanted, candidate)]
        if exact:
            # Multi-fact claims already returned relation_unverified above,
            # so every claim reaching this point references exactly one
            # evidence fact — a compatible amount there is unambiguous.
            continue

        same_value = [candidate for candidate in same
                      if candidate["value"] == wanted["value"]]
        same_unit = [candidate for candidate in same
                     if candidate["unit"] == wanted["unit"]]
        if same_value or same_unit:
            findings.append(_finding("claim_quantity_mismatch", claim_id,
                                     quantity=wanted, fact_refs=refs))
        else:
            # Different values and units may be mathematically convertible,
            # but conversion is outside this guard and must be reviewed.
            findings.append(_finding("claim_quantity_unverified", claim_id,
                                     quantity=wanted, fact_refs=refs))
    # Retaining a fact reference alone does not preserve its numeric content.
    if any(not any(_compatible(wanted, source) for wanted in claim_quantities)
           for source in evidence_quantities):
        findings.append(_finding("claim_quantity_missing", claim_id,
                                 fact_refs=refs))
    return findings


__all__ = ["claim_quantity_findings", "extract_quantities"]
