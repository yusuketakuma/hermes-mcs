"""Shared synthetic fixtures for the Discord card-worker test family —
the runner config/settings, a fake ``discord`` SDK module and the fake
Bot/Channel/Message/Thread objects.  Not a test module (no ``test_``
prefix); sibling files import it via the tests/ sys.path bootstrap."""
from __future__ import annotations

import types
from types import SimpleNamespace

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

    class Container:
        def __init__(self, *children, accent_color=None, **_):
            self.children = list(children)
            self.accent_color = accent_color

    class Button:
        def __init__(self, style=None, label=None, custom_id=None,
                     url=None):
            self.style = style
            self.label = label
            self.custom_id = custom_id
            self.url = url

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

    mod.ui = SimpleNamespace(LayoutView=LayoutView, View=View,
                             TextDisplay=TextDisplay, ActionRow=ActionRow,
                             Container=Container,
                             Button=Button, Modal=Modal,
                             TextInput=TextInput, Select=Select,
                             Label=Label)
    mod.SelectOption = SelectOption
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

    async def edit(self, content=None, allowed_mentions=None):
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

    async def send(self, content, allowed_mentions=None):
        message = _HistMsg(content, self)
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

    async def edit(self, view=None, allowed_mentions=None):
        if self.deleted:
            raise FakeHTTP(404)
        self.view = view
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

    async def send(self, view=None, allowed_mentions=None):
        self._next += 1
        m = FakeMessage(self._next, channel=self)
        m.view = view
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
