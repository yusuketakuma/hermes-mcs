"""Conservative surface normalization and quote-bound laboratory/vital candidates."""

from datetime import date
import re
import unicodedata
import clinical_scope
from typing import Literal, TypedDict

from semantic_quantities import normalized_quantity_number
from mcs_util import locate_quote_span, fold_map

def patient_item_scope(item, body, *, patient_name=None, surface=None, default="patient"):
    """Resolve one stored item's original span before using it as patient current data."""
    if not isinstance(item, dict) or not isinstance(body, str):
        return "unknown"
    quote = item.get("evidence")
    if not isinstance(quote, str) or not quote.strip():
        quote = surface
    span = locate_quote_span(body, quote) if isinstance(quote, str) and quote.strip() else None
    if span is None:
        # Older parsed artifacts did not require a literal quote. Preserve
        # that contract only when the source has no explicit other-person
        # or noncurrent scope; a mixed source cannot prove an unlocated item.
        if not body:
            return "unknown"  # no clause end to check (a non-empty body always has `$`)
        scopes = (clinical_scope.patient_source_scope(body, max(0, m.start() - 1), m.start(),
                                               patient_name=patient_name, default=default)
                  for m in re.finditer(r"[。！？!?;；、,\n]|$", body) if m.start() > 0)
        return "patient" if all(scope == "patient" for scope in scopes) else "unknown"
    # A full-sentence quote may start before its explicit subject. Locate the
    # item's own surface inside that quote, while retaining surrounding scope.
    anchors = (surface, item.get("value"))
    if surface and body[span[0]:span[1]].count(str(surface)) > 1:
        if item.get("value") is None or locate_quote_span(body[span[0]:span[1]], str(item["value"])) is None:
            return "unknown"
        anchors = (item.get("value"), surface)
    for anchor in anchors:
        inner = locate_quote_span(body[span[0]:span[1]], str(anchor)) if anchor is not None else None
        if inner is not None:
            span = (span[0] + inner[0], span[0] + inner[1])
            break
    return clinical_scope.patient_source_scope(body, *span, patient_name=patient_name, default=default)


def patient_current_vitals(values, body, *, patient_name=None):
    """Keep source-consistent vitals and the trusted unscoped legacy contract."""
    kept = patient_vitals(values, body, patient_name=patient_name)
    if isinstance(values, dict) and patient_item_scope({}, body, patient_name=patient_name) == "patient":
        # A source-labelled measurement overrides unscoped parsed compatibility.
        # Unmentioned/legacy fields keep their existing trusted-output contract.
        observed, grounded = set(), {}
        for match in _vital_numbers(body):
            label = _nearest_vital_label(body, *match.span())
            if label == "bp":
                side = _bp_side(body, *match.span())
                observed.update((side,) if side else ("sbp", "dbp"))
            elif label:
                observed.add(label)
                # The writer guard's label grammar (P72, 体温が36.5℃) also confirms an equal patient reading;
                # scope from the label start so an owner before it (ご家族のP72) is seen.
                starts = [m.start() for m in _VITAL_LABELS[label].finditer(
                    body, max(0, match.start() - _VITAL_WIN_BACK), match.start())]
                if (type(values.get(label)) in (int, float) and values[label] == _bp_number(match[0])
                        and not _FLOW_AFTER.match(body, match.end())
                        and clinical_scope.patient_source_scope(body, starts[-1] if starts else match.start(),
                                                         match.end(), patient_name=patient_name,
                                                         measurement=True) == "patient"):
                    grounded[label] = values[label]
        return {**{key: value for key, value in values.items() if key not in observed},
                **_current_only(grounded, body, patient_name), **kept}
    return kept


