"""Project explicit referral documents into literal fields and date-bound fragments."""
import calendar
from datetime import date
import re

from medical_document import _HEADINGS


SCHEMA = "referral-document/v1"
_MAX_RECORDS = 4096
_LABELS = {label: field for field, labels in _HEADINGS.items() for label in labels
           if field != "document_metadata"}
_LABELS.update({
    "治療経過": "treatment_course", "治療内容": "treatment_course",
    "紹介先医療機関等名": "recipient", "紹介先医療機関": "recipient", "紹介先": "recipient",
    "紹介元医療機関の所在地及び名称": "sender", "紹介元医療機関": "sender", "紹介元": "sender", "医療機関名": "institution",
    "診療科": "department", "科名": "department", "担当医": "physician", "医師氏名": "physician", "医師名": "physician",
    "患者氏名": "patient_name", "氏名": "patient_name", "生年月日": "birth_date",
    "性別": "sex", "年齢": "age", "職業": "occupation",
    "患者住所": "address", "住所": "address", "電話番号": "phone", "電話": "phone",
    "文書作成日": "document_date", "作成日": "document_date", "発行日": "document_date",
    "傷病名": "diagnosis",
})
_HEADER_FIELDS = {"recipient", "sender", "institution", "department", "physician", "patient_name",
                  "birth_date", "sex", "age", "occupation", "address", "phone", "document_date", "document_title", "document_metadata", "unassigned_date"}
HEADER_FIELDS = frozenset(_HEADER_FIELDS)
FIELD_LABELS = {field: labels[0] for field, labels in _HEADINGS.items()}
FIELD_LABELS.update({field: label for label, field in reversed(list(_LABELS.items()))})
FIELD_LABELS.update({"document_title": "文書名", "document_date": "文書作成日",
                     "birth_date": "生年月日", "treatment_course": "治療経過",
                     "unassigned_date": "日付（用途未確認）"})
_TITLE = re.compile(r"^[\s【\[（(]*(?:診\s*療\s*情\s*報\s*提\s*供\s*書|紹\s*介\s*状)[\s】\]）)]*$")
_LABEL_PATTERN = "|".join(r"\s*".join(map(re.escape, label))
                           for label in sorted(_LABELS, key=len, reverse=True))
_HEADING = re.compile(r"(?:^|(?<=[\t ｜|]))[【\[（(]?(?P<label>" + _LABEL_PATTERN
                      + r")(?:[：:】\]）)]|(?=[\t ]|$))")
_DATE = re.compile(
    r"(?<![\d.])(?:(?P<era>明治|大正|昭和|平成|令和)(?P<ey>元|\d{1,3})年"
    r"(?:(?P<em>\d{1,2})月(?:(?P<ed>\d{1,2})日)?)?"
    r"|(?P<y>\d{4})年(?:(?P<m>\d{1,2})月(?:(?P<d>\d{1,2})日)?)?"
    r"|(?P<iy>\d{4})(?P<sep>[-/])(?P<im>\d{1,2})(?P=sep)(?P<id>\d{1,2})(?![\d./-])"
    r"|(?P<sm>\d{1,2})月(?P<sd>\d{1,2})日)(?!\d)"
)
_SHORT_RANGE = re.compile(r"^\s*(?:〜|～|~|–|—|－|-|から)\s*(?:\d{1,2}日|\d{1,2}月(?!\d))")
_ERAS = {
    "明治": (1867, date(1868, 1, 25), date(1912, 7, 29)),
    "大正": (1911, date(1912, 7, 30), date(1926, 12, 24)),
    "昭和": (1925, date(1926, 12, 25), date(1989, 1, 7)),
    "平成": (1988, date(1989, 1, 8), date(2019, 4, 30)),
    "令和": (2018, date(2019, 5, 1), date.max),
}


