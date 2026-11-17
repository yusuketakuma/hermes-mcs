"""Confirm an allergy or adverse-reaction assertion only where an explicit sentence template in the post body licenses it.

An assertion is one allergen with explicit axes:

  {"allergen": str | null, "kind": "allergy" | "adverse_reaction",
   "reaction": str | null, "subject": ..., "status": ..., "negated": bool,
   "certainty": "confirmed" | "suspected", "evidence": str}

The default is inverted: a claim stays a candidate (`unverified: true`)
unless the body positively licenses it. `resolve` reads the body alone (never
the reference context) and is memoized on (body, evidence, allergen,
reaction); comparing the model's axes with the licences happens outside it.

- A claim whose quote is not uniquely in the body, whose allergen or
  reaction is not inside the quote and its sentence, whose allergen is a
  value word or carries punctuation, whose sentence is a form field, or whose
  sentence has no cue for its kind is dropped (not stored).
- The unit is the sentence (or the line) holding the allergen. The claimed
  allergen is replaced there by a placeholder and the unit must fully match
  one of `TEMPLATES`: each runs from the subject position (a self marker, a
  family word, an allergy / adverse-reaction heading, or a quoted record) to
  a plain declarative ending. The slot takes only the placeholder, so a cut
  of a longer name, a particle or a cue word in the allergen never matches;
  any other ending (question, hedge, conjecture, condition, request, worry,
  plan) or any word between the subject and the allergen is no match by
  construction. A template fixes kind, polarity, certainty, subject and time.
- A licence is weakened only by the small lists below, each for a case a
  template would otherwise confirm: another post-level owner (`NAME_FIELD`,
  `DISCLAIMER`), a later denial (`DENIAL`), exceptions to a "none" form
  (`EXCEPTION`), names that end in an exception (`NOUN_EXCLUDE`) and a
  different reading of the same allergen elsewhere (R8, `TOLERATED`).
- Nothing is ever corrected towards the patient, towards confirmed or
  towards current: a model `suspected` stays `suspected` even when the body
  licenses a certain assertion.
"""
from __future__ import annotations

import functools
import itertools
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
# An allergen string that carries sentence or clause punctuation or brackets names no single allergen.
_PUNCTUATED = re.compile(r"[。、，,！？!?「」『』（）()\n]")
_PHRASE_BREAK = re.compile(r"[、。，,．！？!?\n「」（）()：:・｜|\s]+|から|より|まで|[はがをにでのともへや]")

# R2: the cue for each kind. 抗アレルギー薬, アレルギー検査/外来/科 and アレルギー性鼻炎 name no allergy.
ALLERGY_CUE = re.compile(r"(?<!抗)(?:アレルギー|過敏症|不耐症?)(?!検査|テスト|薬|外来|科|性)")
ADVERSE_CUE = re.compile(r"副作用|副反応|有害事象|薬疹")
# R2: a relation alone (no cue word) needs a reaction from this vocabulary; it is also the reaction slot.
REACTION_WORDS = tuple(dict.fromkeys(clinical_scope.SYMPTOMS + (
    "じんましん", "蕁麻疹", "かゆ", "痒", "皮疹", "湿疹", "眠気", "肝機能", "腎機能", "腫れ",
    "アナフィラキシー", "ショック", "食欲")))
_TAKE = r"(?:飲|服用|内服|使用|使|貼|塗)"
_EMERGE = r"(?:出|出現|起こ|生じ|みられ|見られ)"

# R11: form fields whose mark cannot be read from text, table rows, and empty heading values.
FORM_FIELD = re.compile(r"有[ \t　]*[・/／][ \t　]*無|あり[ \t　]*[・/／][ \t　]*なし|[□☐]|"
                        r"■[ \t　]*(?:有|無|あり|なし)|(?:有|無|あり|なし)[ \t　]*■|[|｜][^\n]*[|｜]")
EMPTY_VALUE = re.compile(r"[:：][ \t　]*$")
# An allergen that is only a heading value, a form mark or a not-yet-asked note names nothing.
_VALUE_WORD = re.compile(r"[ \t　]*(?:なし|無し|特になし|ない|ありません|有|無|あり|不明|未聴取|聴取中|未確認|確認中|不詳|"
                         r"[・/／□☐■])+[ \t　]*")

# ---------------------------------------------------------------------------------------------
# Weakeners. Each entry is needed by a named forbid case of the gate (tests remove one at a time).

