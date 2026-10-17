#!/usr/bin/env python3
"""MCS structured extraction — rule-based v1.

Parses each stored message body into a structured JSON artifact
(kind='extract_v1') designed for reuse: timelines, search, rollups,
and later LLM passes (stored separately with kind='extract_llm').

Fields are grounded in the NCGG home-visit pharmacy guide + observed MCS
conventions (visit-date headers, med periods "8/17-9/6", SOAP sections,
vital strings, speaker labels). All work is LOCAL — no data leaves the box.

Usage:
  python3 extract.py --all              # every message lacking extract_v1
  python3 extract.py --project <id>     # one patient
  python3 extract.py --stats            # aggregate stats only
"""
import argparse
import bisect
import functools
import json
import os
import re
import sys
import time
from datetime import datetime

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every module dir (any depth) as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import _mcs_path  # noqa: F401
from clinical_values import FAMILY_PERSON, OTHER_PERSON, PAST_MARK, STAFF_PERSON
from patient_context import extract_context
from ledger import Ledger, LedgerReader
from mcs_queries import ELIGIBLE_MESSAGE
from mcs_util import HOME, acquire_run_lock, loads_dict, locate_quote_span

DB = os.path.join(HOME, "data", "ledger.db")
KIND = "extract_v1"

# ---------- patterns ----------

