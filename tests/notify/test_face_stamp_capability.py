"""The footer's stamp aggregate gives way to per-post stamp rows by presenter
capability (STAMP_DETAILS), not by transport name (synthetic fixtures only)."""
import pytest

import notify_render
import present_discord
import present_lineworks
import present_slack
from notify_lineworks_testkit import LINEWORKS
from notify_testkit import CFG, _card, _dispatch, _intent, _seed_thread, led, pinned_clock
from slack_testkit import SLACK

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")
AGGREGATE = "スタンプ 合成集計"


def test_presenter_capabilities():
    assert present_slack.STAMP_DETAILS and present_slack.BODY_ON_FACE
    assert not hasattr(present_slack, "FACE_STAMPS")      # its face posts carry originals
    assert present_discord.STAMP_DETAILS and present_discord.FACE_STAMPS
    assert not present_discord.BODY_ON_FACE              # no folded original on Discord
    assert not getattr(present_lineworks, "STAMP_DETAILS", False)


@pytest.mark.parametrize("cfg, kept", [(SLACK, False), (CFG, False), (LINEWORKS, True)])
def test_thread_card_footer_drops_the_aggregate_only_with_the_capability(led, monkeypatch, cfg, kept):
    monkeypatch.setattr(notify_render, "card_reaction_lines", lambda _r: [AGGREGATE])
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), cfg)
    card = _card(led)

    def footer(c, row=card):
        return "\n".join(i["text"] for i in notify_render.card_content(led.db, row, cfg=c)["footer"])

    def thread(on):
        return {**cfg, "notify": {**cfg["notify"], "card_thread": on}}
    assert kept or card["layout"] >= 3   # Slack/Discord: a layout that can drop it
    assert (AGGREGATE in footer(thread(True))) is kept
    # without a companion thread the aggregate is the only stamp summary
    assert AGGREGATE in footer(thread(False))
    assert AGGREGATE in footer(thread(True), dict(card) | {"thread_state": "failed"})