# R3c: a post with a name / ID field or a name line belongs to someone this text cannot match to the patient.
NAME_LABELS = ("氏名", "名前", "患者", "利用者", "入居者", "対象者", "フリガナ", "ID", "被保険者")
NAME_HONORIFICS = ("様", "さん", "氏")
# R3c: a sentence that puts a record noun next to an otherness or negation marker, or next to a person
# other than the patient, disowns the post.
DISCLAIMER_NOUNS = ("記録", "情報", "内容", "もの", "件", "話")
DISCLAIMER_MARKS = ("別", "他", "違", "誤", "混", "ではな", "ではありません")
# R3e/R7: a later sentence that denies, retracts or still doubts what came before.
DENIAL = ("覚え", "記憶", "事実", "否定", "誤り", "間違", "誤記", "別人", "心当たり", "訂正", "取り消", "削除",
          "飲んだこと", "本当", "未確認", "確認中")
# What a clause after the assertion may do (「…のため、合成薬ACに変更しました」); anything else is no match.
TAIL_ACTIONS = ("変更", "中止", "除去", "注意", "使用")
# R5: a "none" form is exact only when no other sentence limits it or names a reaction or cue.
EXCEPTION = ("ただし", "但し", "のみ", "だけ", "以外", "除")
# R8: a sentence naming the allergen with these words speaks about it even without a cue.
TOLERATED = ("問題", "大丈夫")
# A word holding these is an exception (「卵以外」), neither an allergen nor another list member.
NOUN_EXCLUDE = ("外",)

_OCCURRENCE_LIMIT = 64
_A = "\ue000"  # the claimed allergen's placeholder inside its unit


def _alt(words):
    return "|".join(map(re.escape, sorted(words, key=len, reverse=True)))


# ---------------------------------------------------------------------------------------------
# Template grammar. `<<X>>` tokens expand in `_expand`; capturing tokens get unique group names.

_SELF = r"(?:ご本人|本人|ご?利用者(?:さん|様)|患者(?:さん|様))"
_PERSON = (rf"(?:{clinical_scope.FAMILY_PERSON}|{clinical_scope.OTHER_PERSON}|{clinical_scope.NAMED_PERSON}"
           r"|(?:その|あの|別の|他の|ほかの|隣の|同室の)(?:方|かた|人))")
_NUM = r"[0-9０-９一二三四五六七八九十数]+"
_SPAN = r"(?:年|か月|ヶ月|カ月|週間|日)"
_FIXED = {
    "SELF": _SELF,
    "FAMILY": rf"(?:{clinical_scope.FAMILY_PERSON})",
    "CAT": r"(?:薬や食べ物|薬や食物|薬剤|食べ物|食物|食品|薬)",
    "END": r"[ \t　]*[。．！!]?[ \t　]*$",
    # Reported speech after a plain form; そうです only there (「ありそうです」 is a conjecture, not a report).
    "REPORTTO": r"と(?:のことです|言っています|話されています|話しています|聞いています|聞きました|伺いました|伺っています)",
    "REPORT": r"(?:<<REPORTTO>>|そうです)",
    "SP": r"[ \t　]*",
}


def _noun():
    """Another list member: a run with no particle, punctuation, placeholder or exception word, not a person."""
    char = r"[^\s\ue000、。，,．！？!?「」『』（）()：:・/／|｜はがをにでのともへやか]"
    run = rf"(?:(?!{_alt(NOUN_EXCLUDE)}){char})+" if NOUN_EXCLUDE else f"{char}+"
    return f"(?!{_PERSON}){run}"


def _reaction():
    words = _alt(REACTION_WORDS + ("薬疹",))
    return rf"(?:[一-龥]{{1,3}}(?:の中)?が)?(?:{words})(?:さ|み|感|症状|症)?(?:の悪化)?"


