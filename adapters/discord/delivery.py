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
import io
from functools import partial

from adapters.common import paths, registry, worker
from . import cards

# Discord delete of an already-gone message achieves the revoke goal.
REVOKE_GONE_STATUS = frozenset({404, 410})

# Only an authorization-class answer judges the scope's thread
# capability; other 4xx verdicts are per-message, never per-scope.
CAPABILITY_REJECT = frozenset({401, 403})


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
                and not cards.single_post_ready(self._bot):
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
                    parent = getattr(channel, "parent_id", getattr(getattr(channel, "parent", None), "id", None))
                    guild = getattr(getattr(channel, "guild", None), "id", None)
                    if (str(channel.id) != delivery["thread_id"] or str(parent) != delivery["channel_id"]
                            or str(guild) != delivery.get("guild_id")):
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
            parent = getattr(thread, "parent_id", getattr(getattr(thread, "parent", None), "id", None))
            guild = getattr(getattr(thread, "guild", None), "id", None)
            # an auto-archived thread reopens on send (Discord unarchives
            # it); only a locked one refuses the post
            if (str(thread.id) != delivery["thread_id"]
                    or (notice and str(thread.id) != delivery.get("message_id"))
                    or str(parent) != delivery["channel_id"] or str(guild) != delivery.get("guild_id")
                    or getattr(thread, "locked", False)):
                return {"result": "not_sent", "error_code": "thread_scope_mismatch"}
            channel = thread
        if op == "update":
            mid = delivery.get("message_id")
            if not mid:
                return {"result": "not_sent", "error_code": "no_target"}
            msg = await _read(channel.fetch_message(int(mid)))
            legacy_v2 = getattr(getattr(msg, "flags", None), "is_components_v2", False)
            await msg.edit(**cards.message_payload(spec, components_v2=legacy_v2),
                           allowed_mentions=cards.no_pings())
            return {"result": "delivered", "message_id": mid}
        sent = await cards.single_post(           # create / notice
            self._bot, partial(channel.send, **cards.message_payload(spec),
                               allowed_mentions=cards.no_pings()))
        return {"result": "delivered",
                "message_id": str(sent.id)}

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
        if not cards.single_post_ready(self._bot):
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
                sent = await cards.single_post(self._bot, partial(
                    cards.send_attachment, thread, source,
                    part.get("name") or "file", **extra))
            rid = getattr(sent, "id", None)
            if not rid:
                return {"result": "unknown",
                        "error_code": "missing_remote_id"}
            return {"result": "delivered", "remote_id": str(rid)}
        return {"result": "not_sent", "error_code": "unsupported_part"}

    async def _text_part(self, spec: dict, thread, part: dict, body: str,
                         ctx: dict, *, view=None, update_view=False) -> dict:
        """Post one text message into the thread — deduped against this
        bot's history and rewritten in place when its text changed."""
        if part.get("edit_only") is True:
            return await self._edit_only_prior(spec, thread, part, body, ctx, view=view)
        if spec["op"] == "update" or spec["delivery"].get("thread_id"):
            # writing into a pre-existing thread — a chunk whose
            # text is already remote binds to that message id
            # instead of posting a duplicate (remote-verified)
            mid = await self._remote_match(
                thread, body, ctx, view=view, update_view=update_view)
            if mid is not None:
                return {"result": "delivered",
                        "remote_id": str(mid)}
            # the chunk's text changed since it was first posted
            # (the extraction arrived, a reply joined): rewrite that
            # post so the thread keeps one post per chunk
            mid = await self._rewrite_prior(
                thread, part.get("prior_remote_id"), body, ctx,
                view=view, update_view=update_view)
            if mid is not None:
                return {"result": "delivered",
                        "remote_id": str(mid)}
        sent = await cards.single_post(
            self._bot, partial(thread.send, body,
                               allowed_mentions=cards.no_pings(),
                               **({"view": view} if view is not None else {})))
        rid = getattr(sent, "id", None)
        if not rid:
            # the wire call completed but carries no provable
            # identity — honest unknown, never a bare 'delivered'
            return {"result": "unknown",
                    "error_code": "missing_remote_id"}
        return {"result": "delivered", "remote_id": str(rid)}

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
                thread = await cards.single_post(self._bot, partial(
                    msg.create_thread, name=part["name"]))
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

    async def _remote_match(self, thread, text: str, ctx: dict, *,
                            view=None, update_view=False):
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
            if m.id in ctx["consumed"] or m.content != text:
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
            return m.id
        return None

    async def _edit_only_prior(self, spec, thread, part, text, ctx, *, view=None, attachment=False):
        """Edit only the exact owned thread post; uncertain ownership or edits never POST."""
        prior = part.get("prior_remote_id")
        delivery = spec["delivery"]
        parent = getattr(thread, "parent_id", getattr(getattr(thread, "parent", None), "id", None))
        guild = getattr(getattr(thread, "guild", None), "id", None)
        if (spec.get("op") != "update" or str(thread.id) != delivery.get("thread_id") or str(thread.id) != str(ctx.get("thread_id"))
                or str(parent) != delivery.get("channel_id") or str(guild) != delivery.get("guild_id")):
            return {"result": "unknown", "error_code": "edit_thread_unverified"}
        try:
            pid = int(prior)
        except (TypeError, ValueError):
            return {"result": "unknown", "error_code": "edit_target_unverified"}
        me = getattr(getattr(self._bot, "user", None), "id", None)
        if me is None or pid in ctx["consumed"]:
            return {"result": "unknown", "error_code": "edit_target_unverified"}
        verified = False
        try:
            msg = await thread.fetch_message(pid)
            channel = getattr(msg, "channel", None)
            if (getattr(msg, "id", None) != pid or getattr(getattr(msg, "author", None), "id", None) != me
                    or (channel is not None and str(channel.id) != str(thread.id))):
                return {"result": "unknown", "error_code": "edit_target_unverified"}
            if attachment and not any(
                    getattr(a, "filename", None) == part.get("name")
                    and getattr(a, "size", None) == part.get("bytes") for a in getattr(msg, "attachments", ())):
                return {"result": "unknown", "error_code": "edit_file_unverified"}
            verified = True
            response = await msg.edit(content=text, view=view, allowed_mentions=cards.no_pings())
            if response is not None:
                channel = getattr(response, "channel", None)
                if (getattr(response, "id", None) != pid
                        or (channel is not None and str(channel.id) != str(thread.id))):
                    return {"result": "unknown", "error_code": "edit_response_unverified"}
        except Exception as exc:
            if getattr(exc, "status", None) in (404, 410):
                return {"result": "delivered", "remote_id": str(prior)}
            if verified and worker.is_definitive_reject(exc):
                return {"result": "not_sent", "error_code": f"http_{exc.status}"}
            return {"result": "unknown", "error_code": "edit_result_unknown"}
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
                             view=None, update_view=False):
        """Edit this bot's earlier post of the same chunk in place and
        return its id. Only a message this bot authored in this thread
        can be rewritten — a missing, foreign or already-bound target,
        or an edit the API rejected outright (deleted post, archived
        thread), returns None so the caller posts a new chunk exactly as
        before. An edit is idempotent, so unlike a POST it needs no
        single-shot guard; a crash after the edit is re-bound by
        ``_remote_match``. Any other failure is unknown and propagates."""
        msg = await self._own_prior(thread, prior, ctx)
        if msg is None:
            return None
        try:
            await msg.edit(content=text, allowed_mentions=cards.no_pings(),
                           **({"view": view} if update_view or view is not None else {}))
        except Exception as exc:
            if worker.is_definitive_reject(exc):
                return None       # the edit did not commit — post afresh
            raise
        ctx["consumed"].add(msg.id)
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
