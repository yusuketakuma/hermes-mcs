"""Resolve source person and period scope with bounded, process-local parse caches."""
import bisect
import functools
import re

from mcs_util import locate_quote_span

REPORTED_ABSENCE = r'(?:という|との)(?:報告|記録|記載|連絡)(?:は|が|も)?(?:ない|ありません|なし|未確認)'

FAMILY_PERSON = (r"ご家族|家族|ご主人|主人|旦那(?:さん|様)?|奥(?:さん|様)|お?嫁(?:さん)?|配偶者|"
                 r"お母(?:さん|様)|お父(?:さん|様)|母親|父親|祖母|祖父|義母|義父|(?<!分)母|父|"
                 r"(?:長男|長女|次男|次女)(?:さん|様)?|娘(?:さん|様)?|息子(?:さん|様)?|(?<![丈工])夫|妻|"
                 r"お?兄(?:さん|様)?|お?姉(?:さん|様)?|(?:弟|妹|孫)(?:さん|様)?|同居者")

CARE_WORKER = r"(?:ヘルパー|介護士|介護職員?)(?:さん|様)?"

STAFF_PERSON = r"(?:職員|スタッフ|看護師|医師|薬剤師|ケアマネ|ヘルパー|介護士|介護職員?)(?:さん|様)?"

OTHER_PERSON = rf"本人以外|他者|他人|第三者|他の(?:患者|利用者)|他患者|別の(?:患者|利用者)|別患者|{STAFF_PERSON}"

PAST_MARK = r"昨日|前回|先日|入院中|入院時"

SYMPTOMS = ("発熱", "お熱", "高熱", "血尿", "疼痛", "痛み", "嘔気", "吐き気",
             "嘔吐", "下痢", "便秘", "発疹", "めまい", "転倒", "むくみ", "浮腫",
             "咳", "痰", "喘鳴", "息苦し", "呼吸困難", "呼吸停止",
             "意識消失", "意識障害", "意識低下", "意識がない", "意識なし",
             "意識不明", "反応がない", "食欲低下", "食欲不振", "不眠", "せん妄",
             "誤嚥", "嚥下", "出血", "褥瘡", "皮膚", "倦怠", "疲労", "脱水",
             "低血糖",
             # care/nursing wording: excretion, swallowing, skin, behaviour, incidents
             "失禁", "尿閉", "頻尿", "血便", "下血", "むせ", "口腔乾燥", "口内炎", "しびれ", "麻痺",
             "振戦", "ふらつき", "傾眠", "意欲低下", "不安", "抑うつ", "不穏", "興奮", "幻覚", "妄想",
             "徘徊", "帰宅願望", "介護抵抗", "拒薬", "拒食", "掻痒", "かゆみ", "発赤", "発汗", "冷汗",
             "チアノーゼ", "頻脈", "徐脈", "不整脈", "体重減少", "転落", "誤薬")

URGENT_REQUEST = re.compile(r"ください|下さい|お願い|ほしい|欲しい|願います")

_SELF_PERSON = r"ご本人|患者本人|本人|患者(?:さん|様)?|利用者(?:さん|様)?"


_CLINICAL_OWNER = (r"症状|状態|体調|意識|呼吸|体温|血圧|収縮期|拡張期|容態|病歴|入院|退院|搬送|死亡|脈拍|酸素|薬|服薬|Cr|eGFR|AST|ALT|BUN|HbA1c|Hb|Na|K|血糖|採血|検査|BP|SpO2|熱|体重|身長|weight|height|BS|Glu|glucose|BNP|INR|CRP|Alb|[胸腹頭背腰]痛|"
                   + "|".join(map(re.escape, SYMPTOMS)))

NAMED_PERSON = r"[ぁ-んァ-ヶ一-龥A-Za-z0-9]{1,16}(?:さん|様)"

_COMPOUND_PERSON = rf"(?:{FAMILY_PERSON}|{OTHER_PERSON}|{NAMED_PERSON})(?:ご本人|本人)"

