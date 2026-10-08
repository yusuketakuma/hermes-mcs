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
    return owner

POST_ACTIONS = True           # 💊 button beside each medication post
ACCENT_URGENT = False
