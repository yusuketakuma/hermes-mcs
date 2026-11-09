"""Project stored care-event records to source references without inventing dates."""
import re
from datetime import datetime, timedelta, timezone

import clinical_events
from clinical_values import written_day
from mcs_util import valid_hash
from patient_context_projection import paired_date_conflict
from stats_math import finite_number


_DATED_EVENT = re.compile(
    r"(?<!\d)(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)(?!\d)\s*"
    r"(?:[（(]\s*[月火水木金土日](?:曜(?:日)?)?\s*[）)]\s*)?に\s*"
    r"(?:本人(?:は|が)|患者(?:は|が)|家族(?:は|が))?\s*"
    r"(入院|退院|出院|退所|転院|転棟|転所|転倒|転落|訪問|往診|来訪|受診|検査|採血)"
)
_EVENT_KIND = {
    "入院": "admission", "退院": "discharge", "出院": "discharge", "退所": "discharge",
    "転院": "transfer", "転棟": "transfer", "転所": "transfer",
    "転倒": "fall", "転落": "fall", "訪問": "visit", "往診": "visit", "来訪": "visit",
    "受診": "exam", "検査": "exam", "採血": "exam",
}
_JST = timezone(timedelta(hours=9))
_INTERVENTION_STATES = {"intervention_start": "started", "intervention_pause": "paused",
                        "intervention_resume": "resumed", "intervention_end": "ended"}


def intervention_states(events, coverage):
    """Derive last source-proved transitions by provider, profession and service."""
    groups, uncertain = {}, []
    for event in events:
        detail = event.get("care_detail") or {}
        transition = _INTERVENTION_STATES.get(detail.get("event_type"))
        if (transition is None or event.get("subject") != "patient"
                or event.get("negated") is not False or event.get("unverified") is not False
                or event.get("status") not in ("current", "past")):
            continue
        provider = event.get("service_provider") or {}
        professions = provider.get("professions") or []
        facility, service = provider.get("label"), event.get("service")
        known = (provider.get("state") == "matched" and isinstance(facility, str) and facility
                 and len(professions) == 1 and isinstance(professions[0], str)
                 and professions[0] and service and event.get("event_day")
                 and not event.get("date_uncertain")
                 and detail.get("workflow_status") in ("performed", "done", "in_progress"))
        if not known:
            uncertain.append((facility, tuple(professions) if all(professions) else (), service))
            continue
        key = facility, professions[0], service
        groups.setdefault(key, []).append((event["event_day"], transition, event))
    rows = []
    for (facility, profession, service), records in sorted(groups.items()):
        latest_day = max(record[0] for record in records)
        latest = [record for record in records if record[0] == latest_day]
        transitions = {record[1] for record in latest}
        ambiguous = any((other_facility is None or other_facility == facility)
                        and (not professions or profession in professions)
                        and (other_service is None or other_service == service)
                        for other_facility, professions, other_service in uncertain)
        state = next(iter(transitions)) if len(transitions) == 1 and not ambiguous else "unknown"
        rows.append({"facility": facility, "profession": profession, "service": service,
                     "recorded_state": state, "event_day": latest_day,
                     "current_state": "unknown", "history_complete": coverage.get("source_complete") is True,
                     "references": [{key: event.get(key) for key in (
                         "message_id", "artifact_id", "record_index", "content_hash", "span")}
                         for _, _, event in latest]})
    return {"items": rows, "unresolved_records": len(uncertain),
            "scope": "patient_profession_facility_service", "basis": "explicit_recorded_transitions",
            "history_complete": coverage.get("source_complete") is True}


def _posted(value):
    if not finite_number(value):
        return None
    try:
        return datetime.fromtimestamp(value, _JST)
    except (ValueError, OverflowError, OSError):
        return None


def _event_day(record, quote):
    if record.get("unverified") or re.search(r"本日|今日|昨日|明日|翌日", quote):
        return None
    detail = record.get("care_detail")
    # A bound care-detail date applies to every kind — intervention_start
    # also rides on legacy visit/care records — but a detail day that
    # contradicts the legacy dated-event day is ambiguity, not a fallback.
    detail_day = (clinical_events.care_detail_event_day(detail, quote)
                  if isinstance(detail, dict) else None)
    # The legacy day comes from the dated-event mention of this record's
    # own kind, never from the quote's whole date list: unrelated dates
    # elsewhere in the quote must neither erase it nor hide a mismatch.
    days, unclear = set(), False
    for match in _DATED_EVENT.finditer(quote):
        if _EVENT_KIND[match.group(2)] != record["kind"]:
            continue
        day = written_day(match.group(1))
        if day is None or day[0] != "ymd":
            unclear = True
        else:
            days.add(day[1].isoformat())
    if unclear or len(days) > 1:
        # The parent instance's own date binding is unclear or
        # contradictory — it stays unknown instead of guessing.
        return None
    legacy_day = days.pop() if days else None
    if detail_day is not None and legacy_day is not None \
            and detail_day != legacy_day:
        return None
    return detail_day if detail_day is not None else legacy_day