def _dates(text, role):
    result = []
    for match in _DATE.finditer(text):
        parts = match.groupdict()
        era = parts["era"]
        year = (int(parts["ey"]) if parts["ey"] != "元" else 1) if era else None
        era_year = year
        if era:
            year += _ERAS[era][0]
            month, day = parts["em"], parts["ed"]
        else:
            year = parts["y"] or parts["iy"]
            month, day = parts["m"] or parts["im"] or parts["sm"], parts["d"] or parts["id"] or parts["sd"]
        year = int(year) if year is not None else None
        month = int(month) if month else None
        day = int(day) if day else None
        precision = "month_day" if year is None else "day" if day else "month" if month else "year"
        valid, value = True, None
        try:
            check_year = year if year is not None else 2000
            first = date(check_year, month or 1, day or 1)
            last = date(check_year, month or 12,
                        day or calendar.monthrange(check_year, month or 12)[1])
            if era == "明治" and year < 1873 and month is not None:
                raise ValueError("pre-Gregorian era month")
            if era and (era_year < 1 or first > _ERAS[era][2] or last < _ERAS[era][1]):
                raise ValueError("outside era")
            value = (f"--{month:02d}-{day:02d}" if year is None else
                     f"{year:04d}" + (f"-{month:02d}" if month else "") + (f"-{day:02d}" if day else ""))
        except ValueError:
            valid = False
        result.append({"raw": match[0], "value": value, "precision": precision, "valid": valid, "role": role})
        short_range = _SHORT_RANGE.match(text[match.end():match.end() + 30])
        if short_range:
            raw = re.search(r"\d{1,2}[日月]", short_range[0])[0]
            result.append({"raw": raw, "value": None, "precision": "day" if raw.endswith("日") else "month",
                           "valid": False, "role": role})
        if len(result) > _MAX_RECORDS:
            break
    return result


def _field(label):
    return _LABELS[re.sub(r"\s+", "", label)]


def _title_lines(text):
    lines, found = text.splitlines(), set()
    for index in range(len(lines)):
        candidate = ""
        for end in range(index, min(index + 3, len(lines))):
            if len(candidate) + len(lines[end]) > 100:
                break
            candidate += lines[end]
            if _TITLE.fullmatch(candidate):
                found.update(range(index, end + 1))
                break
    return found


def _limited(output, pages, index, start):
    output["limited"] = True
    output["unprocessed"] = [{"page": page["page"], "evidence_span": [start if i == index else 0, len(page["text"])]}
                             for i, page in enumerate(pages) if i >= index and (start if i == index else 0) < len(page["text"])]
    return output


def project(pages):
    """Preserve source text and dates without inferring clinical assertions or missing years."""
    identified = any(_title_lines(page["text"]) for page in pages)
    output = {"schema": SCHEMA, "identified": identified, "fields": [], "events": []}
    if not identified:
        return output
    current = "document_metadata"
    records = 0
    for page_index, page in enumerate(pages):
        text, offset = page["text"], 0
        title_lines = _title_lines(text)
        for line_index, line in enumerate(text.splitlines(keepends=True)):
            if line_index in title_lines:
                chunks = [("document_title", 0, len(line))]
            else:
                headings = list(_HEADING.finditer(line.rstrip("\r\n")))
                chunks = []
                if not headings or headings[0].start() > 0:
                    chunks.append((current, 0, headings[0].start() if headings else len(line)))
                for index, heading in enumerate(headings):
                    current = _field(heading["label"])
                    end = headings[index + 1].start() if index + 1 < len(headings) else len(line)
                    chunks.append((current, heading.start(), end))
            for field, start, end in chunks:
                raw = line[start:end]
                if not raw.strip():
                    continue
                # A bare date at a page boundary cannot inherit the preceding clinical heading.
                bare_dates = _dates(raw, 'document')
                remainder = raw
                for written in bare_dates:
                    remainder = remainder.replace(written['raw'], '', 1)
                if (field not in ('birth_date', 'document_date') and bare_dates
                        and not remainder.strip(' \t\r\n　()（）【】[]年月日火水木金土曜・:：')):
                    field = 'unassigned_date'
                role = "birth" if field == "birth_date" else "document" if field in _HEADER_FIELDS else "event"
                # Header continuation stays metadata; clinical context continues across page boundaries.
                # Keep a correction or denial in the same line as the statement it modifies.
                fragments = [raw]
                fragment_offset = offset + start
                for fragment in fragments:
                    if fragment.strip():
                        dates = _dates(fragment, role)
                        cost = 1 + (field not in _HEADER_FIELDS) + len(dates)
                        if records + cost > _MAX_RECORDS:
                            return _limited(output, pages, page_index, fragment_offset)
                        records += cost
                        row = {"field": field, "page": page["page"], "text": fragment,
                               "evidence": fragment, "evidence_span": [fragment_offset, fragment_offset + len(fragment)],
                               "dates": dates}
                        output["fields"].append(row)
                        if field not in _HEADER_FIELDS and _HEADING.fullmatch(fragment.strip()) is None:
                            distinct = {(d["value"], d["precision"], d["raw"] if not d["valid"] else None) for d in dates}
                            state = "ambiguous" if len(distinct) > 1 or any(not d["valid"] for d in dates) else "dated" if dates else "undated"
                            output["events"].append({**row, "time": dates[0] if state == "dated" else None,
                                                     "time_state": state})
                    fragment_offset += len(fragment)
            offset += len(line)
    return output
