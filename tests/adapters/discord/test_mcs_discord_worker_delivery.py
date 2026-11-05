"""Synthetic mcs discord worker delivery contracts with isolated shared fixtures."""
from __future__ import annotations
import notify_recovery
import asyncio
import json
import os
import sqlite3
from contextlib import suppress
import sys
import time
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import ledger as _ledger
import notify_cards
import notify_spool
import notify_actions
import notify_cmds
from adapters.common import envelopes, journal, paths, registry, text
from adapters.common import worker as worker_mod
from adapters.discord import delivery
from discord_testkit import CFG, NOW, SETTINGS, FakeHTTP, FakeInteraction, FakeMessage, FakeThread, _deliver
from discord_testkit import discord_world as discord_world
from mcs_discord_testkit import _delivered as _delivered, _face as _face, _pin_wall_clock as _pin_wall_clock

pytestmark = pytest.mark.usefixtures("v1_thread_bodies")


def test_delivery_end_to_end(world):
    """The full A->B->A loop with the real runner answering."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        sent = await _deliver(world, worker)
        assert len(sent) == 1
        msg = sent[0]
        # companion thread receipt went through cmd_int too
        world.drain()
        return msg

    msg = asyncio.run(run())
    card = world.card()
    assert card["delivery_state"] == "delivered"
    assert card["message_id"] == str(msg.id)
    assert card["thread_id"] == "7700"
    # journal proves ordering: started precedes result precedes receipt
    rec = journal.scan(str(world.data / "discord_state"))
    phases = [r["phase"] for rows in rec.values() for r in rows]
    for p in ("claimed", "begin", "granted", "started", "result",
              "receipt"):
        assert p in phases
    assert phases.index("started") < phases.index("result") \
        < phases.index("receipt")
    # token context was registered at claim — usable before the snapshot
    _, spec = world.spec()
    tok = world.token(spec, "ack")
    assert reg.token(tok)["action"] == "ack"


def test_restore_after_send_holds_until_reconcile(world):
    """DB restored to just-before-send: the journal + spec file prove
    the card delivered remotely, so the rewound DB must not resend —
    grants deny while restore_pending, reconcile holds the scope, and
    only an operator rebind (remote receipt verified) releases it."""
    import uuid

    import notify_reconcile

    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    channel = bot.channels[42]

    # backup BEFORE the send — predates begin/send/receipt entirely
    backup = world.data / "ledger-backup.db"
    dst = sqlite3.connect(str(backup))
    world.led.db.backup(dst)
    dst.close()

    async def run():
        sent = await _deliver(world, worker)
        assert len(sent) == 1
        return sent[0]
    msg = asyncio.run(run())
    assert world.card()["delivery_state"] == "delivered"
    delivery_id = world.spec()[0]["delivery_id"]

    # rewind: the live DB is replaced by the pre-send backup; the
    # journal + spec file + remote message all survive the restore
    world.led.close()
    os.replace(backup, world.data / "ledger.db")
    world.led = _ledger.Ledger(str(world.data / "ledger.db"))
    notify_spool.mark_restored(str(world.data), backup_path=str(backup),
                               by="test")
    notify_spool.publish_flags(CFG, str(world.data))

    # a restarted worker sees restore_pending and never even claims
    worker2, reg2, _ = world.mkworker(bot=bot)

    async def tick_and_drain():
        await worker2.tick()
        notify_cmds.drain_int_commands(world.led, {}, CFG,
                                       str(world.data))
        await worker2.tick()
        notify_cmds.drain_int_commands(world.led, {}, CFG,
                                       str(world.data))

    asyncio.run(tick_and_drain())
    assert len(channel.sent) == 1          # zero NEW sends
    assert not reg2.claims()

    rep = notify_reconcile.reconcile_after_restore(world.led, CFG)
    assert rep["counts"].get("held") == 1
    assert notify_spool.restore_pending(str(world.data)) is None
    notify_spool.publish_flags(CFG, str(world.data))

    # reconcile done but the lost delivery stays held — a re-claim is
    # denied by the held render, so still no resend
    asyncio.run(tick_and_drain())
    assert len(channel.sent) == 1
    assert world.led.db.execute(
        "SELECT state FROM notification_renders WHERE delivery_id=?",
        (delivery_id,)).fetchone()["state"] == "held"
    card = dict(world.led.db.execute(
        "SELECT * FROM notification_cards WHERE card_id=1"
        ).fetchone())
    assert card["delivery_state"] == "delivery_unknown"

    # operator verifies the remote receipt and rebinds — the effect
    # stays delivered, holds release, and nothing ever resent
    aid = next(iter(journal.scan(str(world.data / "discord_state"))))
    req = {"version": 1, "cmd": "ops.card_resolve",
           "command_id": str(uuid.uuid4()), "actor": "op-user",
           "human_confirmed": True,
           "reason": "remote message verified on channel",
           "delivery_id": delivery_id, "attempt_id": aid,
           "result": "mark_delivered",
           "profile": "mcs", "application_id": "1", "guild_id": "7",
           "channel_id": "42", "message_id": str(msg.id),
           "evidence": {"method": "remote_receipt",
                        "ref": f"msg:{msg.id}"}}
    out = notify_recovery.apply_card_resolve(world.led, req, CFG)
    assert out["outcome"] == "applied"
    card = dict(world.led.db.execute(
        "SELECT * FROM notification_cards WHERE card_id=1"
        ).fetchone())
    assert card["delivery_state"] == "delivered"
    assert card["message_id"] == str(msg.id)
    assert not world.led.db.execute(
        "SELECT 1 FROM notification_restore_holds "
        "WHERE released_at IS NULL").fetchone()
    asyncio.run(tick_and_drain())
    assert len(channel.sent) == 1          # still exactly one send


def test_unmarked_restore_after_tombstone_expiry_never_resends(world):
    """A DB restored without the restore_pending marker, after the dead
    tombstone expired: the runner sees a queued render and would grant
    it again — the worker's journal alone fences the re-claim."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    channel = bot.channels[42]
    backup = sqlite3.connect(":memory:")
    world.led.db.backup(backup)                 # pre-grant snapshot
    asyncio.run(_deliver(world, worker))
    assert len(channel.sent) == 1
    render, _ = world.spec()
    delivery_id = render["delivery_id"]
    spec_path = world.data / "discord_render" / (delivery_id + ".json")
    raw = spec_path.read_bytes()

    # the terminal spec is gc'd and the tombstone outlives DEAD_TTL_S
    spec_path.unlink()
    reg._data["dead"][delivery_id] = \
        time.time() - registry.DEAD_TTL_S - 1
    reg.expire()
    reg.save(immediate=True)
    assert not reg.is_dead(delivery_id)

    # rewind with no marker; the queued render's spec is live again
    backup.backup(world.led.db)
    backup.close()
    assert notify_spool.restore_pending(str(world.data)) is None
    spec_path.write_bytes(raw)

    worker2, reg2, _ = world.mkworker(bot=bot)

    async def run():
        for _ in range(2):
            await worker2.tick()
            world.drain()
    asyncio.run(run())
    assert len(channel.sent) == 1               # never a second post
    assert reg2.is_dead(delivery_id) and not reg2.claims()
    assert ("restore_suspect", {"delivery_id": delivery_id}) in world.logs
    assert spec_path.exists()                   # left for reconcile
    assert world.led.db.execute(
        "SELECT COUNT(*) FROM notification_delivery_attempts"
    ).fetchone()[0] == 0