def _expand(text):
    """Compile a template text: expand `<<X>>` tokens until none is left, naming capture groups uniquely."""
    counter = itertools.count()

    def group(name, body):
        return f"(?P<{name}_{next(counter)}>{body})"

    def token(match):
        name = match[1]
        if name in _FIXED:
            return _FIXED[name]
        if name == "N":
            return _noun()
        if name == "A":
            return _A
        if name == "ALIST":
            sep = r"(?:と|や|・)"
            return rf"(?:{_noun()}{sep}){{0,3}}{_A}(?:{sep}{_noun()}){{0,3}}"
        if name == "R":
            return group("R", _reaction())
        if name == "RG":
            return f"(?:{_reaction()})"
        if name == "TIME":
            past = rf"以前|過去に|{_NUM}{_SPAN}前に|子供の頃に?"
            now = rf"以前から|昨日から|今朝|初めて|{_NUM}{_SPAN}前から"
            return f"(?:(?:{group('tn', now)}|{group('tp', past)})[、，]?)?"
        if name == "EXISTF":
            return f"(?:あります|あり(?:です)?|ある(?:<<REPORT>>)?|{group('pp', 'ありました|あった(?:<<REPORT>>)?')})<<END>>"
        if name == "EXISTL":
            return r"(?:あり|あるため|あるので)[、，]?<<TAIL>>"
        if name == "COPF":
            return f"(?:です|だ(?:<<REPORT>>)?|{group('pp', 'でした|だった(?:<<REPORT>>)?')})<<END>>"
        if name == "COPL":
            return r"(?:のため|で)[、，]?<<TAIL>>"
        if name == "EMF":
            return (f"(?:が|も)?(?:出(?:ます|る体質です|ています|る(?:<<REPORT>>)?)|出現しています|続いています|落ちています"
                    f"|くなります|くなる(?:<<REPORT>>)?"
                    f"|{group('pv', '出ました|出た(?:<<REPORT>>)?|出現しました|悪化しました|くなりました|出ていました')}"
                    f"|{group('pe', '出たことがあります|出たことがある(?:<<REPORT>>)?')})<<END>>")
        if name == "EML":
            return r"(?:が|も)?(?:出て|出たため|出たので|出現したため)[、，]?<<TAIL>>"
        if name == "RAISE":
            return (f"を起こ(?:します|しています|{group('pv', 'しました')}"
                    f"|{group('pe', 'したことがあります|したことがある(?:<<REPORT>>)?')})<<END>>")
        if name == "NEGF":
            return (f"(?:ありません|ございません|ない(?:<<REPORT>>)?|なし(?:です|<<REPORTTO>>)?"
                    f"|{group('np', 'ありませんでした|なかったです|なかった(?:<<REPORT>>)?')})<<END>>")
        if name == "NEGL":
            return r"(?:なく|なし)[、，](?:<<TAIL>>|<<GCORE>>)"
        if name == "GCORE":
            return r"<<N>>(?:に|の)?(?:は|には)?アレルギー(?:が|は)?(?:あります|ありです|あり)<<END>>"
        if name == "TAIL":
            obj = r"(?:<<N>>(?:を|に|へ|は|も|が|で|から)<<SP>>)*"
            verb = (f"(?:{_alt(TAIL_ACTIONS)})(?:し(?:ています|ております|ている)"
                    f"|{group('tq', 'しました|していました|した|していた|にな(?:りました|った))')}")
            return (f"{obj}(?:{verb}|<<R>>(?:が|も)?(?:出(?:ます|ています|る)|続いています)|<<N>>を飲んで(?:います|いる))"
                    r"(?:そうです)?<<END>>")
        if name == "COORD":
            return (r"(?:<<N>>(?:に|の)?(?:は|には)?(?:アレルギー|副作用)(?:は|が)?"
                    r"(?:なく|なし|あり|ありませんでしたが|ありませんが|ありますが)[、，])?")
        if name == "LEAD":
            lead = r"[^、。\ue000]{1,20}(?:ので|ため)[、，]|[^、。\ue000はがをに]{1,12}(?:から|より)[、，]"
            return f"{group('ld', lead)}?"
        raise KeyError(name)

    previous = None
    while previous != text:
        previous, text = text, re.sub(r"<<([A-Z]+)>>", token, text)
    return re.compile(text)


class Template(NamedTuple):
    """One explicit assertion form: the regex over the unit and the axes it licenses."""
    name: str
    kind: str
    negated: bool
    subject: str  # "patient" or "family"; "subject" when the subject position in front of the core decides
    certainty: str
    pattern: str
    scope: str  # "sentence" (after a subject position), "lead" (own subject), "line" (heading / record), "null"
    record: bool = False


_PAT = r"<<SELF>>(?:は|には|に|が|も)[、，]?"
_POS = r"<<SELF>>の"
_FAM = r"(?:<<SELF>>の)?(?:同居の)?<<FAMILY>>(?:は|には|に|が)[、，]?"
_LABEL_A = (r"<<SP>>[■◆◇●○・※＊*\-－]?<<SP>>(?:<<SELF>>の)?[【［\[]?(?:薬剤|食物|食品)?アレルギー"
            r"(?P<hist_x>歴)?[】］\]]?<<SP>>[:：]<<SP>>")
