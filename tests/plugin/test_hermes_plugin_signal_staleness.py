"""Synthetic native signal approval refuses unreadable latest transitions."""
import json

import pytest

import hermes_plugin
import ledger
from ops_testkit import _source


class Context:
    def __init__(self, root):
        self.settings = {
            "snapshot": str(root / "snapshots" / "ledger-snapshot.db"),
            "inbox": str(root / "cmd"), "allowed_user_ids": ["human"],
            "allowed_chat_ids": ["room"], "project_ids": [1],
        }

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


IDENTITY = {
    "platform": "discord", "authorized": True, "internal": False,
    "is_bot": False, "via_upstream_relay": False, "native_input": True,
    "user_id": "human", "chat_id": "room", "scope_id": "guild",
    "profile": "default", "message_id": "synthetic-message",
}
PREVIEW = {
    "op": "control", "phase": "preview", "action": "signal_dismiss",
    "project_id": 1, "signal_key": "synthetic-plugin-signal",
    "reason": "人が合成シグナルを確認",
}


def _caller(root, transport):
    ctx = Context(root)
    if transport == "discord":
        handler = hermes_plugin._make_handler(ctx)
        return lambda command: json.loads(handler(json.dumps(command), command_context=IDENTITY))
    from adapters.common import commands
    data_root = root / "data"
    (data_root / "flags").mkdir(parents=True)
    (data_root / "flags" / "notify.json").write_text(json.dumps({
        "interactive": True, "transport": transport, "route_epoch": 1,
    }))
    settings = {**ctx.settings, "data_root": str(data_root), "transport": transport,
                "team_id": "synthetic-team", "application_id": "synthetic-app",
                "channel_id": "room", "profile": "default", "route_epoch": 1}
    return lambda command: json.loads(commands.answer(
        settings, json.dumps(command), user="human", channel="room"))


@pytest.mark.parametrize("latest", ["{broken", "[" * 1500 + "]" * 1500,
                                    "[]", '{"state":"dismissed"}'],
                         ids=["malformed", "deep", "nonobject", "closed"])
@pytest.mark.parametrize("phase", ["preview", "confirm"])
@pytest.mark.parametrize("transport", ["discord", "slack", "lineworks"])
def test_latest_transition_is_never_skipped_for_older_open(tmp_path, latest, phase, transport):
    db = _source(tmp_path)
    inbox = tmp_path / "cmd"
    inbox.mkdir()
    call = _caller(tmp_path, transport)

    def publish():
        assert ledger.publish_snapshot(str(tmp_path / "source.db"),
                                       str(tmp_path / "snapshots"))

    try:
        first = db.artifact_add("signal_v1", '{"state":"open","evidence":{}}',
                                project_id=1, meta={"key": PREVIEW["signal_key"]})
        publish()
        initial = call(PREVIEW)
        assert initial["ok"] and initial["payload"]["expected_signal_artifact_id"] == first
        assert list(inbox.iterdir()) == []
        second = db.artifact_add("signal_v1", latest, project_id=1,
                                 meta={"key": PREVIEW["signal_key"]})
        assert second > first
        publish()
        command = PREVIEW if phase == "preview" else {
            "op": "control", "phase": "confirm",
            **{key: initial[key] for key in ("payload", "payload_hash", "origin")},
        }
        assert call(command) == {"ok": False, "error": "signal_changed"}
        assert list(inbox.iterdir()) == []
        assert len(db.artifacts("signal_v1", project_id=1)) == 2
    finally:
        db.close()


@pytest.mark.parametrize("transport", ["discord", "slack", "lineworks"])
def test_clean_signal_confirmation_still_queues_pinned_command(tmp_path, transport):
    db = _source(tmp_path)
    (tmp_path / "cmd").mkdir()
    try:
        artifact = db.artifact_add("signal_v1", '{"state":"open","evidence":{}}',
                                   project_id=1, meta={"key": PREVIEW["signal_key"]})
        assert ledger.publish_snapshot(str(tmp_path / "source.db"),
                                       str(tmp_path / "snapshots"))
        call = _caller(tmp_path, transport)
        initial = call(PREVIEW)
        confirmed = call({
            "op": "control", "phase": "confirm",
            **{key: initial[key] for key in ("payload", "payload_hash", "origin")},
        })
        assert confirmed["ok"] and confirmed["receipt"]["outcome"] == "queued"
        files = list((tmp_path / "cmd").glob("*.json"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text())
        assert payload["expected_signal_artifact_id"] == artifact
        actor = "discord:human" if transport == "discord" else f"{transport}:synthetic-team:human"
        assert payload["actor"] == actor and payload["human_confirmed"] is True
    finally:
        db.close()
