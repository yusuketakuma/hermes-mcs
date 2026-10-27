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
    "介護福祉士": "care_worker", "介護職員": "care_worker", "介護士": "care_worker", "ヘルパー": "care_worker",
    "介護支援専門員": "care_manager", "ケアマネジャー": "care_manager", "ケアマネ": "care_manager",
    "相談員": "social_worker",
}
_ROLE_PATTERN = "|".join(sorted(map(re.escape, _ROLES), key=len, reverse=True))
_DATE_PATTERN = r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?"
_TARGET_PATTERN = r"(?:本人|患者|利用者)(?:への|を|に|へ)"
_INTERVENTION_ACTION = r"(?:訪問|往診|診察|診療|看護|介護|リハビリ|服薬指導|薬剤確認|栄養指導|口腔ケア|相談|確認|検査|採血)"
_RECORD = re.compile(
    rf"(?P<role>{_ROLE_PATTERN})(?:さん)?(?:が|は)[ \t　]*"
    rf"(?:(?P<date_first>{_DATE_PATTERN})に[ \t　]*{_TARGET_PATTERN}|"
    rf"{_TARGET_PATTERN}[ \t　]*(?P<date_last>{_DATE_PATTERN})に)[ \t　]*"
    rf"(?P<action>{_INTERVENTION_ACTION})"
    r"(?P<result>する予定です|する予定|を予定しています|予定です|"
    r"を中止しました|を取消しました|を取り消しました|を実施しました|"
    r"を完了しました|しました|を行いました)[。]?")
_UNRESOLVED_DATE_PATTERN = r"(?:[0-9]{1,2}月[0-9]{1,2}日|[0-9]{1,2}/[0-9]{1,2}|明日|今日|本日)"
_UNRESOLVED_PLAN = re.compile(
    rf"(?P<role>{_ROLE_PATTERN})(?:さん)?(?:が|は)[ \t　]*"
    rf"(?:(?P<date_first>{_UNRESOLVED_DATE_PATTERN})(?:に)?[ \t　]*{_TARGET_PATTERN}|"
    rf"{_TARGET_PATTERN}[ \t　]*(?P<date_last>{_UNRESOLVED_DATE_PATTERN})(?:に)?)[ \t　]*"
    rf"(?P<action>{_INTERVENTION_ACTION})"
    r"(?:する予定です|する予定|を予定しています|予定です)[。]?")
# A terse source may name a possible schedule without naming its actor or
# patient. Preserve only the written mention; never turn it into an assigned
# intervention, a date, or a completion.
_SCHEDULE_DATE = rf"(?:明日|今日|本日|来週[月火水木金土日]曜日?|{_DATE_PATTERN}|[0-9]{{1,2}}/[0-9]{{1,2}}|[0-9]{{1,2}}月[0-9]{{1,2}}日)"
# A bare 確認/相談 with a date is usually the writer's own task note, not a schedule.
_SCHEDULE_ACTION = (r"(?:介護ベッド(?:を)?設置|訪問調査|訪問診療|サービス担当者会議|担当者会議|"
                    r"訪問|往診|診察|診療|看護|介護|リハビリ|服薬指導|薬剤確認|栄養指導|口腔ケア|検査|採血)")
_SCHEDULE_CUE_END = (
    r"(?:します|する予定です|する予定|を予定しています|を予定|の予定です|の予定|"
    r"予定です|予定|ですか|です|しません|かもしれません|"
    r"(?:する)?予定(?:は|を)(?:中止(?:しました|する|です)?|取消(?:しました|する|です)?|取り消(?:しました|す|し))|"
    r"(?:は|を)(?:中止(?:しました|する|です)?|取消(?:しました|する|です)?|取り消(?:しました|す|し)))?"
    r"(?=[ \t　。！？!?\n]|$)"
)
_SCHEDULE_CUE = re.compile(
    rf"{_SCHEDULE_DATE}(?:に)?[ \t　]*(?P<action>{_SCHEDULE_ACTION}){_SCHEDULE_CUE_END}")
_SCHEDULE_MENTION = re.compile(
    rf"(?P<date>{_SCHEDULE_DATE})(?:に)?[ \t　]*(?P<action>{_SCHEDULE_ACTION})"
    r"(?:します|する予定です|する予定|を予定しています|を予定|の予定です|の予定|予定です|予定|です)?[。]?")
