"""Verify allergy and adverse-reaction assertions against the post body, only ever weakening what the model claimed.

An assertion is one allergen with explicit axes:

  {"allergen": str | null, "kind": "allergy" | "adverse_reaction",
   "reaction": str | null, "subject": ..., "status": ..., "negated": bool,
   "certainty": "confirmed" | "suspected", "evidence": str}

Verification has two stages. `resolve` reads the axes from the body alone
(never the reference context) and is memoized on
(body, evidence, allergen, reaction); comparing the model's axes with the
resolution happens outside it, so many claims over one quote cost one parse.

- A claim whose quote is not uniquely in the body, whose allergen or
  reaction is not inside the quote, or whose sentence has no cue for its
  kind is dropped (not stored).
- A claim whose axes disagree with the body, or whose subject or time the
  body does not settle, is kept as a candidate (`unverified: true`).
- Nothing is ever corrected towards the patient, towards confirmed or
  towards current: a model `suspected` stays `suspected` even when the body
  reads as certain.

The rule constants below (R1-R11 of the extraction generation-12 plan) only
weaken: a rule that misfires turns a claim into a candidate or drops it,
never into a wrong confirmed assertion. `clinical_scope` is reused as is;
its person/period parse is the common subject/time reader (R3a, R4).
"""
from __future__ import annotations

import functools
import re
from typing import NamedTuple

import clinical_scope
from clinical_events import STATUSES, SUBJECTS
from mcs_util import locate_quote_span

FIELD = "allergies"
KINDS = ("allergy", "adverse_reaction")
CERTAINTIES = ("confirmed", "suspected")

# One assertion unit: a sentence; a heading line is its label plus the value on the same line.
_UNIT_BREAK = re.compile(r"[。！？!?\n]")

# R2: the cue for each kind. 抗アレルギー薬, アレルギー検査/外来/科 and アレルギー性鼻炎 name no allergy.
ALLERGY_CUE = re.compile(r"(?<!抗)(?:アレルギー|過敏症|不耐症?)(?!検査|テスト|薬|外来|科|性)")
ADVERSE_CUE = re.compile(r"副作用|副反応|有害事象|薬疹")
# R2: 「副作用が心配」「副作用の説明」 talk about a possibility, not a reaction.
CONCERN_AFTER = re.compile(r"(?:が|は|の)?(?:心配|不安|懸念|気にな|注意|説明|予防|対策)")
# R2: a relation alone (no cue word) needs a reaction from this vocabulary.
REACTION_WORDS = tuple(dict.fromkeys(clinical_scope.SYMPTOMS + (
    "じんましん", "蕁麻疹", "かゆ", "痒", "皮疹", "湿疹", "眠気", "肝機能", "腎機能", "腫れ",
    "アナフィラキシー", "ショック", "食欲")))
_TAKE = r"(?:飲|服用|内服|使用|使|貼|塗)"
_EMERGE = r"(?:出|出現|起こ|生じ|みられ|見られ)"

# R3b: another person named by a pointing word inside the sentence, before the allergen.
OTHER_REFERENT = re.compile(
    r"隣(?:室|の部屋|のベッド)の(?:方|人)|同室の(?:方|人)|"
    r"(?:その|あの|別の|他の|ほかの)(?:方|かた|人)(?!が(?:よい|良い|いい|安全|楽|早))|"
    r"同じ(?:苗字|名字|姓)の|同姓の")
# R3c: a post that says it is not this patient's record settles no assertion as the patient's.
NOT_THIS_PATIENT = re.compile(
    r"(?:こちらの|この|当該)?(?:ご本人|本人|利用者(?:さん|様)?|患者(?:さん|様)?)の(?:記録|件|こと|話|情報)では(?:ありません|ない)|"
    r"別の(?:利用者|患者)(?:さん|様)?の(?:件|記録|話|情報)")
