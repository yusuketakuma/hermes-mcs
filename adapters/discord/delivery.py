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


class DeliveryWorker(worker.DeliveryWorker):
    """The neutral worker driving the bound discord.py client."""

    transport = "discord"

    # -- Discord ops ----------------------------------------------------

    async def _channel(self, channel_id: str):
        ch = self._bot.get_channel(int(channel_id))
        if ch is None:
            ch = await self._bot.fetch_channel(int(channel_id))
        return ch

    async def _perform(self, claim: dict) -> dict:
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
        channel = await self._channel(delivery["channel_id"])
        if op == "revoke":
            mid = delivery.get("message_id")
            if not mid:
                return {"result": "not_sent",
                        "error_code": "no_target"}
            try:
                msg = await channel.fetch_message(int(mid))
                await msg.delete()
            except Exception as exc:
                status = getattr(exc, "status", None)
                if isinstance(status, int) and status in REVOKE_GONE_STATUS:
                    # already gone — the revoke goal holds
                    return {"result": "delivered", "message_id": mid}
                raise
            return {"result": "delivered", "message_id": mid}
        view = cards.build_view(spec)
        if op == "update":
            mid = delivery.get("message_id")
            if not mid:
                return {"result": "not_sent", "error_code": "no_target"}
            msg = await channel.fetch_message(int(mid))
            await msg.edit(view=view, allowed_mentions=cards.no_pings())
            return {"result": "delivered", "message_id": mid}
        sent = await cards.single_post(           # create / notice
            self._bot, partial(channel.send, view=view,
                               allowed_mentions=cards.no_pings()))
        return {"result": "delivered",
                "message_id": str(sent.id)}

    # -- durable part deliveries ----------------------------------------

    async def _perform_part(self, claim: dict, part: dict,
                            ctx: dict) -> dict:
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
        if part["kind"] == "body_part":
            i = int(part["part_id"].rsplit(":", 1)[1]) - 1
            body = spec["parts"]["thread_body_parts"][i]
            if spec["op"] == "update" or spec["delivery"].get("thread_id"):
                # writing into a pre-existing thread — a chunk whose
                # text is already remote binds to that message id
                # instead of posting a duplicate (remote-verified)
                mid = await self._remote_match(thread, body, ctx)
                if mid is not None:
                    return {"result": "delivered",
                            "remote_id": str(mid)}
                # the chunk's text changed since it was first posted
                # (the extraction arrived, a reply joined): rewrite that
                # post so the thread keeps one post per chunk
                mid = await self._rewrite_prior(
                    thread, part.get("prior_remote_id"), body, ctx)
                if mid is not None:
                    return {"result": "delivered",
                            "remote_id": str(mid)}
            sent = await cards.single_post(
                self._bot, partial(thread.send, body,
                                   allowed_mentions=cards.no_pings()))
            rid = getattr(sent, "id", None)
            if not rid:
                # the wire call completed but carries no provable
                # identity — honest unknown, never a bare 'delivered'
                return {"result": "unknown",
                        "error_code": "missing_remote_id"}
            return {"result": "delivered", "remote_id": str(rid)}
        if part["kind"] == "attachment_part":
            mid = await self._reuse_prior_file(
                thread, part.get("prior_remote_id"), ctx)
            if mid is not None:
                return {"result": "delivered", "remote_id": str(mid)}
            blob = await asyncio.to_thread(
                paths.read_verified_attachment, part.get("path"), part)
            if blob is None:
                return {"result": "not_sent",
                        "error_code": "attachment_mismatch"}
            with io.BytesIO(blob) as source:
                sent = await cards.single_post(self._bot, partial(
                    cards.send_attachment, thread, source,
                    part.get("name") or "file"))
            rid = getattr(sent, "id", None)
            if not rid:
                return {"result": "unknown",
                        "error_code": "missing_remote_id"}
            return {"result": "delivered", "remote_id": str(rid)}
        return {"result": "not_sent", "error_code": "unsupported_part"}

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
            msg = await channel.fetch_message(int(ctx["card_message_id"]))
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

    async def _remote_match(self, thread, text: str, ctx: dict):
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
            ctx["consumed"].add(m.id)
            return m.id
        return None

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

    async def _rewrite_prior(self, thread, prior, text: str, ctx: dict):
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
            await msg.edit(content=text, allowed_mentions=cards.no_pings())
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
