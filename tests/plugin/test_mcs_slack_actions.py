"""Synthetic native Slack actions without network or patient data."""

import asyncio
import json
from pathlib import Path

from hermes_plugin.mcs_delivery import paths as shared_paths
from hermes_plugin.mcs_delivery import registry
from hermes_plugin.mcs_slack.actions import Actions
from hermes_plugin.mcs_slack.delivery import SlackCardAdapter
from hermes_plugin.mcs_slack.paths import notify_dirs
from notify_cmds import validate_int
from mcs_requests import validate as validate_human

TOKEN = "b" * 32
TS = "1790000000.000001"
SCOPE = {"transport": "slack", "profile": "cco",
         "application_id": "A_SYNTHETIC", "team_id": "T_SYNTHETIC",
         "channel_id": "C_SYNTHETIC"}


class Client:
    def __init__(self):
        self.messages = []
        self.views = []
        self.retry_handlers = [object()]

    async def chat_postEphemeral(self, **kwargs):
        self.messages.append(kwargs)

    async def views_open(self, **kwargs):
        self.views.append(kwargs)


class App:
    def __init__(self):
        self.client = Client()
        self.actions = {}
        self.views = {}

    def action(self, pattern):
        def register(fn):
            self.actions[pattern.pattern] = fn
        return register

    def view(self, name):
        def register(fn):
            self.views[name] = fn
        return register


async def ack():
    return None


def fixture(tmp_path, *, kind="body", project_ids=frozenset({123})):
    dirs = notify_dirs(str(tmp_path))
    for key in ("state", "cmd_int", "cmd_results"):
        Path(dirs[key]).mkdir(exist_ok=True)
    reg = registry.Registry(dirs["state"], scope=SCOPE)
    reg.put_tokens({TOKEN: {
        "action": kind, "team_id": SCOPE["team_id"],
        "channel_id": SCOPE["channel_id"], "message_id": TS,
        "project_id": 123, "context": {
            "project_id": 123, "source_message_id": 456,
            "source_hash": "a" * 64, "signals": {
            "synthetic-key": {"artifact_id": 17, "project_id": 123}}}}})
    app = App()
    settings = {**SCOPE, "allowed_user_ids": {"U_OPERATOR"},
                "project_ids": project_ids}
    sender = SlackCardAdapter(
        app, team_id=SCOPE["team_id"], application_id=SCOPE["application_id"],
        channel_id=SCOPE["channel_id"], profile=SCOPE["profile"],
        allowed_user_ids=settings["allowed_user_ids"])
    actions = Actions(app, settings, dirs, reg, sender, lambda *_a, **_k: None)
    actions.register()
    return actions, app, reg, dirs


def click(*, ts=TS, user="U_OPERATOR", team="T_SYNTHETIC"):
    return ({"team": {"id": team}, "api_app_id": "A_SYNTHETIC",
             "channel": {"id": "C_SYNTHETIC"}, "user": {"id": user},
             "message": {"ts": ts}, "trigger_id": "synthetic-trigger"},
            {"action_id": "mcs:a:" + TOKEN, "value": TOKEN})


def submitted(modal_id, user="U_OPERATOR", **fields):
    return ({"team": {"id": "T_SYNTHETIC"}, "api_app_id": "A_SYNTHETIC",
             "user": {"id": user}},
            {"private_metadata": modal_id, "state": {"values": {
                name: {name: {"value": value}}
                for name, value in fields.items()}}})


def result(dirs, cid, **values):
    with open(f"{dirs['cmd_results']}/{shared_paths.safe_name(cid)}.json", "w",
              encoding="utf-8") as stream:
        json.dump(values, stream)


def command(dirs):
    files = list(Path(dirs["cmd_int"]).glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_body_goes_only_to_original_user_after_runner_result(tmp_path):
    async def scenario():
        actions, app, _, dirs = fixture(tmp_path)
        body, action = click()
        await actions._action(ack, body, action)
        env = command(dirs)
        assert env["version"] == 2 and env["transport"] == "slack"
        assert env["origin"] == {**SCOPE, "message_id": TS}
        assert validate_int(env) is None
        assert app.client.messages == []
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", action="body", title="合成",
               body="合成された非公開の本文")
        await actions.sweep_followups()
        assert len(app.client.messages) == 1
        assert app.client.messages[0]["user"] == "U_OPERATOR"
        assert "合成された非公開の本文" in app.client.messages[0]["text"]
        await actions.sweep_followups()
        assert len(app.client.messages) == 1
    asyncio.run(scenario())


