"""Discord wire edge of the card delivery worker.

The durable claim -> transport_begin -> grant -> send ->
transport_receipt loop lives in ``mcs_delivery.worker``; this subclass
supplies only the Discord operations the neutral loop calls into:
channel/message sends and edits, revoke deletes, and the card's
companion-thread body post.
"""
from __future__ import annotations

import asyncio

from ..mcs_delivery import envelopes, registry, text, worker
from . import cards

# Discord delete of an already-gone message achieves the revoke goal.
REVOKE_GONE_STATUS = frozenset({404, 410})


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
            thread = await sent_message.create_thread(name=name)
            self._reg.put_capability(scope_key, True)
            env = envelopes.thread_receipt(
                spec["delivery_id"], message_id,
                thread_id=str(thread.id))
        except Exception as exc:
            if worker.is_definitive_reject(exc):
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
                posted = {m.content
                          async for m in thread.history(limit=100)}
            except Exception as exc:
                self._log("thread_body_failed",
                          error=type(exc).__name__)
                return
            chunks = [c for c in chunks if c not in posted]
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