_OWNER_MODIFIER = r"(?:(?:現在|以前|過去|本日)の|強い|弱い|激しい|軽い)*"

_VITAL_OWNER = (r"(?:体温|BT|KT|脈拍?|心拍|HR|(?<![A-Za-z])P(?![A-Za-z])|呼吸|RR|血圧|BP|"
                r"SpO2|SPO2|Spo2|酸素|血糖|BS|Glu)")

_READING_OWNER = r"[0-9０-９]+(?:[.．][0-9０-９]+)?(?![0-9０-９.．]|[ \t　]*(?:名|人|歳|才|か?月|ヶ月|年|日|時|分|回目|件|階|号|番|代))"

_PERSON = re.compile(
    rf"(?P<person>{_COMPOUND_PERSON}|{_SELF_PERSON}|{FAMILY_PERSON}|{OTHER_PERSON}|私|自分|{NAMED_PERSON})"
    rf"(?P<link>について|の{_OWNER_MODIFIER}(?:{_CLINICAL_OWNER}|{_VITAL_OWNER})|[ \t　]+(?={_CLINICAL_OWNER})"
    rf"|に対して|に対し|への|には|に(?={_CLINICAL_OWNER})|は|が|も|を|から|より|[:：])", re.I)

_BARE_OWNER = re.compile(
    rf"(?P<person>{_COMPOUND_PERSON}|{_SELF_PERSON}|{FAMILY_PERSON}|{OTHER_PERSON}|{NAMED_PERSON})"
    rf"[ \t　]*(?={_VITAL_OWNER}|{_READING_OWNER})", re.I)

_DIRECT_OWNER = re.compile(rf"(?P<person>{_COMPOUND_PERSON}|{_SELF_PERSON}|{FAMILY_PERSON}|{OTHER_PERSON}|{NAMED_PERSON})の{_OWNER_MODIFIER}[ \t　]*$")

_CLINICAL_ACTOR = re.compile(r"(?:が|は)[、,\s]*[^。！？!?;；\n、,]*?を(?:開始|中止|増量|減量|変更|処方|投与|測定)")

_CLINICAL_RECIPIENT = re.compile(rf"(?:{_COMPOUND_PERSON}|{_SELF_PERSON}|{FAMILY_PERSON}|{OTHER_PERSON}|{NAMED_PERSON})(?:に対して|に対し|への)")

_CARE_ACTOR = re.compile(
    r"(?:が|は)[ \t　]*(?:(?:入浴|排泄|食事|更衣|移乗|歩行)?介助|訪問"
    r"|(?:体温|血圧|脈拍|SpO2)(?:を)?(?:測定|測っ|測り|計測)|検温)"
    r"(?:しました|した|しています|している|た|ました)?[ \t　]*(?=$|[。！？!?;；\n])", re.I)

_SCOPE_HEADING = re.compile(
    rf"(?m)^[ \t　]*(?:[【［\[](.*?)[】］\]]|([^\n:：]{{1,24}})[:：]|"
    rf"({_COMPOUND_PERSON}|{_SELF_PERSON}|{FAMILY_PERSON}|{OTHER_PERSON}|過去|既往|現在|予定|目標|参考|基準)[ \t　]*$)[ \t　]*")

_REPORTING = re.compile(r"(?:より|から|が(?:報告|連絡|説明|相談|話)|は(?:報告|連絡|説明|相談|話))")

CURRENT_MARK = re.compile(r"現在|今も|今は|本日|今日|続いて|持続|(?:昨日|先週|先月)から|(?:昨日|先週|先月)より")

_NONCURRENT_MARK = re.compile(r"過去|既往|以前(?!から|より)|先月(?!から|より)|先週(?!から|より)|昨年|一昨年")

_MEASURED_PAST_MARK = re.compile(rf"(?:{PAST_MARK})(?!から|より)")

_SENTENCE_PAST_LEAD = re.compile(rf"[ \t　]*(?:{PAST_MARK})(?!から|より)(?:は|に|の|も)?[、,]")

