"""Patient context buttons delivered to the clicker's 1:1 talk reach the runner (synthetic)."""
import asyncio

import pytest

from lineworks_testkit import SCOPE, command_files, event, world


@pytest.mark.parametrize("action", ["summary", "search"])
def test_private_context_button_in_direct_talk_is_handled(tmp_path, action):
    w = world(tmp_path, action=action)
    token, actor = "e" * 32, "lineworks:40029600:operator"
    context = {"action": action, "project_id": 1, "actor": actor, "ephemeral": True}
    result = {"outcome": "applied", "action": "list", "projects": [1], "list": {"title": "完全合成の患者まとめ"},
              "navigation": [{"id": action, "ui": "button", "label": "次の8件",
                              "token": token, "style": "secondary"}],
              "token_ctx": {token: context}}
    rec = {"kind": "action", "origin": {**SCOPE, "message_id": "lw:" + "d" * 32}, "actor": actor}
    asyncio.run(w.actions._deliver_followup("operator", rec, result))
    asyncio.run(w.actions.handle(event(postback="mcs:a:" + token, user="other", channel=None)))
    assert command_files(w) == []
    asyncio.run(w.actions.handle(event(postback="mcs:a:" + token, channel=None)))
    assert command_files(w)[0]["token"] == token
