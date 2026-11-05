"""Group grounded written observations without inferring a current clinical value."""
from datetime import datetime, timedelta, timezone
import math
import re

from clinical_values import (_FLOW_AFTER, _nearest_vital_label, _vital_numbers, grounded_vitals,
                             lab_candidate, written_day)
from patient_context_projection import paired_date_conflict
from mcs_util import valid_hash
import original_source_keys

_FIELDS = ("observation_name", "value", "unit", "condition", "measured_on", "observed_on", "last_confirmed_on")
_RISK = re.compile(r"未確認|未確定|不明|疑い|かもしれ|未測定|未実施|未検査|ない|なし|無し|なく|"
                   r"ありません|認めず|認めません|否定|せず|予定|計画|見込み|明日|翌日|または|あるいは")
_JST = timezone(timedelta(hours=9))
_PATIENT = re.compile(r"(?:ご本人|本人|患者(?:本人|さん|様)?|利用者(?:本人|さん|様)?)(?:の|は|が|を|に|へ|も|、|，|:|：|\s|$)")
_OTHER = re.compile(r"家族|母|父|他者|友人|夫|妻|息子|娘|配偶者|同居人")
_CONDITION_CUE = re.compile(r"空腹|食前|食後|安静|起立|臥位|坐位|座位|室内気|酸素投与|酸素\s*\d")
_VITAL_NAMES = {"体温": "bt", "bt": "bt", "spo2": "spo2", "酸素飽和度": "spo2",
                "脈拍": "hr", "心拍": "hr", "hr": "hr", "呼吸数": "rr", "rr": "rr"}
_VITAL_UNITS = {"bt": ("℃",), "spo2": ("%", "％"), "hr": ("回/分", "回／分"),
                "rr": ("回/分", "回／分")}
_VITAL_LIST_ITEM = re.compile(
    r"(?:(?P<condition>酸素\s*\d{1,3}(?:[.．]\d{1,2})?\s*[LＬ]"
    r"(?:\s*[/／]\s*分)?\s*投与下)の)?"
    r"(?P<name>体温|BT|SpO2|酸素飽和度|脈拍|心拍|HR|呼吸数|RR)"
    r"\s*(?:は|が|[:：])?\s*(?P<value>\d{1,3}(?:[.．]\d{1,2})?)\s*"
    r"(?P<unit>℃|%|％|回/分|回／分)", re.I)
_BLOOD_PRESSURE_LIST_ITEM = re.compile(
    r"(?:血圧|BP)\s*(?:は|が|[:：])?\s*\d{2,3}\s*[/／]\s*\d{2,3}"
    r"(?![\d.．/／])\s*(?:mmHg)?", re.I)
_TRAILING_OXYGEN_FLOW = re.compile(r"酸素\s*\d{1,3}(?:[.．]\d{1,2})?\s*[LＬ]"
                                  r"(?:\s*[/／]\s*分)?", re.I)


_SOURCE_FIELDS = ("source_key", "source_binding", "parent_message_id", "attachment_id", "page",
                  "source_artifact_id", "extraction_artifact_id")


def source_identity(item):
    """Keep original OCR pages distinct while retaining the existing chat identity."""
    mid, digest = item.get("message_id"), item.get("content_hash")
    if not valid_hash(digest):
        return None
    if item.get("source") == "chat" and type(mid) is int and mid > 0:
        return mid, digest
    key = item.get("source_key")
    if (item.get("source") != "attachment_ocr" or type(mid) is not int or mid != 0
            or not original_source_keys.valid(key) or original_source_keys.parse(key)[0] != "attachment_ocr"
            or not valid_hash(item.get("source_binding")) or item.get("posted_at_ts") is not None):
        return None
    _, artifact, page = original_source_keys.parse(key)
    if any(field in item and (type(item[field]) is not int or item[field] != expected)
           for field, expected in (("source_artifact_id", artifact), ("page", page))):
        return None
    return key, digest


def _subject(item):
    """Require every explicit subject axis to identify the patient."""
    axes = [item.get("subject"), item.get("reported_subject")]
    temporal = item.get("temporal")
    if isinstance(temporal, dict):
        axes.append(temporal.get("subject"))
    axes = [axis for axis in axes if axis not in (None, "unspecified")]
    return "patient" if axes and all(axis == "patient" for axis in axes) else None


def _unsafe(item):
    temporal = item.get("temporal")
    if item.get("unverified") is True or item.get("negated") is True:
        return True
    if isinstance(temporal, dict) and (temporal.get("unknown") is True or temporal.get("negated") is True
                                       or temporal.get("period") not in (None, "current", "past")):
        return True
    return bool(_RISK.search(str(item.get("quote") or "")))


