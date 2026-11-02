"""Project source-bound measurements without treating posting order as measurement time."""
from datetime import date, datetime, timedelta, timezone
import math
import re
import unicodedata

from clinical_scope import FAMILY_PERSON, OTHER_PERSON, source_assertion_span, source_axes
from clinical_values import (
    _VITAL_CLASS, _nearest_vital_label, _vital_numbers, _bp_number,
    fold_surface, grounded_vitals, lab_analyte, lab_candidate, patient_item_scope, written_day,
)
from patient_context_projection import paired_date_conflict


_OTHER_PERSON_CUE = re.compile(rf"(?:{FAMILY_PERSON}|{OTHER_PERSON})(?:さん|様)?(?:の|に|は|が|も|を)")
_OTHER_TOPIC = re.compile(rf"(?:{FAMILY_PERSON}|{OTHER_PERSON})(?:本人|自身)?"
                          r"(?:の(?:話|件|こと|記録|情報|状態)|について)")


def ambiguous_patient_context(body, target):
    """Hold a bare patient reference when an earlier topic names another person."""
    return (isinstance(body, str) and type(target) is int and 0 <= target < len(body)
            and _OTHER_TOPIC.search(body[:target]) is not None)


def _posted_day(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed.astimezone(timezone(timedelta(hours=9))).date()
                if parsed.tzinfo is not None and parsed.utcoffset() is not None else None)
    except (ValueError, OverflowError):
        return None


def _posted_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _condition_binds_measurement(body, assertion, evidence, condition, anchor, kind):
    """Require the written condition to modify the measurement, not another action."""
    if (not isinstance(condition, str) or not condition.strip() or len(condition) > 60
            or assertion is None or evidence.count(condition) != 1):
        return False
    clause = body[slice(*assertion)]
    if clause.count(condition) != 1 or clause.count(anchor) != 1:
        return False
    condition_start, anchor_start = clause.index(condition), clause.index(anchor)
    if condition_start >= anchor_start:
        return False
    between = clause[condition_start + len(condition):anchor_start]
    # An intervening verb or another condition makes the relation ambiguous.
    label = r"(?:体温|BT|脈拍|脈|心拍|HR|呼吸|RR|血圧|BP|収縮期|拡張期|SpO2|酸素飽和度|血糖|BS)?" \
        if kind == "vital" else ""
    return re.fullmatch(r"\s*(?:(?:に|の)\s*)?(?:採血|検査|測定|計測)"
                        r"(?:の|で|結果|値)?\s*" + label + r"\s*[:：=]?\s*", between,
                        re.IGNORECASE) is not None


