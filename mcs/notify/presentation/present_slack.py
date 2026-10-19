"""Slack presentation choices for MCS cards and clicker-only answers.

The runner-side half of Slack's layout; Block Kit itself is built by
adapters/slack/cards.py. Shared safety stays in notify_cards/adapters.
"""
from __future__ import annotations


def parts_text(parts: dict) -> str:
    """mrkdwn text with a bold heading; body text is never formatted."""
    import notify_render
    return notify_render.render_text(parts, head="*{}*")


# Card behaviour (read by notify_cards; Slack posts are editable and
# replies form a native thread under the card).
EDITABLE = True                # chat.update: stamps/stale chunks stay live
ALWAYS_THREAD = False          # thread only with card_thread on
MORE_BUTTON = False            # secondary actions sit in the card select
DRUG_ROW_NEEDS_THREAD = True   # 💊 answers land in the card's own thread
SOURCE_THREAD = True           # signal cards reply in the source thread
THREAD_DRUG_ACTIONS = False
CAPTION = "header"             # attachment caption: post header line


def face(containers, footer, op, cfg):
    return containers, footer

THREAD_HEAD_PATIENT = False    # replies sit under the patient card


def card_link(card) -> str | None:
    return None


def owner_label(owner: str, member_names) -> str:
    """Private answers are escaped plain text, where <@U…> would show literally:
    the bare Slack user id replaces it, so different owners stay distinct
    (``member_names`` is LINE WORKS-only and never holds Slack ids)."""
    import notify_render
    return notify_render.MENTION_TOKEN_PATTERN.sub(r"\1", owner)

POST_ACTIONS = True           # 💊 button beside each medication post
ACCENT_URGENT = False

# Thread cards carry each shown post's stamp row and folded original on
# the card itself (owner 2026-10-18); the thread keeps attachments, 💊
# answers, new-reply notices and any post the card cannot hold.
BODY_ON_FACE = True
FACE_ORIGINAL_MAX = 30000     # one collapsible container: 10 sections of 3000
BLOCK_MAX = 50                # conservative app budget, including container children
_SECTION_MAX = 3000
_CONTEXT_MAX = 2000
_HEADER_MAX = 150


def _count(text, size) -> int:
    return max(1, -(-len(text) // size))


def face_block_count(parts, face_posts=()) -> int:
    """An upper bound on the Block Kit blocks adapters/slack/cards.render
    builds for this card face plus ``face_posts`` (each: its stamp rows
    as contexts and one folded original with its child sections). Count
    all levels conservatively; Slack's nested-block accounting is unspecified."""
    n = 0
    for item in parts["containers"]:
        kind = item["type"]
        if kind == "meta":
            continue
        if item.get("rule") and n:
            n += 1                                   # divider
        if item.get("rule") and kind == "text":
            n += _count(item["text"], _CONTEXT_MAX)
        elif kind == "heading" and len(item["text"]) <= _HEADER_MAX:
            n += 1
        else:
            text = (f"{item['name']}: {item['value']}" if kind == "field"
                    else item["text"])
            n += _count(text, _SECTION_MAX) + (kind == "quote")
    footer = [i for i in parts.get("footer") or [] if i["type"] == "text"]
    n += bool(footer) + sum(_count(i["text"], _CONTEXT_MAX) for i in footer)
    buttons = [b for row in parts.get("action_rows") or [] for b in row]
    n += 2 * any(b.get("ui") != "link" for b in buttons)
    n += sum(b.get("ui") == "link" for b in buttons)
    for post in face_posts:
        n += sum(_count(s, _CONTEXT_MAX) for s in post["stamps"]) + 1 + _count(post["original"], _SECTION_MAX)
    return n
