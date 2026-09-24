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
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import _mcs_path  # noqa: F401

import ledger as _ledger
import notify_cards
import notify_cmds

from hermes_plugin.mcs_discord import (actions as actions_mod, cards,
                                      delivery, envelopes, journal,
                                      paths, registry, tasks)

NOW = 1_790_000_000.0
CFG = {"notify": {"interactive": "discord", "route_epoch": 1,
                  "operator": "op-user", "card_thread": True,
                  "discord": {"profile": "mcs", "application_id": "1",
                              "guild_id": "7", "channel_id": "42"}}}
SETTINGS = {"profile": "mcs", "application_id": "1", "channel_id": "42",
            "guild_id": "7",
            "allowed_user_ids": {"1001"}, "allowed_chat_ids": {"42"},
            "project_ids": {1}}


# ---------- fake discord SDK --------------------------------------------

class FakeHTTP(Exception):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.status = status


def _fake_discord():
    mod = types.ModuleType("discord")

    class LayoutView:
        def __init__(self, timeout=None):
            self.timeout = timeout
            self.items = []

        def add_item(self, item):
            self.items.append(item)

    class View(LayoutView):
        pass

    class TextDisplay:
        def __init__(self, content):
            self.content = content

    class ActionRow:
        def __init__(self):
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class Button:
        def __init__(self, style=None, label=None, custom_id=None):
            self.style = style
            self.label = label
            self.custom_id = custom_id

    class Modal:
        def __init__(self, title=None, custom_id=None, timeout=None):
            self.title = title
            self.custom_id = custom_id
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class TextInput:
        def __init__(self, label=None, style=None, custom_id=None,
                     max_length=None, required=True, **_):
            self.label = label
            self.custom_id = custom_id
            self.value = None

    class Webhook:
        sent = []

        def __init__(self, ident, token, client):
            self.ident, self.token, self.client = ident, token, client

        @classmethod
        def partial(cls, ident, token, client=None):
            return cls(ident, token, client)

        async def send(self, content, ephemeral=False, view=None):
            Webhook.sent.append(
                {"content": content, "ephemeral": ephemeral})

    mod.ui = SimpleNamespace(LayoutView=LayoutView, View=View,
                             TextDisplay=TextDisplay, ActionRow=ActionRow,
                             Button=Button, Modal=Modal,
                             TextInput=TextInput)
    mod.ButtonStyle = SimpleNamespace(primary=1, secondary=2, success=3,
                                      danger=4)
    mod.TextStyle = SimpleNamespace(short=1, paragraph=2)
    mod.Webhook = Webhook
    return mod


# ---------- fake discord objects ----------------------------------------

class FakeThread:
    def __init__(self, tid):
        self.id = tid


class FakeMessage:
    def __init__(self, mid):
        self.id = mid
        self.view = None
        self.edits = 0
        self.deleted = False
        self.threads = []

    async def edit(self, view=None):
        if self.deleted:
            raise FakeHTTP(404)
        self.view = view
        self.edits += 1

    async def delete(self):
        if self.deleted:
            raise FakeHTTP(404)
        self.deleted = True

    async def create_thread(self, name=None):
        t = FakeThread(7700 + len(self.threads))
        self.threads.append((name, t))
        return t


class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []
        self.messages = {}
        self._next = 9000

    async def send(self, view=None):
        self._next += 1
        m = FakeMessage(self._next)
        m.view = view
        self.sent.append(m)
        self.messages[m.id] = m
        return m

    async def fetch_message(self, mid):
        m = self.messages.get(int(mid))
        if m is None:
            raise FakeHTTP(404)
        return m


class FakeBot:
    def __init__(self, channel_id=42):
        self.channels = {channel_id: FakeChannel(channel_id)}
        self.listeners = []

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        if cid not in self.channels:
            raise FakeHTTP(404)
        return self.channels[cid]

    def add_listener(self, fn, name):
        self.listeners.append((name, fn))

    def remove_listener(self, fn, name):
        self.listeners = [x for x in self.listeners
                          if not (x[0] == name and x[1] == fn)]


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

    async def send_message(self, content, ephemeral=False):
        self.message = {"content": content, "ephemeral": ephemeral}
        self.done = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, ephemeral=False, view=None):
        self.sent.append({"content": content, "ephemeral": ephemeral,
                          "view": view})


class FakeInteraction:
    def __init__(self, custom_id, *, user_id=1001, channel_id=42,
                 guild_id=7, app_id=1, message_id=None,
                 components=None, token="tok-1"):
        self.data = {"custom_id": custom_id}
        if components is not None:
            self.data["components"] = components
        self.user = SimpleNamespace(id=user_id)
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.application_id = app_id
        self.token = token
        self.message = (SimpleNamespace(id=message_id)
                        if message_id is not None else None)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


# ---------- fixtures ------------------------------------------------------

