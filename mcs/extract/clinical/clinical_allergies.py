"""Keep every model allergy or adverse-reaction claim whose allergen occurs in the post body, always as an unverified candidate.

An assertion is one allergen with explicit axes:

  {"allergen": str | null, "kind": "allergy" | "adverse_reaction",
   "reaction": str | null, "subject": ..., "status": ..., "negated": bool,
   "certainty": "confirmed" | "suspected", "evidence": str}

Four rounds of held-out attacks showed that sentence templates cannot make a
"confirmed" assertion both safe and useful, so this validator confirms
nothing: every kept claim carries `unverified: true`, and the model's axes
are stored as written (never corrected). What it still decides:

- Locate the claim in the target body (never the reference context): the
  quote when it is uniquely in the body and holds the allergen, otherwise the
  sentence or line where the allergen first occurs. A claim with no allergen
  ("none") keeps its located quote, or else the sentence of the first cue word.
- Drop only a malformed item, a claim whose allergen does not occur in the
  body at all, and a claim with no allergen that has neither a located quote
  nor a cue word. A claimed reaction that the body does not hold is kept as
  written (the claim is a candidate either way).
- Store one assertion once (R10).
"""
from __future__ import annotations

import functools
import re
from typing import NamedTuple

from clinical_events import STATUSES, SUBJECTS
from mcs_util import fold_find, locate_quote_span, search_fold

FIELD = "allergies"
KINDS = ("allergy", "adverse_reaction")
CERTAINTIES = ("confirmed", "suspected")

# The cue words of each kind. 抗アレルギー薬, アレルギー検査/外来/科 and アレルギー性鼻炎 name no allergy.
ALLERGY_CUE = re.compile(r"(?<!抗)(?:アレルギー|過敏症|不耐症?)(?!検査|テスト|薬|外来|科|性)")
ADVERSE_CUE = re.compile(r"副作用|副反応|有害事象|薬疹")
# A unit ends at a sentence mark or at any line break (LF, CR, NEL, LINE / PARAGRAPH SEPARATOR).
_BREAKS = "。！？!?\n\r\x85  "
_UNIT_END = re.compile(f"[{_BREAKS}]")
_END_MARKS = re.compile(r"[。！？!?]*")


class Resolution(NamedTuple):
    """Where one claim sits in the body, or why it is dropped."""
    drop: str | None = None
    evidence: str = ""


def _cues(text):
    """(start, end, kind) of every cue word."""
    found = [(m.start(), m.end(), "allergy") for m in ALLERGY_CUE.finditer(text)]
    found += [(m.start(), m.end(), "adverse_reaction") for m in ADVERSE_CUE.finditer(text)]
    return sorted(found)


def cue_spans(body) -> list[tuple[int, int, str]]:
    """Positions of the allergy / adverse-reaction cue words in a body, for prefilters and context views."""
    return _cues(body) if isinstance(body, str) else []


def _unit_at(body, position):
    """The sentence or line holding body[position], with its own run of end marks (linear in the body)."""
    left = max(body.rfind(mark, 0, position) for mark in _BREAKS) + 1
    stop = _UNIT_END.search(body, position)
    right = stop.start() if stop else len(body)
    right += len(_END_MARKS.match(body, right)[0])
    return body[left:right]


def _occurs(needle, text):
    return needle in text or search_fold(needle) in search_fold(text)


def _allergen_unit(body, allergen):
    """The unit of the allergen's first occurrence; the whole body when only a folded, ambiguous match exists."""
    start = body.find(allergen)
    if start < 0:
        span = fold_find(body, allergen)
        start = span[0] if span else -1
    return _unit_at(body, start) if start >= 0 else body


