"""Slack native card calls and transport-specific journaled delivery."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import copy
from html import escape
import re
import time
from collections.abc import Mapping

from adapters.common.paths import read_verified_attachment
from adapters.common.spec import token_map
from adapters.common.worker import DeliveryWorker as _BaseWorker

from .actions import origin as parse_action_origin
from .cards import mention_ids, render, validate
from .paths import notify_dirs

_TS = re.compile(r"^[0-9]+\.[0-9]{6}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# A failed users.info (missing scope, 429, network) is retried after this
# long — same idea as registry.CAPABILITY_NEG_S; successes never expire.
NAME_NEG_S = 900
NAME_CACHE_MAX = 1024


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


# chat.update rejections that prove the prior reply cannot be edited
_REPOST_ON_UPDATE = frozenset({"message_not_found", "cant_update_message",
                               "edit_window_closed"})


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
        self._names: OrderedDict = OrderedDict()

    async def display_name(self, uid):
        """The member's Slack display (or real) name via users.info —
        cached per worker, None when unknown. A missing users:read
        scope or any API error is cached as unknown for NAME_NEG_S
        (then re-asked), never raised."""
        hit = self._names.get(uid)
        if hit is not None and (hit[1] is None
                                or time.monotonic() < hit[1]):
            self._names.move_to_end(uid)
            return hit[0]
        name = None
        try:
            user = _payload(await self._client.users_info(user=uid)) \
                .get("user") or {}
            prof = user.get("profile") or {}
            for got in (prof.get("display_name"), prof.get("real_name"),
                        user.get("real_name")):
                if isinstance(got, str) and got.strip():
                    name = " ".join(got.split())[:80]
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        self._names[uid] = (name, None if name
                            else time.monotonic() + NAME_NEG_S)
        self._names.move_to_end(uid)
        if len(self._names) > NAME_CACHE_MAX:
            self._names.popitem(last=False)
        return name

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

    def authored(self, message) -> bool:
        """True only for a message this bound bot/app posted — the
        authorship gate before any reply or file may bind a part. An
        unbound adapter's empty bot_id never matches."""
        return bool((self._bot_id and message.get("bot_id") == self._bot_id)
                    or message.get("app_id") == self._application_id)

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
            uids = sorted(mention_ids(spec))[:16]
        except (AttributeError, TypeError):
            uids = []
        names = {u: await self.display_name(u) for u in uids}
        try:
            text, blocks = render(spec, names)
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
                    text=text, blocks=blocks, parse="none", link_names=False)
            else:
                response = await sender.chat_postMessage(
                    channel=self._channel_id, text=text, blocks=blocks,
                    parse="none", link_names=False,
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
    _notify_dirs = staticmethod(notify_dirs)

    def __init__(self, *, sender, settings, root, reg, worker_id, log):
        super().__init__(bot=sender, settings=settings, root=root,
                         reg=reg, worker_id=worker_id, log=log)
        self._sender = sender

    def scope(self):
        return {key: self._settings[key] for key in
                ("transport", "profile", "application_id",
                 "team_id", "channel_id")}

    def _ours(self, delivery):
        return all(delivery.get(key) == value
                   for key, value in self.scope().items())

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
            return await self._body_part(spec, text, ctx, part)
        if part["kind"] == "attachment_part":
            return await self._attachment_part(spec, part, ctx)
        return {"result": "not_sent", "error_code": "unsupported_part"}

    async def _body_part(self, spec: dict, text: str, ctx: dict,
                         part: dict | None = None) -> dict:
        """Post one sealed chunk inside the card's thread. The send only
        ever targets thread_ts=the bound root ts — a body part can never
        fall back to a top-level channel message."""
        thread_ts = ctx.get("thread_id")
        if not thread_ts:
            return {"result": "not_sent",
                    "error_code": "thread_root_missing"}
        # Slack parses explicit mentions even without automatic name linking.
        # Use the same escaped wire text for both sends and remote verification.
        # mrkdwn=False: user-authored *bold*/_x_/~y~ stay literal text.
        text = escape(text, quote=False)
        if spec["op"] == "update" or spec["delivery"].get("thread_id"):
            # writing into an existing thread — an identical reply
            # authored by this bot binds the part to its real ts
            # instead of posting a duplicate (remote-verified)
            mid = await self._remote_match(thread_ts, text, ctx)
            if mid is not None:
                return {"result": "delivered", "remote_id": mid}
            # the chunk's text changed since it was first posted (the
            # extraction arrived, a reply joined): rewrite that reply so
            # the thread keeps one post per chunk
            edited = await self._rewrite_prior(
                thread_ts, (part or {}).get("prior_remote_id"), text, ctx)
            if edited is not None:
                return edited
        sender = self._sender.single_attempt()
        if sender is None:
            return {"result": "not_sent",
                    "error_code": "retry_policy_unknown"}
        try:
            response = await sender.chat_postMessage(
                channel=self._settings["channel_id"],
                thread_ts=thread_ts, text=text, link_names=False,
                mrkdwn=False, unfurl_links=False, unfurl_media=False)
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

    async def _rewrite_prior(self, thread_ts: str, prior, text: str,
                             ctx: dict):
        """chat.update this bot's earlier reply of the same chunk and
        return the delivered outcome, or None so the caller posts fresh
        exactly as before (no prior, target gone, foreign or already
        bound, or an update Slack rejected outright). Only a reply
        provably authored by this bot/app in this thread is rewritten.
        An update is idempotent, so it needs no single-shot guard; a
        crash after it is re-bound by ``_remote_match``. Any other
        failure is unknown and is never followed by a second post."""
        if not isinstance(prior, str) or not _TS.fullmatch(prior) \
                or prior == thread_ts or prior in ctx["consumed"]:
            return None
        if ctx.get("history") is None:
            ctx["history"] = await self._replies(thread_ts)
        target = next((m for m in ctx["history"]
                       if m.get("ts") == prior), None)
        if target is None:
            return None
        sender = self._sender.single_attempt()
        if not self._sender.authored(target) or sender is None:
            return None
        try:
            response = await sender.chat_update(
                channel=self._settings["channel_id"], ts=prior, text=text,
                link_names=False, mrkdwn=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            out = _failed(exc, "body_part", thread_ts)
            # only a target that can no longer be edited is re-posted;
            # a rate limit or any other rejection stays not_sent for the
            # next render — posting afresh would leave a duplicate reply
            if out["result"] == "not_sent" \
                    and out["error_code"] in _REPOST_ON_UPDATE:
                return None
            return out
        data = _payload(response)
        if data.get("ok") is not True or data.get("ts") != prior:
            return {"result": "unknown", "error_code": "bad_slack_response"}
        ctx["consumed"].add(prior)
        return {"result": "delivered", "remote_id": prior}

    async def _attachment_part(self, spec: dict, part: dict,
                               ctx: dict) -> dict:
        """Upload one sealed file inside the card's thread. The upload
        only ever targets thread_ts=the bound root ts on the bound
        workspace client — no top-level fallback, no second bot. When
        the SDK lacks the upload edge the part stays explicitly
        not_sent(sdk_capability_missing): visible incompleteness,
        never a faked completion."""
        if part.get("unavailable"):
            # no file to send — its caption ("📎 name — 取得失敗") is the
            # visible line, posted and deduped like a body chunk
            if not part.get("caption"):
                return {"result": "not_sent",
                        "error_code": "attachment_unavailable"}
            if part.get("prior_remote_id"):
                # already posted — never a second caption post
                return {"result": "delivered",
                        "remote_id": str(part["prior_remote_id"])}
            return await self._body_part(spec, part["caption"], ctx, part)
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
        extra = {}
        if part.get("caption"):
            # the file's visible line: 📎 name — patient/post
            extra["initial_comment"] = escape(part["caption"], quote=False)
        try:
            response = await upload(
                channel=self._settings["channel_id"],
                thread_ts=thread_ts, file=blob,
                filename=name, title=name, **extra)
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
            if not self._sender.authored(m):
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
                # Slack does not promise a file hash in replies. A sealed
                # earlier receipt can instead prove these same bytes, but
                # only for this bot's still-present immutable hosted upload.
                proven_prior = (rsha is None and fid == part.get("prior_remote_id")
                                and f.get("mode") == "hosted"
                                and f.get("is_external") is False
                                and f.get("editable") is False)
                if not want_sha or (rsha != want_sha and not proven_prior):
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
            if not self._sender.authored(m):
                continue                # not ours — never binds a part
            ctx["consumed"].add(mid)
            return mid
        return None

    async def _replies(self, thread_ts: str):
        """One bounded conversations.replies read through the send-safe
        client. Unverified history must not authorize a fresh post: the
        journaled part stays unknown instead of duplicating a reply."""
        sender = self._sender.single_attempt()
        if sender is None:
            raise RuntimeError("reply_history_unverified")
        data = _payload(await sender.conversations_replies(
            channel=self._settings["channel_id"], ts=thread_ts,
            limit=200))
        msgs = data.get("messages")
        if data.get("ok") is not True or not isinstance(msgs, list) \
                or not all(isinstance(m, dict) for m in msgs):
            raise RuntimeError("reply_history_unverified")
        metadata = data.get("response_metadata", {})
        if not isinstance(metadata, dict):
            raise RuntimeError("reply_history_unverified")
        if data.get("has_more") or metadata.get("next_cursor"):
            raise RuntimeError("reply_history_incomplete")
        return msgs