def _current_only(vitals: dict, body: str, patient_name=None) -> dict:
    """Leave out oxygen-flow numbers and readings superseded by explicitly current ones."""
    def superseded(key, value):
        if key not in _VITAL_CLASS or type(value) not in (int, float):
            return False
        spans = [m.span() for m in _vital_numbers(body) if _bp_number(m[0]) == value]
        return bool(spans) and (all(_FLOW_AFTER.match(body, end) for _, end in spans)
                                or _vital_superseded(body, _VITAL_CLASS[key], spans, patient_name))
    return {key: value for key, value in vitals.items() if not superseded(key, value)}


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
    "weight": ("体重", "weight"),
    "height": ("身長", "height"),
    "blood_glucose": ("血糖", "血糖値", "bs", "glu", "glucose"),
}
_LAB_UNITS = {
    "mg/dl": "mg/dL", "mmol/l": "mmol/L", "μmol/l": "μmol/L",
    "g/dl": "g/dL", "%": "%", "u/l": "U/L", "meq/l": "mEq/L",
    "kg": "kg", "g": "g", "cm": "cm", "m": "m",
    "pg/ml": "pg/mL", "ml/min/1.73m2": "mL/min/1.73m2",
}
_DATE = re.compile(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?(?!\d)")


_MONTH_DAY = re.compile(r"(?<![\d/年-])(\d{1,2})[/月](\d{1,2})日?(?![\d/])")


def written_day(text) -> tuple | None:
    """A comparable key for the one date a text writes: ("ymd", date) for a full
    date (2026-10-01, 2026/10/1 and 2026年10月1日 are one day) or ("md", month,
    day) for a date written without its year. 不明/未確認, an impossible date or
    two dates give None. A year is never guessed, so the two kinds never match."""
    if not isinstance(text, str):
        return None
    text = unicodedata.normalize("NFKC", text)
    full, short = list(_DATE.finditer(text)), list(_MONTH_DAY.finditer(_DATE.sub("", text)))
    try:
        if len(full) == 1 and not short:
            return ("ymd", date(*map(int, full[0].groups())))
        if len(short) == 1 and not full:
            month, day = map(int, short[0].groups())
            date(2000, month, day)          # a leap year: only checks the month/day exists
            return ("md", month, day)
    except ValueError:
        return None
    return None


class LabDetails(TypedDict, total=False):
    reported_measured_on: str
    condition: str
    subject: str
    status: str


class LabCandidate(LabDetails):
    analyte: str | None
    measurement_type: str
    value: str
    value_kind: Literal["decimal", "text"]
    unit: str | None
    measured_on: str | None
    evidence: str | None
    confirmation: Literal["quote_supported", "unverified"]


def lab_analyte(name: str) -> str | None:
    """The analyte key for a lab name spelling (Cr/クレアチニン -> creatinine), else None."""
    label = fold_surface(name)
    return next((key for key, names in _LAB_NAMES.items() if label in names), None)


def lab_candidate(name: str, value: str | int | float, unit: str | None,
                  evidence: str | None, *, unverified: bool = False,
                  flag: str | None = None, subject: str | None = None,
                  status: str | None = None, measured_on: str | None = None,
                  condition: str | None = None, patient_name: str | None = None) -> LabCandidate:
    """Keep unknown/mismatched values separate; quote support is not human approval."""
    label = fold_surface(name)
    analyte = lab_analyte(name)
    raw_value = unicodedata.normalize("NFKC", str(value)).strip()
    # Bound before Decimal rendering (including floats with huge exponents).
    numeric = ((type(value) in (int, float) and 0 <= value <= 1e20)
               or (bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw_value))
                   and sum(c.isdigit() for c in raw_value) <= 20))
    number = normalized_quantity_number(raw_value) if numeric else None
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
            if result and normalized_quantity_number(result[1]) == number:
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
    if re.search(r"予定|検査依頼|目標|基準|参考", quote):
        supported = False
    if readings and isinstance(evidence, str):
        _, starts, ends = fold_map(evidence, casefold=True)
        reading = readings[0]
        if clinical_scope.patient_source_scope(evidence, starts[reading.start()], ends[reading.end() - 1],
                                        patient_name=patient_name) \
                not in ("patient", "past"):
            supported = False
    if flag:
        cues = r"高|↑|high" if flag == "high" else r"低|↓|low"
        tail = quote[readings[0].end():] if readings else ""
        value_token = re.match(r"[+-]?[0-9]+(?:\.[0-9]+)?[a-zμ%/0-9.^²]*", tail)
        suffix = tail[value_token.end():] if value_token else ""
        if not re.match(r"(?:ト|デ|ガ|[,、:（(])*(" + cues + ")", suffix):
            supported = False
    reported = measured_on
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
    details = {}
    for key, value in (("reported_measured_on", reported), ("condition", condition)):
        if value is not None:
            if isinstance(value, str) and value.strip() and isinstance(evidence, str) and value in evidence:
                details[key] = value
            else:
                supported = False
    if subject is not None:
        if subject in ("patient", "family", "other"):
            details["subject"] = subject
        if subject != "patient":
            supported = False
    if status is not None:
        if status in ("current", "past", "planned"):
            details["status"] = status
        if status not in ("current", "past"):
            supported = False
    return {**details, "analyte": analyte,
            "measurement_type": analyte if analyte in
            {"egfr", "creatinine", "ast", "alt", "weight", "height"} else "other",
            "value": normalized_value,
            "value_kind": "decimal" if number is not None else "text",
            "unit": normalized_unit, "measured_on": measured_on if supported else None,
            "evidence": evidence,
            "confirmation": "quote_supported" if supported else "unverified"}