_LABEL_R = r"<<SP>>[■◆◇●○・※＊*\-－]?<<SP>>(?:<<SELF>>の)?[【［\[]?副作用(?P<hist_x>歴)?[】］\]]?<<SP>>[:：]<<SP>>"
_ITEM_SEP = r"[、，,・/／]<<SP>>"
_GITEM = r"<<N>>(?:(?:で|による)<<RG>>)?(?:[（(]<<RG>>[）)])?"
_AITEM = r"<<A>>(?:(?:で|による)<<R>>)?(?:[（(](?:<<R>>|(?P<fp_x>昨年|昔))[）)])?"
_VALUE = rf"(?:{_GITEM}{_ITEM_SEP})*{_AITEM}(?:{_ITEM_SEP}{_GITEM})*(?:[（(]いずれも<<R>>[）)])?<<END>>"
_NEG_VALUE = r"(?:<<N>>(?:なし|無し|あり)" + _ITEM_SEP + r")*<<A>>(?:なし|無し)(?:" + _ITEM_SEP + r"<<N>>(?:なし|無し|あり))*<<END>>"
_RECORD = (r"(?:<<SP>>" + _NUM + _SPAN + r"前の)?(?:前医の)?(?:紹介状|診療情報提供書|情報提供書|退院時?サマリー|看護サマリー|サマリー|"
           r"お薬手帳|カルテ)(?:に|には|では)「")
_RECORD_END = r"」(?:と|との)(?:記載|記録)(?:が)?(?:あり(?:ます|ました)?|されています)<<END>>"
_ADR_LINK = (r"(?:で|による|により|の副作用で|の副作用として|を飲んで|を服用して|を内服して|の(?:内服|服用|使用|投与)後に?"
             r"|を飲んだ(?:とき|時|後)に?|を飲むと|を服用すると)")
_ALG_TO = r"(?:に|へ|の|に対して|に対する|への)?"

