"""Regressions: 📋 task list without _mcs_path, and real ephemeral clicks."""
import asyncio
import subprocess
import sys
from pathlib import Path

from test_mcs_slack_actions import (ack, click, command, fixture,  # noqa: F401
                                    result, _no_modal_wait)

ROOT = Path(__file__).resolve().parents[3]


def test_task_blocks_import_without_flat_mcs_paths():
    # Hermes loads the Slack worker with only the repo root on sys.path
    code = ("import sys; sys.path.insert(0, sys.argv[1]);"
            "from adapters.slack import actions;"
            "assert '_mcs_path' not in sys.modules;"
            "b = actions._task_blocks([{'request_id': 1, 'title': 't',"
            " 'status': 'open', 'due_date': '2000-01-01'}]);"
            "assert '期限切れ' in str(b)")
    proc = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_ephemeral_task_transition_click_uses_container_ts(tmp_path):
    async def scenario():
        actions, app, reg, dirs = fixture(tmp_path, kind="tasks")
        body, action = click()
        await actions._action(ack, body, action)
        env = command(dirs)
        transition = "d" * 32
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", action="tasks",
               tasks=[{"request_id": 7, "title": "合成タスク",
                       "status": "open", "transitions": {
                           "done": {"token": transition,
                                    "label": "✅ 完了"}}}],
               token_ctx={transition: {
                   "action": "task_status", "card_key": "k" * 32,
                   "kind": "thread", "project_id": 123}})
        await actions.sweep_followups()
        for file in Path(dirs["cmd_int"]).glob("*.json"):
            file.unlink()
        # real Slack shape: no "message", location only in container
        body2 = {k: v for k, v in body.items() if k != "message"}
        body2["container"] = {"type": "message", "is_ephemeral": True,
                              "message_ts": "1790000000.000999",
                              "channel_id": "C_SYNTHETIC"}
        await actions._action(
            ack, body2,
            {"action_id": "mcs:a:" + transition, "value": transition})
        assert command(dirs)["token"] == transition

        # a card token clicked from an ephemeral container stays pinned
        for file in Path(dirs["cmd_int"]).glob("*.json"):
            file.unlink()
        await actions._action(ack, body2, action)
        assert list(Path(dirs["cmd_int"]).glob("*.json")) == []
    asyncio.run(scenario())
