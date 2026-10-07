"""Discord presentation choices for MCS cards and clicker-only answers.

The runner-side half of Discord's layout; Components V2 is built by
adapters/discord/cards.py. Shared safety stays in notify_cards/adapters.
"""
from __future__ import annotations

import re

# Twin of adapters.discord.cards.escape_md (mcs/ never imports adapters;
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