def test_delivery_denied_revoked_card(world):
    """Revoke between dispatch and claim -> the runner denies, the
    worker never touches Discord."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    notify_cards.revoke_card(world.led.db, 1, NOW)
    world.led.db.commit()

    async def run():
        await worker.tick()
        world.drain()
        await worker.tick()

    asyncio.run(run())
    assert not bot.channels[42].sent
    assert not reg.claims()
    # the runner denied the begin (render_cancelled) and deleted the
    # dead spec — the attempt is durably settled not_sent, no Discord
    # call ever happened
    attempt = world.led.db.execute(
        "SELECT state,error_code FROM notification_delivery_attempts"
    ).fetchone()
    assert attempt["state"] == "not_sent"
    assert attempt["error_code"].startswith("denied_")


def test_delivery_foreign_scope_ignored(world):
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    worker._settings = {**SETTINGS, "channel_id": "999"}

    async def run():
        await worker.tick()

    asyncio.run(run())
    assert not reg.claims() and not bot.channels[42].sent


def test_in_flight_retry_mints_fresh_envelope(world, monkeypatch):
    """A denied_in_flight re-begin must publish a NEW envelope with a
    NEW attempt_id — replaying the stored begin_env just replays the
    same denial forever."""
    monkeypatch.setattr(worker_mod, "RETRY_IN_FLIGHT_S", 0)
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        await worker.tick()             # claim + begin published
        render, spec = world.spec()
        claim = reg.claimed(spec["delivery_id"])
        first_cid = claim["begin_cid"]
        first_attempt = claim["attempt_id"]
        # a rival attempt owns the card — fabricate the runner's denial
        result = {"granted": False, "error": "denied_in_flight",
                  "command_id": first_cid, "attempt_id": first_attempt,
                  "delivery_id": spec["delivery_id"]}
        (world.data / "cmd_results"
         / (paths.safe_name(first_cid) + ".json")).write_text(
             json.dumps(result))
        await worker.tick()             # denial -> re-attempt staged
        claim = reg.claimed(spec["delivery_id"])
        assert claim["attempt_id"] != first_attempt
        assert claim["begin_env"] is None
        await worker.tick()             # retry -> fresh envelope out
        return first_cid, first_attempt, claim, spec

    first_cid, first_attempt, claim, spec = asyncio.run(run())
    begins = [json.loads(p.read_text())
              for p in (world.data / "cmd_int").glob("*.json")
              if json.loads(p.read_text()).get("op")
              == "transport_begin"]
    by_cid = {b["command_id"]: b for b in begins}
    assert first_cid in by_cid
    new = by_cid[claim["begin_cid"]]
    assert claim["begin_cid"] != first_cid
    assert new["attempt_id"] == claim["attempt_id"] != first_attempt


def test_in_flight_denial_at_the_retry_cap_drops_the_claim(world, monkeypatch):
    """The last denied_in_flight before MAX_BEGIN_RETRIES drops the claim
    dead — staging one more re-begin would leave begin_cid unset with the
    republish branch already capped, so nothing is sent or dropped."""
    monkeypatch.setattr(worker_mod, "RETRY_IN_FLIGHT_S", 0)
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        await worker.tick()             # claim + begin published
        render, spec = world.spec()
        did = spec["delivery_id"]
        claim = reg.claimed(did)
        # an odd count: one ordinary 60 s republish happened earlier
        reg.claim(did, {**claim,
                        "begin_retries": worker_mod.MAX_BEGIN_RETRIES - 1})
        result = {"granted": False, "error": "denied_in_flight",
                  "command_id": claim["begin_cid"],
                  "attempt_id": claim["attempt_id"], "delivery_id": did}
        (world.data / "cmd_results"
         / (paths.safe_name(claim["begin_cid"]) + ".json")).write_text(
             json.dumps(result))
        await worker.tick()
        await worker.tick()
        return did

    did = asyncio.run(run())
    assert reg.claimed(did) is None and reg.is_dead(did)
    assert not bot.channels[42].sent


def test_kill_switch_skips_claim_then_recovers(world):
    """interactive:false in flags -> no claim at all (no denial churn,
    no tombstone); flipping back on -> the same spec delivers."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    flags = world.data / "flags" / "notify.json"
    flags.write_text(json.dumps({"interactive": False}))

    async def run():
        await worker.tick()
        assert not reg.claims() and not bot.channels[42].sent
        flags.write_text(json.dumps({"interactive": True, "route_epoch": 1}))
        await worker.tick()             # claim + begin
        world.drain()                   # grant
        await worker.tick()             # send + receipt
        world.drain()

    asyncio.run(run())
    assert len(bot.channels[42].sent) == 1
    assert world.card()["delivery_state"] == "delivered"
    assert not reg.claims()


