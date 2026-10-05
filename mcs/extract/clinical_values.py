"""Conservative surface normalization and quote-bound laboratory candidates."""

from datetime import date
import re
import unicodedata
from typing import Literal, TypedDict

from semantic_quantities import _number


def fold_surface(text: str) -> str:
    """Fold width, case and kana without erasing identity-bearing punctuation."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c
                   for c in text if not c.isspace())


class MedicationSurface(TypedDict):
    surface: str
    base: str
    strength: str | None
    form: str | None
    resolution: Literal["unresolved"]
    method: Literal["surface"]


_MED_SUFFIX = re.compile(
    r"(?P<form>od錠|錠|カプセル|顆粒|細粒|散|シロップ|テープ|パップ|軟膏|点眼液|注射液|坐剤)?"
    r"(?P<strength>[0-9]+(?:\.[0-9]+)?(?:mg|μg|mcg|g|ml|%))?$")


def medication_surface(name: str) -> MedicationSurface:
    """Annotate spelling only; no ingredient aliases or product identification."""
    surface = fold_surface(name)
    suffix = _MED_SUFFIX.search(surface)
    base = surface[:suffix.start()] if suffix else surface
    # A numeric/form-only name is not a drug identity.
    if not base:
        base = surface
    return {"surface": surface, "base": base,
            "strength": suffix.group("strength") if suffix else None,
            "form": suffix.group("form") if suffix else None,
            "resolution": "unresolved", "method": "surface"}


# Item spellings from docs/roadmap/extraction.md; these are analyte labels,
# not drug-equivalence assertions. No OCR corrections or unit conversions.
_LAB_NAMES = {
    "creatinine": ("cr", "クレアチニン"),
    "egfr": ("egfr",),
    "potassium": ("k", "カリウム"),
    "sodium": ("na", "ナトリウム"),
    "hba1c": ("hba1c",),
    "inr": ("inr",),
    "crp": ("crp",),
    "hemoglobin": ("hb", "ヘモグロビン"),
    "albumin": ("alb", "アルブミン"),
    "ast": ("ast", "got"),
    "alt": ("alt", "gpt"),
    "bnp": ("bnp",),
    "bun": ("bun",),
}
_LAB_UNITS = {
    "mg/dl": "mg/dL", "mmol/l": "mmol/L", "μmol/l": "μmol/L",
    "g/dl": "g/dL", "%": "%", "u/l": "U/L", "meq/l": "mEq/L",
    "pg/ml": "pg/mL", "ml/min/1.73m2": "mL/min/1.73m2",
}
_DATE = re.compile(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?(?!\d)")


class LabCandidate(TypedDict):
    analyte: str | None
    measurement_type: str
    value: str
    value_kind: Literal["decimal", "text"]
    unit: str | None
    measured_on: str | None
    evidence: str | None
    confirmation: Literal["quote_supported", "unverified"]


def lab_candidate(name: str, value: str | int | float, unit: str | None,
                  evidence: str | None, *, unverified: bool = False,
                  flag: str | None = None) -> LabCandidate:
    """Keep unknown/mismatched values separate; quote support is not human approval."""
    label = fold_surface(name)
    analyte = next((key for key, names in _LAB_NAMES.items() if label in names), None)
    raw_value = unicodedata.normalize("NFKC", str(value)).strip()
    # Bound before Decimal rendering (including floats with huge exponents).
    numeric = ((type(value) in (int, float) and 0 <= value <= 1e20)
               or (bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw_value))
                   and sum(c.isdigit() for c in raw_value) <= 20))
    number = _number(raw_value) if numeric else None
    if number is not None and sum(c.isdigit() for c in number) > 20:
        number = None
    normalized_value = number if number is not None else raw_value
    unit_key = fold_surface(unit).replace("µ", "μ") if unit else None
    normalized_unit = _LAB_UNITS.get(unit_key, unit_key) if unit_key else None
    quote = fold_surface(evidence or "").replace("µ", "μ")
    names = _LAB_NAMES[analyte] if analyte else (label,)
    pattern = (r"(?<![a-z0-9])(?:" + "|".join(re.escape(n) for n in names)
               + r")(?![a-z])(?:値|ハ|:|=)*")
    readings = []
    for match in re.finditer(pattern, quote):
        tail = quote[match.end():]
        if number is not None:
            result = re.match(r"([+-]?[0-9]+(?:\.[0-9]+)?)(?![0-9.])"
                              r"([a-zμ%/0-9.^²]*)", tail)
            if result and _number(result[1]) == number:
                if unit_key is None or result[2] == unit_key:
                    readings.append(match)
        elif tail.startswith(fold_surface(raw_value)) and raw_value:
            remainder = tail[len(fold_surface(raw_value)):]
            result_unit = re.match(r"[a-zμ%/0-9.^²]*", remainder)
            suffix = remainder[len(unit_key):] if unit_key else remainder
            # A prefix is not the complete result: 陰性ではありません,
            # 陰性疑い etc. must stay candidates, without guessing their value.
            complete = re.match(r"(?:デス|デシタ)?(?:[。、,;；]|$)", suffix)
            if complete and (unit_key is None or (result_unit and result_unit[0] == unit_key)):
                readings.append(match)
    supported = (len(readings) == 1 and not unverified
                 and (unit_key is None or unit_key in _LAB_UNITS)
                 and (number is not None or not any(c.isdigit() for c in raw_value)))
    if supported and number is not None:
        matched_label = re.sub(r"(?:値|ハ|:|=)*$", "", readings[0].group())
        if len(matched_label) <= 2 and unit_key is None:
            supported = False
    # Subject, plans, comparators and reference intervals do not establish
    # a patient measurement. Ambiguous quotes remain candidates.
    if re.search(r"家族|娘|息子|(?<![丈工])夫|妻|母|父|予定|検査依頼|目標|基準|参考", quote):
        supported = False
    if flag:
        cues = r"高|↑|high" if flag == "high" else r"低|↓|low"
        tail = quote[readings[0].end():] if readings else ""
        value_token = re.match(r"[+-]?[0-9]+(?:\.[0-9]+)?[a-zμ%/0-9.^²]*", tail)
        suffix = tail[value_token.end():] if value_token else ""
        if not re.match(r"(?:ト|デ|ガ|[,、:（(])*(" + cues + ")", suffix):
            supported = False
    measured_on = None
    # Do not mistake another sentence's date or a posting date for sampling.
    clause = ""
    if readings:   # the clause that contains the matched reading itself
        at = readings[0].start()
        start = max((m.end() for m in re.finditer(r"[。、;；\n]", quote[:at])), default=0)
        stop = re.search(r"[。、;；\n]", quote[at:])
        clause = quote[start:at + stop.start() if stop else len(quote)]
    dates = list(_DATE.finditer(clause))
    if (supported and len(dates) == 1 and len(list(_DATE.finditer(quote))) == 1
            and re.search(r"採血|検査|測定", clause)):
        try:
            measured_on = date(*(int(n) for n in dates[0].groups())).isoformat()
        except ValueError:
            pass
    return {"analyte": analyte,
            "measurement_type": analyte if analyte in
            {"egfr", "creatinine", "ast", "alt"} else "other",
            "value": normalized_value,
            "value_kind": "decimal" if number is not None else "text",
            "unit": normalized_unit, "measured_on": measured_on,
            "evidence": evidence,
            "confirmation": "quote_supported" if supported else "unverified"}