_SYMPTOMS = ("発熱", "お熱", "高熱", "血尿", "疼痛", "痛み", "嘔気", "吐き気",
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
# Only particles, fillers, time words or further listed symptoms may sit between a
# symptom and its negation, so 「発熱・疼痛なし」「発熱は本日はなし」 are negated while
# 「痛みが取れない」「食欲不振で食べられない」「発熱あり、疼痛なし」 keep 発熱 positive, and
# a negated negation (「ないわけではない」) stays positive.
_LISTED_SYMPTOM = "|".join(map(re.escape, sorted(_SYMPTOMS, key=len, reverse=True)))
_SYMPTOM_NEGATION = re.compile(
    r"さ?(?:[はもがの]|の?訴え|症状|等|など|特に|本日|今日|今回|現在|今朝|夜間|日中|明らかな|[ \t　]"
    r"|[・/／、とや](?:" + _LISTED_SYMPTOM + r"))*"
    r"(?:なし|無し|なく|無く|ありません|ない|認めず|認めない|認めません|認められず|認められない|認められません"
    r"|見られず|見られない|見られません|見当たらない"
    r"|否定(?!でき|しきれ|し(?:難|がた|ない|ません)|されない|されず)|[(（][-−－][)）])"
    r"(?!(?:では|じゃ)(?:ない|ありません)|わけ(?:では|じゃ)(?:ない|ありません))")
_ADHERENCE = ("残薬", "飲み忘れ", "飲み残し", "未使用", "服薬不良",
              "アドヒアランス", "一包化", "お薬カレンダー", "自己注射",
              "残あり", "残なし")
_URGENT = re.compile(
    r"至急|緊急(?!時|連絡(?:先|網|票|カード)|用|体制)(?:搬送|受診|対応)?|早急|すぐに|急ぎ|"
    r"救急(?:搬送|受診)?|搬送(?!先|元|方法|手段|経路|用|体制|依頼書)")
_URGENT_CLAUSES = re.compile(
    r"[。！？!?;\n]|しかし|ただし|けれど(?:も)?|"
    r"(?<=ない)が|(?<=ません)が|(?<=した)が|(?<=ました)が|(?<=です)が")
# A past/future word negates only when it directly modifies the urgent
# word (「来週、緊急受診」); 「明日の訪問前に至急」 or 「昨日の採血で…、至急」 stay current,
# and 「昨日から/より」「明日までに」 bound a current need, so they never match here.
_URGENT_NONCURRENT = re.compile(
    r"(?:昨日|一昨日|先日|先週|先月|昨年|以前|過去|"
    r"明日|明後日|来週|来月|来年|後日|今度|\d+日前|\d+日後)"
    r"(?:には|にも|は|に|も)?[、,\s　]*$")
# A request in the same clause stays current even after a time word
# (「明日、至急ご連絡ください」); only plans/reports are cancelled.
_URGENT_REQUEST = re.compile(r"ください|下さい|お願い|ほしい|欲しい|願います")
# A contingency head directly before the urgent word (「悪化した場合は救急搬送」
# 「続くようなら至急連絡」) describes a plan, not a current need; a clock time
# (「10時に至急」) is not a contingency.
_URGENT_CONDITIONAL = re.compile(
    r"(?:(?<![0-9０-９])(?:場合|際|とき|時|折)(?:に|には|は|も|の)?|"
    r"(?:なら|ようなら|たら|ましたら|れば|ければ|あれば|ようであれば|ようでしたら))"
    r"[、,\s　]*$")
_URGENT_HYPOTHETICAL = re.compile(r"もし|万一|万が一")
# 「すぐに」 alone is a plain adverb (「すぐに眠れる」); it is urgent only with an
# action or request in its scope.
_URGENT_SOON_ACTION = re.compile(
    r"ください|下さい|お願い|願います|ほしい|欲しい|必要|搬送|受診|往診|連絡|中止|"
    r"対応|確認|報告|相談|救急|呼(?:ん|び)|来て|向か|駆けつけ")
_URGENT_INACTIVE = re.compile(
    r"^(?:性|度|(?:の|な|に)?(?:対応|連絡|確認|受診|処置|搬送|要請))?"
    r"の?(?:は|も|が|を)?(?:で(?:は)?|じゃ|(?:し|され)(?:て(?:い|おり?)?)?|する)?"
    r"(?:ありません|ません|なかった|ない|なく|なし|"
    r"不要(?!では(?:ありません|ない))|必要(?:は|が)?(?:ありません|ない|なし)|"
    r"低い|低く|低め|高くない|高くありません|"
    r"済み|完了|(?:た|ました|された|されました)"
    # A past ending closes the phrase; 「ただちに」「たすけて」 are not past tense.
    r"(?=$|[、,]|ので|ため|から|けど|けれど|が|と))")

_SELF_PERSON = r"ご本人|患者本人|本人|患者(?:さん|様)?|利用者(?:さん|様)?"
_FAMILY_PERSON, _OTHER_PERSON = FAMILY_PERSON, OTHER_PERSON
_CLINICAL_OWNER = (r"症状|状態|体調|意識|呼吸|体温|血圧|収縮期|拡張期|容態|病歴|入院|退院|搬送|死亡|脈拍|酸素|薬|服薬|Cr|eGFR|AST|ALT|BUN|HbA1c|Hb|Na|K|血糖|採血|検査|BP|SpO2|熱|体重|身長|weight|height|BS|Glu|glucose|BNP|INR|CRP|Alb|[胸腹頭背腰]痛|"
                   + "|".join(map(re.escape, _SYMPTOMS)))
_NAMED_PERSON = r"[ぁ-んァ-ヶ一-龥A-Za-z0-9]{1,16}(?:さん|様)"
_COMPOUND_PERSON = rf"(?:{_FAMILY_PERSON}|{_OTHER_PERSON}|{_NAMED_PERSON})(?:ご本人|本人)"
_OWNER_MODIFIER = r"(?:(?:現在|以前|過去|本日)の|強い|弱い|激しい|軽い)*"
_PERSON = re.compile(
    rf"(?P<person>{_COMPOUND_PERSON}|{_SELF_PERSON}|{_FAMILY_PERSON}|{_OTHER_PERSON}|私|自分|{_NAMED_PERSON})"
    rf"(?P<link>について|の{_OWNER_MODIFIER}(?:{_CLINICAL_OWNER})|[ \t　]+(?={_CLINICAL_OWNER})|に対して|に対し|への|には|に(?={_CLINICAL_OWNER})|は|が|も|を|から|より|[:：])", re.I)
_DIRECT_OWNER = re.compile(rf"(?P<person>{_COMPOUND_PERSON}|{_SELF_PERSON}|{_FAMILY_PERSON}|{_OTHER_PERSON}|{_NAMED_PERSON})の{_OWNER_MODIFIER}[ \t　]*$")
_CLINICAL_ACTOR = re.compile(r"(?:が|は)[、,\s]*[^。！？!?;；\n、,]*?を(?:開始|中止|増量|減量|変更|処方|投与|測定)")
_CLINICAL_RECIPIENT = re.compile(rf"(?:{_COMPOUND_PERSON}|{_SELF_PERSON}|{_FAMILY_PERSON}|{_OTHER_PERSON}|{_NAMED_PERSON})(?:に対して|に対し|への)")
_SCOPE_HEADING = re.compile(
    rf"(?m)^[ \t　]*(?:[【［\[](.*?)[】］\]]|([^\n:：]{{1,24}})[:：]|"
    rf"({_COMPOUND_PERSON}|{_SELF_PERSON}|{_FAMILY_PERSON}|{_OTHER_PERSON}|過去|既往|現在|予定|目標|参考|基準)[ \t　]*$)[ \t　]*")
_REPORTING = re.compile(r"(?:より|から|が(?:報告|連絡|説明|相談|話)|は(?:報告|連絡|説明|相談|話))")
_CURRENT_MARK = re.compile(r"現在|今も|今は|本日|今日|続いて|持続|(?:昨日|先週|先月)から|(?:昨日|先週|先月)より")
_NONCURRENT_MARK = re.compile(r"過去|既往|以前(?!から|より)|先月(?!から|より)|先週(?!から|より)|昨年|一昨年")
# A reading taken 昨日/前回 is no current measurement, while 昨日中止 stays a recent change.
_MEASURED_PAST_MARK = re.compile(rf"(?:{PAST_MARK})(?!から|より)")
# 「昨日、体温…」: a past word heading the sentence before a comma dates every reading of it.
_SENTENCE_PAST_LEAD = re.compile(rf"[ \t　]*(?:{PAST_MARK})(?!から|より)(?:は|に|の|も)?[、,]")
_CONDITIONAL_MARK = re.compile(
    r"もし|万一|万が一|(?:場合|とき|(?<![0-9０-９一二三四五六七八九十])(?<!採血)(?<!測定)(?<!検査)(?<!服用)(?<!投与)(?<!訪問)(?<!往診)時)(?:は|には|に)|"
    r"なら(?!ない|なく|なかった|れ|ず|ぬ)|たら(?!しい)|ければ")
_SCOPE_BREAK = re.compile(r"[。！？!?;；\n、,]|しかし|ただし|だが|けれど(?:も)?|一方")
_REASON_CUE = re.compile(r"至急|緊急|救急|搬送|急変|意識|反応|呼吸|SpO2|血圧|体温|発熱|高熱|疼痛|痛み|[胸腹頭背腰]痛|苦し|出血|転倒|死亡|逝去|心肺|酸素|けいれん|痙攣|脱水|嘔吐|吐血|血尿|冷汗|(?:BP|BS|Glu)\s*\d", re.I)
_CLINICAL_REASON_CUE = re.compile(r"救急(?:搬送|受診)|搬送|急変|意識|反応|呼吸|SpO2|血圧|体温|発熱|高熱|疼痛|痛み|[胸腹頭背腰]痛|苦し|出血|転倒|死亡|逝去|心肺|酸素|けいれん|痙攣|脱水|嘔吐|吐血|血尿|冷汗|(?:Cr|eGFR|K|Na|血糖|BP|BS|Glu)\s*\d", re.I)


def clinical_urgency_quote(quote, *, symptom=None):
    """A medical report/event is separate from an urgent reply request; this is not a severity threshold."""
    if not isinstance(quote, str):
        return False
    pattern = (_CLINICAL_REASON_CUE if not isinstance(symptom, str) or not symptom.strip() else
               re.compile(_CLINICAL_REASON_CUE.pattern + "|" + re.escape(symptom), re.I))
    for match in pattern.finditer(quote):
        tail = quote[match.end():]
        prefix = r"(?:障害|消失|低下)" if match[0] == "意識" else r"困難" if match[0] == "呼吸" else ""
        if re.match(prefix + r"(?:は|も|が|を)?(?:して|認めて|認め|はして)?(?:では|で|じゃ)?"
                    r"(?:いません|ありません|ません|ない|なかった|なし|なく)", tail):
            continue
        return True
    return False


def urgent_request_quote(quote):
    """Recognize urgency of a requested action without asserting a patient's clinical state."""
    return (isinstance(quote, str) and _URGENT.search(quote) is not None
            and (_URGENT_REQUEST.search(quote) is not None or _URGENT_SOON_ACTION.search(quote) is not None))


def patient_clinical_quotes(body, *, patient_name=None):
    """Source-grounded acute-event clauses, for an already established urgent request."""
    quotes = []
    offset = 0
    for clause in re.split(r"[。！？!?;；\n]", body):
        start = body.find(clause, offset)
        offset = start + len(clause)
        # An unrelated routine measurement must not turn a paperwork request
        # into a clinical emergency. Cross-sentence linkage requires an
        # explicit acute event, not a new numeric severity heuristic.
        if not re.search(r"急変|救急(?:搬送|受診)|意識(?:を失|がない|消失|障害|低下)|呼吸(?:がない|困難|停止)|心肺停止", clause):
            continue
        if not clinical_urgency_quote(clause):
            continue
        kept, _ = patient_urgency_quotes(body, [clause], patient_name=patient_name)
        quotes.extend(quote for quote in kept if clinical_urgency_quote(quote))
    return list(dict.fromkeys(quotes))


def _person_scope(person, patient_name):
    # 「同居の娘さん」: _NAMED_PERSON also takes の, so read its relation tail, not the whole as a name.
    if re.fullmatch(rf"(?:.+の)?(?:{_FAMILY_PERSON})(?:ご本人|本人)?", person):
        return "family"
    if re.fullmatch(rf"(?:{_OTHER_PERSON})(?:ご本人|本人)?", person):
        return "other"
    alias = person.removesuffix("ご本人").removesuffix("本人").removesuffix("さん").removesuffix("様")
    alias = re.sub(r"\s+", "", alias)
    name = re.sub(r"\s+", "", patient_name) if isinstance(patient_name, str) else ""
    # Strip an explicit time modifier, never a の inside another person's name.
    known_name = bool(name) and (alias == name or re.fullmatch(
        rf"(?:{_CURRENT_MARK.pattern})の{re.escape(name)}", alias) is not None)
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
                and not re.search(rf"{_FAMILY_PERSON}|本人|自身|自分|症状|状態|体調|既往|病歴|受診|入院|退院|体温|血圧|服薬", label)):
            assignments.append(match.span())
            continue  # Provider assignment/signature fields do not own the patient's schedule.
        # A report-source heading is not the clinical subject below it.
        if re.fullmatch(rf"(?:{_FAMILY_PERSON}|{_OTHER_PERSON}|{_NAMED_PERSON})(?:から|より)", label):
            continue
        if re.fullmatch(rf"{_COMPOUND_PERSON}|{_NAMED_PERSON}", label):
            events.append((match.start(), _person_scope(label, patient_name), None))
        elif re.search(_OTHER_PERSON, label):
            events.append((match.start(), "other", None))
        elif re.search(_FAMILY_PERSON, label):
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
        subject = _person_scope(person, patient_name)
        events.append((match.start(), subject, None))
    return tuple(sorted(events, key=lambda event: event[0]))


