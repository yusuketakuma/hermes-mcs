"""Project source-bound, explicitly attributed intervention records without inferring ownership."""

from datetime import datetime, timezone, timedelta
import hashlib
import re

from clinical_values import written_day
from mcs_util import html_to_text, locate_quote_span, valid_hash

_JST = timezone(timedelta(hours=9))
_ROLES = {
    "医師": "physician", "看護師": "nurse", "訪問看護師": "nurse",
    "薬剤師": "pharmacist", "歯科医師": "dentist", "歯科衛生士": "dental_hygienist",
    "理学療法士": "physical_therapist", "作業療法士": "occupational_therapist",
    "言語聴覚士": "speech_therapist", "管理栄養士": "dietitian",
    "介護福祉士": "care_worker", "介護職員": "care_worker",
    "介護支援専門員": "care_manager", "ケアマネジャー": "care_manager", "相談員": "social_worker",
}
_ROLE_PATTERN = "|".join(sorted(map(re.escape, _ROLES), key=len, reverse=True))
_DATE_PATTERN = r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?"
_TARGET_PATTERN = r"(?:本人|患者|利用者)(?:への|を|に|へ)"
_RECORD = re.compile(
    rf"(?P<role>{_ROLE_PATTERN})(?:さん)?(?:が|は)[ \t　]*"
    rf"(?:(?P<date_first>{_DATE_PATTERN})に[ \t　]*{_TARGET_PATTERN}|"
    rf"{_TARGET_PATTERN}[ \t　]*(?P<date_last>{_DATE_PATTERN})に)[ \t　]*"
    r"(?P<action>訪問|往診|診察|診療|看護|介護|リハビリ|服薬指導|薬剤確認|栄養指導|口腔ケア|相談|確認|検査|採血)"
    r"(?P<result>する予定です|する予定|を予定しています|予定です|"
    r"を中止しました|を取消しました|を取り消しました|を実施しました|"
    r"を完了しました|しました|を行いました)[。]?")
_TARGET_CANCEL = re.compile(
    rf"(?P<role>{_ROLE_PATTERN})(?:さん)?(?:が|は)[ \t　]*"
    rf"(?P<target_date>{_DATE_PATTERN})(?P<date_particle>の|に)[ \t　]*{_TARGET_PATTERN}[ \t　]*"
    r"(?P<action>訪問|往診|診察|診療|看護|介護|リハビリ|服薬指導|薬剤確認|栄養指導|口腔ケア|相談|確認|検査|採血)"
    r"(?P<link>を|する予定を)(?:中止し|取消し|取り消し)ました[。]?")
_CATEGORIES = frozenset(("followup", "plan_goals", "care_services", "orders_documents", "care_team"))
_SENTENCE = re.compile(r"[^\n。！？!?]+[。！？!?]?")
_ROLE_MENTION = re.compile(_ROLE_PATTERN)
_ACTION_MENTION = re.compile(r"訪問|往診|診察|診療|看護|介護|リハビリ|服薬指導|薬剤確認|栄養指導|口腔ケア|相談|確認|検査|採血")
_MAX_BODY_CHARS = 20000
_MAX_HTML_CHARS = 80000


def _has_independent_action(quote):
    """Do not count an action word that is only part of a profession name."""
    roles = [match.span() for match in _ROLE_MENTION.finditer(quote)]
    return bool(roles and any(not any(start < action.end() and action.start() < end
                                      for start, end in roles)
                              for action in _ACTION_MENTION.finditer(quote)))


def _posted_day(value):
    if type(value) not in (int, float):
        return None
    try:
        return datetime.fromtimestamp(value, _JST).date()
    except (ValueError, OverflowError, OSError):
        return None


def _bound_span(body, quote, explicit):
    if not isinstance(body, str) or not isinstance(quote, str) or not quote or len(quote) > 4000:
        return None
    if explicit is not None:
        if (not isinstance(explicit, (tuple, list)) or len(explicit) != 2
                or any(type(v) is not int for v in explicit)):
            return None
        start, end = explicit
        return (start, end) if 0 <= start < end <= len(body) and body[start:end] == quote else None
    # Only an unambiguous, literally equal quote is eligible.
    span = locate_quote_span(body, quote)
    return span if span and body[span[0]:span[1]] == quote else None


