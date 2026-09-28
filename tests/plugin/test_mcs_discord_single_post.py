"""Discord create POSTs are single-shot — the SDK's in-call retry loop
(discord.py 2.7.1 HTTPClient.request: 5xx and ECONNRESET re-POST) must
never put a second copy on the wire, while Hermes' own unguarded sends
keep their retries. Pure stubs: the aiohttp session is a recorder, no
network, no real SDK."""
from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from hermes_plugin.mcs_delivery import worker as worker_mod
from hermes_plugin.mcs_delivery.registry import Registry
from hermes_plugin.mcs_delivery import paths
from hermes_plugin.mcs_discord import cards
from hermes_plugin.mcs_discord.delivery import DeliveryWorker
from test_mcs_discord_delivery import (BOT_USER, SETTINGS, FakeHTTP, _chunks,
                                       _claim, _receipts, _sent_parts, _spec,
                                       _state)

_discord = types.ModuleType("discord")          # send_attachment's File
_discord.File = lambda path, filename=None: (path, filename)

SUPPRESSED = "discordretrysuppressed"           # worker.err_code of the guard


@pytest.fixture(autouse=True)
def synthetic_discord_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _discord)
    monkeypatch.setattr(cards, "build_view", lambda spec: "view")


def _ua(version):
    return (f"DiscordBot (https://github.com/Rapptz/discord.py {version})"
            " Python/3.11 aiohttp/3.14.3")


class _Resp:
    def __init__(self, wire, fault):
        self._wire, self._fault = wire, fault

    async def __aenter__(self):
        self._wire.committed += 1           # the server acted on it
        if isinstance(self._fault, OSError):
            raise self._fault               # ...but the answer was lost
        self._wire.next_id += 1
        self.status = self._fault or 200
        self.data = {"id": self._wire.next_id}
        return self

    async def __aexit__(self, *exc):
        return False


class Wire:
    """aiohttp.ClientSession stand-in — every call reaching it is a
    wire call; `faults` scripts the answer of each successive POST."""

    def __init__(self, faults=()):
        self.calls = []
        self.faults = list(faults)
        self.committed = 0
        self.next_id = 5000

    def request(self, method, url, **_kw):
        self.calls.append((method, url))
        fault = self.faults.pop(0) if method == "POST" and self.faults \
            else None
        return _Resp(self, fault)

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


class HTTPClient:
    """discord.py 2.7.1 HTTPClient.request retry loop, reduced to the
    branches that re-POST (http.py:646-786)."""

    def __init__(self, session, version="2.7.1"):
        self.user_agent = _ua(version)
        self.__session = session            # -> _HTTPClient__session

    async def request(self, method, url):
        for tries in range(5):
            try:
                async with self.__session.request(method, url) as resp:
                    if resp.status in {500, 502, 504, 524}:
                        continue
                    return resp.data
            except OSError as e:
                if tries < 4 and e.errno in (54, 10054):
                    continue
                raise
        raise FakeHTTP(500)


class WireThread:
    def __init__(self, bot, tid):
        self.id, self._bot, self.sent = tid, bot, []

    async def send(self, content=None, **kw):
        data = await self._bot.http.request(
            "POST", f"/channels/{self.id}/messages")
        self.sent.append(content if content is not None else kw)
        return types.SimpleNamespace(id=data["id"])

    async def history(self, limit=None):
        for _ in ():
            yield _


class WireMessage:
    def __init__(self, bot, mid):
        self.id, self._bot, self.thread = mid, bot, None

    async def create_thread(self, *, name):
        data = await self._bot.http.request(
            "POST", f"/channels/42/messages/{self.id}/threads")
        thread = WireThread(self._bot, data["id"])
        self._bot.threads[thread.id] = thread
        return thread


class WireChannel:
    def __init__(self, bot):
        self.id, self._bot = 42, bot

    async def send(self, **_kw):
        data = await self._bot.http.request("POST", "/channels/42/messages")
        return WireMessage(self._bot, data["id"])

    async def fetch_message(self, mid):
        return WireMessage(self._bot, mid)