# R3c: a document page headed with a person's name field names someone this text cannot match to the patient.
NAME_FIELD = re.compile(r"(?m)^[ \t　]*(?:患者|利用者|対象者)?(?:氏名|お名前|名前)[ \t　]*[:：][ \t　]*\S")
# R3d: an allergy / adverse-reaction history heading owns its value as the patient's.
HISTORY_HEADING = re.compile(
    r"^[ \t　]*[■◆◇●○・※＊*\-－]?[ \t　]*[【［\[]?[ \t　]*"
    r"(?P<label>(?:薬剤|食物|食品)?(?:アレルギー|副作用)(?:歴)?)[ \t　]*[】］\]]?[ \t　]*[:：]", re.M)
_LINE_PERSON = re.compile(rf"{clinical_scope.FAMILY_PERSON}|{clinical_scope.OTHER_PERSON}|"
                          rf"{clinical_scope.NAMED_PERSON}|{OTHER_REFERENT.pattern}")
# R3e: an allergen quoted from an earlier record is the patient's past one, unless the post denies it.
RECORD_BEFORE = re.compile(
    r"(?:紹介状|診療情報提供書|情報提供書|退院(?:時)?サマリー|看護サマリー|サマリー|お薬手帳|前医の記録|カルテ|記録)"
    r"(?:に|には|で|では)?[ \t　]*$")
RECORD_AFTER = re.compile(r"[ \t　]*(?:と|との)(?:記載|記録|あり|ある|書かれ)")
QUOTE_DENIAL = re.compile(
    r"覚え(?:が|は)(?:ない|ありません)|記憶(?:が|は)(?:ない|ありません)|(?:飲|使)んだこと(?:が|は)(?:ない|ありません)|"
    r"(?:事実|本当)では(?:ない|ありません)|否定(?:して|され)|誤り|間違い")
# R4: time words. 「飲んだときに」 after a past word and the もし of かもしれ are not conditions;
# they are masked (same length) before the shared period parse reads the body.
_PAST_WORD = (r"以前|過去|昔|かつて|前に|昨年|"
              r"[0-9０-９一二三四五六七八九十数]+(?:年|か月|ヶ月|カ月|週間|日)前")
WHEN_AFTER_PAST = re.compile(rf"(?:{_PAST_WORD})[^。！？!?\n]*?(?P<when>(?:んだ|った|いた|した|きた|えた|出た)(?:とき|時)(?:に|は)?)")
MAYBE = re.compile(r"(?<=か)もし(?=れ)")
NUMBERED_AGO = re.compile(r"[0-9０-９一二三四五六七八九十数]+(?:年|か月|ヶ月|カ月|週間|日)前(?!から|より)")
_MASK = "〓"
# R5: negation bound to a cue (heading values included), to a reaction word, or a tolerance after the allergen.
_NOT_AFTER = r"(?!か|よう|ため)"
CUE_NEGATION = re.compile(
    r"(?:歴)?[ \t　]*[:：]?[ \t　]*(?:は|が|も|について(?:は)?)?[ \t　]*"
    r"(?:特に|全く|一切|今のところ|現在|今は|これまで)?[ \t　]*"
    r"(?:ない|なし|無し|ありません|ございません|特記(?:事項)?なし|該当なし|認め(?:ない|ません|られない|られません)|"
    r"出て(?:いない|いません|おらず)|出現(?:して)?(?:いない|いません)|起きて(?:いない|いません)|"
    r"否定(?:され|的|(?=$|[。\s])))" + _NOT_AFTER)
REACTION_NEGATION = re.compile(
    r"(?:は|が|も)?(?:特に|全く)?(?:出て(?:いない|いません|おらず)|出なかった|みられ(?:ない|ません)|"
    r"見られ(?:ない|ません)|ない|なし|ありません)" + _NOT_AFTER)
