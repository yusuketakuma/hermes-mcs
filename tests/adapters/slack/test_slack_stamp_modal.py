"""Slack MCS stamp modal: the shown runner form pins the command. Synthetic only."""

import asyncio

import pytest

from adapters.common import text as common_text

from slack_actions_testkit import ack, click, command, fixture, result, submitted
from slack_actions_testkit import _no_modal_wait  # noqa: F401  (used via usefixtures)

pytestmark = pytest.mark.usefixtures("_no_modal_wait")

FORM = {"card_id": 7, "token": "b" * 32, "mcs_user_id": 501, "project_id": 123,
        "form_hash": "f" * 64, "types": [["viewed", "見ました"], ["completed", "完了"]],
        "posts": [{"opt": "p1", "message_id": 100, "source_hash": "1" * 64,
                   "label": "10-01 09:00 根元",
                   "own": {"type": None, "state": "observed", "observed_at": 1.0}}]}


def test_stamp_modal_pins_the_shown_form_into_the_confirmed_command(tmp_path):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="stamp")

        async def runner_form(request_id, timeout):
            return {"request_id": request_id, "outcome": "applied", "modal": True,
                    "params": {}, "form": FORM}
        actions._wait_result = runner_form
        await actions._action(ack, *click())
        view = app.client.views[0]["view"]
        assert view["title"]["text"] == "MCSスタンプ"
        blocks = {b["block_id"]: b for b in view["blocks"]}
        assert list(blocks) == ["post", "op", "reason"]
        assert [o["value"] for o in blocks["post"]["element"]["options"]] == ["p1"]
        env = command(dirs)
        view_body, submitted_view = submitted(view["private_metadata"], reason="確認しました")
        values = submitted_view["state"]["values"]
        values["post"] = {"post": {"selected_option": {"value": "p1"}}}
        values["op"] = {"op": {"selected_option": {"value": "add:completed"}}}
        await actions._modal(ack, view_body, submitted_view)
        # a later runner answer must not replace the form the user saw
        result(dirs, env["request_id"], request_id=env["request_id"], outcome="applied",
               modal=True, params={}, form={**FORM, "posts": []})
        await actions.sweep_followups()
        preview = app.client.messages[-1]["blocks"][0]["text"]["text"]
        assert "完了を押す" in preview and "収集アカウント" in preview
        confirm_id = app.client.messages[-1]["blocks"][-1]["elements"][0]["action_id"].split(":")[2]
        payload = reg.confirm(confirm_id)["payload"]
        assert payload["cmd"] == "ops.mcs_reaction"
        assert (payload["message_id"], payload["token"], payload["card_id"],
                payload["reaction_type"]) == (100, "b" * 32, 7, "completed")
        assert payload["actor"].startswith("slack:")
        # admission is only "awaiting MCS": the followup stays for the terminal result
        body, _ = click()
        await actions._confirm(ack, body, app.client.messages[-1]["blocks"][-1]["elements"][0])
        cid, before = payload["command_id"], len(app.client.messages)
        result(dirs, cid, outcome="applied", stamp_state="queued")
        await actions.sweep_followups()
        assert reg.followup(cid) is not None and len(app.client.messages) == before
        result(dirs, cid, outcome="applied", stamp_state="confirmed")
        await actions.sweep_followups()
        assert reg.followup(cid) is None
        assert "MCSで確認済み" in app.client.messages[-1]["text"]
    asyncio.run(scenario())


def test_stamp_click_without_a_runner_form_never_opens_a_blind_modal(tmp_path):
    async def scenario():
        actions, app, _, _ = fixture(tmp_path, kind="stamp")
        await actions._action(ack, *click())      # runner slow: no form
        assert app.client.views == []
        assert app.client.messages[-1]["text"] == common_text.STAMP_FORM_PENDING
    asyncio.run(scenario())


def test_obsolete_stamp_result_consumes_followup_without_an_empty_sdk_message(tmp_path):
    from slack_actions_testkit import SCOPE, TOKEN, TS

    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind='stamp')
        cid = 'obsolete-stamp'
        reg.put_followup(cid, {'kind': 'human', 'origin': {**SCOPE, 'message_id': TS},
            'actor': 'slack:T_SYNTHETIC:U_OPERATOR', 'user': 'U_OPERATOR', 'token': TOKEN})
        result(dirs, cid, outcome='rejected', stamp_state='rejected', stamp_error='source_changed')
        await actions._say('C_SYNTHETIC', 'U_OPERATOR', '')
        await actions.sweep_followups()
        await actions.sweep_followups()
        assert reg.followup(cid) is None and app.client.messages == []
    asyncio.run(scenario())
