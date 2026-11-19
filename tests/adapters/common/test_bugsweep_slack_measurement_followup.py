"""A measurement-view click is always answered once, and values only when fresh."""
import asyncio
import json
from pathlib import Path
import time

from slack_actions_testkit import TOKEN, ack, click, command, fixture, result

from adapters.slack import actions as slack_actions

ACTOR = "slack:T_SYNTHETIC:U_OPERATOR"
VIEW = {"title": "合成", "layout": "patient_context", "head": [],
        "items": [{"project_id": 123, "text": "合成値 1", "quotes": ["合成値 1"]}]}


def _measurement(tmp_path):
    actions, app, reg, dirs = fixture(tmp_path, kind="summary")
    reg.put_tokens({TOKEN: {**reg.token(TOKEN), "ephemeral": True, "actor": ACTOR,
                            "measurement_view": True}})
    actions._settings["route_epoch"] = 3
    Path(dirs["flags"], "notify.json").write_text(json.dumps(
        {"interactive": True, "transport": "slack", "route_epoch": 3}))
    return actions, app, reg, dirs


def test_rejected_measurement_click_is_answered(tmp_path):
    async def scenario():
        actions, app, reg, dirs = _measurement(tmp_path)
        await actions._action(ack, *click())
        env = command(dirs)
        rejected = {"request_id": env["request_id"], "outcome": "rejected",
                    "reason": "token_expired"}
        result(dirs, env["request_id"], **rejected)
        await actions.sweep_followups()
        await actions.sweep_followups()
        assert [m["text"] for m in app.client.messages] == [slack_actions.ja(
            {**rejected, "projects": []})]
    asyncio.run(scenario())


def test_stale_guard_result_says_fixed_text_once_without_values(tmp_path):
    async def scenario():
        actions, app, reg, dirs = _measurement(tmp_path)
        await actions._action(ack, *click())
        env = command(dirs)
        result(dirs, env["request_id"], request_id=env["request_id"], outcome="applied",
               action="list", measurement_view=True, list=VIEW, projects=[123])
        await actions.sweep_followups()
        check = command(dirs)
        assert check["request_id"] != env["request_id"] and not app.client.messages
        # the re-read predates the guard's publish by more than 5 s
        result(dirs, check["request_id"], request_id=check["request_id"], outcome="applied",
               action="list", measurement_view=True, list=VIEW, projects=[123],
               processed_at=time.time() - 10)
        await actions.sweep_followups()
        await actions.sweep_followups()
        assert [m["text"] for m in app.client.messages] == [slack_actions.MEASUREMENT_UNCONFIRMED]
        assert "合成値" not in json.dumps(app.client.messages, ensure_ascii=False)
    asyncio.run(scenario())


def test_guard_answered_after_slow_sweep_and_on_deadline(tmp_path, monkeypatch):
    async def scenario():
        actions, app, reg, dirs = _measurement(tmp_path)
        await actions._action(ack, *click())
        env = command(dirs)
        result(dirs, env["request_id"], request_id=env["request_id"], outcome="applied",
               action="list", measurement_view=True, list=VIEW, projects=[123])
        await actions.sweep_followups()
        check = command(dirs)
        result(dirs, check["request_id"], request_id=check["request_id"], outcome="applied",
               action="list", measurement_view=True, list=VIEW, projects=[123],
               processed_at=time.time())
        # a sweep reading the fresh re-read 20 s later (long tick) still shows it
        real = time.time
        monkeypatch.setattr(slack_actions.time, "time", lambda: real() + 20)
        await actions.sweep_followups()
        assert len(app.client.messages) == 1
        assert "合成値 1" in json.dumps(app.client.messages, ensure_ascii=False)
    asyncio.run(scenario())


def test_expired_guard_without_result_says_fixed_text_once(tmp_path, monkeypatch):
    async def scenario():
        actions, app, reg, dirs = _measurement(tmp_path)
        await actions._action(ack, *click())
        env = command(dirs)
        result(dirs, env["request_id"], request_id=env["request_id"], outcome="applied",
               action="list", measurement_view=True, list=VIEW, projects=[123])
        await actions.sweep_followups()
        real = time.monotonic
        monkeypatch.setattr(slack_actions.time, "monotonic",
                            lambda: real() + slack_actions.MEASUREMENT_GUARD_WAIT_S + 1)
        await actions.sweep_followups()
        await actions.sweep_followups()
        assert [m["text"] for m in app.client.messages] == [slack_actions.MEASUREMENT_UNCONFIRMED]
    asyncio.run(scenario())
