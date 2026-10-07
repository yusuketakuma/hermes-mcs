"""LINE WORKS presentation choices for MCS cards and clicker-only answers.

Owns what only LINE WORKS needs: the 1000-character button-template
card and its ``↓ 続き`` split, and the plain-text dialect. Shared safety
(tokens, scope, journal, validation) stays in notify_cards/adapters.
"""
from __future__ import annotations

CARD_LIMIT = 1000          # button_template contentText ceiling
MORE = "\n↓ 続き"


def card_split(text: str) -> tuple[str, str]:
    """(card text, remainder) for the 1000-character card: cut at the
    last line break that leaves room for the ``↓ 続き`` marker,
    hard-cutting only a single overlong line."""
    if len(text) <= CARD_LIMIT:
        return text, ""
    room = CARD_LIMIT - len(MORE)
    cut = text.rfind("\n", 0, room + 1)
    if cut <= 0:
        return text[:room] + MORE, text[room:]
    return text[:cut] + MORE, text[cut + 1:]


def parts_text(parts: dict) -> str:
    """Plain text with a 【】 heading — LINE WORKS renders no markup."""
    import notify_render
    return notify_render.render_text(parts, head="【{}】")


# Card behaviour (read by notify_cards). LINE WORKS posts cannot be
# edited or deleted, and its "thread" is a logical grouping in the room.
EDITABLE = False               # no stamp line; an update is a new post
ALWAYS_THREAD = True
MORE_BUTTON = True             # その他の操作 opens the 1:1 menu
DRUG_ROW_NEEDS_THREAD = False
SOURCE_THREAD = False          # no native thread to bind signals to
THREAD_DRUG_ACTIONS = False
CAPTION = "preview"            # attachment caption: post preview text


def face(containers, footer, op, cfg):
    """No silent mentions on LINE WORKS: configured names go in now; an
    update is a new post, so it is marked 🔄 更新版."""
    import notify_cards
    import notify_render
    names = (notify_cards.notify_cfg(cfg).get("lineworks") or {}).get("user_names")
    footer = [dict(f, text=notify_render.lineworks_member_names(f["text"], names))
              if f.get("type") == "text" else f for f in footer]
    if op == "update":
        containers = notify_cards._mark_reposted(containers)
    return containers, footer


def overflow(parts, split_chunks) -> list:
    """[(name, chunk)] — the card text breaks at a line ending with
    ↓ 続き; the remainder rides as durable body parts, never truncated
    (adapters/lineworks/cards.validate). Buttons beyond the card's
    primary ones live behind その他の操作 in the 1:1 talk."""
    import notify_render
    rest = card_split(notify_render.display_text(parts))[1]
    return [(f"display#{i}", chunk)
            for i, chunk in enumerate(split_chunks(rest), 1)]

THREAD_HEAD_PATIENT = True     # thread posts land in the room itself


def card_link(card) -> str | None:
    return None


def owner_label(owner: str, member_names) -> str:
    """No silent mentions: configured member names replace <@id>."""
    import notify_render
    return notify_render.lineworks_member_names(owner, member_names)
