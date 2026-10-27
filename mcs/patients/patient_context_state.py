"""Project source-bound context history without replacing facts or guessing clinical state."""
from datetime import datetime, timedelta, timezone
import re
import hashlib
import json

from mcs_util import locate_quote_span

from clinical_scope import (CURRENT_MARK, FAMILY_PERSON, OTHER_PERSON, current_medication_continuation, source_axes,
                            source_polarity, source_resolution, source_assertion_span)
from clinical_values import written_day

_JST = timezone(timedelta(hours=9))
_DATES = ("measured_on", "observed_on", "onset", "last_confirmed_on")
_ACTION = re.compile(r"開始|継続|変更|中止|保留|再開")
_PAIRED_DATE = re.compile(r"(?<!\d)(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)(?!\d)\s*[（(]\s*(本日|今日|昨日|明日|翌日)\s*[）)]")
_WEEKDAY_PAIR = re.compile(
    r"(?<!\d)(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)(?!\d)\s*"
    r"[（(]\s*([月火水木金土日])(?:曜(?:日)?)?\s*[）)]"
)
_RELATIVE_WEEKDAY_PAIR = re.compile(
    r"(本日|今日|昨日|明日|翌日)\s*[（(]\s*([月火水木金土日])(?:曜(?:日)?)?\s*[）)]"
)
_WRITTEN_DATE_MARKER = re.compile(
    r"(?<!\d)(?:\d{4}[-/年]\d{1,2}[-/月]\d{1,2}|\d{1,2}[/月]\d{1,2})日?(?!\d)"
)
_RELATIVE_DATE_MARKER = re.compile(r"本日|今日|昨日|明日|翌日")
_WEEKDAY_TOKEN = re.compile(r"(?P<word>[月火水木金土日]曜(?:日)?)|[（(]\s*(?P<single>[月火水木金土日])\s*[）)]")
_CORRECTION = re.compile(r"(?:投稿|記録)#(?P<mid>[1-9][0-9]*)の[「『](?P<quote>[^」』]{1,3000})[」』]を(?P<kind>訂正|撤回)")
_PRIOR_CORRECTION = re.compile(r"前回の[「『](?P<quote>[^」』]{1,3000})[」』](?:を(?P<kind>訂正|撤回)|は誤り)")
_CORRECTION_CUE = re.compile(r"訂正|撤回|誤り")
_INDIRECT_PERSON = re.compile(rf"(?:{FAMILY_PERSON}|{OTHER_PERSON})(?:さん|様)?(?:に|の)")
_DIRECT_SYMPTOM_OWNER = re.compile(
    rf"(?P<family>{FAMILY_PERSON})(?:さん|様)?(?:には|に|は|が|の|も)[ \t　]*$"
    rf"|(?P<other>{OTHER_PERSON})(?:さん|様)?(?:には|に|は|が|の|も)[ \t　]*$")
_DIRECT_SYMPTOM_STATE = re.compile(
    r"(?:が|は|も)[ \t　]*(?:(?:現在|本日|今日|今は|特に|全く)[ \t　]*)?"
    r"(?:あります|ありません|ある|ない|なし|ないです)[ \t　]*[。]?")
_FOLLOWING_RETRACTION = re.compile(
    r"^[ \t　\n]*(?:この|その|先ほどの|先程の|上記の|前記の|直前の|前の)"
    r"(?:記載|投稿|報告|内容|記録)[ \t　]*"
    r"(?:(?:は|が|も)[ \t　]*(?:(?:誤り|間違い|誤記)(?:です|でした|である)"
    r"|(?:撤回|訂正|取消|取り消し)(?:されました|されています))"
    r"|を[ \t　]*(?:撤回|訂正|取消|取り消し)(?:しました|します|いたします))"
    r"(?=[。！!、,\s]|$)")
_QUOTED_RETRACTION = re.compile(
    r"^[ \t　]*[」』][ \t　]*(?:(?:(?:との|という|の)(?:記載|投稿|報告|内容|記録))?"
    r"(?:は|が)[ \t　]*(?:(?:誤り|間違い|誤記)(?:です|でした|である)"
    r"|(?:撤回|訂正|取消|取り消し)(?:されました|されています))"
    r"|を[ \t　]*(?:撤回|訂正|取消|取り消し)(?:しました|します|いたします))"
    r"(?=[。！!、,\s]|$)")