def test_ready_followup_behind_pending_batch_is_delivered(tmp_path):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path)
        origin = {**SCOPE, "message_id": TS}
        for index in range(32):
            cid = f"pending-{index}"
            reg.put_followup(cid, {
                "kind": "action", "request_id": cid, "origin": origin,
                "actor": "slack:T_SYNTHETIC:U_OPERATOR",
                "token": TOKEN, "user": "U_OPERATOR"})
        reg.put_followup("ready", {
            "kind": "action", "request_id": "ready", "origin": origin,
            "actor": "slack:T_SYNTHETIC:U_OPERATOR",
            "token": TOKEN, "user": "U_OPERATOR"})
        result(dirs, "ready", request_id="ready", outcome="applied",
               action="body", body="READY-SYNTHETIC")

        await actions.sweep_followups()
        await actions.sweep_followups()

        assert len(app.client.messages) == 1
        assert "READY-SYNTHETIC" in app.client.messages[0]["text"]
        assert "ready" not in reg.followups()
    asyncio.run(scenario())


def test_pending_body_is_not_sent_after_user_loses_access(tmp_path):
    async def scenario():
        old, app, reg, dirs = fixture(tmp_path)
        body, action = click()
        await old._action(ack, body, action)
        env = command(dirs)
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", action="body", body="PRIVATE-SYNTHETIC")
        old.unload()

        settings = {**SCOPE, "allowed_user_ids": {"U_OTHER"},
                    "project_ids": frozenset({123})}
        sender = SlackCardAdapter(
            app, team_id=SCOPE["team_id"],
            application_id=SCOPE["application_id"],
            channel_id=SCOPE["channel_id"], profile=SCOPE["profile"],
            allowed_user_ids=settings["allowed_user_ids"])
        current = Actions(app, settings, dirs, reg, sender,
                          lambda *_a, **_k: None)
        current.register()
        await current.sweep_followups()
        assert app.client.messages == []
        assert reg.followups() == {}
    asyncio.run(scenario())


def test_foreign_origin_and_project_do_not_publish(tmp_path):
    async def scenario():
        actions, app, _, dirs = fixture(tmp_path, project_ids=frozenset())
        for body, action in (click(), click(ts="1790000000.000002"),
                             click(user="U_FOREIGN"),
                             click(team="T_FOREIGN")):
            await actions._action(ack, body, action)
        assert list(Path(dirs["cmd_int"]).glob("*.json")) == []
        assert not app.client.views
    asyncio.run(scenario())


def test_request_requires_preview_and_explicit_same_user_confirm(tmp_path):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="request")
        body, action = click()
        await actions._action(ack, body, action)
        env = command(dirs)
        assert len(app.client.views) == 1
        modal_id = app.client.views[0]["view"]["private_metadata"]
        view_body, view = submitted(
            modal_id, title="合成依頼", reason="合成理由",
            assignee="", due_date="")
        await actions._modal(ack, view_body, view)
        assert len(list(Path(dirs["cmd_int"]).glob("*.json"))) == 1
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", modal=True, params={"project_id": 123})
        await actions.sweep_followups()
        preview = app.client.messages[-1]
        assert preview["user"] == "U_OPERATOR"
        confirm_action = preview["blocks"][1]["elements"][0]
        confirm_id = confirm_action["action_id"].split(":")[-1]
        assert reg.confirm(confirm_id) is not None
        foreign, _ = click(user="U_FOREIGN")
        await actions._confirm(ack, foreign, confirm_action)
        assert len(list(Path(dirs["cmd_int"]).glob("*.json"))) == 1
        await actions._confirm(ack, body, confirm_action)
        files = list(Path(dirs["cmd_int"]).glob("*.json"))
        assert len(files) == 2
        human = next(json.loads(p.read_text()) for p in files
                     if json.loads(p.read_text()).get("cmd") == "request.create")
        assert human["version"] == 1
        assert validate_int(human) is None
        assert validate_human(human) is None
        assert human["human_confirmed"] is True
        assert human["source_message_id"] == 456
        assert human["project_id"] == 123
        await actions._confirm(ack, body, confirm_action)
        assert len(list(Path(dirs["cmd_int"]).glob("*.json"))) == 2
    asyncio.run(scenario())


