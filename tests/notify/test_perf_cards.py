"""D4 load measurement — RC20/RC26 §7 intervals at synthetic scale.

Real runner + real worker against fake Discord transport; synthetic
rows and a temp ledger only. Records:

- カード配送: spec atomic publish -> successful receipt DB commit
- 操作適用:  click command file -> applied/rejected receipt commit
- queue/lock/sweep/gc/snapshot breakdown at 100 / 1,000 / 10,000 cards

The p95 budgets (30s delivery / 6s op-apply, non-contended) are
asserted on the local pipeline — the remainder is what real Discord
HTTP consumes, verified separately in D5.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import ledger as _ledger
import notify_cards
import notify_cmds

from hermes_plugin.mcs_delivery import envelopes, registry
from hermes_plugin.mcs_discord import delivery
from notify_testkit import NOW, SETTINGS

CFG = {"notify": {"interactive": "discord", "route_epoch": 1,
                  "operator": "op-user",
                  "discord": {"profile": "mcs", "application_id": "1",
                              "guild_id": "7", "channel_id": "42"}}}
SCOPE = {"profile": "mcs", "application_id": "1",
         "guild_id": "7", "channel_id": "42"}


def _fake_discord():
    mod = types.ModuleType("discord")

    class LayoutView:
        def __init__(self, timeout=None):
            self.items = []

        def add_item(self, item):
            self.items.append(item)

        def stop(self):
            self.stopped = True

    class TextDisplay:
        def __init__(self, content):
            self.content = content

    class ActionRow:
        def __init__(self):
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class Container:
        def __init__(self, *children, accent_color=None, **_):
            self.children = list(children)
            self.accent_color = accent_color

    class Button:
        def __init__(self, style=None, label=None, custom_id=None,
                     url=None):
            self.custom_id = custom_id

    class SelectOption:
        def __init__(self, label=None, value=None, **_):
            self.label, self.value = label, value

    class Select:
        def __init__(self, custom_id=None, options=(), **_):
            self.custom_id, self.options = custom_id, list(options)

    class Separator:
        def __init__(self, visible=True, spacing=None):
            self.visible, self.spacing = visible, spacing

    mod.Embed = lambda description=None, colour=None: SimpleNamespace(description=description, colour=colour)
    mod.ui = SimpleNamespace(View=LayoutView, LayoutView=LayoutView, TextDisplay=TextDisplay,
                             ActionRow=ActionRow, Container=Container,
                             Button=Button, Select=Select, Separator=Separator)
    mod.SelectOption = SelectOption
    mod.SeparatorSpacing = SimpleNamespace(small=1, large=2)
    mod.ButtonStyle = SimpleNamespace(primary=1, secondary=2,
                                      success=3, danger=4, link=5)
    mod.AllowedMentions = SimpleNamespace(none=lambda: "no-pings")
    return mod


class FakeMessage:
    def __init__(self, mid):
        self.id = mid


class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []
        self._next = 9000

    async def send(self, view=None, allowed_mentions=None, content=None, embed=None):
        self._next += 1
        m = FakeMessage(self._next)
        self.sent.append(m)
        return m

    async def fetch_message(self, mid):
        for m in self.sent:
            if m.id == int(mid):
                return m
        raise KeyError(mid)


class FakeBot:
    def __init__(self, http=None):
        self.channels = {42: FakeChannel(42)}
        self.http = http

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        return self.channels[cid]


class _NoWireSession:
    def request(self, *_a, **_kw):
        raise AssertionError("fake channels never reach the session")


class FakeHTTPClient:
    """Verified-SDK shape the single-post guard requires before a
    create POST (as in tests/adapters/discord/test_mcs_discord_delivery.py)."""

    user_agent = ("DiscordBot (https://github.com/Rapptz/discord.py 2.7.1)"
                  " Python/3.11 aiohttp/3.14.3")

    def __init__(self):
        self._HTTPClient__session = _NoWireSession()


def _mk_ledger(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    return _ledger.Ledger(str(data / "ledger.db")), data


def _seed_patient(led, pid):
    led.db.execute(
        "INSERT INTO patients(project_id,patient_name,is_archived) "
        "VALUES(?,?,0)", (pid, f"患者{pid}"))


def _seed_msg(led, mid, pid, root=None):
    led.db.execute(
        "INSERT INTO messages(message_id,project_id,sender_name,"
        "posted_at,posted_at_ts,body_text,content_hash,body_state,"
        "parent_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (mid, pid, "職員", "2026-09-24T08:00", int(NOW) + mid,
         "本文", f"{mid:064x}", "full", root))


def _pctl(samples, p):
    if not samples:
        return 0.0
    s = sorted(samples)
    i = min(len(s) - 1, max(0, int(len(s) * p + 0.999) - 1))
    return s[i]


def _report(title, samples, budget_s=None):
    if not samples:
        return {"p50": 0, "p95": 0, "max": 0}
    r = {"p50": round(_pctl(samples, 0.50), 4),
         "p95": round(_pctl(samples, 0.95), 4),
         "max": round(max(samples), 4),
         "n": len(samples)}
    line = (f"{title:<44} p50={r['p50']:>8}s "
            f"p95={r['p95']:>8}s max={r['max']:>8}s n={r['n']}")
    if budget_s is not None:
        line += f"  budget<={budget_s}s"
    print("\n  " + line)
    return r


# ---------- the pipeline measurement ---------------------------------

def _measure_pipeline(world, n):
    """Dispatch n intents, then drive the whole claim->send->settle
    loop. Returns per-stage wall times and per-card e2e latencies."""
    led, data, worker, reg = world

    # stage 1: dispatch -> spec files exist (the §7 start point)
    t0 = time.perf_counter()
    starts = {}
    for i in range(n):
        eid = led.outbox_add("new_messages", 1 + i,
                             {"message_ids": [10_000 + i]})
        ev = dict(led.db.execute(
            "SELECT * FROM notify_outbox WHERE event_id=?",
            (eid,)).fetchone())
        notify_cards.dispatch_intent(led, ev, CFG, now=NOW)
    t_dispatch = time.perf_counter() - t0
    # the worker only claims once the runner publishes interactive=on
    # (as the runner does, after dispatch created the flags dir)
    notify_cards.publish_flags(CFG, str(data))
    for f in (data / "discord_render").glob("*.json"):
        starts[f.stem] = time.time()    # publish observation point
    cpu0 = time.process_time()

    async def run():
        # stage 2: claim + begin publish
        t = time.perf_counter()
        await worker.tick()
        t_claim = time.perf_counter() - t
        # stage 3: drain grants (the lock/queue segment)
        res = {}
        t = time.perf_counter()
        notify_cmds.drain_int_commands(
            led, res, CFG, str(data), limit=n * 2)
        t_grant = time.perf_counter() - t
        # stage 4: send + receipt publish
        t = time.perf_counter()
        await worker.tick()
        t_send = time.perf_counter() - t
        # stage 5: drain receipts (settle + card/coverage commit)
        t = time.perf_counter()
        notify_cmds.drain_int_commands(
            led, res, CFG, str(data), limit=n * 2)
        t_settle = time.perf_counter() - t
        return t_claim, t_grant, t_send, t_settle

    t_claim, t_grant, t_send, t_settle = asyncio.run(run())
    cpu = time.process_time() - cpu0
    end = time.time()
    # per-card e2e: spec publish -> settle drain finished. In batch mode
    # all specs share the finish line; report the batch envelope.
    e2e = [end - starts[k] for k in starts]
    return {"dispatch": t_dispatch, "claim_begin": t_claim,
            "grant_drain": t_grant, "send": t_send,
            "settle_drain": t_settle, "e2e": e2e,
            "e2e_cpu": [cpu] * len(starts)}


@pytest.mark.parametrize("n", [100, 1000])
def test_perf_delivery_pipeline(tmp_path, monkeypatch, n):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    led, data = _mk_ledger(tmp_path)
    for i in range(n):
        _seed_patient(led, 1 + i)
        _seed_msg(led, 10_000 + i, 1 + i)
    led.db.commit()
    # the worker only sends through a verified-SDK client
    bot = FakeBot(http=FakeHTTPClient())
    reg = registry.Registry(str(data / "discord_state"))
    worker = delivery.DeliveryWorker(
        bot=bot, settings=SETTINGS,
        root=str(data), reg=reg,
        worker_id=registry.new_worker_id(),
        log=lambda e, **f: None)
    out = _measure_pipeline((led, data, worker, reg), n)
    # timing alone proves nothing if nothing was sent: every card must
    # be delivered exactly once and bound to the message the bot posted
    cards = [dict(r) for r in led.db.execute(
        "SELECT card_id, delivery_state, message_id "
        "FROM notification_cards")]
    tally = Counter(c["delivery_state"] for c in cards)
    sent_ids = {str(m.id) for m in bot.channels[42].sent}
    print(f"\n  cards={len(cards)} states={dict(tally)} "
          f"sent={len(bot.channels[42].sent)}")
    assert tally == {"delivered": n}
    assert len(bot.channels[42].sent) == n == len(sent_ids)
    assert {str(c["message_id"]) for c in cards} == sent_ids
    print(f"\n== delivery pipeline n={n} ==")
    for k in ("dispatch", "claim_begin", "grant_drain", "send",
              "settle_drain"):
        print(f"  {k:<44} {out[k]:>8.4f}s")
    assert len(out["e2e"]) == n     # every spec was observed published
    r = _report("card delivery e2e (batch envelope, wall)",
                out["e2e"], 30)
    # CPU is diagnostic only: a latency budget must also see IO/wait
    _report("card delivery e2e (batch envelope, CPU)", out["e2e_cpu"])
    led.close()
    # RC26: non-contended p95 for spec publish -> receipt commit
    assert r["p95"] <= 30.0


def test_perf_operation_apply(tmp_path, monkeypatch):
    """RC26 — click command file -> applied receipt commit, p95<=6s."""
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    # tokens are minted at the fixed NOW (TTL 7d); pin the wall clock
    # as test_notify_cards does so apply never sees them expire
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    led, data = _mk_ledger(tmp_path)
    _seed_patient(led, 1)
    _seed_msg(led, 100, 1)
    _seed_msg(led, 101, 1, root=100)
    led.db.commit()
    eid = led.outbox_add("new_messages", 1, {"message_ids": [100, 101]})
    ev = dict(led.db.execute(
        "SELECT * FROM notify_outbox WHERE event_id=?",
        (eid,)).fetchone())
    notify_cards.dispatch_intent(led, ev, CFG, now=NOW)
    # the worker only claims once the runner publishes interactive=on,
    # and only sends through a verified-SDK client
    notify_cards.publish_flags(CFG, str(data))
    bot = FakeBot(http=FakeHTTPClient())
    reg = registry.Registry(str(data / "discord_state"))
    worker = delivery.DeliveryWorker(
        bot=bot, settings=SETTINGS,
        root=str(data), reg=reg,
        worker_id=registry.new_worker_id(),
        log=lambda e, **f: None)

    async def deliver():
        await worker.tick()
        res = {}
        notify_cmds.drain_int_commands(led, res, CFG, str(data))
        await worker.tick()
        notify_cmds.drain_int_commands(led, res, CFG, str(data))
        notify_cmds.drain_int_commands(led, res, CFG, str(data))
    asyncio.run(deliver())

    render = led.db.execute(
        "SELECT * FROM notification_renders LIMIT 1").fetchone()
    spec = json.loads(render["spec_json"])
    tokens = [b["token"] for row in spec["parts"]["action_rows"]
              for b in row if b["id"] == "ack"]
    card = dict(led.db.execute(
        "SELECT * FROM notification_cards WHERE card_id=1").fetchone())
    # clicks can only apply against a card bound to a real message
    assert card["delivery_state"] == "delivered" and card["message_id"]
    assert len(tokens) == 1
    origin = {**SCOPE, "message_id": card["message_id"]}

    samples = []
    outcomes = Counter()
    n = 200
    for i in range(n):
        tok = tokens[0]
        env = envelopes.notification(tok, f"discord:{1000 + i}",
                                     origin)
        t0 = time.perf_counter()
        envelopes.publish_command(str(data / "cmd_int"), env)
        res = {}
        notify_cmds.drain_int_commands(led, res, CFG, str(data))
        # receipt file exists => committed
        got = data / "cmd_results" / (
            "".join(c if c.isalnum() or c in "._-" else "_"
                    for c in env["request_id"]) + ".json")
        assert got.exists()
        samples.append(time.perf_counter() - t0)
        receipt = json.loads(got.read_text())
        outcomes[(receipt.get("outcome"), receipt.get("error"))] += 1
    print("\n== operation apply (click -> receipt commit) ==")
    print(f"  outcomes: {dict(outcomes)}")
    # every click must reach the apply path — a rejected receipt is
    # committed just as fast, so timing alone proves nothing
    assert outcomes == {("applied", None): n}
    r = _report("notification apply", samples, 6)
    led.close()
    assert r["p95"] <= 6.0


def test_perf_scale_stages(tmp_path, monkeypatch):
    """RC20 — queue/lock/sweep/gc/snapshot breakdown at 10,000 cards.
    Full-delivery is measured at 100/1,000 above; here the per-stage
    costs are measured on a seeded 10k-card ledger."""
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    led, data = _mk_ledger(tmp_path)
    n = 10_000
    with led.db:
        for i in range(n):
            _seed_patient(led, 1 + i)
            _seed_msg(led, 10_000 + i, 1 + i)
    # bulk-seed delivered cards+renders in settled steady state
    with led.db:
        for i in range(n):
            cur = led.db.execute(
                "INSERT INTO notification_cards("
                "card_key,kind,project_id,root_message_id,anchor_key,"
                "profile,application_id,guild_id,channel_id,"
                "message_id,source_generation,presentation_generation,"
                "desired_render_rev,applied_render_rev,ui_state,"
                "delivery_state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"k{i}", "thread", 1 + i, 10_000 + i, "{}",
                 "mcs", "1", "7", "42", f"m-{i}", 1, 1, 1, 1,
                 "{}", "delivered", NOW, NOW))
            led.db.execute(
                "INSERT INTO notification_renders("
                "delivery_id,card_id,op,render_rev,route_epoch,"
                "profile,application_id,guild_id,channel_id,"
                "payload_hash,correlation,state,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"d{i}", cur.lastrowid, "create", 1, 1,
                 "mcs", "1", "7", "42", "h" * 32, f"c{i}",
                 "delivered", NOW, NOW))
    # settled = fingerprints already observed. A NULL baseline makes
    # the first sweep re-issue every card (10k spec publishes), which
    # is not the scan this stage measures.
    with led.db:
        for (cid,) in led.db.execute(
                "SELECT card_id FROM notification_cards").fetchall():
            content = notify_cards._card_content(
                led.db, notify_cards._card_row(led.db, cid), cfg=CFG)
            led.db.execute(
                "UPDATE notification_cards SET source_fp=?,content_fp=?"
                " WHERE card_id=?",
                (content["source_fp"], notify_cards._content_fp(content),
                 cid))

    res = {}
    t = time.perf_counter()
    sweep_out = notify_cards.sweep(led, CFG, limit=n)
    t_sweep = time.perf_counter() - t
    t = time.perf_counter()
    gc_out = notify_cards.gc(led, CFG, now=NOW)
    t_gc = time.perf_counter() - t
    t = time.perf_counter()
    notify_cards.recover(led, CFG, res)
    t_recover = time.perf_counter() - t
    snap = tmp_path / "snap"
    snap.mkdir()
    t = time.perf_counter()
    _ledger.publish_snapshot(str(data / "ledger.db"), str(snap))
    t_snap = time.perf_counter() - t
    # a drain batch over a large cmd_int backlog
    int_dir = data / "cmd_int"
    int_dir.mkdir(exist_ok=True)
    for i in range(256):
        env = envelopes.refresh(
            "discord:1001",
            {**SCOPE, "message_id": f"m-{i}"})
        envelopes.publish_command(str(int_dir), env)
    t = time.perf_counter()
    notify_cmds.drain_int_commands(led, res, CFG, str(data), limit=256)
    t_drain = time.perf_counter() - t

    print(f"\n== stage breakdown n={n} ==")
    print(f"  sweep(scan={sweep_out['scanned']})"
          f"{'':<28} {t_sweep:>8.4f}s")
    print(f"  gc(tokens={gc_out['tokens']})"
          f"{'':<28} {t_gc:>8.4f}s")
    print(f"  recover{'':<39} {t_recover:>8.4f}s")
    print(f"  snapshot_publish{'':<30} {t_snap:>8.4f}s")
    print(f"  drain_batch(256){'':<29} {t_drain:>8.4f}s")
    led.close()
    # a fast sweep proves nothing unless it scanned every card and
    # found them settled
    assert sweep_out == {"scanned": n, "updated": 0, "republished": 0}
    assert t_sweep < 30.0 and t_snap < 30.0
