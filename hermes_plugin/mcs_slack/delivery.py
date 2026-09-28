"""Slack native card calls and transport-specific journaled delivery."""
from __future__ import annotations

import asyncio
import copy
import re
from collections.abc import Mapping

from ..mcs_delivery.paths import read_verified_attachment
from ..mcs_delivery.spec import token_map
from ..mcs_delivery.worker import DeliveryWorker as _BaseWorker

from .actions import origin as parse_action_origin
from .cards import render, validate
from .paths import ensure_dirs, notify_dirs

_TS = re.compile(r"^[0-9]+\.[0-9]{6}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _upload_file_id(data) -> str | None:
    """The uploaded file's remote identity — ok:true only proves the
    call returned, not that our file landed with a verifiable id."""
    for key in ("files", "file"):
        v = data.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        if isinstance(v, dict):
            fid = v.get("id")
            if isinstance(fid, str) and fid:
                return fid
    fid = data.get("id")
    return fid if isinstance(fid, str) and fid else None


def _payload(response):
    if isinstance(response, Mapping):
        return response
    data = getattr(response, "data", None)
    return data if isinstance(data, Mapping) else {}


def single_attempt(client):
    # Snapshot the native SDK client without a new token or HTTP session.
    # Its default connection retry can duplicate a post whose response was
    # lost; changing retry_handlers on the shared client races Hermes sends.
    handlers = getattr(client, "retry_handlers", None)
    if not isinstance(handlers, list | tuple):
        return None
    sender = copy.copy(client)
    sender.retry_handlers = []
    return sender


def _failed(exc, op, message_id):
    response = getattr(exc, "response", None)
    data = _payload(response)
    status = getattr(response, "status_code", None)
    code = data.get("error")
    code = code if isinstance(code, str) and _CODE.fullmatch(code) \
        else type(exc).__name__.lower()
    if data.get("error") == "message_not_found" and op == "revoke":
        return {"result": "delivered", "message_id": message_id}
    if (isinstance(status, int) and 400 <= status < 500) \
            or (data.get("ok") is False
                and isinstance(status, int) and status < 500):
        return {"result": "not_sent", "error_code": code}
    return {"result": "unknown", "error_code": code}


class SlackCardAdapter:
    """An inert native-client adapter until explicitly bound and called."""

    def __init__(self, app, *, team_id, application_id, channel_id,
                 profile, allowed_user_ids, native_adapter=None):
        # Hermes keeps one native client per workspace; app.client is only
        # the first workspace's client when several bot tokens are connected.
        self._client = (native_adapter._get_client(channel_id, team_id=team_id)
                        if native_adapter is not None else app.client)
        self._team_id = team_id
        self._application_id = application_id
        self._channel_id = channel_id
        self._profile = profile
        self._allowed_user_ids = frozenset(allowed_user_ids)
        self._bound = False
        self._bot_id = ""

    async def bind(self):
        """Verify the native client belongs to the intended workspace."""
        try:
            identity = _payload(await self._client.auth_test())
        except Exception:
            return False
        self._bound = identity.get("ok") is True \
            and identity.get("team_id") == self._team_id
        if self._bound:
            # authorship identity for thread-reply dedupe — a foreign
            # message with identical text can never bind a part
            bot = identity.get("bot_id")
            self._bot_id = bot if isinstance(bot, str) else ""
        return self._bound

    def single_attempt(self):
        """Send-safe snapshot of the bound client — shared by the
        delivery worker and the ephemeral-reply path in actions."""
        return single_attempt(self._client)

    def _owns(self, delivery):
        return (delivery.get("transport") == "slack"
                and delivery.get("team_id") == self._team_id
                and delivery.get("application_id") == self._application_id
                and delivery.get("channel_id") == self._channel_id
                and delivery.get("profile") == self._profile)

    async def perform(self, spec):
        """One native API attempt, returning a factual transport outcome."""
        if not self._bound:
            return {"result": "not_sent", "error_code": "workspace_unverified"}
        delivery = spec.get("delivery") if isinstance(spec, dict) else None
        if not isinstance(delivery, dict) or not self._owns(delivery):
            return {"result": "not_sent", "error_code": "scope_mismatch"}
        try:
            text, blocks = render(spec)
        except ValueError:
            return {"result": "not_sent", "error_code": "bad_render"}
        sender = single_attempt(self._client)
        if sender is None:
            return {"result": "not_sent", "error_code": "retry_policy_unknown"}
        op = spec["op"]
        message_id = delivery.get("message_id")
        try:
            if op == "revoke":
                response = await sender.chat_delete(
                    channel=self._channel_id, ts=message_id)
            elif op == "update":
                response = await sender.chat_update(
                    channel=self._channel_id, ts=message_id,
                    text=text, blocks=blocks)
            else:
                response = await sender.chat_postMessage(
                    channel=self._channel_id, text=text, blocks=blocks,
                    unfurl_links=False, unfurl_media=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _failed(exc, op, message_id)
        data = _payload(response)
        if data.get("ok") is not True:
            return {"result": "unknown", "error_code": "bad_slack_response"}
        if op == "revoke":
            return {"result": "delivered", "message_id": message_id}
        ts = data.get("ts")
        if data.get("channel") != self._channel_id \
                or not isinstance(ts, str) or not _TS.fullmatch(ts):
            return {"result": "unknown", "error_code": "bad_slack_response"}
        if op == "update" and ts != message_id:
            return {"result": "unknown", "error_code": "message_mismatch"}
        return {"result": "delivered", "message_id": ts}

    def action_origin(self, body, action):
        return parse_action_origin(
            body, action, team_id=self._team_id,
            application_id=self._application_id,
            channel_id=self._channel_id, profile=self._profile,
            allowed_user_ids=self._allowed_user_ids)


class DeliveryWorker(_BaseWorker):
    """Reuse the durable claim/journal/receipt loop with Slack-only edges."""

    transport = "slack"

    def __init__(self, *, sender, settings, root, reg, worker_id, log):
        super().__init__(bot=sender, settings=settings, root=root,
                         reg=reg, worker_id=worker_id, log=log)
        self._sender = sender
        self._dirs = notify_dirs(root)

    def scope(self):
        return {key: self._settings[key] for key in
                ("transport", "profile", "application_id",
                 "team_id", "channel_id")}

    def _ours(self, delivery):
        return all(delivery.get(key) == value
                   for key, value in self.scope().items())

    def _ensure_dirs(self):
        ensure_dirs(self._root)

    def _validate_spec(self, spec):
        validate(spec)

    def _verify_grant(self, claim, result):
        return (super()._verify_grant(claim, result)
                and result.get("transport") == "slack"
                and result.get("team_id") == self._settings["team_id"])

    async def _perform(self, claim):
        outcome = await self._sender.perform(claim["spec"])
        message_id = outcome.get("message_id")
        if outcome.get("result") == "delivered" and message_id \
                and claim["spec"]["op"] != "revoke":
            self._reg.put_tokens({
                token: {**context, "message_id": message_id,
                        "team_id": self._settings["team_id"]}
                for token, context in token_map(claim["spec"]).items()
            })
            # The shared worker batches saves, but its receipt can be durable
            # before that batch exits. Keep Slack's posted-button pins durable.
            self._reg.save(immediate=True)
        return outcome

    async def _maybe_thread(self, claim, message_id):
        # Slack's posted ts is already the thread root; no second HTTP call.
        return None

    # -- durable render parts (T9) ----------------------------------------

    async def _perform_part(self, claim: dict, part: dict,
                            ctx: dict) -> dict:
        """One manifest part's wire call. The card's own ts is the
        thread root, so 'thread' needs no second post — the delivered
        card attempt already proved it exists. Body chunks go inside
        that root as ordered replies; attachments use the bound
        client upload edge with verified sealed bytes."""
        spec = claim["spec"]
        if part["kind"] == "thread":
            mid = ctx.get("card_message_id")
            if not mid:
                return {"result": "not_sent",
                        "error_code": "thread_root_missing"}
            return {"result": "delivered", "remote_id": str(mid)}
        if part["kind"] == "body_part":
            i = int(part["part_id"].rsplit(":", 1)[1]) - 1
            chunks = spec["parts"].get("thread_body_parts") or []
            text = chunks[i] if 0 <= i < len(chunks) else None
            if text is None:
                return {"result": "not_sent",
                        "error_code": "body_part_missing"}
            return await self._body_part(spec, text, ctx)
        if part["kind"] == "attachment_part":
            return await self._attachment_part(spec, part, ctx)
        return {"result": "not_sent", "error_code": "unsupported_part"}

    async def _body_part(self, spec: dict, text: str, ctx: dict) -> dict:
        """Post one sealed chunk inside the card's thread. The send only
        ever targets thread_ts=the bound root ts — a body part can never
        fall back to a top-level channel message."""
        thread_ts = ctx.get("thread_id")
        if not thread_ts:
            return {"result": "not_sent",
                    "error_code": "thread_root_missing"}
        if spec["op"] == "update" or spec["delivery"].get("thread_id"):
            # writing into an existing thread — an identical reply
            # authored by this bot binds the part to its real ts
            # instead of posting a duplicate (remote-verified)
            mid = await self._remote_match(thread_ts, text, ctx)
            if mid is not None:
                return {"result": "delivered", "remote_id": mid}
        sender = self._sender.single_attempt()
        if sender is None:
            return {"result": "not_sent",
                    "error_code": "retry_policy_unknown"}
        try:
            response = await sender.chat_postMessage(
                channel=self._settings["channel_id"],
                thread_ts=thread_ts, text=text,
                unfurl_links=False, unfurl_media=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _failed(exc, "body_part", thread_ts)
        data = _payload(response)
        ts = data.get("ts")
        if data.get("ok") is not True \
                or data.get("channel") != self._settings["channel_id"] \
                or not isinstance(ts, str) or not _TS.fullmatch(ts):
            return {"result": "unknown",
                    "error_code": "bad_slack_response"}
        return {"result": "delivered", "remote_id": ts}

    async def _attachment_part(self, spec: dict, part: dict,
                               ctx: dict) -> dict:
        """Upload one sealed file inside the card's thread. The upload
        only ever targets thread_ts=the bound root ts on the bound
        workspace client — no top-level fallback, no second bot. When
        the SDK lacks the upload edge the part stays explicitly
        not_sent(sdk_capability_missing): visible incompleteness,
        never a faked completion."""
        thread_ts = ctx.get("thread_id")
        if not thread_ts:
            return {"result": "not_sent",
                    "error_code": "thread_root_missing"}
        sender = self._sender.single_attempt()
        if sender is None:
            return {"result": "not_sent",
                    "error_code": "retry_policy_unknown"}
        upload = getattr(sender, "files_upload_v2", None)
        if upload is None:
            return {"result": "not_sent",
                    "error_code": "sdk_capability_missing"}
        if spec["op"] == "update" or spec["delivery"].get("thread_id"):
            # an identical file already remote in this thread binds the
            # part to its real file id (remote-verified) — post-seal
            # corruption locally cannot fake a match either way
            rid = await self._file_remote_match(thread_ts, part, ctx)
            if rid is not None:
                return {"result": "delivered", "remote_id": rid}
        blob = await asyncio.to_thread(
            read_verified_attachment, part.get("path"), part)
        if blob is None:
            return {"result": "not_sent",
                    "error_code": "attachment_mismatch"}
        name = part.get("name") or "file"
        try:
            response = await upload(
                channel=self._settings["channel_id"],
                thread_ts=thread_ts, file=blob,
                filename=name, title=name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _failed(exc, "attachment_part", thread_ts)
        data = _payload(response)
        fid = _upload_file_id(data)
        if data.get("ok") is not True or not fid:
            return {"result": "unknown",
                    "error_code": "bad_slack_response"}
        return {"result": "delivered", "remote_id": fid}

    async def _file_remote_match(self, thread_ts: str, part: dict,
                                 ctx: dict):
        """Bounded replies scan for a bot/app-authored message already
        carrying this file — the remote entry must match the sealed
        name, size and sha256, then the
        part binds that file's real id. A foreign user's file with
        identical metadata can never satisfy a part. Each file id
        binds once so identical uploads dedupe one-for-one."""
        if ctx.get("history") is None:
            ctx["history"] = await self._replies(thread_ts)
        name = part.get("name") or "file"
        want_sha = part.get("sha256")
        for m in ctx["history"]:
            mid = m.get("ts")
            if not isinstance(mid, str) or mid == thread_ts:
                continue
            ours = (self._sender._bot_id
                    and m.get("bot_id") == self._sender._bot_id) \
                or m.get("app_id") == self._settings["application_id"]
            if not ours:
                continue                # not ours — never binds a part
            for f in m.get("files") or []:
                if not isinstance(f, dict):
                    continue
                fid = f.get("id")
                if not isinstance(fid, str) or not fid \
                        or fid in ctx["consumed"]:
                    continue
                if f.get("name") != name:
                    continue
                if part.get("bytes") is not None \
                        and f.get("size") != part["bytes"]:
                    continue
                rsha = f.get("sha256")
                if not want_sha or rsha != want_sha:
                    continue
                ctx["consumed"].add(fid)
                return fid
        return None

    async def _remote_match(self, thread_ts: str, text: str, ctx: dict):
        """Bounded replies scan — a content match binds the part to the
        real reply ts (remote-verified delivery); a miss means send.
        Only replies provably authored by this bot/app may bind: a
        foreign message with identical text is unrelated and can never
        impersonate a part. Each ts binds at most once, so identical
        chunks dedupe one-for-one instead of collapsing."""
        if ctx.get("history") is None:
            ctx["history"] = await self._replies(thread_ts)
        for m in ctx["history"]:
            mid = m.get("ts")
            if not isinstance(mid, str) or mid == thread_ts \
                    or mid in ctx["consumed"] or m.get("text") != text:
                continue
            ours = (self._sender._bot_id
                    and m.get("bot_id") == self._sender._bot_id) \
                or m.get("app_id") == self._settings["application_id"]
            if not ours:
                continue                # not ours — never binds a part
            ctx["consumed"].add(mid)
            return mid
        return None

    async def _replies(self, thread_ts: str):
        """One bounded conversations.replies read through the send-safe
        client. A failed read yields no matches — the part then posts
        fresh rather than silently binding to unverifiable content."""
        sender = self._sender.single_attempt()
        if sender is None:
            return []
        try:
            data = _payload(await sender.conversations_replies(
                channel=self._settings["channel_id"], ts=thread_ts,
                limit=200))
        except Exception:
            return []
        msgs = data.get("messages")
        return [m for m in msgs if isinstance(m, dict)] \
            if isinstance(msgs, list) else []
