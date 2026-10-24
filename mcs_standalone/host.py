"""Own supervisor task lifetimes and unload callbacks independently of Hermes."""
from __future__ import annotations

import asyncio
import inspect
import threading

from adapters.common import eventlog


class NotSent(Exception):
    """The platform provably did not accept the notification."""


class Refused(Exception):
    """The platform refused the payload before accepting it."""


class DeliveryAdmission:
    """Keep connector readiness and actual delivery passes across reconnects."""

    def __init__(self):
        self._lock = threading.Lock()
        self._open = True
        self._identity = None
        self._state = "off"
        self._active_passes = 0

    def begin_connector(self):
        with self._lock:
            if self._identity is not None:
                raise RuntimeError("delivery_connector_busy")
            self._identity = object()
            self._state = "starting"
            return self._identity

    def _current(self, identity):
        return identity is not None and identity is self._identity

    def connector_starting(self, identity):
        with self._lock:
            if self._current(identity) and self._state != "closing":
                self._state = "starting"

    def connector_ready(self, identity):
        with self._lock:
            if self._current(identity) and self._state != "closing":
                self._state = "ready"

    def connector_closing(self, identity):
        with self._lock:
            if self._current(identity):
                self._state = "closing"

    def end_connector(self, identity):
        with self._lock:
            if not self._current(identity) or self._active_passes:
                raise RuntimeError("delivery_connector_not_drained")
            self._identity = None
            self._state = "off"

    def _send_allowed(self, identity):
        return self._open and self._current(identity) and self._state == "ready"

    def send_allowed(self, identity):
        with self._lock:
            return self._send_allowed(identity)

    def begin_pass(self, identity):
        with self._lock:
            if not self._send_allowed(identity):
                return False
            self._active_passes += 1
            return True

    def end_pass(self, identity):
        with self._lock:
            if not self._current(identity) or not self._active_passes:
                raise RuntimeError("delivery_pass_not_owned")
            self._active_passes -= 1

    def close(self):
        with self._lock:
            self._open = False

    def open(self):
        with self._lock:
            self._open = True

    def snapshot(self):
        with self._lock:
            return {"open": self._open, "state": self._state,
                    "active_passes": self._active_passes, "ready": self._state == "ready"}


class Host:
    def __init__(self, settings, *, native_admission=None,
                 delivery_admission=None, delivery_identity=None):
        self.settings = {key: sorted(value) if isinstance(value, frozenset) else value
                         for key, value in settings.items()}
        self.native_admission = native_admission
        self.delivery_admission = delivery_admission
        self.delivery_identity = delivery_identity
        self._close_task = None
        self.tasks = set()
        self._unload = []
        self._closing = False

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def spawn_task(self, coroutine, *, name=None):
        if self._closing:
            coroutine.close()
            return None
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def on_unload(self, callback):
        if self._closing:
            raise RuntimeError("standalone_host_stopping")
        self._unload.append(callback)

    async def close(self):
        """All callers await the same actual cleanup, including after waiter cancellation."""
        if self._close_task is None:
            self._closing = True
            coroutine = self._cleanup()
            try:
                self._close_task = asyncio.create_task(coroutine)
            except BaseException:
                coroutine.close()
                raise
        task, cancelled = self._close_task, False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _cleanup(self):
        errors = []
        for callback in reversed(self._unload):
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except (Exception, asyncio.CancelledError) as exc:
                errors.append(exc)
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        for error in errors:
            if isinstance(error, asyncio.CancelledError):
                raise error
        if errors:
            raise RuntimeError("standalone_host_unload_failed") from None


# Fixed event codes only; adapters.common.eventlog drops anything else.
log = eventlog.make_logger("mcs.standalone")
