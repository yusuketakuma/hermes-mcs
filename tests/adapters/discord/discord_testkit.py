"""Shared synthetic fixtures for the Discord card-worker test family —
the runner config/settings, a fake ``discord`` SDK module and the fake
Bot/Channel/Message/Thread objects.  Not a test module (no ``test_``
prefix); sibling files import it via the tests/ sys.path bootstrap."""
from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace

import ledger as _ledger
import notify_cards
import notify_spool
import notify_cmds
import pytest

from adapters.common import registry
from adapters.discord import actions as actions_mod, delivery
from discord_delivery_testkit import FakeHTTP, FakeHTTPClient  # noqa: F401

NOW = 1_790_000_000.0
MISSING = object()  # discord.py's "argument not passed" sentinel
CFG = {"notify": {"interactive": "discord", "route_epoch": 1,
                  "operator": "op-user", "card_thread": True,
                  "discord": {"profile": "mcs", "application_id": "1",
                              "guild_id": "7", "channel_id": "42"}},
       "signals": {"notify": True}}
SETTINGS = {"profile": "mcs", "application_id": "1", "channel_id": "42",
            "guild_id": "7",
            "allowed_user_ids": {"1001"}, "allowed_chat_ids": {"42"},
            "project_ids": {1}}


# ---------- fake discord SDK --------------------------------------------

def _fake_discord():
    mod = types.ModuleType("discord")

    class LayoutView:
        def __init__(self, timeout=None):
            self.timeout = timeout
            self.items = []

        def add_item(self, item):
            self.items.append(item)

        def stop(self):
            self.stopped = True

    class View(LayoutView):
        pass

    class Embed:
        def __init__(self, description=None, colour=None):
            self.description, self.colour = description, colour

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
                     url=None, row=None):
            self.style = style
            self.label = label
            self.custom_id = custom_id
            self.url = url
            self.row = row

    class SelectOption:
        def __init__(self, label=None, value=None, default=False):
            self.label, self.value, self.default = label, value, default

    class Select:
        def __init__(self, custom_id=None, options=(), required=True,
                     min_values=1, max_values=1, **_):
            self.custom_id = custom_id
            self.options = list(options)
            self.required = required
            self.min_values, self.max_values = min_values, max_values
            self.placeholder, self.row = _.get("placeholder"), _.get("row")

    class Separator:
        def __init__(self, visible=True, spacing=None):
            self.visible, self.spacing = visible, spacing

    class Label:
        def __init__(self, text=None, component=None, **_):
            self.text, self.component = text, component

    class AllowedMentions:
        @classmethod
        def none(cls):
            m = cls()
            m.everyone = m.users = m.roles = m.replied_user = False
            return m

    class Modal:
        def __init__(self, title=None, custom_id=None, timeout=None):
            self.title = title
            self.custom_id = custom_id
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class TextInput:
        def __init__(self, label=None, style=None, custom_id=None,
                     max_length=None, required=True, default=None, **_):
            self.label = label
            self.custom_id = custom_id
            self.required = required
            self.default = default
            self.value = None

    class Webhook:
        sent = []

        def __init__(self, ident, token, client):
            self.ident, self.token, self.client = ident, token, client
            # partial() hardcodes incoming — production sweep must
            # flip it to application before ephemeral sends are legal
            self.type = 1

        @classmethod
        def partial(cls, ident, token, client=None):
            return cls(ident, token, client)

        async def send(self, content, ephemeral=False, view=MISSING,
                       allowed_mentions=None):
            # discord.py validation: ephemeral requires an application
            # webhook; an explicitly-passed view=None is a TypeError
            if ephemeral and self.type != 3:
                raise ValueError("ephemeral messages can only be sent "
                                 "from application webhooks")
            if view is not MISSING and view is None:
                raise TypeError("expected view parameter to be of type "
                                "View, not NoneType")
            Webhook.sent.append(
                {"content": content, "ephemeral": ephemeral,
                 "view": view, "allowed_mentions": allowed_mentions})

    class Section:
        def __init__(self, *children, accessory=None):
            self.children, self.accessory = list(children), accessory

    mod.ui = SimpleNamespace(LayoutView=LayoutView, View=View, Section=Section,
                             TextDisplay=TextDisplay, ActionRow=ActionRow,
                             Container=Container,
                             Button=Button, Modal=Modal,
                             TextInput=TextInput, Select=Select,
                             Separator=Separator,
                             Label=Label)
    mod.SelectOption = SelectOption
    mod.Embed = Embed
    mod.SeparatorSpacing = SimpleNamespace(small=1, large=2)
    mod.AllowedMentions = AllowedMentions
    mod.ButtonStyle = SimpleNamespace(primary=1, secondary=2, success=3,
                                      danger=4, link=5)
    mod.TextStyle = SimpleNamespace(short=1, paragraph=2)
    mod.WebhookType = SimpleNamespace(incoming=1, channel_follower=2,
                                      application=3)
    mod.Webhook = Webhook
    return mod


