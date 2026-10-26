"""Discord markdown for a private patient-context answer, sized for one Components V2 card.

Built from the transport-neutral rows of ``adapters.common.context_view``
(the same rows Slack's ``context_list_blocks`` renders). SDK-free: only
``actions``/``cards`` may import discord.py (ci/gates.py), so actions turns
these texts into the LayoutView.
"""
from __future__ import annotations

from adapters.common.context_view import context_entries
from adapters.common.spec import MAX_BUTTONS, MAX_COMPONENTS, MAX_TOTAL_TEXT
from adapters.common.text import escape_md, view_scope_ok
from .cards import _CONTAINER_MAX

# the private navigation buttons the companion-thread drug view also accepts
NAVIGATION = frozenset({"meds", "drugsearch", "summary", "search"})


def navigation(result: dict) -> list:
    """The runner-minted navigation buttons that ride the patient-context card."""
    return [b for b in result.get("navigation") or []
            if isinstance(b, dict) and b.get("id") in NAVIGATION]


def _subtext(text) -> list:
    return [f"-# {escape_md(ln)}" for ln in str(text).splitlines() if ln.strip()]


def _lines(entry) -> list:
    """One entry as Discord markdown lines; every user text stays literal."""
    kind = entry["kind"]
    if kind == "title":
        return [f"## {escape_md(' '.join(entry['text'].split()))}"]
    if kind in ("head", "notes"):
        return _subtext(entry["text"])
    if kind == "group":
        return [f"**{escape_md(' '.join(entry['text'].split()))}**"]
    if kind in ("quotes", "empty"):
        return [escape_md(entry["text"])]
    rest = escape_md(" ".join(entry["rest"].split()))
    if entry["url"]:
        # the date opens the MCS room; <…> keeps the URL out of embeds
        # (context_view.safe_url already refused < > | and whitespace)
        return [f"-# [{escape_md(entry['date'])}](<{entry['url']}>) {rest}".rstrip()]
    return [f"-# {rest}"] if rest else []


def _zones(entries) -> list:
    """Entry text split at each divider (a Separator is drawn between zones)."""
    zones, cur = [], []
    for entry in entries:
        if entry["kind"] == "divider":
            if cur:
                zones.append("\n".join(cur))
            cur = []
        else:
            cur.extend(_lines(entry))
    if cur:
        zones.append("\n".join(cur))
    return zones


def context_texts(result: dict, allowed) -> list | None:
    """TextDisplay contents (Separators between them) of an applied
    patient-context answer, or None to keep the text answer: any other
    view, a result outside this deployment's scope, or a page over one
    message's Components V2 budget (container children, 40 components,
    4000 chars)."""
    if not isinstance(result, dict) or result.get("action") != "list" \
            or not view_scope_ok(result, allowed):
        return None
    entries = context_entries(result.get("list"), allowed)
    if entries is None:
        return None
    buttons = len(navigation(result))
    rows = -(-buttons // MAX_BUTTONS)

    def fits(texts):
        # texts + Separators between them, then a Separator and the button rows
        children = 2 * len(texts) - 1 + (1 + rows if rows else 0)
        return (sum(map(len, texts)) <= MAX_TOTAL_TEXT and children <= _CONTAINER_MAX
                and 1 + children + buttons <= MAX_COMPONENTS)

    zones = _zones(entries)
    if fits(zones):
        return zones
    # many groups: one text without Separators, as cards.build_view does
    merged = ["\n\n".join(zones)]
    return merged if fits(merged) else None
