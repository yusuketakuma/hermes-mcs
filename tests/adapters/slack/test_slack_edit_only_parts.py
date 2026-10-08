"""Slack exact reply and file-caption edits hold unverified targets without POSTs."""
import asyncio
import copy
import pytest
from test_mcs_slack import isolated_slack_ledger as led, _rewrite_world, _updates, ROOT_TS

__all__ = ["led"]


def setup(led):
    w, spec, replies = _rewrite_world(led)
    spec["delivery"]["thread_id"] = ROOT_TS
    target = replies[0]
    part = {"part_id": "body:0001", "kind": "body_part", "prior_remote_id": target["ts"], "edit_only": True}
    return w, spec, replies, target, part


def run(w, spec, part, history=None):
    ctx = {"thread_id": ROOT_TS, "history": history, "consumed": set()}
    return asyncio.run(w.worker._body_part(spec, "placeholder", ctx, part))


def test_exact_slack_edit_never_binds_identical_other_reply(led):
    w, spec, replies, target, part = setup(led)
    other = {**target, "ts": "1790000000.000099", "text": "placeholder"}
    replies.append(other)
    posted = len(w.client.thread_posts)
    out = run(w, spec, part)
    assert out == {"result": "delivered", "remote_id": target["ts"]}
    assert _updates(w)[-1]["ts"] == target["ts"]
    assert len(w.client.thread_posts) == posted and other["text"] == "placeholder"


@pytest.mark.parametrize("fault", ["missing", "foreign", "wrong_thread", "scope", "timeout"])
def test_slack_unverified_target_is_held_without_post(led, fault):
    w, spec, replies, target, part = setup(led)
    if fault == "missing":
        replies.clear()
    elif fault == "foreign":
        target.pop("bot_id", None)
        target.pop("app_id", None)
        target["user"] = "U_FOREIGN"
    elif fault == "wrong_thread":
        target["thread_ts"] = "1790000000.000999"
    elif fault == "scope":
        spec["delivery"]["channel_id"] = "C_OTHER"
    else:
        async def timeout(**kwargs):
            raise TimeoutError()
        w.client.chat_update = timeout
    posted = len(w.client.thread_posts)
    assert run(w, spec, part)["result"] == "unknown"
    assert len(w.client.thread_posts) == posted


def test_slack_same_owned_file_edits_message_caption_and_returns_file_id(led):
    w, spec, replies, target, _ = setup(led)
    target["files"] = [{"id": "F_SYN", "name": "synthetic.pdf", "size": 10,
                        "mode": "hosted", "is_external": False, "editable": False}]
    part = {"kind": "attachment_part", "part_id": "attach:0001", "prior_remote_id": "F_SYN",
            "edit_only": True, "name": "synthetic.pdf", "bytes": 10, "sha256": "a" * 64, "caption": "metadata only"}
    posted = len(w.client.thread_posts)
    out = asyncio.run(w.worker._attachment_part(spec, part, {"thread_id": ROOT_TS, "history": copy.deepcopy(replies), "consumed": set()}))
    assert out == {"result": "delivered", "remote_id": "F_SYN"}
    assert _updates(w)[-1]["ts"] == target["ts"] and _updates(w)[-1]["text"] == "metadata only"
    assert len(w.client.thread_posts) == posted
    target["files"][0]["editable"] = True
    out = asyncio.run(w.worker._attachment_part(spec, part, {"thread_id": ROOT_TS, "history": copy.deepcopy(replies), "consumed": set()}))
    assert out["result"] == "unknown"


def test_slack_file_id_in_two_owned_replies_is_unverified_without_writes(led):
    w, spec, replies, target, _ = setup(led)
    file = {"id": "F_SYN", "name": "synthetic.pdf", "size": 10,
            "mode": "hosted", "is_external": False, "editable": False}
    target["files"] = [file]
    replies.append({**target, "ts": "1790000000.000099", "files": [dict(file)]})
    part = {"kind": "attachment_part", "part_id": "attach:0001", "prior_remote_id": "F_SYN", "edit_only": True,
            "name": "synthetic.pdf", "bytes": 10, "sha256": "a" * 64, "caption": "metadata"}
    before = len(_updates(w))
    out = asyncio.run(w.worker._attachment_part(spec, part, {"thread_id": ROOT_TS, "history": replies, "consumed": set()}))
    assert out["result"] == "unknown" and len(_updates(w)) == before


@pytest.mark.parametrize("reply", ["gone", "echo", "channel", "403", "429"])
def test_slack_edit_response_preserves_gone_rejection_and_unknown_outcomes(led, reply, monkeypatch):
    w, spec, _, target, part = setup(led)
    class Response(dict):
        status_code = int(reply) if reply.isdigit() else 200
    class Rejected(Exception):
        response = Response(ok=False, error="ratelimited" if reply == "429" else "cant_update_message")
    async def update(**kwargs):
        if reply.isdigit():
            raise Rejected()
        if reply == "gone":
            return {"ok": False, "error": "message_not_found"}
        return {"ok": True, "ts": "1790000000.000999" if reply == "echo" else target["ts"],
                "channel": "C_FOREIGN" if reply == "channel" else spec["delivery"]["channel_id"]}
    w.client.chat_update = update
    from adapters.slack import delivery
    monkeypatch.setattr(delivery, "_rate_wait", lambda exc: None)
    posted = len(w.client.thread_posts)
    out = run(w, spec, part)
    assert out["result"] == ("delivered" if reply == "gone" else "not_sent" if reply.isdigit() else "unknown")
    assert len(w.client.thread_posts) == posted