TEMPLATES = (
    # Affirmative allergy, prose.
    Template("alg-exist", "allergy", False, "subject", "confirmed",
             rf"<<TIME>><<ALIST>>{_ALG_TO}(?:は|も)?(?:アレルギー|過敏症)(?:反応)?(?:が|も)?(?:<<EXISTF>>|<<EXISTL>>)", "sentence"),
    Template("alg-copula", "allergy", False, "subject", "confirmed",
             r"<<TIME>><<ALIST>>(?:の)?アレルギー(?:<<COPF>>|<<COPL>>|<<REPORTTO>><<END>>)", "sentence"),
    Template("alg-raise", "allergy", False, "subject", "confirmed",
             r"<<TIME>><<ALIST>>(?:に|で|に対して)アレルギー(?:反応|症状)?<<RAISE>>", "sentence"),
    Template("alg-emerge", "allergy", False, "subject", "confirmed",
             r"<<TIME>><<ALIST>>(?:で|に)アレルギー(?:反応|症状)?(?:<<EMF>>|<<EML>>)", "sentence"),
    Template("alg-food", "allergy", False, "subject", "confirmed",
             r"<<ALIST>>を(?:食べる|飲む|使う)と<<R>>(?:くなる|が出る)アレルギー(?:が|も)<<EXISTF>>", "sentence"),
    Template("alg-told", "allergy", False, "subject", "confirmed",
             r"<<ALIST>>(?:に|の)アレルギーがあることを(?:<<N>>(?:に|へ))?(?:も)?伝えて(?:います|いました|おります)<<END>>",
             "sentence"),
    Template("alg-topic", "allergy", False, "patient", "confirmed",
             r"<<ALIST>>アレルギーの<<SELF>>は[、，]?<<TAIL>>", "lead"),
    # Affirmative adverse reaction, prose.
    Template("adr-relation", "adverse_reaction", False, "subject", "confirmed",
             rf"<<TIME>><<ALIST>>{_ADR_LINK}<<TIME>><<R>>(?:<<EMF>>|<<EML>>)", "sentence"),
    Template("adr-cue", "adverse_reaction", False, "subject", "confirmed",
             r"<<TIME>><<ALIST>>(?:で|の|による)<<TIME>>副作用(?:<<EMF>>|<<EML>>)", "sentence"),
    Template("adr-history", "adverse_reaction", False, "subject", "confirmed",
             r"<<ALIST>>の副作用(?P<hist_x>歴)?(?:が|も)<<EXISTF>>", "sentence"),
    Template("adr-drug-rash", "adverse_reaction", False, "subject", "confirmed",
             r"<<ALIST>>(?:による|で)(?P<R_x>薬疹)が<<EXISTF>>", "sentence"),
    # Negation, prose.
    Template("neg-alg", "allergy", True, "subject", "confirmed",
             rf"<<ALIST>>{_ALG_TO}(?:は|には)?(?:特に)?(?:アレルギー|過敏症)(?:は|が|も)?(?:特に)?(?:<<NEGF>>|<<NEGL>>)",
             "sentence"),
    Template("neg-alg-denied", "allergy", True, "subject", "confirmed",
             r"<<ALIST>>(?:への|に対する|の)アレルギーは否定され(?:ました|ています)<<END>>", "sentence"),
    Template("neg-adr", "adverse_reaction", True, "subject", "confirmed",
             r"<<ALIST>>(?:で|による|の)(?:副作用|<<R>>)(?:は|も)(?:特に)?"
             r"(?:出ていません|ありません|みられません|見られません|ない|なし|(?P<np_x>ありませんでした|出ませんでした|出なかった))<<END>>",
             "sentence"),
    Template("neg-adr-never", "adverse_reaction", True, "subject", "confirmed",
             r"<<ALIST>>で(?:副作用|<<R>>)が出たことは(?:一度も)?ありません<<END>>", "sentence"),
    Template("neg-adr-taking", "adverse_reaction", True, "subject", "confirmed",
             rf"<<ALIST>>を(?:{_NUM}{_SPAN})?(?:続けて|飲んで|服用して|内服して)いますが[、，](?:特に)?副作用は(?:特に)?"
             r"(?:出ていません|ありません|みられません)<<END>>", "sentence"),
    Template("neg-adr-tolerated", "adverse_reaction", True, "subject", "confirmed",
             r"<<ALIST>>の副作用(?:について|は)[、，]?(?:特に)?問題(?:ない(?:<<REPORT>>)?|ありません|なし(?:<<REPORTTO>>)?)<<END>>", "sentence"),
    # Suspected.
    Template("sus-maybe-alg", "allergy", False, "subject", "suspected",
             r"<<ALIST>>のアレルギーかもしれ(?:ません|ない)(?:が[、，](?:まだ)?(?:分かって|わかって)いません)?<<END>>", "sentence"),
    Template("sus-maybe-adr", "adverse_reaction", False, "subject", "suspected",
             r"<<ALIST>>の副作用かもしれ(?:ません|ない)(?:が[、，](?:まだ)?(?:分かって|わかって)いません)?<<END>>", "sentence"),
    Template("sus-doubt", "adverse_reaction", False, "subject", "suspected",
             r"<<ALIST>>を(?:始めて|飲み始めて|開始して)から<<R>>(?:が|も)?(?:落ちて|出て)おり[、，]副作用の疑いがあります<<END>>",
             "sentence"),
    # Heading fields (one line), owned by the patient unless the post names another owner.
    Template("field-alg", "allergy", False, "patient", "confirmed", _LABEL_A + _VALUE, "line"),
    Template("field-adr", "adverse_reaction", False, "patient", "confirmed", _LABEL_R + _VALUE, "line"),
    Template("field-neg-alg", "allergy", True, "patient", "confirmed", _LABEL_A + _NEG_VALUE, "line"),
    Template("field-family", "allergy", False, "family", "confirmed",
             _LABEL_A + r"(?:同居の)?<<FAMILY>>は<<A>>で<<R>>(?:くなる|くなります|が出る|が出ます)<<END>>", "line"),
    # A quoted earlier record: the patient's past assertion.
    Template("record-alg", "allergy", False, "patient", "confirmed",
             _RECORD + r"<<A>>(?:に|の|で)?アレルギー(?:あり)?" + _RECORD_END, "line", record=True),
    Template("record-adr", "adverse_reaction", False, "patient", "confirmed",
             _RECORD + r"<<A>>(?:で|による)<<R>>" + _RECORD_END, "line", record=True),
    # "None" forms for a null allergen: exact, with no list and no exception.
    Template("null-alg", "allergy", True, "patient", "confirmed",
             r"<<LEAD>><<SELF>>(?:は|には|に|の)[、，]?(?:特に)?(?:<<CAT>>(?:の)?)?アレルギー(?:歴)?(?:は|が|も)?(?:特に)?<<NEGF>>",
             "null"),
    Template("null-alg-label", "allergy", True, "patient", "confirmed",
             r"<<SP>><<SELF>>の(?:<<CAT>>)?アレルギー(?:[（(]なし[）)]|[ \t　]+なし)<<END>>", "null"),
    Template("null-adr", "adverse_reaction", True, "patient", "confirmed",
             r"<<LEAD>><<SELF>>(?:は|には|に|の)[、，]?(?:特に)?副作用(?:歴)?(?:は|が|も)?(?:特に)?<<NEGF>>", "null"),
    Template("null-field-alg", "allergy", True, "patient", "confirmed",
             _LABEL_A + r"(?:なし|無し|特になし|特記(?:事項)?なし|該当なし|ありません)<<END>>", "null"),
    Template("null-field-adr", "adverse_reaction", True, "patient", "confirmed",
             _LABEL_R + r"(?:なし|無し|特になし|特記(?:事項)?なし|該当なし|ありません)<<END>>", "null"),
)

