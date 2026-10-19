"""A 📋 task list answered with Block Kit must be one
ephemeral message. When its plain-text fallback exceeds one 3000-char
ephemeral chunk, _say sends the blocks with chunk 1 and then posts the
fallback's remaining chunks as extra text-only messages — the tail of
the list appears a second time below the block view."""
import asyncio

from slack_actions_testkit import ack, click, command, fixture, result


def test_long_task_list_with_blocks_is_one_message(tmp_path):
    async def scenario():
        actions, app, _reg, dirs = fixture(tmp_path, kind="tasks")
        body, action = click()
        await actions._action(ack, body, action)
        env = command(dirs)
        tasks = [{"request_id": n, "title": f"合成タスク{n} " + "あ" * 900,
                  "status": "open", "assignee": None, "due_date": None,
                  "transitions": {"done": {"token": str(n) * 32,
                                           "label": "✅ 完了"}}}
                 for n in range(1, 5)]
        result(dirs, env["request_id"], request_id=env["request_id"],
               outcome="applied", action="tasks", tasks=tasks, token_ctx={})
        await actions.sweep_followups()
        sent = app.client.messages
        assert sent and sent[0].get("blocks"), sent
        shown = "".join(b["text"]["text"] for b in sent[0]["blocks"]
                        if b["type"] == "section")
        assert all(t["title"] in shown for t in tasks)   # blocks carry every task
        extra = [m["text"][:40] for m in sent[1:]]
        assert len(sent) == 1, f"duplicate text-only follow-ups: {extra}"

    asyncio.run(scenario())