# Vital keys anchor to the measurement label NEAREST their value in the
# body — labels precede the number (脈は48 / 血圧120/80), units follow
# (48回／分, 98℃). A bare 回/分 or % is shared between vitals, so it is
# not a distinguishing label. sbp/dbp share the 血圧 class — position
# inside the N/M pair decides which.
_VITAL_LABELS = {
    "bt":   re.compile(r"体温|BT|Bt|bt|℃"),
    # Bare P (BT36.5 P72) — not the P of BP/SpO2/PM.
    "hr":   re.compile(r"脈拍|(?<!静)(?<!動)脈|心拍|HR|Hr|hr"
                       r"|(?<![A-Za-z])P(?![A-Za-z])"),
    "rr":   re.compile(r"呼吸|RR|Rr|rr"),
    "bp":   re.compile(r"血圧|BP|Bp|bp|収縮|拡張|mmHg"),
    "spo2": re.compile(r"SpO2|Spo2|SPO2|spo2|酸素飽和|酸素"),
    "bs":   re.compile(r"血糖|BS|Bs|bs|Glu|glu|血糖値"),
}
# Forward window: only units that sit right after the number. A label
# word after it (P72 SpO2…) names the NEXT reading, not this one.
_VITAL_UNITS = {"bt": re.compile(r"℃"), "bp": re.compile(r"mmHg")}
_VITAL_CLASS = {"bt": "bt", "hr": "hr", "rr": "rr",
                "sbp": "bp", "dbp": "bp", "spo2": "spo2", "bs": "bs"}
_VITAL_WIN_BACK = 14
_VITAL_WIN_FWD = 8


def _nearest_vital_label(body: str, s: int, e: int):
    """Label class closest to the number — a preceding label beats
    a following unit (a label after the number belongs to the NEXT
    reading: 血圧120/80 脈60)."""
    best = None
    # search the window inside the body, not a slice of it: a slice cuts
    # 'BP' to a bare 'P' whose look-behind can no longer see the 'B'
    start = max(0, s - _VITAL_WIN_BACK)
    for cls, rx in _VITAL_LABELS.items():
        m_end = None
        for m in rx.finditer(body, start, s):
            m_end = m.end()
        if m_end is not None:
            dist = s - m_end
            if best is None or dist < best[0]:
                best = (dist, cls)
    for cls, rx in _VITAL_UNITS.items():
        fm = rx.search(body[e:e + _VITAL_WIN_FWD])
        if fm is not None:
            dist = 100 + fm.start()
            if best is None or dist < best[0]:
                best = (dist, cls)
    return best[1] if best else None


def _bp_side(body: str, s: int, e: int):
    """120/80: before the slash is systolic, after is diastolic."""
    if body[e:e + 1] in ("/", "／"):
        return "sbp"
    if body[s - 1:s] in ("/", "／"):
        return "dbp"
    return None