_NEUTRAL_CONTEXT = re.compile(r"(?:連絡します|共有します|ご確認ください|ありがとうございます|以上です)[。！？!?]?")
_TARGET_CANCEL = re.compile(
    rf"(?P<role>{_ROLE_PATTERN})(?:さん)?(?:が|は)[ \t　]*"
    rf"(?P<target_date>{_DATE_PATTERN})(?P<date_particle>の|に)[ \t　]*{_TARGET_PATTERN}[ \t　]*"
    rf"(?P<action>{_INTERVENTION_ACTION})"
    r"(?P<link>を|する予定を)(?:中止し|取消し|取り消し)ました[。]?")
_CATEGORIES = frozenset(("followup", "plan_goals", "care_services", "orders_documents", "care_team"))
_SENTENCE = re.compile(r"[^\n。！？!?]+[。！？!?]?")
_ROLE_MENTION = re.compile(_ROLE_PATTERN)
_ACTION_MENTION = re.compile(_INTERVENTION_ACTION)
_MAX_BODY_CHARS = 20000
_MAX_HTML_CHARS = 80000


def _has_independent_action(quote):
    """Do not count an action word that is only part of a profession name."""
    roles = iter((match.start() - 2 if quote[max(0, match.start() - 2):match.start()] == "訪問"
                  else match.start(), match.end()) for match in _ROLE_MENTION.finditer(quote))
    current = next(roles, None)
    if current is None:
        return False
    for action in _ACTION_MENTION.finditer(quote):
        while current is not None and current[1] <= action.start():
            current = next(roles, None)
        if current is None or current[0] >= action.end():
            return True
    return False


def _has_schedule_cue(quote):
    """Find adjacent date/action mentions, excluding words inside professions."""
    roles = [(match.start(), match.end()) for match in _ROLE_MENTION.finditer(quote)]
    for match in _SCHEDULE_CUE.finditer(quote):
        start, end = match.span('action')
        if not any(left < end and start < right for left, right in roles):
            return True
    return False


def _valid_schedule_mention(text):
    match = _SCHEDULE_MENTION.fullmatch(text.strip())
    if not match:
        return None
    date = match.group('date')
    if date.startswith(('明日', '今日', '本日', '来週')) or written_day(date) is not None:
        return match
    return None


