"""Slack exact reply and file-caption edits hold unverified targets without POSTs."""
import asyncio
import copy
from types import SimpleNamespace
import pytest
from adapters.slack.delivery import SlackCardAdapter
from slack_testkit import isolated_slack_ledger as led, _rewrite_world, _updates, FakeClient, ROOT_TS, SCOPE

__all__ = ["led"]
pytestmark = pytest.mark.thread_bodies


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


def user_shared_file(led):
    # Synthetic identity contract; this does not assert a live Slack response shape.
    w, spec, replies, target, _ = setup(led)
    target.pop("bot_id", None)
    target.update(user="U_BOT", subtype="file_share", thread_ts=ROOT_TS,
                  files=[{"id": "F_SYN", "name": "synthetic.pdf", "size": 10,
                          "user": "U_BOT", "mode": "hosted", "is_external": False, "editable": False}])
    part = {"kind": "attachment_part", "part_id": "attach:0001", "prior_remote_id": "F_SYN",
            "edit_only": True, "name": "synthetic.pdf", "bytes": 10, "sha256": "a" * 64,
            "caption": "synthetic metadata"}
    return w, spec, replies, target, part


def writes(w):
    return [(kind, kwargs) for kind, kwargs in w.client.calls
            if kind in ("create", "update", "delete", "files_upload_v2")]


def test_slack_user_authorship_requires_initial_bot_bind():
    client = FakeClient()
    sender = SlackCardAdapter(SimpleNamespace(client=client), team_id=SCOPE["team_id"],
                              application_id=SCOPE["application_id"],
                              channel_id=SCOPE["channel_id"], profile=SCOPE["profile"])
    identities = [{"bot_id": client.bot_id}, {"app_id": SCOPE["application_id"]}, {"user": "U_BOT"}]
    assert not any(sender.authored(message) for message in identities)
    assert asyncio.run(sender.bind())
    assert all(sender.authored(message) for message in identities)
    assert not sender.authored({})
    assert not sender.authored({"user": "U_FOREIGN"})
    assert not sender.authored({"bot_id": client.bot_id, "user": ""})
    assert [kind for kind, _ in client.calls] == ["auth_test"]


def test_slack_user_only_file_caption_edits_exact_shared_message(led):
    w, spec, _, target, part = user_shared_file(led)
    before = writes(w)
    ctx = {"thread_id": ROOT_TS, "history": None, "consumed": set()}
    out = asyncio.run(w.worker._attachment_part(spec, part, ctx))
    assert out == {"result": "delivered", "remote_id": "F_SYN"}
    assert writes(w)[len(before):] == [("update", _updates(w)[-1])]
    assert _updates(w)[-1]["ts"] == target["ts"]
    assert _updates(w)[-1]["text"] == part["caption"]
    assert ctx["consumed"] == {"F_SYN"}


def test_slack_user_only_shared_file_is_reused_without_upload(led):
    w, spec, _, _, part = user_shared_file(led)
    part.pop("edit_only")
    before = writes(w)
    ctx = {"thread_id": ROOT_TS, "history": None, "consumed": set()}
    out = asyncio.run(w.worker._attachment_part(spec, part, ctx))
    assert out == {"result": "delivered", "remote_id": "F_SYN"}
    assert writes(w) == before
    assert ctx["consumed"] == {"F_SYN"}


def test_slack_unverified_prior_upload_is_held_without_upload_fallback(led):
    w, spec, _, target, part = user_shared_file(led)
    part.pop("edit_only")
    target["user"] = "U_FOREIGN"
    before = writes(w)
    out = asyncio.run(w.worker._attachment_part(
        spec, part, {"thread_id": ROOT_TS, "history": None, "consumed": set()}))
    assert out == {"result": "unknown", "error_code": "file_target_unverified"}
    assert writes(w) == before


