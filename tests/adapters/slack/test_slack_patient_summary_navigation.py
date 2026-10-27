"""Private summary navigation uses the original actor and card scope."""
import asyncio
import json
from pathlib import Path
import time

import pytest

from slack_actions_testkit import TOKEN, SCOPE, ack, click, command, fixture, result


def test_summary_private_navigation(tmp_path):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="summary")
        pin = {**reg.token(TOKEN), "card_key": "synthetic-card", "kind": "thread"}
        reg.put_tokens({TOKEN: pin})
        await actions._action(ack, *click())
        env = command(dirs)
        nav_token = "e" * 32
        ctx = {key: pin[key] for key in ("card_key", "kind", "project_id", "channel_id", "team_id", "message_id")}
        actor = "slack:T_SYNTHETIC:U_OPERATOR"
        ctx.update(action="summary", actor=actor, ephemeral=True)
        result(dirs, env["request_id"], request_id=env["request_id"], outcome="applied",
               action="list", list={"title": "合成", "items": [{"text": "合成の背景"}]},
               token_ctx={nav_token: ctx}, navigation=[{"id": "summary", "ui": "button",
                   "label": "変化", "token": nav_token, "style": "secondary"}])
        await actions.sweep_followups()
        assert app.client.messages[0]["blocks"][-1]["elements"][0]["value"] == nav_token
        assert "thread_ts" not in app.client.messages[0]
        origin = {**SCOPE, "message_id": "1790000000.000999", "actor": actor}
        assert actions._pinned(nav_token, origin, actor)
        assert actions._pinned(nav_token, {**origin, "actor": "slack:T_SYNTHETIC:U_OTHER"}, "slack:T_SYNTHETIC:U_OTHER") is None
        assert actions._pinned(nav_token, {**origin, "channel_id": "FOREIGN"}, actor) is None
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["same", "changed", "expired", "revoked", "route_changed"])
def test_measurement_result_is_rechecked_before_ephemeral_delivery(tmp_path, mode):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="summary")
        actor = "slack:T_SYNTHETIC:U_OPERATOR"
        reg.put_tokens({TOKEN: {**reg.token(TOKEN), "ephemeral": True, "actor": actor,
                                "measurement_view": True}})
        actions._settings["route_epoch"] = 3
        Path(dirs["flags"], "notify.json").write_text(json.dumps(
            {"interactive": True, "transport": "slack", "route_epoch": 3}))
        await actions._action(ack, *click())
        env = command(dirs)
        view = {"title": "合成", "layout": "patient_context", "head": [],
                "items": [{"project_id": 123, "text": "合成値 1", "quotes": ["合成値 1"]}]}
        result(dirs, env["request_id"], request_id=env["request_id"], outcome="applied",
               action="list", measurement_view=True, list=view, projects=[123])
        await actions.sweep_followups()
        assert not app.client.messages
        check = command(dirs)
        assert check["request_id"] != env["request_id"]
        if mode == "changed":
            view["items"][0]["text"] = "合成値 2"
        if mode == "revoked":
            actions._settings["allowed_user_ids"] = set()
        if mode == "route_changed":
            Path(dirs["flags"], "notify.json").write_text(json.dumps(
                {"interactive": True, "transport": "slack", "route_epoch": 4}))
        result(dirs, check["request_id"], request_id=check["request_id"], outcome="applied",
               action="list", measurement_view=True, list=view, projects=[123],
               processed_at=time.time() - (10 if mode == "expired" else 0))
        await actions.sweep_followups()
        if mode in ("expired", "revoked", "route_changed"):
            assert not app.client.messages
            return
        assert len(app.client.messages) == 1
        rendered = json.dumps(app.client.messages, ensure_ascii=False)
        if mode == "changed":
            assert "変更されました" in rendered and "合成値" not in rendered
        else:
            assert "合成値 1" in rendered
    asyncio.run(scenario())
