"""Shared synthetic fixtures for the Discord part-delivery test family —
the verified-SDK HTTP client shape, a thread card spec with its part
manifest, and journal/receipt readers.  Not a test module (no ``test_``
prefix); sibling files import it via the tests/ sys.path bootstrap."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import types

import pytest

from adapters.common import envelopes, journal, paths
from adapters.common.registry import Registry
from adapters.discord.delivery import DeliveryWorker

class FakeHTTP(Exception):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.status = status


BOT_USER = types.SimpleNamespace(id=4242)


class _NoWireSession:
    def request(self, *_a, **_kw):
        raise AssertionError("fake channels never reach the session")


class FakeHTTPClient:
    """Verified-SDK shape the single-post guard checks before a create
    POST; the fake channels in this module bypass the session entirely."""

    user_agent = ("DiscordBot (https://github.com/Rapptz/discord.py 2.7.1)"
                  " Python/3.11 aiohttp/3.14.3")

    def __init__(self):
        self._HTTPClient__session = _NoWireSession()


SETTINGS = {"profile": "mcs", "application_id": "1", "channel_id": "42",
            "guild_id": "7"}

DELIVERY_ID = "00000000-0000-4000-8000-00000000d00d"


def _chunks(n, size=1900):
    return [f"chunk-{i} " + "x" * (size - 8) for i in range(n)]


def _manifest(chunks, attachments=None, thread=True, prior=None):
    """``prior`` maps a body part_id to the remote id of the post that
    already carries that chunk (an update plan's ``prior_remote_id``)."""
    parts = [{"part_id": "card", "kind": "card", "index": 0}]
    idx = 1
    if thread:
        parts.append({"part_id": "thread", "kind": "thread", "index": idx,
                      "name": "テスト スレッド",
                      "sha256": hashlib.sha256(
                          "テスト スレッド".encode()).hexdigest()})
        idx += 1
    for i, c in enumerate(chunks):
        part = {"part_id": f"body:{i + 1:04d}", "kind": "body_part",
                "index": idx, "sha256": hashlib.sha256(
                    c.encode()).hexdigest(),
                "bytes": len(c.encode())}
        if part["part_id"] in (prior or {}):
            part["prior_remote_id"] = prior[part["part_id"]]
        parts.append(part)
        idx += 1
    for a in attachments or []:
        parts.append(a)
        idx += 1
    return parts


def _spec(chunks, manifest=None, op="create", thread_id=None, prior=None):
    manifest = _manifest(chunks, prior=prior) if manifest is None \
        else manifest
    delivery = {"route_epoch": 1, "correlation": "cd" * 16,
                "profile": "mcs", "application_id": "1",
                "channel_id": "42", "guild_id": "7",
                "intent_event_ids": [1]}
    if thread_id:
        delivery["thread_id"] = thread_id
    return {"schema": "mcs-card-render/v1",
            "delivery_id": DELIVERY_ID,
            "logical_intent_id": "v1|thread|1|100",
            "card_key": "v1|thread|1|100", "kind": "thread", "op": op,
            "render_rev": 1, "source_generation": 1,
            "presentation_generation": 1, "ui_revision": 1,
            "delivery": delivery,
            "parts": {"containers": [{"type": "text", "text": "card"}],
                      "footer": [], "action_rows": [],
                      "thread_name": "テスト スレッド",
                      "thread_body_parts": chunks,
                      "manifest": manifest}}


def _publish_on_drive(w):
    """Part tests drive a spec directly; the worker sends parts only while
    its spec file is still published (revocation withdraws it), so stand
    in for the runner's publication unless a test already did it."""
    async def published(claim, *a, **kw):
        path = os.path.join(w._dirs["render"],
                            claim["spec"]["delivery_id"] + ".json")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(claim["spec"], fh)
        # resolved per call so a class-level monkeypatch still applies
        return await type(w)._drive_parts(w, claim, *a, **kw)
    w._drive_parts = published
    return w


def _state(tmp_path):
    return tmp_path / "discord_state"


def _claim(spec, message_id="9001"):
    return {"attempt_id": "ab" * 8, "worker_id": "w1", "spec": spec,
            "payload_hash": envelopes.payload_hash(spec),
            "spec_path": "/tmp/x.json", "phase": "settled",
            "message_id": message_id}


def _sent_parts(state_dir, delivery_id=DELIVERY_ID):
    """part_id -> journal 'result' rows across all worker journals."""
    rec = journal.scan(str(state_dir))
    out = {}
    for rows in rec.values():
        for r in rows:
            if r.get("phase") == "result" \
                    and r.get("delivery_id") == delivery_id \
                    and r.get("part_id"):
                out[r["part_id"]] = r
    return out


def _receipts(cmd_int):
    from pathlib import Path
    return [json.loads(p.read_text())
            for p in sorted(Path(cmd_int).glob("*.json"))]


# ---------- part-engine fakes --------------------------------------------

# cards.send_attachment lazy-imports the SDK inside the function —
# a File shim is enough for the synthetic thread to record uploads
_discord = types.ModuleType("discord")


class _FakeFile:
    def __init__(self, path, filename=None):
        self.path, self.filename = path, filename


_discord.File = _FakeFile
_discord.AllowedMentions = types.SimpleNamespace(
    none=lambda: "no-pings")        # cards.no_pings()


@pytest.fixture(autouse=True)
def synthetic_discord_sdk(monkeypatch):
    # Collection must see the real package (or its absence), so an SDK
    # serialization test never mistakes this shim for discord.py.
    monkeypatch.setitem(sys.modules, "discord", _discord)


FOREIGN_USER = types.SimpleNamespace(id=9999)


class _HistMsg:
    def __init__(self, mid, content, author=BOT_USER, attachments=()):
        self.id = mid
        self.content = content
        self.author = author        # posts through the fake are ours
        self.attachments = list(attachments)
        self.edits = 0
        self.edit_fail = None       # exception to raise on edit

    async def edit(self, **_kw):
        if self.edit_fail is not None:
            raise self.edit_fail
        self.content = _kw.get("content", self.content)
        self.view = _kw.get("view", getattr(self, "view", None))
        self.edits += 1


class FakeThread:
    def __init__(self, tid):
        self.id = tid
        self.sent = []          # [(mid, content)]
        self._next = 6000
        self.fail_at = None     # send index to raise on (None = never)
        self.fail_exc = FakeHTTP(500)
        self.fetch_fail = None  # exception to raise on fetch_message

    async def send(self, content=None, **kw):
        n = len(self.sent)
        if self.fail_at is not None and n == self.fail_at:
            raise self.fail_exc
        self._next += 1
        files = [kw["file"]] if kw.get("file") is not None else []
        self.sent.append(_HistMsg(self._next, content, attachments=files))
        self.sent[-1].view = kw.get("view")
        return self.sent[-1]

    async def history(self, limit=None):
        items = self.sent if limit is None else self.sent[-limit:]
        for m in reversed(items):
            yield m

    async def fetch_message(self, mid):
        if self.fetch_fail is not None:
            raise self.fetch_fail
        for m in self.sent:
            if m.id == mid:
                return m
        raise FakeHTTP(404)


class FakeMessage:
    def __init__(self, mid, channel):
        self.id = mid
        self._channel = channel
        self.deleted = False
        self.edits = []

    async def edit(self, **_kw):
        self.edits.append(_kw)

    async def delete(self):
        self.deleted = True

    async def create_thread(self, *, name):
        t = FakeThread(7700 + len(self._channel.threads))
        t.name = name
        if self._channel.thread_fail is not None:
            raise self._channel.thread_fail
        self._channel.threads.append(t)
        return t


class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []
        self.threads = []
        self.thread_fail = None
        self._next = 9000

    async def send(self, **_kw):
        self._next += 1
        m = FakeMessage(self._next, channel=self)
        self.sent.append(m)
        return m

    async def fetch_message(self, mid):
        for m in self.sent:
            if m.id == mid:
                return m
        raise FakeHTTP(404)


class FakeBot:
    def __init__(self, channel_id=42):
        self.channels = {channel_id: FakeChannel(channel_id)}
        self.user = BOT_USER
        self.http = FakeHTTPClient()

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        ch = self.channels.get(cid)
        if ch is None:
            # threads resolve as channels on the real client
            for c in self.channels.values():
                for t in c.threads:
                    if t.id == cid:
                        return t
            raise FakeHTTP(404)
        return ch


def _mkworker(tmp_path, bot=None, wid="w1"):
    for name in ("discord_render", "cmd_int", "cmd_results", "flags"):
        (tmp_path / name).mkdir(exist_ok=True)
    flags = tmp_path / "flags" / "notify.json"
    if not flags.exists():
        flags.write_text(json.dumps({"interactive": True, "route_epoch": 1}))
    paths.ensure_dirs(str(tmp_path))          # creates discord_state
    reg = Registry(str(_state(tmp_path)))
    bot = bot or FakeBot()
    # the card message every claim points at — thread creation is only
    # valid under a message the remote actually holds
    ch = bot.channels[42]
    if not any(m.id == 9001 for m in ch.sent):
        ch.sent.append(FakeMessage(9001, ch))
    ch._next = max(ch._next, 9001)
    w = DeliveryWorker(bot=bot, settings=SETTINGS, root=str(tmp_path),
                       reg=reg, worker_id=wid,
                       log=lambda *_a, **_k: None)
    return _publish_on_drive(w), reg, bot


def _envelope(channel_id="42"):
    return {"op": "transport_begin", "profile": "mcs",
            "application_id": "1", "channel_id": channel_id,
            "guild_id": "7"}


def _settled(state, wid, aid, delivery_id, ts, *, result="delivered",
             channel_id="42"):
    for phase, extra in (("begin", {"begin_envelope": _envelope(channel_id),
                                    "receipt_envelope": _envelope(channel_id)}),
                         ("granted", {}), ("started", {}),
                         ("result", {"result": result}),
                         ("receipt", {"result": result})):
        journal.append(str(state), wid, {"phase": phase, "attempt_id": aid,
                                         "delivery_id": delivery_id,
                                         "ts": ts, **extra})