def patient_source_scope(body, start, end, *, patient_name=None, default="patient", provider_fields=False,
                         measurement=False):
    """Resolve an original span's reported person and explicit noncurrent scope, never from a keyword mask."""
    if not isinstance(body, str) or not 0 <= start < end <= len(body):
        return "unknown"
    # Non-str names never match (_person_scope), so None keeps the cache key hashable.
    events = list(_scope_events(body, patient_name if isinstance(patient_name, str) else None,
                                bool(provider_fields)))  # already sorted by position
    # Owner phrases and break words never span these marks, so only the span's own clause is scanned.
    puncts = _scope_puncts(body)
    marks = bisect.bisect_left(puncts, start - 1)  # `$` may sit before a trailing newline at start-1
    owner = _DIRECT_OWNER.search(body, puncts[marks - 1] + 1 if marks else 0, start)
    if owner is not None:  # after equal positions, as a stable sort of an appended event
        bisect.insort_right(events, (owner.start(), _person_scope(owner["person"], patient_name), None),
                            key=lambda event: event[0])
    scope, period = default, "current"
    for position, subject, phase in events[:bisect.bisect_right(events, start, key=lambda event: event[0])]:
        if subject is not None:
            scope = subject
        if phase is not None:
            period = phase
    if scope != "patient":
        return scope
    # Qualifiers are evaluated in the span's own source clause, not from an
    # unrelated patient's/history sentence elsewhere in the same message.
    marks = bisect.bisect_left(puncts, start)
    left = puncts[marks - 1] + 1 if marks else 0
    left = max((m.end() for m in _SCOPE_BREAK.finditer(body, left, start)), default=left)
    right = _SCOPE_BREAK.search(body, end)
    clause = body[left:right.start() if right else len(body)]
    if _CONDITIONAL_MARK.search(clause):
        return "conditional"
    if period != "current" and not _CURRENT_MARK.search(clause):
        return period
    sentence = max(body.rfind(c, 0, start) for c in "。！？!?\n") + 1
    if (_NONCURRENT_MARK.search(clause)
            or (measurement and (_MEASURED_PAST_MARK.search(clause)
                                 or _SENTENCE_PAST_LEAD.match(body, sentence, start)))) \
            and not (_CURRENT_MARK.search(clause) or _URGENT_REQUEST.search(clause)):
        return "past"
    if re.search(r"(?:予定|目標|参考|基準)(?:の|は|値|:|：)", clause) and not _CURRENT_MARK.search(clause):
        return "planned"
    return scope


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


