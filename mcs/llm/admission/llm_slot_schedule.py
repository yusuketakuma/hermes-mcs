"""Allocate MCS's three physical local slots with realtime priority and JST night borrowing."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import os
from pathlib import Path
import time

import safe_io

SLOTS = (0, 1, 2)
POLL_S = 0.05
JST = timezone(timedelta(hours=9))


def night() -> bool:
    hour = datetime.now(JST).hour
    return hour >= 20 or hour < 8


def _open(directory, name):
    fd = safe_io.open_lock_file(Path(directory) / name)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        os.close(fd)
        raise OSError("llm_slot_lock_untrusted")
    return fd


def _try(fd) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _available(directory):
    free = {}
    try:
        for slot in SLOTS:
            fd = _open(directory, f"llm_slot_{slot}.lock")
            if _try(fd):
                free[slot] = fd
            else:
                os.close(fd)
        return free
    except BaseException:
        for fd in free.values():
            os.close(fd)
        raise


@contextmanager
def turn(directory, *, realtime, preferred, deadline=None, min_s=0.0, held_slots=None,
         legacy_path=None):
    """Hold one physical slot until the existing transport finishes its turn.

    Waiting RT holds a shared intent lock. Under the short allocation lock,
    backlog yields the next free slot; an active RT leaves two slots available.
    This preserves the legacy transport's timeout handling. Completion-unknown
    protection, when enabled, remains owned by the admission broker.
    """
    gate = _open(directory, "llm_schedule.lock")
    intent = active = legacy = slot_fd = None
    chosen = None
    try:
        intent = _open(directory, "llm_realtime.lock")
        active = _open(directory, "llm_realtime_active.lock")
        path = Path(legacy_path) if legacy_path else Path(directory) / "llm_turn.lock"
        legacy = _open(path.parent, path.name)
        if realtime:
            fcntl.flock(intent, fcntl.LOCK_SH)
        while deadline is None or time.monotonic() + min_s < deadline:
            try:
                # Drain an old worker's exclusive turn; new physical turns coexist.
                fcntl.flock(legacy, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(POLL_S)
                continue
            if _try(gate):
                free = {}
                try:
                    free = _available(directory)
                    unavailable = held_slots() if held_slots is not None else ()
                    rt_active = not _try(active)
                    rt_waiting = False
                    if not realtime:
                        rt_waiting = not _try(intent)
                        if not rt_waiting:
                            fcntl.flock(intent, fcntl.LOCK_UN)
                    allowed = SLOTS if realtime or night() else (1, 2)
                    ready = not rt_active if realtime else not rt_waiting or rt_active
                    order = (0, 1, 2) if realtime else tuple(
                        dict.fromkeys((preferred, 1, 2, 0)))
                    if ready:
                        chosen = next((slot for slot in order
                                       if slot in allowed and slot in free and slot not in unavailable), None)
                    if chosen is not None:
                        slot_fd = free.pop(chosen)
                finally:
                    if not realtime or chosen is None:
                        fcntl.flock(active, fcntl.LOCK_UN)
                    for unused in free.values():
                        os.close(unused)
                    fcntl.flock(gate, fcntl.LOCK_UN)
                if chosen is not None:
                    break
            time.sleep(POLL_S if deadline is None else min(
                POLL_S, max(0.0, deadline - time.monotonic())))
        if chosen is None:
            yield None
            return
        # Recheck after allocation; an expired caller never sends.
        yield chosen if deadline is None or time.monotonic() + min_s < deadline else None
    finally:
        if slot_fd is not None:
            os.close(slot_fd)
        if legacy is not None:
            os.close(legacy)
        if active is not None:
            os.close(active)
        if intent is not None:
            os.close(intent)
        os.close(gate)
