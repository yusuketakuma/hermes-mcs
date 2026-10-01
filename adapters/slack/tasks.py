"""Supervise the Slack sender with the existing durable card worker.

The Hermes Slack adapter rebuilds its native ``AsyncApp`` on an
in-process reconnect and re-runs the handler factory on the new app,
while the previous supervisor is still alive and holds the scope lock.
The newest supervisor for a scope therefore asks its in-process
predecessor to stop and waits ``LOCK_WAIT_S`` for the lock, so the
rebuilt app regains its MCS handlers without a process restart (the
Discord supervisor does the same via ``bot.is_closed()``).
"""
from __future__ import annotations

import asyncio

from hermes_plugin.mcs_delivery import paths as shared_paths
from hermes_plugin.mcs_delivery import registry
from hermes_plugin.mcs_delivery.worker import POLL_S

from . import paths
from .actions import Actions
from .delivery import DeliveryWorker, SlackCardAdapter

# Handoff window: the predecessor stops between sends (every send
# re-checks the stop flag), then releases the lock when its tick ends.
LOCK_WAIT_S = POLL_S * 15

# scope key -> newest supervisor in this process
_LIVE: dict = {}


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
        key = registry.scope_key(self._settings)
        previous = _LIVE.get(key)
        _LIVE[key] = self
        if previous is not None and previous is not self:
            previous.unload()          # superseded by a rebuilt app
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
            # wait out a superseded in-process predecessor
            if not await self._worker.wait_scope_lock(
                    LOCK_WAIT_S, lambda: self._stopping):
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
            key = registry.scope_key(self._settings)
            if _LIVE.get(key) is self:
                del _LIVE[key]