def project(project_id, items, *, coverage=None):
    """Read caller-validated current assertions and return records plus coverage.

    The caller supplies each current source body from the same patient-scoped
    snapshot as the assertion and is responsible for current-generation and
    source-binding checks. Source bodies are transient and never returned.
    """
    if type(project_id) is not int or project_id <= 0 or not isinstance(items, (list, tuple)):
        raise ValueError("invalid_intervention_input")
    records = []
    counts = {"candidates": 0, "unbound": 0, "foreign": 0, "deleted": 0, "unknown": 0}
    for item in items:
        if not isinstance(item, dict) or item.get("category") not in _CATEGORIES:
            continue
        counts["candidates"] += 1
        source_project = item.get("project_id")
        if type(source_project) is not int or source_project != project_id:
            counts["foreign"] += 1
            continue
        if item.get("body_state") == "deleted":
            counts["deleted"] += 1
            continue
        mid = item.get("message_id")
        digest = item.get("content_hash")
        quote = item.get("quote") or item.get("evidence")
        span = _bound_span(item.get("source_body"), quote, item.get("evidence_span"))
        valid_source = (type(mid) is int and mid > 0 and valid_hash(digest)
                        and item.get("source", "chat") == "chat" and item.get("body_state") == "full")
        bound = bool(valid_source and span)
        record = {"project_id": project_id, "message_id": mid if valid_source else None,
                  "content_hash": digest if valid_source else None, "span": list(span) if bound else None,
                  "quote": quote if bound else None, "role": None, "role_text": None,
                  "action": None, "date_text": None, "event_day": None, "state": "unknown"}
        if not bound:
            counts["unbound"] += 1
        else:
            target_cancel = _TARGET_CANCEL.fullmatch(quote.strip())
            # 「日の...を中止」and「日に...する予定を中止」bind the date
            # to the planned intervention, not to the cancellation itself.
            if target_cancel and not (
                    target_cancel.group("link") == "する予定を"
                    or target_cancel.group("date_particle") == "の"):
                target_cancel = None
            match = _RECORD.fullmatch(quote.strip())
            date_text = (match.group("date_first") or match.group("date_last")) if match else None
            day = written_day(date_text)
            posted = _posted_day(item.get("posted_at_ts"))
            if (target_cancel and item.get("subject") == "patient" and item.get("unverified") is False
                    and posted is not None):
                date_text = target_cancel.group("target_date")
                target_day = written_day(date_text)
                if target_day and target_day[0] == "ymd":
                    role = target_cancel.group("role")
                    record.update(role=_ROLES[role], role_text=role, action=target_cancel.group("action"),
                                  date_text=date_text, target_day=target_day[1].isoformat(),
                                  date_basis="cancelled_intervention", state="cancelled")
            elif (match and item.get("subject") == "patient" and item.get("unverified") is False
                    and day and day[0] == "ymd" and posted is not None):
                result_text = match.group("result")
                state = ("planned" if "予定" in result_text else
                         "cancelled" if any(word in result_text for word in ("中止", "取消", "取り消し")) else
                         "completed")
                if (state == "planned" and day[1] >= posted or
                        state in ("completed", "cancelled") and day[1] <= posted):
                    role = match.group("role")
                    record.update(role=_ROLES[role], role_text=role, action=match.group("action"),
                                  date_text=date_text,
                                  event_day=None if state == "cancelled" else day[1].isoformat(), state=state)
                    if state == "cancelled":
                        record["date_basis"] = "cancellation_or_intervention_unclear"
        if record["state"] == "unknown":
            counts["unknown"] += 1
            # A fragment can omit a negation or subject in its context.
            record["quote"] = None
        records.append(record)
    complete = coverage.get("source_complete") if isinstance(coverage, dict) else None
    return {"project_id": project_id, "records": records,
            "coverage": {**counts, "upstream_source_complete": complete if type(complete) is bool else None,
                         "source_validation": "caller_required"}}