# One blood-pressure reading is a written 'A/B' whose nearest label is a
# blood-pressure label (BP/血圧/mmHg — never inferred from the values, so
# an unlabelled date such as 10/12 is not one) or an explicitly
# side-labelled single value.
# numbers follow the guard's own grammar (\d+ with an optional ./． decimal)
VITAL_NUMBER_TOKEN = r"\d+(?:[.．]\d+)?"
_BP_PAIR = re.compile(rf"(?<![\d.．])({VITAL_NUMBER_TOKEN})\s*[/／]\s*({VITAL_NUMBER_TOKEN})(?![\d.．])")
_BP_SIDE_WORD = re.compile(r"(収縮期|拡張期)(?:血圧)?[^0-9]*$")


def _bp_readings(body: str, keep=None):
    """(pairs, side-labelled values) of the body's blood pressures; spans
    keep(start, end) rejects — another person's, say — are left out."""
    accept = keep or (lambda s, e: True)
    pairs = set()
    for m in _BP_PAIR.finditer(body):
        if _nearest_vital_label(body, m.start(1), m.end(1)) == "bp" \
                and accept(m.start(), m.end()):
            pairs.add((_bp_number(m[1]), _bp_number(m[2])))
    sides = {"sbp": set(), "dbp": set()}
    for m in re.finditer(rf"(?<![\d.．/／]){VITAL_NUMBER_TOKEN}(?![\d.．/／])", body):
        word = _BP_SIDE_WORD.search(body[max(0, m.start() - _VITAL_WIN_BACK):m.start()])
        if word and accept(*m.span()):
            sides["sbp" if word[1] == "収縮期" else "dbp"].add(_bp_number(m[0]))
    return pairs, sides


def _bp_number(text: str) -> float:
    """The guard's token value (full-width point folded, no rounding)."""
    return float(text.replace("．", "."))


def _bp_pair_unproven(body: str, sbp, dbp, keep=None) -> bool:
    """True when sbp and dbp come from the body's blood-pressure readings
    but no single reading carries both: a side holds several different
    values and this combination is never written as one reading. Values
    with no such structure keep the existing per-value contract."""
    pairs, sides = _bp_readings(body, keep)
    if (sbp, dbp) in pairs:
        return False
    systolic = {upper for upper, _ in pairs} | sides["sbp"]
    diastolic = {lower for _, lower in pairs} | sides["dbp"]
    if sbp not in systolic and dbp not in diastolic:
        return False
    return len(systolic) > 1 or len(diastolic) > 1


# A model's flat vitals carry no person or time: a reading that is another
# person's, conditional or planned is never the patient's current value.
_NOT_PATIENT_CURRENT = ("family", "other", "conditional", "planned", "past")


# Other times a reading can be written at; the nearest mark before it decides.
_OTHER_TIME = rf"午前|午後|朝|昼|夕方?|夜|{clinical_scope.PAST_MARK}|以前|過去|先週|先月|昨年"


def _vital_current(body: str, pos: int) -> bool:
    """Whether the nearest time mark before pos in its sentence is an explicitly current one."""
    left = max(body.rfind(mark, 0, pos) for mark in "。！？!?\n") + 1
    last = None
    for last in re.finditer(rf"{clinical_scope.CURRENT_MARK.pattern}|{_OTHER_TIME}", body[left:pos]):
        pass
    return last is not None and clinical_scope.CURRENT_MARK.fullmatch(last[0]) is not None