def _day(value):
    if type(value) not in (int, float):
        return None
    try:
        if not math.isfinite(value):
            return None
        return datetime.fromtimestamp(value, _JST).date()
    except (ValueError, OverflowError, OSError):
        return None


def _supported_vital_list(quote, wanted, condition, date_kind):
    """Accept only an explicit patient/date/measurement and a bounded vital list."""
    key = _VITAL_NAMES.get(wanted["observation_name"].casefold())
    if key is None or date_kind != "measured_on":
        return None
    if len(quote) > 1000:
        return False
    prefix = re.match(r"\s*(?:ご本人|本人|患者本人|患者さん|患者様|利用者本人|利用者さん|利用者様)"
                      r"(?:は|が)\s*" + re.escape(wanted[date_kind])
                      + r"\s*(?:(?:に|の)\s*(?:測定|計測)(?:した|の)"
                        r"|の\s*(?:測定|計測))\s*", quote)
    if prefix is None:
        return None
    items, seen, offset, trailing_flow = [], set(), prefix.end(), False
    while len(items) < 6:
        match = _VITAL_LIST_ITEM.match(quote, offset)
        if match is None:
            match = _BLOOD_PRESSURE_LIST_ITEM.match(quote, offset)
            if match is None:
                match = _TRAILING_OXYGEN_FLOW.match(quote, offset)
                if (match is None or "spo2" not in seen
                        or not re.fullmatch(r"\s*[。．]?\s*", quote[match.end():])):
                    return False
                # A bare flow at the end has no proven relationship to SpO2.
                item_key, trailing_flow = "oxygen_flow", True
            else:
                # Consume a complete, explicitly labelled pair without projecting
                # it as a separate observation or borrowing either side's value.
                item_key = "bp"
        else:
            item_key = _VITAL_NAMES.get(match["name"].casefold())
            if (item_key is None or match["unit"] not in _VITAL_UNITS[item_key]
                    or match["condition"] is not None and item_key != "spo2"):
                return False
        if item_key in seen:
            return False
        seen.add(item_key)
        items.append((item_key, match))
        offset = match.end()
        ending = re.match(r"\s*[。．]?\s*$", quote[offset:])
        if ending:
            break
        separator = re.match(r"\s*[、，,]\s*", quote[offset:])
        if separator is None:
            return False
        offset += separator.end()
    else:
        return False
    if len(items) < 2 and items[0][0] not in ("bp", "oxygen_flow") and not items[0][1]["condition"]:
        return None
    if trailing_flow and key == "spo2":
        return False
    target = next((m for item_key, m in items if item_key == key), None)
    if (target is None or target["name"].casefold() != wanted["observation_name"].casefold()
            or target["value"] != wanted["value"] or target["unit"] != wanted["unit"]
            or target["condition"] != condition):
        return False
    value = float(target["value"].replace("．", "."))
    hits = [m for m in _vital_numbers(quote) if _nearest_vital_label(quote, *m.span()) == key
            and not _FLOW_AFTER.match(quote, m.end())]
    return (len(hits) == 1 and float(hits[0][0].replace("．", ".")) == value
            and grounded_vitals(quote, {key: value}) == {key: value})