def patient_vitals(values, body, *, patient_name=None):
    """Filter flat cached vital values against original patient spans without reinterpreting other-person measurements."""
    if not isinstance(values, dict) or not isinstance(body, str):
        return {}
    kept = {}
    for key, pattern in _VITAL_PATTERNS.items():
        for match in re.finditer(pattern, body):
            if patient_source_scope(body, match.start(), match.end(), patient_name=patient_name,
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
    from clinical_values import _bp_side, _nearest_vital_label, _vitals_guard
    partial = {key: values[key] for key in ("sbp", "dbp")
               if key not in kept and type(values.get(key)) in (int, float)}
    grounded = _vitals_guard(body, partial)
    for match in re.finditer(r"\d+(?:[.．]\d+)?", body):
        start, end = match.span()
        if _nearest_vital_label(body, start, end) != "bp":
            continue
        side = _bp_side(body, start, end)
        label = re.search(r"(収縮期|拡張期)(?:血圧)?[^0-9]*$", body[max(0, start - 14):start])
        if side is None and label is not None:
            side = "sbp" if label[1] == "収縮期" else "dbp"
        if side is None or patient_source_scope(body, start, end, patient_name=patient_name,
                                                measurement=True) != "patient":
            continue
        if side in partial and grounded.get(side) == partial[side] == float(match[0].replace("．", ".")):
            kept[side] = partial[side]
    from clinical_values import _bp_pair_unproven
    if "sbp" in kept and "dbp" in kept and _bp_pair_unproven(
            body, kept["sbp"], kept["dbp"],
            keep=lambda s, e: patient_source_scope(
                body, s, e, patient_name=patient_name) == "patient"):
        del kept["sbp"], kept["dbp"]  # two readings never make one pair
    from clinical_values import _current_only
    return _current_only(kept, body, patient_name)  # an earlier reading is not the current one
_REQUESTS = ((r"ご?確認(?:を|お願い|ください|をお願い)", "confirm"),
             (r"(?:ご)?連絡(?:ください|をお願い|いただき)", "contact"),
             (r"共有(?:いたします|します|をお願い|させて)", "share"),
             (r"お願い(?:いたします|します|申し上げ)", "request"),
             (r"教えて(?:ください|いただけ|欲しい)", "ask"),
             (r"報告(?:いたします|します|をお願い)", "report"))

# Bounded repeats keep findall linear in the body length: the lazy name
# class shares digits with the dose, so unbounded runs backtrack cubically.
_MED_TOKEN = re.compile(
    r"([ァ-ヶー一-龥][ァ-ヶー一-龥A-Za-z0-9０-９・ー\-]{0,30}?)"
    # (?<!\d): a dose never starts inside a longer number (7+ digits match nothing)
    r"(?<!\d)(\d{1,6}(?:\.\d{1,4})?)\s{0,8}(mg|μg|mcg|g|mL|単位|錠|cap|カプセル|包|枚|本)")
_MED_CTX = re.compile(r"薬|処方|内服|外用|点眼|貼付|mg|錠|剤|坐薬|座薬|注射")
# date range like 8/17-9/6 or 9/24-10/7 (med periods)
_MED_PERIOD = re.compile(
    r"(?<![\d/])(?:(\d{4})/)?(\d{1,2}/\d{1,2})"
    r"\s*[-–~〜]\s*(?:(\d{4})/)?(\d{1,2}/\d{1,2})(?!\d)")
# Facility/provider names and service explanations are not encounter evidence.
ENCOUNTER_CUE = (r"(?:診察|往診|診療)(?!医|所|科|機関|方針|報酬|体制|契約|情報|録|券|室|"
                 r"の(?:説明|方針|契約|案内|資料)|について(?:の)?(?:説明|案内|資料)|"
                 r"に関する(?:説明|案内|資料))|診て(?:くれ|いただ|もら)")
RULE_VERSION = 24
_VISIT_DATE = re.compile(
    r"(?:(\d{4})[-/年])?(\d{1,2})[/月](\d{1,2})日?[　\s]*(?:\(|（)?[月火水木金土日]?"
    r"(?:\)|）)?[　\s]*(?:訪問(?:診療)?|診察|往診)")
_NEXT_VISIT_HEAD = (
    r"(?:次回|次の)[ \t　]*(?:の[ \t　]*)?(?:(?:訪問診療|訪問|往診|診察)"
    r"[ \t　]*(?:の[ \t　]*)?(?:予定(?:日(?:時)?)?|日時|日程|日)?|予定(?:日(?:時)?)?|日時|日程)"
    r"[ \t　]*(?:は|が)?[ \t　]*[:：]?[ \t　、,]*")
_NEXT_PLANNED = re.compile(
    rf"(?:(?P<scheduled>{_NEXT_VISIT_HEAD})(?P<newline>\r?\n[ \t　]*)?|次回[^。！？!?;；\r\n]{{0,8}}?)"
    r"(?<!\d)(?:(?P<year>\d{4})[-－/／年])?(?P<month>\d{1,2})[-－/／月](?P<day>\d{1,2})(?!\d)日?"
    r"(?:[ \t　]*[（(][月火水木金土日](?:曜(?:日)?)?[）)])?")
_NEXT_CANCELLED = re.compile(
    r"^[ \t　、,（(]*(?:\d{1,2}[:：時](?:\d{1,2}分?)?[ \t　]*)?"
    r"(?:の|に)?(?:訪問(?:診療)?|往診|診察)?(?:予定(?:していました|していた)?)?(?:です|でした)?(?:が|は|を)?"
    r"[ \t　、,]*(?:中止|キャンセル|延期|未定)")
_PLANNED_BEFORE = re.compile(r"次回|予定(?!通り|どおり)|明日|明後日|今度|来週")
# 予定通りなら (conditional) right before the date, beyond the 6-char window
_PLANNED_IF = re.compile(r"予定(?:通り|どおり)なら[　\s、,，]*$")
_PLANNED_AFTER = re.compile(r"[　\s]*(?:の|を)?[　\s]*(?:予定|します|いたします|致します)")
_VITAL_PATTERNS = {
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


def _next_planned_matches(body):
    """Keep explicit upcoming dates in their own schedule field, excluding cancelled or other-person plans."""
    for match in _NEXT_PLANNED.finditer(body):
        if match["newline"] and not re.match(
                r"[ \t　]*(?:(?:[01０１]?\d|[2２][0-3０-３])[:：時][0-5０-５]\d分?[ \t　]*)?(?:\r?\n|$)",
                body[match.end():]):
            continue
        if (re.search(r"中止|キャンセル|延期|未定", body[match.start():match.start("month")])
                or _NEXT_CANCELLED.match(body[match.end():])):
            continue
        scopes = (patient_source_scope(body, start, match.end(), provider_fields=True)
                  for start in (match.start(), match.start("year") if match["year"] else match.start("month")))
        if any(scope in ("family", "other", "unknown", "past", "conditional") for scope in scopes):
            continue
        yield match


def _ymd(month: int, day: int, year: int | None,
         posted: datetime | None = None,
         mode: str = "past") -> str | None:
    """Resolve an M/D (no year) against the post date. `mode`:
      'past'   — event already happened (visit_date): never after posted
      'future' — planned date (next_planned): never before posted
      'any'    — explicit year or neutral
    Year wraps at Dec/Jan are resolved by shifting the year, not by
    guessing (Oracle B26)."""
    if year is None:
        return None
    years = (year,) if mode == "any" else (year, year + 1, year - 1)
    for y in years:
        try:
            d = datetime(y, month, day).date()
        except ValueError:
            continue
        if posted is None or mode == "any":
            return d.isoformat()
        if mode == "future" and d < posted.date():
            continue        # push to next year
        if mode == "past" and d > posted.date():
            continue        # pull to last year
        return d.isoformat()
    return None


def _md_date(month: int, day: int, year: int):
    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None


def _period_dates(a, b, start_year, end_year, posted, context):
    start_md = tuple(map(int, a.split("/")))
    end_md = tuple(map(int, b.split("/")))
    wraps = end_md < start_md
    if start_year or end_year:
        sy = int(start_year) if start_year else int(end_year) - wraps
        ey = int(end_year) if end_year else sy + wraps
        start, end = _md_date(*start_md, sy), _md_date(*end_md, ey)
        return (start, end) if start and end and start <= end else (None, None)
    if posted is None:
        return None, None
    candidates = [(_md_date(*start_md, y), _md_date(*end_md, y + wraps))
                  for y in (posted.year - 1, posted.year, posted.year + 1)]
    candidates = [(s, e) for s, e in candidates if s and e]
    day = posted.date()
    if re.search(r"予定|開始予定|投与予定", context):
        candidates = [(s, e) for s, e in candidates if e >= day]
    if not candidates:
        return None, None
    def distance(pair):
        return max((pair[0] - day).days, (day - pair[1]).days, 0)
    candidates.sort(key=distance)
    if len(candidates) > 1 and distance(candidates[0]) == distance(candidates[1]):
        return None, None
    return candidates[0]


def patient_rule_urgency_quotes(body):
    """Current patient-only lexical emergency scopes, shared by writers and cached readers."""
    clause_start = 0
    for clause in _URGENT_CLAUSES.split(body):
        offset = body.find(clause, clause_start)
        clause_start = offset + len(clause)
        # Adjacent urgent words (「すぐに搬送」「緊急で搬送」) share one scope.
        spans: list[list[int]] = []
        for match in _URGENT.finditer(clause):
            if spans and clause[spans[-1][1]:match.start()].strip() in ("", "で"):
                spans[-1][1] = match.end()
            else:
                spans.append([match.start(), match.end()])
        cursor = 0
        for i, (start, end) in enumerate(spans):
            before = clause[cursor:start]
            cursor = end
            stop = spans[i + 1][0] if i + 1 < len(spans) else len(clause)
            if patient_source_scope(body, offset + start, offset + end) != "patient":
                continue
            if ((_URGENT_NONCURRENT.search(before)
                 and not _URGENT_REQUEST.search(clause[end:]))
                    or _URGENT_CONDITIONAL.search(before)
                    or _URGENT_HYPOTHETICAL.search(before)
                    or _URGENT_INACTIVE.match(clause[end:stop].strip())
                    or (clause[start:end] == "すぐに"
                        and not _URGENT_SOON_ACTION.search(clause[end:stop]))):
                continue
            return [clause.strip()]
    return []


def extract_message(body: str, posted_at: str) -> dict:
    """Structured view of one message. Missing fields are simply absent."""
    out: dict = {"v": 1}
    year = None
    posted = None
    try:
        posted = datetime.fromisoformat(posted_at)
        year = posted.year
    except (ValueError, TypeError):
        pass

    # --- events ---
    ev = set()
    event_patterns = {
        "visit": r"訪問(?:し|した|時|実施|致し)", "exam": ENCOUNTER_CUE,
        "admission": r"入院|退院|搬送|急性期",
        "eol": r"看取り|緩和|終末期|ACP|オピオイド|モルヒネ|逝去|お亡くなり|亡くなっ|死亡確認|息を引き取|心肺停止",
        "care": r"デイ|ショートステイ|ケアプラン|要介護|介護度",
        "adherence": r"残薬|服薬|服用|一包化", "medication": r"処方|変更|開始|中止|減量|増量",
    }
    event_mentions = []
    for event, pattern in event_patterns.items():
        for match in re.finditer(pattern, body):
            scope = patient_source_scope(body, match.start(), match.end())
            if scope in ("family", "other", "unknown"):
                event_mentions.append({"event": event, "scope": scope, "evidence": match[0]})
            else:
                ev.add(event)  # patient historical/planned mentions remain mentions
    if event_mentions:
        out["event_mentions"] = event_mentions
    if re.search(r"写真|画像|添付", body):
        ev.add("media_ref")
    if ev:
        out["events"] = sorted(ev)

    # --- visit date (first M/D preceding 訪問/診察) — a past event ---
    # A planned mention (次回10/5訪問予定) is not a visit that happened:
    # resolving it "past" would fabricate a date one year back.
    planned = list(_next_planned_matches(body))
    for m in _VISIT_DATE.finditer(body):
        # the look-behind stays inside the date's own sentence
        pre = re.split(r"[。．\n!！?？]",
                       body[max(0, m.start() - 6):m.start()])[-1]
        if _PLANNED_BEFORE.search(pre) \
                or _PLANNED_IF.search(body, 0, m.start()) \
                or _PLANNED_AFTER.match(body, m.end()) \
                or any(plan["scheduled"] and plan.start() <= m.start() < plan.end()
                       for plan in _NEXT_PLANNED.finditer(body)):
            continue
        d = _ymd(int(m.group(2)), int(m.group(3)),
                 int(m.group(1)) if m.group(1) else year,
                 posted, "any" if m.group(1) else "past")
        if d:
            out["visit_date"] = d
        break

    # --- next planned date — a FUTURE date ---
    if planned:
        m = planned[0]
        d = _ymd(int(m["month"]), int(m["day"]),
                 int(m["year"]) if m["year"] else year,
                 posted, "any" if m["year"] else "future")
        if d:
            out["next_planned"] = d

    # --- med periods (date ranges, typically regimens) ---
    pers = []
    for pm in _MED_PERIOD.finditer(body):
        sy, a, ey, b = pm.groups()
        # the range must sit in a medication context — a bare date span
        # (shift schedule, visit window) is not a regimen (F07)
        ctx = body[max(0, pm.start() - 60):pm.end() + 60]
        if not _MED_CTX.search(ctx):
            continue
        s, e = _period_dates(a, b, sy, ey, posted, ctx)
        period = {"raw": pm.group(0)}
        if s and e:
            period.update(start=s.isoformat(), end=e.isoformat())
        pers.append(period)
    if pers:
        out["med_periods"] = pers

    # --- medications (name + dose) ---
    meds = []
    for name, dose, unit in _MED_TOKEN.findall(body):
        if unit in ("mg", "μg", "mcg", "g", "mL", "%") and len(name) >= 2:
            meds.append({"name": name, "dose": dose + unit})
    if meds:
        seen, uniq = set(), []
        for x in meds:
            k = x["name"] + x["dose"]
            if k not in seen:
                seen.add(k)
                uniq.append(x)
        out["medications"] = uniq[:20]

    # --- rx change actions (ctx must look medication-related, else a
    # generic 開始/中止 like 食事開始 is a false positive) ---
    acts = []
    for m in re.finditer(r"(変更なし|変更|開始|中止|減量|増量|追加|停止|終了)",
                         body):
        s = m.group(1)
        ctx = body[max(0, m.start() - 14):m.end() + 4]
        before = body[max(0, m.start() - 10):m.start()]
        if not (_MED_CTX.search(ctx)
                or re.search(r"[ァ-ヶー・]{3,}$", before)):
            continue
        code = {"変更なし": "no_change", "開始": "start", "追加": "start",
                "中止": "stop", "停止": "stop", "終了": "stop",
                "減量": "decrease", "増量": "increase"}.get(s, "change")
        acts.append({"action": code, "ctx": ctx.strip()})
    if acts:
        out["rx_actions"] = acts[:10]

    # --- vitals ---
    vit, mentions = {}, []
    for k, pat in _VITAL_PATTERNS.items():
        for m in re.finditer(pat, body):
            scope = patient_source_scope(body, m.start(), m.end(), measurement=True)
            if k == "sbp":
                values = {"sbp": int(m.group(1)), "dbp": int(m.group(2))}
            else:
                v = m.group(1).replace("．", ".")  # int/float take full-width digits
                values = {k: float(v) if "." in v else int(v)}
            mentions.append({"scope": scope, "values": values, "evidence": m.group(0)})
            if scope == "patient":
                for key, value in values.items():
                    vit.setdefault(key, value)
    if vit:
        out["vitals"] = vit
    if any(mention["scope"] != "patient" for mention in mentions):
        out["vital_mentions"] = mentions

    # --- symptoms / adherence flags ---
    # A symptom counts when at least one mention is not directly negated (発熱なし, 吐き気は見られません).
    sym = [s for s in _SYMPTOMS
           if any(not _SYMPTOM_NEGATION.match(body, m.end()) for m in re.finditer(re.escape(s), body))]
    if sym:
        out["symptoms"] = sym
    adh = [s for s in _ADHERENCE if s in body]
    if adh:
        out["adherence_flags"] = adh

    # --- requests to other professions ---
    reqs = []
    sentences = list(re.finditer(r"[^。！？!?;；\r\n]+", body))
    for pat, kind in _REQUESTS:
        m = re.search(pat, body)
        if m:
            ctx = next((s[0].strip() for s in sentences
                        if s.start() <= m.start() and m.end() <= s.end()), "")
            if ctx and not any(r["ctx"] == ctx for r in reqs):
                reqs.append({"kind": kind, "ctx": ctx})
    if reqs:
        out["requests"] = reqs[:6]

    # --- actors / voices ---
    actors = set()
    if re.search(rf"{_FAMILY_PERSON}|親族", body):
        actors.add("family")
    if re.search(r"[（(]本人[)）]", body):
        actors.add("patient_voice")
    if re.search(r"[（(][^)）]{1,6}[)）]", body):
        actors.add("dialogue")
    if actors:
        out["actors"] = sorted(actors)

    # --- SOAP sections ---
    soap = [s for s in "SOAP"
            if re.search(r"(?:^|\n|\s)" + s + r"\s*[)）]", body)]
    if soap:
        out["soap"] = soap

    # Recompute the same source scopes when reading an older cached rule row.
    if patient_rule_urgency_quotes(body):
        out["urgency"] = "high"
    context = extract_context(body)
    if context:
        out["patient_context"] = context
    return out


_STALE = """CASE WHEN json_valid(a.meta) THEN
        json_extract(a.meta,'$.hash') IS NULL
        OR json_extract(a.meta,'$.hash') != m.content_hash
        OR COALESCE(json_extract(a.meta,'$.rule_version'),0) != ?
      ELSE 1 END"""


def _delete_stale(ledger) -> set:
    """Find extract_v1 artifacts whose pinned hash no longer matches the
    live body (content_hash drift — MCS allows edits) or whose rule
    version is old. NULL meta hashes (legacy CLI artifacts) count as stale
    — a missing hash must never shield a changed body (Oracle B19).
    Stale rows whose body can no longer be extracted are dropped here; the
    rest are returned and kept until _extract_rows replaces them, so a
    deadline cut never leaves a message without extract_v1."""
    stale = ledger.db.execute(f"""
      SELECT DISTINCT a.message_id, {ELIGIBLE_MESSAGE} AS ok FROM artifacts a
      JOIN messages m ON m.message_id=a.message_id
      WHERE a.kind=? AND {_STALE}
    """, (KIND, RULE_VERSION)).fetchall()
    with ledger.db:
        for r in stale:
            if not r["ok"]:
                ledger.db.execute(
                    "DELETE FROM artifacts WHERE kind=? AND message_id=?",
                    (KIND, r["message_id"]))
    return {r["message_id"] for r in stale if r["ok"]}


def run_pending(ledger, *, deadline: float | None = None) -> dict:
    """Extract messages lacking an extract_v1 artifact, and re-extract
    ones whose body changed on the server (content_hash drift — see
    _delete_stale). Returns {done, pids} so rollups can rebuild touched
    patients."""
    if deadline is not None and time.monotonic() >= deadline:
        return {"done": 0, "pids": []}
    _delete_stale(ledger)
    rows = ledger.db.execute(f"""
      SELECT m.message_id, m.project_id, m.body_text, m.posted_at,
             m.content_hash
      FROM messages m
      WHERE {ELIGIBLE_MESSAGE}
        AND (m.message_id NOT IN (SELECT message_id FROM artifacts
                                  WHERE kind=? AND message_id IS NOT NULL)
             OR EXISTS (SELECT 1 FROM artifacts a
                        WHERE a.kind=? AND a.message_id=m.message_id
                          AND {_STALE}))
      ORDER BY m.posted_at_ts DESC
    """, (KIND, KIND, RULE_VERSION)).fetchall()
    done, pids = _extract_rows(ledger, rows, deadline=deadline)
    return {"done": done, "pids": sorted(pids)}


_CHUNK = 200   # rows per commit: a RULE_VERSION bump re-extracts ~17k rows,
               # but the drainer's per-write lock must not wait long


def _extract_rows(ledger, rows, *, deadline: float | None = None) -> tuple[int, set[int]]:
    """Replace each row's extract_v1 artifact (delete + insert in the same
    transaction), committing every _CHUNK rows; a crash keeps committed
    chunks and the rest keeps its old artifact until the next pass.
    Returns the touched project ids."""
    pids = set()
    done = 0
    for i in range(0, len(rows), _CHUNK):
        with ledger.db:
            for r in rows[i:i + _CHUNK]:
                if deadline is not None and time.monotonic() >= deadline:
                    return done, pids
                d = extract_message(r["body_text"], r["posted_at"])
                ledger.db.execute(
                    "DELETE FROM artifacts WHERE kind=? AND message_id=?",
                    (KIND, r["message_id"]))
                ledger.artifact_add_tx(
                    KIND, json.dumps(d, ensure_ascii=False),
                    project_id=r["project_id"], message_id=r["message_id"],
                    model="rules-v1", meta={"hash": r["content_hash"],
                                            "rule_version": RULE_VERSION})
                pids.add(r["project_id"])
                done += 1
    return done, pids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="compatibility only; without --project every patient is processed")
    ap.add_argument("--project", type=int, default=0)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    # writers must hold the same run lock as the scheduled tick — a manual
    # extract running concurrently could double-process pending rows
    lock_fd = None
    if not args.stats:
        lock_fd = acquire_run_lock()
        if lock_fd is None:
            print(json.dumps({"ok": False, "error": "lock_held"}))
            return 3
    try:
        led = LedgerReader(DB) if args.stats else Ledger(DB)
    except Exception:
        if lock_fd is not None:
            os.close(lock_fd)
        raise
    q = f"""SELECT m.message_id, m.project_id, m.body_text, m.posted_at,
                  m.content_hash
           FROM messages m WHERE {ELIGIBLE_MESSAGE}"""
    params: list = []
    if args.project:
        q += " AND m.project_id=?"
        params.append(args.project)
    rows = led.db.execute(q + " ORDER BY m.posted_at_ts", params).fetchall()

    # Same stale handling as the tick path: a hash-drifted artifact
    # (edited body, or a legacy row without a pinned hash) must not
    # count as done — otherwise --all could never re-extract it.
    stale = set() if args.stats else _delete_stale(led)
    done = {r["message_id"] for r in led.db.execute(
        "SELECT message_id FROM artifacts WHERE kind=? "
        "AND message_id IS NOT NULL", (KIND,))} - stale
    todo = [r for r in rows if r["message_id"] not in done]
    if args.stats:
        todo = []
    if args.limit:
        todo = todo[:args.limit]

    _extract_rows(led, todo)
    n = len(todo)
    print(json.dumps({"extracted": n, "skipped_existing": len(done),
                      "total_msgs": len(rows)}, ensure_ascii=False))

    # aggregate stats over all extract_v1 artifacts
    import collections
    keys = collections.Counter()
    events = collections.Counter()
    for a in led.db.execute(
            "SELECT content FROM artifacts WHERE kind=?", (KIND,)):
        d = loads_dict(a["content"])
        if not isinstance(d, dict):
            continue
        for k in d:
            if k != "v":
                keys[k] += 1
        for e in d.get("events", []) if isinstance(d.get("events"), list) else []:
            if isinstance(e, str):
                events[e] += 1
    print(json.dumps({"field_coverage": dict(keys),
                      "events": dict(events)}, ensure_ascii=False, indent=1))
    led.close()
    if lock_fd is not None:
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