def _vital_superseded(body: str, cls: str, spans, patient_name=None) -> bool:
    """True when the patient's same vital is explicitly reported as current and none of spans is that report."""

    def patient(start, end):
        return clinical_scope.patient_source_scope(body, start, end, patient_name=patient_name) == "patient"

    # Only the value's own same-vital readings of the patient count, never another person's equal value.
    own = [(s, e) for s, e in spans if _nearest_vital_label(body, s, e) == cls
           and patient(s, e)]
    if not own or any(_vital_current(body, s) for s, _ in own):
        return False
    # The patient's current report: a current reading (血圧は現在90/60), or a current
    # mention without one (現在の血圧は測定できませんでした).
    return (any(_nearest_vital_label(body, *m.span()) == cls and _vital_current(body, m.start()) and patient(*m.span())
                for m in _vital_numbers(body))
            or any(_vital_current(body, m.start()) and patient(*m.span())
                   for m in _VITAL_LABELS[cls].finditer(body)))


_NON_VITAL_AFTER = re.compile(r"[ \t　]*(?:名|人|歳|才|か?月|ヶ月|年|日|時|分|回目|件|階|号|番|代)")


def _vital_numbers(body):
    # The 2 in SpO2 is part of the label, not a measurement.
    return (m for m in re.finditer(r"SpO2|(?P<number>\d+(?:[.．]\d+)?)", body, re.I)
            if m["number"] is not None and not _NON_VITAL_AFTER.match(body, m.end()))


_FLOW_AFTER = re.compile(r"[ \t　]*(?:[LＬlℓ](?![A-Za-z])|リットル)")


def grounded_vitals(body: str | None, vit: dict,
                  drops: dict | None = None) -> dict:
    """Anchor each vital to the label nearest its value in the body.

    A mislabelled key ('脈は48' extracted as bs:48 — pulse shown as
    blood sugar) is clinically dangerous: when the label nearest the
    number names a different vital, remap to it; when the value is
    absent or the nearest labels disagree, drop the key. A verbatim
    value with no nearby label is kept — unverifiable, not contradicted
    (same policy as evidence)."""
    if not body or not vit:
        return vit
    toks = [(m.start(), m.end(), float(m.group().replace("．", ".")))
            for m in _vital_numbers(body)]
    issues = drops.setdefault("vitals", []) if drops is not None else []

    def note(s):
        if len(issues) < 6:
            issues.append(s)

    out = {}
    for k, val in vit.items():
        hits = [(s, e) for s, e, num in toks if num == val]
        if not hits:
            note(f"vitals.{k}={val:g} は本文に数値がありません")
            continue
        # 酸素2L: a number followed by a flow unit is oxygen delivery, never a vital
        hits = [(s, e) for s, e in hits if not _FLOW_AFTER.match(body, e)]
        hits = [(s, e) for s, e in hits
                if clinical_scope.patient_source_scope(body, s, e) not in _NOT_PATIENT_CURRENT]
        if not hits:
            note(f"vitals.{k}={val:g} は本文では本人以外・条件付き・予定・過去の記述です")
            continue
        kcls = _VITAL_CLASS[k]
        winners = {_nearest_vital_label(body, s, e) for s, e in hits}
        if kcls in winners:
            tgt = k
            if kcls == "bp":
                sides = [_bp_side(body, s, e) for s, e in hits]
                # BP80/80: the value also stands on its own side
                if k not in sides:
                    tgt = next((side for side in sides if side), k)
        else:
            winners.discard(None)
            if len(winners) != 1:
                if len(winners) > 1:
                    note(f"vitals.{k}={val:g} の測定名が本文で"
                         "一意に特定できません")
                else:
                    out[k] = val    # verbatim, unlabelled — keep
                continue
            tgt = winners.pop()
            if tgt == "bp":
                tgt = next((_bp_side(body, s, e) for s, e in hits
                            if _bp_side(body, s, e)), None)
                if tgt is None:
                    note(f"vitals.{k}={val:g} の測定名が本文で"
                         "一意に特定できません")
                    continue
        if _vital_superseded(body, _VITAL_CLASS[tgt], hits):
            note(f"vitals.{k}={val:g} は本文の現在の測定ではありません")
            continue
        if tgt != k:
            if tgt in vit or tgt in out:
                note(f"vitals.{k}={val:g} は本文では{tgt}の記述です")
                continue
            out[tgt] = val          # relabel, e.g. bs -> hr
        else:
            out[k] = val
    if "sbp" in out and "dbp" in out \
            and _bp_pair_unproven(body, out["sbp"], out["dbp"]):
        note(f"vitals.sbp/dbp={out['sbp']:g}/{out['dbp']:g} は"
             "本文で同じ測定ではありません")
        del out["sbp"], out["dbp"]
    return out