def test_tasks_list_and_status_transition_reach_original_user(tmp_path):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="tasks")
        body, action = click()
        await actions._action(ack, body, action)
        env = command(dirs)
        assert env["transport"] == "slack"
        transition = "d" * 32
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", action="tasks",
               tasks=[{"request_id": 7, "title": "合成タスク",
                       "status": "open", "assignee": None,
                       "due_date": None,
                       "transitions": {
                           "done": {"token": transition,
                                    "label": "✅ 完了"}}}],
               token_ctx={transition: {
                   "action": "task_status", "card_key": "k" * 32,
                   "kind": "thread", "project_id": 123}})
        await actions.sweep_followups()
        assert len(app.client.messages) == 1
        listing = app.client.messages[0]
        assert listing["user"] == "U_OPERATOR"
        assert "合成タスク" in listing["text"]
        buttons = [e for b in listing["blocks"] if b["type"] == "actions"
                   for e in b["elements"]]
        assert [b["action_id"] for b in buttons] == ["mcs:a:" + transition]
        assert buttons[0]["value"] == transition

        # the transition click arrives from the ephemeral message —
        # its ts differs from the card's, which the task_status ctx
        # is allowed for (card/project scope is still enforced)
        for file in Path(dirs["cmd_int"]).glob("*.json"):
            file.unlink()
        body2 = {**body, "message": {"ts": "1790000000.000999"}}
        await actions._action(
            ack, body2,
            {"action_id": "mcs:a:" + transition, "value": transition})
        env2 = command(dirs)
        assert env2["token"] == transition
        result(dirs, env2["request_id"], request_id=env2["request_id"],
               outcome="applied", action="task_status",
               status="done", title="合成タスク")
        await actions.sweep_followups()
        assert len(app.client.messages) == 2
        assert "完了" in app.client.messages[1]["text"]
        assert app.client.messages[1]["user"] == "U_OPERATOR"
    asyncio.run(scenario())


def test_dismiss_cancel_never_writes_human_command(tmp_path):
    async def scenario():
        actions, app, _, dirs = fixture(tmp_path, kind="dismiss")
        body, action = click()
        await actions._action(ack, body, action)
        env = command(dirs)
        modal_id = app.client.views[0]["view"]["private_metadata"]
        view_body, view = submitted(modal_id, reason="合成却下理由")
        await actions._modal(ack, view_body, view)
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", modal=True,
               params={"signal_key": "synthetic-key"})
        await actions.sweep_followups()
        cancel = app.client.messages[-1]["blocks"][1]["elements"][1]
        await actions._confirm(ack, body, cancel)
        assert len(list(Path(dirs["cmd_int"]).glob("*.json"))) == 1
    asyncio.run(scenario())


def test_cancel_during_confirm_publish_never_reports_cancelled(tmp_path):
    """取消 racing an in-flight 確定 must not claim the command was
    cancelled — the command file is being queued."""
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="dismiss")
        body, action = click()
        await actions._action(ack, body, action)
        env = command(dirs)
        modal_id = app.client.views[0]["view"]["private_metadata"]
        view_body, view = submitted(modal_id, reason="合成却下理由")
        await actions._modal(ack, view_body, view)
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", modal=True,
               params={"signal_key": "synthetic-key"})
        await actions.sweep_followups()
        buttons = app.client.messages[-1]["blocks"][1]["elements"]
        confirm_action, cancel_action = buttons
        confirm_id = confirm_action["action_id"].split(":")[2]

        gate = asyncio.Event()
        real = actions._publish

        async def slow_publish(envelope):
            await gate.wait()
            await real(envelope)
        actions._publish = slow_publish
        first = asyncio.create_task(actions._confirm(ack, body, confirm_action))
        for _ in range(50):
            await asyncio.sleep(0)
            if reg.confirm(confirm_id).get("in_flight"):
                break
        before = len(app.client.messages)
        await actions._confirm(ack, body, cancel_action)
        await actions._confirm(ack, body, confirm_action)
        replies = [m["text"] for m in app.client.messages[before:]]
        gate.set()
        await first
        assert replies and all("処理中" in t for t in replies)
        assert not any("取り消し" in m["text"] for m in app.client.messages)
        assert "受け付けました" in app.client.messages[-1]["text"]
        assert len(list(Path(dirs["cmd_int"]).glob("*.json"))) == 2
        assert reg.confirm(confirm_id) is None
    asyncio.run(scenario())