_PATIENT_SUBJECT = re.compile(r"(?:ご本人|本人|患者(?:本人|さん|様)?|利用者(?:本人|さん|様)?)(?:は|が|の|に|も)")
_GENERAL_ALLERGY = re.compile(
    r"(?:^|[\s:：、,。]|(?:ご本人|本人|患者(?:さん|様)?|利用者(?:さん|様)?)(?:は|に|の))"
    r"(?:本日|今日|現在)?(?:全ての|すべての|食物|食品|薬剤)?アレルギー"
    r"(?:の)?(?:記録|有無|情報)?(?:は|が|も|を)?(?:本日|今日|現在)?\s*"
    r"(?:ない|ありません|なし|無し|認めず|不明|未確認|未確定|訂正|撤回|誤り)"
)
_ALLERGY_UNCERTAIN = re.compile(
    r"ない|なし|無し|ありません|認めず|否定|不明|不詳|未確認|未確定|未把握|未調査|不確か|"
    r"疑い|かもしれ|確認でき(?:ない|ず|ていない|ていません)|訂正|撤回|誤り|誤記"
)
_MISSING_REPORT = re.compile(
    r"(?:記録|報告|記載|情報|連絡)(?:は|が|も)?\s*(?:現在|今は|本日|今日)?\s*"
    r"(?:ない|ありません|なし|未確認)"
)
_VALUE_KEYS = {"actual_dose", "actual_frequency", "residual_quantity", "medication_manager", "supporter",
               "storage_place", "value", "self_management", "swallowing", "food_texture"}


def _day(posted):
    try:
        return datetime.fromtimestamp(posted, _JST).date() if type(posted) in (int, float) else None
    except (ValueError, OverflowError, OSError):
        return None


def paired_date_conflict(quote, posted_at_ts):
    """Detect contradictory or unbound weekday and relative-date references."""
    posted_day = _day(posted_at_ts)
    # "翌日" refers to an earlier event, not necessarily to the posting day.
    if "翌日" in quote:
        return True
    date_markers = list(_WRITTEN_DATE_MARKER.finditer(quote))
    relative_markers = list(_RELATIVE_DATE_MARKER.finditer(quote))
    paired_dates = list(_PAIRED_DATE.finditer(quote))
    if date_markers and relative_markers and not (
            len(date_markers) == len(relative_markers) == len(paired_dates) == 1
            and date_markers[0].start() == paired_dates[0].start(1)
            and relative_markers[0].start() == paired_dates[0].start(2)):
        return True
    for match in paired_dates:
        absolute = written_day(match.group(1))
        delta = -1 if match.group(2) == "昨日" else 1 if match.group(2) in ("明日", "翌日") else 0
        try:
            relative_day = posted_day + timedelta(days=delta) if posted_day is not None else None
        except OverflowError:
            relative_day = None
        if absolute is None or absolute[0] != "ymd" or absolute[1] != relative_day:
            return True
    pairs = list(_WEEKDAY_PAIR.finditer(quote))
    for match in pairs:
        day = written_day(match.group(1))
        if not day or day[0] != "ymd" or day[1].weekday() != "月火水木金土日".index(match.group(2)):
            return True
    relative_pairs = list(_RELATIVE_WEEKDAY_PAIR.finditer(quote))
    if relative_pairs and (_WRITTEN_DATE_MARKER.search(quote)
                           or len(_RELATIVE_DATE_MARKER.findall(quote)) != 1):
        return True  # Another written time may refer to a different assertion.
    for match in relative_pairs:
        delta = -1 if match.group(1) == "昨日" else 1 if match.group(1) in ("明日", "翌日") else 0
        try:
            day = posted_day + timedelta(days=delta) if posted_day is not None else None
        except OverflowError:
            day = None
        if day is None or day.weekday() != "月火水木金土日".index(match.group(2)):
            return True
    # An unpaired weekday alongside a date may refer to another event. A
    # standalone recurring weekday does not by itself identify any event date.
    if any(not any(token.start("word" if token.group("word") else "single") == pair.start(2)
                   and token.end() <= pair.end() for pair in (*pairs, *relative_pairs))
           for token in _WEEKDAY_TOKEN.finditer(quote)) and (
                   _WRITTEN_DATE_MARKER.search(quote) or _RELATIVE_DATE_MARKER.search(quote)):
        return True
    return False