TOLERATED = re.compile(r"問題(?:なし|無し|なく|ない|ありません|ございません)|大丈夫")
# R5: 「…との記録はない」 is a missing record, neither a negation nor an affirmation.
REPORTED_ABSENCE = re.compile(clinical_scope.REPORTED_ABSENCE)
# R6: hedged wording in the sentence makes the assertion suspected.
UNCERTAIN = re.compile(
    r"疑い|かもしれ|可能性|おそらく|恐らく|ようだ|ようです|みたい|と思われ|と考えられ|未確定|不明|未確認|"
    r"はっきりしない|分かって(?:いない|いません)|わかって(?:いない|いません)|確認中|相談中|検査中")
# R7: a follow-up still to be done leaves a current assertion open.
FOLLOW_UP = re.compile(r"確認します|確認する予定|聞いてみます|伺います|検査(?:を)?(?:受け|予定)")
# R11: form fields whose mark cannot be read from text, table rows, and empty heading values.
FORM_FIELD = re.compile(r"有[ \t　]*[・/／][ \t　]*無|あり[ \t　]*[・/／][ \t　]*なし|[□☐]|"
                        r"■[ \t　]*(?:有|無|あり|なし)|(?:有|無|あり|なし)[ \t　]*■|[|｜][^\n]*[|｜]")
EMPTY_VALUE = re.compile(r"[:：][ \t　]*$")
# An allergen that is only a heading value or a form mark names nothing.
_VALUE_WORD = re.compile(r"[ \t　]*(?:なし|無し|特になし|ない|ありません|有|無|あり|不明|[・/／□☐■])+[ \t　]*")


class Resolution(NamedTuple):
    """Axes the body supports for one (evidence, allergen, reaction), or why the claim is dropped."""
    drop: str | None = None
    evidence: str = ""
    kinds: frozenset = frozenset()
    subject: str = "unknown"
    status: str = "unknown"
    history: bool = False
    negated: bool | None = None
    certainty: str = "confirmed"
    conflict: bool = False


def _unit(body, position):
    left = max((body.rfind(mark, 0, position) for mark in "。！？!?\n"), default=-1) + 1
    stop = _UNIT_BREAK.search(body, position)
    return left, stop.start() if stop else len(body)


def _line(body, position):
    left = body.rfind("\n", 0, position) + 1
    right = body.find("\n", position)
    return left, right if right >= 0 else len(body)


def _masked(body):
    """The body with non-conditional もし/とき masked, for the shared period parse (R4)."""
    chars = list(body)
    for match in MAYBE.finditer(body):
        chars[match.start():match.end()] = _MASK * (match.end() - match.start())
    for sentence in re.finditer(r"[^。！？!?\n]+", body):
        for match in WHEN_AFTER_PAST.finditer(body, sentence.start(), sentence.end()):
            start, end = match.span("when")
            chars[start:end] = _MASK * (end - start)
    return "".join(chars)


def _cues(text, start=0, end=None):
    """(start, end, kind) of every allergy cue and every adverse cue that is not a concern."""
    end = len(text) if end is None else end
    found = [(m.start(), m.end(), "allergy") for m in ALLERGY_CUE.finditer(text, start, end)]
    found += [(m.start(), m.end(), "adverse_reaction") for m in ADVERSE_CUE.finditer(text, start, end)
              if not CONCERN_AFTER.match(text, m.end())]
    return sorted(found)


def cue_spans(body) -> list[tuple[int, int, str]]:
    """Positions of the allergy / adverse-reaction cue words in a body (R2), for prefilters and context views."""
    return _cues(body) if isinstance(body, str) else []


def _related(unit, allergen, reaction):
    """R2: an allergen-to-reaction relation in one sentence (only an adverse reaction)."""
    if allergen is None or reaction is None or not any(word in reaction for word in REACTION_WORDS):
        return False
    a, r = re.escape(allergen), re.escape(reaction)
    return bool(re.search(rf"{a}(?:で|による|により)[^。！？!?\n]*?{r}", unit)
                or re.search(rf"{a}を{_TAKE}[^。！？!?\n]*?(?:とき|時|後|ところ|て)(?:に|から)?"
                             rf"[^。！？!?\n]*?{r}が{_EMERGE}", unit))


