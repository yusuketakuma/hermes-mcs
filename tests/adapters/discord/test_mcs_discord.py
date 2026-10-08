"""D2 plugin side — the Hermes Discord card worker and interaction
surface exercised against the real runner drain.

Synthetic fixtures + temp dirs only. A fake ``discord`` module and fake
Bot/Channel/Interaction objects stand in for the SDK — no network, no
real MCS data. The runner half is the real thing: ``dispatch_intent``
publishes specs and ``drain_int_commands`` answers cmd_int envelopes,
so every test is an honest A->B->A round trip.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import suppress
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import ledger as _ledger
import notify_cards
import notify_transport
import notify_cmds

from hermes_plugin.mcs_delivery import (envelopes, journal, paths,
                                        registry, text)
from hermes_plugin.mcs_delivery import spec as spec_mod
from hermes_plugin.mcs_delivery import worker as worker_mod
from hermes_plugin.mcs_discord import (actions as actions_mod, cards,
                                       delivery, tasks)
from notify_testkit import _add_request, _llm_extract
from discord_testkit import (
    CFG, MISSING, NOW, SETTINGS, FakeBot, FakeHTTP, FakeMessage, FakeThread,
    _fake_discord,
)


@pytest.fixture(autouse=True)
def _pin_wall_clock(monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)


# ---------- fake discord objects ----------------------------------------

FOREIGN_USER = SimpleNamespace(id=9999)


def _pings(allowed):
    """True unless the send disabled every mention kind."""
    return allowed is None or any(
        getattr(allowed, k) for k in ("everyone", "users", "roles",
                                      "replied_user"))


class FakeResponse:
    def __init__(self):
        self.done = False
        self.deferred = None
        self.modal = None
        self.message = None

    def is_done(self):
        return self.done

    async def defer(self, ephemeral=False, thinking=False):
        self.deferred = {"ephemeral": ephemeral, "thinking": thinking}
        self.done = True

    async def send_modal(self, modal):
        self.modal = modal
        self.done = True

    async def send_message(self, content, ephemeral=False,
                           allowed_mentions=None):
        self.message = {"content": content, "ephemeral": ephemeral,
                        "pings": _pings(allowed_mentions)}
        self.done = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, ephemeral=False, view=MISSING,
                   allowed_mentions=None):
        # interaction followups are application webhooks — ephemeral is
        # always legal, but discord.py rejects an explicit view=None
        if view is not MISSING and view is None:
            raise TypeError("expected view parameter to be of type "
                            "View, not NoneType")
        self.sent.append({"content": content, "ephemeral": ephemeral,
                          "view": view, "pings": _pings(allowed_mentions)})


class FakeInteraction:
    def __init__(self, custom_id, *, user_id=1001, channel_id=42,
                 guild_id=7, app_id=1, message_id=None,
                 components=None, token="tok-1", channel=None):
        self.data = {"custom_id": custom_id}
        if components is not None:
            self.data["components"] = components
        self.user = SimpleNamespace(id=user_id)
        self.channel_id = channel_id
        self.channel = channel
        self.guild_id = guild_id
        self.application_id = app_id
        self.token = token
        self.message = (SimpleNamespace(id=message_id)
                        if message_id is not None else None)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


# ---------- fixtures ------------------------------------------------------

@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    data = tmp_path / "data"
    data.mkdir()
    led = _ledger.Ledger(str(data / "ledger.db"))
    notify_cards.ensure_dirs(str(data))
    notify_cards.publish_flags(CFG, str(data))
    logs = []

    def _seed(mids=(100, 101), pid=1):
        led.db.execute(
            "INSERT INTO patients(project_id,patient_name,is_archived)"
            " VALUES(?,?,0)", (pid, "患者A"))
        for m in mids:
            led.db.execute(
                "INSERT INTO messages(message_id,project_id,sender_name,"
                "posted_at,posted_at_ts,body_text,content_hash,"
                "body_state,parent_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (m, pid, "職員", f"2026-09-24T08:{m % 60:02d}",
                 int(NOW) + m, "本文", f"{m:064x}", "full",
                 100 if m != 100 else None))
        led.db.commit()

    def _signal(key, pid=1, mids=None, state="open"):
        led.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,"
            "model,meta,created_at) VALUES('signal_v1',?,?,?,'test',?,?)",
            (pid, None,
             json.dumps({"type": "med_followup", "state": state,
                         "project_id": pid, "severity": "info",
                         "note": f"note {key}",
                         "evidence": {"message_ids": mids or []}}),
             json.dumps({"key": key, "type": "med_followup"}), NOW))
        led.db.commit()

    def _dispatch(kind="new_messages", pid=1,
                  payload=None, cfg=CFG):
        eid = led.outbox_add(
            kind, pid, payload or {"message_ids": [100, 101]})
        ev = dict(led.db.execute(
            "SELECT * FROM notify_outbox WHERE event_id=?",
            (eid,)).fetchone())
        return notify_cards.dispatch_intent(led, ev, cfg, now=NOW)

    def _drain():
        return notify_cmds.drain_int_commands(led, {}, CFG, str(data))

    def _mkworker(worker_id=None, bot=None):
        reg = registry.Registry(str(data / "discord_state"))
        bot = bot or FakeBot()
        worker = delivery.DeliveryWorker(
            bot=bot, settings=SETTINGS, root=str(data),
            reg=reg, worker_id=worker_id or registry.new_worker_id(),
            log=lambda e, **f: logs.append((e, f)))
        return worker, reg, bot

    def _mkactions(reg, bot):
        return actions_mod.Actions(
            bot=bot, settings=SETTINGS, root=str(data), reg=reg,
            log=lambda e, **f: logs.append((e, f)))

    async def _interact(act, ix):
        """Drive an interaction while the runner drains in parallel —
        mirrors the real deployment where the cmd watcher answers while
        the plugin waits on cmd_results."""
        task = asyncio.create_task(act.on_interaction(ix))
        for _ in range(500):
            _drain()
            if task.done():
                break
            await asyncio.sleep(0.01)
        await task

    def _latest_spec():
        render = led.db.execute(
            "SELECT * FROM notification_renders "
            "ORDER BY render_rev DESC LIMIT 1").fetchone()
        spec = json.loads(
            (data / "discord_render"
             / (render["delivery_id"] + ".json")).read_text())
        return render, spec

    def _token(spec, action):
        return next(b["token"] for row in spec["parts"]["action_rows"]
                    for b in row if b["id"] == action)

    def _card(card_id=1):
        return dict(led.db.execute(
            "SELECT * FROM notification_cards WHERE card_id=?",
            (card_id,)).fetchone())

    yield SimpleNamespace(
        led=led, data=data, logs=logs, seed=_seed, signal=_signal,
        dispatch=_dispatch, drain=_drain, mkworker=_mkworker,
        mkactions=_mkactions, interact=_interact, spec=_latest_spec,
        token=_token, card=_card)
    led.close()


# ---------- paths / envelopes / journal / registry -----------------------


def test_deeply_nested_result_is_unreadable_without_stopping_poll(tmp_path):
    (tmp_path / "synthetic.json").write_text("[" * 10000 + "]" * 10000)
    assert paths.read_result(str(tmp_path), "synthetic") is None

def test_publish_and_result_roundtrip(tmp_path):
    d = tmp_path / "cmd_int"
    r = tmp_path / "cmd_results"
    d.mkdir()
    r.mkdir()
    env = envelopes.transport_receipt(
        {"attempt_id": "ab" * 8,
         "spec": {"delivery_id":
                  "00000000-0000-4000-8000-000000000001",
                  "render_rev": 1,
                  "delivery": {"route_epoch": 1, "correlation": "cd" * 16,
                               "profile": "mcs", "application_id": "1",
                               "channel_id": "42", "guild_id": "7"}},
         "payload_hash": "ef" * 32},
        "delivered", message_id="9001")
    path = envelopes.publish_command(str(d), env)
    raw = json.loads(Path(path).read_text())
    assert notify_cmds.validate_int(raw) is None
    notify_cards.publish_file(str(r), paths.safe_name(
        env["command_id"]) + ".json", envelopes.canonical(
        {"outcome": "applied"}))
    assert paths.read_result(str(r), env["command_id"])["outcome"] \
        == "applied"


def test_envelope_shapes_validate(tmp_path):
    """Every envelope the plugin emits passes the runner's own
    validator — shape drift fails closed, not silently."""
    origin = {"profile": "mcs", "application_id": "1",
              "guild_id": "7", "channel_id": "42",
              "message_id": "9001"}
    env = envelopes.notification("ab" * 16, "discord:1001", origin)
    assert notify_cmds.validate_int(env) is None
    assert env["command_id"] == f"{'ab' * 16}:{envelopes.actor_hash('discord:1001')}"
    bad = dict(env, origin={**origin, "extra": "x"})
    assert notify_cmds.validate_int(bad) == "bad_origin"
    # refresh + a foreign op the inbox must refuse
    assert notify_cmds.validate_int(
        envelopes.refresh("discord:1001", origin)) is None
    assert notify_cmds.validate_int(
        {"version": 1, "op": "ops.card_resolve",
         "command_id": "00000000-0000-4000-8000-000000000001"}) \
        == "unknown_op"


def test_journal_classifies_attempts(tmp_path):
    d = str(tmp_path)
    journal.append(d, "w1", {"phase": "claimed", "attempt_id": "a1",
                             "delivery_id": "d1"})
    journal.append(d, "w1", {"phase": "begin", "attempt_id": "a1",
                             "delivery_id": "d1"})
    rec = journal.scan(d)
    assert journal.unfinished(rec)["a1"]["phase"] == "pre_http"
    assert not journal.unreported(rec)
    journal.append(d, "w1", {"phase": "started", "attempt_id": "a1",
                             "delivery_id": "d1"})
    rec = journal.scan(d)
    assert journal.unfinished(rec)["a1"]["phase"] == "post_http"
    journal.append(d, "w1", {"phase": "result", "attempt_id": "a1",
                             "delivery_id": "d1", "result": "delivered",
                             "message_id": "9001"})
    rec = journal.scan(d)
    assert "a1" in journal.unreported(rec)
    assert "a1" not in journal.unfinished(rec)
    journal.append(d, "w1", {"phase": "receipt", "attempt_id": "a1",
                             "delivery_id": "d1", "result": "delivered"})
    rec = journal.scan(d)
    assert not journal.unreported(rec)
    assert not journal.unfinished(rec)


def test_registry_persistence_and_expiry(tmp_path):
    d = str(tmp_path)
    reg = registry.Registry(d)
    reg.claim("d1", {"phase": "begin_sent", "attempt_id": "a1"})
    reg.put_modal("m1", {"actor": "discord:1", "token": "t"})
    reg.put_followup("c1", {"token": "tok", "application_id": "1"})
    reg2 = registry.Registry(d)                 # reload from disk
    assert reg2.claimed("d1")["attempt_id"] == "a1"
    assert reg2.modal("m1")["actor"] == "discord:1"
    assert reg2.followup("c1")["token"] == "tok"
    # expired records read as absent and are swept
    reg2._data["pending_modals"]["m1"]["expires"] = 0
    assert reg2.modal("m1") is None
    reg2._data["followups"]["c1"]["expires"] = 0
    reg2.expire()
    assert not registry.Registry(d).followups()


@pytest.mark.parametrize("contents", ["{", "[]", "null", '{"claims":[]}',
                                      '{"claims":{"delivery":[]}}'])
def test_corrupt_registry_is_not_reset_or_overwritten(tmp_path, contents):
    path = tmp_path / "registry.json"
    path.write_text(contents)
    with pytest.raises(ValueError, match="registry_corrupt"):
        registry.Registry(str(tmp_path))
    assert path.read_text() == contents


@pytest.mark.parametrize(("table", "lookup"), [
    ("pending_modals", "modal"), ("pending_confirms", "confirm"),
    ("followups", "followup"),
])
@pytest.mark.parametrize("expires", [float("nan"), float("inf"), "later", None])
def test_registry_malformed_expiry_never_authorizes(tmp_path, table, lookup, expires):
    reg = registry.Registry(str(tmp_path))
    reg._data[table]["synthetic"] = {"actor": "discord:1", "expires": expires}
    assert getattr(reg, lookup)("synthetic") is None
    reg._data[table]["synthetic"] = {"expires": expires}
    reg.expire()
    assert not reg._data[table]


# ---------- spec validation / view build ---------------------------------

def test_real_spec_validates_and_builds(world):
    world.seed()
    world.dispatch()
    _, spec = world.spec()
    assert spec_mod.validate(spec) is spec
    view = cards.build_view(spec)
    # the face is one bordered Container carrying text + action rows
    assert [type(i).__name__ for i in view.items] == ["Container"]
    inner = view.items[0].children
    kinds = [type(i).__name__ for i in inner]
    assert "TextDisplay" in kinds and "ActionRow" in kinds
    buttons = [b for i in inner if hasattr(i, "children")
               for b in i.children if not hasattr(b, "options")]
    ids = [b.custom_id for b in buttons if b.url is None]
    assert ids and all(i.startswith("mcs:a:") for i in ids)
    assert all(len(i) == 38 for i in ids)      # "mcs:a:" + 32 hex
    # 🔗 MCSで開く is a plain link button — no custom_id, no token
    links = [b for b in buttons if b.url is not None]
    assert [b.url for b in links] == [
        "https://www.medical-care.net/projects/medical/1"]
    assert all(b.custom_id is None and b.style == 5 for b in links)


@pytest.mark.parametrize(("mutate", "error"), [
    (lambda s: s.update(schema="bogus"), "bad_schema"),
    (lambda s: s.update(op="explode"), "bad_op"),
    (lambda s: s.update(delivery_id=s["delivery_id"] + "\n"), "bad_delivery_id"),
    (lambda s: s["delivery"].update(correlation="zz"), "bad_correlation"),
    (lambda s: s["delivery"].update(correlation="ab" * 16 + "\n"), "bad_correlation"),
    (lambda s: s["parts"]["action_rows"][0][0].update(token="ab" * 16 + "\n"),
     "bad_button_token"),
    (lambda s: s["parts"]["action_rows"][0][0].update(style=[]), "bad_button_style"),
    (lambda s: s["parts"].update(context=[]), "bad_context"),
    (lambda s: next(p for p in s["parts"]["manifest"] if p["kind"] == "body_part").update(
        part_id="body:0000"), "bad_body_part_id"),
    (lambda s: s["parts"].update(action_rows=[[{"ui": "button",
        "token": "not-hex", "label": "x", "id": "ack"}]]),
     "bad_button_token"),
    (lambda s: s["parts"].update(
        containers=[{"type": "text", "text": "x" * 5000}]),
     "container_too_long"),
])
def test_spec_rejects(world, mutate, error):
    world.seed()
    world.dispatch()
    _, spec = world.spec()
    mutate(spec)
    with pytest.raises(ValueError, match=error):
        spec_mod.validate(spec)


def test_spec_text_budget_covers_multiline_quotes(world):
    world.seed()
    world.dispatch()
    _, spec = world.spec()
    spec["parts"]["footer"] = []
    spec["parts"]["action_rows"] = []
    spec["parts"]["containers"] = [
        {"type": "quote", "text": "x\n" * 1500}]
    view = cards.build_view(spec)
    rendered = sum(len(item.content) for item in view.items[0].children)
    assert rendered > spec_mod.MAX_TOTAL_TEXT
    with pytest.raises(ValueError, match="text_budget"):
        spec_mod.validate(spec)


# ---------- delivery -------------------------------------------------------

async def _deliver(world, worker):
    """claim -> begin -> grant -> send -> receipt -> settled."""
    await worker.tick()
    world.drain()                       # grant
    await worker.tick()                 # send + receipt
    world.drain()                       # settle
    return worker._bot.channels[42].sent


def _delivered_source_signal(world):
    """Prove the original thread through the fake SDK and deliver the signal there."""
    world.dispatch(payload={"message_ids": [100]})
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    source = world.card(1)
    assert source["delivery_state"] == "delivered" and source["thread_state"] == "created"
    thread = bot.channels[int(source["thread_id"])]
    thread.parent_id = 42
    thread.guild = SimpleNamespace(id=7)
    original_send = thread.send

    async def send(content=None, *, embed=None, view=MISSING, allowed_mentions=None):
        message = await original_send(content, view=view, allowed_mentions=allowed_mentions)
        message.embed = embed
        message.channel = thread
        return message

    thread.send = send  # SDK accepts card embeds and Components in threads.
    world.signal("sig-1", mids=[100])
    world.dispatch(kind="signal", pid=1, payload={
        "signal_keys": ["sig-1"], "project_id": 1, "type": "med_followup"})
    for _ in range(3):
        asyncio.run(_deliver(world, worker))
        ready = world.led.db.execute("SELECT delivery_state FROM notification_cards WHERE kind='signal'").fetchone()
        if ready[0] == "delivered":
            break
    render = world.led.db.execute("SELECT r.* FROM notification_renders r "
        "JOIN notification_cards c ON c.card_id=r.card_id WHERE c.kind='signal' "
        "ORDER BY r.render_rev DESC LIMIT 1").fetchone()
    spec = json.loads(render["spec_json"])
    signal = world.card(render["card_id"])
    assert signal["delivery_state"] == "delivered"
    assert signal["thread_id"] == source["thread_id"] and spec["parts"]["source_thread"] is True
    assert len(bot.channels[42].sent) == 1
    message = next(m for m in thread.messages if str(m.id) == signal["message_id"])
    return worker, reg, bot, spec, message, thread


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
    notify_cards.mark_restored(str(world.data), backup_path=str(backup),
                               by="test")
    notify_cards.publish_flags(CFG, str(world.data))

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
    assert notify_cards.restore_pending(str(world.data)) is None
    notify_cards.publish_flags(CFG, str(world.data))

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
    out = notify_transport.apply_card_resolve(world.led, req, CFG)
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
    assert notify_cards.restore_pending(str(world.data)) is None
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
        flags.write_text(json.dumps({"interactive": True}))
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
        flags.write_text(json.dumps({"interactive": True}))
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

    w3, reg3, _ = world.mkworker()

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
    # runner-side the attempts were never granted — the drain answers
    # both with an honest rejection and nothing is re-sent
    world.drain()
    results = [json.loads(p.read_text())
               for p in (world.data / "cmd_results").glob("*.json")]
    assert len(results) == 2
    assert all(r["error"] == "unknown_attempt" for r in results)


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


# ---------- interactions ---------------------------------------------------

def test_action_ack_round_trip(world):
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "ack")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    assert ix.response.deferred is not None
    rows = world.led.db.execute(
        "SELECT actor FROM notification_acknowledgements").fetchall()
    assert rows and rows[0]["actor"] == "discord:1001"


def test_action_body_ephemeral_full_text(world):
    """📄本文表示 answers with chunked ephemeral followups carrying the
    untruncated shown-set text — the card itself is untouched. The
    button still ships on card_thread-less renders, so exercise that
    surface here (threaded cards show the body inside the thread)."""
    world.seed()
    cfg = {"notify": {k: v for k, v in CFG["notify"].items()
                      if k != "card_thread"},
           "signals": CFG["signals"]}
    world.dispatch(cfg=cfg)
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "body")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    assert ix.followup.sent
    assert all(m["ephemeral"] for m in ix.followup.sent)
    joined = "\n".join(m["content"] for m in ix.followup.sent)
    # sender names + bodies — content only the full-text answer has,
    # since the card itself shows a snippet
    assert "職員" in joined and "本文" in joined
    card = world.card()
    assert card["desired_render_rev"] == card["applied_render_rev"]


def test_action_click_inside_companion_thread(world):
    """A click inside the card's companion thread reports the thread's
    id as channel_id — the thread inherits the parent channel's
    authorization, and the origin reaching the runner is normalized
    to the channel the card is bound to."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "ack")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    thread = SimpleNamespace(id=777, parent_id=42)
    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id,
                         channel_id=777, channel=thread)
    asyncio.run(world.interact(act, ix))
    receipt = json.loads(world.led.db.execute(
        "SELECT receipt_json FROM command_receipts "
        "WHERE outcome='applied'").fetchone()["receipt_json"])
    assert receipt["origin"]["channel_id"] == "42"
    assert receipt["origin"]["thread_id"] == "777"