async def _to_confirm(actions, app, dirs, project_id):
    body, action = click()
    await actions._action(ack, body, action)
    env = command(dirs)
    modal_id = app.client.views[0]["view"]["private_metadata"]
    view_body, view = submitted(modal_id, title="合成依頼", reason="合成理由",
                                assignee="", due_date="")
    await actions._modal(ack, view_body, view)
    result(dirs, env["request_id"], request_id=env["request_id"],
           outcome="applied", modal=True, params={"project_id": project_id})
    await actions.sweep_followups()
    return body, app.client.messages[-1]["blocks"][1]["elements"]


def test_confirm_out_of_scope_after_preview_is_denied_but_cancellable(
        tmp_path):
    """A payload project leaving scope after the preview blocks 確定
    (権限がありません) without marking it in flight; 取消 still succeeds."""
    async def scenario():
        actions, app, reg, dirs = fixture(
            tmp_path, kind="request", project_ids=frozenset({123, 999}))
        body, (confirm_action, cancel_action) = await _to_confirm(
            actions, app, dirs, 999)
        confirm_id = confirm_action["action_id"].split(":")[2]
        actions._settings["project_ids"] = frozenset({123})
        before = len(app.client.messages)
        await actions._confirm(ack, body, confirm_action)
        assert [m["text"] for m in app.client.messages[before:]] == [
            "権限がありません。"]
        assert len(list(Path(dirs["cmd_int"]).glob("*.json"))) == 1
        assert not reg.confirm(confirm_id).get("in_flight")
        await actions._confirm(ack, body, cancel_action)
        assert app.client.messages[-1]["text"] == "取り消しました。"
        assert reg.confirm(confirm_id) is None
    asyncio.run(scenario())


def test_confirm_expiring_before_take_never_publishes(tmp_path):
    """TTL lapsing between the lookup and the take must not queue the
    command from the stale lookup."""
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="request")
        body, (confirm_action, _) = await _to_confirm(
            actions, app, dirs, 123)
        confirm_id = confirm_action["action_id"].split(":")[2]
        real = actions._pinned

        def lapse(*args):
            reg._data["pending_confirms"][confirm_id]["expires"] = 0
            return real(*args)
        actions._pinned = lapse
        await actions._confirm(ack, body, confirm_action)
        assert len(list(Path(dirs["cmd_int"]).glob("*.json"))) == 1
        assert reg.confirm(confirm_id) is None
    asyncio.run(scenario())


def test_card_project_out_of_scope_after_preview_still_cancellable(
        tmp_path):
    """The card's own project leaving scope must not strand the preview:
    取消 needs only the actor's card pin, 確定 answers 権限がありません."""
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="request")
        body, (confirm_action, cancel_action) = await _to_confirm(
            actions, app, dirs, 123)
        confirm_id = confirm_action["action_id"].split(":")[2]
        actions._settings["project_ids"] = frozenset({999})
        await actions._confirm(ack, body, confirm_action)
        assert app.client.messages[-1]["text"] == "権限がありません。"
        assert not reg.confirm(confirm_id).get("in_flight")
        foreign, _ = click(user="U_FOREIGN")
        before = len(app.client.messages)
        await actions._confirm(ack, foreign, cancel_action)
        assert len(app.client.messages) == before
        assert reg.confirm(confirm_id) is not None
        await actions._confirm(ack, body, cancel_action)
        assert app.client.messages[-1]["text"] == "取り消しました。"
        assert reg.confirm(confirm_id) is None
        assert len(list(Path(dirs["cmd_int"]).glob("*.json"))) == 1
    asyncio.run(scenario())


EXPIRED = "この確認は期限切れです。もう一度操作してください。"


