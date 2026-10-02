"""Own supervisor task lifetimes and unload callbacks independently of Hermes."""
from __future__ import annotations

import asyncio
import inspect


class Host:
    def __init__(self, settings):
        self.settings = dict(settings)
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
            except Exception as exc:
                errors.append(exc)
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        if errors:
            raise RuntimeError("standalone_host_unload_failed") from None
