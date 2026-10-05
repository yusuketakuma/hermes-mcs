"""Synthetic native Slack actions without network or patient data."""

import asyncio
import json
from pathlib import Path

import pytest

from hermes_plugin.mcs_delivery import paths as shared_paths
from hermes_plugin.mcs_delivery import registry
from hermes_plugin.mcs_slack import actions as slack_actions
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


@pytest.fixture(autouse=True)
def _no_modal_wait(monkeypatch):
    # the 📝 modal polls once for the runner's form; tests publish any
    # result up front, so no polling window is needed
    monkeypatch.setattr(slack_actions, "MODAL_OPEN_WAIT_S", 0.0)


def fixture(tmp_path, *, kind="body", project_ids=frozenset({123})):
    dirs = notify_dirs(str(tmp_path))
    for key in ("state", "cmd_int", "cmd_results", "flags"):
        Path(dirs[key]).mkdir(exist_ok=True)
    # 確定 re-checks the runner flags, as the other interactive actions do
    Path(dirs["flags"], "notify.json").write_text(
        json.dumps({"interactive": True, "transport": "slack"}), encoding="utf-8")
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
            modal_id, task="合成依頼",
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
        view_body, view = submitted(modal_id, reason_code="false_positive",
                                    note="合成却下理由")
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
        view_body, view = submitted(modal_id, reason_code="false_positive",
                                    note="合成却下理由")
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
    view_body, view = submitted(modal_id, task="合成依頼",
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


# ---------- card buttons: task form, denial, report, summary, link -------

def _set_ctx(reg, kind, **extra):
    reg.put_tokens({TOKEN: {
        "action": kind, "team_id": SCOPE["team_id"],
        "channel_id": SCOPE["channel_id"], "message_id": TS,
        "project_id": 123, "context": {
            "project_id": 123, "source_message_id": 456,
            "source_hash": "a" * 64, **extra}}})


def test_task_modal_offers_roster_and_prefill(tmp_path):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="request")

        async def runner_form(request_id, timeout):
            return {"request_id": request_id, "outcome": "applied",
                    "modal": True, "params": {"project_id": 123},
                    "form": {"hint": "残薬を確認",
                             "staff": ["山田 花子（みどり薬局）",
                                       "佐藤 一郎（みどり薬局）"]}}
        actions._wait_result = runner_form
        body, action = click()
        body["user"]["name"] = "佐藤 一郎"
        await actions._action(ack, body, action)
        view = app.client.views[0]["view"]
        assert view["title"]["text"] == "タスク作成"
        blocks = {b["block_id"]: b for b in view["blocks"]}
        assert list(blocks) == ["task", "assignee_pick", "assignee",
                                "due_date", "reason"]
        assert blocks["task"]["element"]["initial_value"] == "残薬を確認"
        assert blocks["task"]["optional"] is False
        pick = blocks["assignee_pick"]["element"]
        assert pick["type"] == "static_select"
        assert [o["value"] for o in pick["options"]] == [
            "山田 花子（みどり薬局）", "佐藤 一郎（みどり薬局）"]
        # the clicker is preselected
        assert pick["initial_option"]["value"] == "佐藤 一郎（みどり薬局）"

        env = command(dirs)
        modal_id = view["private_metadata"]
        view_body, submitted_view = submitted(
            modal_id, task="残薬を確認", assignee="", due_date="2026-10-01")
        submitted_view["state"]["values"]["assignee_pick"] = {
            "assignee_pick": {"selected_option": {
                "value": "山田 花子（みどり薬局）"}}}
        await actions._modal(ack, view_body, submitted_view)
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", modal=True, params={"project_id": 123})
        await actions.sweep_followups()
        confirm_id = app.client.messages[-1]["blocks"][1]["elements"][0][
            "action_id"].split(":")[2]
        assert "理由: 通知カードからタスク作成" in \
            app.client.messages[-1]["blocks"][0]["text"]["text"]
        payload = reg.confirm(confirm_id)["payload"]
        assert payload["cmd"] == "request.create"
        assert (payload["title"], payload["assignee"], payload["due_date"],
                payload["reason"]) == ("残薬を確認", "山田 花子（みどり薬局）",
                                       "2026-10-01", "通知カードからタスク作成")
        assert validate_human(payload) is None
    asyncio.run(scenario())