# The subject positions of a "sentence" template, in front of its core.
SUBJECTS_OF = (("patient", _PAT), ("patient", _POS), ("family", _FAM))
# 「ご本人の発疹は…かもしれません」: the reaction as topic, for the suspected templates only.
SUSPECTED_SUBJECTS = (("patient", r"<<SELF>>の<<RG>>は"),)


@functools.lru_cache(maxsize=None)
def _compiled(templates, subjects, suspected_subjects, noun_exclude, tail_actions, reaction_words):
    """(template, subject, regex) for every template and subject position (cached per vocabulary)."""
    del noun_exclude, tail_actions, reaction_words  # cache keys: the expansion reads the module constants
    out = []
    for template in templates:
        if template.scope == "sentence":
            heads = list(subjects) + (list(suspected_subjects) if template.certainty == "suspected" else [])
            for subject, head in heads:
                out.append((template, subject, _expand(rf"^<<SP>><<LEAD>>{head}<<COORD>>{template.pattern}")))
        else:
            out.append((template, template.subject, _expand(rf"^<<SP>>{template.pattern}")))
    return tuple(out)


def _templates():
    return _compiled(TEMPLATES, SUBJECTS_OF, SUSPECTED_SUBJECTS, NOUN_EXCLUDE, TAIL_ACTIONS, REACTION_WORDS)


class Licence(NamedTuple):
    """The axes one template match licenses for one allergen occurrence."""
    template: str
    kind: str
    subject: str
    statuses: frozenset
    negated: bool
    certainty: str
    reactions: tuple  # (start, end) spans in the body that may hold the claimed reaction


class Resolution(NamedTuple):
    """What the body licenses for one (evidence, allergen, reaction), or why the claim is dropped."""
    drop: str | None = None
    evidence: str = ""
    kinds: frozenset = frozenset()
    licences: tuple = ()
    conflict: bool = False


def _unit(body, position):
    left = max((body.rfind(mark, 0, position) for mark in "。！？!?\n"), default=-1) + 1
    stop = _UNIT_BREAK.search(body, position)
    return left, stop.start() if stop else len(body)


def _unit_text(body, left, right):
    """The unit with its own end mark (a question mark stays, so no template ends on it)."""
    return body[left:right + 1] if right < len(body) and body[right] != "\n" else body[left:right]


def _cues(text, start=0, end=None):
    """(start, end, kind) of every cue word."""
    end = len(text) if end is None else end
    found = [(m.start(), m.end(), "allergy") for m in ALLERGY_CUE.finditer(text, start, end)]
    found += [(m.start(), m.end(), "adverse_reaction") for m in ADVERSE_CUE.finditer(text, start, end)]
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


def _groups(match, prefix):
    return [name for name, value in match.groupdict().items() if value is not None and name.startswith(prefix + "_")]


def _statuses(template, match):
    """R4: the time a template match licenses; an empty set leaves the time open."""
    if template.record:
        return frozenset({"past"})
    if _groups(match, "fp"):
        return frozenset({"past"})
    if _groups(match, "hist"):
        return frozenset({"current", "past"})
    if template.negated:
        return frozenset({"current", "past"}) if _groups(match, "np") else frozenset({"current"})
    past_form = bool(_groups(match, "pp") or _groups(match, "pe"))
    if _groups(match, "ld"):  # A clause in front may carry its own time: only a present form is read.
        return frozenset() if past_form or any(_groups(match, tag) for tag in ("pv", "tq", "tp", "tn")) \
            else frozenset({"current"})
    if _groups(match, "tp"):
        return frozenset({"past"}) if past_form or _groups(match, "pv") or _groups(match, "tq") else frozenset()
    if _groups(match, "tn"):
        return frozenset() if _groups(match, "pe") else frozenset({"current"})
    return frozenset({"past"}) if past_form else frozenset({"current"})


def _licences(text, offset, scope_null):
    """Every licence the templates give a unit text (the allergen already replaced by the placeholder)."""
    found = []
    for template, subject, regex in _templates():
        if (template.scope == "null") != scope_null:
            continue
        match = regex.match(text)
        if match is None:
            continue
        reactions = tuple((offset + match.start(name), offset + match.end(name)) for name in _groups(match, "R"))
        found.append(Licence(template.name, template.kind, subject, _statuses(template, match),
                             template.negated, template.certainty, reactions))
    return tuple(found)


