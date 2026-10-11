"""Archive revocation must stop a delivered card's unsent body parts (synthetic)."""
import asyncio
import json
from pathlib import Path

import ledger
import notify_cards
import notify_cmds
import pytest
from adapters.lineworks import delivery
from hermes_plugin.mcs_delivery import registry
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread
from test_lineworks_adapter import CONFIG, world

MARK = "SYNTHETIC-REVOKED-BODY-"


def _parts(led, delivery_id):
    return [tuple(r) for r in led.db.execute(
        "SELECT kind,state,error_code FROM notification_render_parts "
        "WHERE delivery_id=? ORDER BY idx", (delivery_id,))]


@pytest.mark.parametrize("archive", [True, False])
def test_revoked_card_never_resumes_pending_body_parts(tmp_path, monkeypatch, archive):
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
        spec = json.loads(_latest_render(led)["spec_json"])
        spec_file = Path(w.dirs["render"]) / (spec["delivery_id"] + ".json")
        perform = worker._perform_part

        async def stop_after_thread(claim, part, ctx):
            out = await perform(claim, part, ctx)
            if part["kind"] == "thread":
                worker._stopping = True     # pause with body parts unsent
            return out

        def drain():
            result = {"errors": []}
            notify_cmds.drain_int_commands(led, result, CONFIG, str(data))
            assert result["errors"] == []

        async def scenario():
            worker._perform_part = stop_after_thread
            assert worker.acquire_scope_lock()
            try:
                await worker.tick()
                drain()
                await worker.tick()
                drain()
                before = _parts(led, spec["delivery_id"])
                assert before[0][:2] == ("card", "delivered")
                assert ("body_part", "pending", None) in before
                if archive:
                    led.db.execute("UPDATE patients SET is_archived=1")
                    led.db.commit()
                notify_cards.sweep(led, CONFIG, now=NOW + 1)
                # withdrawn by the revoking sweep itself, not only by a later gc
                assert spec_file.exists() is not archive
                notify_cards.gc(led, CONFIG, now=NOW + 2)
                recovered = notify_cards.recover(led, CONFIG, {})
                worker._perform_part = perform
                worker._stopping = False
                start = len(w.client.calls)
                await worker.tick()
                drain()
                return recovered, w.client.calls[start:]
            finally:
                worker.release_scope_lock()

        recovered, resumed = asyncio.run(scenario())
        after = _parts(led, spec["delivery_id"])
        leaked = [c for c in resumed if c[1].get("type") == "text" and MARK in c[1].get("text", "")]
        if archive:
            state = led.db.execute("SELECT delivery_state FROM notification_cards").fetchone()[0]
            assert state == "revoked"
            assert leaked == []
            assert not spec_file.exists()
            assert recovered["republished"] == 0
            assert [p for p in after if p[0] == "body_part"] and all(
                p[1:] == ("held", "card_revoked") for p in after if p[0] == "body_part")
            assert led.db.execute(
                "SELECT parts_state FROM notification_renders WHERE delivery_id=?",
                (spec["delivery_id"],)).fetchone()[0] == "incomplete"
        else:
            # control: an unrevoked card still finishes its body after the pause
            assert leaked
            assert all(p[1] == "delivered" for p in after)
    finally:
        led.close()