def test_action_foreign_thread_denied(world):
    """A click inside a thread whose parent is NOT the allowed channel
    stays denied — thread normalization never widens scope."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "ack")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    thread = SimpleNamespace(id=888, parent_id=999)
    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id,
                         channel_id=888, channel=thread)
    before = len(list((world.data / "cmd_int").glob("*.json")))
    asyncio.run(act.on_interaction(ix))
    assert "権限" in ix.response.message["content"]
    assert len(list((world.data / "cmd_int").glob("*.json"))) == before
    # the denial reason is journaled — the user-facing text stays generic
    denials = [f for e, f in world.logs if e == "interaction_denied"]
    assert denials and denials[0]["reason"] == "chat_not_allowed"


def test_confirm_preview_text_per_transport():
    req = {"title": "件" * 250, "reason": "理" * 450,
           "assignee": "担当者A", "due_date": "2026-10-01"}
    tail = (f"内容: {'件' * 200}\n担当: 担当者A\n期限: 2026-10-01\n"
            f"理由: {'理' * 400}")
    assert text.preview_text("request", req, True) \
        == "**確認 — タスク作成**\n" + tail
    assert text.preview_text("request", req, False) \
        == "確認 — タスク作成\n" + tail
    assert text.preview_text(
        "request", {"title": "t", "reason": "r"}, False) \
        == "確認 — タスク作成\n内容: t\n理由: r"
    report = {"field": "meds", "reason": "用量が違う"}
    assert text.preview_text("report", report, False).startswith(
        "確認 — 抽出の誤り報告\n箇所: 薬\nメモ: 用量が違う")
    dismiss = {"signal_key": "sig:1", "reason": "r"}
    assert text.preview_text("dismiss", dismiss, True) \
        == "**確認 — 候補の却下**\nsignal: `sig:1`\n理由: r"
    assert text.preview_text("dismiss", dismiss, False) \
        == "確認 — 候補の却下\nsignal: sig:1\n理由: r"


@pytest.mark.parametrize("due,ok", [
    ("2026-10-01", True), ("2026-02-30", False), ("20261001", False),
    ("2026-10-1", False), ("", False)])
def test_valid_due_requires_real_yyyy_mm_dd(due, ok):
    assert text.valid_due(due) is ok


def test_split_body_chunks_bounded():
    body = "\n".join(f"line-{i} " + "x" * 100 for i in range(80))
    chunks = text.split_body(body)
    assert 1 < len(chunks) <= text.BODY_MAX_CHUNKS
    assert all(len(c) <= text.BODY_CHUNK for c in chunks)
    assert chunks[0].startswith("line-0")
    with pytest.raises(ValueError, match="chunk_limit"):
        text.split_body("synthetic", limit=0)
    oversized = "合" * (text.BODY_CHUNK * (text.BODY_MAX_CHUNKS + 3))
    assert text.split_body(oversized) == [
        "合" * text.BODY_CHUNK] * text.BODY_MAX_CHUNKS
    one = text.split_body("短い")
    assert one == ["短い"]
    long_line = "y" * 5000
    chunks = text.split_body(long_line)
    assert all(len(c) <= text.BODY_CHUNK for c in chunks)


def test_action_ignores_foreign_and_denies(world):
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    act = world.mkactions(reg, bot)
    # unrelated interactions get silence — not even a response
    ix = FakeInteraction("other:x")
    asyncio.run(act.on_interaction(ix))
    assert not ix.response.done and not ix.followup.sent
    # unknown token -> ephemeral notice + an origin-bound refresh so
    # the card can rebuild its tokens (RC15)
    ix = FakeInteraction("mcs:a:" + "ff" * 16, message_id=1)
    asyncio.run(act.on_interaction(ix))
    assert ix.response.message["ephemeral"] is True
    files = [json.loads(f.read_text())
             for f in (world.data / "cmd_int").glob("*.json")]
    assert [f["op"] for f in files] == ["refresh"]
    # disallowed user -> denied before any file write
    _, spec = world.spec()
    tok = world.token(spec, "ack")
    before = len(list((world.data / "cmd_int").glob("*.json")))
    ix = FakeInteraction(f"mcs:a:{tok}", user_id=9999,
                         message_id=bot.channels[42].sent[0].id)
    asyncio.run(act.on_interaction(ix))
    assert "権限" in ix.response.message["content"]
    assert len(list((world.data / "cmd_int").glob("*.json"))) == before


def test_request_modal_full_flow(world):
    """request click -> modal -> submit -> preview -> confirm ->
    request.create lands as a human-confirmed command."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "request")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]

    # 1. click -> send_modal is the initial response (no defer)
    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id)
    asyncio.run(act.on_interaction(ix))
    assert ix.response.modal is not None and ix.response.deferred is None
    modal_id = ix.response.modal.custom_id[len("mcs:m:"):]
    assert reg.modal(modal_id)["actor"] == "discord:1001"

    # 2. submit -> token applies, preview is an ephemeral followup
    submit = FakeInteraction(
        f"mcs:m:{modal_id}", message_id=msg.id,
        components=[{"components": [
            {"custom_id": "title", "value": "服薬確認"},
            {"custom_id": "reason", "value": "フォロー要"},
            {"custom_id": "assignee", "value": ""},
            {"custom_id": "due_date", "value": ""}]}])
    asyncio.run(world.interact(act, submit))
    assert submit.response.deferred == {"ephemeral": True,
                                        "thinking": False}
    preview = submit.followup.sent[-1]
    assert preview["ephemeral"] is True
    cid = next(b.custom_id for b in preview["view"].items
               if b.custom_id.startswith("mcs:c:")
               and not b.custom_id.endswith(":cancel"))
    pend = reg.confirm(cid[len("mcs:c:"):])
    assert pend["payload"]["cmd"] == "request.create"
    assert pend["payload"]["source_message_id"] == 100
    assert pend["payload"]["source_hash"] == f"{100:064x}"
    assert pend["payload"]["human_confirmed"] is True

    # 3. confirm -> the pinned payload is enqueued verbatim
    confirm = FakeInteraction(cid, message_id=msg.id)
    asyncio.run(world.interact(act, confirm))
    rows = world.led.db.execute(
        "SELECT project_id,source_message_id,title,status "
        "FROM requests").fetchall()
    assert len(rows) == 1
    assert rows[0]["source_message_id"] == 100
    assert rows[0]["title"] == "服薬確認"