@pytest.mark.parametrize("held_flags", [
    {"interactive": False}, {"interactive": True, "restore_pending": True},
    {"interactive": True, "transport": "slack"},
    {}, {"interactive": "true"},
])
def test_send_grant_waits_for_current_flags(world, held_flags):
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    flags = world.data / "flags" / "notify.json"

    async def run():
        await worker.tick()
        world.drain()  # Grant exists before the stop flag changes.
        flags.write_text(json.dumps(held_flags))
        await worker.tick()
        assert not bot.channels[42].sent
        assert reg.claims()
        flags.write_text(json.dumps({"interactive": True, "route_epoch": 1}))
        await worker.tick()
        world.drain()
        assert len(bot.channels[42].sent) == 1

    asyncio.run(run())


def test_interactive_off_denial_stays_claimable(world):
    """If a begin is denied interactive_off (the flags file raced), the
    claim releases WITHOUT a dead tombstone — after the switch lifts the
    same spec claims and delivers."""
    cfg_off = {"notify": {**CFG["notify"], "interactive": ""}}
    world.seed()
    world.dispatch()                    # spec published while on
    worker, reg, bot = world.mkworker()
    did = world.spec()[0]["delivery_id"]

    async def run():
        await worker.tick()             # claim + begin
        notify_cmds.drain_int_commands(world.led, {}, cfg_off,
                                       str(world.data))
        await worker.tick()             # denied_interactive_off
        assert reg.claimed(did) is None
        assert not reg.is_dead(did)     # transient — not tombstoned
        await worker.tick()             # re-claim + re-begin
        world.drain()                   # on again -> granted
        await worker.tick()             # send + receipt
        world.drain()

    asyncio.run(run())
    assert len(bot.channels[42].sent) == 1
    assert world.card()["delivery_state"] == "delivered"


def test_recovery_pre_http_granted(world):
    """Worker A claims + begins, then dies holding the grant. Worker B
    never reuses A's grant (worker_id is lifecycle-bound): it reports
    A's attempt not_sent, the runner re-renders, and B delivers the
    fresh spec — exactly one message on Discord."""
    world.seed()
    world.dispatch()
    w1, reg, bot = world.mkworker()

    async def run():
        await w1.tick()                 # claim + begin published
        world.drain()                   # grant lands in cmd_results
        # worker A dies here — its claim + grant are orphaned; B is a
        # fresh process on the same Discord channel (shared FakeBot)
        w2, reg2, _ = world.mkworker(bot=bot)
        stats = await w2.reconcile()    # A's unfinished attempt
        assert stats["not_sent"] == 1
        world.drain()                   # settles not_sent -> re-render
        await w2.tick()                 # claims the fresh spec
        world.drain()                   # granted under B's worker_id
        await w2.tick()                 # send + receipt
        world.drain()                   # settle
        return reg2

    reg2 = asyncio.run(run())
    assert len(bot.channels[42].sent) == 1    # exactly once — no resend
    assert world.card()["delivery_state"] == "delivered"
    assert not reg2.claims()


