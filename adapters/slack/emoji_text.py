"""Compare Slack's documented emoji spellings without changing other text."""
from functools import lru_cache
import json
from pathlib import Path
import re


@lru_cache(maxsize=1)
def _data():
    data = json.loads(Path(__file__).with_name("emoji_data.json").read_text(encoding="utf-8"))
    names = data["sequences"]
    blocked = set(data["unmapped_sequences"])
    sequences = sorted(set(names) | blocked, key=len, reverse=True)
    return names, blocked, re.compile("|".join(re.escape(s) for s in sequences))


def _continuation(char):
    return char in "\u200d\ufe0e\ufe0f\u20e3" or "\U0001f3fb" <= char <= "\U0001f3ff"


def slack_emoji_pattern(text):
    """Build a full-text matcher for known Unicode-to-Slack emoji spellings."""
    if not any(ord(char) > 127 for char in text):
        return None
    names, blocked, sequence_pattern = _data()
    pieces = []
    end = 0
    for match in sequence_pattern.finditer(text):
        sequence = match.group()
        left = text[match.start() - 1] if match.start() else ""
        right = text[match.end()] if match.end() < len(text) else ""
        if sequence in blocked or (left and _continuation(left)) or (right and _continuation(right)):
            continue
        # A single regional indicator inside an unknown flag is not a flag mapping.
        if len(sequence) == 1 and "\U0001f1e6" <= sequence <= "\U0001f1ff" and (
            left and "\U0001f1e6" <= left <= "\U0001f1ff"
            or right and "\U0001f1e6" <= right <= "\U0001f1ff"
        ):
            continue
        pieces.append(re.escape(text[end:match.start()]))
        pieces.append("(?:" + "|".join(map(re.escape, names[sequence])) + ")")
        end = match.end()
    if not pieces:
        return None
    pieces.append(re.escape(text[end:]))
    return re.compile("".join(pieces))