def read_source_schedule(db, project_id, *, limit=200, max_records=200):
    """Read a bounded set of original chat assertions on the caller's snapshot.

    Dates and professions are accepted only when an entire sentence follows the
    explicit grammar. These are source statements, not inferred completion or
    links between separate messages. The caller owns access and snapshot scope.
    """
    if (type(project_id) is not int or project_id <= 0 or type(limit) is not int
            or not 1 <= limit <= 1000 or type(max_records) is not int
            or not 1 <= max_records <= 1000):
        raise ValueError("invalid_intervention_input")
    coverage = {"state": "unknown", "fetch_state": "unknown", "messages_examined": 0,
                "incomplete": 0, "deleted": 0, "invalid_source": 0, "oversize": 0,
                "truncated": False, "records_truncated": False,
                "source_complete": None, "extraction": "not_assessed",
                "relations": "not_assessed", "recognition": "exact_sentences_only"}
    result = {"project_id": project_id, "records": [], "coverage": coverage}
    needed = {"patients": {"project_id", "fetch_state", "fetch_reason", "is_archived"},
              "messages": {"project_id", "message_id", "body_html", "body_text", "body_state",
                           "content_hash", "posted_at_ts"}}
    for table, required in needed.items():
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if not required <= columns:
            coverage["state"] = "schema_unknown"
            return result
    rows = db.execute(
        "SELECT p.fetch_state,p.fetch_reason,p.is_archived,m.message_id,m.body_state,"
        "m.content_hash,m.posted_at_ts,length(m.body_text),length(m.body_html),"
        "CASE WHEN length(m.body_text)<=? AND length(m.body_html)<=? THEN m.body_text END,"
        "CASE WHEN length(m.body_text)<=? AND length(m.body_html)<=? THEN m.body_html END "
        "FROM patients p LEFT JOIN messages m ON m.project_id=p.project_id "
        "WHERE p.project_id=? ORDER BY m.message_id DESC LIMIT ?",
        (_MAX_BODY_CHARS, _MAX_HTML_CHARS, _MAX_BODY_CHARS, _MAX_HTML_CHARS, project_id, limit + 1)).fetchall()
    if not rows:
        coverage["state"] = "scope_unknown"
        return result
    fetch_state, reason, archived = rows[0][:3]
    if archived != 0 or reason in ("forbidden", "permission_denied", "access_revoked"):
        coverage["state"] = "scope_unavailable"
        return result
    if fetch_state in ("complete", "incomplete", "failed", "pending", "forbidden"):
        coverage["fetch_state"] = fetch_state
    if fetch_state == "forbidden":
        coverage["state"] = "scope_unavailable"
        return result
    coverage["state"] = "observed"
    coverage["truncated"] = len(rows) > limit
    items = []
    for _, _, _, mid, body_state, digest, posted, body_len, html_len, body, html in rows[:limit]:
        if mid is None:
            continue
        coverage["messages_examined"] += 1
        if body_state == "deleted":
            coverage["deleted"] += 1
            continue
        if body_state != "full":
            coverage["incomplete"] += 1
            continue
        if ((type(body_len) is int and body_len > _MAX_BODY_CHARS)
                or (type(html_len) is int and html_len > _MAX_HTML_CHARS)):
            coverage["oversize"] += 1
            continue
        if (type(mid) is not int or mid <= 0 or not valid_hash(digest)
                or not isinstance(body, str) or not isinstance(html, str)
                or hashlib.sha256(html.encode()).hexdigest() != digest or html_to_text(html) != body):
            coverage["invalid_source"] += 1
            continue
        sentences = [match for match in _SENTENCE.finditer(body) if match.group().strip()]
        for match in sentences:
            quote = match.group()
            if not _has_independent_action(quote):
                continue
            if len(items) >= max_records:
                coverage["records_truncated"] = True
                break
            # A neighbouring sentence may retract, quote, condition or change
            # the subject of this one. Sentence boundaries alone do not prove
            # independence, so only an isolated assertion can be classified.
            explicit = (len(sentences) == 1 and (_RECORD.fullmatch(quote.strip()) is not None
                        or _TARGET_CANCEL.fullmatch(quote.strip()) is not None))
            items.append({"project_id": project_id, "message_id": mid, "content_hash": digest,
                          "category": "followup", "subject": "patient" if explicit else "unspecified",
                          "body_state": "full", "source": "chat", "source_body": body,
                          "posted_at_ts": posted, "quote": quote,
                          "evidence_span": [match.start(), match.end()], "unverified": not explicit})
        if coverage["records_truncated"]:
            break
    projected = project(project_id, items)
    result["records"] = [{**record, "assertion_only": True} for record in projected["records"]]
    coverage["candidates"] = projected["coverage"]["candidates"]
    coverage["unknown"] = projected["coverage"]["unknown"]
    return result
