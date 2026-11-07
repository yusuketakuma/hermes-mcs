"""Summary/search can start in a proven thread and answer privately."""
import sys

from adapters.discord import cards
from discord_testkit import _fake_discord


def test_summary_controls_are_available_on_the_thread_and_private_answer(monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    spec = {"parts": {"action_rows": [[{"id": action, "label": action, "token": action}
                                     for action in ("summary", "search")]]}}
    assert len(cards.thread_drug_view(spec).items) == 2
    view = cards.thread_drug_view(spec, private=True)
    assert view is not None
    assert len(view.items) == 2


def test_followup_renders_summary_controls_on_first_private_chunk(monkeypatch):
    from adapters.discord.actions import _answer_view
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    result = {"navigation": [{"id": "summary", "label": "合成の背景", "token": "summary"}]}
    assert _answer_view(None, result, 0) is not None
    assert _answer_view(None, result, 1) is None
