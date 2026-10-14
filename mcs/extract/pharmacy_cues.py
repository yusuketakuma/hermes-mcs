"""Detect clause-local pharmacy cues and their lexical coverage in mapped extraction fields."""
import re
import unicodedata
from dataclasses import dataclass


type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


_CLAUSE = re.compile(r"[^。、，,;；！？!?\n]+")
_NEGATION = re.compile(
    r"(?!\s)(?:の)?(?:状況|症状|確認|対応|予定)?(?:は|が|も|を)?\s*"
    r"(?:で(?:は)?|じゃ|し(?:て)?|され(?:て)?|認め(?:て)?)?"
    r"(?:い)?(?:ない|なし|なく|ありません|ません|なかった|不要)")
_FAMILY = re.compile(
    r"(?:ご家族|家族|母親|父親|祖母|祖父|娘|息子|夫|妻|兄|姉|弟|妹|孫)"
    r"(?:さん|様)?(?:の|は|が)")
_PATIENT = re.compile(r"ご本人|本人|患者|利用者")
_SUPPORT = re.compile(r"介助|支援|手伝")
_PREFIX_NEGATION = re.compile(r"(?:不要|必要(?:は|が)?ありません)(?:なのは|なのが|な|の)?\s*")
_PAST = re.compile(r"過去|既往|以前(?!から|より)|先月(?!から|より)|先週(?!から|より)|昨年|昨日(?!から|より)")
_CURRENT = re.compile(r"現在|今も|今は|本日|今日|続いて|持続|(?:昨日|先週|先月)から")
_TEXT_KEYS = ("text", "evidence", "value", "name", "action", "due", "condition")


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def _field_texts(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _field_texts(item)]
    if isinstance(value, dict):
        return [value[key] for key in _TEXT_KEYS if isinstance(value.get(key), str)]
    return []


@dataclass(frozen=True, slots=True)
class CueClass:
    """Bind a lexical cue to extraction paths and canonical semantic categories."""

    code: str
    field_paths: tuple[str, ...]
    positive: re.Pattern[str]
    projection_categories: tuple[str, ...]
    negation_guard: re.Pattern[str] = _NEGATION
    family_guard: re.Pattern[str] = _FAMILY
    past_guard: re.Pattern[str] = _PAST

    def matches(self, body: str) -> list[tuple[int, int]]:
        """Return original-body cue spans with guards confined to each clause."""
        if not isinstance(body, str) or not body:
            return []
        spans = []
        for clause in _CLAUSE.finditer(body):
            text = clause[0]
            if self.past_guard.search(text) and not _CURRENT.search(text):
                continue
            family = list(self.family_guard.finditer(text))
            patient = list(_PATIENT.finditer(text))
            if family:
                owned = any(
                    owner[0].endswith("の")
                    and text.startswith(("もの", "分", "薬", "残薬", "服薬"), owner.end())
                    for owner in family)
                family_subject = not patient or family[-1].start() > patient[-1].start()
                support = not family[-1][0].endswith("の") and _SUPPORT.search(text)
                if owned or (family_subject and not support):
                    continue
            # Scan each clause once; cue checks use offsets without copying its tails.
            negated_starts = {mark.start() for mark in self.negation_guard.finditer(text)}
            negated_ends = {mark.end() for mark in _PREFIX_NEGATION.finditer(text)}
            for cue in self.positive.finditer(text):
                tail_start = cue.end()
                while tail_start < len(text) and text[tail_start].isspace():
                    tail_start += 1
                if tail_start in negated_starts or cue.start() in negated_ends:
                    continue
                spans.append((clause.start() + cue.start(), clause.start() + cue.end()))
        return spans

    def captured(self, extraction: dict[str, JsonValue], body: str, span: tuple[int, int]) -> bool:
        """Check only mapped field text or evidence for the source cue or its NFKC form.

        This is a lexical coverage proxy for finding omission candidates. It does
        not prove the action, subject, negation or tense is right: a stop in the
        source captured by a start output still counts as captured.
        """
        if not isinstance(extraction, dict) or not isinstance(body, str):
            return False
        if (not isinstance(span, tuple) or len(span) != 2
                or not all(isinstance(i, int) for i in span)
                or not 0 <= span[0] < span[1] <= len(body)):
            return False
        surface = body[span[0]:span[1]]
        needle = _normalized(surface)
        if not needle or self.positive.fullmatch(surface) is None:
            return False
        for path in self.field_paths:
            root, _, selector = path.partition(":")
            parts = root.split(".")
            values = extraction.get(parts[0], [])
            if parts[0] == "patient_context" and len(parts) > 1:
                values = [
                    item for item in values if isinstance(item, dict)
                    and item.get("category") == parts[1]
                ] if isinstance(values, list) else []
                if len(parts) > 2:
                    values = [
                        detail for item in values
                        if isinstance(details := item.get("details"), list)
                        for detail in details
                        if isinstance(detail, dict) and detail.get("key") == parts[2]
                    ]
            if selector:
                values = [
                    item for item in values
                    if item == selector or (isinstance(item, dict)
                                            and item.get("type") == selector)
                ] if isinstance(values, list) else []
            if any(needle in _normalized(text) for text in _field_texts(values)):
                return True
        return False