def _licences_at(body, start, end):
    """Licences for the allergen written at body[start:end], read in its own unit."""
    left, right = _unit(body, start)
    text = _unit_text(body, left, right)
    placed = text[:start - left] + _A + text[end - left:]
    found = _licences(placed, left, scope_null=False)
    # Spans after the placeholder are shifted by the allergen's length minus one.
    shift = (end - start) - 1
    return tuple(lic._replace(reactions=tuple((s + shift if s > start else s, e + shift if e > start else e)
                                              for s, e in lic.reactions)) for lic in found)


def _token(allergen):
    """An allergen fills a slot only as one word: letters only, hiragana only when all of it, no cue, reaction or self word."""
    letters = re.fullmatch(r"[一-龥々ァ-ヶーA-Za-z0-9Ａ-Ｚａ-ｚ０-９]+|[ぁ-ゖー]+", allergen) is not None
    return (letters and not _cues(allergen) and not (NOUN_EXCLUDE and re.search(_alt(NOUN_EXCLUDE), allergen))
            and not any(word in allergen for word in REACTION_WORDS + ("薬疹",))
            and not re.search(_SELF, allergen))


def _sentences(body):
    return [(m.start(), m.end()) for m in re.finditer(r"[^。！？!?\n]+", body)]


def _name_field(body):
    labels = _alt(NAME_LABELS)
    honorifics = _alt(NAME_HONORIFICS)
    return bool(re.search(rf"(?m)^[ \t　]*[【［\[]?[^\n:：]{{0,6}}(?:{labels})[^\n:：]{{0,4}}(?:[ \t　]*[:：]|[ \t　]+\S)", body)
                or re.search(rf"(?m)^[ \t　]*[【［\[]?[^\s:：、。]{{1,10}}(?:[ \t　]+[^\s:：、。]{{1,10}})?[ \t　]*"
                             rf"(?:{honorifics})[】］\]]?[ \t　]*$", body))


def _disclaimer(body):
    nouns, marks = _alt(DISCLAIMER_NOUNS), _alt(DISCLAIMER_MARKS)
    for s, e in _sentences(body):
        others = re.sub(_SELF, "", body[s:e])
        if re.search(nouns, others) and (re.search(marks, others) or re.search(_PERSON, others)):
            return True
    return False


def _denied_after(body, right):
    return bool(DENIAL) and re.search(_alt(DENIAL), body[right:]) is not None


def _speaks(body, left, right):
    """R8: a sentence that has a cue, a reaction word or a tolerance word speaks about what it names."""
    text = body[left:right]
    return bool(_cues(body, left, right) or any(word in text for word in REACTION_WORDS)
                or TOLERATED and re.search(_alt(TOLERATED), text))


def _occurrences_conflict(body, allergen, start, licences):
    """R8: the allergen written in another sentence that speaks of it must be licensed the same way."""
    own = _unit(body, start)
    readings = {(lic.subject, lic.negated) for lic in licences}
    for index, match in enumerate(re.finditer(re.escape(allergen), body)):
        if index >= _OCCURRENCE_LIMIT:
            return True  # Too many places to read: leave the assertion open.
        left, right = _unit(body, match.start())
        if (left, right) == own or not _speaks(body, left, right):
            continue
        if {(lic.subject, lic.negated) for lic in _licences_at(body, match.start(), match.end())} != readings:
            return True
    return False


def _null_conflict(body, own):
    """R5: a "none" form is open when another sentence names a cue, a reaction or an exception (unless it is "none" too)."""
    exception = _alt(EXCEPTION) if EXCEPTION else None
    for left, right in _sentences(body):
        if _unit(body, left) == own:
            continue
        text = body[left:right]
        named = (_cues(body, left, right) or any(word in text for word in REACTION_WORDS)
                 or exception and re.search(exception, text))
        if named and not _licences(_unit_text(body, *_unit(body, left)), left, scope_null=True):
            return True
    return False


def _weaken(body, licences):
    """R3c: another owner named by the post turns patient licences into unknown ones."""
    if licences and (_name_field(body) or _disclaimer(body)):
        licences = tuple(lic._replace(subject="unknown") if lic.subject == "patient" else lic for lic in licences)
    return licences


