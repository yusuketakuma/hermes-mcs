"""A file group blocked by settled attachments is recorded as finished once."""
import asyncio
from types import SimpleNamespace

import pytest

from adapters.common import envelopes, worker

THREAD = {"part_id": "th", "kind": "thread"}


def _drive(records, thread_id="t1", *, transport="slack", fresh=False, thread=False):
    done = []
    instance = object.__new__(worker.DeliveryWorker)
    instance.transport = transport
    instance._worker_id = "current"
    instance._reg = SimpleNamespace(put_parts_done=done.append)

    async def never(*_args, **_kwargs):
        raise AssertionError("a blocked group must never be attempted")

    instance._attempt_part = never
    instance._republish_part_receipt = never
    spec = {"delivery_id": "d1", "card_key": "synthetic", "delivery": {},
            "parts": {"manifest": []}}
    manifest = [{"part_id": "card", "kind": "card"}, *([THREAD] if thread else []),
                {"part_id": "a1", "kind": "attachment_part"},
                {"part_id": "g1", "kind": "file_group", "members": ["a1"]}]
    claim = {"attempt_id": "resume", "worker_id": "current", "spec": spec}
    ctx = {"card_message_id": "m1", "thread_id": thread_id, "consumed": set()}
    if fresh:
        ctx["_fresh_primary_drive"] = True
    asyncio.run(instance._drive_parts(claim, manifest, ctx, records))
    return done


def _settled(part_id, result="not_sent"):
    aid = envelopes.part_attempt_id("d1", part_id)
    return {aid: [{"phase": "result", "delivery_id": "d1", "part_id": part_id,
                   "result": result},
                  {"phase": "receipt", "delivery_id": "d1", "part_id": part_id,
                   "result": result}]}


@pytest.mark.parametrize("transport", ["slack", "lineworks"])
def test_settled_attachment_without_remote_marks_group_done(transport):
    assert _drive(_settled("a1"), transport=transport) == ["d1"]


def test_started_only_attachment_is_final_but_never_resent():
    aid = envelopes.part_attempt_id("d1", "a1")
    records = {aid: [{"phase": "started", "delivery_id": "d1", "part_id": "a1"}]}
    assert _drive(records) == ["d1"]


def test_unattempted_attachment_keeps_group_open():
    # held for its thread: it may still be attempted, so the group stays open
    assert _drive({}, thread_id=None) == []


@pytest.mark.parametrize("thread_rows", [
    _settled("th")[envelopes.part_attempt_id("d1", "th")],
    [{"phase": "started", "delivery_id": "d1", "part_id": "th"}]])
def test_final_thread_without_id_ends_the_held_group(thread_rows):
    # the thread part is never resent, so its held attachments never send
    records = {envelopes.part_attempt_id("d1", "th"): thread_rows}
    assert _drive(records, thread_id=None, thread=True) == ["d1"]


def test_fresh_primary_drive_without_thread_settles_only_final_groups():
    assert _drive(_settled("a1"), thread_id=None, fresh=True) == ["d1"]
    assert _drive({}, thread_id=None, fresh=True) == []
