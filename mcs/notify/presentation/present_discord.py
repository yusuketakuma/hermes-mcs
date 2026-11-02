"""Discord presentation choices for MCS cards and clicker-only answers.

The runner-side half of Discord's layout; Components V2 is built by
adapters/discord/cards.py. Shared safety stays in notify_cards/adapters.
"""
from __future__ import annotations

import re

# Twin of adapters.common.text.escape_md (mcs/ never imports adapters;
# tests pin the two equal): same-length look-alikes for Discord markup.
_MD_KEEP = re.compile(r"(<@[!&]?\w+>|https?://\S+)")
_MD_INLINE = str.maketrans("*_~|`[]<\\", "＊＿～｜｀［］＜＼")
_MD_LINE = re.compile(r"^([ \t]*)(?:(\d+)\.|([#>+-]))", re.M)
_MD_LEAD = str.maketrans("#>+-", "＃＞＋－")


def literal(text: str) -> str:
    text = "".join(part if i % 2 else part.translate(_MD_INLINE)
                   for i, part in enumerate(_MD_KEEP.split(text)))
    return _MD_LINE.sub(
        lambda m: m.group(1) + (f"{m.group(2)}．" if m.group(2)
                                else m.group(3).translate(_MD_LEAD)), text)


def parts_text(parts: dict) -> str:
    """Markdown heading and ``-#`` subtext footer; staff text escaped to
    literal text of the same length so it cannot render as markup."""
    import notify_render
    return notify_render.render_text(parts, head="## {}", lit=literal,
                                     footer="-# {}")


# Card behaviour (read by notify_cards; Discord messages are editable
# and the card owns a native thread).
EDITABLE = True
ALWAYS_THREAD = False
MORE_BUTTON = False
DRUG_ROW_NEEDS_THREAD = False  # ephemeral answers work without a thread
SOURCE_THREAD = True
THREAD_DRUG_ACTIONS = True     # 💊 buttons on the thread body post
CAPTION = "header"


def face(containers, footer, op, cfg):
    return containers, footer

THREAD_HEAD_PATIENT = False    # replies sit under the patient card


def card_link(card) -> str | None:
    """A jump link to the delivered card message."""
    if not card["message_id"] or not card["channel_id"]:
        return None
    return ("https://discord.com/channels/"
            f"{card['guild_id'] or '@me'}/{card['channel_id']}/"
            f"{card['message_id']}")


def owner_label(owner: str, member_names) -> str:
    return owner

POST_ACTIONS = True           # 💊 button beside each medication post
ACCENT_URGENT = True          # red container edge on an urgent card

# Thread cards show each shown post's stamp row under its summary on the
# card face; the original stays in the thread (Components V2 has no
# collapsible block). adapters/discord/cards.py drops the rows when the
# face would exceed its component/text budget; the thread keeps them.
BODY_ON_FACE = False
FACE_STAMPS = True
# Rows that do not fit the face remain on their post in the companion thread.
STAMP_DETAILS = True
FACE_TEXT_MAX = 4000          # adapters/common/spec.py MAX_TOTAL_TEXT


def face_text_cost(parts, face_posts=()) -> int:
    """The text adapters/common/spec.validate charges for this face:
    the same per-item costs, plus each face stamp line (twin of its
    _containers_cost/_footer_cost text half; tests pin the two equal)."""
    n = sum(len(ln) + 4 for c in parts.get("footer") or [] if c["type"] == "text"
            for ln in c["text"].splitlines())
    for c in parts["containers"]:
        if c["type"] == "quote":
            n += sum(len(line) + 3 for line in c["text"].splitlines())
        elif c["type"] in ("heading", "text"):
            n += len(c["text"]) + 4
        elif c["type"] == "field":
            n += len(c["name"]) + len(c["value"]) + 6
    if parts.get("section_divider_version") == 1:
        n += 14 * sum(bool(c.get("rule")) and c.get("type") != "meta" for c in parts["containers"])
    return n + sum(len(ln) + 4 for p in face_posts for ln in p["stamps"])