class WireBot:
    def __init__(self, faults=(), version="2.7.1"):
        self.wire = Wire(faults)
        self.http = HTTPClient(self.wire, version)
        self.user = BOT_USER
        self.channel = WireChannel(self)
        self.threads = {}

    def get_channel(self, cid):
        return self.channel if cid == 42 else self.threads.get(cid)

    async def fetch_channel(self, cid):
        raise FakeHTTP(404)


def _mk(tmp_path, bot):
    for name in ("discord_render", "cmd_int", "cmd_results", "flags"):
        (tmp_path / name).mkdir(exist_ok=True)
    (tmp_path / "flags" / "notify.json").write_text(
        json.dumps({"interactive": True}))
    paths.ensure_dirs(str(tmp_path))
    logs = []
    w = DeliveryWorker(bot=bot, settings=SETTINGS, root=str(tmp_path),
                       reg=Registry(str(_state(tmp_path))), worker_id="w1",
                       log=lambda ev, **kw: logs.append((ev, kw)))
    return w, logs


def _attachment_spec(tmp_path):
    blob = b"synthetic-attachment"
    f = tmp_path / "att.bin"
    f.write_bytes(blob)
    import hashlib
    spec = _spec(_chunks(1))
    spec["parts"]["manifest"].append(
        {"part_id": "attach:0007", "kind": "attachment_part",
         "index": len(spec["parts"]["manifest"]), "attachment_id": 7,
         "name": "att.bin", "path": str(f),
         "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)})
    return spec


def _legacy_spec():
    spec = _spec(["body"], manifest=[])
    spec["parts"]["thread_body"] = "legacy body"
    return spec


# fault that makes discord.py re-POST after the first POST committed
RETRY_FAULTS = [500, ConnectionResetError(54, "Connection reset by peer")]


@pytest.mark.parametrize("fault", RETRY_FAULTS, ids=["http500", "econnreset"])
def test_card_create_sdk_retry_never_reaches_wire(tmp_path, fault):
    bot = WireBot(faults=[fault])
    w, _ = _mk(tmp_path, bot)
    out = asyncio.run(worker_mod._outcome_of(w._perform(_claim(_spec([])))))
    # one committed POST, the SDK's re-POST stopped before the wire —
    # an honest unknown, never a duplicate card reported as delivered
    assert len(bot.wire.posts()) == 1 and bot.wire.committed == 1
    assert out == {"result": "unknown", "error_code": SUPPRESSED}


@pytest.mark.parametrize("part_id,faults,posts", [
    ("thread", [500], 1),                       # dependents held
    ("body:0001", [None, 500], 3),              # attachment still follows
    ("attach:0007", [None, None, 500], 3),
], ids=["thread_create", "body_send", "attachment_upload"])
def test_durable_part_sdk_retry_is_single_post(tmp_path, part_id, faults,
                                               posts):
    bot = WireBot(faults=faults)
    w, _ = _mk(tmp_path, bot)
    asyncio.run(w._deliver_parts(_claim(_attachment_spec(tmp_path)), "9001"))
    assert len(bot.wire.posts()) == posts
    row = _sent_parts(_state(tmp_path))[part_id]
    assert (row["result"], row["error_code"]) == ("unknown", SUPPRESSED)


def test_legacy_thread_create_sdk_retry_is_single_post(tmp_path):
    bot = WireBot(faults=[500])
    w, _ = _mk(tmp_path, bot)
    asyncio.run(w._deliver_parts(_claim(_legacy_spec()), "9001"))
    assert len(bot.wire.posts()) == 1
    tre = [e for e in _receipts(tmp_path / "cmd_int")
           if e["op"] == "thread_receipt"]
    assert tre and tre[0]["error_code"] == SUPPRESSED


def test_legacy_body_send_sdk_retry_is_single_post(tmp_path):
    bot = WireBot(faults=[None, 500])
    w, logs = _mk(tmp_path, bot)
    asyncio.run(w._deliver_parts(_claim(_legacy_spec()), "9001"))
    assert len(bot.wire.posts()) == 2          # thread + one body POST
    assert ("thread_body_failed",
            {"error": "DiscordRetrySuppressed", "chunks_sent": 0}) in logs


def _no_session(bot):
    bot.http._HTTPClient__session = None       # discord MISSING / renamed


def _no_http(bot):
    del bot.http


@pytest.mark.parametrize("break_sdk", [
    lambda bot: setattr(bot.http, "user_agent", _ua("2.7.2")),
    _no_session, _no_http,
], ids=["version_mismatch", "missing_private_session", "missing_http"])
def test_unverified_retry_policy_fails_closed(tmp_path, break_sdk):
    bot = WireBot()
    wire = bot.wire
    break_sdk(bot)
    w, _ = _mk(tmp_path, bot)
    out = asyncio.run(worker_mod._outcome_of(w._perform(_claim(_spec([])))))
    asyncio.run(w._deliver_parts(_claim(_attachment_spec(tmp_path)), "9001"))
    assert out == {"result": "not_sent", "error_code": "retry_policy_unknown"}
    rows = _sent_parts(_state(tmp_path))
    assert rows["thread"]["result"] == "not_sent"
    assert rows["thread"]["error_code"] == "retry_policy_unknown"
    assert wire.calls == []                     # nothing reached the wire


def test_unguarded_hermes_send_keeps_retry_during_guarded_post():
    bot = WireBot(faults=[None, 500])
    posted, release = asyncio.Event(), asyncio.Event()

    async def guarded():
        await bot.http.request("POST", "/channels/42/messages")
        posted.set()
        await release.wait()                    # guard window stays open
        return "card"

    async def hermes():
        await posted.wait()
        try:                                    # Hermes' own send: 500 ->
            return await bot.http.request(      # SDK retry must still work
                "POST", "/channels/1/messages")
        finally:
            release.set()

    async def run():
        return await asyncio.gather(cards.single_post(bot, guarded), hermes())

    card, reply = asyncio.run(run())
    assert card == "card" and reply == {"id": 5003}
    assert bot.wire.posts() == [("POST", "/channels/42/messages")] + \
        [("POST", "/channels/1/messages")] * 2


def test_guard_state_resets_after_exceptions():
    bot = WireBot(faults=[500])

    async def boom():
        await bot.http.request("POST", "/x")

    async def run():
        with pytest.raises(cards.DiscordRetrySuppressed):
            await cards.single_post(bot, boom)
        assert cards._POSTS.get() is None
        with pytest.raises(ValueError):
            await cards.single_post(bot, _raise_value)
        assert cards._POSTS.get() is None
        bot.wire.faults = [500]                 # unguarded again: retries
        await bot.http.request("POST", "/y")
        # each guarded call gets its own single POST budget
        assert await cards.single_post(
            bot, lambda: bot.http.request("POST", "/z")) == {"id": 5004}

    asyncio.run(run())
    assert [u for _, u in bot.wire.posts()] == ["/x", "/y", "/y", "/z"]


async def _raise_value():
    raise ValueError("interrupted mid-send")


def test_replaced_session_gets_guard_reinstalled_once():
    bot = WireBot()
    assert cards.single_post_ready(bot) and cards.single_post_ready(bot)
    first = bot.http._HTTPClient__session.request
    assert first.__wrapped__ == Wire.request.__get__(bot.wire)  # not stacked
    fresh = Wire(faults=[500])                  # discord recreated it
    bot.http._HTTPClient__session = fresh

    async def post():
        await bot.http.request("POST", "/x")

    with pytest.raises(cards.DiscordRetrySuppressed):
        asyncio.run(cards.single_post(bot, post))
    assert len(fresh.posts()) == 1