def test_modal_wrong_actor_and_origin(world):
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "request")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id)
    asyncio.run(act.on_interaction(ix))
    modal_id = ix.response.modal.custom_id[len("mcs:m:"):]
    # a different user cannot submit another user's form
    bad = FakeInteraction(f"mcs:m:{modal_id}", user_id=2222,
                          message_id=msg.id)
    asyncio.run(act.on_interaction(bad))
    assert "本人" in bad.response.message["content"]
    queued = sorted((world.data / "cmd_int").glob("*.json"))
    # a channel outside the allowlist is refused by authorization
    bad2 = FakeInteraction(f"mcs:m:{modal_id}", channel_id=99,
                           message_id=msg.id)
    asyncio.run(act.on_interaction(bad2))
    assert bad2.response.message == {"content": "権限がありません。",
                                     "ephemeral": True, "pings": False}
    # an allowed channel but a different card message trips the
    # strict_message origin pin — the submit must come from the card
    # the form was opened on
    bad3 = FakeInteraction(f"mcs:m:{modal_id}", message_id=msg.id + 1)
    asyncio.run(act.on_interaction(bad3))
    assert bad3.response.message == {
        "content": "フォームを開いたカードと送信元が一致しません。",
        "ephemeral": True, "pings": False}
    assert bad3.response.deferred is None
    assert sorted((world.data / "cmd_int").glob("*.json")) == queued
    assert reg.modal(modal_id) is not None


def test_dismiss_flow_pins_artifact(world):
    """dismiss click -> modal -> confirm -> ops.signal_dismiss with
    the render-time artifact id — a stale signal is the runner's job."""
    world.seed(mids=(100,))
    worker, reg, bot, spec, msg, thread = _delivered_source_signal(world)
    tok = world.token(spec, "dismiss")
    act = world.mkactions(reg, bot)

    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id, channel_id=thread.id, channel=thread)
    asyncio.run(act.on_interaction(ix))
    modal_id = ix.response.modal.custom_id[len("mcs:m:"):]
    submit = FakeInteraction(
        f"mcs:m:{modal_id}", message_id=msg.id, channel_id=thread.id, channel=thread,
        components=[{"components": [
            {"custom_id": "reason", "value": "対応済み"}]}])
    asyncio.run(world.interact(act, submit))
    preview = submit.followup.sent[-1]
    cid = next(b.custom_id for b in preview["view"].items
               if b.custom_id.startswith("mcs:c:")
               and not b.custom_id.endswith(":cancel"))
    payload = reg.confirm(cid[len("mcs:c:"):])["payload"]
    assert payload["cmd"] == "ops.signal_dismiss"
    assert payload["signal_key"] == "sig-1"
    assert payload["expected_signal_artifact_id"] > 0

    confirm = FakeInteraction(cid, message_id=msg.id, channel_id=thread.id, channel=thread)
    asyncio.run(world.interact(act, confirm))
    row = world.led.db.execute(
        "SELECT content FROM artifacts WHERE kind='signal_v1' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    # the dismissal was applied (or appended) — not silently dropped
    assert row is not None


class SignalBot(FakeBot):
    """FakeBot that signals listener wiring (set) and removal (clear), so
    supervisor tests wait on an event instead of polling a time budget.
    The supervisor wires its listener only after taking the scope lock."""

    def __init__(self):
        super().__init__()
        self.wired = asyncio.Event()

    def add_listener(self, fn, name):
        super().add_listener(fn, name)
        self.wired.set()

    def remove_listener(self, fn, name):
        super().remove_listener(fn, name)
        if not self.listeners:
            self.wired.clear()


def test_signal_bot_wiring_event_tracks_listeners():
    """The wait signal the supervisor tests rely on: set on wiring, cleared
    only when the last listener is removed (so a re-wire is observable)."""
    async def run():
        bot = SignalBot()
        assert not bot.wired.is_set()
        bot.add_listener(print, "on_a")
        bot.add_listener(len, "on_b")
        assert bot.wired.is_set()
        bot.remove_listener(print, "on_a")
        assert bot.wired.is_set()
        bot.remove_listener(len, "on_b")
        assert not bot.wired.is_set()
    asyncio.run(run())


def test_supervisor_registers_and_stops(world):
    """Listener lands once per Bot, unload removes it, and the
    supervisor drives the loop through spawn_task."""
    world.seed()
    world.dispatch()
    spawned = []

    class FakeCtx:
        def spawn_task(self, coro, *, name=None):
            t = asyncio.ensure_future(coro)
            spawned.append(t)
            return t

        def on_unload(self, cb):
            self._unload = cb

    async def run():
        bot = SignalBot()
        sup = tasks.Supervisor(
            ctx=FakeCtx(), bot=bot,
            settings={**SETTINGS, "data_root": str(world.data)},
            log=lambda e, **f: world.logs.append((e, f)))
        assert sup.start() is True
        assert spawned and not spawned[0].done()
        await asyncio.wait_for(bot.wired.wait(), 5)
        assert len(bot.listeners) == 1
        sup.unload()
        spawned[0].cancel()
        with suppress(asyncio.CancelledError):
            await spawned[0]
        assert not bot.listeners

    asyncio.run(run())


def test_supervisor_retries_a_transient_reconcile_failure(world, monkeypatch):
    """Regression: an OSError (or registry_corrupt) during the start-up
    reconcile made _run exit silently — delivery stopped until restart."""
    world.seed()
    world.dispatch()
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(worker_mod.asyncio, "sleep", fast_sleep)
    reconcile = delivery.DeliveryWorker.reconcile
    fails = [1]

    async def flaky(self):
        if fails[0]:
            fails[0] -= 1
            raise OSError("synthetic disk blip")
        return await reconcile(self)

    monkeypatch.setattr(delivery.DeliveryWorker, "reconcile", flaky)
    ctx = SimpleNamespace(spawn_task=lambda coro, name=None: asyncio.ensure_future(coro),
                          on_unload=lambda cb: None)

    async def run():
        bot = SignalBot()
        sup = tasks.Supervisor(
            ctx=ctx, bot=bot,
            settings={**SETTINGS, "data_root": str(world.data)},
            log=lambda e, **f: world.logs.append((e, f)))
        assert sup.start() is True
        await asyncio.wait_for(bot.wired.wait(), 5)
        sup.unload()
        await asyncio.wait_for(sup._task, 5)

    asyncio.run(run())
    assert ("startup_retry", {"error": "OSError"}) in world.logs


# ---------- D3: authorization depth (RC01) ---------------------------------

def test_action_denies_wrong_channel_and_project(world):
    """allowed_user_ids alone is not the gate — a click from an
    unlisted channel or for an unlisted project dies before any file
    is written, even though the adapter's own auth may allow-all."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "ack")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]

    ix = FakeInteraction(f"mcs:a:{tok}", channel_id=99,
                         message_id=msg.id)
    asyncio.run(act.on_interaction(ix))
    assert "権限" in ix.response.message["content"]
    # a token whose pinned project is outside project_ids
    reg.put_tokens({"aa" * 16: {"action": "ack", "card_key": "x",
                                "kind": "signal", "project_id": 2,
                                "context": {"project_id": 2},
                                "channel_id": "42"}})
    ix = FakeInteraction("mcs:a:" + "aa" * 16, message_id=msg.id)
    asyncio.run(act.on_interaction(ix))
    assert "権限" in ix.response.message["content"]
    assert not list((world.data / "cmd_int").glob("*.json"))


def test_digest_action_checks_every_project_before_publication(world, monkeypatch):
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    act = world.mkactions(reg, bot)
    token = "aa" * 16
    reg.put_tokens({token: {"action": "ack", "project_id": None,
                           "context": {"signals": {
                               "one": {"project_id": 1},
                               "two": {"project_id": 2}}}}})
    monkeypatch.setattr(actions_mod, "RESULT_WAIT_S", 0)
    ix = FakeInteraction(f"mcs:a:{token}", message_id=9001)
    asyncio.run(act.on_interaction(ix))
    assert not list((world.data / "cmd_int").glob("*.json"))
    assert "権限" in ix.response.message["content"]


@pytest.mark.parametrize(("key", "value"), [
    ("allowed_user_ids", {"2002"}), ("project_ids", {2}),
    ("application_id", "999"), ("channel_id", "999"), ("profile", "other"),
    ("profile", "mcs"),
])
def test_delayed_discord_body_rechecks_current_access(world, monkeypatch, key, value):
    world.seed()
    cfg = {"notify": {**CFG["notify"], "card_thread": False}}
    world.dispatch(cfg=cfg)
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    act = world.mkactions(reg, bot)
    token = world.token(spec, "body")
    ix = FakeInteraction(f"mcs:a:{token}", message_id=bot.channels[42].sent[0].id)
    monkeypatch.setattr(actions_mod, "RESULT_WAIT_S", 0)
    asyncio.run(act.on_interaction(ix))
    assert reg.followups()
    world.drain()
    act._settings = {**SETTINGS, key: value}
    asyncio.run(act.sweep_followups())
    sent = sys.modules["discord"].Webhook.sent
    if key == "profile" and value == "mcs":
        assert len(sent) == 1 and "本文" in sent[0]["content"]
    else:
        assert not sent
    assert not reg.followups()


def test_ack_idempotent_per_actor(world):
    """RC13 — the same actor re-clicking is one acknowledgement
    (deterministic command_id replays the stored receipt); a second
    allowed actor's ack on the same card stands alongside it."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "ack")
    act = actions_mod.Actions(
        bot=bot, settings={**SETTINGS,
                           "allowed_user_ids": {"1001", "2002"}},
        root=str(world.data), reg=reg,
        log=lambda e, **f: world.logs.append((e, f)))
    msg = bot.channels[42].sent[0]

    for uid in (1001, 1001, 2002):
        ix = FakeInteraction(f"mcs:a:{tok}", user_id=uid,
                             message_id=msg.id)
        asyncio.run(world.interact(act, ix))
    rows = world.led.db.execute(
        "SELECT actor FROM notification_acknowledgements "
        "ORDER BY actor").fetchall()
    assert [r["actor"] for r in rows] == ["discord:1001",
                                          "discord:2002"]


# ---------- D3: confirm hardening (RC14) -------------------------------------

def _drive_to_confirm(world, act, tok, msg):
    """request click -> modal -> submit -> returns the confirm id."""
    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id)
    asyncio.run(act.on_interaction(ix))
    modal_id = ix.response.modal.custom_id[len("mcs:m:"):]
    submit = FakeInteraction(
        f"mcs:m:{modal_id}", message_id=msg.id,
        components=[{"components": [
            {"custom_id": "title", "value": "服薬確認"},
            {"custom_id": "reason", "value": "フォロー要"},
            {"custom_id": "assignee", "value": ""},
            {"custom_id": "due_date", "value": ""}]}])
    asyncio.run(world.interact(act, submit))
    preview = submit.followup.sent[-1]
    return next(b.custom_id for b in preview["view"].items
                if b.custom_id.startswith("mcs:c:")
                and not b.custom_id.endswith(":cancel"))