def test_task_modal_without_roster_defaults_to_clicker(tmp_path):
    async def scenario():
        actions, app, _, _ = fixture(tmp_path, kind="request")
        body, action = click()
        body["user"]["name"] = "佐藤 一郎"
        await actions._action(ack, body, action)     # runner slow: no form
        blocks = {b["block_id"]: b
                  for b in app.client.views[0]["view"]["blocks"]}
        assert list(blocks) == ["task", "assignee", "due_date", "reason"]
        assert "initial_value" not in blocks["task"]["element"]
        assert blocks["assignee"]["element"]["initial_value"] == "佐藤 一郎"
    asyncio.run(scenario())


def test_denied_member_click_is_answered(tmp_path):
    async def scenario():
        actions, app, _, dirs = fixture(tmp_path, kind="ack")
        await actions._action(ack, *click(user="U_STAFF2"))
        assert [(m["user"], m["text"]) for m in app.client.messages] == [
            ("U_STAFF2", "権限がありません。")]
        # another workspace stays silent — nothing unverified earns a post
        await actions._action(ack, *click(user="U_STAFF2", team="T_OTHER"))
        assert len(app.client.messages) == 1
        assert list(Path(dirs["cmd_int"]).glob("*.json")) == []
        # several allowed members all work
        actions._settings["allowed_user_ids"] = {"U_OPERATOR", "U_STAFF2"}
        actions._sender._allowed_user_ids = frozenset(
            actions._settings["allowed_user_ids"])
        await actions._action(ack, *click(user="U_STAFF2"))
        assert len(list(Path(dirs["cmd_int"]).glob("*.json"))) == 1
    asyncio.run(scenario())


def test_report_modal_builds_feedback_command(tmp_path):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="report")
        _set_ctx(reg, "report", extract_ref={
            "message_id": 456, "artifact_id": 77, "content_hash": "a" * 64})
        await actions._action(ack, *click())
        view = app.client.views[0]["view"]
        assert view["title"]["text"] == "抽出の誤りを報告"
        field = view["blocks"][0]["element"]
        assert field["type"] == "static_select"
        assert [o["value"] for o in field["options"]] == [
            "summary", "meds", "symptoms", "requests", "vitals", "other"]
        env = command(dirs)
        body, submitted_view = submitted(view["private_metadata"],
                                         note="用量が違う")
        submitted_view["state"]["values"]["field"] = {
            "field": {"selected_option": {"value": "meds"}}}
        await actions._modal(ack, body, submitted_view)
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", modal=True, params={})
        await actions.sweep_followups()
        confirm_id = app.client.messages[-1]["blocks"][1]["elements"][0][
            "action_id"].split(":")[2]
        payload = reg.confirm(confirm_id)["payload"]
        assert {k: payload[k] for k in ("cmd", "project_id", "message_id",
                                        "artifact_id", "field", "reason")} \
            == {"cmd": "ops.extract_feedback", "project_id": 123,
                "message_id": 456, "artifact_id": 77, "field": "meds",
                "reason": "用量が違う"}
        assert validate_human(payload) is None
        assert validate_int(payload) is None
    asyncio.run(scenario())


def test_summary_reaches_only_the_clicker(tmp_path):
    async def scenario():
        actions, app, _, dirs = fixture(tmp_path, kind="summary")
        await actions._action(ack, *click())
        env = command(dirs)
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", action="summary", title="🧾 サマリー",
               body="※ 暫定集約\n■ 未完了タスク: なし")
        await actions.sweep_followups()
        assert [m["user"] for m in app.client.messages] == ["U_OPERATOR"]
        assert "暫定集約" in app.client.messages[0]["text"]
    asyncio.run(scenario())


