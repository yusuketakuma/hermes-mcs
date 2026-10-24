"""Synthetic Slack wire, remote size and thread identity boundaries."""
import asyncio
from html import escape
import json
from types import SimpleNamespace

import pytest

from adapters.slack import delivery
from slack_testkit import FakeClient, ROOT_TS, SCOPE


@pytest.fixture
def world():
    client = FakeClient()
    sender = delivery.SlackCardAdapter(SimpleNamespace(client=client),
        team_id=SCOPE["team_id"], application_id=SCOPE["application_id"],
        channel_id=SCOPE["channel_id"], profile=SCOPE["profile"])
    assert asyncio.run(sender.bind())
    worker = object.__new__(delivery.DeliveryWorker)
    worker._sender, worker._settings = sender, SCOPE
    worker._log = lambda *args, **kwargs: None
    return SimpleNamespace(client=client, worker=worker)


def _spec(part, *, update=False):
    return {"op": "update" if update else "create", "card_key": "synthetic|reply:202",
            "delivery": {**SCOPE, "thread_id": ROOT_TS},
            "parts": {"post_source_id": 202, "manifest": [part]}}


@pytest.mark.parametrize("mode", ["create", "edit", "legacy"])
@pytest.mark.parametrize("text", ["あ" * 1332 + "ab", "&" * 799 + "abc", "😀" * 999 + "ab"])
def test_3998_byte_body_preserves_wire_text_for_post_and_updates(world, mode, text):
    prior = "1790000000.000020"
    part = {"kind": "body_part", "name": "m:202#1"}
    spec = _spec(part, update=mode != "create")
    if mode != "create":
        part["prior_remote_id"] = prior
        if mode == "edit":
            part["edit_only"] = True
        else:
            spec["card_key"] = "synthetic-legacy"
            spec["parts"].pop("post_source_id")
    context = {"thread_id": ROOT_TS, "consumed": set(), "history": [
        {"ts": prior, "thread_ts": ROOT_TS, "bot_id": world.client.bot_id, "text": "old"}]}
    out = asyncio.run(world.worker._body_part(spec, text, context, part))
    assert out["result"] == "delivered"
    writes = [(op, kw) for op, kw in world.client.calls if op in ("create", "update")]
    op, payload = writes[0]
    assert len(writes) == 1 and op == ("create" if mode == "create" else "update")
    assert "blocks" not in payload
    assert payload["text"] == escape(text, quote=False)
    assert len(payload["text"].encode("utf-8")) == 3998
    assert json.loads(json.dumps(payload))["text"] == payload["text"]


@pytest.mark.parametrize("failure", ["msg_too_long", "timeout"])
def test_under_budget_failure_is_never_split_or_resent(world, failure):
    attempts = []

    async def rejected(**kwargs):
        attempts.append(kwargs)
        if failure == "timeout":
            raise TimeoutError("synthetic uncertain response")
        exc = RuntimeError("synthetic rejection")
        exc.response = SimpleNamespace(status_code=200, data={"ok": False, "error": failure})
        raise exc

    world.client.chat_postMessage = rejected
    part = {"kind": "body_part", "name": "m:202#1"}
    out = asyncio.run(world.worker._body_part(_spec(part), "あ" * 1332 + "ab",
        {"thread_id": ROOT_TS, "history": None, "consumed": set()}, part))
    assert out == {"result": "not_sent" if failure == "msg_too_long" else "unknown",
                   "error_code": "msg_too_long" if failure == "msg_too_long" else "timeouterror"}
    assert len(attempts) == 1
    assert len(attempts[0]["text"].encode("utf-8")) == 3998


def _file_part(size=10):
    return {"kind": "attachment_part", "name": "synthetic.pdf", "bytes": size,
            "sha256": "a" * 64, "prior_remote_id": "F_SYN", "edit_only": True,
            "caption": "synthetic caption"}


def _file_message(client, *, size=10, thread=ROOT_TS):
    return {"ts": "1790000000.000020", "thread_ts": thread, "bot_id": client.bot_id,
            "files": [{"id": "F_SYN", "name": "synthetic.pdf", "size": size,
                       "mode": "hosted", "is_external": False, "editable": False}]}


@pytest.mark.parametrize("remote_size,sealed_size", [(10.0, 10), (True, 1), ("10", 10), (None, 10)])
def test_non_integer_remote_size_cannot_bind_or_authorize_caption_edit(world, remote_size, sealed_size):
    part = _file_part(sealed_size)
    message = _file_message(world.client, size=remote_size)
    context = {"thread_id": ROOT_TS, "history": [message], "consumed": set()}
    assert asyncio.run(world.worker._file_remote_match(ROOT_TS, part, context, prior_only=True)) is None
    out = asyncio.run(world.worker._attachment_part(_spec(part, update=True), part, context))
    assert out == {"result": "unknown", "error_code": "edit_target_unverified"}
    assert context["consumed"] == set()
    assert [op for op, _ in world.client.calls] == ["auth_test"]


def test_cached_other_thread_cannot_bind_body_file_or_rewrite_prior(world):
    message = _file_message(world.client, thread="1790000000.000999")
    message["text"] = "synthetic body"
    context = {"thread_id": ROOT_TS, "history": [message], "consumed": set()}
    assert asyncio.run(world.worker._remote_match(ROOT_TS, message["text"], context)) is None
    assert asyncio.run(world.worker._file_remote_match(ROOT_TS, _file_part(), context, prior_only=True)) is None
    with pytest.raises(RuntimeError, match="reply_history_unverified"):
        asyncio.run(world.worker._rewrite_prior(ROOT_TS, message["ts"], "changed", context))
    assert context["consumed"] == set()
    assert [op for op, _ in world.client.calls] == ["auth_test"]


@pytest.mark.parametrize("size", [10, 4267])
def test_exact_remote_size_guard_is_preserved(world, size):
    part = _file_part()
    context = {"thread_id": ROOT_TS, "history": [_file_message(world.client, size=size)], "consumed": set()}
    out = asyncio.run(world.worker._attachment_part(_spec(part, update=True), part, context))
    assert out["result"] == ("delivered" if size == 10 else "unknown")
    assert [op for op, _ in world.client.calls] == (["auth_test", "update"] if size == 10 else ["auth_test"])
