"""Shared synthetic Slack native-actions harness — a fake Bolt app/client, a
token-bound registry fixture, click/modal payload builders and runner-result
helpers.  Not a test module (no ``test_`` prefix); sibling files import it via
the tests/ sys.path bootstrap."""
import json
from pathlib import Path

import pytest

from adapters.common import paths as shared_paths
from adapters.common import registry
from adapters.slack import actions as slack_actions
from adapters.slack.actions import Actions
from adapters.slack.delivery import SlackCardAdapter
from adapters.slack.paths import notify_dirs

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


@pytest.fixture
def _no_modal_wait(monkeypatch):
    """Not autouse: a module opts in with ``pytest.mark.usefixtures``, so a
    missing import fails loudly instead of silently restoring the wait."""
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
        channel_id=SCOPE["channel_id"], profile=SCOPE["profile"])
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