def test_link_button_click_is_only_acked(tmp_path):
    async def scenario():
        actions, app, _, dirs = fixture(tmp_path)
        acked = []

        async def record():
            acked.append(1)
        await app.actions["^mcs:link$"](record, *click())
        assert acked == [1] and app.client.messages == []
        assert list(Path(dirs["cmd_int"]).glob("*.json")) == []
    asyncio.run(scenario())


# ---------- 📋 / 🔎 / 🚫 reason code --------------------------------------

def _list_result(items):
    return {"title": "📋 自分のタスク（担当: 佐藤 一郎）", "head": ["未完了"],
            "items": items, "more": 0, "empty": "なし", "notes": ["※ 注記"]}


def test_my_tasks_sends_the_name_and_filters_scope(tmp_path):
    async def scenario():
        actions, app, _, dirs = fixture(tmp_path, kind="mytasks")
        body, action = click()
        body["user"]["name"] = "佐藤 一郎"
        await actions._action(ack, body, action)
        env = command(dirs)
        # the static project scope rides along so runner counts match
        assert env["input"] == {"name": "佐藤 一郎", "projects": [123]}
        assert validate_int(env) is None
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", action="list", list=_list_result([
                   {"project_id": 123, "text": "・#1 範囲内"},
                   {"project_id": 999, "text": "・#2 範囲外"}]))
        await actions.sweep_followups()
        text_ = "\n".join(m["text"] for m in app.client.messages)
        assert [m["user"] for m in app.client.messages] == ["U_OPERATOR"]
        assert "範囲内" in text_ and "範囲外" not in text_
        # Slack answers are plain text — no markup characters at all
        assert "*" not in text_ and "📋 自分のタスク" in text_
    asyncio.run(scenario())


def test_my_tasks_prefers_users_info_display_name(tmp_path):
    """body.user.name is the legacy handle — 📋 matches on the
    users.info display name when the lookup works."""
    async def scenario():
        actions, app, _, dirs = fixture(tmp_path, kind="mytasks")

        async def users_info(user):
            return {"ok": True, "user": {
                "name": "sato", "profile": {"display_name": "",
                                            "real_name": "佐藤 一郎"}}}
        app.client.users_info = users_info
        body, action = click()
        body["user"]["name"] = "sato"
        await actions._action(ack, body, action)
        assert command(dirs)["input"]["name"] == "佐藤 一郎"
    asyncio.run(scenario())


def test_search_modal_submits_query_as_view_click(tmp_path):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="search")
        await actions._action(ack, *click())
        view = app.client.views[0]["view"]
        assert view["title"]["text"] == "この患者を検索"
        first = command(dirs)
        Path(dirs["cmd_int"], shared_paths.safe_name(first["command_id"])
             + ".json").unlink()
        view_body, submitted_view = submitted(view["private_metadata"],
                                              query=" 発熱  咳 ")
        await actions._modal(ack, view_body, submitted_view)
        env = command(dirs)
        assert env["input"] == {"query": "発熱 咳"}
        assert env["token"] == TOKEN and validate_int(env) is None
        assert env["command_id"] != first["command_id"]
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", action="list", list=_list_result(
                   [{"project_id": 123, "text": "・09-24 看護師: 発熱あり"}]))
        await actions.sweep_followups()
        assert "発熱あり" in app.client.messages[-1]["text"]
        assert reg.modal(view["private_metadata"]) is None
    asyncio.run(scenario())