def test_confirm_wrong_actor_and_replay(world):
    """Only the preview's own actor may confirm; once consumed the
    confirm id is dead — a replayed click reports expiry."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "request")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    cid = _drive_to_confirm(world, act, tok, msg)

    bad = FakeInteraction(cid, user_id=2002, message_id=msg.id)
    asyncio.run(act.on_interaction(bad))
    assert "本人" in bad.response.message["content"]
    assert reg.confirm(cid[len("mcs:c:"):]) is not None

    malformed = FakeInteraction(cid + ":unexpected", message_id=msg.id)
    asyncio.run(act.on_interaction(malformed))
    assert reg.confirm(cid[len("mcs:c:"):]) is not None
    assert not list((world.data / "cmd_int").glob("*.json"))

    ok = FakeInteraction(cid, message_id=msg.id)
    asyncio.run(world.interact(act, ok))
    assert world.led.db.execute(
        "SELECT COUNT(*) c FROM requests").fetchone()["c"] == 1
    again = FakeInteraction(cid, message_id=msg.id)
    asyncio.run(act.on_interaction(again))
    assert "処理中" in again.response.message["content"]   # consumed, not expired


def test_confirm_cancel_drops_pending(world):
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "request")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    cid = _drive_to_confirm(world, act, tok, msg)

    foreign = FakeInteraction(cid + ":cancel", channel_id=99, message_id=msg.id)
    asyncio.run(act.on_interaction(foreign))
    assert reg.confirm(cid[len("mcs:c:"):]) is not None

    cancel = FakeInteraction(cid + ":cancel", message_id=msg.id)
    asyncio.run(act.on_interaction(cancel))
    assert "取り消し" in cancel.response.message["content"]
    assert reg.confirm(cid[len("mcs:c:"):]) is None
    assert not world.led.db.execute(
        "SELECT COUNT(*) c FROM requests").fetchone()["c"]
    # the cancelled confirm cannot be resurrected
    late = FakeInteraction(cid, message_id=msg.id)
    asyncio.run(act.on_interaction(late))
    assert "期限切れ" in late.response.message["content"]


def test_cancel_during_confirm_publish_never_reports_cancelled(
        world, monkeypatch):
    """A 取消 (or a second 確定) arriving while the first 確定 is still
    writing the command file must not answer 取り消しました — the
    command is queued, so the cancel reports in-progress instead."""
    import threading
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "request")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    cid = _drive_to_confirm(world, act, tok, msg)

    gate = threading.Event()
    entered = threading.Event()
    real = envelopes.publish_command

    def slow_publish(d, env):
        entered.set()
        assert gate.wait(5)
        return real(d, env)
    monkeypatch.setattr(envelopes, "publish_command", slow_publish)

    ok = FakeInteraction(cid, message_id=msg.id)
    cancel = FakeInteraction(cid + ":cancel", message_id=msg.id)
    again = FakeInteraction(cid, message_id=msg.id)

    async def race():
        first = asyncio.create_task(act.on_interaction(ok))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            await act.on_interaction(cancel)
            await act.on_interaction(again)
        finally:
            gate.set()
        await first
    monkeypatch.setattr(actions_mod, "HUMAN_WAIT_S", 0.05)
    asyncio.run(race())

    assert "取り消し" not in cancel.response.message["content"]
    assert "処理中" in cancel.response.message["content"]
    assert "処理中" in again.response.message["content"]
    assert "受け付けました" in ok.followup.sent[0]["content"]
    assert len(list((world.data / "cmd_int").glob("*.json"))) == 1
    assert reg.confirm(cid[len("mcs:c:"):])["consumed"] is True


@pytest.mark.parametrize("error", [OSError("disk full"),
                                   ValueError("command_too_large")])
def test_confirm_publish_failure_releases_in_flight(world, monkeypatch,
                                                    error):
    """A failed command write keeps the confirm usable (and cancellable)."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "request")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    cid = _drive_to_confirm(world, act, tok, msg)

    def fail(d, env):
        raise error
    monkeypatch.setattr(envelopes, "publish_command", fail)
    ok = FakeInteraction(cid, message_id=msg.id)
    asyncio.run(act.on_interaction(ok))
    assert "送信に失敗" in ok.followup.sent[-1]["content"]
    assert not reg.confirm(cid[len("mcs:c:"):]).get("in_flight")
    cancel = FakeInteraction(cid + ":cancel", message_id=msg.id)
    asyncio.run(act.on_interaction(cancel))
    assert "取り消し" in cancel.response.message["content"]


def test_confirm_expiring_before_take_never_publishes(world):
    """The TTL may lapse between the confirm lookup and taking it — the
    take must answer expiry, not queue the command from the stale
    lookup (the old begin_confirm result was ignored)."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "request")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    cid = _drive_to_confirm(world, act, tok, msg)
    before = len(list((world.data / "cmd_int").glob("*.json")))
    real = act._authorized

    def lapse(interaction, pids=None):
        reg._data["pending_confirms"][cid[len("mcs:c:"):]]["expires"] = 0
        return real(interaction, pids)
    act._authorized = lapse
    ok = FakeInteraction(cid, message_id=msg.id)
    asyncio.run(act.on_interaction(ok))
    assert "期限切れ" in ok.response.message["content"]
    assert len(list((world.data / "cmd_int").glob("*.json"))) == before


def test_send_modal_failure_releases_modal(world):
    """send_modal past the ~3s window raises — the modal entry must be
    dropped (a dead modal_id is not claimable state) and the click gets
    a best-effort followup instead of silence."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "request")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]

    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id)

    async def boom(modal):
        raise FakeHTTP(400)
    ix.response.send_modal = boom
    asyncio.run(act.on_interaction(ix))
    assert not reg._data["pending_modals"]
    assert ix.followup.sent \
        and "期限切れ" in ix.followup.sent[-1]["content"]


# ---------- D3: dismiss staleness (RC05) -------------------------------------

def test_dismiss_rejected_when_signal_moved(world):
    """The confirm pins the artifact the card displayed — when the
    signal gains a newer transition between preview and confirm the
    runner refuses and the signal stays open."""
    world.seed(mids=(100,))
    worker, reg, bot, spec, msg, thread = _delivered_source_signal(world)
    tok = world.token(spec, "dismiss")
    act = world.mkactions(reg, bot)

    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id, channel_id=thread.id, channel=thread)
    asyncio.run(act.on_interaction(ix))
    modal_id = ix.response.modal.custom_id[len("mcs:m:"):]
    submit = FakeInteraction(
        f"mcs:m:{modal_id}", message_id=msg.id, channel_id=thread.id, channel=thread,
        components=[{"components": [
            {"custom_id": "reason", "value": "対応済み"}]}])
    asyncio.run(world.interact(act, submit))
    preview = submit.followup.sent[-1]
    cid = next(b.custom_id for b in preview["view"].items
               if b.custom_id.startswith("mcs:c:")
               and not b.custom_id.endswith(":cancel"))

    # evidence moved after the preview — the pin no longer names HEAD
    world.signal("sig-1", mids=[100, 101])

    confirm = FakeInteraction(cid, message_id=msg.id, channel_id=thread.id, channel=thread)
    asyncio.run(world.interact(act, confirm))
    assert "やり直してください" in confirm.followup.sent[-1]["content"]
    rows = world.led.db.execute(
        "SELECT content FROM artifacts WHERE kind='signal_v1' "
        "ORDER BY artifact_id").fetchall()
    assert len(rows) == 2            # open + re-opened — no dismissal
    assert json.loads(rows[-1]["content"])["state"] == "open"


# ---------- D3: internal-op isolation (RC06) ---------------------------------

def test_cmd_int_rejects_foreign_and_internal_ops(world):
    """cmd_int carries only transport/notification envelopes and the
    two card human commands — ops.* and unknown verbs are refused."""
    world.seed()
    world.dispatch()                  # materializes the notify dirs
    int_dir = str(world.data / "cmd_int")
    res_dir = str(world.data / "cmd_results")
    for env in ({"version": 1, "op": "ops.scan",
                 "command_id":
                 "00000000-0000-4000-8000-0000000000aa"},
                {"version": 1, "cmd": "ops.card_resolve",
                 "command_id":
                 "00000000-0000-4000-8000-0000000000bb"},
                {"version": 1, "cmd": "request.update",
                 "command_id":
                 "00000000-0000-4000-8000-0000000000cc"}):
        envelopes.publish_command(int_dir, env)
    world.drain()
    for suffix in ("aa", "bb", "cc"):
        cid = "00000000-0000-4000-8000-0000000000" + suffix
        res = paths.read_result(res_dir, cid)
        assert res["outcome"] == "rejected"
        assert res["error"] == "unknown_op"
        # refused commands are quarantined, not re-drained
        assert (world.data / "cmd_int"
                / (cid + ".json.invalid")).exists()


