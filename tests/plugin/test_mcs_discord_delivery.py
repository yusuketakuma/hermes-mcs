"""Synthetic Discord part-delivery engine — journal-bound thread/body/
attachment parts with restart-resume. No network, no MCS data."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types

import pytest

from hermes_plugin.mcs_delivery import envelopes, journal, paths
from hermes_plugin.mcs_delivery.registry import Registry, scope_key
from hermes_plugin.mcs_discord.delivery import DeliveryWorker

# cards.send_attachment lazy-imports the SDK inside the function —
# a File shim is enough for the synthetic thread to record uploads
_discord = types.ModuleType("discord")


class _FakeFile:
    def __init__(self, path, filename=None):
        self.path, self.filename = path, filename


_discord.File = _FakeFile


@pytest.fixture(autouse=True)
def synthetic_discord_sdk(monkeypatch):
    # Collection must see the real package (or its absence), so an SDK
    # serialization test never mistakes this shim for discord.py.
    monkeypatch.setitem(sys.modules, "discord", _discord)


class FakeHTTP(Exception):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.status = status


BOT_USER = types.SimpleNamespace(id=4242)
FOREIGN_USER = types.SimpleNamespace(id=9999)


class _HistMsg:
    def __init__(self, mid, content, author=BOT_USER):
        self.id = mid
        self.content = content
        self.author = author        # posts through the fake are ours


class FakeThread:
    def __init__(self, tid):
        self.id = tid
        self.sent = []          # [(mid, content)]
        self._next = 6000
        self.fail_at = None     # send index to raise on (None = never)
        self.fail_exc = FakeHTTP(500)

    async def send(self, content=None, **_kw):
        n = len(self.sent)
        if self.fail_at is not None and n == self.fail_at:
            raise self.fail_exc
        self._next += 1
        self.sent.append(_HistMsg(self._next, content))
        return self.sent[-1]

    async def history(self, limit=None):
        items = self.sent if limit is None else self.sent[-limit:]
        for m in reversed(items):
            yield m


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


class _NoWireSession:
    def request(self, *_a, **_kw):
        raise AssertionError("fake channels never reach the session")


class FakeHTTPClient:
    """Verified-SDK shape the single-post guard checks before a create
    POST; the fake channels above bypass the session entirely."""

    user_agent = ("DiscordBot (https://github.com/Rapptz/discord.py 2.7.1)"
                  " Python/3.11 aiohttp/3.14.3")

    def __init__(self):
        self._HTTPClient__session = _NoWireSession()


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


SETTINGS = {"profile": "mcs", "application_id": "1", "channel_id": "42",
            "guild_id": "7"}

DELIVERY_ID = "00000000-0000-4000-8000-00000000d00d"


def _chunks(n, size=1900):
    return [f"chunk-{i} " + "x" * (size - 8) for i in range(n)]


def _manifest(chunks, attachments=None, thread=True):
    parts = [{"part_id": "card", "kind": "card", "index": 0}]
    idx = 1
    if thread:
        parts.append({"part_id": "thread", "kind": "thread", "index": idx,
                      "name": "テスト スレッド",
                      "sha256": hashlib.sha256(
                          "テスト スレッド".encode()).hexdigest()})
        idx += 1
    for i, c in enumerate(chunks):
        parts.append({"part_id": f"body:{i + 1:04d}", "kind": "body_part",
                      "index": idx, "sha256": hashlib.sha256(
                          c.encode()).hexdigest(),
                      "bytes": len(c.encode())})
        idx += 1
    for a in attachments or []:
        parts.append(a)
        idx += 1
    return parts


def _spec(chunks, manifest=None, op="create", thread_id=None):
    manifest = _manifest(chunks) if manifest is None else manifest
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


def _state(tmp_path):
    return tmp_path / "discord_state"


def _mkworker(tmp_path, bot=None, wid="w1"):
    for name in ("discord_render", "cmd_int", "cmd_results", "flags"):
        (tmp_path / name).mkdir(exist_ok=True)
    flags = tmp_path / "flags" / "notify.json"
    if not flags.exists():
        flags.write_text(json.dumps({"interactive": True}))
    paths.ensure_dirs(str(tmp_path))          # creates discord_state
    reg = Registry(str(_state(tmp_path)))
    bot = bot or FakeBot()
    # the card message every claim points at — thread creation is only
    # valid under a message the remote actually holds
    ch = bot.channels[42]
    if not any(m.id == 9001 for m in ch.sent):
        ch.sent.append(FakeMessage(9001, ch))
    w = DeliveryWorker(bot=bot, settings=SETTINGS, root=str(tmp_path),
                       reg=reg, worker_id=wid,
                       log=lambda *_a, **_k: None)
    return w, reg, bot


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


def _journal_card_delivered(state_dir, message_id="9001"):
    """The card attempt's durable result — the gate _resume_parts reads
    before it may attach any dependent part."""
    journal.append(str(state_dir), "w0",
                   {"phase": "result", "attempt_id": "ab" * 8,
                    "delivery_id": DELIVERY_ID,
                    "result": "delivered", "message_id": message_id})


def _card_delivered(tmp_path, message_id="9001"):
    _journal_card_delivered(_state(tmp_path), message_id)


# ---------- part engine -----------------------------------------------------

def test_all_parts_delivered_with_remote_ids(tmp_path):
    chunks = _chunks(5)
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(chunks)
    claim = _claim(spec)

    async def run():
        await w._deliver_parts(claim, "9001")

    asyncio.run(run())
    ch = bot.channels[42]
    assert len(ch.threads) == 1
    th = ch.threads[0]
    assert [m.content for m in th.sent] == chunks
    parts = _sent_parts(_state(tmp_path))
    assert set(parts) == {"thread"} | {f"body:{i + 1:04d}"
                                       for i in range(5)}
    assert all(p["result"] == "delivered" and p.get("remote_id")
               for p in parts.values())
    # every part produced a part_receipt + the thread got its
    # card-binding thread_receipt too
    envs = _receipts(tmp_path / "cmd_int")
    preceipts = [e for e in envs if e["op"] == "part_receipt"]
    assert len(preceipts) == len(chunks) + 1
    assert {e["part_id"] for e in preceipts} == set(parts)
    assert [e for e in envs if e["op"] == "thread_receipt"]
    for e in preceipts:
        assert e["delivery_id"] == spec["delivery_id"]
        assert e["correlation"] == spec["delivery"]["correlation"]
        assert e["result"] == "delivered" and e.get("remote_id")


def test_crash_after_second_part_resumes_without_dupes(tmp_path):
    chunks = _chunks(5)
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(chunks)
    claim = _claim(spec)
    ch = bot.channels[42]

    # hard crash between parts: part 3's attempt never even starts —
    # the driver dies before its journal write
    orig_attempt = w._attempt_part

    async def crashy(claim_, part, ctx):
        if part["part_id"] == "body:0003":
            raise RuntimeError("simulated worker crash")
        return await orig_attempt(claim_, part, ctx)

    w._attempt_part = crashy
    with pytest.raises(RuntimeError):
        asyncio.run(w._deliver_parts(claim, "9001"))
    th = ch.threads[0]
    assert [m.content for m in th.sent] == chunks[:2]
    parts = _sent_parts(_state(tmp_path))
    assert "body:0003" not in parts and "body:0004" not in parts

    # a NEW worker on the same dirs resumes: thread + parts 1-2 are
    # settled in the journal, parts 3+ provably never began
    _card_delivered(tmp_path)
    w2, reg2, _ = _mkworker(tmp_path, bot=bot, wid="w2")
    asyncio.run(w2._resume_parts(spec))
    assert [m.content for m in th.sent] == chunks          # no dupes
    assert len(ch.sent) == 1          # no card repost to rescue parts
    parts = _sent_parts(_state(tmp_path))
    body = {k: v for k, v in parts.items() if k.startswith("body:")}
    assert len(body) == 5 and all(v["result"] == "delivered"
                                  for v in body.values())
    # identical part identities across the restart
    assert sorted(body) == [f"body:{i + 1:04d}" for i in range(5)]


def test_crash_after_result_before_receipt_republishes(tmp_path):
    """Kill the publish step of part 3: its result is fsync'd in the
    journal (sent), the receipt never left — restart republishes the
    fact instead of resending, then delivers the never-attempted rest."""
    chunks = _chunks(4)
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(chunks)
    claim = _claim(spec)
    ch = bot.channels[42]

    publish = envelopes.publish_command
    def crashy(d, env):
        if env.get("op") == "part_receipt" \
                and env.get("part_id") == "body:0003":
            raise OSError("simulated crash")
        return publish(d, env)

    import hermes_plugin.mcs_delivery.worker as wm
    old = wm.envelopes.publish_command
    wm.envelopes.publish_command = crashy
    try:
        with pytest.raises(OSError, match="simulated crash"):
            asyncio.run(w._deliver_parts(claim, "9001"))
    finally:
        wm.envelopes.publish_command = old
    parts = _sent_parts(_state(tmp_path))
    assert parts["body:0003"]["result"] == "delivered"
    assert "body:0004" not in parts

    _card_delivered(tmp_path)
    w2, _, _ = _mkworker(tmp_path, bot=bot, wid="w2")
    asyncio.run(w2.reconcile())          # republish unreported results
    asyncio.run(w2._resume_parts(spec))
    th = ch.threads[0]
    assert [m.content for m in th.sent] == chunks      # each once
    envs = [e for e in _receipts(tmp_path / "cmd_int")
            if e["op"] == "part_receipt"
            and e["part_id"] == "body:0003"]
    # republished factual receipt, not a resend
    assert envs and all(e["result"] == "delivered" for e in envs)


def test_timeout_after_acceptance_stays_unknown_no_resend(tmp_path):
    chunks = _chunks(3)
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(chunks)
    claim = _claim(spec)

    orig = FakeThread.send
    accepted = []

    async def timeout_send(self, content=None, **kw):
        if not accepted and len(self.sent) == 1:
            accepted.append(content)          # remote commit, then hang
            raise asyncio.TimeoutError()
        return await orig(self, content, **kw)

    FakeThread.send = timeout_send
    try:
        asyncio.run(w._deliver_parts(claim, "9001"))
    finally:
        FakeThread.send = orig
    th = bot.channels[42].threads[0]
    # the timed-out send never appended; part 3 still went out
    assert [m.content for m in th.sent] == [chunks[0], chunks[2]]
    parts = _sent_parts(_state(tmp_path))
    assert parts["body:0002"]["result"] == "unknown"
    assert parts["body:0003"]["result"] == "delivered"
    # resume must NOT re-send the unknown part — it may have committed
    _card_delivered(tmp_path)
    w2, _, _ = _mkworker(tmp_path, bot=bot, wid="w2")
    asyncio.run(w2._resume_parts(spec))
    assert [m.content for m in th.sent] == [chunks[0], chunks[2]]


def test_part_receipt_publish_failure_remains_resumable(tmp_path, monkeypatch):
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(_chunks(2))
    original = envelopes.publish_command

    def fail_receipt(directory, env):
        if env.get("op") == "part_receipt" and env.get("part_id") == "body:0001":
            raise OSError("synthetic disk unavailable")
        return original(directory, env)

    monkeypatch.setattr(envelopes, "publish_command", fail_receipt)
    with pytest.raises(OSError):
        asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    _card_delivered(tmp_path)
    try:
        asyncio.run(w._resume_parts(spec))
    except OSError:
        pass
    assert not reg.parts_done(DELIVERY_ID)
    monkeypatch.setattr(envelopes, "publish_command", original)
    asyncio.run(w._resume_parts(spec))
    assert reg.parts_done(DELIVERY_ID)
    assert [m.content for m in bot.channels[42].threads[0].sent] == _chunks(2)
    assert any(e.get("part_id") == "body:0001" for e in _receipts(tmp_path / "cmd_int"))


@pytest.mark.parametrize("stop_kind", ["kill_switch", "restore_marker"])
def test_stop_between_parts_resumes_only_unsent_remainder(tmp_path, monkeypatch, stop_kind):
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(_chunks(2))
    flags = tmp_path / "flags" / "notify.json"
    attempt = w._attempt_part

    async def stop_after_thread(claim, part, ctx):
        await attempt(claim, part, ctx)
        if part["kind"] == "thread":
            if stop_kind == "restore_marker":
                # The marker precedes the runner's next flag publication.
                (tmp_path / "restore_pending.json").write_text("{}")
            else:
                flags.write_text(json.dumps({"interactive": False}))

    monkeypatch.setattr(w, "_attempt_part", stop_after_thread)
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    thread = bot.channels[42].threads[0]
    assert not thread.sent and not reg.parts_done(DELIVERY_ID)
    _card_delivered(tmp_path)
    asyncio.run(w._resume_parts(spec))
    assert not thread.sent
    flags.write_text(json.dumps({"interactive": True}))
    if stop_kind == "restore_marker":
        (tmp_path / "restore_pending.json").unlink()
    asyncio.run(w._resume_parts(spec))
    assert [m.content for m in thread.sent] == _chunks(2)
    assert len(bot.channels[42].threads) == 1


def test_thread_create_failure_holds_dependents_no_card_repost(tmp_path):
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(_chunks(3))
    claim = _claim(spec)
    bot.channels[42].thread_fail = FakeHTTP(403)

    asyncio.run(w._deliver_parts(claim, "9001"))
    ch = bot.channels[42]
    assert not ch.threads                        # no thread
    assert len(ch.sent) == 1                     # no card repost
    parts = _sent_parts(_state(tmp_path))
    assert parts["thread"]["result"] == "not_sent"
    assert parts["thread"]["error_code"] == "http_403"
    assert not [k for k in parts if k.startswith("body:")]
    envs = _receipts(tmp_path / "cmd_int")
    tre = [e for e in envs if e["op"] == "thread_receipt"]
    assert tre and tre[0].get("error_code") == "http_403"
    # body parts never even attempted — runner holds them
    assert not [e for e in envs if e["op"] == "part_receipt"
                and e.get("part_id", "").startswith("body:")]


def test_thread_part_binds_thread_left_by_earlier_attempt(tmp_path):
    """A sealed create-spec races a thread created between publication
    and the claim (older-generation worker, crash retry): Discord
    rejects the dup create with 400 but the goal already holds — bind
    the existing thread instead of failing, and carry on with body
    parts. msg.thread populated = the discord.py fast path."""
    chunks = _chunks(3)
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(chunks)                    # create, no bound thread_id
    claim = _claim(spec)
    ch = bot.channels[42]
    msg = ch.sent[0]
    existing = FakeThread(9001)             # Discord: thread.id == msg.id
    ch.threads.append(existing)
    msg.thread = existing                   # discord.py Message.thread
    ch.thread_fail = FakeHTTP(400)          # "already has a thread"

    asyncio.run(w._deliver_parts(claim, "9001"))
    assert len(ch.threads) == 1             # bound, never re-created
    assert [m.content for m in existing.sent] == chunks
    parts = _sent_parts(_state(tmp_path))
    assert parts["thread"]["result"] == "delivered"
    assert parts["thread"]["remote_id"] == "9001"
    envs = [e for e in _receipts(tmp_path / "cmd_int")
            if e["op"] == "thread_receipt"]
    assert envs and envs[0].get("thread_id") == "9001"


def test_thread_part_binds_thread_via_channel_lookup(tmp_path):
    """Same race when the message payload carries no .thread (older
    SDK, cold cache): fall back to resolving the thread under its
    starter-message snowflake — fetch_channel(msg.id)."""
    chunks = _chunks(2)
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(chunks)
    claim = _claim(spec)
    ch = bot.channels[42]
    existing = FakeThread(9001)
    ch.threads.append(existing)             # msg.thread stays unset
    ch.thread_fail = FakeHTTP(400)

    asyncio.run(w._deliver_parts(claim, "9001"))
    assert [m.content for m in existing.sent] == chunks
    parts = _sent_parts(_state(tmp_path))
    assert parts["thread"]["result"] == "delivered"
    assert parts["thread"]["remote_id"] == "9001"


def test_thread_create_failure_without_existing_thread_still_fails(
        tmp_path):
    """A genuine 400 (no thread under the message) must NOT bind —
    the part stays not_sent and the original error is preserved."""
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(_chunks(2))
    claim = _claim(spec)
    bot.channels[42].thread_fail = FakeHTTP(400)

    asyncio.run(w._deliver_parts(claim, "9001"))
    ch = bot.channels[42]
    assert not ch.threads
    parts = _sent_parts(_state(tmp_path))
    assert parts["thread"]["result"] == "not_sent"
    assert parts["thread"]["error_code"] == "http_400"
    assert not [k for k in parts if k.startswith("body:")]
    # a per-message 400 is not a scope verdict — capability stays open
    cap = reg.capability(scope_key(w.scope()))
    assert not (cap and cap.get("ok") is False)


def test_thread_create_auth_failure_blocks_scope(tmp_path):
    """A 403 *is* a scope verdict: the thread part fails, dependents
    hold, and the capability cache marks the scope unable."""
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(_chunks(2))
    claim = _claim(spec)
    bot.channels[42].thread_fail = FakeHTTP(403)

    asyncio.run(w._deliver_parts(claim, "9001"))
    parts = _sent_parts(_state(tmp_path))
    assert parts["thread"]["result"] == "not_sent"
    assert parts["thread"]["error_code"] == "http_403"
    cap = reg.capability(scope_key(w.scope()))
    assert cap and cap.get("ok") is False


def test_update_backfill_dedupe_remote_verified(tmp_path):
    """An update render against an already-populated thread marks each
    part delivered against the REAL remote message it matched — never a
    second copy, never a claimed-but-unverified skip."""
    chunks = _chunks(3)
    w, reg, bot = _mkworker(tmp_path)
    th = FakeThread(7700)
    bot.channels[42].threads.append(th)
    # the create render already posted all chunks — with real remote ids
    for c in chunks:
        asyncio.run(th.send(c))
    spec = _spec(chunks, op="update", thread_id="7700")
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    assert [m.content for m in th.sent] == chunks     # zero new posts
    parts = _sent_parts(_state(tmp_path))
    body = {k: v for k, v in parts.items() if k.startswith("body:")}
    assert len(body) == 3
    for part_id, row in body.items():
        i = int(part_id.split(":")[1]) - 1
        assert row["result"] == "delivered"
        # bound to the existing remote message, not a fresh post
        assert row["remote_id"] == str(th.sent[i].id)


def test_update_backfill_posts_only_missing_parts(tmp_path):
    chunks = _chunks(4)
    w, reg, bot = _mkworker(tmp_path)
    th = FakeThread(7700)
    bot.channels[42].threads.append(th)
    asyncio.run(th.send(chunks[0]))             # only part 1 got out
    spec = _spec(chunks, op="update", thread_id="7700")
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    assert [m.content for m in th.sent] == chunks
    parts = _sent_parts(_state(tmp_path))
    assert parts["body:0001"]["remote_id"] == str(th.sent[0].id)
    for i in (1, 2, 3):
        assert parts[f"body:{i + 1:04d}"]["remote_id"] \
            == str(th.sent[i].id)


def test_remote_match_never_binds_foreign_content(tmp_path):
    """Unrelated remote content — even byte-identical, even inside the
    scan window — is not our part. Only messages this bot authored can
    be bound as remote-verified deliveries."""
    chunks = _chunks(2)
    w, reg, bot = _mkworker(tmp_path)
    th = FakeThread(7700)
    bot.channels[42].threads.append(th)
    # a foreign user happens to have posted the same text earlier
    th.sent.append(_HistMsg(7001, chunks[0], author=FOREIGN_USER))
    spec = _spec(chunks, op="update", thread_id="7700")
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    # the part posted its own copy; the foreign id was never consumed
    assert len(th.sent) == 1 + len(chunks)
    parts = _sent_parts(_state(tmp_path))
    for i in range(len(chunks)):
        row = parts[f"body:{i + 1:04d}"]
        assert row["result"] == "delivered"
        assert row["remote_id"] != "7001"
        assert row["remote_id"] == str(th.sent[1 + i].id)


def test_second_attachment_failure_leaves_incomplete(tmp_path):
    """The second file's upload vanishing mid-wire is unknown — the
    first file's delivered receipt stands, no part resends."""
    b1, b2 = b"first-file", b"second-file"
    f1, f2 = tmp_path / "a1.bin", tmp_path / "a2.bin"
    f1.write_bytes(b1)
    f2.write_bytes(b2)
    atts = [
        {"part_id": "attach:0007", "kind": "attachment_part",
         "attachment_id": 7, "name": "a1.bin", "path": str(f1),
         "sha256": hashlib.sha256(b1).hexdigest(), "bytes": len(b1)},
        {"part_id": "attach:0008", "kind": "attachment_part",
         "attachment_id": 8, "name": "a2.bin", "path": str(f2),
         "sha256": hashlib.sha256(b2).hexdigest(), "bytes": len(b2)}]
    w, reg, bot = _mkworker(tmp_path)
    th = FakeThread(7700)
    bot.channels[42].threads.append(th)
    th.fail_at = 2                  # sends: body(0) attach1(1) attach2(!)
    spec = _spec(_chunks(1), op="update", thread_id="7700")
    for a in atts:
        a["index"] = len(spec["parts"]["manifest"])
        spec["parts"]["manifest"].append(a)
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    parts = _sent_parts(_state(tmp_path))
    assert parts["attach:0007"]["result"] == "delivered"
    assert parts["attach:0008"]["result"] == "unknown"
    # a replayed drive resends nothing — every part is journaled final
    n_sent = len(th.sent)
    asyncio.run(w._deliver_parts(claim, "9001"))
    assert len(th.sent) == n_sent