def _candidate(record):
    """Check source binding; clinical value and subject validation belongs to the caller."""
    span, body, evidence = record.get("evidence_span"), record.get("source_body"), record.get("evidence")
    valid_span = (isinstance(body, str) and isinstance(evidence, str) and isinstance(span, (list, tuple))
                  and len(span) == 2 and all(type(n) is int for n in span)
                  and 0 <= span[0] < span[1] <= len(body) and body[span[0]:span[1]] == evidence)
    measured = record.get("measured_on")
    try:
        day = date.fromisoformat(measured) if isinstance(measured, str) else None
    except ValueError:
        day = None
    quote_day = written_day(evidence) if valid_span else None
    posted_day = _posted_day(record.get("posted_at"))
    value, unit = record.get("value"), record.get("unit")
    value_ok = (isinstance(value, str) and 0 < len(value.strip()) <= 30 or type(value) is int and abs(value) <= 10**308
                or type(value) is float and math.isfinite(value)) and (unit is None or
                isinstance(unit, str) and 0 < len(unit.strip()) <= 15)
    if isinstance(value, str) and unicodedata.normalize("NFKC", value).strip().casefold() in (
            "nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"):
        value_ok = False
    source_ref = record.get("source_ref")
    source_ok = (isinstance(source_ref, dict) and type(source_ref.get("artifact_id")) is int
                 and source_ref["artifact_id"] > 0
                 and ((record.get("kind") == "lab" and source_ref.get("field") == "labs"
                       and type(source_ref.get("index")) is int and source_ref["index"] >= 0)
                      or (record.get("kind") == "vital" and source_ref.get("field") == "vitals"
                          and "index" not in source_ref)
                      or (source_ref.get("field") == "facts" and type(source_ref.get("index")) is int
                          and source_ref["index"] >= 0 and isinstance(source_ref.get("evidence_id"), str)
                          and re.fullmatch(r"ev_[0-9a-f]{16}", source_ref["evidence_id"]) is not None)))
    verified_source = (record.get("verification") == "quote_supported" and record.get("subject") == "patient"
                       and record.get("negated") is False and record.get("status") in ("current", "past", "planned", "conditional")
                       and valid_span and value_ok and source_ok
                       and not paired_date_conflict(evidence, _posted_timestamp(record.get("posted_at"))))
    name, condition = record.get("name"), record.get("condition")
    if record.get("kind") == "lab" and (not isinstance(name, str) or not 0 < len(name.strip()) <= 40):
        verified_source = False
    if verified_source:
        surface = name if record.get("kind") == "lab" else str(value)
        target = body.find(surface, span[0], span[1]) if isinstance(surface, str) else -1
        left = max((body.rfind(mark, 0, target) for mark in "。！？!?;；\n"), default=-1) + 1
        if (target < 0 or _OTHER_PERSON_CUE.search(body[left:target])
                or ambiguous_patient_context(body, target)):
            # An unrecognized owner or recipient must not default to the patient.
            verified_source = False
    if condition is not None:
        anchor = name if record.get("kind") == "lab" else str(value)
        assertion = source_assertion_span(body, anchor) if valid_span and isinstance(anchor, str) else None
        if (not valid_span or not _condition_binds_measurement(
                body, assertion, evidence, condition, anchor, record.get("kind"))):
            verified_source = False
    comparison = (str(value), unit) if value_ok else (None, None)
    plan = record.get("status") in ("planned", "conditional")
    validation_quote = evidence
    if verified_source and plan:
        if not isinstance(name, str) or evidence.count(name) != 1:
            verified_source = False
        else:
            anchor = span[0] + evidence.index(name)
            subject, period = source_axes(body, anchor, anchor + len(name))
            marker = re.search(r"予定|計画|検討中|見込み|なら|場合|たら|とき|れば", evidence[evidence.index(name):])
            verified_source = subject == "patient" and period == record["status"] and marker is not None
            if marker is not None:
                # This is a contiguous original prefix, used only to prove the label/value relation.
                validation_quote = evidence[:evidence.index(name) + marker.start()]
    if verified_source:
        allowed_scope = ("patient", "past") if record["status"] == "past" else ("patient", record["status"]) if plan else ("patient",)
        if (record.get("kind") == "lab" and isinstance(name, str)
                and (lab_analyte(name) or fold_surface(name)) == record.get("key")):
            lab = lab_candidate(name, value, unit, validation_quote, measured_on=measured, condition=condition,
                                patient_name=record.get("patient_name"))
            comparison = (lab["value"], lab["unit"])
            verified_source = (lab["confirmation"] == "quote_supported" and lab["measured_on"] == measured
                               and patient_item_scope(record, body, patient_name=record.get("patient_name"),
                                                      surface=name) in allowed_scope)
        elif record.get("kind") == "vital" and record.get("key") in _VITAL_CLASS and type(value) in (int, float):
            vital_class = _VITAL_CLASS[record["key"]]
            clauses = re.split(r"[。！？!?;；\n]", validation_quote)
            matching = []
            for clause in clauses:
                if written_day(clause) != ("ymd", day):
                    continue
                for match in _vital_numbers(clause):
                    tail = clause[match.end():].lstrip()
                    unit_ok = (unit is None or unicodedata.normalize("NFKC", tail).casefold().startswith(
                        unicodedata.normalize("NFKC", unit).casefold()))
                    if (_bp_number(match[0]) == value and _nearest_vital_label(clause, *match.span()) == vital_class
                            and unit_ok and grounded_vitals(clause, {record["key"]: value}) == {record["key"]: value}):
                        matching.append(clause)
            verified_source = (len(matching) == 1
                               and patient_item_scope(record, body, patient_name=record.get("patient_name"),
                                                      surface=str(value)) in allowed_scope)
            comparison = (str(value), unicodedata.normalize("NFKC", unit).casefold() if unit else None)
        else:
            verified_source = False
    verified = (verified_source and day is not None and day.isoformat() == measured and quote_day == ("ymd", day)
                and posted_day is not None and day <= posted_day)
    keys = ("project_id", "message_id", "content_hash", "kind", "key", "value", "unit", "condition", "measured_on",
            "posted_at", "subject", "verification", "evidence_span", "source_ref")
    item = {key: record.get(key) for key in keys}
    item["source_ref"] = ({"artifact_id": source_ref["artifact_id"], "field": source_ref["field"],
                           **({"index": source_ref["index"]} if source_ref["field"] in ("labs", "facts") else {}),
                           **({"evidence_id": source_ref["evidence_id"]} if source_ref["field"] == "facts" else {})}
                          if source_ok else None)
    item["evidence_span"] = list(span) if valid_span else None
    item["subject"] = "patient" if verified_source else "unknown"
    item["verification"] = "quote_supported" if verified_source else "unverified"
    item["posted_at"] = record.get("posted_at") if posted_day is not None else None
    date_grounded = (verified_source and day is not None and day.isoformat() == measured
                     and quote_day == ("ymd", day) and posted_day is not None)
    item["measured_on"] = measured if date_grounded and (plan or day <= posted_day) else None
    item["condition"] = condition if verified_source else None
    item["_comparison"] = comparison
    item["_condition"] = fold_surface(condition) if verified_source and condition else None
    item["state"] = ("plan" if verified_source and record.get("status") in ("planned", "conditional")
                     else "history" if verified and record.get("status") == "past"
                     else "candidate" if verified else "unknown")
    if item["state"] == "unknown":
        item["value"] = item["unit"] = item["condition"] = None
        if record.get("kind") == "lab" and (not valid_span or not isinstance(name, str) or name not in evidence
                or (lab_analyte(name) or fold_surface(name)) != record.get("key")):
            item["key"] = None
    return item