@pytest.mark.parametrize("identity", [
    {}, {"user": "U_FOREIGN"},
    {"user": "U_FOREIGN", "bot_id": "B_SYNTHETIC"},
    {"user": "U_FOREIGN", "app_id": "A_SYNTHETIC"},
    {"user": "U_BOT", "bot_id": "B_FOREIGN"},
    {"user": "U_BOT", "app_id": "A_FOREIGN"},
    {"bot_id": "B_SYNTHETIC", "app_id": "A_FOREIGN"},
    {"bot_id": "B_FOREIGN", "app_id": "A_SYNTHETIC"},
])
def test_slack_owned_upload_cannot_authorize_foreign_or_conflicting_share(led, identity):
    w, spec, _, target, part = user_shared_file(led)
    target.pop("user")
    target.update(identity)
    before = writes(w)
    assert not w.sender.authored(target)
    out = asyncio.run(w.worker._attachment_part(
        spec, part, {"thread_id": ROOT_TS, "history": None, "consumed": set()}))
    assert out == {"result": "unknown", "error_code": "edit_target_unverified"}
    assert writes(w) == before


@pytest.mark.parametrize("failure", ["timeout", "foreign_team", "rejected", "cancel"])
def test_slack_failed_rebind_cannot_reuse_any_previous_author_identity(led, failure):
    w, spec, _, _, part = user_shared_file(led)

    async def auth_test():
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "timeout":
            raise TimeoutError("synthetic auth failure")
        return {"ok": failure != "rejected", "bot_id": w.client.bot_id, "user_id": "U_BOT",
                "team_id": "T_FOREIGN" if failure == "foreign_team" else SCOPE["team_id"]}

    w.client.auth_test = auth_test
    before = writes(w)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(w.sender.bind())
    else:
        assert not asyncio.run(w.sender.bind())
    assert not any(w.sender.authored(message) for message in (
        {"bot_id": w.client.bot_id}, {"app_id": SCOPE["application_id"]}, {"user": "U_BOT"}))
    out = asyncio.run(w.worker._attachment_part(
        spec, part, {"thread_id": ROOT_TS, "history": None, "consumed": set()}))
    assert out == {"result": "unknown", "error_code": "edit_target_unverified"}
    assert writes(w) == before


@pytest.mark.parametrize("field", ["bot_id", "user_id"])
@pytest.mark.parametrize("invalid", ["missing", None, "", 17])
def test_slack_incomplete_bot_identity_cannot_authorize_user_only_share(led, field, invalid):
    w, spec, _, target, part = user_shared_file(led)
    identity = {"ok": True, "team_id": SCOPE["team_id"], "bot_id": w.client.bot_id, "user_id": "U_BOT"}
    if invalid == "missing":
        identity.pop(field)
    else:
        identity[field] = invalid

    async def auth_test():
        return identity

    w.client.auth_test = auth_test
    before = writes(w)
    asyncio.run(w.sender.bind())
    assert not w.sender.authored(target)
    out = asyncio.run(w.worker._attachment_part(
        spec, part, {"thread_id": ROOT_TS, "history": None, "consumed": set()}))
    assert out == {"result": "unknown", "error_code": "edit_target_unverified"}
    assert writes(w) == before


@pytest.mark.parametrize("fault", ["id", "name", "size", "editable", "is_external", "mode", "sha256", "thread", "duplicate"])
def test_slack_user_identity_preserves_file_and_thread_preflight_checks(led, fault):
    w, spec, replies, target, part = user_shared_file(led)
    logs = []
    w.worker._log = lambda event, **fields: logs.append((event, fields))
    if fault == "thread":
        target["thread_ts"] = "1790000000.000999"
    elif fault == "duplicate":
        replies.append({**target, "ts": "1790000000.000099"})
    else:
        target["files"][0][fault] = {"id": "F_OTHER", "name": "other.pdf", "size": 11,
                                      "editable": True, "is_external": True, "mode": "external",
                                      "sha256": "b" * 64}[fault]
    before = writes(w)
    out = asyncio.run(w.worker._attachment_part(
        spec, part, {"thread_id": ROOT_TS, "history": None, "consumed": set()}))
    assert out == {"result": "unknown", "error_code": "edit_target_unverified"}
    assert writes(w) == before
    reason = {"id": "edit_reference_missing", "thread": "edit_thread_mismatch",
              "duplicate": "edit_target_ambiguous"}.get(fault, "edit_file_unverified")
    assert logs == [("edit_preflight_held", {"reason": reason, "rows": 2 if fault == "duplicate" else 0})]