def test_mcs_command_rejects_internal_ops(world):
    """The /mcs surface keeps the same wall — a confirm envelope that
    smuggles a foreign cmd (request.* into control, ops.* into request)
    is refused before anything is queued."""
    import hermes_plugin

    class FakeCtx:
        def get_config(self, key, default=None):
            cfg = {"snapshot": str(world.data / "snap"
                                   / "ledger-snapshot.db"),
                   "inbox": str(world.data / "cmd"),
                   "allowed_user_ids": ["1001"],
                   "allowed_chat_ids": ["42"], "project_ids": [1]}
            return cfg.get(key, default)

    handler = hermes_plugin._make_handler(FakeCtx())
    ctx = {"platform": "discord", "authorized": True, "internal": False,
           "is_bot": False, "via_upstream_relay": False,
           "native_input": True, "user_id": "1001", "chat_id": "42",
           "scope_id": None, "profile": None, "message_id": None}
    origin = {"user_id": "1001", "chat_id": "42", "scope_id": None,
              "profile": None}
    import mcs_requests

    def _confirm(op, cmd):
        payload = {"cmd": cmd, "version": 1, "command_id":
                   "00000000-0000-4000-8000-0000000000dd",
                   "actor": "discord:1001", "human_confirmed": True,
                   "project_id": 1}
        return json.loads(handler(json.dumps({
            "op": op, "phase": "confirm", "payload": payload,
            "payload_hash": mcs_requests.payload_hash(
                {"payload": payload, "origin": origin}),
            "origin": origin}), ctx))

    res = _confirm("control", "request.create")
    assert res["ok"] is False and res["error"] == "invalid_command"
    res = _confirm("request", "ops.scan")
    assert res["ok"] is False and res["error"] == "invalid_command"


# ---------- D3: notification_receipt via /mcs (RC27) -------------------------

def test_mcs_notification_receipt_scoped(world):
    """The card-UX receipt query is reachable through /mcs after the
    interaction token dies — narrowed to the caller's actor, scope and
    projects; operator receipts and foreign actors stay hidden."""
    import hermes_plugin
    import ledger as _ledger2

    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
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
def test_corrupt_spec_quarantined(world, contents):
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


# ---------- D3: supervisor scope isolation (RC17) ------------------------------