def test_digest_modal_submits_scope_and_answers_with_a_card(tmp_path):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="digest")
        await actions._action(ack, *click())
        view = app.client.views[0]["view"]
        assert view["title"]["text"] == "サマリー（絞込み）"
        first = command(dirs)
        Path(dirs["cmd_int"], shared_paths.safe_name(first["command_id"])
             + ".json").unlink()
        view_body, submitted_view = submitted(view["private_metadata"],
                                              query=" mine  days:2 ", name="佐藤 一郎")
        await actions._modal(ack, view_body, submitted_view)
        env = command(dirs)
        assert env["input"]["query"] == "mine days:2"
        assert env["input"]["projects"] == [123]
        assert env["input"]["name"] == "佐藤 一郎"
        assert validate_int(env) is None
        parts = {"containers": [{"type": "heading", "text": "📊 MCS サマリー"},
                                {"type": "text", "text": "■ 新着\n・project 123 1件"}],
                 "footer": [{"type": "text", "text": "※ 注記"}]}
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", action="digest", parts=parts,
               text="*📊 MCS サマリー*\n■ 新着")
        await actions.sweep_followups()
        sent = app.client.messages[-1]
        assert sent["text"].startswith("*📊 MCS サマリー*")
        assert sent["blocks"][0]["type"] == "header"
    asyncio.run(scenario())


def test_summary_slash_command_answers_ephemerally(tmp_path, monkeypatch):
    from adapters.common import summary
    calls = []
    monkeypatch.setattr(summary, "answer", lambda *a, **kw: (calls.append(
        (a, kw)) or {"text": "*📊 MCS サマリー*", "parts": {
            "containers": [{"type": "heading", "text": "📊 MCS サマリー"}],
            "footer": []}}))

    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path)
        actions._settings["snapshot"] = "/synthetic/snap.db"
        body = {"team_id": SCOPE["team_id"], "api_app_id": "A_SYNTHETIC",
                "user_id": "U_OPERATOR", "channel_id": "C_SYNTHETIC",
                "response_url": "https://evil.invalid/", "text": "station:みどり days:2"}
        await actions._summary(ack, body)
        assert app.client.messages[-1]["user"] == "U_OPERATOR"
        assert app.client.messages[-1]["channel"] == "C_SYNTHETIC"
        assert app.client.messages[-1]["blocks"][0]["type"] == "header"
        assert calls[-1][0][1] == "station:みどり days:2"
        assert calls[-1][1]["allowed"] == [123]
        await actions._summary(ack, {**body, "user_id": "U_OTHER"})
        assert len(app.client.messages) == 1 and len(calls) == 1
    asyncio.run(scenario())


def test_dismiss_modal_sends_reason_code(tmp_path):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="dismiss")
        await actions._action(ack, *click())
        view = app.client.views[0]["view"]
        assert [b["block_id"] for b in view["blocks"]] == ["reason_code",
                                                           "note"]
        env = command(dirs)
        body, submitted_view = submitted(view["private_metadata"], note="")
        submitted_view["state"]["values"]["reason_code"] = {
            "reason_code": {"selected_option": {"value": "already_handled"}}}
        await actions._modal(ack, body, submitted_view)
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", modal=True,
               params={"signal_key": "synthetic-key"})
        await actions.sweep_followups()
        confirm_id = app.client.messages[-1]["blocks"][1]["elements"][0][
            "action_id"].split(":")[2]
        payload = reg.confirm(confirm_id)["payload"]
        assert (payload["reason_code"], payload["reason"]) == (
            "already_handled", "対応済み")
        assert validate_human(payload) is None
    asyncio.run(scenario())


@pytest.mark.parametrize("kind,fields,params,expect", [
    ("request", {"title": "旧件名", "reason": "旧理由", "assignee": "",
                 "due_date": ""}, {"project_id": 123},
     {"title": "旧件名", "reason": "旧理由"}),
    ("dismiss", {"reason": "旧理由"}, {"signal_key": "synthetic-key"},
     {"reason": "旧理由"}),
])
def test_modal_opened_before_upgrade_still_submits(tmp_path, kind, fields,
                                                   params, expect):
    """A pending modal written by the previous worker has no field_ids —
    its legacy block ids are still read."""
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind=kind)
        await actions._action(ack, *click())
        env = command(dirs)
        modal_id = app.client.views[0]["view"]["private_metadata"]
        pending = reg.modal(modal_id)
        pending.pop("field_ids")
        reg.put_modal(modal_id, pending)
        await actions._modal(ack, *submitted(modal_id, **fields))
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", modal=True, params=params)
        await actions.sweep_followups()
        confirm_id = app.client.messages[-1]["blocks"][1]["elements"][0][
            "action_id"].split(":")[2]
        payload = reg.confirm(confirm_id)["payload"]
        assert {k: payload[k] for k in expect} == expect
        assert validate_human(payload) is None
    asyncio.run(scenario())