# ---------- fake discord objects ----------------------------------------

BOT_USER = SimpleNamespace(id=4242)


class _HistMsg:
    _next = 0

    def __init__(self, content, thread):
        self.content = content
        self.author = BOT_USER
        self.thread = thread
        self.index = len(thread.sent)
        self.edits = 0
        type(self)._next += 1
        self.id = type(self)._next

    async def edit(self, content=MISSING, allowed_mentions=None, view=MISSING):
        if content is MISSING:
            content = self.content
        self.view = view if view is not MISSING else getattr(self, "view", MISSING)
        self.content = content
        self.allowed_mentions = allowed_mentions
        self.thread.sent[self.index] = content
        self.edits += 1
        return self


class FakeThread:
    def __init__(self, tid):
        self.id = tid
        self.sent = []
        self.messages = []

    async def send(self, content, allowed_mentions=None, view=MISSING):
        message = _HistMsg(content, self)
        message.view = view
        self.sent.append(content)
        self.messages.append(message)
        self.allowed_mentions = allowed_mentions
        return message

    async def history(self, limit=None):
        items = self.messages if limit is None else self.messages[-limit:]
        for message in reversed(items):
            yield message

    async def fetch_message(self, mid):
        for message in self.messages:
            if message.id == mid:
                return message
        raise FakeHTTP(404)


class FakeMessage:
    def __init__(self, mid, channel=None):
        self.id = mid
        self.channel = channel
        self.view = None
        self.edits = 0
        self.deleted = False
        self.threads = []

    async def edit(self, view=None, allowed_mentions=None, content=None, embed=None):
        if self.deleted:
            raise FakeHTTP(404)
        self.view = view
        self.content, self.embed = content, embed
        self.allowed_mentions = allowed_mentions
        self.edits += 1

    async def delete(self):
        if self.deleted:
            raise FakeHTTP(404)
        self.deleted = True

    async def create_thread(self, name=None):
        t = FakeThread(7700 + len(self.threads))
        self.threads.append((name, t))
        bot = getattr(getattr(self, "channel", None), "bot", None)
        if bot is not None:
            bot.channels[t.id] = t
        return t


class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []
        self.messages = {}
        self._next = 9000

    async def send(self, view=None, allowed_mentions=None, content=None, embed=None):
        self._next += 1
        m = FakeMessage(self._next, channel=self)
        m.view = view
        m.content, m.embed = content, embed
        m.allowed_mentions = allowed_mentions
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
        self.channels[channel_id].bot = self
        self.user = BOT_USER
        self.listeners = []
        self.http = FakeHTTPClient()     # verified-SDK shape for create POSTs

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


# ---------- fake interactions ---------------------------------------------

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


# ---------- world fixture --------------------------------------------------

@pytest.fixture(name="world")
def discord_world(tmp_path, monkeypatch):
    """Fixture ``world``: import ``discord_world`` to register it."""
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    # the clock pin lives here so every importer of ``world`` gets it
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    data = tmp_path / "data"
    data.mkdir()
    led = _ledger.Ledger(str(data / "ledger.db"))
    notify_spool.ensure_dirs(str(data))
    notify_spool.publish_flags(CFG, str(data))
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


async def _deliver(world, worker):
    """claim -> begin -> grant -> send -> receipt -> settled."""
    await worker.tick()
    world.drain()                       # grant
    await worker.tick()                 # send + receipt
    world.drain()                       # settle
    return worker._bot.channels[42].sent


def _request(world, src_mid=100, title="経過確認", status="open", rev=1):
    rid = world.led.db.execute(
        "INSERT INTO requests(project_id,source_message_id,source_hash,"
        "title,status,revision,created_at,updated_at) "
        "VALUES(1,?,?,?,?,?,?,?)",
        (src_mid, "h" * 64, title, status, rev, NOW, NOW)).lastrowid
    world.led.db.commit()
    return rid