def _dated_start_then_yesterday_continuation(quote, names, posted_day):
    """Recognize only the written start and later ongoing use of one drug."""
    if len(names) != 1 or posted_day is None:
        return False
    match = re.fullmatch(
        r"本人は(?P<day>\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)に"
        + re.escape(names[0]) + r"を開始し[、,]昨日から服用しています[。]?", quote)
    day = written_day(match.group("day")) if match else None
    try:
        return bool(day and day[0] == "ymd" and day[1] <= posted_day - timedelta(days=1))
    except OverflowError:
        return False


def annotate(fact, body):
    """Record independent source time/person axes and literal relation evidence."""
    start, end = fact["evidence_span"]
    quote = body[start:end]
    details = fact.get("details") or []
    names = [d["value"] for d in details if d["key"] in ("drug_name", "observation_name", "symptom")]
    anchors = [start + quote.find(name) for name in names if quote.count(name) == 1]
    actions = list(_ACTION.finditer(quote))
    if not anchors:
        anchors = [start + m.start() for m in actions] or [max(start, end - 2)]
    axes = [source_axes(body, at, min(end, at + 1)) for at in anchors]
    subjects, periods = {a[0] for a in axes}, {a[1] for a in axes}
    subject = next(iter(subjects)) if len(subjects) == 1 else "unknown"
    period = next(iter(periods)) if len(periods) == 1 else "unknown"
    subject_ambiguous = False
    if (fact.get("category") == "symptoms"
            and len(names) == 1 and quote.count(names[0]) == 1):
        at = start + quote.index(names[0])
        sentence_start = max(body.rfind(mark, 0, at) for mark in "。！？!?;；\n、,") + 1
        prefix = body[sentence_start:at]
        owner = _DIRECT_SYMPTOM_OWNER.search(prefix)
        if owner:
            tail_start = at + len(names[0])
            sentence_tail = re.match(r"[^。！？!?;；\n]*[。]?", body[tail_start:])
            tail = sentence_tail.group() if sentence_tail else ""
            suffix = body[tail_start + len(tail):]
            quoted_report = (re.match(r"[ \t　]*[」』]", suffix)
                             or re.match(r"[）)]?[ \t　]*(?:と|って|との|という)", suffix))
            if (_PATIENT_SUBJECT.search(prefix) or quoted_report
                    or not _DIRECT_SYMPTOM_STATE.fullmatch(body[tail_start:end])
                    or not _DIRECT_SYMPTOM_STATE.fullmatch(tail)):
                subject, subject_ambiguous = "unknown", True
            else:
                subject = "family" if owner.group("family") else "other"
    if fact.get("category") == "symptoms":
        if body[end - 1] in "。！？!?;；\n":
            next_sentence = body[end:]
        else:
            boundary = re.search(r"[。！？!?;；\n]", body[end:])
            next_sentence = body[end + boundary.end():] if boundary else ""
        if (_QUOTED_RETRACTION.match(body[end:])
                or _FOLLOWING_RETRACTION.match(next_sentence)):
            subject, subject_ambiguous = "unknown", True
    if fact.get("category") not in ("medications", "symptoms") and subject == "patient":
        sentence_start = max(body.rfind(mark, 0, start) for mark in "。！？!?;；\n") + 1
        context = body[sentence_start:end]
        patient = _PATIENT_SUBJECT.search(context)
        indirect = list(_INDIRECT_PERSON.finditer(context))
        if any(not (patient and (re.match(r"(?:介助|支援|協力)(?:で|を|により|のもと)", context[m.end():])
                                or m[0].endswith("に") and patient.start() < m.start()
                                and re.search(r"報告|連絡|相談", context[m.end():])
                                and (fact.get("category") != "allergies"
                                     or _PATIENT_SUBJECT.search(context, m.end())))) for m in indirect):
            subject = "unknown"
    dates = [written_day(d["value"]) for d in details if d["key"] in _DATES]
    written = written_day(quote)
    if written:
        dates.append(written)
    dates = {d[1] for d in dates if d and d[0] == "ymd"}
    posted_day = _day(fact.get("posted_at_ts"))
    relative = set(re.findall(r"本日|今日|昨日|明日|翌日", quote))
    paired_conflict = paired_date_conflict(quote, fact.get("posted_at_ts"))
    if (paired_conflict and any(d["key"] == "drug_name" for d in details)
            and _dated_start_then_yesterday_continuation(quote, names, posted_day)):
        paired_conflict = False
    relative_overflow = False
    if not dates and posted_day is not None and len(relative) == 1:
        word = next(iter(relative))
        try:
            if word != "翌日":
                dates.add(posted_day + timedelta(days=-1 if word == "昨日" else 1 if word == "明日" else 0))
        except OverflowError:
            relative_overflow = True
    if relative == {"明日"} and period == "current":
        period = "planned"
    event_day = next(iter(dates)).isoformat() if len(dates) == 1 and not paired_conflict else None
    action = actions[0].group() if len(actions) == 1 else None
    polarity = source_polarity(body, start, end, target_end=start + actions[0].end()) if action else None
    present, resolution, use_continuation = False, None, False
    if (action in ("開始", "継続", "再開") and any(d["key"] == "drug_name" for d in details)
            and len(names) == 1 and quote.count(names[0]) == 1
            and quote.index(names[0]) < actions[0].start()
            and re.fullmatch(r"(?:を|の(?:服用|内服|使用|投与)を)",
                             quote[quote.index(names[0]) + len(names[0]):actions[0].start()].strip())
            and current_medication_continuation(quote[actions[0].end():])):
        present, polarity, use_continuation = True, False, True
    if action is None and len(names) == 1 and quote.count(names[0]) == 1:
        target_end = start + quote.index(names[0]) + len(names[0])
        medication = any(d["key"] == "drug_name" for d in details)
        polarity = source_polarity(body, start, end, target_end=target_end, medication=medication)
        if any(d["key"] == "symptom" for d in details):
            resolution = source_resolution(body, start, end, target_end=target_end)
        tail = body[target_end:end]
        present = polarity is not None and bool(re.search(r"中|あり|ある|あります|なし|ない|ありません|しています|している", tail))
    current_cue = bool(CURRENT_MARK.search(quote) or present or resolution is not None)
    ongoing = use_continuation or bool(re.search(r"続いて|持続|継続中|今も|(?:昨日|先週|先月)(?:から|より)", quote))
    symptom_state = ("resolved" if resolution is True else "not_resolved" if resolution is False
                     else "absent" if polarity is True else "present" if polarity is False else None)
    if not any(d["key"] == "symptom" for d in details):
        symptom_state = None
    use_state = None
    if any(d["key"] == "drug_name" for d in details) and polarity is False:
        use_state = ("using" if action in ("開始", "継続", "再開") or action is None and present
                     else "stopped" if action == "中止" else "held" if action == "保留" else None)
    corrections = []
    for pattern in (_CORRECTION, _PRIOR_CORRECTION):
        for match in pattern.finditer(quote):
            corrections.append({"kind": "retracts" if match.groupdict().get("kind") == "撤回" else "corrects",
                "message_id": int(match["mid"]) if "mid" in match.groupdict() else None,
                "quote": match["quote"], "span": [start + match.start(), start + match.end()],
                "affirmed": (source_polarity(body, start + match.start(), end,
                                              target_end=start + match.end()) is False
                    or pattern is _PRIOR_CORRECTION and re.match(r"(?:でした|です)[。！？!?\s]*$", quote[match.end():]) is not None)})
    reference_unknown = bool(re.search(r"同じ|同薬|その薬|これを", quote) and not any(quote.count(name) == 1 for name in names))
    unknown = bool(subject_ambiguous or corrections or _MISSING_REPORT.search(quote) or re.search(r"不明|未確認|未確定|かもしれ|疑い|どちら", quote) or reference_unknown or paired_conflict or relative_overflow or (len(dates) > 1) or len(actions) > 1
                   or _CORRECTION_CUE.search(quote) and not corrections)
    if fact.get("reported_subject") in ("patient", "family", "other") and fact["reported_subject"] != subject:
        unknown = True
    return {"subject": subject, "period": period, "event_day": event_day,
            "posted_day": posted_day.isoformat() if posted_day else None,
            "as_of_day": (posted_day.isoformat() if posted_day and current_cue and period == "current"
                          and not (paired_conflict or relative_overflow) else None),
            "ongoing": ongoing,
            "state_day": (posted_day.isoformat() if posted_day and current_cue and period == "current" and ongoing
                          and not (paired_conflict or relative_overflow) else event_day),
            "posted_at_ts": fact.get("posted_at_ts"), "action": action, "negated": polarity,
            "resolution": resolution, "symptom_state": symptom_state,
            "use_state": use_state, "current_cue": current_cue, "unknown": unknown, "corrections": corrections}