def test_recovery_unfinished_and_unreported(world):
    """Hand-built crash journals: pre-HTTP -> not_sent, post-HTTP ->
    unknown, result-without-receipt -> republished verbatim."""
    world.seed()
    world.dispatch()
    _, spec = world.spec()
    state = str(world.data / "discord_state")
    claim = {"attempt_id": "cd" * 8, "worker_id": "ef" * 8,
             "spec": spec, "payload_hash": envelopes.payload_hash(spec),
             "spec_path": "x", "phase": "granted"}
    # worker A: begin + granted + started, then dies mid-HTTP
    journal.append(state, "w1", {"phase": "begin",
                                 "attempt_id": "cd" * 8,
                                 "delivery_id": spec["delivery_id"],
                                 "receipt_envelope":
                                     envelopes.transport_receipt(
                                         claim, "unknown")})
    journal.append(state, "w1", {"phase": "started",
                                 "attempt_id": "cd" * 8,
                                 "delivery_id": spec["delivery_id"]})
    # worker B: result recorded, receipt never published
    journal.append(state, "w2", {"phase": "begin",
                                 "attempt_id": "12" * 8,
                                 "delivery_id": spec["delivery_id"],
                                 "receipt_envelope":
                                     envelopes.transport_receipt(
                                         {**claim,
                                          "attempt_id": "12" * 8},
                                         "unknown")})
    journal.append(state, "w2", {"phase": "result",
                                 "attempt_id": "12" * 8,
                                 "delivery_id": spec["delivery_id"],
                                 "result": "delivered",
                                 "message_id": "9555"})

    w3, reg3, bot3 = world.mkworker()

    async def run():
        return await w3.reconcile()

    stats = asyncio.run(run())
    assert stats["unknown"] == 1 and stats["receipt_republished"] == 1
    # the republished envelopes are the journal's facts, verbatim
    sent = [json.loads(p.read_text())
            for p in (world.data / "cmd_int").glob("*.json")]
    by_attempt = {e["attempt_id"]: e for e in sent}
    assert by_attempt["cd" * 8]["result"] == "unknown"
    assert by_attempt["cd" * 8]["error_code"] == "worker_crash"
    assert by_attempt["12" * 8]["result"] == "delivered"
    assert by_attempt["12" * 8]["message_id"] == "9555"
    # The runner has not reached either begin. Keep both factual
    # witnesses intact until their attempts exist; absence cannot
    # establish a rejection or permission to resend the original spec.
    witnesses = {p.name: p.read_bytes()
                 for p in (world.data / "cmd_int").glob("*.json")}
    assert len(witnesses) == 2
    assert reg3.is_dead(spec["delivery_id"])

    async def tick_with_runner():
        task = asyncio.create_task(w3.tick())
        try:
            for _ in range(500):
                world.drain()
                if task.done():
                    break
                await asyncio.sleep(0.01)
            assert task.done(), "worker tick did not finish with the synthetic runner"
            await task
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        world.drain()

    assert world.drain() == 0
    for _ in range(2):
        # Parts have their own grant/receipt bookkeeping. Pump the
        # runner as in deployment, while following the original two
        # primary witnesses separately from those legitimate commands.
        asyncio.run(tick_with_runner())
        for name, raw in witnesses.items():
            assert (world.data / "cmd_int" / name).read_bytes() == raw
            command_id = json.loads(raw)["command_id"]
            assert not (world.data / "cmd_results" / (command_id + ".json")).exists()
        assert bot3.channels[42].sent == []
        assert not reg3.claims()
        assert world.led.db.execute(
            "SELECT count(*) FROM notification_delivery_attempts").fetchone()[0] == 0


def test_update_op_edits_bound_message(world):
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        sent = await _deliver(world, worker)
        msg = sent[0]
        # force a re-render via refresh — the runner issues an update
        # spec addressed at the bound message_id
        env = envelopes.refresh(
            "discord:1001",
            {"profile": "mcs", "application_id": "1", "guild_id": "7",
             "channel_id": "42", "message_id": str(msg.id)})
        envelopes.publish_command(
            str(world.data / "cmd_int"), env)
        world.drain()
        await _deliver(world, worker)
        return msg

    msg = asyncio.run(run())
    assert msg.edits == 1 and msg.view is not None


def test_revoke_op_deletes_message(world):
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        sent = await _deliver(world, worker)
        msg = sent[0]
        # the real revoke path: archive detected at sweep -> revoke_card
        # -> _issue_render emits the delete spec (delivered + revoked)
        world.led.db.execute(
            "UPDATE patients SET is_archived=1 WHERE project_id=1")
        world.led.db.commit()
        notify_cards.sweep(world.led, CFG)
        await _deliver(world, worker)
        return msg

    msg = asyncio.run(run())
    assert msg.deleted
    assert world.card()["delivery_state"] == "revoked"


