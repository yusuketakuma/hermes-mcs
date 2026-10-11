"""A card revoked mid-pass stops the body parts still ahead in that pass.

Adopted from the Codex proof (SAME-PASS REVOKE, synthetic): the manifest
already in memory must not outrun a revocation that withdrew the spec."""
import asyncio

import ledger
import notify_cards
import notify_cmds
import pytest
from adapters.lineworks import delivery
from hermes_plugin.mcs_delivery import registry
from notify_testkit import NOW, _dispatch, _intent, _seed_thread
from test_lineworks_adapter import CONFIG, world

MARK = "SYNTHETIC-SAME-PASS-"


@pytest.mark.parametrize("archive", [True, False])
def test_revoke_after_thread_part_stops_same_pass_bodies(tmp_path, monkeypatch, archive):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    data = tmp_path / "data"
    data.mkdir()
    led = ledger.Ledger(str(data / "ledger.db"))
    try:
        _seed_thread(led)
        led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", (MARK * 120,))
        led.db.commit()
        assert _dispatch(led, _intent(led), CONFIG)["dispatched"]
        w = world(tmp_path)
        worker = delivery.DeliveryWorker(sender=w.sender, settings=w.settings, root=str(data),
                                         reg=w.reg, worker_id=registry.new_worker_id(),
                                         log=lambda *a, **kw: None)
        perform = worker._perform_part
        at_cancel = []

        async def revoke_after_thread(claim, part, ctx):
            result = await perform(claim, part, ctx)
            if part["kind"] == "thread":
                if archive:
                    led.db.execute("UPDATE patients SET is_archived=1")
                    led.db.commit()
                    notify_cards.sweep(led, CONFIG, now=NOW + 1)
                at_cancel.append(len(w.client.calls))
            return result

        def drain():
            result = {"errors": []}
            notify_cmds.drain_int_commands(led, result, CONFIG, str(data))
            assert result["errors"] == []

        async def run():
            worker._perform_part = revoke_after_thread
            assert worker.acquire_scope_lock()
            try:
                await worker.tick()
                drain()
                await worker.tick()
                drain()
            finally:
                worker.release_scope_lock()

        asyncio.run(run())
        assert at_cancel
        bodies = [c for c in w.client.calls[at_cancel[0]:]
                  if c[1].get("type") == "text" and MARK in c[1].get("text", "")]
        # revoked: nothing after the revocation; control: bodies continue
        assert (not bodies) if archive else bodies
    finally:
        led.close()