def _entity(fact):
    temporal = fact.get("temporal") or {}
    names = {(d["key"], d["value"]) for d in fact.get("details", [])
             if d["key"] in ("drug_name", "observation_name", "symptom")}
    if len(names) != 1 or temporal.get("subject") not in ("patient", "family", "other"):
        return None
    conditions = tuple(sorted((d["key"], d["value"]) for d in fact.get("details", []) if d["key"] in ("unit", "condition", "body_site")))
    return (fact["project_id"], temporal["subject"], fact["category"], next(iter(names)), conditions)


def _values(fact):
    values = [(d["key"], d["value"]) for d in fact.get("details", []) if d["key"] in _VALUE_KEYS]
    temporal = fact.get("temporal") or {}
    if temporal.get("use_state"):
        values.append(("use_state", temporal["use_state"]))
    elif temporal.get("action") and temporal.get("negated") is False:
        values.append(("action", temporal["action"]))
    if any(d["key"] == "symptom" for d in fact.get("details", [])) and temporal.get("symptom_state"):
        values.append(("symptom_presence", "absent" if temporal["symptom_state"] in ("resolved", "absent") else "present"))
    return values


def _unbound_target(fact):
    """Identify only one explicitly patient-bound allergy target from its source."""
    if fact.get("category") != "allergies":
        return None
    quote = fact.get("evidence")
    if not isinstance(quote, str):
        return None
    text = re.sub(r"^\s*(?:アレルギー|アレルギー歴|薬剤アレルギー)\s*[:：]\s*", "", quote)
    if text.count("アレルギー") != 1:
        return None
    match = re.search(r"(?:ご本人|本人|患者(?:さん|様)?|利用者(?:さん|様)?)(?:に|は|の)"
                      r"(?:本日|今日|現在)?\s*([一-龯々ァ-ヶーA-Za-z0-9]{1,30})(?:の)?アレルギー", text)
    target = match[1] if match else None
    # shortcut: unrecognized names remain unbound, expand only with source-grounded identity rules.
    if (not target or not re.fullmatch(r"[一-龯々ァ-ヶーA-Za-z0-9]+", target)
            or target in ("食物", "食品", "薬剤", "薬", "不明", "その他")):
        return None
    return target