def _refusal(tmp_path, spoil, *, clicker="U_OPERATOR", channel="C_SYNTHETIC"):
    """Drive a request to its preview, spoil it, click 確定 as `clicker`
    from `channel`; return the new ephemerals and the queued-file count."""
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="request")
        actions._settings["allowed_user_ids"] = {"U_OPERATOR", "U_OTHER"}
        _, (confirm_action, _) = await _to_confirm(actions, app, dirs, 123)
        confirm_id = confirm_action["action_id"].split(":")[2]
        spoil(reg, confirm_id)
        body, _ = click(user=clicker)
        body["channel"]["id"] = channel
        before = len(app.client.messages)
        await actions._confirm(ack, body, confirm_action)
        return (app.client.messages[before:],
                len(list(Path(dirs["cmd_int"]).glob("*.json"))),
                reg.confirm(confirm_id))
    return asyncio.run(scenario())


def test_confirm_refusals_answer_only_the_clicker(tmp_path):
    """Each refused 確定 answers the clicking allowed user with fixed
    Discord wording — ephemeral in the configured channel, no preview
    content — and queues nothing."""
    def expire(reg, cid):
        reg._data["pending_confirms"][cid]["expires"] = 0

    def drop_token(reg, _cid):
        reg._data["tokens"].pop(TOKEN)

    def keep(*_):
        pass
    cases = [
        ("expired", expire, {}, EXPIRED, False),
        ("token_gone", drop_token, {}, EXPIRED, True),
        ("other_user", keep, {"clicker": "U_OTHER"},
         "確認した本人のみ確定できます。", True),
        ("channel", keep, {"channel": "C_OTHER"},
         "確認を開始した場所と送信元が一致しません。", True),
    ]
    for name, spoil, kw, text, pending in cases:
        (tmp_path / name).mkdir()
        msgs, queued, rec = _refusal(tmp_path / name, spoil, **kw)
        clicker = kw.get("clicker", "U_OPERATOR")
        assert [(m["user"], m["channel"], m["text"]) for m in msgs] == [
            (clicker, "C_SYNTHETIC", text)], name
        assert all("blocks" not in m and "合成" not in m["text"]
                   for m in msgs), name
        assert queued == 1, name              # only the notification env
        assert (rec is not None) is pending, name
        assert not (rec or {}).get("in_flight"), name


def test_confirm_from_unverified_user_stays_silent(tmp_path):
    msgs, queued, rec = _refusal(tmp_path, lambda *_: None,
                                 clicker="U_FOREIGN")
    assert msgs == [] and queued == 1 and rec is not None


def test_confirm_gone_at_take_tells_the_clicker(tmp_path):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="request")
        body, (confirm_action, _) = await _to_confirm(
            actions, app, dirs, 123)
        confirm_id = confirm_action["action_id"].split(":")[2]
        real = actions._pinned

        def lapse(*args):
            reg._data["pending_confirms"][confirm_id]["expires"] = 0
            return real(*args)
        actions._pinned = lapse
        await actions._confirm(ack, body, confirm_action)
        assert app.client.messages[-1]["text"] == EXPIRED
        assert app.client.messages[-1]["user"] == "U_OPERATOR"
        assert len(list(Path(dirs["cmd_int"]).glob("*.json"))) == 1
    asyncio.run(scenario())


def test_modal_refusals_answer_only_the_submitter(tmp_path):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="request")
        actions._settings["allowed_user_ids"] = {"U_OPERATOR", "U_OTHER"}
        await actions._action(ack, *click())
        modal_id = app.client.views[0]["view"]["private_metadata"]
        fields = {"title": "合成依頼", "reason": "合成理由",
                  "assignee": "", "due_date": ""}
        for user, mid, text in (
                ("U_FOREIGN", modal_id, None),
                ("U_OTHER", modal_id, "操作した本人のみ送信できます。"),
                ("U_OPERATOR", "0" * 16,
                 "この入力フォームは期限切れです。もう一度操作してください。")):
            before = len(app.client.messages)
            await actions._modal(ack, *submitted(mid, user=user, **fields))
            got = [(m["user"], m["text"])
                   for m in app.client.messages[before:]]
            assert got == ([] if text is None else [(user, text)])
        actions._settings["project_ids"] = frozenset({999})
        await actions._modal(ack, *submitted(modal_id, **fields))
        assert app.client.messages[-1]["text"] == "権限がありません。"
        assert not reg.followups()
        assert len(list(Path(dirs["cmd_int"]).glob("*.json"))) == 1
    asyncio.run(scenario())