def _supported_kinds(body, left, right, allergen, reaction):
    """R2: the kinds a sentence has a cue (or, for an adverse reaction, a relation) for."""
    kinds = {kind for _, _, kind in _cues(body, left, right)}
    if _related(body[left:right], allergen, reaction):
        kinds.add("adverse_reaction")
    return frozenset(kinds)


def _subject(body, masked, start, end, *, cue_anchor=False):
    """R3a-R3e: whose assertion the allergen (or, without one, the cue) at body[start:end] is.

    Returns (subject, history heading, quoted from a record, denied).
    """
    subject, _ = clinical_scope.source_axes(masked, start, end, default="unknown")
    left, right = _unit(body, start)
    if OTHER_REFERENT.search(body, left, start):
        subject = "other"
    not_patient = bool(NOT_THIS_PATIENT.search(body) or NAME_FIELD.search(body))
    history = False
    line_left, line_right = _line(body, start)
    heading = HISTORY_HEADING.match(body, line_left, line_right)
    if heading is not None:
        history = "歴" in heading["label"]
        if (subject == "unknown" and not not_patient
                and (start >= heading.end() or cue_anchor and heading.start("label") <= start < heading.end("label"))
                and not _LINE_PERSON.search(body, line_left, line_right)):
            subject = "patient"
    quoted = denied = False
    opening = body.rfind("「", left, start)
    closing = body.find("」", end, right)
    if opening >= 0 and closing >= 0 and "」" not in body[opening:start] and \
            RECORD_BEFORE.search(body, left, opening) and RECORD_AFTER.match(body, closing + 1):
        quoted = True
        if subject in ("unknown", "patient") and not not_patient and \
                not _LINE_PERSON.search(body, opening, closing):
            subject = "patient"
        _, after = _unit(body, right + 1) if right < len(body) else (right, right)
        denied = bool(QUOTE_DENIAL.search(body, closing, after))
    if not_patient and subject == "patient":
        subject = "unknown"
    return subject, history, quoted, denied


def _status(body, masked, start, end, *, quoted):
    """R4: the time of the allergen at body[start:end]."""
    if quoted:
        return "past"
    _, period = clinical_scope.source_axes(masked, start, end, default="unknown")
    left, _ = _unit(body, start)
    if period == "current" and NUMBERED_AGO.search(body, left, start):
        return "past"
    return period


def _polarity(body, left, right, start=None):
    """R5: True (negated), False (affirmed) or None (mixed, or a missing record) for one sentence."""
    if REPORTED_ABSENCE.search(body, left, right):
        return None
    signals = [bool(CUE_NEGATION.match(body, cue_end, right)) for _, cue_end, _ in _cues(body, left, right)]
    for word in REACTION_WORDS:
        for match in re.finditer(re.escape(word), body[left:right]):
            signals.append(bool(REACTION_NEGATION.match(body, left + match.end(), right)))
    if start is not None and TOLERATED.search(body, start, right):
        signals.append(True)
    if not any(signals):
        return False
    return True if all(signals) else None


_OCCURRENCE_LIMIT = 64


def _occurrences_conflict(body, masked, allergen):
    """R8: an allergen written more than once must read the same (subject, polarity) at every place."""
    seen = set()
    for index, match in enumerate(re.finditer(re.escape(allergen), body)):
        if index >= _OCCURRENCE_LIMIT:
            return True  # Too many places to read: leave the assertion open.
        subject = _subject(body, masked, match.start(), match.end())[0]
        left, right = _unit(body, match.start())
        seen.add((subject, _polarity(body, left, right, match.start())))
    return len(seen) > 1