def _other_allergy(fact):
    """Exclude an explicit other person's report, not an unowned report."""
    subject = (fact.get("temporal") or {}).get("subject")
    quote = fact.get("evidence") or ""
    return (subject in ("family", "other") or not _PATIENT_SUBJECT.search(quote)
            and re.search(rf"(?:{FAMILY_PERSON}|{OTHER_PERSON})(?:さん|様)?(?:に|は|が|の)"
                          r"(?:は)?(?:本日|今日|現在)?(?:(?:食物|食品|薬剤)?アレルギー|"
                          r"ありません|なし|不明|未確認)", quote) is not None)


def _general_allergy(fact):
    quote = fact.get("evidence") or ""
    if _GENERAL_ALLERGY.search(quote):
        return True
    return re.fullmatch(
        r"\s*(?:アレルギー|アレルギー歴|薬剤アレルギー)\s*[:：]\s*"
        r"(?:(?:ご本人|本人|患者(?:さん|様)?|利用者(?:さん|様)?)(?:に|は|の)(?:は)?)?"
        r"(?:本日|今日|現在)?\s*(?:ない|ありません|なし|無し|認めず|不明|未確認|未確定|訂正|撤回|誤り)"
        r"(?:です|でした|しました)?[。\s]*", quote) is not None


def _uncertain_allergy(fact):
    if _general_allergy(fact):
        return True
    if _unbound_target(fact) is not None:
        return False
    quote = re.sub(r"^\s*(?:アレルギー|アレルギー歴|薬剤アレルギー)\s*[:：]\s*", "", fact.get("evidence") or "")
    clauses = re.split(r"[。！？!?;；、,\n]", quote)
    return any(_ALLERGY_UNCERTAIN.search(clause) and ("アレルギー" in clause
               or re.search(r"詳細|有無|種類|対象|内容|原因|記録|情報", clause)
               or len(clauses) <= 2 and not any("アレルギー" in part for part in clauses))
               for clause in clauses)


def _allergy_day(fact):
    temporal = fact.get("temporal") or {}
    return temporal.get("event_day") or temporal.get("state_day") or temporal.get("as_of_day")



