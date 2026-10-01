"""The small slice of Hermes's plugin context that MCS supervisors use."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging


class NotSent(Exception):
    """The platform provably did not accept anything — safe to retry."""


class Refused(Exception):
    """The platform definitively refused this payload (e.g. a file over
    the upload limit) — exit 2, so notify_flush retries text-only."""


class Host:
    """spawn_task / on_unload / get_config, owned by this process."""

    def __init__(self, config: dict):
        # plugin-config shape: id grants are lists, as Hermes YAML gives them
        self._config = {key: sorted(value) if isinstance(value, frozenset) else value
                        for key, value in config.items()}
        self._tasks: set[asyncio.Task] = set()
        self._unload: list = []

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def spawn_task(self, coroutine, *, name=None):
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def on_unload(self, callback):
        self._unload.append(callback)

    async def close(self):
        """Stop intake first, then cancel and await every supervised task
        so scope locks are released before the process exits."""
        for callback in reversed(self._unload):
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                log("unload_error", error=type(exc).__name__)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def raise_ended(done: set, waiter: asyncio.Task) -> None:
    """Surface why the connection or worker ended (stop requests excepted)
    so the process exits non-zero and launchd's log shows the cause."""
    for task in done - {waiter}:
        if not task.cancelled() and task.exception() is not None:
            raise task.exception()
        # an unrequested end (e.g. scope lock held elsewhere) is a failure:
        # launchd restarts only non-zero exits
        raise RuntimeError(f"ended:{task.get_name()}")


def log(event: str, **fields) -> None:
    """Fixed event codes only — never message bodies, ids of people or secrets."""
    logging.getLogger("mcs.standalone").info(
        "%s %s", event, json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str))