def _status_agrees(model, computed, history):
    """R4: a time the body does not settle, or a different one, is not confirmed (current = past under a 歴 heading)."""
    if computed == "unknown":
        return False
    return model == computed or (history and {model, computed} <= {"current", "past"})


@functools.lru_cache(maxsize=4096)
def resolve(body, evidence, allergen, reaction) -> Resolution:
    """Read the axes the body supports for one quoted claim (memoized; independent of the claimed axes)."""
    if (not isinstance(body, str) or not isinstance(evidence, str) or not evidence.strip()
            or any(value is not None and not (isinstance(value, str) and value.strip())
                   for value in (allergen, reaction))):
        return Resolution(drop="形式が不正です")
    span = locate_quote_span(body, evidence)
    if span is None:  # R1/R9: the reference context is never searched.
        return Resolution(drop="引用が対象の本文に一意に見つかりません")
    quote = body[span[0]:span[1]]
    if allergen is not None and (allergen not in quote or _VALUE_WORD.fullmatch(allergen)):
        return Resolution(drop="アレルゲンが引用の中にありません")
    if allergen is not None:
        start = span[0] + quote.index(allergen)
        end = start + len(allergen)
    else:
        cues = _cues(quote)
        if not cues:
            return Resolution(drop="アレルギー・副作用の手がかり語がありません")
        start, end = span[0] + cues[0][0], span[0] + cues[0][1]
    left, right = _unit(body, start)
    if reaction is not None and not any(
            left <= span[0] + m.start() and span[0] + m.end() <= right
            for m in re.finditer(re.escape(reaction), quote)):
        return Resolution(drop="反応が同じ文の引用の中にありません")
    if FORM_FIELD.search(body, left, right) or EMPTY_VALUE.search(body[left:right]):
        return Resolution(drop="様式の欄は印の位置や値が読めません")
    kinds = _supported_kinds(body, left, right, allergen, reaction)
    if not kinds:
        return Resolution(drop="アレルギー・副作用の手がかり語がありません")
    masked = _masked(body)
    subject, history, quoted, denied = _subject(body, masked, start, end, cue_anchor=allergen is None)
    status = _status(body, masked, start, end, quoted=quoted)
    conflict = denied or bool(FOLLOW_UP.search(body, left, right) and status == "current")
    if allergen is not None and not conflict:
        conflict = _occurrences_conflict(body, masked, allergen)
    return Resolution(
        evidence=quote, kinds=kinds, subject=subject, status=status, history=history,
        negated=_polarity(body, left, right, start if allergen is not None else None),
        certainty="suspected" if UNCERTAIN.search(body, left, right) else "confirmed",
        conflict=conflict)


def _shape(item):
    return (isinstance(item, dict) and item.get("kind") in KINDS and item.get("subject") in SUBJECTS
            and item.get("status") in STATUSES and type(item.get("negated")) is bool
            and item.get("certainty") in CERTAINTIES and isinstance(item.get("evidence"), str)
            and all(item.get(key) is None or isinstance(item[key], str) for key in ("allergen", "reaction")))


def _judge(item, res):
    """Compare the claimed axes with a resolution: (kept item or None, drop reason)."""
    if res.drop is not None:
        return None, res.drop
    if item["kind"] not in res.kinds:
        return None, "種類の手がかり語が同じ文にありません"
    if item["allergen"] is None and res.negated is not True:
        return None, "アレルゲンの無い主張は否定のときだけ残せます"
    open_axis = (res.conflict or res.subject == "unknown" or item["subject"] != res.subject
                 or not _status_agrees(item["status"], res.status, res.history)
                 or res.negated is None or item["negated"] != res.negated
                 or item["certainty"] == "confirmed" and res.certainty == "suspected")
    kept = {"allergen": item["allergen"], "kind": item["kind"], "reaction": item["reaction"],
            "subject": item["subject"], "status": item["status"], "negated": item["negated"],
            "certainty": item["certainty"], "evidence": res.evidence}
    if open_axis:
        kept["unverified"] = True
    return kept, None


