"""Restore reconcile + lost ``file_group`` journal: commands_only
reconciles, the held delivery's spec is withdrawn, and a real
``worker.tick`` must never resend the unjournaled group — while an
unrelated delivery and a hold-free first send still go through."""
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
from adapters.slack.delivery import DeliveryWorker
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread
from slack_testkit import (ROOT_TS, SLACK, SCOPE, _attach, _granted_card,
                           _mkworld)
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


def _commands_only(led, tmp_path, monkeypatch):
    import run_check_stage_testkit
    run_check_stage_testkit._point_run_check_at(tmp_path, monkeypatch)
    monkeypatch.setattr(run_check, "_config", lambda: SLACK)
    fd = os.open(str(tmp_path / "synthetic.lock"),
                 os.O_CREAT | os.O_RDWR, 0o600)
    run_check._commands_only(fd, time.monotonic() + 60)  # closes fd itself


def _group_updates(client, remotes):
    return [kw for kind, kw in client.calls
            if kind == "update" and "file_ids" in kw
            and set(kw["file_ids"]) & set(remotes)]


def test_held_delivery_never_resends_the_lost_group(
        led, tmp_path, monkeypatch):
    world = _world(led, tmp_path, monkeypatch)
    spec = json.loads(_latest_render(led)["spec_json"])
    did = spec["delivery_id"]
    spec_path = Path(world.dirs["render"]) / f"{did}.json"
    original_spec_bytes = spec_path.read_bytes()
    group_remotes = {f"F_SYN{i}" for i, p in enumerate(
        spec["parts"]["manifest"]) if p.get("kind") == "attachment_part"}
    assert any(p.get("kind") == "file_group"
               for p in spec["parts"]["manifest"])
    _journal_everything_but_the_group(world.dirs["state"], spec)

    _seed_thread(led, pid=2, root=200, mids=(200, 201))
    _attach(led, tmp_path, mid=200, name="other-0.jpg", blob=b"other-0")
    _attach(led, tmp_path, mid=200, name="other-1.pdf", blob=b"other-1")
    other_eid = led.outbox_add(
        "new_messages", 2, {"message_ids": [200, 201]})
    led.db.execute(
        "UPDATE notify_outbox SET route='interactive', state='pending' "
        "WHERE event_id=?", (other_eid,))
    led.db.commit()

    for _ in range(2):
        notify_spool.mark_restored(str(world.root), backup_path="b.db", by="test")
        _commands_only(led, tmp_path, monkeypatch)
        assert notify_spool.restore_pending(str(world.root)) is None
        other_row = led.db.execute(
            "SELECT state, progress FROM notify_outbox WHERE event_id=?",
            (other_eid,)).fetchone()
        assert other_row["state"] == "pending"
        assert json.loads(other_row["progress"] or "{}").get("hold_reason") is None

    # the reconcile receipt and the durable hold row name the real scope
    receipt = json.loads(
        (world.root / "restore_reconcile.json").read_text())
    held = [h for h in receipt["held"] if h["delivery_id"] == did]
    assert held and all(h["reason"] and h["scope"].get("transport") == "slack"
                        for h in held)
    rows = led.db.execute(
        "SELECT delivery_id,reason,scope_json FROM notification_restore_holds "
        "WHERE released_at IS NULL AND delivery_id=?", (did,)).fetchall()
    assert rows and all(r["reason"] for r in rows)

    # A worker may still hold the original spec in memory after withdrawal.
    holds_path = world.root / "flags" / "restore_holds.json"
    assert did in json.loads(holds_path.read_bytes())["held_delivery_ids"]
    assert not asyncio.run(world.worker._send_allowed(spec))
    holds_path.write_bytes(b"{corrupt")
    assert not asyncio.run(world.worker._send_allowed())
    notify_spool.publish_restore_holds(led, str(world.root))
    assert asyncio.run(world.worker._send_allowed())
    assert not asyncio.run(world.worker._send_allowed(spec))

    # the held delivery's spec is withdrawn into the reconcile-only archive;
    # another commands_only recover pass must not republish it while unreleased
    assert not spec_path.exists()
    assert (world.root / "restore_specs" / f"{did}.json").read_bytes() == original_spec_bytes
    _commands_only(led, tmp_path, monkeypatch)
    assert not spec_path.exists()

    # real worker ticks — including a fresh worker as a restart — never
    # reach the delivery's parts and never re-emit the group update
    drove = []

    def wrap_drive_parts(worker):
        original = worker._drive_parts

        async def spy(claim, manifest, ctx, records):
            drove.append(claim["spec"]["delivery_id"])
            return await original(claim, manifest, ctx, records)

        worker._drive_parts = spy

    w = world.worker
    wrap_drive_parts(w)

    async def drive_ticks(worker):
        assert await worker._sender.bind()
        worker.acquire_scope_lock()
        try:
            for _ in range(3):
                await worker.tick()
        finally:
            worker.release_scope_lock()

    asyncio.run(drive_ticks(w))
    restarted = DeliveryWorker(
        sender=w._sender, settings=SCOPE, root=str(world.root),
        reg=registry.Registry(world.dirs["state"], scope=SCOPE),
        worker_id=registry.new_worker_id(), log=lambda *_a, **_k: None)
    wrap_drive_parts(restarted)
    asyncio.run(drive_ticks(restarted))

    assert did not in drove
    assert _group_updates(world.client, group_remotes) == []

    # the unrelated pending interactive delivery still dispatches and delivers
    other_ev = dict(led.db.execute(
        "SELECT * FROM notify_outbox WHERE event_id=?", (other_eid,)).fetchone())
    other = _dispatch(led, other_ev, SLACK)
    assert other["dispatched"]
    other_spec = json.loads(led.db.execute(
        "SELECT spec_json FROM notification_renders WHERE delivery_id!=? "
        "ORDER BY render_rev DESC LIMIT 1", (did,)).fetchone()["spec_json"])
    assert other_spec["delivery_id"] != did
    asyncio.run(_granted_card(world.worker, led, world.root))
    other_groups = [kw for kind, kw in world.client.calls
                    if kind == "update" and "file_ids" in kw
                    and set(kw["file_ids"]).isdisjoint(group_remotes)]
    assert other_groups


def test_hold_free_first_send_still_posts_the_group(led, tmp_path, monkeypatch):
    """Control: with no restore hold the same grouped delivery sends its
    file_group update exactly once through the normal grant path."""
    world = _world(led, tmp_path, monkeypatch)
    spec = json.loads(_latest_render(led)["spec_json"])
    assert any(p.get("kind") == "file_group"
               for p in spec["parts"]["manifest"])
    asyncio.run(_granted_card(world.worker, led, world.root))
    groups = [kw for kind, kw in world.client.calls
              if kind == "update" and "file_ids" in kw]
    assert len(groups) == 1
