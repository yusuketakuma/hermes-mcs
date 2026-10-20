"""Private summary navigation uses the original actor and card scope."""
import asyncio

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