def test_404_on_update_marks_not_sent(world):
    """A definitive 404 settles not_sent — the runner then re-renders
    as a fresh create instead of looping edits at a dead message."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        sent = await _deliver(world, worker)
        msg = sent[0]
        del bot.channels[42].messages[msg.id]   # message vanished
        env = envelopes.refresh(
            "discord:1001",
            {"profile": "mcs", "application_id": "1", "guild_id": "7",
             "channel_id": "42", "message_id": str(msg.id)})
        envelopes.publish_command(
            str(world.data / "cmd_int"), env)
        world.drain()
        await worker.tick()
        world.drain()
        await worker.tick()
        world.drain()
        # next render after message_deleted is a fresh create
        await worker.tick()
        world.drain()
        await worker.tick()
        world.drain()

    asyncio.run(run())
    card = world.card()
    # the 404 settled not_sent -> message_deleted -> one fresh create,
    # which is delivered and rebinds the card to the new message
    assert card["delivery_state"] == "delivered"
    sent = bot.channels[42].sent
    assert len(sent) == 2
    assert str(card["message_id"]) == str(sent[1].id)
    rec = journal.scan(str(world.data / "discord_state"))
    results = [r["result"] for rows in rec.values() for r in rows
               if r.get("phase") == "result" and not r.get("part_id")]
    assert results.count("not_sent") == 1
    assert not any(r == "unknown" for r in results)


# ---------- D3: notification_receipt via /mcs (RC27) -------------------------

def test_mcs_notification_receipt_scoped(world):
    """The card-UX receipt query is reachable through /mcs after the
    interaction token dies — narrowed to the caller's actor, scope and
    projects; operator receipts and foreign actors stay hidden."""
    import hermes_plugin
    import ledger as _ledger2

    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    tok = world.token(spec, "ack")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    cid = f"{tok}:{envelopes.actor_hash('discord:1001')}"
    # an operator-only receipt and a digest-shaped (NULL project) row
    world.led.db.execute(
        "INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
        ("00000000-0000-4000-8000-0000000000e1", "f" * 64, None, None,
         "applied",
         json.dumps({"kind": "ops.card_resolve", "actor": "op-user",
                     "outcome": "applied"}), NOW))
    world.led.db.execute(
        "INSERT INTO command_receipts VALUES(?,?,?,?,?,?,?)",
        ("00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:digest",
         "e" * 64, None, None, "applied",
         json.dumps({"kind": "notification", "actor": "discord:1001",
                     "outcome": "applied", "origin": {
                         "profile": "mcs", "application_id": "1",
                         "guild_id": "7", "channel_id": "42",
                         "message_id": str(msg.id)},
                     "projects": []}), NOW))
    world.led.db.commit()

    snap = world.data / "snap"
    snap.mkdir()
    assert _ledger2.publish_snapshot(
        str(world.data / "ledger.db"), str(snap))

    class FakeCtx:
        def get_config(self, key, default=None):
            cfg = {"snapshot": str(snap / "ledger-snapshot.db"),
                   "inbox": str(world.data / "cmd"),
                   "allowed_user_ids": ["1001", "2002"],
                   "allowed_chat_ids": ["42", "99"],
                   "project_ids": [1],
                   "application_id": "1", "guild_id": "7"}
            return cfg.get(key, default)

    handler = hermes_plugin._make_handler(FakeCtx())

    def ask(user="1001", chat="42", profile="mcs", **kw):
        ctx = {"platform": "discord", "authorized": True,
               "internal": False, "is_bot": False,
               "via_upstream_relay": False, "native_input": True,
               "user_id": user, "chat_id": chat,
               "scope_id": None, "profile": profile,
               "message_id": None}
        data = {"op": "read", "kind": "notification_receipt",
                "command_id": cid, **kw}
        return json.loads(handler(json.dumps(data), ctx))

    res = ask()
    assert res["ok"] is True
    assert res["result"]["outcome"] == "applied"
    # digest-style receipt — NULL project is not an error
    res = ask(command_id="00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:digest")
    assert res["ok"] and res["result"]["outcome"] == "applied"
    # snapshot lag reports as not_processed, never failure
    res = ask(command_id="deadbeef")
    assert res["result"]["outcome"] == "not_processed_or_not_in_snapshot"
    # hash tamper -> conflict
    res = ask(payload_hash="0" * 64)
    assert res["result"]["error"] == "command_id_conflict"
    # another actor -> refused
    res = ask(user="2002")
    assert res["result"]["error"] == "actor_mismatch"
    # another allowed channel -> scope refused
    res = ask(chat="99")
    assert res["result"]["error"] == "scope_mismatch"
    # another profile -> refused
    res = ask(profile="other")
    assert res["result"]["error"] == "scope_mismatch"
    # operator-only receipts stay operator-only
    res = ask(command_id="00000000-0000-4000-8000-0000000000e1")
    assert res["result"]["error"] == "operator_only"
    # unlisted user -> refused at the gate
    res = ask(user="9999")
    assert res["ok"] is False and res["error"] == "user_not_allowed"


# ---------- D3: revoke/corrupt-spec fixes ------------------------------------

def test_revoke_delete_404_is_delivered(world):
    """fetch succeeds but delete finds the message already gone —
    the revoke goal holds, so the attempt settles delivered, not
    a misleading not_sent that leaves the message bound."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        sent = await _deliver(world, worker)
        msg = sent[0]
        msg.deleted = True            # gone by the time delete lands
        world.led.db.execute(
            "UPDATE patients SET is_archived=1 WHERE project_id=1")
        world.led.db.commit()
        notify_cards.sweep(world.led, CFG)
        await _deliver(world, worker)

    asyncio.run(run())
    assert world.card()["delivery_state"] == "revoked"
    attempt = world.led.db.execute(
        "SELECT state FROM notification_delivery_attempts "
        "ORDER BY attempt_id DESC LIMIT 1").fetchone()
    assert attempt["state"] == "delivered"


@pytest.mark.parametrize("contents", ["{not json", "[" * 10000 + "]" * 10000],
                         ids=["syntax", "deep_nesting"])