def _safe_mention_spans(body):
    """Index full dated sentences only when all adjacent sentences are safe."""
    spans = set()
    for match in _SENTENCE.finditer(body):
        text = match.group()
        if not text.strip():
            continue
        if _valid_schedule_mention(text):
            spans.add(match.span())
            left = len(text) - len(text.lstrip())
            right = len(text.rstrip())
            spans.add((match.start() + left, match.start() + right))
            if right > left and text[right - 1] == '。':
                spans.add((match.start() + left, match.start() + right - 1))
        elif not _NEUTRAL_CONTEXT.fullmatch(text.strip()):
            return set()
    return spans


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
    safe_spans_by_body = {}
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
            unresolved = _UNRESOLVED_PLAN.fullmatch(quote.strip())
            mention = _valid_schedule_mention(quote)
            date_text = (match.group("date_first") or match.group("date_last")) if match else None
            day = written_day(date_text)
            posted = _posted_day(item.get("posted_at_ts"))
            source_body = item['source_body']
            if mention and source_body not in safe_spans_by_body:
                safe_spans_by_body[source_body] = _safe_mention_spans(source_body)
            if (mention and item.get("subject") in (None, "unknown", "unspecified")
                    and span in safe_spans_by_body[source_body]):
                record.update(action=mention.group("action"), date_text=mention.group("date"),
                              state="schedule_mention_unconfirmed")
            elif (target_cancel and item.get("subject") == "patient" and item.get("unverified") is False
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
            elif (unresolved and item.get("subject") == "patient" and item.get("unverified") is False
                  and item["source_body"].strip() == quote.strip()):
                date_text = unresolved.group("date_first") or unresolved.group("date_last")
                written = written_day(date_text)
                if date_text in ("明日", "今日", "本日") or (written and written[0] == "md"):
                    role = unresolved.group("role")
                    record.update(role=_ROLES[role], role_text=role, action=unresolved.group("action"),
                                  date_text=date_text, state="planned_date_unknown")
        if record["state"] in ("unknown", "schedule_mention_unconfirmed"):
            counts["unknown"] += 1
        if record["state"] == "unknown":
            # A fragment can omit a negation or subject in its context.
            record["quote"] = None
        records.append(record)
    complete = coverage.get("source_complete") if isinstance(coverage, dict) else None
    return {"project_id": project_id, "records": records,
            "coverage": {**counts, "upstream_source_complete": complete if type(complete) is bool else None,
                         "source_validation": "caller_required"}}


def _source_items(project_id, mid, digest, body, posted, remaining, *, start_offset=0):
    """Build the same conservative sentence assertions for both readers."""
    sentences = [match for match in _SENTENCE.finditer(body) if match.group().strip()]
    if start_offset and not any(match.end() == start_offset and
                                (_has_independent_action(match.group()) or _has_schedule_cue(match.group()))
                                for match in sentences):
        raise ValueError("invalid_intervention_cursor")
    items = []
    truncated = False
    for match in sentences:
        quote = match.group()
        if match.start() < start_offset or not (_has_independent_action(quote) or _has_schedule_cue(quote)):
            continue
        if len(items) >= remaining:
            truncated = True
            break
        explicit = (len(sentences) == 1 and (_RECORD.fullmatch(quote.strip()) is not None
                    or _TARGET_CANCEL.fullmatch(quote.strip()) is not None
                    or _UNRESOLVED_PLAN.fullmatch(quote.strip()) is not None))
        items.append({"project_id": project_id, "message_id": mid, "content_hash": digest,
                      "category": "followup", "subject": "patient" if explicit else "unspecified",
                      "body_state": "full", "source": "chat", "source_body": body,
                      "posted_at_ts": posted, "quote": quote,
                      "evidence_span": [match.start(), match.end()], "unverified": not explicit})
    return items, truncated


def compact_source_record(project_id, message_id, content_hash, body, posted_at_ts, *,
                          body_state="full", max_records=200):
    """Build compact assertions for one caller-validated source; never prove currentness.

    Spans index the supplied normalized body. The caller must validate scope,
    source binding, currentness and the body-to-hash relationship before saving
    or using these records. No source text is returned or persisted by this API.
    """
    if (type(project_id) is not int or project_id <= 0 or type(message_id) is not int
            or message_id <= 0 or not valid_hash(content_hash) or type(max_records) is not int
            or not 1 <= max_records <= 1000):
        raise ValueError("invalid_intervention_input")
    coverage = {"state": "unknown", "records_truncated": False, "candidates": 0,
                "unknown": 0, "source_complete": None, "source_validation": "caller_required"}
    result = {"records": [], "coverage": coverage}
    if body_state == "deleted":
        coverage["state"] = "deleted"
        return result
    if body_state != "full":
        coverage["state"] = "incomplete"
        return result
    if not isinstance(body, str) or "\x00" in body:
        coverage["state"] = "invalid_source"
        return result
    if len(body) > _MAX_BODY_CHARS:
        coverage["state"] = "oversize"
        return result
    items, truncated = _source_items(project_id, message_id, content_hash, body, posted_at_ts, max_records)
    projected = project(project_id, items)
    # All quotes and written-role/date strings are recoverable from the span.
    # Dates are only emitted when the shared classifier resolved them safely.
    fields = ("span", "role", "action", "event_day", "target_day", "date_basis", "state")
    result["records"] = [{key: row[key] for key in fields if key in row}
                         for row in projected["records"]]
    coverage.update(state="observed", records_truncated=truncated,
                    candidates=projected["coverage"]["candidates"], unknown=projected["coverage"]["unknown"])
    return result


def _source_problem(mid, body_state, digest, body_len, html_len, body_nul, html_nul, body, html):
    """Classify a source without returning original content on rejection."""
    if body_state == "deleted":
        return "deleted"
    if body_state != "full":
        return "incomplete"
    if body_nul or html_nul:
        return "invalid_source"
    if ((type(body_len) is int and body_len > _MAX_BODY_CHARS)
            or (type(html_len) is int and html_len > _MAX_HTML_CHARS)):
        return "oversize"
    if (type(mid) is not int or mid <= 0 or not valid_hash(digest)
            or not isinstance(body, str) or not isinstance(html, str)
            or hashlib.sha256(html.encode()).hexdigest() != digest or html_to_text(html) != body):
        return "invalid_source"
    return None


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
        "instr(m.body_text,char(0)) AS body_nul,instr(m.body_html,char(0)) AS html_nul,"
        "CASE WHEN length(m.body_text)<=? AND length(m.body_html)<=? "
        "AND instr(m.body_text,char(0))=0 AND instr(m.body_html,char(0))=0 THEN m.body_text END,"
        "CASE WHEN length(m.body_text)<=? AND length(m.body_html)<=? "
        "AND instr(m.body_text,char(0))=0 AND instr(m.body_html,char(0))=0 THEN m.body_html END "
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
    for _, _, _, mid, body_state, digest, posted, body_len, html_len, body_nul, html_nul, body, html in rows[:limit]:
        if mid is None:
            continue
        coverage["messages_examined"] += 1
        problem = _source_problem(mid, body_state, digest, body_len, html_len, body_nul, html_nul, body, html)
        if problem:
            coverage[problem] += 1
            continue
        source_items, truncated = _source_items(project_id, mid, digest, body, posted, max_records - len(items))
        items.extend(source_items)
        coverage["records_truncated"] = truncated
        if coverage["records_truncated"]:
            break
    projected = project(project_id, items)
    result["records"] = [{**record, "assertion_only": True} for record in projected["records"]]
    coverage["candidates"] = projected["coverage"]["candidates"]
    coverage["unknown"] = projected["coverage"]["unknown"]
    return result


def _source_cursor_fingerprint(db, mid, digest, posted):
    """Bind a partial position to body, posting time and source occurrence."""
    columns = {row[1]: row for row in db.execute("PRAGMA table_info(message_revisions)")}
    if (not {"message_id", "seq"} <= columns.keys()
            or any(str(columns[name][2]).upper() != "INTEGER" or columns[name][3] != 1
                   or columns[name][5] != position
                   for name, position in (("message_id", 1), ("seq", 2)))):
        raise ValueError("intervention_cursor_revision_unavailable")
    # The ledger uses implicit occurrence 1 for a source with no revision row.
    revision = db.execute("SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=?",
                          (mid,)).fetchone()[0]
    if type(revision) is not int or not 0 < revision < 2**63:
        raise ValueError("intervention_cursor_revision_unavailable")
    return hashlib.sha256((digest + '\0' + repr(posted) + '\0' + str(revision)).encode()).hexdigest()


def _page_cursor(project_id, mid, *, consistency, offset=0, source_fp=None):
    return {"version": 1, "project_id": project_id, "message_id": mid,
            "offset": offset, "source_fp": source_fp, "consistency": consistency}


def read_source_schedule_page(db, project_id, *, cursor=None, limit=200, max_records=200,
                              consistency="snapshot"):
    """Read one patient-scoped page on the caller-owned, authorized snapshot.

    In ``snapshot`` mode the caller must retain the same snapshot and bind its
    generation to every page. In ``page`` mode each click may use a new snapshot;
    changes between pages are not checked and traversal is not a point-in-time
    history. In both modes the caller reauthorizes each page, the cursor is a
    position rather than authorization, and partial-message continuation checks
    the source, posting time and revision occurrence. Per-page messages_examined
    includes a partially resumed message again; adding these counts across pages
    does not produce a distinct-message total. No result proves fetch or
    extraction completeness.
    """
    if not db.in_transaction:
        raise ValueError("intervention_snapshot_required")
    if (type(project_id) is not int or project_id <= 0 or type(limit) is not int
            or not 1 <= limit <= 1000 or type(max_records) is not int
            or not 1 <= max_records <= 1000 or consistency not in ("snapshot", "page")):
        raise ValueError("invalid_intervention_input")
    if cursor is not None:
        if (not isinstance(cursor, dict)
                or set(cursor) != {"version", "project_id", "message_id", "offset", "source_fp", "consistency"}
                or cursor["consistency"] != consistency
                or type(cursor["version"]) is not int or cursor["version"] != 1
                or type(cursor["project_id"]) is not int or cursor["project_id"] != project_id
                or type(cursor["message_id"]) is not int or not -(2**63) <= cursor["message_id"] < 2**63
                or type(cursor["offset"]) is not int or not 0 <= cursor["offset"] <= _MAX_BODY_CHARS
                or cursor["source_fp"] is not None and not valid_hash(cursor["source_fp"])
                or cursor["source_fp"] is None and cursor["offset"] != 0):
            raise ValueError("invalid_intervention_cursor")
    coverage = {"state": "unknown", "fetch_state": "unknown", "messages_examined": 0,
                "incomplete": 0, "deleted": 0, "invalid_source": 0, "oversize": 0,
                "records_truncated": False, "tail_reached": None,
                "source_complete": None, "extraction": "not_assessed",
                "relations": "not_assessed", "recognition": "exact_sentences_only",
                "snapshot_consistency": ("caller_same_snapshot_required" if consistency == "snapshot"
                                         else "page_only_changes_unverified")}
    result = {"project_id": project_id, "records": [], "next_cursor": None, "coverage": coverage}
    needed = {"patients": {"project_id", "fetch_state", "fetch_reason", "is_archived"},
              "messages": {"project_id", "message_id", "body_html", "body_text", "body_state",
                           "content_hash", "posted_at_ts"}}
    for table, required in needed.items():
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if not required <= columns:
            coverage["state"] = "schema_unknown"
            return result
    patient = db.execute("SELECT fetch_state,fetch_reason,is_archived FROM patients WHERE project_id=?",
                         (project_id,)).fetchone()
    if patient is None:
        coverage["state"] = "scope_unknown"
        return result
    fetch_state, reason, archived = patient
    if archived != 0 or reason in ("forbidden", "permission_denied", "access_revoked") or fetch_state == "forbidden":
        coverage["state"] = "scope_unavailable"
        return result
    if fetch_state in ("complete", "incomplete", "failed", "pending"):
        coverage["fetch_state"] = fetch_state
    coverage["state"] = "observed"
    resume = cursor is not None and cursor["source_fp"] is not None
    predicate = (" AND m.message_id<=?" if resume else " AND m.message_id<?") if cursor is not None else ""
    params = [_MAX_BODY_CHARS, _MAX_HTML_CHARS, _MAX_BODY_CHARS, _MAX_HTML_CHARS, project_id]
    if cursor is not None:
        params.append(cursor["message_id"])
    params.append(limit + 1)
    source_cursor = db.execute(
        "SELECT m.message_id,m.body_state,m.content_hash,m.posted_at_ts,length(m.body_text),length(m.body_html),"
        "instr(m.body_text,char(0)),instr(m.body_html,char(0)),"
        "CASE WHEN length(m.body_text)<=? AND length(m.body_html)<=? "
        "AND instr(m.body_text,char(0))=0 AND instr(m.body_html,char(0))=0 THEN m.body_text END,"
        "CASE WHEN length(m.body_text)<=? AND length(m.body_html)<=? "
        "AND instr(m.body_text,char(0))=0 AND instr(m.body_html,char(0))=0 THEN m.body_html END "
        "FROM messages m WHERE m.project_id=?" + predicate + " ORDER BY m.message_id DESC LIMIT ?",
        params)
    next_cursor, processed, last_mid = None, 0, None
    coverage["candidates"] = coverage["unknown"] = 0
    try:
        while processed < limit:
            row = source_cursor.fetchone()
            if row is None:
                if resume and processed == 0:
                    raise ValueError("intervention_cursor_source_changed")
                break
            mid, body_state, digest, posted, body_len, html_len, body_nul, html_nul, body, html = row
            if resume and processed == 0 and mid != cursor["message_id"]:
                raise ValueError("intervention_cursor_source_changed")
            coverage["messages_examined"] += 1
            processed += 1
            last_mid = mid
            problem = _source_problem(mid, body_state, digest, body_len, html_len, body_nul, html_nul, body, html)
            if resume and processed == 1:
                if problem or _source_cursor_fingerprint(db, mid, digest, posted) != cursor["source_fp"]:
                    raise ValueError("intervention_cursor_source_changed")
                offset = cursor["offset"]
            else:
                offset = 0
            if problem:
                coverage[problem] += 1
                continue
            page_items, truncated = _source_items(project_id, mid, digest, body, posted,
                                                   max_records - len(result["records"]), start_offset=offset)
            if page_items:
                projected = project(project_id, page_items)
                result["records"].extend({**record, "assertion_only": True} for record in projected["records"])
                coverage["candidates"] += projected["coverage"]["candidates"]
                coverage["unknown"] += projected["coverage"]["unknown"]
            if truncated:
                coverage["records_truncated"] = True
                next_offset = page_items[-1]["evidence_span"][1] if page_items else offset
                next_cursor = _page_cursor(project_id, mid, consistency=consistency, offset=next_offset,
                                           source_fp=_source_cursor_fingerprint(db, mid, digest, posted))
                break
            if len(result["records"]) == max_records:
                if source_cursor.fetchone() is not None:
                    coverage["records_truncated"] = True
                    next_cursor = _page_cursor(project_id, mid, consistency=consistency)
                break
        else:
            if source_cursor.fetchone() is not None:
                next_cursor = _page_cursor(project_id, last_mid, consistency=consistency)
    finally:
        source_cursor.close()
    coverage["tail_reached"] = next_cursor is None
    result["next_cursor"] = next_cursor
    return result