_CONDITIONAL_MARK = re.compile(
    r"もし|万一|万が一|(?:場合|とき|(?<![0-9０-９一二三四五六七八九十])(?<!採血)(?<!測定)(?<!検査)(?<!服用)(?<!投与)(?<!訪問)(?<!往診)時)(?:は|には|に)|"
    r"なら(?!ない|なく|なかった|れ|ず|ぬ)|たら(?!しい)|ければ")

_SCOPE_BREAK = re.compile(r"[。！？!?;；\n、,]|しかし|ただし|だが|けれど(?:も)?|一方")

_REASON_CUE = re.compile(r"至急|緊急|救急|搬送|急変|意識|反応|呼吸|SpO2|血圧|体温|発熱|高熱|疼痛|痛み|[胸腹頭背腰]痛|苦し|出血|転倒|死亡|逝去|心肺|酸素|けいれん|痙攣|脱水|嘔吐|吐血|血尿|冷汗|(?:BP|BS|Glu)\s*\d", re.I)

def person_scope(person, patient_name):
    # 「同居の娘さん」: _NAMED_PERSON also takes の, so read its relation tail, not the whole as a name.
    if re.fullmatch(rf"(?:.+の)?(?:{FAMILY_PERSON})(?:ご本人|本人)?", person):
        return "family"
    if re.fullmatch(rf"(?:{OTHER_PERSON})(?:ご本人|本人)?", person):
        return "other"
    alias = person.removesuffix("ご本人").removesuffix("本人").removesuffix("さん").removesuffix("様")
    alias = re.sub(r"\s+", "", alias)
    name = re.sub(r"\s+", "", patient_name) if isinstance(patient_name, str) else ""
    # Strip an explicit time modifier, never a の inside another person's name.
    known_name = bool(name) and (alias == name or re.fullmatch(
        rf"(?:{CURRENT_MARK.pattern})の{re.escape(name)}", alias) is not None)
    return "patient" if re.fullmatch(_SELF_PERSON, person) or known_name else "unknown"

@functools.lru_cache(maxsize=32)
def _scope_puncts(body):
    """Positions of the single-character _SCOPE_BREAK marks, cached per body."""
    return tuple(m.start() for m in re.finditer(r"[。！？!?;；、,\n]", body))