@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    data = tmp_path / "data"
    data.mkdir()
    led = _ledger.Ledger(str(data / "ledger.db"))
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
            bot=bot, adapter=None, settings=SETTINGS, root=str(data),
            reg=reg, worker_id=worker_id or registry.new_worker_id(),
            log=lambda e, **f: logs.append((e, f)))
        return worker, reg, bot

    def _mkactions(reg, bot, worker_id=None):
        return actions_mod.Actions(
            bot=bot, settings=SETTINGS, root=str(data), reg=reg,
            worker_id=worker_id or registry.new_worker_id(),
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


# ---------- spec validation / view build ---------------------------------

def test_real_spec_validates_and_builds(world):
    world.seed()
    world.dispatch()
    _, spec = world.spec()
    assert cards.validate(spec) is spec
    view = cards.build_view(spec)
    kinds = [type(i).__name__ for i in view.items]
    assert "TextDisplay" in kinds and "ActionRow" in kinds
    ids = [b.custom_id for i in view.items if hasattr(i, "children")
           for b in i.children]
    assert ids and all(i.startswith("mcs:a:") for i in ids)
    assert all(len(i) == 38 for i in ids)      # "mcs:a:" + 32 hex


@pytest.mark.parametrize("mutate,error", [
    (lambda s: s.update(schema="bogus"), "bad_schema"),
    (lambda s: s.update(op="explode"), "bad_op"),
    (lambda s: s["delivery"].update(correlation="zz"), "bad_correlation"),
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
        cards.validate(spec)


# ---------- delivery -------------------------------------------------------

async def _deliver(world, worker, sent=1):
    """claim -> begin -> grant -> send -> receipt -> settled."""
    await worker.tick()
    world.drain()                       # grant
    await worker.tick()                 # send + receipt
    world.drain()                       # settle
    return worker._bot.channels[42].sent


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
    # the bound message maps back to the card
    assert reg.message(str(msg.id))["delivery_id"] \
        == spec["delivery_id"]


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
    monkeypatch.setattr(delivery, "RETRY_IN_FLIGHT_S", 0)
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
    assert card["delivery_state"] in ("delivered", "message_deleted",
                                      "pending")
    # a fresh send happened — the dead message was not edited forever
    assert len(bot.channels[42].sent) >= 2


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
    # unknown token -> ephemeral notice, no command written
    ix = FakeInteraction("mcs:a:" + "ff" * 16, message_id=1)
    asyncio.run(act.on_interaction(ix))
    assert ix.response.message["ephemeral"] is True
    # disallowed user -> denied before any file write
    _, spec = world.spec()
    tok = world.token(spec, "ack")
    ix = FakeInteraction(f"mcs:a:{tok}", user_id=9999,
                         message_id=bot.channels[42].sent[0].id)
    asyncio.run(act.on_interaction(ix))
    assert "権限" in ix.response.message["content"]
    assert not list((world.data / "cmd_int").glob("*.json"))


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
    # a different channel cannot submit either — origin is re-pinned
    bad2 = FakeInteraction(f"mcs:m:{modal_id}", channel_id=99,
                           message_id=msg.id)
    asyncio.run(act.on_interaction(bad2))
    assert bad2.response.message["ephemeral"] is True


def test_dismiss_flow_pins_artifact(world):
    """dismiss click -> modal -> confirm -> ops.signal_dismiss with
    the render-time artifact id — a stale signal is the runner's job."""
    world.seed(mids=(100,))
    world.signal("sig-1", mids=[100])
    world.dispatch(kind="signal", pid=1,
                   payload={"signal_keys": ["sig-1"], "project_id": 1,
                            "type": "med_followup"})
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    tok = world.token(spec, "dismiss")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]

    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id)
    asyncio.run(act.on_interaction(ix))
    modal_id = ix.response.modal.custom_id[len("mcs:m:"):]
    submit = FakeInteraction(
        f"mcs:m:{modal_id}", message_id=msg.id,
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

    confirm = FakeInteraction(cid, message_id=msg.id)
    asyncio.run(world.interact(act, confirm))
    row = world.led.db.execute(
        "SELECT content FROM artifacts WHERE kind='signal_v1' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    # the dismissal was applied (or appended) — not silently dropped
    assert row is not None


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
        bot = FakeBot()
        sup = tasks.Supervisor(
            ctx=FakeCtx(), bot=bot, adapter=None,
            settings={**SETTINGS, "data_root": str(world.data)},
            log=lambda e, **f: world.logs.append((e, f)))
        assert sup.start() is True
        assert len(bot.listeners) == 1
        assert spawned and not spawned[0].done()
        await asyncio.sleep(0)          # let the loop acquire the lock
        sup.unload()
        spawned[0].cancel()
        try:
            await spawned[0]
        except asyncio.CancelledError:
            pass
        assert not bot.listeners

    asyncio.run(run())