@pytest.mark.parametrize("body", ["BP<90 & >60 <!channel> <@U0OP>", "&<漢" * 1900],
                         ids=["literal_mentions", "utf8_entity_expansion"])
def test_ephemeral_source_text_is_literal_and_every_chunk_fits_wire_budget(tmp_path, body):
    from html import unescape

    async def scenario():
        actions, app, _, _ = fixture(tmp_path)
        await actions._say(SCOPE["channel_id"], "U_OPERATOR", body)
        wire = [message["text"] for message in app.client.messages]
        assert wire and all(len(chunk.encode("utf-8")) <= 4000 for chunk in wire)
        assert all("<!channel>" not in chunk and "<@U0OP>" not in chunk for chunk in wire)
        # Decode each chunk separately: an entity must not straddle messages.
        assert "".join(unescape(chunk) for chunk in wire) == body
        assert all(message["user"] == "U_OPERATOR" and message["link_names"] is False
                   for message in app.client.messages)
    asyncio.run(scenario())


def test_task_blocks_pair_each_task_with_its_own_buttons():
    tasks = [
        {"request_id": 1, "status": "open", "title": "残薬確認",
         "assignee": "合成 太郎", "due_date": "2000-01-01",
         "transitions": {"in_progress": {"label": "対応中", "token": "a" * 32},
                         "done": {"label": "完了", "token": "b" * 32}}},
        {"request_id": 2, "status": "done", "title": "完了済み",
         "due_date": "2000-01-01", "transitions": {}},
        {"request_id": 3, "status": "in_progress", "title": "先の予定",
         "due_date": "2999-12-31",
         "transitions": {"done": {"label": "完了", "token": "c" * 32}}}]
    blocks = slack_actions._task_blocks(tasks)
    dumped = json.dumps(blocks, ensure_ascii=False)
    assert "**" not in dumped
    kinds = [b["type"] for b in blocks]
    assert kinds == ["section", "section", "actions", "section",
                     "section", "actions"]
    texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert texts[1] == ("⚠ 期限切れ ⬜ #1 残薬確認\n"
                        "担当: 合成 太郎 ・ 期限: 2000-01-01")
    assert texts[2] == "✅ #2 完了済み\n期限: 2000-01-01"
    assert texts[3].startswith("⏳ #3 先の予定")
    assert [e["value"] for e in blocks[2]["elements"]] == ["a" * 32, "b" * 32]
    assert [e["value"] for e in blocks[5]["elements"]] == ["c" * 32]


def test_ephemeral_chunks_split_by_characters_at_line_boundaries():
    # a Japanese answer of 1900 chars is one message (was 2 by bytes)
    one = "本文（1/2）\n" + "合" * 1890
    assert list(slack_actions._ephemeral_chunks(one)) == [one]
    lines = ["行" * 1000 + "<&>"] * 5
    chunks = list(slack_actions._ephemeral_chunks("\n".join(lines)))
    assert all(len(c) <= 3000 for c in chunks)
    assert len(chunks) == 3
    assert all(c.startswith("行") for c in chunks)
    assert "".join(chunks).count("&lt;&amp;&gt;") == 5


def test_plain_answers_carry_no_markup():
    from hermes_plugin.mcs_delivery import text
    body = text.view_answer({"outcome": "applied", "action": "body",
                             "title": "合成", "body": "本文"},
                            lambda _p: True, markdown=False)
    assert body == [("合成\n本文", None)]
    tasks = text.view_answer({"outcome": "applied", "action": "tasks",
                              "tasks": [{"request_id": 1, "status": "open",
                                         "title": "t"}]},
                             lambda _p: True, markdown=False)
    assert "*" not in tasks[0][0]
