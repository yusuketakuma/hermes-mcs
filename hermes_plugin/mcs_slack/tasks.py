"""Supervise the Slack sender with the existing durable card worker."""
from __future__ import annotations

import asyncio

from ..mcs_delivery import paths as shared_paths
from ..mcs_delivery import registry
from ..mcs_delivery.worker import POLL_S

from . import paths
from .actions import Actions
from .delivery import DeliveryWorker, SlackCardAdapter


class Supervisor:
    def __init__(self, *, ctx, app, adapter, settings, log):
        self._ctx = ctx
        self._settings = settings
        self._log = log
        self._root = shared_paths.data_root(settings)
        self._dirs = paths.ensure_dirs(self._root)
        self._reg = registry.Registry(self._dirs["state"], scope=settings)
        self._worker_id = registry.new_worker_id()
        self._sender = SlackCardAdapter(
            app, native_adapter=adapter, team_id=settings["team_id"],
            application_id=settings["application_id"],
            channel_id=settings["channel_id"], profile=settings["profile"],
            allowed_user_ids=settings["allowed_user_ids"])
        self._worker = DeliveryWorker(
            sender=self._sender, settings=settings, root=self._root,
            reg=self._reg, worker_id=self._worker_id, log=log)
        self._actions = Actions(
            app=app, settings=settings, dirs=self._dirs,
            reg=self._reg, sender=self._sender, log=log)
        self._stopping = False
        self._task = None

    def start(self):
        self._task = self._ctx.spawn_task(
            self._run(), name=f"mcs-slack:{self._worker_id}")
        if self._task is None or self._task.done():
            self._log("worker_start_failed")
            return False
        self._ctx.on_unload(self.unload)
        return True

    def unload(self):
        self._stopping = True
        self._worker.stop()
        self._actions.unload()

    async def _run(self):
        try:
            if not self._worker.acquire_scope_lock():
                self._log("scope_lock_unavailable")
                return
            self._reg.reload()
            with self._reg.batch():
                stats = await self._worker.reconcile()
            if any(stats.values()):
                self._log("reconciled", **stats)
            if not await self._sender.bind():
                self._log("workspace_bind_failed")
                return
            if self._stopping:
                return
            self._actions.register()
            while not self._stopping:
                try:
                    await self._worker.tick()
                    await self._actions.sweep_followups()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log("tick_error", error=type(exc).__name__)
                await asyncio.sleep(POLL_S)
        except asyncio.CancelledError:
            raise
        finally:
            self._actions.unload()
            self._worker.release_scope_lock()
