"""Group grounded written observations without inferring a current clinical value."""
from datetime import datetime, timedelta, timezone
import math
import re

from clinical_values import lab_candidate, written_day
from patient_context_state import paired_date_conflict

_FIELDS = ("observation_name", "value", "unit", "condition", "measured_on", "observed_on", "last_confirmed_on")
_RISK = re.compile(r"未確認|未確定|不明|疑い|かもしれ|未測定|未実施|未検査|ない|なし|無し|なく|"
                   r"ありません|認めず|認めません|否定|せず|予定|計画|見込み|明日|翌日")
_JST = timezone(timedelta(hours=9))
_PATIENT = re.compile(r"(?:ご本人|本人|患者(?:本人|さん|様)?|利用者(?:本人|さん|様)?)(?:の|は|が|を|に|へ|も|、|，|:|：|\s|$)")
_OTHER = re.compile(r"家族|母|父|他者|友人|夫|妻|息子|娘|配偶者|同居人")


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


def _supported_item(item, values, date_kind):
    """Require one source quote to bind the person, name, value, unit and time."""
    quote = item.get("quote")
    if not isinstance(quote, str) or _OTHER.search(quote):
        return False
    wanted = {field: next(iter(values[field])) for field in
              ("observation_name", "value", "unit", "condition", date_kind)}
    details = item.get("details") or []
    if any(not any(isinstance(d, dict) and d.get("key") == key and d.get("value") == value
                       and isinstance(d.get("evidence"), str) and d["evidence"] in quote
                       for d in details) for key, value in wanted.items()):
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
    candidate = lab_candidate(wanted["observation_name"], wanted["value"], wanted["unit"], clause,
                              subject="patient", measured_on=wanted[date_kind], condition=wanted["condition"])
    return (candidate["confirmation"] == "quote_supported"
            and candidate["measured_on"] == wanted[date_kind])


def _reference(item):
    """Return only bounded source identity, never an isolated clinical fragment."""
    mid, digest = item.get("message_id"), item.get("content_hash")
    posted = item.get("posted_at_ts")
    return {"source": "chat" if item.get("source") == "chat" else "unknown",
            "message_id": mid if type(mid) is int and mid > 0 else None,
            "content_hash": digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) else None,
            "posted_at_ts": posted if _day(posted) is not None else None}


def _whole_statement(item, source_bodies):
    """Accept only an exact unique quote covering one complete source sentence."""
    if not isinstance(source_bodies, dict):
        return False
    mid, digest = item.get("message_id"), item.get("content_hash")
    if type(mid) is not int or mid <= 0 or not isinstance(digest, str):
        return False
    body = source_bodies.get((mid, digest))
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
               "source", "evidence", "evidence_span", "temporal", "view_bucket", "relations")
    return {key: item[key] for key in allowed if key in item}


def observation_series(ev, *, source_bodies=None):
    """Preserve source-bound rows; classify only whether grouping is unambiguous.

    `source_bodies` maps (message_id, content_hash) to full source text verified
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
                 and all(_subject(item) == "patient" and not _unsafe(item) and item.get("source") == "chat"
                     and type(item.get("message_id")) is int and item["message_id"] > 0
                     and isinstance(item.get("content_hash"), str)
                     and re.fullmatch(r"[0-9a-f]{64}", item["content_hash"])
                     and isinstance(item.get("quote"), str) and item["quote"] for item in items)
                 and all(len(values[field]) == 1 for field in ("observation_name", "value", "unit", "condition"))
                 and date_kind in ("measured_on", "observed_on") and day is not None and posted is not None
                 and day <= posted and not conflicted
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
        identity = (*tuple(next(iter(values[field])) for field in ("observation_name", "unit", "condition")),
                    date_kind)
        if identity not in identities:
            identities[identity] = len(series)
            series.append({"subject": "patient", "name": identity[0], "unit": identity[1],
                           "condition": identity[2], "event_date_kind": identity[3], "records": []})
        series[identities[identity]]["records"].append(record)
    return {"view": "observation_series", "series": series, "unconfirmed": unconfirmed,
            "scope_valid": True, "scope_rejected": rejected,
            "coverage": ev.get("coverage"), "order_basis": "posting_time_within_series"}