def _key(item):
    return (item.get("allergen"), item.get("kind"), item.get("evidence"), item.get("subject"),
            item.get("status"), item.get("negated"), item.get("certainty"))


def same_assertion(a, b) -> bool:
    """R10: one allergen, kind, quote and axis set is one assertion, whatever chunk or reaction wording repeated it."""
    return isinstance(a, dict) and isinstance(b, dict) and _key(a) == _key(b)


def _better(new, old):
    """Of two copies of one assertion keep the confirmed one, then the one naming a reaction."""
    return ((not new.get("unverified"), new.get("reaction") is not None)
            > (not old.get("unverified"), old.get("reaction") is not None))


def validate_items(items, body, drops=None, *, ctx=False) -> tuple[list, int]:
    """Verify model allergy claims against the target body; returns (kept items, dropped count).

    `drops`, when given, collects bounded repair detail in the extract_validation
    shape ({"ev": [...], "items": ["allergies"], "reasons": [...]}). `ctx` says the
    prompt carried reference context, which is never a valid source (R9).
    """
    kept, dropped = {}, 0
    for item in items if isinstance(items, list) else []:
        result, reason = None, "形式が不正です"
        if _shape(item):
            res = resolve(body, item.get("evidence"), item.get("allergen"), item.get("reaction"))
            result, reason = _judge(item, res)
            if result is None and res.drop is not None and res.drop.startswith("引用") and ctx:
                reason = "参考コンテキストの文は引用にできません。対象の投稿の本文から引用してください"
        if result is None:
            dropped += 1
            if drops is not None:
                if reason.startswith(("引用", "参考")) and isinstance(item, dict):
                    quotes = drops.setdefault("ev", [])
                    if len(quotes) < 5:
                        quotes.append(str(item.get("evidence"))[:60])
                fields = drops.setdefault("items", [])
                if FIELD not in fields and len(fields) < 5:
                    fields.append(FIELD)
                reasons = drops.setdefault("reasons", [])
                if len(reasons) < 5:
                    reasons.append(f"{FIELD}: {reason}")
            continue
        key = _key(result)
        if key not in kept or _better(result, kept[key]):
            kept[key] = result
    return list(kept.values()), dropped


def stored_assertions(meta, content, *, first_generation):
    """Confirmed assertions of one stored extraction, None when its generation never looked for them.

    None is "not acquired" (an older generation, an unreadable version or field);
    [] is "looked and found none". Unverified candidates are never returned. The
    caller checks that the row is a per-post extraction: other kinds (for example
    semantic_facts_v4, whose meta says extract_version 4) read as not acquired.
    """
    version = meta.get("extract_version") if isinstance(meta, dict) else None
    if type(version) is not int or version < first_generation or not isinstance(content, dict):
        return None
    items = content.get(FIELD, [])
    if not isinstance(items, list):
        return None
    return [dict(item) for item in items if isinstance(item, dict) and item.get("unverified") is not True]


_PHRASE_BREAK = re.compile(r"[、。，,．！？!?\n「」（）()：:・｜|\s]+|から|より|まで|[はがをにでのともへや]")


def allergen_candidates(body, labels=(), meds=()) -> list:
    """Allergen strings for exhaustive claim gates: label allergens, null, body phrases (20 longest) and med names.

    A test aid for the validator gate; runtime code never calls it.
    """
    found = [None]
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("allergen"), str):
            found.append(label["allergen"])
    phrases = sorted({part for part in _PHRASE_BREAK.split(body) if part.strip()},
                     key=lambda part: (-len(part), part))
    found += phrases[:20]
    found += [med["name"] for med in meds if isinstance(med, dict) and isinstance(med.get("name"), str)]
    return list(dict.fromkeys(found))