def test_corrupt_spec_quarantined(world, contents, recursion_limited_json):
    """A readable spec file that fails to parse is permanent
    corruption (publication is atomic) — quarantine it like a corrupt
    cmd_int instead of re-reading it every tick."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    bad = world.data / "discord_render" / \
        "00000000-0000-4000-8000-00000000dead.json"
    bad.write_text(contents)

    async def run():
        await worker.tick()

    asyncio.run(run())
    assert not bad.exists()
    assert (world.data / "discord_render"
            / (bad.name + ".invalid")).exists()
    assert any(e == "spec_corrupt" for e, _ in world.logs)


# ---------- D4: stale claim reclaim (RC11) ------------------------------------

def test_stale_claim_marker_reclaimed(world):
    """RC11 — a worker that died between writing .claimed and the
    registry claim leaves an orphan marker; the spec must not sit
    forever. The new lock-holder reclaims markers older than
    CLAIM_STALE_S; a fresh orphan is left for one more tick."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    _, spec = world.spec()
    marker = (world.data / "discord_render"
              / (spec["delivery_id"] + ".json.claimed"))
    marker.write_text(json.dumps({"worker_id": "dead-worker",
                                  "attempt_id": "ab" * 8,
                                  "at": NOW}))

    async def run():
        await worker.tick()          # fresh marker -> not reclaimed yet
        assert reg.claimed(spec["delivery_id"]) is None
        assert not bot.channels[42].sent
        old = time.time() - worker_mod.CLAIM_STALE_S - 1
        os.utime(marker, (old, old))
        sent = await _deliver(world, worker)
        assert len(sent) == 1

    asyncio.run(run())
    assert world.card()["delivery_state"] == "delivered"
    assert any(e == "claim_stale_reclaimed" for e, _ in world.logs)


# ---------- D4: begin republish idempotency (RC09) ----------------------------

def test_begin_republishes_same_envelope(world, monkeypatch):
    """RC09 — a begin whose result never arrives republishes the SAME
    envelope: deterministic command_id lands on the same file and the
    runner's idempotent replay answers the existing attempt."""
    monkeypatch.setattr(worker_mod, "REPUBLISH_BEGIN_S", 0)
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        await worker.tick()              # claim + begin published
        _, spec = world.spec()
        claim = reg.claimed(spec["delivery_id"])
        cid = claim["begin_cid"]
        await worker.tick()              # no result -> republish
        await worker.tick()              # again — still the same file
        files = sorted((world.data / "cmd_int").glob("*.json"))
        envs = [json.loads(f.read_text()) for f in files]
        begins = [e for e in envs if e["op"] == "transport_begin"]
        assert begins and all(e["command_id"] == cid for e in begins)
        # the drained result applies normally — no conflict
        world.drain()
        claim2 = reg.claimed(spec["delivery_id"])
        assert claim2["attempt_id"] == claim["attempt_id"]

    asyncio.run(run())
    attempt = world.led.db.execute(
        "SELECT state FROM notification_delivery_attempts"
    ).fetchone()
    assert attempt["state"] == "granted"


# ---------- D4: thread failure separation (RC18) ------------------------------

def test_thread_failure_keeps_body(world, monkeypatch):
    """RC18 — a definitive thread-create failure (403) negative-caches
    the capability and reports via thread_receipt; the card body still
    settles delivered and is never resent."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def boom(self, name=None):
        raise FakeHTTP(403)
    monkeypatch.setattr(FakeMessage, "create_thread", boom)

    async def run():
        sent = await _deliver(world, worker)
        assert len(sent) == 1
        world.drain()                  # thread_receipt settles

    asyncio.run(run())
    card = world.card()
    assert card["delivery_state"] == "delivered"
    assert card["thread_state"] == "failed"
    assert card["thread_id"] is None
    assert len(bot.channels[42].sent) == 1      # no body resend
    scope_key = registry.scope_key(worker.scope())
    assert reg.capability(scope_key)["ok"] is False


def test_thread_capability_recovers_after_expiry(world, monkeypatch):
    """RC18 — the negative capability cache ages out, so a permission
    repair lets the NEXT card create its thread — without resending or
    resurrecting anything."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    original = FakeMessage.create_thread

    async def boom(self, name=None):
        raise FakeHTTP(403)
    monkeypatch.setattr(FakeMessage, "create_thread", boom)

    async def run():
        await _deliver(world, worker)
        world.drain()

    asyncio.run(run())
    assert world.card()["thread_state"] == "failed"

    # permission repaired + negative cache aged out
    monkeypatch.setattr(FakeMessage, "create_thread", original)
    monkeypatch.setattr(registry, "CAPABILITY_NEG_S", 0)

    # second card on the same patient — new root, no patient re-insert
    for m in (200, 201):
        world.led.db.execute(
            "INSERT INTO messages(message_id,project_id,sender_name,"
            "posted_at,posted_at_ts,body_text,content_hash,body_state,"
            "parent_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (m, 1, "職員", f"2026-09-24T09:{m % 60:02d}",
             int(NOW) + m, "本文", f"{m:064x}", "full",
             200 if m != 200 else None))
    world.led.db.commit()

    async def run2():
        world.dispatch(payload={"message_ids": [200, 201]})
        sent = await _deliver(world, worker)
        assert len(sent) == 2
        world.drain()

    asyncio.run(run2())
    card2 = world.card(2)
    assert card2["delivery_state"] == "delivered"
    assert card2["thread_state"] == "created"
    assert card2["thread_id"] is not None
    # the first card's body was never resent nor its thread resurrected
    assert world.card(1)["thread_state"] == "failed"


