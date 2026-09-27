"""Discord wire edge of the card delivery worker.

The durable claim -> transport_begin -> grant -> send ->
transport_receipt loop lives in ``mcs_delivery.worker``; this subclass
supplies only the Discord operations the neutral loop calls into:
channel/message sends and edits, revoke deletes, the card's
companion-thread body post, and the durable per-part deliveries a
sealed manifest plans.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from collections import Counter

from ..mcs_delivery import envelopes, registry, text, worker
from . import cards

# Discord delete of an already-gone message achieves the revoke goal.
REVOKE_GONE_STATUS = frozenset({404, 410})

# Only an authorization-class answer judges the scope's thread
# capability; other 4xx verdicts are per-message, never per-scope.
CAPABILITY_REJECT = frozenset({401, 403})


def _file_matches(path: str, part: dict) -> bool:
    """The file on disk must still be the sealed payload — a changed
    or missing file is a not_sent, never a substituted send."""
    try:
        st = os.stat(path)
        if part.get("bytes") is not None and st.st_size != part["bytes"]:
            return False
        want = part.get("sha256")
        if not want:
            return False
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest() == want
    except OSError:
        return False


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
            await msg.edit(view=view)
            return {"result": "delivered", "message_id": mid}
        sent = await channel.send(view=view)      # create / notice
        return {"result": "delivered",
                "message_id": str(sent.id)}

    # -- durable part deliveries ----------------------------------------

    async def _perform_part(self, claim: dict, part: dict,
                            ctx: dict) -> dict:
        """One manifest part's wire call — thread create/verify, a body
        chunk, or an attachment upload, each returning its remote id."""
        spec = claim["spec"]
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
            sent = await thread.send(body)
            rid = getattr(sent, "id", None)
            if not rid:
                # the wire call completed but carries no provable
                # identity — honest unknown, never a bare 'delivered'
                return {"result": "unknown",
                        "error_code": "missing_remote_id"}
            return {"result": "delivered", "remote_id": str(rid)}
        if part["kind"] == "attachment_part":
            path = part.get("path")
            if not path or not _file_matches(path, part):
                return {"result": "not_sent",
                        "error_code": "attachment_mismatch"}
            sent = await cards.send_attachment(
                thread, path, part.get("name") or "file")
            rid = getattr(sent, "id", None)
            if not rid:
                return {"result": "unknown",
                        "error_code": "missing_remote_id"}
            return {"result": "delivered", "remote_id": str(rid)}
        return {"result": "not_sent", "error_code": "unsupported_part"}

    async def _thread_part(self, claim: dict, part: dict,
                           ctx: dict) -> dict:
        """Create the companion thread (create/notice) or verify the
        bound one is still live (update backfill). The capability cache
        mirrors the legacy path: only an authorization-class reject is
        a scope verdict — a per-message 4xx (gone, already-threaded,
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
                thread = await msg.create_thread(name=part["name"])
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
        if ctx.get("history") is None:
            ctx["history"] = [m async for m in thread.history(limit=100)]
        me_id = getattr(getattr(self._bot, "user", None), "id", None)
        for m in ctx["history"]:
            if me_id is not None:
                aid = getattr(getattr(m, "author", None), "id", None)
                if aid != me_id:
                    continue            # not ours — never binds a part
            if m.id in ctx["consumed"] or m.content != text:
                continue
            ctx["consumed"].add(m.id)
            return m.id
        return None

    async def _maybe_thread(self, claim: dict, message_id: str) -> None:
        """Card companion thread — separated from the body send; a
        thread failure never resends the card itself (plan §3)."""
        spec = claim["spec"]
        if spec["delivery"].get("thread_id"):
            # update on a card whose thread predates the in-thread
            # body — backfill whatever chunks are missing, once
            if spec["op"] == "update" \
                    and spec["parts"].get("thread_body"):
                await self._thread_backfill(spec)
            return
        name = (spec["parts"].get("thread_name")
                if spec["op"] in ("create", "notice") else None)
        if not name:
            return
        scope_key = registry.scope_key(self.scope())
        cap = self._reg.capability(scope_key)
        if cap is not None and cap.get("ok") is False:
            return                                 # negative-cached
        thread = None
        try:
            channel = await self._channel(spec["delivery"]["channel_id"])
            sent_message = await channel.fetch_message(int(message_id))
            try:
                thread = await sent_message.create_thread(name=name)
            except Exception as create_exc:
                # same already-exists recovery as _thread_part — the
                # thread a previous attempt or older worker left under
                # this card message satisfies the create goal
                thread = getattr(sent_message, "thread", None)
                if thread is None:
                    try:
                        thread = await self._channel(
                            str(sent_message.id))
                    except Exception:
                        raise create_exc from None
            self._reg.put_capability(scope_key, True)
            env = envelopes.thread_receipt(
                spec["delivery_id"], message_id,
                thread_id=str(thread.id))
        except Exception as exc:
            if getattr(exc, "status", None) in CAPABILITY_REJECT:
                self._reg.put_capability(scope_key, False)
            env = envelopes.thread_receipt(
                spec["delivery_id"], message_id,
                error_code=worker.err_code(exc))
        await asyncio.to_thread(
            envelopes.publish_command, self._dirs["cmd_int"], env)
        await self._thread_body(spec, thread)

    async def _thread_backfill(self, spec: dict) -> None:
        """Threads created before the in-thread body hold no text —
        the next update render posts it there. Best-effort like the
        create path: failures only log."""
        try:
            thread = await self._channel(spec["delivery"]["thread_id"])
        except Exception as exc:
            self._log("thread_body_failed", error=type(exc).__name__)
            return
        await self._thread_body(spec, thread, dedupe=True)

    async def _thread_body(self, spec: dict, thread,
                           dedupe: bool = False) -> None:
        """Full text lands inside the companion thread — the card
        itself stays a summary surface. Best-effort by design: the
        thread (and its receipt) is already settled, so a chunk send
        failure only logs; it must not re-enter the delivery path.
        `dedupe` content-matches against recent history so re-renders
        of an already-populated thread — or a partial earlier post —
        never duplicate chunks."""
        if thread is None:
            return
        body = str(spec["parts"].get("thread_body") or "")
        chunks = [c for c in text.split_body(body) if c.strip()]
        if not chunks:
            return
        if dedupe:
            try:
                posted = Counter(
                    [m.content async for m in thread.history(limit=100)])
            except Exception as exc:
                self._log("thread_body_failed",
                          error=type(exc).__name__)
                return
            missing = []
            for chunk in chunks:
                if posted[chunk]:
                    posted[chunk] -= 1
                else:
                    missing.append(chunk)
            chunks = missing
            if not chunks:
                self._log("thread_body_skipped", reason="already_posted")
                return
        sent = 0
        for chunk in chunks:
            try:
                await thread.send(chunk)
                sent += 1
            except Exception as exc:
                self._log("thread_body_failed",
                          error=type(exc).__name__,
                          chunks_sent=sent)
                return
        self._log("thread_body_posted",
                  thread_id=str(thread.id), chunks=sent)