def project(project_id, message, artifact_id, document):
    """Project one caller-validated current artifact for one source on its borrowed snapshot.

    The caller owns current-generation, source-binding and snapshot checks. Records
    returned here reference the original instead of copying its quote or body.
    Coverage counts refer only to this artifact; date_unknown_count counts returned
    records without a reliable event date, independently of extraction completeness.
    """
    def result(state, records=None, invalid=0, unverified=0):
        return {"events": records or [], "coverage": {"state": state,
                "records": None if records is None else len(records),
                "invalid": invalid, "unverified": unverified,
                "date_unknown_count": None if records is None else sum(
                    event["event_day"] is None or event["date_uncertain"] for event in records)}}

    if (type(project_id) is not int or project_id <= 0 or not isinstance(message, dict)
            or type(message.get("project_id")) is not int or message.get("project_id") != project_id
            or type(message.get("message_id")) is not int
            or message["message_id"] <= 0):
        return result("invalid_source")
    if message.get("body_state") == "deleted":
        return result("deleted")
    if message.get("body_state") != "full" or not isinstance(message.get("body_text"), str):
        return result("incomplete_source")
    if not valid_hash(message.get("content_hash")):
        return result("invalid_source")
    if document is None:
        return result("not_extracted")
    if type(artifact_id) is not int or artifact_id <= 0 or not isinstance(document, dict):
        return result("invalid_artifact")
    if "event_records" not in document:
        return result("not_recorded")
    if not isinstance(document["event_records"], list):
        return result("invalid_records")
    body = message["body_text"]
    posted = _posted(message.get("posted_at_ts"))
    events, invalid, unverified = [], 0, 0
    for index, item in enumerate(document["event_records"]):
        record = clinical_events.validate_record(item, body)
        if record is None:
            invalid += 1
            continue
        span = item.get("span")
        quote = item.get("evidence")
        bound = (isinstance(span, dict) and type(span.get("start")) is int
                 and type(span.get("end")) is int and 0 <= span["start"] < span["end"] <= len(body)
                 and isinstance(quote, str) and body[span["start"]:span["end"]] == quote)
        unresolved = (not bound or record.get("unverified") is True
                      or not clinical_events.context_clean(
                          body, span["start"], span["end"], kind=record["kind"],
                          subject=record["subject"], status=record["status"],
                          negated=record["negated"],
                          detail=record.get("care_detail")))
        date_uncertain = bound and paired_date_conflict(quote, message.get("posted_at_ts"))
        event_day = None if unresolved or date_uncertain else _event_day(record, quote)
        reported_status = record["status"]
        if (event_day is not None and reported_status == "current" and record["negated"] is False
                and (posted is None or event_day > posted.date().isoformat())):
            date_uncertain = True
        status = ("unknown" if unresolved or (date_uncertain and reported_status == "current"
                                              and record["negated"] is False) else reported_status)
        unverified += unresolved
        event = {"project_id": project_id, "message_id": message["message_id"],
                       "artifact_id": artifact_id, "record_index": index,
                       "content_hash": message["content_hash"],
                       "span": [span["start"], span["end"]] if bound else None,
                       "kind": record["kind"], "subject": "unknown" if unresolved else record["subject"],
                       "status": status, "reported_status": reported_status,
                       "negated": None if unresolved else record["negated"],
                       "unverified": unresolved,
                       "date_uncertain": date_uncertain,
                       "event_day": event_day,
                       "posted_at": posted.isoformat() if posted else None,
                       "posted_at_ts": message["posted_at_ts"] if posted else None}
        if not unresolved and isinstance(record.get("care_detail"), dict):
            event["care_detail"] = record["care_detail"]
        events.append(event)
    return result("incomplete" if invalid or unverified else "complete", events, invalid, unverified)