@functools.lru_cache(maxsize=32)
def _scope_events(body, patient_name, provider_fields):
    """Collect span-independent person/period events once per body, sorted by position, for patient_source_scope."""
    events, assignments = [], []
    # _REPORTING/_CLINICAL_ACTOR are matched in place (pos), never on a copied tail.
    stops = [m.start() for m in re.finditer(r"[。！？!?;；\n]", body)]
    for match in _SCOPE_HEADING.finditer(body):
        label = (match[1] or match[2] or match[3] or "").strip()
        if (provider_fields and (
                re.fullmatch(r"(?:.*(?:担当(?:医師|看護師|薬剤師|ケアマネ|スタッフ|職員)|主治医|診療医)(?:名|氏名|名前)?"
                             r"|(?:医師|看護師|薬剤師|ケアマネ)(?:名|氏名|名前))", label)
                or (match[2] is not None and re.fullmatch(r".+(?:医師|看護師|薬剤師|ケアマネ)", label)))
                and not re.search(rf"{FAMILY_PERSON}|本人|自身|自分|症状|状態|体調|既往|病歴|受診|入院|退院|体温|血圧|服薬", label)):
            assignments.append(match.span())
            continue  # Provider assignment/signature fields do not own the patient's schedule.
        # A report-source heading is not the clinical subject below it.
        if re.fullmatch(rf"(?:{FAMILY_PERSON}|{OTHER_PERSON}|{NAMED_PERSON})(?:から|より)", label):
            continue
        # 【ヘルパー報告】: a staff report heading names its writer, not the subject.
        if re.fullmatch(rf"(?:{STAFF_PERSON})(?:から|より)?の?(?:報告|記録|連絡|申し送り)", label):
            continue
        if re.fullmatch(rf"{_COMPOUND_PERSON}|{NAMED_PERSON}", label):
            events.append((match.start(), person_scope(label, patient_name), None))
        elif re.search(OTHER_PERSON, label):
            events.append((match.start(), "other", None))
        elif re.search(FAMILY_PERSON, label):
            events.append((match.start(), "family", None))
        elif re.search(_SELF_PERSON, label):
            events.append((match.start(), "patient", None))
        if re.search(r"過去|既往|以前", label):
            events.append((match.start(), None, "past"))
        elif re.search(r"予定|目標|参考|基準", label):
            events.append((match.start(), None, "planned"))
        elif re.search(r"現在|現状|本日|本人|患者", label):
            events.append((match.start(), None, "current"))
    for match in _PERSON.finditer(body):
        if any(left <= match.start() < right for left, right in assignments):
            continue
        person = match["person"]
        tail = match.start() + len(person)
        if _REPORTING.match(body, tail) and not re.fullmatch(_SELF_PERSON, person) and not re.fullmatch(_COMPOUND_PERSON, person):
            continue
        if (re.fullmatch(STAFF_PERSON, person)
                and _CLINICAL_ACTOR.match(body, tail)):
            k = bisect.bisect_left(stops, match.start())
            before = body[stops[k - 1] + 1 if k else 0:match.start()]
            k = bisect.bisect_left(stops, tail)
            after = body[tail:stops[k] if k < len(stops) else len(body)]
            if _CLINICAL_RECIPIENT.search(before) and not re.search(r"自身|自分", after):
                continue  # A prescribing/measuring clinician is the actor, not the recipient.
        # Only a complete care/measurement action names the actor rather than the owner.
        # Self-measurement, illness and modified/ambiguous clauses keep the staff owner.
        if re.fullmatch(CARE_WORKER, person) and _CARE_ACTOR.match(body, tail):
            continue
        subject = person_scope(person, patient_name)
        events.append((match.start(), subject, None))
    for match in _BARE_OWNER.finditer(body):
        if not any(left <= match.start() < right for left, right in assignments):
            events.append((match.start(), person_scope(match["person"], patient_name), None))
    return tuple(sorted(events, key=lambda event: event[0]))

def _source_scope_axes(body, start, end, *, patient_name=None, default="patient", provider_fields=False,
                         measurement=False):
    """Parse the existing person and period axes without collapsing either one."""
    if not isinstance(body, str) or not 0 <= start < end <= len(body):
        return "unknown", "unknown"
    # Non-str names never match (_person_scope), so None keeps the cache key hashable.
    events = list(_scope_events(body, patient_name if isinstance(patient_name, str) else None,
                                bool(provider_fields)))  # already sorted by position
    # Owner phrases and break words never span these marks, so only the span's own clause is scanned.
    puncts = _scope_puncts(body)
    marks = bisect.bisect_left(puncts, start - 1)  # `$` may sit before a trailing newline at start-1
    owner = _DIRECT_OWNER.search(body, puncts[marks - 1] + 1 if marks else 0, start)
    if owner is not None:  # after equal positions, as a stable sort of an appended event
        bisect.insort_right(events, (owner.start(), person_scope(owner["person"], patient_name), None),
                            key=lambda event: event[0])
    scope, period = default, "current"
    for position, subject, phase in events[:bisect.bisect_right(events, start, key=lambda event: event[0])]:
        if subject is not None:
            scope = subject
        if phase is not None:
            period = phase
    # Qualifiers are evaluated in the span's own source clause, not from an
    # unrelated patient's/history sentence elsewhere in the same message.
    marks = bisect.bisect_left(puncts, start)
    left = puncts[marks - 1] + 1 if marks else 0
    left = max((m.end() for m in _SCOPE_BREAK.finditer(body, left, start)), default=left)
    right = _SCOPE_BREAK.search(body, end)
    clause = body[left:right.start() if right else len(body)]
    if _CONDITIONAL_MARK.search(clause):
        return scope, "conditional"
    if period != "current" and not CURRENT_MARK.search(clause):
        return scope, period
    sentence = max(body.rfind(c, 0, start) for c in "。！？!?\n") + 1
    if measurement and CURRENT_MARK.search(clause) and (
            _NONCURRENT_MARK.search(clause) or _MEASURED_PAST_MARK.search(clause)):
        # 「昨日の体温38.5℃と本日の体温36.5℃」: one clause with both times — each reading
        # takes the nearest time word written before it within the clause
        before = body[left:start]
        nearest = max([(m.end(), "past") for rx in (_NONCURRENT_MARK, _MEASURED_PAST_MARK)
                       for m in rx.finditer(before)]
                      + [(m.end(), "current") for m in CURRENT_MARK.finditer(before)], default=None)
        if nearest is not None:
            return scope, nearest[1]
    if (_NONCURRENT_MARK.search(clause)
            or (measurement and (_MEASURED_PAST_MARK.search(clause)
                                 or _SENTENCE_PAST_LEAD.match(body, sentence, start)))) \
            and not (CURRENT_MARK.search(clause) or URGENT_REQUEST.search(clause)):
        return scope, "past"
    if re.search(r"(?:予定|目標|参考|基準)(?:の|は|値|:|：)", clause) and not CURRENT_MARK.search(clause):
        return scope, "planned"
    return scope, "current"

