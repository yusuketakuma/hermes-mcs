"""Plan source-preserving clinical units before inference using output-pressure heuristics."""
import re
import unicodedata

from mcs_util import text_chunks

# Bump whenever boundaries or density weights change: checkpoints pin this contract.
PLAN_VERSION = 1
# These are output-pressure units, not measured tokenizer counts or a speed promise.
_OUTPUT_BUDGET = 850
_DOSE = re.compile(r"\d+(?:\.\d+)?\s*(?:mg|μg|mcg|g|ml|錠|包|滴|kg|cm|%|割|回)", re.I)
_OBSERVATION = re.compile(r"体重|身長|血糖|体温|脈拍|血圧|SpO2|HbA1c|eGFR|\b(?:Cr|AST|ALT|BUN|CRP|Na|K|Hb|Alb)\b", re.I)
_CHANGE = re.compile(r"中止|終了|開始|変更|増量|減量|頓服|指示|希望|予定|痛|睡眠|減少|残薬|飲み忘れ|嚥下|支援|確認")
_FIELD = re.compile(r"(?:^|\n)[ \t　]*(?:[■●◆・*-][ \t　]*)?[^\n:：]{1,24}[:：]")
_HEADING = re.compile(r"^(?:[【\[［].+[】\]］]|[■●◆#].+|[^:：]{1,32}[:：]|本人|患者本人|家族|母|父|妻|夫|過去|現在|予定)\s*$")
_CONTINUATION = re.compile(r"^(?:用法|用量|服用|対応|転帰|結果|変更後|支援後|同薬|同剤|朝|昼|夕|夜|食前|食後|就寝|\d+日\d+回)")


def is_heading(text: str) -> bool:
    """Explicit or bare source heading, shared with reference-only consumers."""
    return isinstance(text, str) and _HEADING.fullmatch(text.strip()) is not None


def _pressure(source: str) -> int:
    text = unicodedata.normalize("NFKC", source)
    return (len(text) // 6 + 130 * len(_DOSE.findall(text))
            + 100 * len(_OBSERVATION.findall(text)) + 90 * len(_CHANGE.findall(text))
            + 100 * len(_FIELD.findall(text)))


def plan_chunks(source: str, size: int = 3000) -> list[str]:
    """Complete <=size parts; preserve semantic neighbours, then bound output pressure.

    Sparse short text remains one call. Unknown prose is never discarded.
    Oversized units use the existing line/sentence-aware hard-bound splitter.
    """
    if not isinstance(source, str) or type(size) is not int or size < 1:
        raise ValueError("clinical_chunk_arguments_invalid")
    if not source:
        return []
    units = []
    for line in source.splitlines(keepends=True):
        parts = re.split(r"(?<=[。！？!?；;])", line) if _pressure(line) > _OUTPUT_BUDGET else [line]
        for part in parts:
            if not part:
                continue
            if units and (is_heading(units[-1]) or _CONTINUATION.match(part.lstrip())):
                units[-1] += part
            else:
                units.append(part)
    bounded = []
    for unit in units:
        # Density never justifies cutting a drug from its dose/frequency.
        # Only clear new item starts split a dense single sentence; otherwise
        # keep the semantic unit and let the guarded length fallback handle it.
        fragments, start = [], 0
        if _pressure(unit) > _OUTPUT_BUDGET:
            for comma in re.finditer(r"[、,]", unit):
                tail = unit[comma.end():].lstrip()
                head = re.split(r"[、,。\n]", tail, maxsplit=1)[0]
                folded = unicodedata.normalize("NFKC", head)
                dose = _DOSE.search(folded)
                prefix = folded[:dose.start()].strip() if dose else ""
                if (dose and prefix and not re.match(r"毎|翌|隔|就寝|食後|食前|週|\d", prefix)
                        and not _CONTINUATION.match(tail)):
                    fragments.append(unit[start:comma.end()])
                    start = comma.end()
        fragments.append(unit[start:])
        for fragment in fragments:
            bounded.extend(text_chunks(fragment, size))
    result, current = [], ""
    for part in bounded:
        proposed = current + part
        if current and (len(proposed) > size or _pressure(proposed) > _OUTPUT_BUDGET):
            result.append(current)
            current = part
        else:
            current = proposed
    if current:
        result.append(current)
    assert "".join(result) == source and all(len(part) <= size for part in result)
    return result