def test_supervisor_profile_scopes_do_not_mix(world, monkeypatch):
    """A->B->A: two profiles on the same data root hold separate scope
    locks; unload removes only its own listener; a duplicate worker on
    an owned scope reports visible-stopped instead of racing sends."""
    monkeypatch.setattr(tasks, "LOCK_WAIT_S", 1.0)
    world.seed()
    world.dispatch()
    spawned = []

    class FakeCtx:
        def spawn_task(self, coro, *, name=None):
            t = asyncio.ensure_future(coro)
            spawned.append(t)
            return t

        def on_unload(self, cb):
            self._unload = cb

    def _sup(bot, profile):
        return tasks.Supervisor(
            ctx=FakeCtx(), bot=bot,
            settings={**SETTINGS, "data_root": str(world.data),
                      "profile": profile},
            log=lambda e, **f: world.logs.append((e, f)))

    async def run():
        bot_a, bot_b = SignalBot(), SignalBot()
        sup_a = _sup(bot_a, "mcs")
        sup_b = _sup(bot_b, "other")
        assert sup_a.start() and sup_b.start()
        await asyncio.wait_for(
            asyncio.gather(bot_a.wired.wait(), bot_b.wired.wait()), 5)
        locks = list((world.data / "discord_state")
                     .glob("send-*.lock"))
        assert len(locks) == 2        # different profiles, both live

        # a second worker on A's owned scope gets a visible stop
        bot_dup = FakeBot()
        sup_dup = _sup(bot_dup, "mcs")
        assert sup_dup.start()
        dup_task = spawned[-1]
        await asyncio.wait_for(dup_task, 5)
        assert not bot_dup.listeners
        assert any(e == "scope_lock_unavailable"
                   for e, _ in world.logs)

        # unload A -> its listener goes; B is untouched
        sup_a.unload()
        assert not bot_a.listeners and len(bot_b.listeners) == 1
        spawned[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await spawned[0]
        # A returns -> exactly one listener on the fresh binding
        sup_a2 = _sup(bot_a, "mcs")
        assert sup_a2.start()
        assert not bot_a.wired.is_set()
        await asyncio.wait_for(bot_a.wired.wait(), 5)
        assert len(bot_a.listeners) == 1

        sup_b.unload()
        sup_a2.unload()
        for t in spawned:
            t.cancel()
        for t in spawned:
            with suppress(asyncio.CancelledError):
                await t

    asyncio.run(run())


def test_supervisor_yields_scope_when_bot_closes(world):
    """Fatal adapter rebuild: the predecessor's bot is closed before the
    replacement connects; the old supervisor must release the scope lock
    so the successor — wired on the new bot — can take over intake."""
    world.seed()
    world.dispatch()
    spawned = []

    class FakeCtx:
        def spawn_task(self, coro, *, name=None):
            t = asyncio.ensure_future(coro)
            spawned.append(t)
            return t

        def on_unload(self, cb):
            self._unload = cb

    def _sup(bot):
        return tasks.Supervisor(
            ctx=FakeCtx(), bot=bot,
            settings={**SETTINGS, "data_root": str(world.data),
                      "profile": "mcs"},
            log=lambda e, **f: world.logs.append((e, f)))

    async def run():
        closed = {"a": False}
        bot_a = SignalBot()
        bot_a.is_closed = lambda: closed["a"]
        sup_a = _sup(bot_a)
        assert sup_a.start()
        await asyncio.wait_for(bot_a.wired.wait(), 5)

        # fatal adapter error path: host closes the old client, then a
        # rebuilt adapter wires a successor on the new bot
        closed["a"] = True
        bot_b = SignalBot()
        sup_b = _sup(bot_b)
        assert sup_b.start()

        # predecessor notices the closed client and releases the lock;
        # the successor waits out the handoff and takes over intake
        await asyncio.wait_for(spawned[0], 10)
        await asyncio.wait_for(bot_b.wired.wait(), 10)
        assert len(bot_b.listeners) == 1
        assert not bot_a.listeners
        assert any(e == "bot_closed" for e, _ in world.logs)

        sup_b.unload()
        for t in spawned:
            t.cancel()
        for t in spawned:
            with suppress(asyncio.CancelledError):
                await t

    asyncio.run(run())


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


# ---------- D4: unknown token -> origin-bound refresh (RC15) ------------------

def test_unknown_token_self_heals_via_refresh(world):
    """RC15 — a click whose token context is gone (expired/GC'd) still
    publishes a refresh bound to the native origin; the runner resolves
    the card from that origin and re-issues the render with fresh
    tokens — the card rebuilds itself instead of staying dead."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    msg = bot.channels[42].sent[0]
    act = world.mkactions(reg, bot)
    dead = "ff" * 16                   # never registered / expired
    ix = FakeInteraction(f"mcs:a:{dead}", message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    assert "無効化" in ix.response.message["content"] \
        or "無効化" in ix.followup.sent[-1]["content"]
    # a refresh command was published and applied — its receipt names
    # the op; the runner re-issued the render from the origin
    rows = world.led.db.execute(
        "SELECT receipt_json FROM command_receipts").fetchall()
    kinds = {json.loads(r["receipt_json"]).get("kind") for r in rows}
    assert "refresh" in kinds
    # a fresh render with fresh tokens exists
    r = world.led.db.execute(
        "SELECT render_rev,state FROM notification_renders "
        "ORDER BY render_rev DESC LIMIT 1").fetchone()
    assert r["render_rev"] >= 2


def test_unknown_token_unauthorized_no_refresh(world):
    """An unauthorized click earns no refresh — the runner op has no
    actor check of its own, so the gate must live here."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    msg = bot.channels[42].sent[0]
    act = world.mkactions(reg, bot)
    ix = FakeInteraction("mcs:a:" + "ff" * 16, user_id=9999,
                         message_id=msg.id)
    asyncio.run(act.on_interaction(ix))
    assert "権限" in ix.response.message["content"]
    assert not list((world.data / "cmd_int").glob("*.json"))


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


def test_thread_opens_with_body_and_card_drops_body_button(world):
    """The companion thread opens holding the full text — the card
    drops its 📄 button because the body already lives inside."""
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
    assert "body" not in ids
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
    notify_cards.apply_refresh(world.led, {
        "command_id": "00000000-0000-4000-8000-00000000f001", "actor": "discord:1001",
        "origin": {"profile": "mcs", "application_id": "1", "guild_id": "7",
                   "channel_id": "42", "message_id": world.card()["message_id"]}}, CFG)
    assert world.led.db.execute("SELECT count(*) FROM notification_renders").fetchone()[0] == before
    assert len(bot.channels[42].sent) == 1


def test_update_backfills_body_into_existing_thread(world, monkeypatch):
    """A card whose thread was created before the in-thread body —
    here simulated by a definitive 400 body rejection — gets the text
    on the next update. Unknown outcomes remain held, never retried."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()

    async def run():
        original = FakeThread.send

        async def boom(self, content, allowed_mentions=None):
            raise FakeHTTP(400)
        monkeypatch.setattr(FakeThread, "send", boom)
        sent = await _deliver(world, worker)
        assert len(sent) == 1
        world.drain()
        # restore by re-setting — undo() would also revert the world
        # fixture's own monkeypatch actions (shared instance)
        monkeypatch.setattr(FakeThread, "send", original)
        thread = sent[0].threads[0][1]
        # A real discord.Thread carries these ownership fields; the
        # exact-edit path must prove them rather than infer its parent.
        thread.parent_id = 42
        thread.guild = SimpleNamespace(id=7)
        original_thread_id = thread.id
        assert not thread.sent
        assert {r["state"] for r in world.led.db.execute(
            "SELECT state FROM notification_render_parts WHERE kind='body_part'")}
        assert all(r["state"] == "not_sent" for r in world.led.db.execute(
            "SELECT state FROM notification_render_parts WHERE kind='body_part'"))
        # source drifts -> update render on the same message
        world.led.db.execute(
            "UPDATE messages SET body_text='追記あり',content_hash=? "
            "WHERE message_id=101", ("e" * 64,))
        world.led.db.commit()
        notify_cards.sweep(world.led, CFG)
        await _deliver(world, worker)
        world.drain()
        assert thread.sent                   # backfilled
        body = "\n".join(thread.sent)
        assert "📄 本文" not in body and "追記あり" in body and "スタンプ 未取得" in body
        post = next(p for p in thread.sent if "追記あり" in p)
        assert post.index("追記あり") < post.index("スタンプ 未取得")   # body first, stamps trail
        assert "📋 要約" not in body and "処理待ち" not in body and "解析更新中" not in body
        _, current_spec = world.spec()
        assert current_spec["delivery"]["thread_id"] == str(original_thread_id)
        assert "要約 処理待ち" in notify_cards._card_body_text(world.led.db, dict(world.led.db.execute("SELECT * FROM notification_cards WHERE card_id=1").fetchone()), {"shown": "[100,101]"})[1]
        assert "要約 処理待ち" in "\n".join(item.get("text", "") for item in current_spec["parts"]["containers"])

        n = len(thread.sent)
        # re-running the same delivery posts nothing — the journal
        # already proves every part of this spec
        _, spec2 = world.spec()
        claim2 = {"attempt_id": "replay", "worker_id": "w9",
                  "spec": spec2,
                  "payload_hash": envelopes.payload_hash(spec2),
                  "spec_path": None, "phase": "settled"}
        await worker._deliver_parts(
            claim2, str(spec2["delivery"]["message_id"]))
        assert len(thread.sent) == n
        # a changed body rewrites its proven earlier post in place
        world.led.db.execute(
            "UPDATE messages SET body_text='さらに追記',content_hash=? "
            "WHERE message_id=101", ("f" * 64,))
        world.led.db.commit()
        notify_cards.sweep(world.led, CFG)
        await _deliver(world, worker)
        world.drain()
        assert len(thread.sent) == n
        assert "さらに追記" in "\n".join(thread.sent)
        # The unchanged first chunk also reconciles its current empty drug view.
        assert thread.messages[0].edits == 1 and thread.messages[0].view is None
        assert sum(message.edits for message in thread.messages[1:]) == 1

    asyncio.run(run())


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
    res = notify_transport.apply_card_resolve(world.led, {
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


def test_body_reclick_waits_for_fresh_result_after_source_deletion(world):
    world.seed()
    world.led.db.execute(
        "UPDATE messages SET body_text='synthetic retired body' WHERE message_id=100")
    world.led.db.commit()
    # body button lives on card_thread-less renders — dispatch that way
    cfg = {"notify": {k: v for k, v in CFG["notify"].items()
                      if k != "card_thread"},
           "signals": CFG["signals"]}
    world.dispatch(cfg=cfg)
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    token = world.token(spec, "body")
    act = world.mkactions(reg, bot)
    mid = bot.channels[42].sent[0].id
    first = FakeInteraction(f"mcs:a:{token}", message_id=mid)
    asyncio.run(world.interact(act, first))
    assert any("synthetic retired body" in m["content"]
               for m in first.followup.sent)
    world.led.db.execute(
        "UPDATE messages SET body_state='deleted' WHERE message_id=100")
    world.led.db.commit()
    second = FakeInteraction(f"mcs:a:{token}", message_id=mid)
    asyncio.run(world.interact(act, second))
    assert second.followup.sent
    assert all("synthetic retired body" not in m["content"]
               for m in second.followup.sent)
    assert all(m["ephemeral"] for m in second.followup.sent)


def test_body_messages_include_heading_in_discord_limit():
    body = "x" * 6000
    messages = text.body_messages({"title": "合成見出し" * 200, "body": body})
    assert all(len(message) <= 2000 for message in messages)
    assert sum(message.count("x") for message in messages) == len(body)


# ---------- task list + transitions ----------------------------------------

def _request(world, src_mid=100, title="経過確認", status="open", rev=1):
    rid = world.led.db.execute(
        "INSERT INTO requests(project_id,source_message_id,source_hash,"
        "title,status,revision,created_at,updated_at) "
        "VALUES(1,?,?,?,?,?,?,?)",
        (src_mid, "h" * 64, title, status, rev, NOW, NOW)).lastrowid
    world.led.db.commit()
    return rid


def test_action_tasks_ephemeral_list_and_transition(world):
    """📋 answers with an ephemeral list; a transition button on it
    applies through the same token pipeline even though its origin
    message is the followup, not the card."""
    world.seed()
    _request(world)
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'tasks')}",
                         message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    assert ix.followup.sent
    last = ix.followup.sent[-1]
    assert last["ephemeral"] and "経過確認" in last["content"]
    labels = [b.label for b in last["view"].items]
    assert any("対応中" in label for label in labels) \
        and any("完了" in label for label in labels)
    # click ⏳対応中 — the origin message is the ephemeral list itself
    btn = next(b for b in last["view"].items if "対応中" in b.label)
    ix2 = FakeInteraction(btn.custom_id, message_id=555)
    asyncio.run(world.interact(act, ix2))
    joined = "\n".join(m["content"] for m in ix2.followup.sent)
    assert "対応中" in joined and "経過確認" in joined
    row = world.led.db.execute(
        "SELECT status FROM requests WHERE title='経過確認'").fetchone()
    assert row["status"] == "in_progress"
    # both clicks landed in the interaction audit journal
    results = [f["action"] for e, f in world.logs
               if e == "interaction_result"]
    assert "tasks" in results and "task_status" in results


def test_action_tasks_empty_list(world):
    """A ☑ button still on a posted card after its last task left the
    list answers the empty list instead of failing."""
    world.seed()
    rid = _request(world)
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    world.led.db.execute("UPDATE requests SET status='cancelled' "
                         "WHERE request_id=?", (rid,))
    world.led.db.commit()
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'tasks')}",
                         message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    joined = "\n".join(m["content"] for m in ix.followup.sent)
    assert "タスクはありません" in joined


def test_action_task_stale_shows_latest_hint(world):
    """A transition clicked after another one landed reports the stale
    view instead of double-applying."""
    world.seed()
    _request(world)
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'tasks')}",
                         message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    view = ix.followup.sent[-1]["view"]
    done = next(b for b in view.items if "完了" in b.label)
    asyncio.run(world.interact(
        act, FakeInteraction(done.custom_id, message_id=555)))
    # replaying the stale ⏳ token from the same rendered list
    stale = next(b for b in view.items if "対応中" in b.label)
    ix2 = FakeInteraction(stale.custom_id, message_id=556)
    asyncio.run(world.interact(act, ix2))
    joined = "\n".join(m["content"] for m in ix2.followup.sent)
    assert "最新" in joined
    row = world.led.db.execute(
        "SELECT status,revision FROM requests").fetchone()
    assert row["status"] == "done" and row["revision"] == 2


def test_project_auto_authorizes_snapshot_projects(tmp_path):
    """project_ids_auto: the snapshot's patients table becomes the
    allowlist — a new patient works without a config edit, and a
    project that is not a patient still fails closed."""
    import sqlite3
    from hermes_plugin import projects as projects_mod
    snap = tmp_path / "snap.db"
    db = sqlite3.connect(str(snap))
    db.execute("CREATE TABLE patients(project_id INTEGER)")
    db.execute("INSERT INTO patients VALUES (99)")
    db.commit()
    db.close()
    settings = {"project_ids": {1}, "snapshot": str(snap)}
    assert not projects_mod.project_allowed(settings, 99)     # static only
    settings["project_ids_auto"] = True
    assert projects_mod.project_allowed(settings, 99)         # dynamic
    assert not projects_mod.project_allowed(settings, 100)    # no patient
    bad = {"project_ids": {1}, "project_ids_auto": True,
           "snapshot": str(tmp_path / "missing.db")}
    assert not projects_mod.project_allowed(bad, 99)          # fail closed
    assert projects_mod.project_allowed(bad, 1)               # static floor


def test_confirm_out_of_scope_after_preview_is_denied_but_cancellable(world):
    """Project scope gates only 確定: a project leaving scope after the
    preview answers 権限がありません without taking the confirm, while
    the actor's own 取消 still drops it. An actor who lost user/channel
    authorization can do neither."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "request")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    cid = _drive_to_confirm(world, act, tok, msg)
    confirm_id = cid[len("mcs:c:"):]
    before = len(list((world.data / "cmd_int").glob("*.json")))

    act._settings = {**SETTINGS, "allowed_user_ids": {"2002"},
                     "project_ids": set()}
    gone = FakeInteraction(cid + ":cancel", message_id=msg.id)
    asyncio.run(act.on_interaction(gone))
    assert gone.response.message["content"] == "権限がありません。"
    assert reg.confirm(confirm_id) is not None

    act._settings = {**SETTINGS, "project_ids": set()}
    ok = FakeInteraction(cid, message_id=msg.id)
    asyncio.run(act.on_interaction(ok))
    assert ok.response.message["content"] == "権限がありません。"
    assert not reg.confirm(confirm_id).get("in_flight")
    assert len(list((world.data / "cmd_int").glob("*.json"))) == before
    assert any(e == "interaction_denied"
               and f["reason"] == "project_not_allowed"
               for e, f in world.logs)

    cancel = FakeInteraction(cid + ":cancel", message_id=msg.id)
    asyncio.run(act.on_interaction(cancel))
    assert cancel.response.message["content"] == "取り消しました。"
    assert reg.confirm(confirm_id) is None
    assert len(list((world.data / "cmd_int").glob("*.json"))) == before


# ---------- card buttons: toggles, names, roles, task form, report --------

def _delivered(world):
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    return worker, reg, bot, spec


def _face(msg):
    if msg.embed is not None:
        return msg.embed.description
    return "\n".join(c.content for c in msg.view.items[0].children
                     if hasattr(c, "content"))


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


def test_role_member_can_click_and_others_are_told(world):
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    msg = bot.channels[42].sent[0]
    act = actions_mod.Actions(
        bot=bot, settings={**SETTINGS, "allowed_role_ids": {"555"}},
        root=str(world.data), reg=reg, log=lambda *a, **k: None)
    staff = FakeInteraction(f"mcs:a:{world.token(spec, 'assign')}",
                            user_id=3003, message_id=msg.id)
    staff.user = SimpleNamespace(id=3003, roles=[SimpleNamespace(id=555)])
    asyncio.run(world.interact(act, staff))
    tri = world.led.db.execute(
        "SELECT owner FROM notification_triage").fetchone()
    assert tri["owner"] == "discord:3003"
    other = FakeInteraction(f"mcs:a:{world.token(spec, 'ack')}",
                            user_id=3004, message_id=msg.id)
    other.user = SimpleNamespace(id=3004, roles=[SimpleNamespace(id=556)])
    asyncio.run(world.interact(act, other))
    assert other.response.message == {"content": "権限がありません。",
                                      "ephemeral": True, "pings": False}
    assert world.led.db.execute(
        "SELECT COUNT(*) FROM notification_acknowledgements"
    ).fetchone()[0] == 0
    # the guild id is the @everyone role every member holds — a config
    # that lists it never grants anyone
    act._settings = {**SETTINGS, "allowed_role_ids": {"7"}}
    everyone = FakeInteraction(f"mcs:a:{world.token(spec, 'ack')}",
                               user_id=3005, message_id=msg.id)
    everyone.user = SimpleNamespace(id=3005, roles=[SimpleNamespace(id=7)])
    asyncio.run(world.interact(act, everyone))
    assert everyone.response.message["content"] == "権限がありません。"
    act._settings = {**SETTINGS, "allowed_role_ids": {"555"}}
    # a pending followup keeps the role it was authorized by — and
    # loses it when the role leaves allowed_role_ids
    rec = {"actor": "discord:3003", "roles": ["555"], "origin": {
        "application_id": "1", "channel_id": "42", "guild_id": "7",
        "profile": "mcs"}, "project_ids": [1], "application_id": "1"}
    assert act._followup_authorized(rec) is True
    act._settings = SETTINGS
    assert act._followup_authorized(rec) is False


def test_task_modal_roster_prefill_confirm_and_footer(world):
    import mcs_signals
    world.seed()
    _llm_extract(world.led, 100,
                 {"requests": [{"action": "残薬を確認", "to": None}]})
    with world.led.db:
        mcs_signals.record_station_staff(world.led.db, [
            {"staff_id": 1, "name": "山田 花子", "station": "みどり薬局"},
            {"staff_id": 2, "name": "佐藤 一郎", "station": "みどり薬局"}])
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    msg = bot.channels[42].sent[0]
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'request')}",
                         message_id=msg.id)
    ix.user = SimpleNamespace(id=1001, display_name="佐藤 一郎")
    asyncio.run(world.interact(act, ix))
    modal = ix.response.modal
    assert modal.title == "タスク作成"
    task, pick, typed, due, reason = modal.children
    assert (reason.custom_id, reason.required) == ("reason", False)
    assert (task.custom_id, task.default, task.required) == (
        "task", "残薬を確認", True)
    assert pick.text == "担当者（一覧から）"
    assert [(o.value, o.default) for o in pick.component.options] == [
        ("山田 花子（みどり薬局）", False), ("佐藤 一郎（みどり薬局）", True)]
    assert pick.component.min_values == 0
    assert (typed.custom_id, due.custom_id) == ("assignee", "due_date")

    modal_id = modal.custom_id[len("mcs:m:"):]

    def submit(due_value):
        s = FakeInteraction(
            f"mcs:m:{modal_id}", message_id=msg.id, components=[
                {"components": [{"custom_id": "task", "value": "残薬を確認"}]},
                {"component": {"custom_id": "assignee_pick",
                               "values": ["山田 花子（みどり薬局）"]}},
                {"components": [{"custom_id": "assignee", "value": ""}]},
                {"components": [{"custom_id": "due_date",
                                 "value": due_value}]}])
        asyncio.run(world.interact(act, s))
        return s.followup.sent[-1]

    assert submit("2026-13-01")["content"] \
        == "期限は YYYY-MM-DD 形式で入力してください。"
    ix2 = FakeInteraction(f"mcs:a:{world.token(spec, 'request')}",
                          message_id=msg.id)
    asyncio.run(world.interact(act, ix2))
    modal_id = ix2.response.modal.custom_id[len("mcs:m:"):]
    preview = submit("2026-10-01")
    assert preview["content"].startswith("**確認 — タスク作成**\n内容: 残薬を確認")
    assert preview["content"].endswith("\n理由: 通知カードからタスク作成")
    cid = next(b.custom_id for b in preview["view"].items
               if not b.custom_id.endswith(":cancel"))
    confirm = FakeInteraction(cid, message_id=msg.id)
    asyncio.run(world.interact(act, confirm))
    row = world.led.db.execute(
        "SELECT title,assignee,due_date FROM requests").fetchone()
    assert tuple(row) == ("残薬を確認", "山田 花子（みどり薬局）", "2026-10-01")
    _, spec2 = world.spec()
    footer = "\n".join(f.get("text", "") for f in spec2["parts"]["footer"])
    assert "📝 タスク 1件" \
        in footer
    assert any(b["id"] == "tasks" for row in spec2["parts"]["action_rows"]
               for b in row)