def _supported_item(item, values, date_kind):
    """Require one source quote to bind the person, name, value, unit and time."""
    quote = item.get("quote")
    if not isinstance(quote, str) or _OTHER.search(quote):
        return False
    wanted = {field: next(iter(values[field])) for field in ("observation_name", "value", "unit", date_kind)}
    condition = next(iter(values["condition"])) if values["condition"] else None
    if condition is not None:
        wanted["condition"] = condition
    details = item.get("details") or []
    if any(not any(isinstance(d, dict) and d.get("key") == key and d.get("value") == value
                       and isinstance(d.get("evidence"), str) and d["evidence"] in quote
                       for d in details) for key, value in wanted.items()):
        return False
    listed = _supported_vital_list(quote, wanted, condition, date_kind)
    if listed is not None:
        return listed
    if condition is None and _CONDITION_CUE.search(quote):
        return False
    person = _PATIENT.search(quote)
    at = quote.find(wanted["observation_name"])
    if person is None or at < person.end():
        return False
    start = max(quote.rfind(mark, 0, at) for mark in "。！？!?;；\r\n") + 1
    end = next((pos for pos in range(at, len(quote)) if quote[pos] in "。！？!?;；\r\n"), len(quote))
    clause = quote[start:end]
    person = _PATIENT.search(clause)
    if (person is None or clause.find(wanted["observation_name"]) < person.end()
            or any(value not in clause for value in wanted.values())):
        return False
    if condition is not None:
        marker = r"(?:採血|検査|測定|計測|観測|観察)"
        # shortcut: unlisted conditions stay unconfirmed, expand only with source-bound grammar.
        if not re.fullmatch(r"(?:空腹|食前|食後|安静|起立|臥位|坐位|座位|室内気|酸素投与)"
                            r"(?:時|下)?(?:の)?(?:採血|検査|測定|計測|観測|観察)?", condition):
            return False
        link = r"(?:時)?(?:の|に|で)?" + marker if not re.search(marker + r"$", condition) else ""
        if not re.search(re.escape(condition) + link + r"(?:の結果は|した|の|は|が)?\s*"
                         + re.escape(wanted["observation_name"]), clause, re.I):
            return False
    if re.fullmatch(r"\d+(?:[.．]\d+)?", wanted["value"]):
        numbers = re.findall(r"(?<![\d.．])\d+(?:[.．]\d+)?\s*" + re.escape(wanted["unit"]), clause)
        if len(numbers) != 1:
            return False
    key = _VITAL_NAMES.get(wanted["observation_name"].casefold())
    if key is not None:
        day = re.escape(wanted[date_kind])
        marker = r"(?:測定|計測|観測|観察)"
        name = re.escape(wanted["observation_name"])
        date_first = day + r"\s*(?:に|の)?\s*" + marker + r"(?:した|の)?\s*" + name
        value_first = (day + r"\s*(?:に|の)\s*" + name + r"\s*" + re.escape(wanted["value"])
                       + r"\s*" + re.escape(wanted["unit"]) + r"\s*を?\s*" + marker)
        if not re.search(date_first + "|" + value_first, clause, re.I):
            return False
    candidate = lab_candidate(wanted["observation_name"], wanted["value"], wanted["unit"], clause,
                              subject="patient", measured_on=wanted[date_kind], condition=condition)
    if candidate["confirmation"] == "quote_supported" and candidate["measured_on"] == wanted[date_kind]:
        return True
    unit, value = wanted["unit"], wanted["value"]
    if (key is None or unit not in _VITAL_UNITS[key] or not re.fullmatch(r"\d+(?:[.．]\d+)?", value)
            or not re.search(r"測定|計測|観測|観察", clause)
            or written_day(clause) != ("ymd", written_day(wanted[date_kind])[1])):
        return False
    number = float(value.replace("．", "."))
    if not math.isfinite(number) or number > 1e20:
        return False
    hits = [m for m in _vital_numbers(clause) if _nearest_vital_label(clause, *m.span()) == key
            and not _FLOW_AFTER.match(clause, m.end())]
    return (len(hits) == 1 and float(hits[0][0].replace("．", ".")) == number
            and clause[hits[0].end():].lstrip().startswith(unit)
            and grounded_vitals(clause, {key: number}) == {key: number})


def _reference(item):
    """Return only bounded source identity, never an isolated clinical fragment."""
    mid, digest = item.get("message_id"), item.get("content_hash")
    posted = item.get("posted_at_ts")
    if item.get("source") == "attachment_ocr" and source_identity(item) is not None:
        return {"source": "attachment_ocr", "message_id": 0, "content_hash": digest, "posted_at_ts": None,
                **{key: item[key] for key in _SOURCE_FIELDS if key in item}}
    return {"source": "chat" if item.get("source") == "chat" else "unknown",
            "message_id": mid if type(mid) is int and mid > 0 else None,
            "content_hash": digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) else None,
            "posted_at_ts": posted if _day(posted) is not None else None}


def _whole_statement(item, source_bodies):
    """Accept only an exact unique quote covering one complete source sentence."""
    if not isinstance(source_bodies, dict):
        return False
    identity = source_identity(item)
    if identity is None:
        return False
    body = source_bodies.get(identity)
    quote = item.get("quote")
    if not isinstance(body, str) or not body or not isinstance(quote, str) or body != quote:
        return False
    # A second sentence or paragraph can change subject or negate the quoted
    # result. Only the whole source, with one final punctuation mark, is safe.
    return (re.search(r"[。！？!?;；\r\n]\s*\S", body) is None
            and re.search(r"(?<!\d)\.(?!\d)\s*\S", body) is None)


def _visible_item(item):
    """Keep source-checked fields, excluding borrowed body/reference context."""
    allowed = ("category", "text", "quote", "subject", "reported_subject", "details", "message_id",
               "content_hash", "posted_at_ts", "sender", "profession", "temporality", "discovery",
               "source", "evidence", "evidence_span", "temporal", "view_bucket", "relations", *_SOURCE_FIELDS)
    return {key: item[key] for key in allowed if key in item}


