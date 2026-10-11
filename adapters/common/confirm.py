"""Shared human-confirmation publish step and followup sweep window for every transport."""
from __future__ import annotations

from . import registry

# The two preview buttons, in display order: (label, action-id suffix).
CHOICES = (("確定する", ""), ("取消", ":cancel"))


def open_confirm(reg, *, token, actor, origin, payload) -> str:
    """Store a pending preview for ``take_confirm`` and return its id."""
    confirm_id = registry.new_confirm_id()
    reg.put_confirm(confirm_id, {"token": token, "actor": actor,
                                 "origin": origin, "payload": payload})
    return confirm_id


async def publish_confirmed(reg, confirm_id, publish, queued, *,
                            on_queued, on_failed,
                            reraise_queued: bool = False) -> bool:
    """Queue a taken confirm's command; True once it is durably queued.

    The caller has already won ``take_confirm`` and saved the receipt
    followup. ``publish`` and ``queued`` are zero-argument awaitables
    (the command write and ``envelopes.command_queued``). On an
    ``OSError``/``ValueError`` the confirm stays in flight when the file
    may have landed (``on_queued``; with ``reraise_queued`` the publish
    error is raised afterwards), otherwise it is released for a retry
    (``on_failed``). Success consumes the confirm before returning.
    """
    try:
        await publish()
    except (OSError, ValueError):
        if await queued():
            # possibly applied: keep the confirm in flight and the followup
            await on_queued()
            if reraise_queued:
                raise
            return False
        reg.end_confirm(confirm_id)
        await on_failed()
        return False
    reg.consume_confirm(confirm_id)
    return True


def followup_window(reg, cursor: int, limit: int = 32) -> tuple[int, list]:
    """The next round-robin slice of followups and the advanced cursor.

    Liveness is not checked here: callers re-read ``reg.followup(cid)``
    for each item when they reach it, since earlier items await.
    """
    followups = list(reg.followups().items())
    if not followups:
        return 0, []
    start = cursor % len(followups)
    count = min(limit, len(followups))
    return ((start + count) % len(followups),
            [followups[(start + offset) % len(followups)]
             for offset in range(count)])
