"""Supervised worker lifecycle — one Supervisor per live Bot instance.

Registration happens per ``register_platform_handler`` call (i.e. per
connect / fresh Bot). The supervisor owns the scope lock, the reconcile
pass, and the ~2s poll loop; ``ctx.spawn_task`` supervises the task so
plugin unload cancels it — the delivery lock is only released after the
task actually finishes, never on the unload signal itself.
"""
from __future__ import annotations

import asyncio
from typing import Any

from . import actions, delivery, paths, registry

POLL_S = delivery.POLL_S


class Supervisor:
    def __init__(self, *, ctx: Any, bot: Any, adapter: Any,
                 settings: dict, log) -> None:
        self._ctx = ctx
        self._bot = bot
        self._adapter = adapter
        self._settings = settings
        self._log = log
        self._root = paths.data_root(settings)
        paths.ensure_dirs(self._root)
        self._dirs = paths.notify_dirs(self._root)
        self._reg = registry.Registry(self._dirs["state"])
        self._worker_id = registry.new_worker_id()
        self._worker = delivery.DeliveryWorker(
            bot=bot, adapter=adapter, settings=settings,
            root=self._root, reg=self._reg,
            worker_id=self._worker_id, log=log)
        self._actions = actions.Actions(
            bot=bot, settings=settings, root=self._root,
            reg=self._reg, worker_id=self._worker_id, log=log)
        self._task = None
        self._stopping = False

    # -- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Register the interaction listener on THIS Bot and spawn the
        supervised poll loop. Returns False when startup deterministically
        failed (visible stopped state, no silent retry)."""
        self._bot.add_listener(
            self._actions.on_interaction, "on_interaction")
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
            if not self._worker.acquire_scope_lock():
                # another process owns this delivery scope — stay
                # visible-stopped rather than racing sends
                self._log("scope_lock_unavailable")
                return
            stats = await self._worker.reconcile()
            if any(stats.values()):
                self._log("reconciled", **stats)
            while not self._stopping:
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
            self._worker.release_scope_lock()