def observation_series(ev, *, source_bodies=None):
    """Preserve source-bound rows; classify only whether grouping is unambiguous.

    `source_bodies` maps (message_id or external source_key, content_hash) to full source text verified
    by the caller against the same patient and snapshot. It is never returned.
    Missing or partial originals cannot establish the subject or sentence scope.
    """
    project_id = ev.get("project_id")
    if type(project_id) is not int or project_id <= 0:
        return {"view": "observation_series", "series": [], "unconfirmed": [], "scope_valid": False,
                "scope_rejected": None, "coverage": ev.get("coverage"), "order_basis": "posting_time_within_series"}
    grouped, order, blocked_groups = {}, [], set()
    rejected = 0
    for index, item in enumerate(reversed(ev.get("items") or [])):
        if not isinstance(item, dict) or item.get("category") != "observations":
            continue
        if item.get("project_id") is not None and item["project_id"] != project_id:
            rejected += 1
            continue
        mid, identity = item.get("message_id"), item.get("content_hash")
        heading, subject = item.get("_heading_group"), _subject(item)
        # Heading groups are produced only after the input's unique source spans
        # were checked. Unowned assertions are never joined by posting identity.
        has_heading = (item.get("source") == "chat" and type(mid) is int and mid > 0
                and isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity)
                and type(heading) is int and heading >= 0)
        heading_key = ("heading", mid, identity, heading)
        if has_heading and (subject != "patient" or _unsafe(item)
                            or paired_date_conflict(str(item.get("quote") or ""), item.get("posted_at_ts"))):
            blocked_groups.add(heading_key)
        if has_heading and subject == "patient":
            key = heading_key
        else:
            key = ("single", index)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(item)

    series, unconfirmed, identities = [], [], {}
    for key in order:
        items = grouped[key]
        values = {field: set() for field in _FIELDS}
        for item in items:
            for detail in item.get("details") or []:
                if (isinstance(detail, dict) and detail.get("key") in values
                        and isinstance(detail.get("value"), str) and detail["value"].strip()):
                    values[detail["key"]].add(detail["value"])
        date_fields = [field for field in ("measured_on", "observed_on", "last_confirmed_on") if values[field]]
        date_kind = date_fields[0] if len(date_fields) == 1 and len(values[date_fields[0]]) == 1 else None
        date_text = next(iter(values[date_kind])) if date_kind else None
        parsed = written_day(date_text) if date_text else None
        day = parsed[1] if parsed and parsed[0] == "ymd" else None
        posted = _day(items[0].get("posted_at_ts"))
        conflicted = any(paired_date_conflict(str(item.get("quote") or ""), item.get("posted_at_ts"))
                         for item in items)
        valid = (key not in blocked_groups
                 and all(_subject(item) == "patient" and not _unsafe(item) and source_identity(item) is not None
                     and isinstance(item.get("quote"), str) and item["quote"] for item in items)
                 and all(len(values[field]) == 1 for field in ("observation_name", "value", "unit"))
                 and len(values["condition"]) <= 1
                 and date_kind in ("measured_on", "observed_on") and day is not None
                 and (items[0].get("source") == "attachment_ocr" or posted is not None and day <= posted)
                 and not conflicted
                 and all(_whole_statement(item, source_bodies) for item in items)
                 and all(_supported_item(item, values, date_kind) for item in items))
        if not valid:
            references = []
            for item in items:
                reference = _reference(item)
                if reference not in references:
                    references.append(reference)
            # The existing timeline retains the complete source context; do not
            # expose a fragment that could omit the person or a negation.
            unconfirmed.append({"references": references, "relation": "unknown"})
            continue
        record = {"items": [_visible_item(item) for item in items], "event_date_kind": date_kind, "event_date_text": date_text,
                  "posted_at_ts": items[0].get("posted_at_ts"), "relation": "as_written"}
        condition = next(iter(values["condition"])) if values["condition"] else None
        # Missing recorded conditions do not prove that two observations had
        # the same condition, so retain each source as its own series.
        separate = None if condition is not None else (*source_identity(items[0]), key)
        identity = (next(iter(values["observation_name"])), next(iter(values["unit"])), condition, date_kind, separate)
        if identity not in identities:
            identities[identity] = len(series)
            series.append({"subject": "patient", "name": identity[0], "unit": identity[1],
                           "condition": identity[2], "condition_status": "recorded" if condition else "not_recorded",
                           "event_date_kind": identity[3], "records": []})
        series[identities[identity]]["records"].append(record)
    has_ocr = False
    for group in series:
        if any(item.get("source") == "attachment_ocr" for record in group["records"] for item in record["items"]):
            has_ocr = True
            group["records"].sort(key=lambda record: written_day(record["event_date_text"])[1])
    return {"view": "observation_series", "series": series, "unconfirmed": unconfirmed,
            "scope_valid": True, "scope_rejected": rejected,
            "coverage": ev.get("coverage"), "order_basis": "event_day_within_series" if has_ocr else "posting_time_within_series"}