def test_thread_opens_with_body_and_card_retains_detail_button(world):
    """The companion thread and the private detail view remain accessible."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        sent = await _deliver(world, worker)
        assert len(sent) == 1
        world.drain()

    asyncio.run(run())
    msg = bot.channels[42].sent[0]
    assert msg.threads
    thread = msg.threads[0][1]
    assert thread.sent                       # body chunks in the thread
    assert "本文" in "\n".join(thread.sent)
    _, spec = world.spec()
    ids = {b["id"] for row in spec["parts"]["action_rows"] for b in row}
    assert "body" in ids
    assert {"ack", "assign", "summary", "link"} <= ids
    assert not {"defer", "tasks"} & ids       # retired / no open tasks
    assert spec["parts"].get("thread_body_parts")   # durable part text


def test_body_button_survives_without_card_thread(world):
    """card_thread off -> no thread exists to carry the text, so 📄
    stays the only way to reach it and must keep shipping."""
    world.seed()
    cfg = {"notify": {k: v for k, v in CFG["notify"].items()
                      if k != "card_thread"},
           "signals": CFG["signals"]}
    world.dispatch(cfg=cfg)
    _, spec = world.spec()
    ids = {b["id"] for row in spec["parts"]["action_rows"] for b in row}
    assert "body" in ids
    assert "thread_body" not in spec["parts"]


def test_thread_body_send_failure_only_logs(world, monkeypatch):
    """A chunk-send failure inside the fresh thread is a durable part
    outcome, never a delivery fault — the card and thread stay settled
    while every body part records its honest 'unknown' (a 500 can
    have committed; it is never resent)."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        original = FakeThread.send

        async def boom(self, content, allowed_mentions=None):
            raise FakeHTTP(500)
        monkeypatch.setattr(FakeThread, "send", boom)
        sent = await _deliver(world, worker)
        assert len(sent) == 1
        world.drain()
        monkeypatch.setattr(FakeThread, "send", original)

    asyncio.run(run())
    card = world.card()
    assert card["delivery_state"] == "delivered"
    assert card["thread_state"] == "created"
    parts = world.led.db.execute(
        "SELECT kind,state FROM notification_render_parts "
        "ORDER BY idx").fetchall()
    assert [p["kind"] for p in parts][:2] == ["card", "thread"]
    assert parts[0]["state"] == "delivered"
    assert parts[1]["state"] == "delivered"
    bodies = [p["state"] for p in parts if p["kind"] == "body_part"]
    assert bodies and set(bodies) == {"unknown"}
    render = world.led.db.execute(
        "SELECT parts_state FROM notification_renders").fetchone()
    assert render["parts_state"] == "incomplete"
    errors = [r["error_code"] for r in world.led.db.execute(
        "SELECT error_code FROM notification_render_parts WHERE kind='body_part'")]
    assert errors and set(errors) == {"http_500"}
    before = world.led.db.execute("SELECT count(*) FROM notification_renders").fetchone()[0]
    notify_actions.apply_refresh(world.led, {
        "command_id": "00000000-0000-4000-8000-00000000f001", "actor": "discord:1001",
        "origin": {"profile": "mcs", "application_id": "1", "guild_id": "7",
                   "channel_id": "42", "message_id": world.card()["message_id"]}}, CFG)
    assert world.led.db.execute("SELECT count(*) FROM notification_renders").fetchone()[0] == before
    assert len(bot.channels[42].sent) == 1