def project_measurements(records, *, source_complete):
    """Return measurements with explicit date buckets; caller validates the clinical values.

    `source_complete` describes whether every source was examined, independent of display limits.
    Each input carries source_body and a matching evidence span; neither body nor quote is returned.
    """
    items, groups = [], {}
    excluded = invalid = 0
    for record in records:
        if not isinstance(record, dict):
            invalid += 1
            continue
        if record.get("subject") in ("family", "other"):
            excluded += 1
            continue
        if (type(record.get("project_id")) is not int or record["project_id"] <= 0
                or type(record.get("message_id")) is not int or record["message_id"] <= 0
                or record.get("kind") not in ("vital", "lab")
                or not isinstance(record.get("key"), str) or not 0 < len(record["key"]) <= 40
                or not isinstance(record.get("content_hash"), str)
                or re.fullmatch(r"[0-9a-f]{64}", record["content_hash"]) is None
                or record["kind"] == "vital" and record["key"] not in _VITAL_CLASS
                or record["kind"] == "lab" and (
                    not isinstance(record.get("name"), str) or not 0 < len(record["name"].strip()) <= 40)):
            invalid += 1
            continue
        item = _candidate(record)
        items.append(item)
        groups.setdefault((item["project_id"], item["kind"], record["key"]), []).append(item)
    incomplete = source_complete is not True or invalid > 0
    for group in groups.values():
        candidates = [item for item in group if item["state"] == "candidate"]
        if not candidates:
            continue
        measured = [item for item in group if item["state"] in ("candidate", "history")]
        signatures = {(item["_condition"], item["_comparison"][1]) for item in measured}
        comparable = (len(signatures) == 1 and all(condition and unit for condition, unit in signatures))
        if incomplete or not comparable or any(item["state"] == "unknown" for item in group):
            for item in candidates:
                # The source-bound value remains a written observation, not a current value.
                item["state"] = "observed"
            continue
        newest = max(item["measured_on"] for item in measured)
        latest = [item for item in measured if item["measured_on"] == newest]
        values = {item["_comparison"] for item in latest}
        for item in candidates:
            item["state"] = ("history" if item["measured_on"] < newest else
                             "different_records" if len(values) > 1 else "current")
    items.sort(key=lambda item: (item["project_id"], item["kind"], item["key"] or "",
                                 item["measured_on"] if isinstance(item["measured_on"], str) else "9999-99-99",
                                 item["message_id"]))
    for item in items:
        del item["_comparison"]
        del item["_condition"]
        if item["state"] == "unknown":
            item["value"] = item["unit"] = None
    unknown = sum(item["state"] == "unknown" for item in items)
    return {"items": items, "coverage": {"source_complete": source_complete is True,
            "input_count": len(records), "patient_count": len(items), "excluded_other": excluded,
            "invalid_count": invalid, "unknown_count": unknown,
            "observed_count": sum(item["state"] in ("observed", "different_records") for item in items)}}