def patient_source_scope(body, start, end, *, patient_name=None, default="patient", provider_fields=False,
                         measurement=False):
    """Preserve the legacy scope projection for existing current-state consumers."""
    subject, period = _source_scope_axes(body, start, end, patient_name=patient_name, default=default,
                                        provider_fields=provider_fields, measurement=measurement)
    return subject if subject != "patient" or period == "current" else period


def source_axes(body, start, end, *, default="patient"):
    """Return independent reported person and period axes for a grounded assertion."""
    subject, period = _source_scope_axes(body, start, end, default=default)
    if period in ("conditional", "unknown"):
        return subject, period
    left = max(body.rfind(mark, 0, start) for mark in "。！？!?;；\n") + 1
    # A later explicit owner begins a new assertion, not the earlier person's time.
    owners = list(_PERSON.finditer(body, left, start))
    if owners:
        left = owners[-1].start()
    before = body[left:start]
    if _CONDITIONAL_MARK.search(before):
        return subject, "conditional"
    times = [(m.end(), "current") for m in CURRENT_MARK.finditer(before)]
    times += [(m.end(), "past") for rx in (_NONCURRENT_MARK, _MEASURED_PAST_MARK)
              for m in rx.finditer(before)]
    times += [(m.end(), "planned") for m in re.finditer(r"明日|翌日|来週|来月|予定|計画", before)]
    if times:
        period = max(times)[1]
    stop = _SCOPE_BREAK.search(body, end)
    tail = body[start:stop.start() if stop else len(body)]
    if re.search(r"予定|計画|検討中|見込み", tail) and not CURRENT_MARK.search(tail):
        period = "planned"
    return subject, period


def source_assertion_span(body, surface):
    """Locate one unique target and its existing source-clause boundaries."""
    span = locate_quote_span(body, surface) if isinstance(body, str) and isinstance(surface, str) else None
    if span is None:
        return None
    start, end = span
    marks = _scope_puncts(body)
    before = bisect.bisect_left(marks, start)
    left = marks[before - 1] + 1 if before else 0
    left = max((m.end() for m in _SCOPE_BREAK.finditer(body, left, start)), default=left)
    right = _SCOPE_BREAK.search(body, end)
    return left, right.start() if right else len(body)


