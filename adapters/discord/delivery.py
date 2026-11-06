"""Discord wire edge of the card delivery worker.

The durable claim -> transport_begin -> grant -> send ->
transport_receipt loop lives in ``adapters.common.worker``; this subclass
supplies only the Discord operations the neutral loop calls into:
channel/message sends and edits, revoke deletes, and the durable
per-part deliveries (companion thread, body chunks, attachments) a
sealed manifest plans.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import re
import io
from functools import partial

from adapters.common import paths, registry, worker
from adapters.common.announce import new_reply_announced
from adapters.common.spec import post_scoped, discord_file_name
from . import cards

# discord.py releases whose HTTPClient.request retry loop and private
# aiohttp session (``_HTTPClient__session``) the single-post guard was
# verified against; the version is read off the client's own
# user_agent (formatted from discord.__version__) — anything else fails
# closed rather than risk an unguarded re-POST
_VERIFIED_SDK = frozenset({"2.7.1"})
_SDK_VERSION = re.compile(r"discord\.py (\S+)\)")
_GUARD_MARK = "_mcs_single_post"
# HTTP status of each POST issued inside the current single_post() call
# (None until/unless a response arrives); None outside it, so Hermes'
# own sends (other tasks, default context) pass untouched
_POSTS: contextvars.ContextVar[list[int | None] | None] = contextvars.ContextVar(
    "mcs_discord_single_post", default=None)
_AUTHORITY = contextvars.ContextVar("mcs_discord_post_authority", default=None)

class RetryPolicyUnknown(RuntimeError):
    """The bound client is not a verified discord.py — a create POST
    could be re-sent by an unguarded SDK retry, so it is never made."""


class DiscordRetrySuppressed(RuntimeError):
    """The SDK tried a second POST inside one single_post() call; the
    first may have committed, so the outcome is unknown, not a resend."""


class _StatusRecorder:
    """Response context of a guarded POST: stores its HTTP status in
    the call's POST log once the response arrives (None if it never
    did — reset, timeout), so only a definitive 429 lets a retry through."""

    def __init__(self, factory, posts, authority):
        self._factory, self._posts, self._authority = factory, posts, authority
        self._cm = None

    async def __aenter__(self):
        if self._posts and self._posts[-1] != 429:
            raise DiscordRetrySuppressed("discord.py retry of a create POST suppressed")
        if self._authority is not None:
            try:
                allowed = await self._authority(retry=bool(self._posts))
            except asyncio.CancelledError:
                raise
            except Exception:
                allowed = False
            if allowed is not True:
                raise worker.SendNotAdmitted(self._posts[-1] if self._posts else None)
        # The authority read can yield. Never let another POST in the same
        # invocation turn a possibly committed result into a retry right.
        if self._posts and self._posts[-1] != 429:
            raise DiscordRetrySuppressed("discord.py retry of a create POST suppressed")
        self._posts.append(None)
        index = len(self._posts) - 1
        self._cm = self._factory()
        resp = await self._cm.__aenter__()
        self._posts[index] = getattr(resp, "status", None)
        return resp

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)


def _guarded(request):
    @functools.wraps(request)
    def guarded(method, *args, **kwargs):
        posts = _POSTS.get()
        if posts is None or str(method).upper() != "POST":
            return request(method, *args, **kwargs)
        # a 429 is Discord refusing before acting — its re-POST cannot
        # duplicate; after 5xx, a reset or anything else the previous
        # POST may have committed
        if posts and posts[-1] != 429:
            raise DiscordRetrySuppressed(
                "discord.py retry of a create POST suppressed")
        return _StatusRecorder(lambda: request(method, *args, **kwargs),
                               posts, _AUTHORITY.get())
    setattr(guarded, _GUARD_MARK, True)
    return guarded


def single_post_ready(bot) -> bool:
    """Install the single-post guard on the bot's current aiohttp
    session (once per session object) if the SDK is a verified one."""
    http = getattr(bot, "http", None)
    m = _SDK_VERSION.search(str(getattr(http, "user_agent", "")))
    if m is None or m.group(1) not in _VERIFIED_SDK:
        return False
    session = getattr(http, "_HTTPClient__session", None)
    request = getattr(session, "request", None)
    if not session or not callable(request):
        return False                     # discord MISSING before login
    if not getattr(request, _GUARD_MARK, False):
        session.request = _guarded(request)
    return True


async def single_post(bot, factory, *, retry_allowed=None):
    """Await ``factory()`` allowing exactly one committable POST onto the
    wire — the SDK's in-call re-POST after a 429 goes out, after 5xx or
    ECONNRESET it raises DiscordRetrySuppressed before the session."""
    if not single_post_ready(bot):
        raise RetryPolicyUnknown("discord.py retry policy unverified")
    token = _POSTS.set([])
    authority_token = _AUTHORITY.set(retry_allowed)
    try:
        return await factory()
    finally:
        _AUTHORITY.reset(authority_token)
        _POSTS.reset(token)


# An already-gone message: a delete achieved the revoke goal, and an
# owned edit target needs no edit.
REVOKE_GONE_STATUS = frozenset({404, 410})


def _thread_parent_guild(thread) -> tuple[str, str]:
    """(parent channel id, guild id) of a fetched thread as text — compared
    against the sealed delivery so a post never lands in another scope."""
    parent = getattr(thread, "parent_id", getattr(getattr(thread, "parent", None), "id", None))
    guild = getattr(getattr(thread, "guild", None), "id", None)
    return str(parent), str(guild)


# Only an authorization-class answer judges the scope's thread
# capability; other 4xx verdicts are per-message, never per-scope.
CAPABILITY_REJECT = frozenset({401, 403})


class _FilesUnverified(RuntimeError):
    """A grouped surface's file set could not be proven — the outcome
    stays unknown; a fresh POST fallback would duplicate the post."""


class PrefetchFailed(Exception):
    """A read before any write failed ambiguously — nothing was sent."""


async def _read(awaitable):
    """A GET before this attempt's first write. A definitive reject keeps
    its own meaning (404 = message gone, revoke achieved); any other
    failure (5xx, timeout) proves only that nothing was written yet, so
    it settles not_sent instead of an unknown that would hold the card
    and its parts for manual reconcile."""
    try:
        return await awaitable
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if worker.is_definitive_reject(exc):
            raise
        raise PrefetchFailed(worker.err_code(exc)) from exc


_PREFETCH_FAILED = {"result": "not_sent", "error_code": "prefetch_failed"}


class DeliveryWorker(worker.DeliveryWorker):
    """The neutral worker driving the bound discord.py client."""

    transport = "discord"

    # -- Discord ops ----------------------------------------------------

    async def _channel(self, channel_id: str):
        ch = self._bot.get_channel(int(channel_id))
        if ch is None:
            ch = await _read(self._bot.fetch_channel(int(channel_id)))
        return ch

    async def _perform(self, claim: dict) -> dict:
        try:
            return await self._perform_card(claim)
        except PrefetchFailed:
            return dict(_PREFETCH_FAILED)

    async def _perform_card(self, claim: dict) -> dict:
        """One HTTP attempt. Returns {result, message_id?, error_code?}.
        Only a definitive rejection maps to not_sent; anything that can
        have committed is unknown (§4)."""
        spec = claim["spec"]
        delivery = spec["delivery"]
        op = spec["op"]
        if op not in ("revoke", "update") \
                and not single_post_ready(self._bot):
            return {"result": "not_sent",
                    "error_code": "retry_policy_unknown"}
        if op == "revoke":
            mid = delivery.get("message_id")
            if not mid:
                return {"result": "not_sent", "error_code": "no_target"}
            try:
                # A sealed delete targets the delivered reply even when its
                # original evidence disappeared or its thread is archived.
                source_thread = spec["parts"].get("source_thread") is True
                channel = await self._channel(delivery["thread_id"] if source_thread else delivery["channel_id"])
                if source_thread:
                    parent, guild = _thread_parent_guild(channel)
                    if (str(channel.id) != delivery["thread_id"] or parent != delivery["channel_id"]
                            or guild != delivery.get("guild_id")):
                        return {"result": "not_sent", "error_code": "thread_scope_mismatch"}
                msg = await _read(channel.fetch_message(int(mid)))
                await msg.delete()
            except Exception as exc:
                status = getattr(exc, "status", None)
                if isinstance(status, int) and status in REVOKE_GONE_STATUS:
                    return {"result": "delivered", "message_id": mid}
                raise
            return {"result": "delivered", "message_id": mid}
        channel = await self._channel(delivery["channel_id"])
        if spec["parts"].get("thread_notice") is True or spec["parts"].get("source_thread") is True:
            notice = spec["parts"].get("thread_notice") is True
            if notice and op != "notice":
                return {"result": "not_sent", "error_code": "thread_scope_mismatch"}
            thread = await self._channel(delivery["thread_id"])
            parent, guild = _thread_parent_guild(thread)
            # an auto-archived thread reopens on send (Discord unarchives
            # it); only a locked one refuses the post
            if (str(thread.id) != delivery["thread_id"]
                    or (notice and str(thread.id) != delivery.get("message_id"))
                    or parent != delivery["channel_id"] or guild != delivery.get("guild_id")
                    or getattr(thread, "locked", False)):
                return {"result": "not_sent", "error_code": "thread_scope_mismatch"}
            channel = thread
        chat_v1 = spec["parts"].get("chat_format_version") == 1
        if op == "update":
            mid = delivery.get("message_id")
            if not mid:
                return {"result": "not_sent", "error_code": "no_target"}
            msg = await _read(channel.fetch_message(int(mid)))
            legacy_v2 = getattr(getattr(msg, "flags", None), "is_components_v2", False)
            payload = cards.message_payload(spec, components_v2=legacy_v2 or chat_v1)
            if chat_v1 and not legacy_v2:
                # IS_COMPONENTS_V2 can only be set, never unset: converting
                # a legacy embed message requires explicitly clearing
                # content and embeds (discord.py edit contract).
                payload.update(content=None, embed=None)
            members = self._group_members(spec, "card")
            if members:
                # the card surface's files ride this same edit: every
                # current attachment is retained and only members whose
                # file is not already proven upload — the response
                # proves the whole set in one call.
                files = await self._edit_group_files(msg, members)
                if files is None:
                    return {"result": "unknown",
                            "error_code": "attachment_mismatch"}
                try:
                    resp = await msg.edit(
                        **payload,
                        attachments=files,
                        allowed_mentions=cards.no_pings())
                except Exception:
                    return {"result": "unknown",
                            "error_code": "file_group_unverified"}
                response_channel = getattr(resp, "channel", None)
                if (resp is None or str(getattr(resp, "id", "")) != str(mid)
                        or (response_channel is not None and str(response_channel.id) != str(channel.id))):
                    return {"result": "unknown",
                            "error_code": "file_group_unverified"}
                echo = self._files_echo(resp, members)
                if echo is None:
                    return {"result": "unknown",
                            "error_code": "file_group_unverified"}
                return {"result": "delivered", "message_id": mid,
                        "files": echo}
            await msg.edit(**payload, allowed_mentions=cards.no_pings())
            return {"result": "delivered", "message_id": mid}
        members = self._group_members(spec, "card")
        files = await self._member_files(members) if members else []
        if members and files is None:
            return {"result": "unknown", "error_code": "attachment_mismatch"}
        send_payload = cards.message_payload(spec)
        if files:
            send_payload["files"] = files
        sent = await single_post(           # create / notice
            self._bot, partial(channel.send, **send_payload,
                               allowed_mentions=cards.no_pings()),
            retry_allowed=self._retry_gate(spec))
        out = {"result": "delivered",
               "message_id": str(sent.id)}
        if members:
            echo = self._files_echo(sent, members)
            if echo is None:
                return {"result": "unknown",
                        "error_code": "file_group_unverified"}
            out["files"] = echo
        return out

    # -- durable part deliveries ----------------------------------------

    async def _perform_part(self, claim: dict, part: dict,
                            ctx: dict) -> dict:
        try:
            return await self._part(claim, part, ctx)
        except PrefetchFailed:
            return dict(_PREFETCH_FAILED)

    async def _part(self, claim: dict, part: dict, ctx: dict) -> dict:
        """One manifest part's wire call — thread create/verify, a body
        chunk, or an attachment upload, each returning its remote id."""
        spec = claim["spec"]
        if not single_post_ready(self._bot):
            # every part kind may POST — none runs under an SDK whose
            # retry loop the single-post guard cannot hold
            return {"result": "not_sent",
                    "error_code": "retry_policy_unknown"}
        if part["kind"] == "thread":
            return await self._thread_part(claim, part, ctx)
        thread = ctx.get("thread")
        if thread is None:
            thread = await self._channel(ctx["thread_id"])
            ctx["thread"] = thread
        if part["kind"] == "attachment_part" and part.get("unavailable"):
            if part.get("edit_only") is True:
                return await self._edit_only_prior(spec, thread, part, cards.escape_md(part["caption"]), ctx)
            # no file to send — its caption is the visible line
            if not part.get("caption"):
                return {"result": "not_sent",
                        "error_code": "attachment_unavailable"}
            if part.get("prior_remote_id"):
                if post_scoped(spec):
                    return await self._text_part(
                        spec, thread, part, cards.escape_md(part["caption"]), ctx)
                # already posted — never a second caption post
                return {"result": "delivered",
                        "remote_id": str(part["prior_remote_id"])}
            return await self._text_part(
                spec, thread, part, cards.escape_md(part["caption"]), ctx)
        if part["kind"] == "body_part":
            i = int(part["part_id"].rsplit(":", 1)[1]) - 1
            # user-authored text renders literally, never as markdown;
            # the escaped text is also what remote matching compares
            body = cards.escape_md(
                spec["parts"]["thread_body_parts"][i])
            update_view = i == 0 and spec["parts"].get("thread_drug_actions") is True
            view = cards.thread_drug_view(spec) if update_view else None
            return await self._text_part(spec, thread, part, body, ctx,
                                         view=view, update_view=update_view)
        if part["kind"] == "attachment_part":
            if part.get("edit_only") is True:
                return await self._edit_only_prior(spec, thread, part, cards.escape_md(part["caption"]), ctx,
                                                   attachment=True)
            mid = await self._reuse_prior_file(
                thread, part.get("prior_remote_id"), ctx)
            if mid is not None:
                return {"result": "delivered", "remote_id": str(mid)}
            if part.get("prior_remote_id") and post_scoped(spec):
                return {"result": "unknown", "error_code": "file_target_unverified"}
            blob = await asyncio.to_thread(
                paths.read_verified_attachment, part.get("path"), part,
                self._root)
            if blob is None:
                return {"result": "not_sent",
                        "error_code": "attachment_mismatch"}
            # the caption is the file's visible line, in the same message
            extra = ({"content": cards.escape_md(part["caption"])}
                     if part.get("caption") else {})
            with io.BytesIO(blob) as source:
                sent = await single_post(self._bot, partial(
                    cards.send_attachment, thread, source,
                    part.get("name") or "file", **extra),
                    retry_allowed=self._retry_gate(spec))
            rid = getattr(sent, "id", None)
            if not rid:
                return {"result": "unknown",
                        "error_code": "missing_remote_id"}
            return {"result": "delivered", "remote_id": str(rid)}
        return {"result": "not_sent", "error_code": "unsupported_part"}

    # -- discord file_group: member files ride their surface's call ----

    @staticmethod
    def _group_members(spec: dict, surface: str) -> list:
        """Ordered member attachment_part entries of the sealed group
        anchored to ``surface`` (``card`` or a body_part part_id)."""
        manifest = spec["parts"].get("manifest") or []
        group = next((p for p in manifest
                      if p.get("kind") == "file_group"
                      and p.get("surface") == surface), None)
        if group is None:
            return []
        by_id = {p["part_id"]: p for p in manifest}
        return [by_id[m] for m in group.get("members") or () if m in by_id]

    @staticmethod
    def _wire_names(name) -> set:
        """Filenames a member's attachment may carry remotely — the
        sealed name plus the SPOILER_ normalization discord.py applies."""
        name = str(name or "")
        uploaded = name
        while uploaded.startswith("SPOILER_"):
            uploaded = uploaded[8:]
        if uploaded != name or cards.spoiler_attachment(name):
            uploaded = "SPOILER_" + uploaded
        return {name, uploaded}

    async def _member_files(self, members: list) -> list | None:
        """discord.File per member, bytes re-verified against the sealed
        sha256/bytes — None when any blob fails verification."""
        out = []
        for m in members:
            blob = await asyncio.to_thread(
                paths.read_verified_attachment, m.get("path"), m, self._root)
            if blob is None:
                return None
            out.append(cards.attachment_file(blob, m))
        return out

    @staticmethod
    def _match_attachment(msg, m: dict):
        """Retain only the exact file id and source identity sealed by a prior receipt."""
        if str(getattr(msg, "id", "")) != m.get("prior_remote_id") or not m.get("prior_remote_file_id"):
            return None
        hits = [a for a in getattr(msg, "attachments", ()) or ()
                if str(getattr(a, "id", "")) == m["prior_remote_file_id"]
                and getattr(a, "size", None) == m.get("bytes")
                and getattr(a, "filename", None) == discord_file_name(m)]
        return hits[0] if len(hits) == 1 else None

    async def _edit_group_files(self, msg, members):
        """Validate the complete retained file set before making one surface edit."""
        existing = list(getattr(msg, "attachments", ()) or ())
        retained, missing = [], []
        for member in members:
            match = self._match_attachment(msg, member)
            if match is None:
                if member.get("prior_remote_id") or member.get("prior_remote_file_id"):
                    return None
                missing.append(member)
            else:
                retained.append(match)
        if len(existing) != len(retained) or {str(a.id) for a in existing} != {str(a.id) for a in retained}:
            return None
        files = await self._member_files(missing)
        return None if files is None else retained + files

    def _files_echo(self, msg, members: list) -> list | None:
        """Bijection between the group's members and the response
        message's attachments — every member exactly one remote file
        id; any missing/extra/ambiguous match fails the whole group."""
        atts = list(getattr(msg, "attachments", ()) or ())
        if len(atts) != len(members):
            return None
        used, file_ids, echo = set(), set(), []
        for m in members:
            hits = [(i, a) for i, a in enumerate(atts)
                    if i not in used
                    and getattr(a, "size", None) == m.get("bytes")
                    and getattr(a, "filename", None) == discord_file_name(m)]
            if len(hits) != 1:
                return None
            i, a = hits[0]
            used.add(i)
            fid = getattr(a, "id", None)
            if fid is None or str(fid) in file_ids:
                return None
            file_ids.add(str(fid))
            echo.append({"part_id": m["part_id"],
                         "wire_name": getattr(a, "filename", None),
                         "bytes": m.get("bytes"),
                         "remote_file_id": str(fid)})
        return echo

    async def _group_bound(self, spec, part, mid, ctx, members):
        """Prove the complete file set on an already bound surface without another write."""
        msg = ctx.get("matched_msg")
        if msg is None or getattr(msg, "id", None) != int(mid):
            thread = ctx.get("thread")
            if thread is None:
                return {"result": "unknown",
                        "error_code": "file_group_unverified"}
            try:
                msg = await thread.fetch_message(int(mid))
            except Exception:
                return {"result": "unknown",
                        "error_code": "file_group_unverified"}
        echo = self._files_echo(msg, members)
        if any(self._match_attachment(msg, member) is None for member in members):
            echo = None
        if echo is None:
            return {"result": "unknown",
                    "error_code": "file_group_unverified"}
        return {"result": "delivered", "remote_id": str(mid),
                "files": echo}

    async def _text_part(self, spec: dict, thread, part: dict, body: str,
                         ctx: dict, *, view=None, update_view=False) -> dict:
        """Post one text message into the thread — deduped against this
        bot's history and rewritten in place when its text changed."""
        members = self._group_members(spec, part["part_id"]) \
            if part["kind"] == "body_part" else []
        if members:
            view = cards.thread_body_view(spec, part, body, controls=view)
        wire_body = None if members else body
        if part.get("edit_only") is True:
            return await self._edit_only_prior(spec, thread, part, wire_body, ctx,
                                               view=view, members=members)
        scoped = post_scoped(spec)
        if members and part.get("prior_remote_id"):
            return await self._edit_only_prior(spec, thread, part, wire_body, ctx,
                                               view=view, members=members)
        if scoped and part.get("prior_remote_id"):
            mid = await self._remote_match(thread, body, ctx, view=view,
                                           update_view=update_view, prior=part["prior_remote_id"])
            if mid is not None:
                if members:
                    return await self._group_bound(spec, part, mid, ctx, members)
                return {"result": "delivered", "remote_id": str(mid)}
            return await self._edit_only_prior(spec, thread, part, wire_body, ctx,
                                               view=view, members=members)
        if not scoped and (spec["op"] == "update" or spec["delivery"].get("thread_id")):
            # writing into a pre-existing thread — a chunk whose
            # text is already remote binds to that message id
            # instead of posting a duplicate (remote-verified)
            mid = await self._remote_match(
                thread, body, ctx, view=None if members else view,
                update_view=False if members else update_view, grouped=bool(members))
            if mid is not None:
                if members:
                    return await self._group_bound(spec, part, mid, ctx, members)
                return {"result": "delivered",
                        "remote_id": str(mid)}
            # the chunk's text changed since it was first posted
            # (the extraction arrived, a reply joined): rewrite that
            # post so the thread keeps one post per chunk
            mid = await self._rewrite_prior(
                thread, part.get("prior_remote_id"), wire_body, ctx,
                view=view, update_view=update_view, members=members)
            if mid is not None:
                if members:
                    return {"result": "delivered", "remote_id": str(mid),
                            "files": ctx["group_files"]}
                return {"result": "delivered",
                        "remote_id": str(mid)}
        files = await self._member_files(members) if members else []
        if members and files is None:
            return {"result": "unknown", "error_code": "attachment_mismatch"}
        send_kwargs = {"allowed_mentions": cards.no_pings()}
        if view is not None:
            send_kwargs["view"] = view
        if files:
            send_kwargs["files"] = files
        sent = await single_post(
            self._bot, partial(thread.send, wire_body, **send_kwargs),
            retry_allowed=self._retry_gate(spec))
        rid = getattr(sent, "id", None)
        if not rid:
            # the wire call completed but carries no provable
            # identity — honest unknown, never a bare 'delivered'
            return {"result": "unknown",
                    "error_code": "missing_remote_id"}
        out = {"result": "delivered", "remote_id": str(rid)}
        if members:
            echo = self._files_echo(sent, members)
            if echo is None:
                return {"result": "unknown",
                        "error_code": "file_group_unverified"}
            out["files"] = echo
        if new_reply_announced(spec, part):
            await self._announce_reply(spec, body, sent)
        return out

    async def _announce_reply(self, spec: dict, body: str, sent) -> None:
        """Discord has no reply_broadcast: a late reply gets one short channel
        notice linking the thread post. Best effort and never retried — the
        thread post is already delivered and must not be resent for it."""
        link = getattr(sent, "jump_url", None)
        if not isinstance(link, str) or not link.startswith("https://"):
            return
        head = body.split("\n", 1)[0][:200]
        try:
            channel = await self._channel(spec["delivery"]["channel_id"])
            await single_post(self._bot, partial(
                channel.send, f"↳ スレッドに新しい返信: {head}\n{link}",
                allowed_mentions=cards.no_pings()),
                # This is already an additional post after a delivered body;
                # withdrawal must also block its first SDK attempt.
                retry_allowed=lambda **_kw: self._retry_gate(spec)())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log("reply_announce_failed", error=type(exc).__name__)

    async def _thread_part(self, claim: dict, part: dict,
                           ctx: dict) -> dict:
        """Create the companion thread (create/notice) or verify the
        bound one is still live (update backfill). Only an
        authorization-class reject is cached as a scope verdict — a per-message 4xx (gone, already-threaded,
        bad name) says nothing about thread capability."""
        spec = claim["spec"]
        delivery = spec["delivery"]
        if delivery.get("thread_id"):
            thread = await self._channel(delivery["thread_id"])
            ctx["thread"] = thread
            return {"result": "delivered", "remote_id": str(thread.id)}
        scope_key = registry.scope_key(self.scope())
        cap = self._reg.capability(scope_key)
        if cap is not None and cap.get("ok") is False:
            return {"result": "not_sent",
                    "error_code": "thread_capability_blocked"}
        try:
            channel = await self._channel(delivery["channel_id"])
            msg = await _read(channel.fetch_message(int(ctx["card_message_id"])))
            try:
                thread = await single_post(self._bot, partial(
                    msg.create_thread, name=part["name"]),
                    retry_allowed=self._retry_gate(spec))
            except Exception as create_exc:
                # the goal may already hold — a thread bound under the
                # card message by a crashed or older-generation worker
                # between spec publication and this claim. Discord gives
                # a message-started thread the message's own snowflake,
                # so bind that thread instead of failing a dup create.
                thread = getattr(msg, "thread", None)
                if thread is None:
                    try:
                        thread = await self._channel(str(msg.id))
                    except Exception:
                        raise create_exc from None
        except Exception as exc:
            if getattr(exc, "status", None) in CAPABILITY_REJECT:
                self._reg.put_capability(scope_key, False)
            raise
        self._reg.put_capability(scope_key, True)
        ctx["thread"] = thread
        return {"result": "delivered", "remote_id": str(thread.id)}

    @staticmethod
    def _group_body_text(message):
        def texts(components):
            for component in components:
                content = getattr(component, "content", None)
                if isinstance(content, str):
                    yield content
                yield from texts(getattr(component, "components", ()) or ())
        return next(texts(getattr(message, "components", ()) or ()), None)

    async def _remote_match(self, thread, text: str, ctx: dict, *,
                            view=None, update_view=False, prior=None, grouped=False):
        """Bounded history scan — a content match binds the part to the
        real remote message id (remote-verified delivery); a miss means
        send. Only messages authored by this bot may bind: historical or
        foreign content with the same text is unrelated and can never
        impersonate a part. Each remote id matches at most once, so
        identical chunks dedupe one-for-one instead of collapsing."""
        me_id = getattr(getattr(self._bot, "user", None), "id", None)
        if me_id is None:
            return None                 # no identity can prove authorship
        if ctx.get("history") is None:
            ctx["history"] = [m async for m in thread.history(limit=100)]
        for m in ctx["history"]:
            aid = getattr(getattr(m, "author", None), "id", None)
            if aid != me_id:
                continue                # not ours — never binds a part
            if m.id in ctx["consumed"]:
                continue
            if m.content != text:
                if not grouped or self._group_body_text(m) != text:
                    continue
            if prior is not None and str(m.id) != str(prior):
                continue
            if update_view or view is not None:
                # Text equality cannot prove current runner tokens are installed.
                try:
                    await m.edit(view=view, allowed_mentions=cards.no_pings())
                except Exception as exc:
                    if worker.is_definitive_reject(exc):
                        return None     # rejected edit: verify prior or post afresh
                    raise
            ctx["consumed"].add(m.id)
            ctx["matched_msg"] = m
            return m.id
        return None

    async def _edit_only_prior(self, spec, thread, part, text, ctx, *, view=None, attachment=False, members=None):
        """Edit only the exact owned thread post; uncertain ownership or edits never POST."""
        missing_is_settled = not post_scoped(spec)
        prior = part.get("prior_remote_id")
        delivery = spec["delivery"]
        parent, guild = _thread_parent_guild(thread)
        if (spec.get("op") != "update" or str(thread.id) != delivery.get("thread_id") or str(thread.id) != str(ctx.get("thread_id"))
                or parent != delivery.get("channel_id") or guild != delivery.get("guild_id")):
            return {"result": "unknown", "error_code": "edit_thread_unverified"}
        try:
            pid = int(prior)
        except (TypeError, ValueError):
            return {"result": "unknown", "error_code": "edit_target_unverified"}
        me = getattr(getattr(self._bot, "user", None), "id", None)
        if me is None or pid in ctx["consumed"]:
            return {"result": "unknown", "error_code": "edit_target_unverified"}
        name = part.get("name") or ""
        uploaded_name = name
        # discord.py collapses repeated original spoiler prefixes to one.
        while uploaded_name.startswith("SPOILER_"):
            uploaded_name = uploaded_name[8:]
        if uploaded_name != name or cards.spoiler_attachment(name):
            uploaded_name = "SPOILER_" + uploaded_name
        verified = False
        try:
            msg = await thread.fetch_message(pid)
            channel = getattr(msg, "channel", None)
            if (getattr(msg, "id", None) != pid or getattr(getattr(msg, "author", None), "id", None) != me
                    or (channel is not None and str(channel.id) != str(thread.id))):
                return {"result": "unknown", "error_code": "edit_target_unverified"}
            if attachment and not any(
                    getattr(a, "filename", None) in (name, uploaded_name)
                    and getattr(a, "size", None) == part.get("bytes") for a in getattr(msg, "attachments", ())):
                return {"result": "unknown", "error_code": "edit_file_unverified"}
            verified = True
            edit_kwargs = {"content": text, "view": view,
                           "allowed_mentions": cards.no_pings()}
            if members:
                files = await self._edit_group_files(msg, members)
                if files is None:
                    return {"result": "unknown",
                            "error_code": "attachment_mismatch"}
                edit_kwargs["attachments"] = files
            response = await msg.edit(**edit_kwargs)
            if response is not None:
                channel = getattr(response, "channel", None)
                if (getattr(response, "id", None) != pid
                        or (channel is not None and str(channel.id) != str(thread.id))):
                    return {"result": "unknown", "error_code": "edit_response_unverified"}
                if members:
                    ctx["matched_msg"] = response
                    echo = self._files_echo(response, members)
                    if echo is None:
                        return {"result": "unknown",
                                "error_code": "file_group_unverified"}
                    ctx["consumed"].add(pid)
                    return {"result": "delivered", "remote_id": str(prior),
                            "files": echo}
        except Exception as exc:
            if getattr(exc, "status", None) in REVOKE_GONE_STATUS:     # already gone
                return ({"result": "delivered", "remote_id": str(prior)} if missing_is_settled else
                        {"result": "unknown", "error_code": "edit_target_unverified"})
            if verified and worker.is_definitive_reject(exc):
                return {"result": "not_sent", "error_code": f"http_{exc.status}"}
            return {"result": "unknown", "error_code": "edit_result_unknown"}
        if members:
            # the edit committed but returned no message to prove the
            # file set on — honest unknown, never a bare delivered
            return {"result": "unknown", "error_code": "file_group_unverified"}
        ctx["consumed"].add(pid)
        return {"result": "delivered", "remote_id": str(prior)}

    async def _own_prior(self, thread, prior, ctx: dict):
        """The runner-named earlier message, only when it still exists
        in this thread, this bot authored it and no part bound it yet.
        A missing, foreign or consumed target — or a lookup the API
        rejected outright — is None; any other failure is unknown and
        propagates."""
        try:
            pid = int(prior) if prior else None
        except (TypeError, ValueError):
            return None
        me_id = getattr(getattr(self._bot, "user", None), "id", None)
        if pid is None or me_id is None or pid in ctx["consumed"]:
            return None
        try:
            msg = await thread.fetch_message(pid)
        except Exception as exc:
            if worker.is_definitive_reject(exc):
                return None       # deleted meanwhile — post afresh
            raise
        if getattr(getattr(msg, "author", None), "id", None) != me_id:
            return None
        return msg

    async def _rewrite_prior(self, thread, prior, text: str, ctx: dict, *,
                             view=None, update_view=False, members=None):
        """Edit this bot's earlier post of the same chunk in place and
        return its id. Only a message this bot authored in this thread
        can be rewritten — a missing, foreign or already-bound target,
        or an edit the API rejected outright (deleted post, archived
        thread), returns None so the caller posts a new chunk exactly as
        before. An edit is idempotent, so unlike a POST it needs no
        single-shot guard; a crash after the edit is re-bound by
        ``_remote_match``. Any other failure is unknown and propagates.
        With ``members`` the same edit carries the surface's unproven
        files; an unverifiable file set raises instead of falling back
        to a fresh POST that would duplicate the post."""
        msg = await self._own_prior(thread, prior, ctx)
        if msg is None:
            return None
        edit_kwargs = {"content": text,
                       "allowed_mentions": cards.no_pings()}
        if update_view or view is not None:
            edit_kwargs["view"] = view
        if members:
            files = await self._edit_group_files(msg, members)
            if files is None:
                raise _FilesUnverified
            edit_kwargs["attachments"] = files
        try:
            resp = await msg.edit(**edit_kwargs)
        except Exception as exc:
            if worker.is_definitive_reject(exc):
                return None       # the edit did not commit — post afresh
            raise
        ctx["consumed"].add(msg.id)
        if members:
            if resp is None or self._files_echo(resp, members) is None:
                raise _FilesUnverified
            ctx["matched_msg"] = resp
            ctx["group_files"] = self._files_echo(resp, members)
        return msg.id

    async def _reuse_prior_file(self, thread, prior, ctx: dict):
        """The runner names the message that already carries this exact
        file (same sealed sha256) from an earlier render. When that
        message still exists in this thread, was posted by this bot and
        still holds a file, bind the part to it instead of uploading a
        second copy; otherwise None and the file is uploaded as before
        (never a blind second upload on an unknown lookup failure)."""
        msg = await self._own_prior(thread, prior, ctx)
        if msg is None or not getattr(msg, "attachments", None):
            return None
        ctx["consumed"].add(msg.id)
        return msg.id
