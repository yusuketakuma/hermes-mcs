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
sys.modules.setdefault("discord", _discord)


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


class FakeBot:
    def __init__(self, channel_id=42):
        self.channels = {channel_id: FakeChannel(channel_id)}
        self.user = BOT_USER

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
