"""Keep every model allergy or adverse-reaction claim whose allergen occurs in the post body, always as an unverified candidate.

An assertion is one allergen with explicit axes:

  {"allergen": str | null, "kind": "allergy" | "adverse_reaction",
   "reaction": str | null, "subject": ..., "status": ..., "negated": bool,
   "certainty": "confirmed" | "suspected", "evidence": str}

Four rounds of held-out attacks showed that sentence templates cannot make a
"confirmed" assertion both safe and useful, so this validator confirms
nothing: every kept claim carries `unverified: true`, and the model's axes
are stored as written (never corrected). What it still decides:

- Locate the claim in the target body (never the reference context). The
  body is folded once per call (NFKC, casefold, whitespace removed); the same
  folding decides whether the allergen occurs and where. The evidence is the
  model's quote when it is uniquely in the body and holds the allergen,
  otherwise the sentence or line around the allergen's first occurrence,
  widened over a line break inside the allergen. A claim with no allergen
  ("none") keeps its located quote, or else the sentence of the first cue word.
- Drop only a malformed item, a claim whose allergen does not occur in the
  body at all, and a claim with no allergen that has neither a located quote
  nor a cue word. A claimed reaction is kept as written.
- Store one assertion once (R10): copies with the same allergen, kind, quote
  and axes merge when their reactions are equal or one names none; distinct
  reactions stay separate candidates.
"""
from __future__ import annotations

import re
from typing import NamedTuple

from clinical_events import STATUSES, SUBJECTS
from mcs_util import fold_map

FIELD = "allergies"
KINDS = ("allergy", "adverse_reaction")
CERTAINTIES = ("confirmed", "suspected")

# The cue words of each kind. 抗アレルギー薬, アレルギー検査/外来/科 and アレルギー性鼻炎 name no allergy.
ALLERGY_CUE = re.compile(r"(?<!抗)(?:アレルギー|過敏症|不耐症?)(?!検査|テスト|薬|外来|科|性)")
ADVERSE_CUE = re.compile(r"副作用|副反応|有害事象|薬疹")
# A unit ends at a sentence mark or at any line break (LF, CR, NEL, LINE / PARAGRAPH SEPARATOR).
_BREAKS = "。！？!?\n\r\x85\u2028\u2029"
_UNIT_END = re.compile(f"[{re.escape(_BREAKS)}]")
_END_MARKS = re.compile(r"[。！？!?]*")
# Why a claim is dropped (the code is stable; the reason goes to the repair detail).
REASONS = {
    "shape": "形式が不正です",
    "absent": "アレルゲンが本文にありません",
    "unlocated": "引用が対象の本文に一意に見つかりません",
}
_CONTEXT_REASON = "参考コンテキストの文は引用にできません。対象の投稿の本文から引用してください"


class Resolution(NamedTuple):
    """Where one claim sits in the body (`evidence`), or the code of why it is dropped (`drop`)."""
    drop: str | None = None
    evidence: str = ""


class _Body:
    """One body and its folding, computed once and shared by every claim of a call."""

    def __init__(self, text):
        self.text = text
        self.flat, self.starts, self.ends = fold_map(text, casefold=True)

    def first(self, term):
        """(start, end) in the body of the first folded occurrence of `term`, or None."""
        exact = self.text.find(term)
        if exact >= 0:
            return exact, exact + len(term)
        needle = fold_map(term, casefold=True)[0]
        at = self.flat.find(needle) if needle else -1
        return (self.starts[at], self.ends[at + len(needle) - 1]) if at >= 0 else None

    def unique(self, term):
        """(start, end) of the only occurrence of `term` (exact, else folded), widened to whole characters, or None."""
        exact = self.text.find(term)
        if exact >= 0:
            return (exact, exact + len(term)) if self.text.find(term, exact + 1) < 0 else None
        needle = fold_map(term, casefold=True)[0]
        at = self.flat.find(needle) if needle else -1
        if at < 0 or self.flat.find(needle, at + 1) >= 0:
            return None
        return self.starts[at], self.ends[at + len(needle) - 1]  # Whole source characters around the match.

    def unit(self, start, end):
        """The sentences or lines covering body[start:end], with the run of end marks after them."""
        text = self.text
        left = max(text.rfind(mark, 0, start) for mark in _BREAKS) + 1
        stop = _UNIT_END.search(text, end)
        right = stop.start() if stop else len(text)
        right += len(_END_MARKS.match(text, right)[0])
        return text[left:right]


def _contains(text, term):
    return term in text or fold_map(term, casefold=True)[0] in fold_map(text, casefold=True)[0]


def _cues(text):
    """(start, end, kind) of every cue word."""
    found = [(m.start(), m.end(), "allergy") for m in ALLERGY_CUE.finditer(text)]
    found += [(m.start(), m.end(), "adverse_reaction") for m in ADVERSE_CUE.finditer(text)]
    return sorted(found)


def cue_spans(body) -> list[tuple[int, int, str]]:
    """Positions of the allergy / adverse-reaction cue words in a body, for prefilters and context views."""
    return _cues(body) if isinstance(body, str) else []