def test_attachment_part_uploads_verified_file(tmp_path):
    blob = b"synthetic-attachment"
    f = tmp_path / "att.bin"
    f.write_bytes(blob)
    sha = hashlib.sha256(blob).hexdigest()
    att = {"part_id": "attach:0007", "kind": "attachment_part",
           "index": 0, "attachment_id": 7, "name": "att.bin",
           "path": str(f), "sha256": sha, "bytes": len(blob)}
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(_chunks(1))
    att["index"] = len(spec["parts"]["manifest"])
    spec["parts"]["manifest"].append(att)
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    parts = _sent_parts(_state(tmp_path))
    assert parts["attach:0007"]["result"] == "delivered"
    assert parts["attach:0007"]["remote_id"]


def test_attachment_part_hash_mismatch_not_sent(tmp_path):
    f = tmp_path / "att.bin"
    f.write_bytes(b"mutated bytes")
    att = {"part_id": "attach:0007", "kind": "attachment_part",
           "index": 0, "attachment_id": 7, "name": "att.bin",
           "path": str(f), "sha256": "ab" * 32, "bytes": 13}
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(_chunks(1))
    att["index"] = len(spec["parts"]["manifest"])
    spec["parts"]["manifest"].append(att)
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    parts = _sent_parts(_state(tmp_path))
    assert parts["attach:0007"]["result"] == "not_sent"
    assert parts["attach:0007"]["error_code"] == "attachment_mismatch"
    envs = [e for e in _receipts(tmp_path / "cmd_int")
            if e["op"] == "part_receipt" and e["part_id"] == "attach:0007"]
    assert envs and envs[0]["result"] == "not_sent"