def source_resolution(body, start, end, *, target_end=None):
    """Read explicit target-bound symptom resolution independently of negation and time."""
    if (not isinstance(body, str) or any(type(v) is not int for v in (start, end, target_end))
            or not 0 <= start < target_end <= end <= len(body)):
        return None
    tail = body[target_end:end]
    if (re.search(REPORTED_ABSENCE, tail)
            or re.match(r"[、,]?\s*" + REPORTED_ABSENCE, body[end:])):
        return None
    if re.search(r"[?？]|かどうか|(?:か|でしょうか)[。\s]*$|予定|計画|見込み|なら|場合|たら|とき|れば|"
                 r"かもしれ|疑い|不明|未確認|ではない|じゃない|ない(?:こと|わけ|訳)はない", tail):
        return None
    lead = r"^(?:は|が|も|を)?(?:現在|今は|本日|今日|まだ|既に|すでに)?\s*"
    not_resolved = (r"(?:(?:消失|消退|解消)(?:して(?:いない|いません|おらず|いなかった)|し(?:ない|ません)|"
                    r"(?:を|は|が|も)?認め(?:ず|ない|ません))|治ま(?:って(?:いない|いません|おらず)|らない|りません))")
    if re.search(lead + not_resolved, tail):
        return False
    resolved = (r"(?:(?:消失|消退|解消)(?:しました|した|しています|している|済み|を認めました|を認めた)|"
                r"治ま(?:りました|った|っています|っている))(?=$|[。．！!、,\s])")
    return True if re.search(lead + resolved, tail) else None


def current_medication_continuation(tail):
    """Recognize a complete affirmative current-use continuation after a medication action."""
    return re.fullmatch(r"し(?:て)?(?:服用|内服|使用|投与)(?:しています|している|中(?:です)?)[。．\s]*", tail) is not None


def source_polarity(body, start, end, *, target_end=None, medication=False):
    """Return direct target-bound negation, affirmation or unknown without defaulting omitted polarity."""
    if (not isinstance(body, str) or type(target_end) is not int
            or not 0 <= start < target_end <= end <= len(body)):
        return None
    tail = body[target_end:end]
    if re.search(r"ではない|じゃない|ない(?:こと|わけ|訳)はない|かもしれ|不明|疑い", tail):
        return None
    lead = r"^(?:は|が|も|を)?(?:現在|今は|特に|全く)?"
    negative = r"(?:なし|無し|ない|なく|ありません|認めず|認めません|認められない|見られない|みられません|して(?:いない|いません)|されて(?:いない|いません)|しない|しません|せず)"
    positive = r"(?:あり|有り|ある|出現|持続|続いて|認める|しています|している|しました|した(?!い|ら|場合|とき|予定|つもり)|済み)"
    if medication:
        from extract_schema import RX_DOSE, RX_PARTICLE
        lead = "^" + RX_PARTICLE + RX_DOSE + RX_PARTICLE + r"(?:で)?"
        negative = r"(?:(?:服用|内服|使用|投与)(?:は|を|も)?(?:して(?:いない|いません)|せず|しない|しません)|飲(?:んで(?:いない|いません)|まず|まない|みません)|使(?:わず|わない|って(?:いない|いません)))"
        positive = r"(?:服用|内服|使用|投与)(?:中|しています|している|した|しました)"
    if re.search(lead + negative, tail):
        return True
    if re.search(lead + positive, tail):
        return False
    return None


def patient_urgency_quotes(body, quotes, *, patient_name=None, default="patient"):
    """Keep only source-located patient quotes; another person's urgency remains in the original record."""
    kept, scopes = [], []
    for quote in quotes:
        span = locate_quote_span(body, quote) if isinstance(quote, str) and quote.strip() else None
        if span is None:
            scopes.append("unknown")
            continue
        cuts = sorted({span[0], span[1], *[m.end() for m in _SCOPE_BREAK.finditer(body, *span)],
                       *[m.start() for m in _PERSON.finditer(body, *span)]})
        pieces = [(left, right, patient_source_scope(body, left, right, patient_name=patient_name, default=default))
                  for left, right in zip(cuts, cuts[1:]) if body[left:right].strip()]
        if pieces and all(scope == "patient" for _, _, scope in pieces):
            text = body[span[0]:span[1]]
            scopes.append("patient")
            if text not in kept:
                kept.append(text)
            continue
        for left, right, scope in pieces:
            text = body[left:right].strip()
            if not text or (len(cuts) > 2 and not _REASON_CUE.search(text)):
                continue
            scopes.append(scope)
            if scope == "patient" and text not in kept:
                kept.append(text)
    return kept, scopes