def test_unknown_never_resends_late_success_binds_update(world):
    """RC23 — an attempt that went unknown (post-HTTP uncertainty)
    keeps exclusive send rights: the render is never re-issued until
    ops.card_resolve lands. A late proven-success binds the discovered
    message and the NEXT render is an update on it — the old render is
    not resurrected."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        # force the send outcome to 'unknown' — a timeout that may
        # have committed
        async def hang(view=None):
            raise TimeoutError("no response")
        worker._bot.channels[42].send = hang
        await worker.tick()
        world.drain()
        await worker.tick()            # send raises -> unknown
        world.drain()                  # unknown receipt settles

    asyncio.run(run())
    card = world.card()
    assert card["delivery_state"] == "delivery_unknown"
    # the render stays terminal — no auto re-issue
    r = world.led.db.execute(
        "SELECT COUNT(*) c FROM notification_renders").fetchone()
    assert r["c"] == 1
    notify_cards.sweep(world.led, CFG)
    assert world.led.db.execute(
        "SELECT COUNT(*) c FROM notification_renders"
    ).fetchone()["c"] == 1

    # operator proves the send actually landed — mark_delivered binds
    # the discovered message instead of reviving the dead render
    attempt = world.led.db.execute(
        "SELECT attempt_id,delivery_id FROM notification_delivery_attempts"
    ).fetchone()
    res = notify_recovery.apply_card_resolve(world.led, {
        "version": 1, "cmd": "ops.card_resolve",
        "command_id": "00000000-0000-4000-8000-0000000000aa",
        "actor": "op-user", "human_confirmed": True,
        "reason": "found the message in channel history",
        "delivery_id": attempt["delivery_id"],
        "attempt_id": attempt["attempt_id"],
        "result": "mark_delivered", "message_id": "m-late",
        "profile": "mcs", "application_id": "1", "guild_id": "7",
        "channel_id": "42",
        "evidence": {"method": "api_lookup", "ref": "history-scan"}},
        CFG, now=NOW)
    assert res["outcome"] == "applied"
    card = world.card()
    assert card["delivery_state"] == "delivered"
    assert card["message_id"] == "m-late"
    # nothing changed content-wise — no render yet. When the source
    # drifts the successor is an UPDATE on the bound message, never a
    # re-create that would double-post the card
    world.led.db.execute(
        "UPDATE messages SET body_text='追記あり',content_hash=? "
        "WHERE message_id=101", ("e" * 64,))
    world.led.db.commit()
    notify_cards.sweep(world.led, CFG)
    r2 = world.led.db.execute(
        "SELECT op,spec_json FROM notification_renders "
        "ORDER BY render_rev DESC LIMIT 1").fetchone()
    assert r2["op"] == "update"
    spec = json.loads(r2["spec_json"])
    assert spec["delivery"]["message_id"] == "m-late"


@pytest.mark.parametrize("restart", [False, True])
def test_receipt_failure_never_repeats_discord_send(world, monkeypatch, restart):
    """A durable result survives a failed receipt publication and restart."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    publish = envelopes.publish_command
    failed = False

    def fail_receipt_once(directory, envelope):
        nonlocal failed
        if envelope.get("op") == "transport_receipt" and not failed:
            failed = True
            raise OSError("synthetic receipt failure")
        return publish(directory, envelope)

    async def run():
        await worker.tick()
        world.drain()
        monkeypatch.setattr(envelopes, "publish_command", fail_receipt_once)
        await worker.tick()
        assert len(bot.channels[42].sent) == 1
        resumed = worker
        if restart:
            resumed, _, _ = world.mkworker(bot=bot)
            await resumed.reconcile()
        # The runner need not have drained the receipt before the next tick.
        await resumed.tick()
        world.drain()
        assert len(bot.channels[42].sent) == 1
        assert world.card()["delivery_state"] == "delivered"

    asyncio.run(run())


def test_reconcile_ignores_other_live_scope(world):
    """Holding B's send lock cannot settle A's active attempt."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        await worker.tick()
        world.drain()
        foreign = delivery.DeliveryWorker(
            bot=bot, settings={**SETTINGS, "profile": "other"},
            root=str(world.data), reg=reg, worker_id="other-worker",
            log=lambda *a, **kw: None)
        stats = await foreign.reconcile()
        assert not any(stats.values())
        assert not list((world.data / "cmd_int").glob("*.json"))
        await worker.tick()
        world.drain()
        assert len(bot.channels[42].sent) == 1
        assert world.card()["delivery_state"] == "delivered"

    asyncio.run(run())


def test_scoped_registries_keep_independent_state_and_restore_pinned_flows(world):
    state = str(world.data / "discord_state")
    world.seed()
    world.dispatch()
    origin = {k: SETTINGS[k] for k in
              ("profile", "application_id", "channel_id", "guild_id")}
    legacy = registry.Registry(state)
    legacy.put_confirm("mine", {"origin": origin, "actor": "discord:1001"})
    legacy.put_confirm("theirs", {"origin": {**origin, "profile": "other"}})
    a = registry.Registry(state, scope=SETTINGS)
    b_scope = {**SETTINGS, "profile": "other"}
    b = registry.Registry(state, scope=b_scope)
    assert a.confirm("mine") and not a.confirm("theirs")
    assert b.confirm("theirs") and not b.confirm("mine")
    a.put_followup("a", {"token": "synthetic-a"})
    b.put_followup("b", {"token": "synthetic-b"})
    assert set(registry.Registry(state, scope=SETTINGS).followups()) == {"a"}
    assert set(registry.Registry(state, scope=b_scope).followups()) == {"b"}
    assert legacy.confirm("mine") and legacy.confirm("theirs")
    # Delivery batching must not defer a human preview's durable state.
    with a.batch():
        a.put_confirm("new", {"origin": origin})
        assert registry.Registry(state, scope=SETTINGS).confirm("new")


def test_body_messages_include_heading_in_discord_limit():
    body = "x" * 6000
    messages = text.body_messages({"title": "合成見出し" * 200, "body": body})
    assert all(len(message) <= 2000 for message in messages)
    assert sum(message.count("x") for message in messages) == len(body)


def test_card_posts_and_edits_never_ping(world):
    """Footer names are <@id> mentions — every send and edit of the card
    goes out with allowed_mentions none, so they render as names only."""
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    msg = bot.channels[42].sent[0]
    assert msg.allowed_mentions.users is False \
        and msg.allowed_mentions.everyone is False
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'ack')}",
                         message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    asyncio.run(_deliver(world, worker))
    assert msg.edits == 1 and msg.allowed_mentions.roles is False
    assert "-# ✅ 確認: <@1001>" in _face(msg)
    buttons = (msg.view.items if msg.embed is not None else
               [b for row in msg.view.items[0].children
                if hasattr(row, "children") for b in row.children])
    labels = [x.label for b in buttons for x in getattr(b, "options", [b])]
    assert "確認する" in labels and "⏸ 保留" not in labels