def patient_vitals(values, body, *, patient_name=None):
    """Filter flat cached vital values against original patient spans without reinterpreting other-person measurements."""
    if not isinstance(values, dict) or not isinstance(body, str):
        return {}
    kept = {}
    for key, pattern in VITAL_PATTERNS.items():
        for match in re.finditer(pattern, body):
            if clinical_scope.patient_source_scope(body, match.start(), match.end(), patient_name=patient_name,
                                    measurement=True) != "patient":
                continue
            actual = ({"sbp": int(match[1]), "dbp": int(match[2])} if key == "sbp" else
                      {key: float(match[1].replace("．", ".")) if "." in match[1].replace("．", ".")
                       else int(match[1])})
            present = [field for field in actual if type(values.get(field)) in (int, float)]
            if len(present) == 2 and any(values[field] != actual[field] for field in present):
                continue            # both sides cached: only this whole reading may match
            for field in present:
                if values[field] == actual[field]:
                    kept[field] = actual[field]
    # Partial LLM blood pressures still need their own label, side and patient span.
    partial = {key: values[key] for key in ("sbp", "dbp")
               if key not in kept and type(values.get(key)) in (int, float)}
    grounded = grounded_vitals(body, partial)
    for match in re.finditer(r"\d+(?:[.．]\d+)?", body):
        start, end = match.span()
        if _nearest_vital_label(body, start, end) != "bp":
            continue
        side = _bp_side(body, start, end)
        label = re.search(r"(収縮期|拡張期)(?:血圧)?[^0-9]*$", body[max(0, start - 14):start])
        if side is None and label is not None:
            side = "sbp" if label[1] == "収縮期" else "dbp"
        if side is None or clinical_scope.patient_source_scope(body, start, end, patient_name=patient_name,
                                                measurement=True) != "patient":
            continue
        if side in partial and grounded.get(side) == partial[side] == float(match[0].replace("．", ".")):
            kept[side] = partial[side]
    if "sbp" in kept and "dbp" in kept and _bp_pair_unproven(
            body, kept["sbp"], kept["dbp"],
            keep=lambda s, e: clinical_scope.patient_source_scope(
                body, s, e, patient_name=patient_name) == "patient"):
        del kept["sbp"], kept["dbp"]  # two readings never make one pair
    return _current_only(kept, body, patient_name)  # an earlier reading is not the current one

VITAL_PATTERNS = {
    "bt":   r"(?:体温|BT)[:：は]?\s*(\d{2}(?:[.．]\d)?)(?![\d.．])\s*[℃度]?",
    # 不整脈 is a finding, not a pulse label ("不整脈は20回" ≠ HR 20)
    "hr":   r"(?:脈拍|(?<!静)(?<!動)(?<!整)脈|HR|心拍数?)[:：は]?\s*(\d{2,3})(?![\d.．])",
    "rr":   r"(?:呼吸(?:数)?|RR)[:：は]?\s*(\d{1,2})(?![\d.．])",
    "sbp":  r"(?:血圧|BP)[:：は]?\s*(\d{2,3})\s*[/／]\s*(\d{2,3})(?![\d.．])",
    # a number followed by a flow unit is oxygen delivery (酸素10L),
    # never a saturation reading
    "spo2": r"(?:SpO2|Spo2|SPO2|spo2|酸素)[:：は]?\s*(\d{2,3})(?![\d.．])"
            r"(?!\s*(?:[LＬlℓ]|リットル))\s*[%％]?",
    "bs":   r"(?:血糖|BS|Glu)[:：は]?\s*(\d{2,3})(?![\d.．])",
}


