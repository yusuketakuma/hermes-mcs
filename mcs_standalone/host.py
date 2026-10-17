"""Own supervisor task lifetimes and unload callbacks independently of Hermes."""
from __future__ import annotations

import asyncio
import inspect

from adapters.common import eventlog


class NotSent(Exception):
    """The platform provably did not accept the notification."""


class Refused(Exception):
    """The platform refused the payload before accepting it."""


class Host:
    def __init__(self, settings):
        self.settings = {key: sorted(value) if isinstance(value, frozenset) else value
                         for key, value in settings.items()}
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
        if self._closing:
            return
        self._closing = True
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
