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
from slack_actions_testkit import fixture

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


@pytest.mark.parametrize("target", ["card", pytest.param("body", marks=pytest.mark.thread_bodies)])
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
        # a post is two thread chunks (summary/stamps header, source text);
        # a repeated 429 is waited out once per chunk
        parts = 2 if target == "body" else 1
        assert waits == [2.0] * (parts if mode == "repeat429" else 1)
        results = [r for rows in journal.scan(w.dirs["state"]).values() for r in rows
                   if r.get("phase") == "result"
                   and (r.get("kind") == "body_part") == (target == "body")]
        if mode == "none":
            assert len(attempts) == parts + 1 and results[-1]["result"] == "delivered"
        else:
            # a lost authority or a second 429 never sends again; the 429 stands
            assert len(attempts) == (2 * parts if mode == "repeat429" else 1)
            assert (results[-1]["result"], results[-1]["error_code"]) == ("not_sent", "ratelimited")
        assert ("retry_skipped" in logs) == (mode in ("off", "epoch", "restore"))
        if target == "body":
            assert len(w.client.thread_posts) == (parts if mode == "none" else 0)
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


def test_action_origin_uses_construction_pinned_scope(tmp_path, monkeypatch):
    from adapters.slack import actions as module
    actions, _app, _reg, _dirs = fixture(tmp_path)
    original = dict(actions._origin_scope)
    observed = []
    def inspect(body, action, **scope):
        observed.append(scope)
        return None
    monkeypatch.setattr(module, "parse_action_origin", inspect)
    actions._settings["team_id"] = "T_FOREIGN"
    actions._settings["channel_id"] = "C_FOREIGN"
    actions._settings["allowed_user_ids"].add("U_FOREIGN")
    async def ack():
        pass
    asyncio.run(actions._action(ack, {"user": {}}, {}))
    assert observed == [original]
    assert "U_FOREIGN" not in observed[0]["allowed_user_ids"]


@pytest.mark.parametrize("allowed", [False, True])
def test_explicit_native_retry_authority_is_per_call(monkeypatch, allowed):
    attempts, checks = [], []
    async def post(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RateLimited()
        return "sent"
    async def permit():
        checks.append("explicit")
        return allowed
    async def ambient():
        pytest.fail("explicit authority must not borrow another send's gate")
    async def sleep(_seconds):
        pass
    monkeypatch.setattr(delivery.asyncio, "sleep", sleep)
    async def run():
        token = delivery.RETRY_ALLOWED.set(ambient)
        try:
            if allowed:
                assert await delivery.call_native(post, retry_allowed=permit, text="synthetic") == "sent"
            else:
                with pytest.raises(RateLimited):
                    await delivery.call_native(post, retry_allowed=permit, text="synthetic")
            assert delivery.RETRY_ALLOWED.get() is ambient
        finally:
            delivery.RETRY_ALLOWED.reset(token)
    asyncio.run(run())
    assert checks == ["explicit"] and len(attempts) == (2 if allowed else 1)
