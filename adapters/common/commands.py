"""Native Slack/LINE WORKS JSON commands using the shared MCS preview and confirmation gates."""
from __future__ import annotations

import asyncio
from pathlib import Path
import threading

from .dispatch import dispatch, parse
from .mcs_api import deny
from . import paths


NATIVE_ADMISSION_CLOSED_TEXT = (
    "新しい操作の受付を停止しています。処理状態は未確認です。"
    "既に受付済みの操作は、その結果の確認をお待ちください。")


class NativeAdmissionClosed(RuntimeError):
    """Refuse new native work with a fixed, input-free code."""
    def __init__(self):
        super().__init__('native_admission_closed')


class NativeAdmission:
    """Share a synchronous admission gate and actual queued/running work lifetime."""
    def __init__(self):
        self._lock = threading.Lock()
        self._open, self._queued, self._running = True, 0, 0

    def _snapshot(self):
        return {'open': self._open, 'queued': self._queued, 'running': self._running}

    def snapshot(self):
        with self._lock:
            return self._snapshot()

    def close(self):
        with self._lock:
            self._open = False
            return self._snapshot()

    def open(self):
        # Only the owning Runtime may explicitly reopen. Connector/Host lifecycle
        # never invokes this: a reconnect must retain its borrowed closed gate.
        with self._lock:
            self._open = True
            return self._snapshot()

    def _reserve(self):
        with self._lock:
            if not self._open:
                raise NativeAdmissionClosed()
            ticket = _NativeTicket(self)
            self._queued += 1
            return ticket


class _NativeTicket:
    def __init__(self, owner):
        self.owner, self.stage = owner, 'queued'

    def cancel_queued(self):
        with self.owner._lock:
            if self.stage == 'queued':
                self.owner._queued -= 1
                self.stage = 'done'

    def invoke(self, handler, *args, **kwargs):
        with self.owner._lock:
            if self.stage != 'queued':
                raise NativeAdmissionClosed()
            self.owner._queued -= 1
            self.owner._running += 1
            self.stage = 'running'
        try:
            return handler(*args, **kwargs)
        finally:
            with self.owner._lock:
                self.owner._running -= 1
                self.stage = 'done'


async def run_native(handler, *args, admission=None, **kwargs):
    """Own native work before executor submission until the real handler finally ends."""
    ticket = admission._reserve() if admission is not None else None
    coroutine = None
    try:
        coroutine = (asyncio.to_thread(ticket.invoke, handler, *args, **kwargs)
                     if ticket else asyncio.to_thread(handler, *args, **kwargs))
        work = asyncio.create_task(coroutine)
    except BaseException:
        if coroutine is not None:
            coroutine.close()
        if ticket is not None:
            ticket.cancel_queued()
        raise
    cancelled = False
    try:
        while not work.done():
            try:
                # Cancel only the waiter, never the queued/running actual work.
                await asyncio.wait({work})
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            if not work.cancelled():
                work.exception()
            raise asyncio.CancelledError
        return work.result()
    finally:
        # Executor submission itself can fail before the wrapper ever starts.
        # A running/done ticket is never decremented by waiter cancellation.
        if ticket is not None:
            ticket.cancel_queued()


def answer(settings, raw, *, user, channel, authorized=None):
    """Run an authenticated native command; callers deliver the answer privately."""
    transport = settings.get("transport")
    if transport not in ("slack", "lineworks"):
        return deny("native_context_rejected")
    if user not in settings.get("allowed_user_ids", ()):
        return deny("user_not_allowed")
    if channel != settings.get("channel_id"):
        return deny("chat_not_allowed")
    def current():
        if authorized is not None and authorized() is not True:
            return False
        flags = paths.read_flags(settings["data_root"])
        return paths.route_open(flags, transport, settings.get("route_epoch"))

    if not current():
        return deny("interactive_off")
    identity = {"transport": transport, "user_id": user, "chat_id": channel,
                "scope_id": settings["team_id"], "profile": settings["profile"],
                "application_id": settings["application_id"],
                "route_epoch": settings["route_epoch"]}
    scoped = {**settings, "inbox": settings.get("inbox") or str(Path(settings["data_root"]) / "cmd"),
              "allowed_chat_ids": {channel}}
    # only a parse error is the caller's; a handler bug is operation_failed,
    # as in the Hermes /mcs handler
    try:
        data = parse(raw)
    except (ValueError, UnicodeError, TypeError):
        return deny("bad_command")
    except Exception:
        return deny("operation_failed")
    if not isinstance(data, dict) or "op" not in data:
        return deny("bad_command")
    try:
        return dispatch(data, scoped, identity, authorized=current)
    except Exception:
        return deny("operation_failed")