@functools.lru_cache(maxsize=4096)
def resolve(body, evidence, allergen, reaction) -> Resolution:
    """Locate one claim in the body (memoized); `reaction` never decides anything and is kept as written."""
    del reaction
    if not isinstance(body, str) or not body.strip() or allergen is not None and not (
            isinstance(allergen, str) and allergen.strip()):
        return Resolution(drop="形式が不正です")
    if not (isinstance(evidence, str) and evidence.strip()):
        evidence = None  # No usable quote: the claim is located by its allergen or cue.
    if allergen is not None and not _occurs(allergen, body):
        return Resolution(drop="アレルゲンが本文にありません")
    span = locate_quote_span(body, evidence) if evidence is not None else None
    quote = body[span[0]:span[1]] if span else None
    if allergen is not None:
        return Resolution(evidence=quote if quote is not None and _occurs(allergen, quote)
                          else _allergen_unit(body, allergen))
    if quote is not None:
        return Resolution(evidence=quote)
    cues = _cues(body)
    if not cues:
        return Resolution(drop="引用が対象の本文に一意に見つかりません")
    return Resolution(evidence=_unit_at(body, cues[0][0]))


def _shape(item):
    return (isinstance(item, dict) and item.get("kind") in KINDS and item.get("subject") in SUBJECTS
            and item.get("status") in STATUSES and type(item.get("negated")) is bool
            and item.get("certainty") in CERTAINTIES
            and all(item.get(key) is None or isinstance(item[key], str) for key in ("allergen", "reaction", "evidence")))


def _key(item):
    return (item.get("allergen"), item.get("kind"), item.get("evidence"), item.get("subject"),
            item.get("status"), item.get("negated"), item.get("certainty"))


def same_assertion(a, b) -> bool:
    """R10: one allergen, kind, quote and axis set is one assertion, whatever chunk or reaction wording repeated it."""
    return isinstance(a, dict) and isinstance(b, dict) and _key(a) == _key(b)


def validate_items(items, body, drops=None, *, ctx=False) -> tuple[list, int]:
    """Keep the model allergy claims located in the target body as unverified candidates; returns (kept, dropped count).

    `drops`, when given, collects bounded repair detail in the extract_validation
    shape ({"ev": [...], "items": ["allergies"], "reasons": [...]}). `ctx` says the
    prompt carried reference context, which is never a valid source.
    """
    kept, dropped = {}, 0
    for item in items if isinstance(items, list) else []:
        res = (resolve(body, item.get("evidence"), item.get("allergen"), item.get("reaction")) if _shape(item)
               else Resolution(drop="形式が不正です"))
        if res.drop is not None:
            dropped += 1
            if drops is not None:
                _note_drop(drops, item, res.drop, ctx)
            continue
        result = {"allergen": item["allergen"], "kind": item["kind"], "reaction": item["reaction"],
                  "subject": item["subject"], "status": item["status"], "negated": item["negated"],
                  "certainty": item["certainty"], "evidence": res.evidence, "unverified": True}
        key = _key(result)
        # Of two copies of one assertion keep the one naming a reaction.
        if key not in kept or (result["reaction"] is not None and kept[key]["reaction"] is None):
            kept[key] = result
    return list(kept.values()), dropped


def _note_drop(drops, item, reason, ctx):
    if reason.startswith("引用") and ctx:
        reason = "参考コンテキストの文は引用にできません。対象の投稿の本文から引用してください"
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


def stored_assertions(meta, content, *, first_generation):
    """Confirmed assertions of one stored extraction, None when its generation never looked for them.

    None is "not acquired" (an older generation, an unreadable version or field);
    [] is "looked and found none confirmed". Unverified candidates are never
    returned, and this validator stores only candidates. The caller checks that
    the row is a per-post extraction: other kinds (for example semantic_facts_v4,
    whose meta says extract_version 4) read as not acquired.
    """
    version = meta.get("extract_version") if isinstance(meta, dict) else None
    if type(version) is not int or version < first_generation or not isinstance(content, dict):
        return None
    items = content.get(FIELD, [])
    if not isinstance(items, list):
        return None
    return [dict(item) for item in items if isinstance(item, dict) and item.get("unverified") is not True]