def dependency_entity(fact):
    """Return the versioned opaque entity identity used only for dependency invalidation."""
    entity = _entity(fact)
    return hashlib.sha256(json.dumps(['entity/v1', entity], ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest() if entity is not None else None


def dependency_axes(fact):
    """Return affected attribute names without copying clinical values."""
    return sorted({axis for axis, _ in _values(fact)})


def dependency_values(fact):
    """Return transient comparison material; callers must not persist these values."""
    return _values(fact)


def _dependency_barriers(losses):
    indexed = {}
    for loss in losses:
        key = (loss.get('entity'), loss.get('axis'))
        group = indexed.setdefault(key, {})
        origin = loss.get('origin') or {}
        origin_key = (origin.get('message_id'), origin.get('source_key', origin.get('message_id')))
        source = group.setdefault(origin_key, {'bindings': set(), 'revision': 0, 'latest': None, 'unknown': False})
        source['bindings'].add(origin.get('binding'))
        source['revision'] = max(source['revision'], origin.get('revision', 0))
        day = loss.get('day')
        source['unknown'] |= day is None
        if day is not None:
            source['latest'] = max(source['latest'] or day, day)
    return {key: (sources,
        sorted(((source['latest'], origin) for origin, source in sources.items() if source['latest'] is not None),
               key=lambda row: row[0], reverse=True)[:2],
        sum(source['unknown'] for source in sources.values())) for key, sources in indexed.items()}


def _dependency_blocked(fact, barriers):
    entity = dependency_entity(fact)
    temporal = fact.get('temporal') or {}
    day = temporal.get('as_of_day') or temporal.get('event_day')
    origin_key = (fact.get('message_id'), fact.get('source_key', fact.get('message_id')))
    proof = fact.get('dependency_origin') or {}
    for axis in dependency_axes(fact):
        barrier = barriers.get((entity, axis))
        if barrier is None:
            continue
        sources, latest, unknown = barrier
        own = sources.get(origin_key)
        own_restored = (own is not None and proof.get('grounded') is True
            and proof.get('source_key') == origin_key[1] and proof.get('content_hash') == fact.get('content_hash')
            and type(proof.get('revision')) is int and proof['revision'] >= own['revision']
            and isinstance(proof.get('binding'), str) and re.fullmatch('[0-9a-f]{64}', proof['binding']) is not None
            and proof['binding'] not in own['bindings'])
        # Attested new source support only overrides losses of that same
        # source. Re-extracting A cannot override a separate lost stop B.
        if own_restored and len(sources) == 1:
            continue
        latest_other = next((stamp for stamp, origin in latest if not own_restored or origin != origin_key), None)
        unknown_other = unknown - int(own_restored and own['unknown'])
        if ((own is None or own_restored) and day is not None and not unknown_other
                and latest_other is not None and day > latest_other):
            continue
        return True
    return False


def project(facts, *, dependency_losses=()):
    """Return all facts with non-destructive buckets and narrowly bound relations.

    Event dates order observations. Posting order never establishes an event
    date, supersession, execution of a plan or reversal of a withdrawn source.
    """
    by_id = {f["id"]: f for f in facts}
    buckets, relations, replaced, disputed = {}, [], set(), set()
    targets = {}
    for old in facts:
        subject = (old.get("temporal") or {}).get("subject")
        base = (old["project_id"], old["category"], subject)
        entity = _entity(old)
        attrs = tuple(sorted(d["key"] for d in old.get("details", []) if d["key"] in _VALUE_KEYS))
        for mid in (old["message_id"], None):
            for scope in (None, (entity, None), (entity, attrs)):
                key = (*base, mid, old.get("evidence"), scope)
                # More than two matches are already ambiguous. Keep a capped
                # index rather than pair-scanning years of repeated reports.
                matches = targets.setdefault(key, [])
                if len(matches) < 2 and not any(f["id"] == old["id"] for f in matches):
                    matches.append(old)
    for fact in facts:
        t = fact.get("temporal") or {}
        if fact.get("historical") or t.get("subject") in ("family", "other") or t.get("period") == "past":
            bucket = "history"
        elif t.get("unknown"):
            bucket = "unknown"
        elif t.get("period") in ("planned", "conditional"):
            bucket = "plan"
        elif (t.get("subject") != "patient" or t.get("period") != "current"
              or t.get("action") and t.get("negated") is not False
              or t.get("posted_day") is None
              or t.get("event_day") is None and t.get("as_of_day") is None):
            bucket = "unknown"
        elif t.get("event_day") and t.get("posted_day") and t["event_day"] < t["posted_day"] and not t.get("ongoing"):
            bucket = "history"
        elif t.get("event_day") and t.get("posted_day") and t["event_day"] > t["posted_day"]:
            bucket = "unknown"
        else:
            bucket = "current"
        buckets[fact["id"]] = bucket
        for ref in t.get("corrections", []):
            entity = _entity(fact)
            attrs = tuple(sorted(d["key"] for d in fact.get("details", []) if d["key"] in _VALUE_KEYS))
            scope = (entity, None) if entity is not None else None
            key = (fact["project_id"], fact["category"], t.get("subject"), ref["message_id"], ref["quote"], scope)
            candidates = targets.get(key, []) if ref.get("affirmed") is True and t.get("subject") in ("patient", "family", "other") else []
            if len(candidates) > 1 and entity is not None and attrs:
                candidates = targets.get((*key[:-1], (entity, attrs)), [])
            if any(old["id"] == fact["id"] for old in candidates):
                candidates = []
            # A correction must refer backwards to an existing report.
            # This also excludes circular or future-target relations without
            # discarding either source assertion.
            posted = t.get("posted_at_ts")
            candidates = [old for old in candidates if type(posted) in (int, float)
                          and type((old.get("temporal") or {}).get("posted_at_ts")) in (int, float)
                          and old["temporal"]["posted_at_ts"] < posted]
            relation = {"type": ref["kind"], "source_fact_id": fact["id"], "evidence_span": ref["span"]}
            if len(candidates) == 1:
                old = candidates[0]
                relation["target_fact_id"] = old["id"]
                replaced.add(old["id"])
            else:
                relation.update(type="unresolved", target_fact_id=None)
                buckets[fact["id"]] = "unknown"
            relations.append(relation)
    # Without a bound entity, two separate reports in the same category may
    # refer to the same state or to unrelated things. Neither reading permits
    # selecting a current state merely from their posting order.
    unbound = {}
    for fact in facts:
        if (fact["id"] in replaced or fact["category"] in ("medications", "symptoms")
                or _entity(fact) is not None or buckets[fact["id"]] not in ("current", "unknown")):
            continue
        key = (fact["project_id"], fact["category"], _unbound_target(fact))
        unbound.setdefault(key, []).append(fact)
    for rows in unbound.values():
        identities = {(f.get("message_id"), f.get("content_hash"),
                       tuple(f.get("evidence_span") or ()), f.get("evidence")) for f in rows}
        if len(identities) > 1:
            for fact in rows:
                if buckets[fact["id"]] == "current":
                    buckets[fact["id"]] = "unknown"
    allergies = {}
    for fact in facts:
        if (fact["category"] == "allergies" and fact["id"] not in replaced
                and buckets[fact["id"]] in ("current", "unknown") and not _other_allergy(fact)):
            allergies.setdefault(fact["project_id"], []).append(fact)
    for rows in allergies.values():
        broad = [f for f in rows if _uncertain_allergy(f)]
        broad_ids = {f["id"] for f in broad}
        specific = [f for f in rows if f["id"] not in broad_ids]
        broad_days = {_allergy_day(f) for f in broad}
        specific_days = {_allergy_day(f) for f in specific}
        for fact in rows:
            other_days = specific_days if fact["id"] in broad_ids else broad_days
            day = _allergy_day(fact)
            if (buckets[fact["id"]] == "current" and other_days
                    and (day is None or None in other_days or day in other_days)):
                buckets[fact["id"]] = "unknown"
    initial_buckets = dict(buckets)
    groups = {}
    for fact in facts:
        entity = _entity(fact)
        if entity is None or fact["id"] in replaced or buckets[fact["id"]] in ("history", "plan"):
            continue
        for key, value in _values(fact):
            groups.setdefault((entity, key), []).append((fact, value))
    for rows in groups.values():
        values = {value for _, value in rows}
        if len(values) < 2:
            continue
        if any(initial_buckets[f["id"]] == "unknown" for f, _ in rows):
            known = [(f, value) for f, value in rows if initial_buckets[f["id"]] != "unknown"]
            by_known_day = {}
            for fact, value in known:
                temporal = fact.get("temporal") or {}
                day = temporal.get("state_day") or temporal.get("event_day") or temporal.get("as_of_day")
                if day is not None:
                    by_known_day.setdefault(day, set()).add(value)
            if any(len(values) > 1 for values in by_known_day.values()):
                disputed.update(f["id"] for f, _ in known)
            else:
                for fact, _ in known:
                    buckets[fact["id"]] = "unknown"
            continue
        days = [(f.get("temporal") or {}).get("state_day") or (f.get("temporal") or {}).get("event_day")
                or (f.get("temporal") or {}).get("as_of_day") for f, _ in rows]
        by_day = {}
        for (_, value), day in zip(rows, days):
            by_day.setdefault(day, set()).add(value)
        if None in days or any(len(values) > 1 for values in by_day.values()):
            disputed.update(f["id"] for f, _ in rows)
        else:
            latest = max(days)
            for fact, _ in rows:
                if (fact["temporal"].get("state_day") or fact["temporal"].get("event_day") or fact["temporal"].get("as_of_day")) != latest:
                    buckets[fact["id"]] = "history"
    dependency_barriers = _dependency_barriers(dependency_losses)
    for fact in facts:
        fid = fact["id"]
        if fid in replaced:
            buckets[fid] = "history"
        elif fid in disputed:
            buckets[fid] = "conflict"
        elif buckets[fid] == "current" and _dependency_blocked(fact, dependency_barriers):
            buckets[fid] = "unknown"
            relations.append({"type": "source_withdrawn", "source_fact_id": fid, "target_fact_id": None})
    return {"groups": {key: [fid for fid in by_id if buckets[fid] == key]
                       for key in ("current", "history", "plan", "conflict", "unknown")},
            "relations": relations}


def clinical_state_fact(message, item, category, source="extract_llm"):
    """Adapt a stored DTO to the common temporal schema, preserving its original fields."""
    from mcs_queries import item_unverified

    if category not in ("medications", "symptoms"):
        raise ValueError("clinical state mentions must be medications or symptoms")
    message = dict(message)
    name = item.get("name" if category == "medications" else "text")
    if not isinstance(name, str) or not name.strip() or name in ("処方薬", "薬"):
        return None
    body = message.get("body_text") or ""
    quote = item.get("evidence")
    explicit = item.get("evidence_span")
    if isinstance(item.get("span"), dict):
        explicit = [item["span"].get("start"), item["span"].get("end")]
    if explicit is not None:
        span = tuple(explicit) if (isinstance(explicit, (list, tuple)) and len(explicit) == 2
            and all(type(n) is int for n in explicit) and isinstance(quote, str)
            and 0 <= explicit[0] < explicit[1] <= len(body) and body[explicit[0]:explicit[1]] == quote) else None
    else:
        span = locate_quote_span(body, quote) if isinstance(quote, str) and quote else None
    if quote is None and explicit is None:
        span = source_assertion_span(body, name)
    name_span = locate_quote_span(body[span[0]:span[1]], name) if span else None
    details = [{"key": "drug_name" if category == "medications" else "symptom",
                "value": body[span[0] + name_span[0]:span[0] + name_span[1]] if name_span else name,
                "evidence": body[span[0]:span[1]] if span else ""}]
    keys = (("dose", "actual_dose"), ("freq", "actual_frequency"), ("onset", "onset"),
            ("measured_on", "measured_on"), ("condition", "condition"))
    if span:
        quote = body[span[0]:span[1]]
        for raw_key, key in keys:
            value = item.get(raw_key)
            inner = locate_quote_span(quote, value) if isinstance(value, str) and value else None
            if inner is not None:
                details.append({"key": key, "value": quote[inner[0]:inner[1]], "evidence": quote})
    else:
        quote = ""  # an unbound candidate's reported fields remain below, never invented evidence
    identity = [message.get("project_id"), message.get("message_id"), message.get("content_hash"),
                category, source, {key: value for key, value in item.items() if key != "ref"}]
    fact = {"id": hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "project_id": message.get("project_id"), "message_id": message.get("message_id"),
            "content_hash": message.get("content_hash"), "category": category, "details": details,
            "evidence": quote, "evidence_span": list(span) if span else None,
            "reported_subject": item.get("subject"), "posted_at_ts": message.get("posted_at_ts"),
            "posted_at": message.get("posted_at"), "source": source, "reported": dict(item)}
    temporal = (annotate(fact, body) if span else
                {"subject": item.get("subject", "unknown"), "period": "unknown", "unknown": True})
    if (source == "extract_v1" and category == "medications" or item_unverified(item) or not span
            or message.get("body_state") not in (None, "full")
            or span and name_span is None):
        temporal["unknown"] = True
    fact["temporal"] = temporal
    return fact