@functools.lru_cache(maxsize=4096)
def resolve(body, evidence, allergen, reaction) -> Resolution:
    """Read the licences the body gives one quoted claim (memoized; independent of the claimed axes)."""
    if (not isinstance(body, str) or not isinstance(evidence, str) or not evidence.strip()
            or any(value is not None and not (isinstance(value, str) and value.strip())
                   for value in (allergen, reaction))):
        return Resolution(drop="形式が不正です")
    span = locate_quote_span(body, evidence)
    if span is None:  # R1/R9: the reference context is never searched.
        return Resolution(drop="引用が対象の本文に一意に見つかりません")
    quote = body[span[0]:span[1]]
    if allergen is not None and (allergen not in quote or _VALUE_WORD.fullmatch(allergen)
                                 or _PUNCTUATED.search(allergen)):
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
    if allergen is None:
        licences = _licences(_unit_text(body, left, right), left, scope_null=True)
        if not licences:
            return Resolution(drop="アレルゲンの無い主張は「なし」の形のときだけ残せます")
        conflict = _null_conflict(body, (left, right))
    else:
        licences = _licences_at(body, start, end) if _token(allergen) else ()
        conflict = bool(licences) and _occurrences_conflict(body, allergen, start, licences)
    # R2: a kind needs a cue or relation in the sentence, or a template that licenses it.
    kinds |= {lic.kind for lic in licences}
    if not kinds:
        return Resolution(drop="アレルギー・副作用の手がかり語がありません")
    conflict = conflict or bool(licences) and _denied_after(body, right)
    return Resolution(evidence=quote, kinds=kinds, licences=_weaken(body, licences), conflict=conflict)


def _shape(item):
    return (isinstance(item, dict) and item.get("kind") in KINDS and item.get("subject") in SUBJECTS
            and item.get("status") in STATUSES and type(item.get("negated")) is bool
            and item.get("certainty") in CERTAINTIES and isinstance(item.get("evidence"), str)
            and all(item.get(key) is None or isinstance(item[key], str) for key in ("allergen", "reaction")))


def _judge(item, res, body):
    """Compare the claimed axes with a resolution: (kept item or None, drop reason)."""
    if res.drop is not None:
        return None, res.drop
    if item["kind"] not in res.kinds:
        return None, "種類の手がかり語が同じ文にありません"
    kept = {"allergen": item["allergen"], "kind": item["kind"], "reaction": item["reaction"],
            "subject": item["subject"], "status": item["status"], "negated": item["negated"],
            "certainty": item["certainty"], "evidence": res.evidence}
    if not _confirmed(item, res, body):
        kept["unverified"] = True
    return kept, None


def _confirmed(item, res, body):
    """One licence carries every claimed axis (a suspected claim fits a certain licence) and none reads otherwise."""
    if res.conflict or not res.licences or len({(lic.subject, lic.negated) for lic in res.licences}) > 1:
        return False
    return any(lic.kind == item["kind"] and lic.subject == item["subject"] and item["subject"] != "unknown"
               and item["status"] in lic.statuses and lic.negated == item["negated"]
               and (item["certainty"] == "suspected" or lic.certainty == "confirmed")
               and (item["reaction"] is None or any(item["reaction"] in body[s:e] for s, e in lic.reactions))
               for lic in res.licences)


def _key(item):
    return (item.get("allergen"), item.get("kind"), item.get("evidence"), item.get("subject"),
            item.get("status"), item.get("negated"), item.get("certainty"))


def same_assertion(a, b) -> bool:
    """R10: one allergen, kind, quote and axis set is one assertion, whatever chunk or reaction wording repeated it."""
    return isinstance(a, dict) and isinstance(b, dict) and _key(a) == _key(b)


def _better(new, old):
    """Of two copies of one assertion keep a confirmed one, then the one naming a reaction."""
    def rank(item):
        return item.get("unverified") is not True, item.get("reaction") is not None
    return rank(new) > rank(old)


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
            result, reason = _judge(item, res, body)
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


def allergen_candidates(body, labels=(), meds=()) -> list:
    """Allergen strings for exhaustive claim gates: label allergens and their padded copies, null, body phrases and med names.

    A padded copy is a label allergen as written in the body with 1-8 characters
    before it or 1-4 after it (the scorer matches allergens by containment). Body
    phrases are the 20 longest. A test aid for the validator gate; runtime code never calls it.
    """
    found = [None]
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("allergen"), str):
            allergen = label["allergen"]
            found.append(allergen)
            for match in re.finditer(re.escape(allergen), body):
                found += [body[max(match.start() - k, 0):match.end()] for k in range(1, 9)]
                found += [body[match.start():match.end() + k] for k in range(1, 5)]
    phrases = sorted({part for part in _PHRASE_BREAK.split(body) if part.strip()},
                     key=lambda part: (-len(part), part))
    found += phrases[:20]
    found += [med["name"] for med in meds if isinstance(med, dict) and isinstance(med.get("name"), str)]
    return list(dict.fromkeys(found))