def absolute_date(text: str) -> str | None:
    """occurred_at only when the original expression already carries an
    absolute date — relative/ambiguous text stays null and the raw
    time_text preserves it (spec §15.2)."""
    if not isinstance(text, str):
        return None
    m = re.match(r"^\s*(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if not m:
        return None
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    if not (2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return None
    from datetime import date
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


_VITAL_CTX_WIN = 40

_VITAL_CTX_EXCLUDE = re.compile(
    r"父|母|義父|義母|(?<![丈工])夫|妻|主人|息子|娘|兄|姉|弟|妹|祖父|祖母|家族|親族"
    r"|なら|たら|れば|の場合|の際|切ったら|超えたら|下回ったら"
    r"|を切ると|を超えると|を下回ると"
    r"|だった|でした|ありました|していた|になった|なりました"
    r"|先月|先週|昨日|以前|過去に"
    r"|測定不能|測れ(?:な|ませ|ず)|測定でき(?:な|ませ|ず)|未測定|不明")


def vital_threshold_flags(vit: dict, body: str | None,
                           thresholds: dict) -> list[dict]:
    """Deterministic threshold crossings on validated vitals.

    Returns [{key, value, rule, evidence}] — the reading's own body
    span (±20 chars) as the verbatim quote. A crossing whose reading
    cannot be anchored to its label, or whose context window shows a
    family/conditional/past/unmeasurable marker, does not flag."""
    if not body or not vit:
        return []
    toks = [(m.start(), m.end(), float(m.group().replace("．", ".")))
            for m in re.finditer(r"\d+(?:[.．]\d+)?", body)]
    rules = (("spo2", "lte", thresholds.get("spo2_lte")),
             ("sbp", "lte", thresholds.get("sbp_lte")),
             ("sbp", "gte", thresholds.get("sbp_gte")),
             ("bs", "lte", thresholds.get("bs_lte")))
    out = []
    for key, op, limit in rules:
        if limit is None or key not in vit:
            continue
        val = vit[key]
        if not ((op == "lte" and val <= limit)
                or (op == "gte" and val >= limit)):
            continue
        span = None
        for s, e, num in toks:
            if num != val:
                continue
            cls = _nearest_vital_label(body, s, e)
            if cls == "bp":
                cls = _bp_side(body, s, e) or cls
            if cls == key:
                span = (s, e)
                break
        if span is None:
            continue
        s, e = span
        if _VITAL_CTX_EXCLUDE.search(
                body, max(0, s - _VITAL_CTX_WIN), e + _VITAL_CTX_WIN):
            continue
        out.append({"key": key, "value": val,
                    "rule": f"{key}_{op}_{limit:g}",
                    "evidence": body[max(0, s - 20):min(len(body), e + 20)]
                    .strip()})
    return out



def prose_preserves_current_readings(text: str, body) -> bool:
    """Do not present a source's explicitly noncurrent reading without its qualifier."""
    if not isinstance(body, str):
        return True
    # Only explicit measurement syntax is checked, never a nearby drug dose or body weight.
    readings = [(m.start(group), m.end(group))
                for pattern in (*VITAL_PATTERNS.values(),
                                r"(?:収縮期|拡張期)(?:血圧)?[:：はが=\s]*(\d+(?:[.．]\d+)?)")
                for m in re.finditer(pattern, text) for group in range(1, (m.lastindex or 0) + 1)]
    for start, end in readings:
        cls = _nearest_vital_label(text, start, end)
        if cls is None or clinical_scope.patient_source_scope(text, start, end) != "patient":
            continue
        left = max(text.rfind(mark, 0, start) for mark in "。！？!?\n") + 1
        if (re.search(_OTHER_TIME, text[left:start])
                and not _vital_current(text, start)):
            continue
        hits = [m.span() for m in re.finditer(r"\d+(?:[.．]\d+)?", body)
                if _bp_number(m[0]) == _bp_number(text[start:end])
                and _nearest_vital_label(body, *m.span()) == cls]
        if hits and (all(clinical_scope.patient_source_scope(body, s, e)
                         in _NOT_PATIENT_CURRENT for s, e in hits)
                     or _vital_superseded(body, cls, hits)):
            return False
    return True