# Paths filter context categories/detail keys, rather than searching the entire JSON.
_MEDICATION = "patient_context.medication_management"
_FOLLOWUP = "patient_context.followup"
_OBSERVATIONS = "patient_context.observations"
_DEFINITIONS = (
    ("one_dose_packaging", r"一包化|分包",
     (_MEDICATION + ".support_method", _MEDICATION, "requests"),
     ("adherence_administration", "request_pending")),
    ("prescription_inquiry", r"疑義|照会|処方提案",
     ("events:prescription_inquiry", _MEDICATION, "requests"),
     ("medication", "request_pending")),
    ("storage", r"保管|置き場|カレンダー|配薬ボックス|お薬箱",
     (_MEDICATION, "requests"), ("adherence_administration", "request_pending")),
    ("leftover", r"残薬|余って|余り",
     (_MEDICATION + ".residual_quantity", _MEDICATION),
     ("adherence_administration",)),
    ("swallowing", r"嚥下|むせ|飲み込|粉砕|簡易懸濁",
     (_MEDICATION + ".swallowing", _MEDICATION + ".support_method",
      _MEDICATION, "patient_context.nutrition", "symptoms", "requests"),
     ("adherence_administration", "symptom_state", "request_pending")),
    ("next_visit", r"次回|次の訪問|来週",
     (_FOLLOWUP + ".followup", _FOLLOWUP, "requests", "events:visit"),
     ("care_event", "request_pending")),
    ("adherence", r"飲み忘れ|拒薬|服薬",
     (_MEDICATION + ".missed_doses", _MEDICATION, "requests"),
     ("adherence_administration", "request_pending")),
    ("rx_change", r"処方変更|増量|減量|中止|追加",
     ("meds", _MEDICATION, "requests"), ("medication", "request_pending")),
    ("interaction_check", r"重複|相互作用|併用",
     (_MEDICATION, "meds", "requests"), ("medication", "request_pending")),
    ("sleep", r"睡眠|不眠|眠れない|眠れて",
     ("symptoms", "patient_context.adl"), ("symptom_state",)),
    ("excretion", r"排泄|排便|排尿|便秘|下痢",
     ("symptoms", "patient_context.adl"), ("symptom_state",)),
    # ASCII abbreviations only as standalone tokens (not create/CRP/salt/last/paste)
    ("renal_hepatic", r"腎機能|肝機能|クレアチニン|(?<![A-Za-z0-9])(?:eGFR|CrCl|Cre|Cr|BUN|AST|ALT)(?![A-Za-z])",
     ("labs", _OBSERVATIONS), ("vital_lab",)),
    ("pain_opioid", r"疼痛|痛み|麻薬|オピオイド",
     ("symptoms", "meds", _MEDICATION), ("symptom_state", "medication")),
    ("weight", r"体重|weight",
     ("labs", "vitals", _OBSERVATIONS), ("vital_lab",)),
    ("fall", r"転倒",
     ("symptoms", "events:fall", "patient_context.adl"), ("care_event", "symptom_state")),
    ("adverse", r"副作用|有害事象",
     ("symptoms", "patient_context.adverse_events"), ("adverse_drug_event",)),
)
CUE_CLASSES: dict[str, CueClass] = {
    code: CueClass(code, paths, re.compile(pattern, re.I), categories)
    for code, pattern, paths, categories in _DEFINITIONS
}
