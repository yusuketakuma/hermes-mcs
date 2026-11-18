"""Restore reconcile + lost ``file_group`` journal: the commands_only
entry reconciles first, then a real ``worker.tick`` must reach the
delivery without resending the group whose journal vanished while
primary/stage evidence remains."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import notify_cards
import notify_spool
import run_check
from adapters.common import envelopes, journal, registry
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread
from slack_testkit import ROOT_TS, SLACK, _attach, _mkworld
from slack_testkit import isolated_slack_ledger as led

__all__ = ["led"]


def _world(led, tmp_path, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    _seed_thread(led, mids=(100,))
    _attach(led, tmp_path, name="synthetic-0.jpg", blob=b"synthetic-0")
    _attach(led, tmp_path, name="synthetic-1.pdf", blob=b"synthetic-1")
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    return _mkworld(led)


def _journal_everything_but_the_group(state, spec):
    """Primary card plus every non-group part journaled delivered; the
    ``file_group`` part is deliberately left unjournaled — the restore
    lost exactly that evidence while primary/stage proof remains."""
    did = spec["delivery_id"]
    claim = {"attempt_id": registry.new_attempt_id(), "worker_id": "w1",
             "spec": spec, "payload_hash": envelopes.payload_hash(spec)}
    for phase, extra in (
            ("claimed", {"render_rev": spec["render_rev"], "op": spec["op"]}),
            ("begin", {"begin_envelope": envelopes.transport_begin(claim),
                       "receipt_envelope": envelopes.transport_receipt(claim, "unknown")}),
            ("started", {}),
            ("result", {"result": "delivered", "message_id": ROOT_TS}),
            ("receipt", {"result": "delivered"})):
        journal.append(state, "w1", {"phase": phase,
                                     "attempt_id": claim["attempt_id"],
                                     "delivery_id": did, **extra})
    pclaim = {"attempt_id": "c" * 16, "worker_id": "d" * 16, "spec": spec,
              "payload_hash": envelopes.payload_hash(spec)}
    for index, part in enumerate(spec["parts"]["manifest"]):
        if part.get("kind") == "file_group":
            continue
        aid = envelopes.part_attempt_id(did, part["part_id"])
        remote = {"attachment_part": f"F_SYN{index}",
                  "thread": "T_SYN", "body_part": f"B_SYN{index}"}.get(
                      part.get("kind"), f"R_SYN{index}")
        env = envelopes.part_receipt(pclaim, part, "delivered",
                                     remote_id=remote)
        for phase, extra in (
                ("started", {}),
                ("result", {"result": "delivered", "remote_id": remote,
                            "receipt_envelope": env}),
                ("receipt", {"result": "delivered"})):
            journal.append(state, "w1", {"phase": phase, "attempt_id": aid,
                                         "delivery_id": did,
                                         "part_id": part["part_id"],
                                         "kind": part.get("kind"), **extra})


def test_restore_reconcile_then_worker_tick_reaches_the_lost_group(
        led, tmp_path, monkeypatch):
    """Reproduction at this SHA: with primary/stage journal proof intact
    and only the ``file_group`` journal lost, commands_only reconciles
    (held=2, settled=3) and clears the marker — then a real worker.tick
    resumes the delivery and the unjournaled group part IS attempted:
    one ``chat_update(file_ids=...)`` reaches the transport stub. The
    runner-side hold does not reach the worker's part send, so the
    visible group update is emitted a second time."""
    world = _world(led, tmp_path, monkeypatch)
    spec = json.loads(_latest_render(led)["spec_json"])
    assert any(p.get("kind") == "file_group"
               for p in spec["parts"]["manifest"])
    _journal_everything_but_the_group(world.dirs["state"], spec)
    notify_spool.mark_restored(str(world.root), backup_path="b.db", by="test")

    # commands_only's normal entry: recover -> reconcile -> drains
    _point = __import__("run_check_stage_testkit")
    _point._point_run_check_at(tmp_path, monkeypatch)
    monkeypatch.setattr(run_check, "_config", lambda: SLACK)
    fd = os.open(str(tmp_path / "synthetic.lock"),
                 os.O_CREAT | os.O_RDWR, 0o600)
    run_check._commands_only(fd, time.monotonic() + 60)  # closes fd itself
    assert notify_spool.restore_pending(str(world.root)) is None

    # a real worker.tick reaches the delivery's remaining parts
    w = world.worker
    drove = []
    original = w._drive_parts

    async def spy(claim, manifest, ctx, records):
        drove.append(claim["spec"]["delivery_id"])
        return await original(claim, manifest, ctx, records)

    w._drive_parts = spy

    async def drive_ticks():
        assert await w._sender.bind()
        w.acquire_scope_lock()
        try:
            await w.tick()      # spent fence marks the spec dead
            await w.tick()      # resume drives the unjournaled remainder
        finally:
            w.release_scope_lock()

    asyncio.run(drive_ticks())

    assert spec["delivery_id"] in drove
    group_sends = [kw for kind, kw in world.client.calls
                   if kind == "update" and "file_ids" in kw]
    # observed: the lost group journal does not stop the resend — the
    # part is unjournaled, its stages prove remotes, and the worker's
    # pre-send spec check passes, so one file_ids update goes out again
    assert len(group_sends) == 1
    assert group_sends[0]["ts"] == ROOT_TS
    assert len(group_sends[0]["file_ids"]) == 2
