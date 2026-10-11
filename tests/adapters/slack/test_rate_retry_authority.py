"""A Slack 429 retry re-checks current delivery authority after the wait (synthetic)."""
import asyncio
import json
from types import SimpleNamespace

import ledger
import notify_cards
import notify_cmds
import pytest
from adapters.common import journal
from adapters.slack import delivery
from notify_testkit import NOW, _dispatch, _intent, _seed_thread
from slack_testkit import SLACK, _mkworld
from test_mcs_slack_actions import fixture

BODY = "PRIVATE-SYNTHETIC-MESSAGE"


class RateLimited(Exception):
    response = SimpleNamespace(status_code=429, data={"ok": False, "error": "ratelimited"},
                               headers={"Retry-After": "2"})


def _change(root, mode):
    flags_file = root / "flags" / "notify.json"
    flags = json.loads(flags_file.read_text())
    if mode == "off":
        flags["interactive"] = False
    elif mode == "epoch":
        flags["route_epoch"] += 1
    elif mode == "restore":
        (root / "restore_pending.json").write_text("{}")
    flags_file.write_text(json.dumps(flags))


@pytest.mark.parametrize("target", ["card", "body"])
@pytest.mark.parametrize("mode", ["none", "off", "epoch", "restore", "repeat429"])
def test_rate_limit_retry_needs_current_authority(tmp_path, monkeypatch, target, mode):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    root = tmp_path / "data"
    root.mkdir()
    led = ledger.Ledger(str(root / "ledger.db"))
    try:
        _seed_thread(led, mids=(100,))
        led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", (BODY,))
        led.db.commit()
        assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)["dispatched"]
        w = _mkworld(led)
        attempts, waits = [], []
        post = w.client.chat_postMessage

        async def flaky(**kwargs):
            if ("thread_ts" in kwargs) == (target == "body"):
                attempts.append(kwargs)
                if len(attempts) == 1 or mode == "repeat429":
                    raise RateLimited()
            return await post(**kwargs)

        async def sleep(seconds):
            waits.append(seconds)
            _change(root, mode)

        w.client.chat_postMessage = flaky
        monkeypatch.setattr(delivery.asyncio, "sleep", sleep)
        logs = []
        w.worker._log = lambda event, **kw: logs.append(event)

        async def scenario():
            assert await w.sender.bind()
            assert w.worker.acquire_scope_lock()
            try:
                await w.worker.tick()
                result = {"errors": []}
                notify_cmds.drain_int_commands(led, result, SLACK, str(root))
                await w.worker.tick()
            finally:
                w.worker.release_scope_lock()

        asyncio.run(scenario())
        assert waits == [2.0]
        results = [r for rows in journal.scan(w.dirs["state"]).values() for r in rows
                   if r.get("phase") == "result"
                   and (r.get("kind") == "body_part") == (target == "body")]
        if mode == "none":
            assert len(attempts) == 2 and results[-1]["result"] == "delivered"
        else:
            # a lost authority or a second 429 never sends again; the 429 stands
            assert len(attempts) == (2 if mode == "repeat429" else 1)
            assert (results[-1]["result"], results[-1]["error_code"]) == ("not_sent", "ratelimited")
        assert ("retry_skipped" in logs) == (mode in ("off", "epoch", "restore"))
        if target == "body":
            assert len(w.client.thread_posts) == (1 if mode == "none" else 0)
    finally:
        led.close()


@pytest.mark.parametrize("unload", [False, True])
def test_ephemeral_rate_limit_retry_needs_an_active_adapter(tmp_path, monkeypatch, unload):
    actions, app, _reg, _dirs = fixture(tmp_path)
    sent, waits = [], []

    async def flaky(**kwargs):
        sent.append(kwargs)
        if len(sent) == 1:
            raise RateLimited()

    async def sleep(seconds):
        waits.append(seconds)
        if unload:
            actions.unload()

    app.client.chat_postEphemeral = flaky
    monkeypatch.setattr(delivery.asyncio, "sleep", sleep)
    asyncio.run(actions._say("C_SYNTHETIC", "U_OPERATOR", "synthetic answer"))
    assert waits == [2.0] and len(sent) == (1 if unload else 2)
