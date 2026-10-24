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
import weakref
from typing import Any

from adapters.common import paths, registry
from adapters.common.worker import POLL_S, await_settled, retry_startup
from . import actions, delivery
from contextlib import suppress

# Takeover window for the scope lock after a bot rebuild: the host
# disconnects a fatally-failed adapter (client.close()) BEFORE the
# replacement adapter connects, so the previous supervisor releases the
# lock within one poll of close; this bound covers that handoff without
# letting a foreign owner be raced.
LOCK_WAIT_S = POLL_S * 5

# scope key -> newest supervisor in this process
_LIVE: dict = {}
# host -> scope keys whose unload hook it already holds. Hosts keep their
# unload callbacks until they close, so one hook per host and scope stops the
# newest supervisor instead of pinning every superseded one; weak keys let a
# closed, dropped host go.
_HOOKED = weakref.WeakKeyDictionary()


def _hook_unload(ctx, key) -> None:
    try:
        hooked = _HOOKED.setdefault(ctx, set())
    except TypeError:                # a host that cannot be weakly referenced
        hooked = None
    if hooked is not None and key in hooked:
        return

    def unload():
        live = _LIVE.get(key)
        if live is not None and live._ctx is ctx:
            # a task cancelled before it ever ran skips its finally, so the
            # host's unload drops the entry itself
            del _LIVE[key]
            live.unload()
    ctx.on_unload(unload)
    if hooked is not None:
        hooked.add(key)


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
        self._dirs = paths.ensure_dirs(self._root)
        self._reg = registry.Registry(self._dirs["state"], scope=settings)
        self._worker_id = registry.new_worker_id()
        self._worker = delivery.DeliveryWorker(
            bot=bot, settings=settings,
            root=self._root, reg=self._reg,
            worker_id=self._worker_id, log=log)
        self._delivery = getattr(ctx, "delivery_admission", None)
        self._delivery_identity = getattr(ctx, "delivery_identity", None)
        if self._delivery is not None:
            self._worker.send_admitted = lambda: self._delivery.send_allowed(self._delivery_identity)
        self._actions = actions.Actions(
            bot=bot, settings=settings, root=self._root,
            reg=self._reg, log=log,
            native_admission=getattr(ctx, "native_admission", None))
        self._task = None
        self._stopping = False
        self._ready = False

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
        key = registry.scope_key(self._settings)
        _LIVE[key] = self
        _hook_unload(self._ctx, key)
        return True

    def _stop_listener(self) -> None:
        with suppress(Exception):
            self._bot.remove_listener(
                self._actions.on_interaction, "on_interaction")
    def unload(self) -> None:
        """Stop intake synchronously; the supervised task retains its I/O until settled."""
        self._stopping = True
        if self._delivery is not None:
            self._delivery.connector_closing(self._delivery_identity)
        self._worker.stop()
        self._stop_listener()

    # -- the poll loop ---------------------------------------------------

    async def _run(self) -> None:
        try:
            # another worker owns this delivery scope — wait out a
            # dying predecessor (fatal adapter rebuild closes its bot,
            # which releases the lock within one poll)
            if not await self._worker.wait_scope_lock(
                    LOCK_WAIT_S,
                    lambda: self._stopping or _bot_closed(self._bot)):
                return
            async def reconcile():
                await await_settled(self._worker.reload_reconcile(), on_cancel=self.unload)
                return True

            if not await retry_startup(
                    reconcile,
                    lambda: self._stopping or _bot_closed(self._bot),
                    self._log) \
                    or self._stopping or _bot_closed(self._bot):
                return
            self._bot.add_listener(
                self._actions.on_interaction, "on_interaction")
            self._ready = True
            while not self._stopping:
                if _bot_closed(self._bot):
                    # adapter rebuild: the successor wires its own
                    # listener; yield the lock so it can take over
                    self._log("bot_closed")
                    return
                if self._delivery is not None and not self._delivery.begin_pass(self._delivery_identity):
                    await asyncio.sleep(POLL_S)
                    continue
                try:
                    await await_settled(self._pass(), on_cancel=self.unload)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._log("tick_error", error=type(e).__name__)
                finally:
                    if self._delivery is not None:
                        self._delivery.end_pass(self._delivery_identity)
                await asyncio.sleep(POLL_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log("worker_exit", error=type(e).__name__)
            raise
        finally:
            self._ready = False
            if self._delivery is not None:
                self._delivery.connector_closing(self._delivery_identity)
            self._stop_listener()
            self._worker.release_scope_lock()
            key = registry.scope_key(self._settings)
            if _LIVE.get(key) is self:
                del _LIVE[key]

    async def _pass(self):
        await self._worker.tick()
        if not self._stopping and (self._delivery is None
                or self._delivery.send_allowed(self._delivery_identity)):
            await self._actions.sweep_followups()