def test_summary_click_answers_ephemeral(world):
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    msg = bot.channels[42].sent[0]
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'summary')}",
                         message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    sent = "\n".join(m["content"] for m in ix.followup.sent)
    assert "患者の記録まとめ" in sent and "暫定集約" not in sent and "集約資料なし" in sent
    assert all(m["ephemeral"] for m in ix.followup.sent)


def test_report_modal_records_feedback(world):
    world.seed()
    aid = _llm_extract(world.led, 101, {"summary": "s"})
    world.led.db.commit()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    msg = bot.channels[42].sent[0]
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'report')}",
                         message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    field, note = ix.response.modal.children
    assert [o.value for o in field.component.options] == [
        "summary", "urgency", "meds", "symptoms", "requests", "vitals", "other"]
    assert field.component.min_values == 1
    modal_id = ix.response.modal.custom_id[len("mcs:m:"):]
    s = FakeInteraction(f"mcs:m:{modal_id}", message_id=msg.id, components=[
        {"component": {"custom_id": "field", "values": ["vitals"]}},
        {"components": [{"custom_id": "note", "value": ""}]}])
    asyncio.run(world.interact(act, s))
    preview = s.followup.sent[-1]
    assert "箇所: バイタル" in preview["content"]
    cid = next(b.custom_id for b in preview["view"].items
               if not b.custom_id.endswith(":cancel"))
    asyncio.run(world.interact(act, FakeInteraction(cid, message_id=msg.id)))
    row = world.led.db.execute(
        "SELECT content FROM artifacts WHERE kind='extract_feedback_v1'"
    ).fetchone()
    content = json.loads(row["content"])
    assert (content["artifact_id"], content["field"], content["note"]) == (
        aid, "vitals", "抽出の誤り報告（バイタル）")


def test_old_defer_button_answers_retired(world):
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    msg = bot.channels[42].sent[0]
    card = world.card()
    tok = notify_cards._mint_token(world.led.db, card["card_id"], "defer",
                                   None, {}, NOW)
    world.led.db.commit()
    reg.put_tokens({tok: {"action": "defer", "card_key": card["card_key"],
                          "kind": "thread", "project_id": 1,
                          "context": {"project_id": 1},
                          "channel_id": "42"}})
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    assert ix.followup.sent[-1]["content"] == text.ERR_JA["action_retired"]
    assert world.led.db.execute(
        "SELECT COUNT(*) FROM notification_triage").fetchone()[0] == 0


# ---------- 📋 / 🗂 / 🔎 / 🚫 reason code ------------------------------------

def test_my_tasks_uses_display_name_and_project_scope(world):
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    _add_request(world.led, "残薬確認", "山田 花子（みどり薬局）",
                 "2026-01-01")
    _add_request(world.led, "範囲外の件", "山田 花子", pid=2)
    _add_request(world.led, "他人の件", "佐藤")
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'mytasks')}",
                         message_id=bot.channels[42].sent[0].id)
    ix.user = SimpleNamespace(id=1001, display_name="山田 花子")
    asyncio.run(world.interact(act, ix))
    out = "\n".join(m["content"] for m in ix.followup.sent)
    assert all(m["ephemeral"] for m in ix.followup.sent)
    assert "自分のタスク（担当: 山田 花子）" in out
    assert "⚠期限切れ" in out and "残薬確認" in out
    # project 2 is outside this deployment's scope; 佐藤 is not the clicker
    assert "範囲外の件" not in out and "他人の件" not in out


def test_unacked_list_links_the_card(world):
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    msg = bot.channels[42].sent[0]
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'unacked')}",
                         message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    out = "\n".join(m["content"] for m in ix.followup.sent)
    assert "■ 患者A" in out and "未確認" in out
    assert f"https://discord.com/channels/7/42/{msg.id}" in out
    assert "作業が済んだかどうかは表しません" not in out


def test_search_modal_answers_hits_ephemeral(world):
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    msg = bot.channels[42].sent[0]
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'search')}",
                         message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    modal = ix.response.modal
    assert modal.title == "この患者を検索"
    modal_id = modal.custom_id[len("mcs:m:"):]
    s = FakeInteraction(f"mcs:m:{modal_id}", message_id=msg.id, components=[
        {"components": [{"custom_id": "query", "value": "本文"}]}])
    asyncio.run(world.interact(act, s))
    out = "\n".join(m["content"] for m in s.followup.sent)
    assert all(m["ephemeral"] for m in s.followup.sent)
    assert "「本文」の検索結果" in out and "2件（取得済み投稿・新しい順）" in out
    assert "履歴取得:" in out          # the fetched range, no caveat sentence
    # another member cannot submit the clicker's form
    s2 = FakeInteraction(f"mcs:m:{modal_id}", user_id=2002,
                         message_id=msg.id, components=[])
    asyncio.run(world.interact(act, s2))
    assert "検索結果" not in (s2.response.message or {}).get("content", "")


def test_late_search_result_reaches_the_sweep_without_pings(world,
                                                             monkeypatch):
    """🔎 hits that outlive the 20 s wait are delivered by the followup
    sweep like other view clicks; every ephemeral/webhook send disables
    mentions."""
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    msg = bot.channels[42].sent[0]
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'search')}",
                         message_id=msg.id)
    asyncio.run(world.interact(act, ix))
    modal_id = ix.response.modal.custom_id[len("mcs:m:"):]
    monkeypatch.setattr(actions_mod, "RESULT_WAIT_S", 0)
    s = FakeInteraction(f"mcs:m:{modal_id}", message_id=msg.id, components=[
        {"components": [{"custom_id": "query", "value": "本文"}]}])
    asyncio.run(act.on_interaction(s))
    assert "受け付けました" in s.followup.sent[-1]["content"]
    assert not any(m["pings"] for m in s.followup.sent)
    assert reg.followups()
    world.drain()
    asyncio.run(act.sweep_followups())
    sent = sys.modules["discord"].Webhook.sent
    assert any("「本文」の検索結果" in m["content"] for m in sent)
    assert all(m["allowed_mentions"] is not None
               and not m["allowed_mentions"].users for m in sent)
    assert not reg.followups()


def test_dismiss_reason_code_select_reaches_the_ledger(world):
    world.seed(mids=(100,))
    worker, reg, bot, spec, msg, thread = _delivered_source_signal(world)
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'dismiss')}",
                         message_id=msg.id, channel_id=thread.id, channel=thread)
    asyncio.run(act.on_interaction(ix))
    code, note = ix.response.modal.children
    assert [o.value for o in code.component.options] == [
        "false_positive", "already_handled", "duplicate", "out_of_scope",
        "other"]
    modal_id = ix.response.modal.custom_id[len("mcs:m:"):]
    submit = FakeInteraction(f"mcs:m:{modal_id}", message_id=msg.id, channel_id=thread.id, channel=thread,
                             components=[
        {"component": {"custom_id": "reason_code", "values": ["duplicate"]}},
        {"components": [{"custom_id": "note", "value": ""}]}])
    asyncio.run(world.interact(act, submit))
    preview = submit.followup.sent[-1]
    assert "区分: 重複" in preview["content"]
    cid = next(b.custom_id for b in preview["view"].items
               if not b.custom_id.endswith(":cancel"))
    asyncio.run(world.interact(act, FakeInteraction(cid, message_id=msg.id, channel_id=thread.id, channel=thread)))
    row = json.loads(world.led.db.execute(
        "SELECT content FROM artifacts WHERE kind='signal_v1' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()[0])
    assert (row["state"], row["dismiss_reason_code"], row["dismiss_reason"]) \
        == ("dismissed", "duplicate", "重複")


def test_menu_select_dispatches_like_the_button(world):
    """The 他の操作 select (custom_id mcs:menu, value = token) runs the
    same gated dispatch as an mcs:a:<token> click."""
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    msg = bot.channels[42].sent[0]
    act = world.mkactions(reg, bot)
    token = world.token(spec, "summary")
    button = FakeInteraction(f"mcs:a:{token}", message_id=msg.id)
    asyncio.run(world.interact(act, button))
    menu = FakeInteraction("mcs:menu", message_id=msg.id)
    menu.data["values"] = [token]
    asyncio.run(world.interact(act, menu))
    assert menu.followup.sent and [m["content"] for m in menu.followup.sent] \
        == [m["content"] for m in button.followup.sent]
    outsider = FakeInteraction("mcs:menu", message_id=msg.id, user_id=9999)
    outsider.data["values"] = [token]
    asyncio.run(world.interact(act, outsider))
    assert "権限がありません。" in str(outsider.response.__dict__) \
        + str(outsider.followup.sent)
    # a malformed select payload is ignored silently
    bad = FakeInteraction("mcs:menu", message_id=msg.id)
    bad.data["values"] = [token, token]
    asyncio.run(world.interact(act, bad))
    assert not bad.followup.sent


def _drug_thread(world, monkeypatch, *, medication_count=1, older_medication=False):
    import hashlib
    import drug_map
    from test_drug_map import DOCUMENT
    world.seed()
    medication = {"name": "キラナ", "dose": "5mg", "action": "start",
                  "subject": "patient", "status": "current", "negated": False,
                  "unverified": False, "evidence": "fictional quotation only"}
    _llm_extract(world.led, 101, {"meds": [
        {**medication, "name": "キラナ" if i == 0 else f"合成薬{i + 1}"}
        for i in range(medication_count)]})
    if older_medication:
        _llm_extract(world.led, 100, {"meds": [{**medication, "name": "旧投稿薬"}]})
    raw = json.dumps(DOCUMENT, ensure_ascii=False).encode()
    path = world.data / "fictional-drug-map.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    sha = hashlib.sha256(raw).hexdigest()
    drug_map.derive(world.led, drug_map.load(path, expected_sha256=sha))
    monkeypatch.setitem(CFG, "drug_map", {"path": str(path), "sha256": sha})
    world.dispatch()
    _, reg, bot, spec = _delivered(world)
    message = bot.channels[42].sent[0]
    thread = message.threads[0][1]
    body = thread.messages[0]
    assert spec["parts"]["thread_drug_actions"] is True
    return world.mkactions(reg, bot), reg, bot, spec, thread, body


def test_meds_thread_button_answers_only_clicker(world, monkeypatch):
    act, _, bot, spec, thread, body = _drug_thread(world, monkeypatch)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'meds')}",
                         channel_id=thread.id, channel=SimpleNamespace(parent_id=42),
                         message_id=body.id)
    assert actions_mod._origin(ix, "mcs")["thread_id"] == str(thread.id)
    shared_body = list(thread.sent)
    asyncio.run(world.interact(act, ix))
    out = "\n".join(m["content"] for m in ix.followup.sent)
    assert "キラナ" in out and all(m["ephemeral"] for m in ix.followup.sent)
    assert not any(m["pings"] for m in ix.followup.sent)
    assert len(bot.channels[42].sent) == 1
    assert thread.sent == shared_body


