"""Supervised worker lifecycle — one Supervisor per live Bot instance.

Registration happens per ``register_platform_handler`` call (i.e. per
connect / fresh Bot). The supervisor owns the scope lock, the reconcile
pass, and the ~2s poll loop; ``ctx.spawn_task`` supervises the task so
plugin unload cancels it — the delivery lock is only released after the
task actually finishes, never on the unload signal itself.

Adapter rebuilds (fatal transport error → host discards the adapter and
connects a fresh one) re-run the factory on a NEW Bot while the old
supervisor may still be alive. Two guards keep the interaction surface
recoverable: the running loop exits once ``bot.is_closed()`` (releasing
the scope lock), and a successor waits ``LOCK_WAIT_S`` for that release
before declaring the scope unavailable — so a rebuilt adapter regains
its listener without a process restart.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ..mcs_delivery import paths, registry
from ..mcs_delivery.worker import POLL_S
from . import actions, delivery

# Takeover window for the scope lock after a bot rebuild: the host
# disconnects a fatally-failed adapter (client.close()) BEFORE the
# replacement adapter connects, so the previous supervisor releases the
# lock within one poll of close; this bound covers that handoff without
# letting a foreign owner be raced.
LOCK_WAIT_S = POLL_S * 5


def _bot_closed(bot: Any) -> bool:
    """True when the bound discord.py client was closed — the adapter is
    gone for good (fatal rebuild), so this supervisor must yield the
    scope lock to the successor wired on the new client."""
    is_closed = getattr(bot, "is_closed", None)
    try:
        return bool(is_closed()) if callable(is_closed) else False
    except Exception:
        return False


class Supervisor:
    def __init__(self, *, ctx: Any, bot: Any,
                 settings: dict, log) -> None:
        self._ctx = ctx
        self._bot = bot
        self._settings = settings
        self._log = log
        self._root = paths.data_root(settings)
        paths.ensure_dirs(self._root)
        self._dirs = paths.notify_dirs(self._root)
        self._reg = registry.Registry(self._dirs["state"], scope=settings)
        self._worker_id = registry.new_worker_id()
        self._worker = delivery.DeliveryWorker(
            bot=bot, settings=settings,
            root=self._root, reg=self._reg,
            worker_id=self._worker_id, log=log)
        self._actions = actions.Actions(
            bot=bot, settings=settings, root=self._root,
            reg=self._reg, log=log)
        self._task = None
        self._stopping = False

    # -- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Register the interaction listener on THIS Bot and spawn the
        supervised poll loop. Returns False when startup deterministically
        failed (visible stopped state, no silent retry)."""
        self._task = self._ctx.spawn_task(
            self._run(), name=f"mcs-discord:{self._worker_id}")
        # spawn_task returns the asyncio.Task; task is None or done()
        # means it never started — is_running() is not the contract
        if self._task is None or self._task.done():
            self._stop_listener()
            self._log("worker_start_failed")
            return False
        self._ctx.on_unload(self.unload)
        return True

    def _stop_listener(self) -> None:
        try:
            self._bot.remove_listener(
                self._actions.on_interaction, "on_interaction")
        except Exception:
            pass

    def unload(self) -> None:
        """ctx.on_unload callback — stop intake now; the supervised task
        is cancelled by the host and releases the scope lock itself."""
        self._stopping = True
        self._worker.stop()
        self._stop_listener()

    # -- the poll loop ---------------------------------------------------

    async def _run(self) -> None:
        try:
            deadline = time.monotonic() + LOCK_WAIT_S
            while not self._worker.acquire_scope_lock():
                # another worker owns this delivery scope — wait out a
                # dying predecessor (fatal adapter rebuild closes its
                # bot, which releases the lock within one poll), but
                # never race a live foreign owner
                if self._stopping or _bot_closed(self._bot) \
                        or time.monotonic() >= deadline:
                    self._log("scope_lock_unavailable")
                    return
                await asyncio.sleep(0.25)
            self._reg.reload()
            with self._reg.batch():
                stats = await self._worker.reconcile()
            if any(stats.values()):
                self._log("reconciled", **stats)
            if self._stopping or _bot_closed(self._bot):
                return
            self._bot.add_listener(
                self._actions.on_interaction, "on_interaction")
            while not self._stopping:
                if _bot_closed(self._bot):
                    # adapter rebuild: the successor wires its own
                    # listener; yield the lock so it can take over
                    self._log("bot_closed")
                    return
                try:
                    await self._worker.tick()
                    await self._actions.sweep_followups()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._log("tick_error", error=type(e).__name__)
                await asyncio.sleep(POLL_S)
        except asyncio.CancelledError:
            raise
        finally:
            # released only when the task is truly done — in-flight
            # cancellation mid-send is an 'unknown' the journal keeps
            self._stop_listener()
            self._worker.release_scope_lock()