def test_attachment_upload_uses_the_bytes_that_were_verified(tmp_path, monkeypatch):
    from pathlib import Path
    from hermes_plugin.mcs_discord import cards
    blob = b"sealed-synthetic"
    path = tmp_path / "attachment.bin"
    path.write_bytes(blob)
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec([])
    spec["parts"]["manifest"].append({
        "part_id": "attach:0001", "kind": "attachment_part", "index": 2,
        "attachment_id": 1, "name": path.name, "path": str(path),
        "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)})
    uploaded = []

    async def send(target, source, name):
        path.write_bytes(b"changed-on-disk!")
        uploaded.append(source.read() if hasattr(source, "read")
                        else Path(source).read_bytes())
        return await target.send("synthetic-file")

    monkeypatch.setattr(cards, "send_attachment", send)
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert uploaded == [blob]


def test_unavailable_attachment_entry_never_attempted(tmp_path):
    att = {"part_id": "attach:0009", "kind": "attachment_part",
           "index": 0, "attachment_id": 9, "name": "gone.bin",
           "unavailable": True}
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(_chunks(1))
    att["index"] = len(spec["parts"]["manifest"])
    spec["parts"]["manifest"].append(att)
    claim = _claim(spec)
    asyncio.run(w._deliver_parts(claim, "9001"))
    parts = _sent_parts(_state(tmp_path))
    assert "attach:0009" not in parts         # runner pre-settled it


def test_parts_stable_across_restart_identity(tmp_path):
    """The planned part-id set + hashes live in the spec — identical
    after a worker restart, independent of send progress."""
    chunks = _chunks(6)
    spec = _spec(chunks)
    w1, _, bot = _mkworker(tmp_path)
    plan1 = [(p["part_id"], p.get("sha256"))
             for p in spec["parts"]["manifest"]]
    # w1 delivers the thread + part 1, then dies
    claim = _claim(spec)
    orig = w1._attempt_part

    async def crashy(claim_, part, ctx):
        if part["part_id"] == "body:0002":
            raise RuntimeError("crash")
        return await orig(claim_, part, ctx)
    w1._attempt_part = crashy
    with pytest.raises(RuntimeError):
        asyncio.run(w1._deliver_parts(claim, "9001"))
    _card_delivered(tmp_path)
    w2, _, _ = _mkworker(tmp_path, bot=bot, wid="w2")
    # a restarted worker reads the same spec file — same plan
    raw = json.loads(json.dumps(spec))
    plan2 = [(p["part_id"], p.get("sha256"))
             for p in raw["parts"]["manifest"]]
    assert plan1 == plan2
    asyncio.run(w2._resume_parts(spec))
    parts = _sent_parts(_state(tmp_path))
    assert len(parts) == len(plan1) - 1    # card excluded
    assert all(v["result"] == "delivered" for v in parts.values())


def test_resume_requires_delivered_card(tmp_path):
    """Parts never attach to an unproven card: resume on a spec whose
    journal shows no delivered card result is a no-op."""
    chunks = _chunks(2)
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(chunks)
    asyncio.run(w._resume_parts(spec))
    ch = bot.channels[42]
    assert not ch.threads and len(ch.sent) == 1
    # a not_sent card result must not unblock parts either
    journal.append(str(_state(tmp_path)), "w0",
                   {"phase": "result", "attempt_id": "ab" * 8,
                    "delivery_id": DELIVERY_ID,
                    "result": "not_sent", "error_code": "http_403"})
    asyncio.run(w._resume_parts(spec))
    assert not ch.threads and len(ch.sent) == 1


# ---------- journal growth / scan bounds (U04-F05) --------------------------

def _sealed(spec):
    """The runner seals the card part hash; the synthetic spec must too
    before the tick's validate() will accept it."""
    parts = spec["parts"]
    parts["manifest"][0]["sha256"] = hashlib.sha256(envelopes.canonical(
        {k: parts[k] for k in ("containers", "footer", "action_rows")}
    )).hexdigest()
    return spec


def _publish_spec(tmp_path, spec):
    (tmp_path / "discord_render" / f"{spec['delivery_id']}.json").write_text(
        json.dumps(spec))


def test_dead_spec_with_unproven_card_is_not_rescanned_every_tick(
        tmp_path, monkeypatch):
    """A lingering dead spec whose card outcome is unknown (awaiting an
    operator card_resolve) must not cost a full journal scan per tick."""
    w, reg, _ = _mkworker(tmp_path)
    spec = _sealed(_spec(_chunks(2)))
    _publish_spec(tmp_path, spec)
    journal.append(str(_state(tmp_path)), "w0",
                   {"phase": "result", "attempt_id": "ab" * 8,
                    "delivery_id": DELIVERY_ID, "result": "unknown",
                    "error_code": "worker_crash"})
    reg.mark_dead(DELIVERY_ID)
    calls = []
    real = journal.scan
    monkeypatch.setattr(journal, "scan",
                        lambda d: calls.append(d) or real(d))
    real_refresh = journal.ScanCache.refresh
    monkeypatch.setattr(journal.ScanCache, "refresh",
                        lambda c: calls.append(c) or real_refresh(c))

    async def ticks():
        for _ in range(5):
            await w.tick()
    asyncio.run(ticks())
    assert len(calls) <= 1
    assert reg.parts_done(DELIVERY_ID)
    assert not bot_sent_parts(tmp_path)


def bot_sent_parts(tmp_path):
    return _sent_parts(_state(tmp_path))


def test_dead_tombstone_outlives_ttl_while_spec_is_published(
        tmp_path, monkeypatch):
    """An expired tombstone for a still-published spec would re-claim it
    daily; one whose spec is gone expires with its part progress."""
    from hermes_plugin.mcs_delivery import registry as registry_mod
    w, reg, _ = _mkworker(tmp_path)
    spec = _sealed(_spec(_chunks(2)))
    _publish_spec(tmp_path, spec)
    gone = "00000000-0000-4000-8000-00000000beef"
    for did in (DELIVERY_ID, gone):
        reg.mark_dead(did)
        reg.put_parts_done(did)
    monkeypatch.setattr(registry_mod, "time", types.SimpleNamespace(
        time=lambda: 10 ** 10))                  # far past DEAD_TTL_S
    asyncio.run(w.tick())
    assert reg.is_dead(DELIVERY_ID) and reg.parts_done(DELIVERY_ID)
    assert not reg.is_dead(gone) and not reg.parts_done(gone)
    assert not reg.claimed(DELIVERY_ID)
    assert not list((tmp_path / "cmd_int").glob("*.json"))


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


def _backup(tmp_path, mtime):
    import os
    (tmp_path / "backups").mkdir(exist_ok=True)
    path = tmp_path / "backups" / "ledger-20260101.db"
    path.write_bytes(b"")
    os.utime(path, (mtime, mtime))


def test_journal_compaction_drops_only_settled_pre_backup_attempts(tmp_path):
    import time as _time
    w, reg, _ = _mkworker(tmp_path)
    state = _state(tmp_path)
    now = _time.time()
    old, recent = now - 5 * 86400, now - 3600
    ids = {k: f"00000000-0000-4000-8000-0000000000{i:02d}"
           for i, k in enumerate(("done", "unknown", "open", "recent",
                                  "claimed", "pending", "foreign"))}
    _settled(state, "w0", "a1" * 8, ids["done"], old)
    _settled(state, "w0", "a2" * 8, ids["unknown"], old, result="unknown")
    journal.append(str(state), "w0", {
        "phase": "started", "attempt_id": "a3" * 8,
        "delivery_id": ids["open"], "ts": old,
        "receipt_envelope": _envelope()})
    _settled(state, "w0", "a4" * 8, ids["recent"], recent)
    _settled(state, "w0", "a5" * 8, ids["claimed"], old)
    _settled(state, "w0", "a6" * 8, ids["pending"], old)
    _settled(state, "w9", "a7" * 8, ids["foreign"], old, channel_id="99")
    _settled(state, "w8", "a8" * 8, ids["done"] + "x", old)
    with (state / "journal-w8.jsonl").open("a") as stream:
        stream.write("{torn\n")
    reg.put_parts_done(ids["done"])
    reg.claim(ids["claimed"], {"attempt_id": "zz", "phase": "settled"})
    pending = dict(_sealed(_spec(_chunks(1))), delivery_id=ids["pending"])
    _publish_spec(tmp_path, pending)             # parts not yet done
    before = journal.scan(str(state))

    # no restorable backup set -> nothing is pruned
    asyncio.run(w.maintain_journal(rotate=True))
    assert journal.scan(str(state)) == before

    _backup(tmp_path, now - 86400)
    asyncio.run(w.maintain_journal(rotate=True))
    after = journal.scan(str(state))
    assert "a1" * 8 not in after
    assert set(before) - set(after) == {"a1" * 8}
    assert after["a3" * 8] == before["a3" * 8]
    assert (state / "journal-w8.jsonl").read_text().endswith("{torn\n")
    # the recorded crash-recovery classification is unchanged
    assert journal.unfinished(after).keys() == journal.unfinished(before).keys()
    assert journal.unreported(after).keys() == journal.unreported(before).keys()


def test_journal_compaction_pauses_while_a_restore_is_pending(tmp_path):
    """The journal is the post-restore reconcile's evidence; a pending
    restore may retire its backup and move the horizon — never prune."""
    import time as _time
    w, _reg, _ = _mkworker(tmp_path)
    state = _state(tmp_path)
    now = _time.time()
    _settled(state, "w0", "a1" * 8,
             "00000000-0000-4000-8000-000000000001", now - 5 * 86400)
    _backup(tmp_path, now - 86400)
    from pathlib import Path
    (Path(w._root) / "restore_pending.json").write_text("{}")
    before = journal.scan(str(state))
    asyncio.run(w.maintain_journal(rotate=True))
    assert journal.scan(str(state)) == before

def test_live_segment_rotates_and_closed_segment_is_compacted(
        tmp_path, monkeypatch):
    import time as _time
    from hermes_plugin.mcs_delivery import worker as worker_mod
    w, reg, _ = _mkworker(tmp_path)
    state = _state(tmp_path)
    old = _time.time() - 5 * 86400
    _backup(tmp_path, _time.time())
    monkeypatch.setattr(worker_mod, "JOURNAL_SEGMENT_BYTES", 256)
    did = "00000000-0000-4000-8000-0000000000aa"
    for phase in ("begin", "granted", "started", "result", "receipt"):
        w._journal(phase, attempt_id="b1" * 8, delivery_id=did, ts=old,
                   result="delivered", begin_envelope=_envelope())
    reg.put_parts_done(did)
    first = state / "journal-w1.jsonl"
    assert first.exists()
    asyncio.run(w.maintain_journal())
    w._journal("claimed", attempt_id="b2" * 8, delivery_id="x")
    assert (state / "journal-w1~000001.jsonl").exists()
    assert not first.exists()                  # fully settled -> removed
    assert list(journal.scan(str(state))) == ["b2" * 8]


# ---------- runner/worker spec compatibility (U04-F04) ----------------------

@pytest.mark.parametrize("mutate,reason", [
    (lambda s: s["parts"].__setitem__("future_poll", {"q": "?"}),
     "unsupported_parts_key"),
    (lambda s: s["parts"]["manifest"][1].__setitem__("future_flag", 1),
     "unsupported_part_key"),
    (lambda s: s["delivery"].__setitem__("future_route", "x"),
     "unsupported_delivery_key"),
    (lambda s: s.__setitem__("future_top", 1), "unsupported_spec_key"),
])
def test_outdated_worker_holds_spec_with_unknown_feature(tmp_path, mutate,
                                                         reason):
    """A worker that was not restarted after a runner upgrade must not
    send the card and silently drop a feature it does not understand —
    the spec is rejected whole, nothing is claimed or sent, and the
    rejection is logged once instead of every tick."""
    from hermes_plugin.mcs_delivery import spec as spec_mod
    logs = []
    w, reg, bot = _mkworker(tmp_path)
    w._log = lambda event, **f: logs.append((event, f))
    spec = _sealed(_spec(_chunks(1)))
    spec_mod.validate(spec)                       # baseline is accepted
    mutate(spec)
    with pytest.raises(ValueError, match=reason):
        spec_mod.validate(spec)
    _publish_spec(tmp_path, spec)
    sent_before = len(bot.channels[42].sent)

    async def ticks():
        for _ in range(3):
            await w.tick()
    asyncio.run(ticks())
    assert len(bot.channels[42].sent) == sent_before
    assert not reg.claimed(DELIVERY_ID)
    assert not list((tmp_path / "cmd_int").glob("*.json"))
    assert not list((tmp_path / "discord_render").glob("*.claimed"))
    assert logs == [("spec_rejected", {"delivery_id": DELIVERY_ID,
                                       "error": reason})]


# ---------- transient begin denials (signal notify) -------------------------

REVOKED_ID = "00000000-0000-4000-8000-00000000dead"


def _card_spec(delivery_id, kind="signal"):
    """A legacy card with no manifest — the tick sends only the card."""
    return {"schema": "mcs-card-render/v1",
            "delivery_id": delivery_id,
            "logical_intent_id": f"v1|{kind}|1|sig-1",
            "card_key": f"v1|{kind}|1|sig-1", "kind": kind, "op": "create",
            "render_rev": 1, "source_generation": 1,
            "presentation_generation": 1, "ui_revision": 1,
            "delivery": {"route_epoch": 1, "correlation": "cd" * 16,
                         "profile": "mcs", "application_id": "1",
                         "channel_id": "42", "guild_id": "7",
                         "intent_event_ids": [1]},
            "parts": {"containers": [{"type": "text",
                                      "text": f"{kind} card"}]}}


def _write_begin_result(tmp_path, begin, *, granted, error=None):
    """The runner's begin-result shape, written where the worker reads it."""
    body = {"granted": granted, "command_id": begin["command_id"],
            "attempt_id": begin["attempt_id"],
            "delivery_id": begin["delivery_id"],
            "attempt_state": "granted" if granted else "not_sent",
            "worker_id": begin["worker_id"],
            "render_rev": begin["render_rev"],
            "payload_hash": begin["payload_hash"],
            "route_epoch": begin["route_epoch"]}
    if error:
        body["error"] = error
    name = paths.safe_name(begin["command_id"]) + ".json"
    (tmp_path / "cmd_results" / name).write_text(json.dumps(body))


def _spec_path(tmp_path, delivery_id):
    return tmp_path / "discord_render" / f"{delivery_id}.json"


def _journal_rows(tmp_path):
    rec = journal.scan(str(_state(tmp_path)))
    return [r for rows in rec.values() for r in rows]


def test_signal_notify_off_stays_claimable_and_revoked_stays_dead(
        tmp_path, monkeypatch):
    """denied_signal_notify_off drops the claim without a dead tombstone.

    A worker restart between OFF and ON reloads the registry and still
    claims the same spec; the grant then records a real send. A
    non-transient denial (denied_card_revoked) still tombstones.
    """
    from hermes_plugin.mcs_discord import cards as cards_mod
    from hermes_plugin.mcs_delivery import spec as spec_mod

    monkeypatch.setattr(cards_mod, "build_view", lambda spec: object())
    w, reg, bot = _mkworker(tmp_path)
    sent_before = len(bot.channels[42].sent)
    revoked = _card_spec(REVOKED_ID, kind="thread")
    signal = _card_spec(DELIVERY_ID, kind="signal")
    spec_mod.validate(revoked)
    spec_mod.validate(signal)
    _publish_spec(tmp_path, revoked)
    _publish_spec(tmp_path, signal)

    async def deny(worker, registry, delivery_id, error):
        await worker.tick()
        claim = registry.claimed(delivery_id)
        assert claim and claim["phase"] == "begin_sent"
        _write_begin_result(tmp_path, claim["begin_env"],
                            granted=False, error=error)
        await worker.tick()
        return claim

    async def run():
        revoked_claim = await deny(
            w, reg, REVOKED_ID, "denied_card_revoked")
        assert reg.claimed(REVOKED_ID) is None
        assert reg.is_dead(REVOKED_ID)
        assert _spec_path(tmp_path, REVOKED_ID).is_file()
        # restart must not expire a tombstone whose spec is still published
        reloaded = Registry(str(_state(tmp_path)))
        assert reloaded.is_dead(REVOKED_ID)
        assert reloaded.claimed(REVOKED_ID) is None

        denied = await deny(
            w, reg, DELIVERY_ID, "denied_signal_notify_off")
        assert reg.claimed(DELIVERY_ID) is None
        assert not reg.is_dead(DELIVERY_ID)
        assert _spec_path(tmp_path, DELIVERY_ID).is_file()
        assert len(bot.channels[42].sent) == sent_before
        assert any(r.get("phase") == "denied"
                   and r.get("error") == "denied_signal_notify_off"
                   and r.get("delivery_id") == DELIVERY_ID
                   for r in _journal_rows(tmp_path))

        # process restart between OFF and ON — disk registry, new worker_id
        w2, reg2, _ = _mkworker(tmp_path, bot=bot, wid="w-restart")
        assert not reg2.is_dead(DELIVERY_ID)
        assert reg2.claimed(DELIVERY_ID) is None
        assert _spec_path(tmp_path, DELIVERY_ID).is_file()
        await w2.tick()                    # re-claim + new begin
        claim = reg2.claimed(DELIVERY_ID)
        assert claim and claim["phase"] == "begin_sent"
        assert claim["attempt_id"] != denied["attempt_id"]
        assert claim["worker_id"] == "w-restart"
        _write_begin_result(tmp_path, claim["begin_env"], granted=True)
        await w2.tick()                    # grant -> send -> receipt
        return revoked_claim, claim

    _, claim = asyncio.run(run())
    sent = bot.channels[42].sent
    assert len(sent) == sent_before + 1
    message_id = str(sent[-1].id)
    delivered = [r for r in _journal_rows(tmp_path)
                 if r.get("phase") == "result"
                 and r.get("delivery_id") == DELIVERY_ID
                 and not r.get("part_id")]
    assert len(delivered) == 1
    assert delivered[0]["result"] == "delivered"
    assert delivered[0]["message_id"] == message_id
    assert delivered[0]["attempt_id"] == claim["attempt_id"]
    receipts = [e for e in _receipts(tmp_path / "cmd_int")
                if e.get("op") == "transport_receipt"
                and e.get("delivery_id") == DELIVERY_ID]
    assert any(e.get("result") == "delivered"
               and e.get("message_id") == message_id
               and e.get("attempt_id") == claim["attempt_id"]
               for e in receipts)
    # the revoked spec was never sent
    assert not any(r.get("phase") == "result"
                   and r.get("delivery_id") == REVOKED_ID
                   for r in _journal_rows(tmp_path))


def _begins(tmp_path, delivery_id=DELIVERY_ID):
    return [e for e in _receipts(tmp_path / "cmd_int")
            if e.get("op") == "transport_begin"
            and e.get("delivery_id") == delivery_id]


def _clock(monkeypatch, start=1_000_000.0):
    from hermes_plugin.mcs_delivery import worker as worker_mod
    now = [start]
    monkeypatch.setattr(worker_mod, "time",
                        types.SimpleNamespace(time=lambda: now[0]))
    return now


def test_signal_notify_off_backs_off_before_rebegin(tmp_path, monkeypatch):
    """A signal_notify_off denial holds the spec for SIGNAL_OFF_RETRY_S —
    no re-claim churn (attempt row + result file + journal per tick)."""
    from hermes_plugin.mcs_delivery import worker as worker_mod
    from hermes_plugin.mcs_discord import cards as cards_mod

    monkeypatch.setattr(cards_mod, "build_view", lambda spec: object())
    now = _clock(monkeypatch)
    w, reg, _ = _mkworker(tmp_path)
    _publish_spec(tmp_path, _card_spec(DELIVERY_ID))
    backoff = worker_mod.SIGNAL_OFF_RETRY_S

    async def run():
        await w.tick()
        first = reg.claimed(DELIVERY_ID)
        _write_begin_result(tmp_path, first["begin_env"], granted=False,
                            error="denied_signal_notify_off")
        await w.tick()                              # denied -> released
        assert reg.claimed(DELIVERY_ID) is None
        assert not reg.is_dead(DELIVERY_ID)
        start = now[0]
        for dt in (0.0, 2.0, 60.0, backoff - 1):
            now[0] = start + dt
            await w.tick()
            assert len(_begins(tmp_path)) == 1, dt
            assert reg.claimed(DELIVERY_ID) is None
        now[0] = start + backoff
        await w.tick()
        await w.tick()
        second = reg.claimed(DELIVERY_ID)
        assert second and second["phase"] == "begin_sent"
        assert second["attempt_id"] != first["attempt_id"]

    asyncio.run(run())
    assert len(_begins(tmp_path)) == 2
    claimed = [r for r in _journal_rows(tmp_path)
               if r.get("phase") == "claimed"
               and r.get("delivery_id") == DELIVERY_ID]
    assert len(claimed) == 2


def test_restart_reconcile_keeps_begin_sent_signal_claimable(
        tmp_path, monkeypatch):
    """A gateway restart with the claim in begin_sent (OFF) goes through
    the real reconcile(): the pre-HTTP attempt is reported not_sent but
    never tombstoned, a second restart does not tombstone it either, and
    after ON the same delivery_id is sent exactly once."""
    from hermes_plugin.mcs_discord import cards as cards_mod

    monkeypatch.setattr(cards_mod, "build_view", lambda spec: object())
    _clock(monkeypatch)
    w1, reg1, bot = _mkworker(tmp_path)
    sent_before = len(bot.channels[42].sent)
    _publish_spec(tmp_path, _card_spec(DELIVERY_ID))
    marker = tmp_path / "discord_render" / f"{DELIVERY_ID}.json.claimed"

    async def run():
        await w1.tick()                   # claim + begin, then "crash"
        a = reg1.claimed(DELIVERY_ID)
        assert a["phase"] == "begin_sent" and marker.is_file()

        # restart 1 — tasks.py order: fresh Registry -> reconcile -> ticks
        w2, reg2, _ = _mkworker(tmp_path, bot=bot, wid="w2")
        stats = await w2.reconcile()
        assert stats["not_sent"] == 1
        assert not reg2.is_dead(DELIVERY_ID)
        assert reg2.claimed(DELIVERY_ID) is None
        assert not marker.exists()
        assert _spec_path(tmp_path, DELIVERY_ID).is_file()
        assert any(e.get("op") == "transport_receipt"
                   and e.get("attempt_id") == a["attempt_id"]
                   and e.get("result") == "not_sent"
                   and e.get("error_code") == "worker_restart"
                   for e in _receipts(tmp_path / "cmd_int"))
        await w2.tick()                   # still OFF: re-begin, denied
        b = reg2.claimed(DELIVERY_ID)
        assert b["attempt_id"] != a["attempt_id"]
        _write_begin_result(tmp_path, b["begin_env"], granted=False,
                            error="denied_signal_notify_off")
        await w2.tick()
        assert reg2.claimed(DELIVERY_ID) is None

        # restart 2 during the backoff — A's receipt row must not
        # tombstone the delivery now that no claim holds it
        w3, reg3, _ = _mkworker(tmp_path, bot=bot, wid="w3")
        await w3.reconcile()
        assert not reg3.is_dead(DELIVERY_ID)
        await w3.tick()                   # ON now: runner grants C
        c = reg3.claimed(DELIVERY_ID)
        assert c["attempt_id"] not in (a["attempt_id"], b["attempt_id"])
        _write_begin_result(tmp_path, c["begin_env"], granted=True)
        await w3.tick()
        await w3.tick()
        return c

    c = asyncio.run(run())
    sent = bot.channels[42].sent
    assert len(sent) == sent_before + 1
    delivered = [r for r in _journal_rows(tmp_path)
                 if r.get("phase") == "result"
                 and r.get("delivery_id") == DELIVERY_ID]
    assert [(r["result"], r["attempt_id"]) for r in delivered] \
        == [("delivered", c["attempt_id"])]
    receipts = [e for e in _receipts(tmp_path / "cmd_int")
                if e.get("op") == "transport_receipt"
                and e.get("result") == "delivered"]
    assert [e["attempt_id"] for e in receipts] == [c["attempt_id"]]


# ---------- incremental journal view for part dedupe ------------------------

def _did(i):
    return f"00000000-0000-4000-8000-{i:012x}"


def _same_as_full_scan(view, state):
    full = journal.scan(str(state))
    assert list(view) == list(full)
    assert {aid: view[aid] for aid in view} == full


@pytest.mark.parametrize("seed", range(20))
def test_scan_cache_equals_full_scan_under_random_journal_mutation(
        tmp_path, seed):
    """Appends, torn/corrupt lines, compact-style atomic rewrites,
    unlink + same-name recreate and truncation — the cache, refreshed
    at random points, always returns exactly what a full scan does."""
    import os
    import random
    rnd = random.Random(seed)
    cache = journal.ScanCache(str(tmp_path))
    held = []                 # (view, full scan when it was taken)
    names = [f"journal-w{i}.jsonl" for i in range(3)]
    aids = [f"a{i}" for i in range(6)]
    for _ in range(80):
        path = tmp_path / rnd.choice(names)
        op = rnd.randrange(8)
        if op <= 2:
            journal.append(str(tmp_path), path.name[8:-6], {
                "phase": rnd.choice(journal.PHASES),
                "attempt_id": rnd.choice(aids), "n": rnd.random()})
        elif op == 3:                     # torn tail, maybe parseable
            with path.open("ab") as f:
                f.write(rnd.choice([b'{"attempt_id":"a1"',
                                    b'{"attempt_id":"a2","phase":"result"}',
                                    b"\n", b"garbage\n", b"[1]\n"]))
        elif op == 4 and path.exists():   # compact-style atomic rewrite
            lines = path.read_bytes().split(b"\n")
            keep = [ln for ln in lines if rnd.random() < 0.6]
            paths.atomic_write(str(path), b"\n".join(keep),
                               tmp_prefix=".journal-", mode=0o600)
        elif op == 5 and path.exists():   # same name, new longer file
            data = path.read_bytes()
            path.unlink()
            path.write_bytes(data[: len(data) // 2] + b"\n" + data)
        elif op == 6 and path.exists():
            with path.open("r+b") as f:
                f.truncate(rnd.randrange(os.path.getsize(path) + 1))
        elif op == 7 and path.exists():
            path.unlink()
        if rnd.random() < 0.5:
            view = cache.refresh()
            _same_as_full_scan(view, tmp_path)
            held.append((view, journal.scan(str(tmp_path))))
    _same_as_full_scan(cache.refresh(), tmp_path)
    # a view held across later refreshes stays the scan it was taken as
    for view, full in held:
        assert list(view) == list(full)
        assert {aid: view[aid] for aid in view} == full


def test_scan_cache_rereads_a_replaced_file_and_falls_back_on_oserror(
        tmp_path):
    """A same-length rewrite that keeps the last line in place is only
    visible as a new inode; an unreadable file drops to a full scan."""
    import os
    cache = journal.ScanCache(str(tmp_path))
    path = tmp_path / "journal-w0.jsonl"
    path.write_bytes(b'{"attempt_id":"a1"}\n{"attempt_id":"zz"}\n')
    _same_as_full_scan(cache.refresh(), tmp_path)
    paths.atomic_write(str(path),
                       b'{"attempt_id":"b2"}\n{"attempt_id":"zz"}\n'
                       b'{"attempt_id":"c3"}\n', tmp_prefix=".journal-")
    _same_as_full_scan(cache.refresh(), tmp_path)
    other = tmp_path / "journal-w1.jsonl"
    other.write_bytes(b'{"attempt_id":"d4"}\n')
    os.chmod(other, 0)
    try:
        _same_as_full_scan(cache.refresh(), tmp_path)
    finally:
        os.chmod(other, 0o600)
    _same_as_full_scan(cache.refresh(), tmp_path)


def test_part_dedupe_view_matches_full_scan_through_crash_and_compaction(
        tmp_path, monkeypatch):
    """The records every _drive_parts decision reads, and a shadow cache
    refreshed after every journal append, equal a fresh full scan —
    across a mid-card crash, a started-only part, a restart, a torn and
    tainted journal, compaction behind the cache's back and in-worker
    compaction. Exactly-once holds: no part attempt is ever started
    twice and the unknown part stays unsent."""
    import time as _time
    from hermes_plugin.mcs_delivery import worker as worker_mod
    state = _state(tmp_path)
    w, reg, bot = _mkworker(tmp_path)
    shadow = journal.ScanCache(str(state))
    seen = {"drive": 0, "append": 0}
    starts = {}           # attempt_id -> started rows ever appended
    real_append = journal.append

    def append(*a, **kw):
        out = real_append(*a, **kw)
        _same_as_full_scan(shadow.refresh(), state)
        seen["append"] += 1
        if a[2]["phase"] == "started":
            starts[a[2]["attempt_id"]] = \
                starts.get(a[2]["attempt_id"], 0) + 1
        return out

    real_drive = DeliveryWorker._drive_parts

    async def drive(self, claim, manifest, ctx, records):
        _same_as_full_scan(records, state)
        seen["drive"] += 1
        return await real_drive(self, claim, manifest, ctx, records)

    monkeypatch.setattr(journal, "append", append)
    monkeypatch.setattr(DeliveryWorker, "_drive_parts", drive)
    monkeypatch.setattr(worker_mod, "JOURNAL_SEGMENT_BYTES", 4096)
    specs = {i: dict(_spec(_chunks(4)), delivery_id=_did(i))
             for i in range(1, 7)}

    def deliver(worker, i):
        asyncio.run(worker._deliver_parts(_claim(specs[i]), "9001"))

    def aid(i, part):
        return envelopes.part_attempt_id(_did(i), part)

    # a foreign closed file with settled rows compaction may prune, and a
    # tainted one (corrupt middle line) it must leave alone
    old = _time.time() - 5 * 86400
    _settled(state, "w8", "c1" * 8, _did(90), old)
    _settled(state, "w8", "c2" * 8, _did(91), old)
    _settled(state, "w0", "c3" * 8, _did(92), old)
    with (state / "journal-w0.jsonl").open("ab") as f:
        f.write(b"garbage\n")
    _settled(state, "w0", "c4" * 8, _did(93), old)

    deliver(w, 1)
    # crash between parts: body:0003 never begins
    orig = w._attempt_part

    async def crash(claim, part, ctx):
        if part["part_id"] == "body:0003":
            raise RuntimeError("simulated worker crash")
        return await orig(claim, part, ctx)
    w._attempt_part = crash
    with pytest.raises(RuntimeError):
        deliver(w, 2)
    w._attempt_part = orig
    # interruption past the wire: body:0002 started, no result
    orig_perform = w._perform_part

    async def cancel(claim, part, ctx):
        if part["part_id"] == "body:0002":
            raise asyncio.CancelledError
        return await orig_perform(claim, part, ctx)
    w._perform_part = cancel
    with pytest.raises(asyncio.CancelledError):
        deliver(w, 3)
    w._perform_part = orig_perform
    # torn but parseable tail row: the full scan counts it, so must we
    with (state / "journal-w0.jsonl").open("ab") as f:
        f.write(json.dumps({"phase": "result", "attempt_id":
                            aid(2, "body:0004"), "delivery_id": _did(2),
                            "part_id": "body:0004", "result": "delivered",
                            "remote_id": "1"}).encode())
    deliver(w, 4)
    # compaction behind the cache's back (no invalidate): w8 rewritten
    journal.compact(str(state), active="", file_ok=lambda rows: True,
                    prunable=lambda a, rows: a == "c1" * 8)
    deliver(w, 5)
    with (state / "journal-w0.jsonl").open("ab") as f:
        f.write(b"\n{torn")

    # restart: a fresh worker resumes the interrupted cards
    w2, _reg2, _ = _mkworker(tmp_path, bot=bot, wid="w2")
    deliver(w2, 2)
    deliver(w2, 3)
    deliver(w2, 2)                  # repeated resume: nothing left to do
    # in-worker rotation + compaction of every settled closed file (a
    # parts_done delivery is never resumed again, so its rows may go)
    _backup(tmp_path, _time.time() + 3 * 86400)
    for i in (1, 2, 3, 4, 5):
        reg.put_parts_done(_did(i))
    asyncio.run(w.maintain_journal())
    assert w._segment >= 1
    deliver(w, 6)                   # the old worker's cache, stale state

    assert seen["drive"] == 9 and seen["append"] > 50
    _same_as_full_scan(w._jview.refresh(), state)
    rows = journal.scan(str(state))
    assert "c1" * 8 not in rows and "c3" * 8 in rows
    assert "c2" * 8 not in rows              # in-worker compaction ran
    assert starts and set(starts.values()) == {1}      # never twice
    assert aid(2, "body:0004") not in starts     # proven by the tail row
    unknown = rows[aid(3, "body:0002")]
    assert {r["phase"] for r in unknown} == {"started"}  # never resent
    for i in (2, 3):
        assert aid(i, "body:0003") in starts          # resumed remainder


def test_tick_journal_reads_match_full_scan_across_rotation(
        tmp_path, monkeypatch):
    """_spent_deliveries, _started, _resume_dead and the _resume_parts
    fallback read the incremental view; at each call it equals a fresh
    full scan and they decide exactly as a full scan would — across a
    peer's appends, a torn tail, compaction behind the cache's back,
    in-worker rotation + compaction and a deleted segment."""
    import time as _time
    from hermes_plugin.mcs_delivery import worker as worker_mod
    state = _state(tmp_path)
    w, reg, _bot = _mkworker(tmp_path)
    monkeypatch.setattr(worker_mod, "JOURNAL_SEGMENT_BYTES", 2048)
    views = []
    real_refresh = w._jview.refresh

    def refresh():
        view = real_refresh()
        _same_as_full_scan(view, state)
        views.append((view, journal.scan(str(state))))
        return view
    w._jview.refresh = refresh
    scans = []
    real_scan = journal.scan

    def scan(d):
        # only the worker's own full scans count, not this test's checks
        if sys._getframe(1).f_code.co_filename == worker_mod.__file__:
            scans.append(d)
        return real_scan(d)
    monkeypatch.setattr(journal, "scan", scan)

    def full_spent():
        return {str(r.get("delivery_id"))
                for rows in real_scan(str(state)).values() for r in rows
                if r.get("phase") in ("started", "result")
                and not r.get("part_id")}

    def full_started(aid):
        return any(r.get("phase") == "started"
                   for r in real_scan(str(state)).get(aid, []))

    def check(i):
        assert w._spent_deliveries() == full_spent()
        for aid in ("ab" * 8, f"s{i}"):
            assert w._started({"attempt_id": aid}) == full_started(aid)
        spec = dict(_spec(_chunks(2)), delivery_id=_did(i))
        journal.append(str(state), "w0", {
            "phase": "result", "attempt_id": f"card{i}",
            "delivery_id": _did(i), "result": "delivered",
            "message_id": "9001"})
        asyncio.run(w._resume_dead([spec]) if i % 2
                    else w._resume_parts(spec))
        assert reg.parts_done(_did(i))
        assert set(_sent_parts(state, _did(i))) \
            == {"thread", "body:0001", "body:0002"}

    old = _time.time() - 5 * 86400
    _settled(state, "w8", "c1" * 8, _did(90), old)
    _settled(state, "w8", "c2" * 8, _did(91), old)
    check(1)
    journal.append(str(state), "w9", {"phase": "started",
                                      "attempt_id": "s2",
                                      "delivery_id": _did(80)})
    with (state / "journal-w9.jsonl").open("ab") as f:
        f.write(json.dumps({"phase": "started", "attempt_id": "s3",
                            "delivery_id": _did(81)}).encode())
    check(2)                  # torn but parseable tail counts
    with (state / "journal-w9.jsonl").open("ab") as f:
        f.write(b"\n")
    journal.compact(str(state), active="", file_ok=lambda rows: True,
                    prunable=lambda a, rows: a == "c1" * 8)
    check(3)                  # compacted behind the cache's back
    _backup(tmp_path, _time.time() + 3 * 86400)
    for i in (1, 2, 3):
        reg.put_parts_done(_did(i))
    asyncio.run(w.maintain_journal())
    assert w._segment >= 1
    check(4)                  # rotated + compacted in-worker
    (state / "journal-w9.jsonl").unlink()
    check(5)
    assert "c2" * 8 not in real_scan(str(state))
    assert scans == [] and len(views) >= 15
    # every view handed out is still the scan it was taken as — the
    # resume loop holds one while its own sends append rows
    for view, full in views:
        assert {aid: view[aid] for aid in view} == full


def _count_journal_reads(monkeypatch):
    """Byte counts of every read the journal module makes — a full
    scan's line iteration and ScanCache's reads alike."""
    import builtins
    got = []

    class Counted:
        def __init__(self, handle):
            self._h = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._h.close()

        def __getattr__(self, name):
            return getattr(self._h, name)

        def __iter__(self):
            for line in self._h:
                got.append(len(line))
                yield line

        def read(self, *a):
            data = self._h.read(*a)
            got.append(len(data))
            return data

    def counted_open(path, mode="r", *a, **kw):
        handle = builtins.open(path, mode, *a, **kw)
        return Counted(handle) if mode == "rb" else handle

    monkeypatch.setattr(journal, "open", counted_open, raising=False)
    return got


def test_part_dedupe_reads_only_appended_journal_bytes(tmp_path,
                                                       monkeypatch):
    """Per-card part dedupe costs O(new rows), not O(journal): no full
    scan, and the bytes read over n cards stay within ~2x the journal."""
    state = _state(tmp_path)
    w, _reg, _ = _mkworker(tmp_path)
    scans = []
    real_scan = journal.scan
    monkeypatch.setattr(journal, "scan",
                        lambda d: scans.append(d) or real_scan(d))
    got = _count_journal_reads(monkeypatch)
    per_card = []
    for i in range(1, 41):
        got.clear()
        asyncio.run(w._deliver_parts(
            _claim(dict(_spec(_chunks(3)), delivery_id=_did(i))), "9001"))
        per_card.append(sum(got))
    size = sum(p.stat().st_size for p in state.glob("journal-*.jsonl"))
    assert scans == []
    assert sum(per_card) <= 2 * size
    # constant per card: the 40th card reads no more than the 2nd did
    assert per_card[-1] <= 2 * per_card[1]


def test_tick_journal_reads_do_not_grow_with_the_journal(tmp_path,
                                                         monkeypatch):
    """A tick that claims a fresh spec (spent check), resumes a dead
    spec's parts and settles a withdrawn granted claim (started check)
    reads only the journal bytes appended since the last tick — not the
    whole journal again, however large it has grown."""
    import time as _time
    state = _state(tmp_path)
    w, reg, _ = _mkworker(tmp_path)
    for i in range(300):            # a large settled history
        _settled(state, "w8", f"h{i:07d}", _did(10_000 + i), _time.time())
    size = sum(p.stat().st_size for p in state.glob("journal-*.jsonl"))
    got = _count_journal_reads(monkeypatch)
    per_tick = []
    for i in range(1, 13):
        _publish_spec(tmp_path, _sealed(dict(_spec(_chunks(1)),
                                             delivery_id=_did(i))))
        dead = dict(_spec(_chunks(1)), delivery_id=_did(500 + i))
        _publish_spec(tmp_path, _sealed(dead))
        reg.mark_dead(dead["delivery_id"])
        journal.append(str(state), "w0", {
            "phase": "result", "attempt_id": f"card{i}",
            "delivery_id": dead["delivery_id"], "result": "delivered",
            "message_id": "9001"})
        gone = dict(_spec(_chunks(1)), delivery_id=_did(900 + i))
        reg.claim(gone["delivery_id"], {
            "attempt_id": f"g{i}", "worker_id": "w1", "spec": gone,
            "payload_hash": envelopes.payload_hash(gone),
            "spec_path": str(tmp_path / "gone.json"), "phase": "granted"})
        got.clear()
        asyncio.run(w.tick())
        per_tick.append(sum(got))
        assert reg.claimed(_did(i)) and reg.parts_done(dead["delivery_id"])
        assert not reg.claimed(gone["delivery_id"])
    # the first tick reads the history once; later ticks only new rows
    assert per_tick[0] >= size
    assert max(per_tick[1:]) < size / 10
    assert per_tick[-1] <= 2 * per_tick[1]