@pytest.mark.parametrize("delayed", [False, True])
def test_drug_search_thread_modal_and_followup_stay_private(world, monkeypatch, delayed):
    act, reg, bot, spec, thread, body = _drug_thread(world, monkeypatch)
    ix = FakeInteraction(f"mcs:a:{world.token(spec, 'drugsearch')}",
                         channel_id=thread.id, channel=SimpleNamespace(parent_id=42),
                         message_id=body.id)
    shared_body = list(thread.sent)
    asyncio.run(world.interact(act, ix))
    assert ix.response.modal is not None
    modal_id = ix.response.modal.custom_id[len("mcs:m:"):]
    assert reg.modal(modal_id)["origin"]["thread_id"] == str(thread.id)
    submit = FakeInteraction(f"mcs:m:{modal_id}", channel_id=thread.id,
                             channel=SimpleNamespace(parent_id=42), message_id=body.id,
                             components=[{"components": [{
                                 "custom_id": "query", "value": "キラナ"}]}])
    if delayed:
        monkeypatch.setattr(actions_mod, "RESULT_WAIT_S", 0)
        asyncio.run(act.on_interaction(submit))
        assert reg.followups()
        rec = next(iter(reg.followups().values()))
        assert rec["origin"]["thread_id"] == str(thread.id)
        world.drain()
        asyncio.run(act.sweep_followups())
        answers = sys.modules["discord"].Webhook.sent
    else:
        asyncio.run(world.interact(act, submit))
        answers = submit.followup.sent
    assert any("キラナ" in m["content"] for m in answers)
    assert all(m["ephemeral"] for m in answers)
    assert len(bot.channels[42].sent) == 1
    assert thread.sent == shared_body


@pytest.mark.parametrize("delayed", [False, True])
def test_private_medication_navigation_registers_tokens_before_first_chunk(world, monkeypatch,
                                                                         delayed):
    _, reg, bot = world.mkworker()
    act = world.mkactions(reg, bot)
    token = "a" * 32
    result = {"outcome": "applied", "action": "list",
              "navigation": [{"id": "meds", "ui": "button", "style": "secondary",
                              "label": "次の5件", "token": token}],
              "token_ctx": {token: {"action": "meds", "project_id": 1}}}
    answer = [("synthetic first", None), ("synthetic second", None)]
    original = (sys.modules["discord"].Webhook.send if delayed else FakeFollowup.send)

    async def checked_send(self, *args, **kwargs):
        assert reg.token(token) is not None
        return await original(self, *args, **kwargs)

    if delayed:
        monkeypatch.setattr(sys.modules["discord"].Webhook, "send", checked_send)
        origin = {key: SETTINGS[key] for key in
                  ("application_id", "channel_id", "guild_id", "profile")}
        origin["thread_id"] = "7700"
        reg.put_followup("synthetic-cmd", {"application_id": "1", "token": "synthetic",
                                         "actor": "discord:1001", "origin": origin,
                                         "project_ids": [1]})
        monkeypatch.setattr(paths, "read_result", lambda *_: result)
        monkeypatch.setattr(text, "view_answer", lambda *_: answer)
        asyncio.run(act.sweep_followups())
        sent = sys.modules["discord"].Webhook.sent
    else:
        monkeypatch.setattr(FakeFollowup, "send", checked_send)
        interaction = FakeInteraction("synthetic", channel_id=7700,
                                      channel=SimpleNamespace(parent_id=42))
        asyncio.run(act._send_answer(interaction, answer, result))
        sent = interaction.followup.sent
    assert len(sent) == 2 and all(message["ephemeral"] for message in sent)
    assert sent[0]["view"].items[0].custom_id == f"mcs:a:{token}"
    assert sent[0]["view"].stopped
    assert sent[1]["view"] is MISSING


def test_lost_registry_thread_drug_token_refreshes_and_recovers_buttons(world, monkeypatch):
    act, reg, bot, spec, thread, body = _drug_thread(world, monkeypatch)
    old_token = world.token(spec, "meds")
    reg._data["tokens"].pop(old_token)
    missing = FakeInteraction(f"mcs:a:{old_token}", channel_id=thread.id,
                              channel=SimpleNamespace(parent_id=42), message_id=body.id)
    asyncio.run(world.interact(act, missing))
    world.drain()
    receipts = [json.loads(row[0]) for row in world.led.db.execute(
        "SELECT receipt_json FROM command_receipts")]
    refresh = [receipt for receipt in receipts if receipt.get("kind") == "refresh"]
    assert len(refresh) == 1 and refresh[0]["outcome"] == "applied"
    _, fresh = world.spec()
    assert fresh["render_rev"] > spec["render_rev"]
    worker, recovered_registry, _ = world.mkworker(bot=bot)
    asyncio.run(_deliver(world, worker))
    new_token = world.token(fresh, "meds")
    assert new_token != old_token
    assert recovered_registry.token(new_token) is not None
    assert f"mcs:a:{new_token}" in [item.custom_id for item in body.view.items]
    assert len(bot.channels[42].sent) == 1
    recovered = world.mkactions(recovered_registry, bot)
    click = FakeInteraction(f"mcs:a:{new_token}", channel_id=thread.id,
                            channel=SimpleNamespace(parent_id=42), message_id=body.id)
    asyncio.run(world.interact(recovered, click))
    assert any("キラナ" in item["content"] for item in click.followup.sent)
    assert all(item["ephemeral"] for item in click.followup.sent)


@pytest.mark.parametrize("delayed", [False, True])
def test_private_medication_navigation_pages_posts_and_rejects_other_origin(world, monkeypatch,
                                                                          delayed):
    act, reg, bot, spec, thread, body = _drug_thread(
        world, monkeypatch, medication_count=6, older_medication=True)
    initial = FakeInteraction(f"mcs:a:{world.token(spec, 'meds')}",
                              channel_id=thread.id, channel=SimpleNamespace(parent_id=42),
                              message_id=body.id)
    shared_body = list(thread.sent)
    if delayed:
        original_wait = actions_mod.RESULT_WAIT_S
        monkeypatch.setattr(actions_mod, "RESULT_WAIT_S", 0)
        asyncio.run(act.on_interaction(initial))
        assert reg.followups()
        world.drain()
        asyncio.run(act.sweep_followups())
        first = sys.modules["discord"].Webhook.sent[0]
        monkeypatch.setattr(actions_mod, "RESULT_WAIT_S", original_wait)
    else:
        asyncio.run(world.interact(act, initial))
        first = initial.followup.sent[0]
    assert first["ephemeral"]
    assert "キラナ" in first["content"] and "合成薬6" not in first["content"]
    controls = {button.label: button.custom_id for button in first["view"].items}
    assert {"次の5件", "古い投稿"} <= controls.keys()

    def click(custom_id, *, user_id=1001, thread_id=None):
        interaction = FakeInteraction(custom_id, user_id=user_id,
                                      channel_id=thread.id if thread_id is None else thread_id,
                                      channel=SimpleNamespace(parent_id=42), message_id=999999)
        asyncio.run(world.interact(act, interaction))
        return interaction

    # A native ephemeral response message has a new id, but remains in the same thread.
    page = click(controls["次の5件"])
    assert any("合成薬6" in message["content"] for message in page.followup.sent)
    previous = next(button for button in page.followup.sent[0]["view"].items
                    if button.label == "前の5件")
    back = click(previous.custom_id)
    assert any("キラナ" in message["content"] for message in back.followup.sent)
    older = click(controls["古い投稿"])
    assert any("旧投稿薬" in message["content"] for message in older.followup.sent)
    assert any(button.label == "新しい投稿" for button in older.followup.sent[0]["view"].items)
    # Adapter authorization alone is insufficient: the runner also pins actor and thread.
    act._settings = {**SETTINGS, "allowed_user_ids": {"1001", "2002"}}
    outsider = click(controls["次の5件"], user_id=2002)
    fresh_next = next(button.custom_id for button in back.followup.sent[0]["view"].items
                      if button.label == "次の5件")
    sibling = click(fresh_next, thread_id=thread.id + 1)
    for denied in (outsider, sibling):
        assert denied.followup.sent
        assert all("合成薬6" not in message["content"] for message in denied.followup.sent)
    errors = {json.loads(row[0]).get("error") for row in world.led.db.execute(
        "SELECT receipt_json FROM command_receipts WHERE outcome='rejected'")}
    assert "actor_mismatch" in errors and "origin_mismatch" in errors
    assert all(message["ephemeral"] for interaction in (initial, page, back, older)
               for message in interaction.followup.sent)
    assert thread.sent == shared_body and len(bot.channels[42].sent) == 1
    assert reg.token(controls["次の5件"][len("mcs:a:"):]) is not None


def test_thread_post_line_is_subtext_but_signal_patient_heading_is_not():
    from adapters.discord.cards import _zones, escape_md
    containers = [{"type": "heading", "text": "合成"},
                  {"type": "text", "text": "10-01 09:40 合成さん", "rule": True},
                  {"type": "text", "text": "📋 要約"}]
    thread = {"kind": "thread", "parts": {"containers": containers, "footer": []}}
    zones, _ = _zones(thread, escape_md)
    assert zones[1] == ["-# 10-01 09:40 合成さん", "📋 要約"]
    signal = {"kind": "signal", "parts": {"containers": containers, "footer": []}}
    zones, _ = _zones(signal, escape_md)
    assert zones[1][0] == "10-01 09:40 合成さん"