def _locate(view, evidence, allergen):
    if allergen is not None and not (isinstance(allergen, str) and allergen.strip()):
        return Resolution(drop="shape")
    span = view.unique(evidence) if isinstance(evidence, str) and evidence.strip() else None
    quote = view.text[span[0]:span[1]] if span else None
    if allergen is not None:
        found = view.first(allergen)
        if found is None:
            return Resolution(drop="absent")
        if quote is not None and _contains(quote, allergen):
            return Resolution(evidence=quote)
        return Resolution(evidence=view.unit(*found))
    if quote is not None:
        return Resolution(evidence=quote)
    cues = _cues(view.text)
    return Resolution(evidence=view.unit(cues[0][0], cues[0][1])) if cues else Resolution(drop="unlocated")


def resolve(body, evidence, allergen) -> Resolution:
    """Locate one claim in the body: its stored evidence, or the code of why it is dropped."""
    if not isinstance(body, str) or not body.strip():
        return Resolution(drop="shape")
    return _locate(_Body(body), evidence, allergen)


def _shape(item):
    return (isinstance(item, dict) and item.get("kind") in KINDS and item.get("subject") in SUBJECTS
            and item.get("status") in STATUSES and type(item.get("negated")) is bool
            and item.get("certainty") in CERTAINTIES
            and all(item.get(key) is None or isinstance(item[key], str) for key in ("allergen", "reaction", "evidence")))


def _key(item):
    return (item.get("allergen"), item.get("kind"), item.get("evidence"), item.get("subject"),
            item.get("status"), item.get("negated"), item.get("certainty"))


def same_assertion(a, b) -> bool:
    """R10: one allergen, kind, quote and axis set whose reactions are equal or one names none is one assertion."""
    return (isinstance(a, dict) and isinstance(b, dict) and _key(a) == _key(b)
            and (a.get("reaction") == b.get("reaction") or a.get("reaction") is None or b.get("reaction") is None))


def _merge(copies, result):
    """Add one candidate to the copies of its key: a reaction fills an empty copy; a distinct reaction stays apart."""
    for index, copy in enumerate(copies):
        if same_assertion(copy, result):
            if copy["reaction"] is None and result["reaction"] is not None:
                copies[index] = result
            return
    copies.append(result)


def validate_items(items, body, drops=None, *, ctx=False) -> tuple[list, int]:
    """Keep the model allergy claims located in the target body as unverified candidates; returns (kept, dropped count).

    `drops`, when given, collects bounded repair detail in the extract_validation
    shape ({"ev": [...], "items": ["allergies"], "reasons": [...]}). `ctx` says the
    prompt carried reference context, which is never a valid source.
    """
    view = _Body(body) if isinstance(body, str) and body.strip() else None
    located, kept, dropped = {}, {}, 0
    for item in items if isinstance(items, list) else []:
        if view is None or not _shape(item):
            res = Resolution(drop="shape")
        else:
            where = (item["evidence"], item["allergen"])
            res = located[where] = located[where] if where in located else _locate(view, *where)
        if res.drop is not None:
            dropped += 1
            if drops is not None:
                _note_drop(drops, item, res.drop, ctx)
            continue
        result = {"allergen": item["allergen"], "kind": item["kind"], "reaction": item["reaction"],
                  "subject": item["subject"], "status": item["status"], "negated": item["negated"],
                  "certainty": item["certainty"], "evidence": res.evidence, "unverified": True}
        _merge(kept.setdefault(_key(result), []), result)
    return [result for copies in kept.values() for result in copies], dropped


def _note_drop(drops, item, code, ctx):
    reason = _CONTEXT_REASON if code == "unlocated" and ctx else REASONS[code]
    if code == "unlocated" and isinstance(item, dict):
        quotes = drops.setdefault("ev", [])
        if len(quotes) < 5:
            quotes.append(str(item.get("evidence"))[:60])
    fields = drops.setdefault("items", [])
    if FIELD not in fields and len(fields) < 5:
        fields.append(FIELD)
    reasons = drops.setdefault("reasons", [])
    if len(reasons) < 5:
        reasons.append(f"{FIELD}: {reason}")


def stored_assertions(meta, content, *, first_generation):
    """Confirmed assertions of one stored extraction (copies), None when its generation never looked for them.

    None is "not acquired" (an older generation, an unreadable version or field);
    [] is "looked and found none confirmed". Only well-formed assertions without
    an `unverified` mark count; this validator stores only candidates. The
    caller checks that the row is a per-post extraction: other kinds (for
    example semantic_facts_v4, whose meta says extract_version 4) read as not acquired.
    """
    version = meta.get("extract_version") if isinstance(meta, dict) else None
    if type(version) is not int or version < first_generation or not isinstance(content, dict):
        return None
    items = content.get(FIELD, [])
    if not isinstance(items, list):
        return None
    return [dict(item) for item in items
            if _shape(item) and isinstance(item.get("evidence"), str) and item.get("unverified", False) is False]
