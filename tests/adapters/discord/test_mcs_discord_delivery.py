"""Synthetic mcs discord delivery contracts with isolated shared fixtures."""
from __future__ import annotations
import asyncio
import json
import types
import pytest
from adapters.common import envelopes, journal
from adapters.common.registry import Registry, scope_key
from adapters.discord.delivery import DeliveryWorker
from discord_delivery_testkit import DELIVERY_ID, FakeHTTP, FakeThread, _chunks, _claim, _envelope, _HistMsg, _mkworker, _receipts, _sent_parts, _settled, _spec, _state
from discord_delivery_testkit import synthetic_discord_sdk as synthetic_discord_sdk
from mcs_discord_delivery_testkit import REVOKED_ID as REVOKED_ID, _attach_update as _attach_update, _backup as _backup, _begins as _begins, _card_delivered as _card_delivered, _card_spec as _card_spec, _clock as _clock, _did as _did, _journal_rows as _journal_rows, _part_ctx as _part_ctx, _publish_spec as _publish_spec, _same_as_full_scan as _same_as_full_scan, _sealed as _sealed, _spec_path as _spec_path, _write_begin_result as _write_begin_result, bot_sent_parts as bot_sent_parts


# ---------- part engine -----------------------------------------------------

def test_seeded_card_and_new_messages_keep_unique_ids_after_worker_restart(tmp_path):
    _, _, bot = _mkworker(tmp_path)
    channel = bot.channels[42]

    async def run():
        card = await channel.fetch_message(9001)
        first = await channel.send()
        _mkworker(tmp_path, bot=bot, wid="w2")
        second = await channel.send()
        assert len({card.id, first.id, second.id}) == 3
        for message in (card, first, second):
            assert await channel.fetch_message(message.id) is message

    asyncio.run(run())


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

    import adapters.common.worker as wm
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
    flags.write_text(json.dumps({"interactive": True, "route_epoch": 1}))
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


def test_update_reuses_the_post_that_already_carries_the_file(tmp_path):
    """Every update render re-plans the thread's attachments; the file
    already in the thread is bound again, not uploaded a second time."""
    w, th, spec = _attach_update(tmp_path, None)
    th.sent.append(_HistMsg(6100, None, attachments=["att.bin"]))
    spec["parts"]["manifest"][-1]["prior_remote_id"] = "6100"
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert len(th.sent) == 1                     # nothing uploaded
    row = _sent_parts(_state(tmp_path))["attach:0007"]
    assert row["result"] == "delivered" and row["remote_id"] == "6100"


def test_unknown_prior_lookup_never_uploads_a_second_copy(tmp_path):
    w, th, spec = _attach_update(tmp_path, "6100")
    th.sent.append(_HistMsg(6100, None, attachments=["att.bin"]))
    th.fetch_fail = FakeHTTP(500)                # may still be there
    asyncio.run(w._deliver_parts(_claim(spec), "9001"))
    assert len(th.sent) == 1
    assert _sent_parts(_state(tmp_path))["attach:0007"]["result"] \
        == "unknown"


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


def test_dead_tombstone_outlives_ttl_while_spec_is_published(
        tmp_path, monkeypatch):
    """An expired tombstone for a still-published spec would re-claim it
    daily; one whose spec is gone expires with its part progress."""
    from adapters.common import registry as registry_mod
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


def test_registry_expire_runs_once_per_interval(tmp_path, monkeypatch):
    """The O(registry) TTL sweep runs on the first tick, then at most
    once per EXPIRE_EVERY_S — not on every 2s poll."""
    from adapters.common import worker as worker_mod
    clock = [1000.0]
    monkeypatch.setattr(worker_mod, "time", types.SimpleNamespace(
        time=lambda: clock[0], monotonic=lambda: clock[0]))
    w, reg, _ = _mkworker(tmp_path)
    calls = []
    real = reg.expire
    monkeypatch.setattr(reg, "expire",
                        lambda **kw: calls.append(kw) or real(**kw))
    for _ in range(3):
        asyncio.run(w.tick())
        clock[0] += worker_mod.POLL_S
    assert len(calls) == 1
    clock[0] = 1000.0 + worker_mod.EXPIRE_EVERY_S
    asyncio.run(w.tick())
    assert len(calls) == 2


def test_live_segment_rotates_and_closed_segment_is_compacted(
        tmp_path, monkeypatch):
    import time as _time
    from adapters.common import worker as worker_mod
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
     "unsupported_parts_key:future_poll"),
    (lambda s: s["parts"]["manifest"][1].__setitem__("future_flag", 1),
     "unsupported_part_key:future_flag"),
    (lambda s: s["delivery"].__setitem__("future_route", "x"),
     "unsupported_delivery_key:future_route"),
    (lambda s: s.__setitem__("future_top", 1), "unsupported_spec_key:future_top"),
])
def test_outdated_worker_holds_spec_with_unknown_feature(tmp_path, mutate,
                                                         reason):
    """A worker that was not restarted after a runner upgrade must not
    send the card and silently drop a feature it does not understand —
    the spec is rejected whole, nothing is claimed or sent, and the
    rejection is logged once instead of every tick."""
    from adapters.common import spec as spec_mod
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


@pytest.mark.parametrize("part_id,prior", [
    ("thread", "6001"),          # only a body chunk can be rewritten
    ("body:0001", ""),           # …and it names the earlier post
    ("body:0001", 6001),         # …by its transport id, which is text
    ("body:0001", "6" * 65),
])
def test_prior_remote_id_is_valid_only_as_a_body_chunk_post_id(part_id,
                                                               prior):
    from adapters.common import spec as spec_mod
    spec_mod.validate(_sealed(_spec(_chunks(2),
                                    prior={"body:0002": "6001"})))
    bad = _sealed(_spec(_chunks(2)))
    next(p for p in bad["parts"]["manifest"]
         if p["part_id"] == part_id)["prior_remote_id"] = prior
    with pytest.raises(ValueError, match="bad_prior_remote_id"):
        spec_mod.validate(bad)


def test_signal_notify_off_stays_claimable_and_revoked_stays_dead(
        tmp_path, monkeypatch):
    """denied_signal_notify_off drops the claim without a dead tombstone.

    A worker restart between OFF and ON reloads the registry and still
    claims the same spec; the grant then records a real send. A
    non-transient denial (denied_card_revoked) still tombstones.
    """
    from adapters.discord import cards as cards_mod
    from adapters.common import spec as spec_mod

    monkeypatch.setattr(cards_mod, "message_payload", lambda spec, **kw: {"view": object(), "content": "card"})
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


def test_signal_notify_off_backs_off_before_rebegin(tmp_path, monkeypatch):
    """A signal_notify_off denial holds the spec for SIGNAL_OFF_RETRY_S —
    no re-claim churn (attempt row + result file + journal per tick)."""
    from adapters.common import worker as worker_mod
    from adapters.discord import cards as cards_mod

    monkeypatch.setattr(cards_mod, "message_payload", lambda spec, **kw: {"view": object(), "content": "card"})
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
    from adapters.discord import cards as cards_mod

    monkeypatch.setattr(cards_mod, "message_payload", lambda spec, **kw: {"view": object(), "content": "card"})
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


def test_part_dedupe_view_matches_full_scan_through_crash_and_compaction(
        tmp_path, monkeypatch):
    """The records every _drive_parts decision reads, and a shadow cache
    refreshed after every journal append, equal a fresh full scan —
    across a mid-card crash, a started-only part, a restart, a torn and
    tainted journal, compaction behind the cache's back and in-worker
    compaction. Exactly-once holds: no part attempt is ever started
    twice and the unknown part stays unsent."""
    import time as _time
    from adapters.common import worker as worker_mod
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
    # compaction behind the cache's back (no invalidate): the tainted
    # w0 file aborts the whole pass, so w8 is not rewritten either
    before_bytes = {p.name: p.read_bytes()
                    for p in state.glob("journal-*.jsonl")}
    assert journal.compact(str(state), active="", file_ok=lambda rows: True,
                           prunable=lambda a, rows: a == "c1" * 8) == 0
    assert {p.name: p.read_bytes()
            for p in state.glob("journal-*.jsonl")} == before_bytes
    deliver(w, 5)
    with (state / "journal-w0.jsonl").open("ab") as f:
        f.write(b"\n{torn")

    # restart: a fresh worker resumes the interrupted cards
    w2, _reg2, _ = _mkworker(tmp_path, bot=bot, wid="w2")
    deliver(w2, 2)
    deliver(w2, 3)
    deliver(w2, 2)                  # repeated resume: nothing left to do
    # in-worker rotation still runs, but compaction keeps every closed
    # file byte-for-byte while journal-w0 stays tainted
    _backup(tmp_path, _time.time() + 3 * 86400)
    for i in (1, 2, 3, 4, 5):
        reg.put_parts_done(_did(i))
    before_bytes = {p.name: p.read_bytes()
                    for p in state.glob("journal-*.jsonl")}
    asyncio.run(w.maintain_journal())
    assert w._segment >= 1
    assert {p.name: p.read_bytes()
            for p in state.glob("journal-*.jsonl")} == before_bytes
    deliver(w, 6)                   # the old worker's cache, stale state

    assert seen["drive"] == 9 and seen["append"] > 50
    _same_as_full_scan(w._jview.refresh(), state)
    rows = journal.scan(str(state))
    assert "c1" * 8 in rows and "c3" * 8 in rows
    assert "c2" * 8 in rows               # tainted file kept compaction off
    assert starts and set(starts.values()) == {1}      # never twice
    assert aid(2, "body:0004") not in starts     # proven by the tail row
    unknown = rows[aid(3, "body:0002")]
    assert {r["phase"] for r in unknown} == {"started"}  # never resent
    for i in (2, 3):
        assert aid(i, "body:0003") in starts          # resumed remainder


def test_thread_body_is_posted_as_literal_text(tmp_path):
    w, _reg, _bot = _mkworker(tmp_path)
    spec = _spec(["**太字** [x](https://e.invalid)\n# 見出し\n> 引用"])
    thread = FakeThread(7001)
    part = {"part_id": "body:1", "kind": "body_part", "index": 2}
    out = asyncio.run(w._perform_part(_claim(spec), part, _part_ctx(thread)))
    assert out["result"] == "delivered"
    assert thread.sent[0].content == (
        "＊＊太字＊＊ ［x］(https://e.invalid)\n＃ 見出し\n＞ 引用")


@pytest.mark.parametrize("op", ["update", "revoke"])
def test_ambiguous_prefetch_before_card_write_is_not_sent(tmp_path, op):
    """Regression: a 5xx/timeout on the GET before the edit/delete was
    'unknown' — manual reconcile and held bodies though nothing was sent."""
    w, reg, bot = _mkworker(tmp_path)
    ch = bot.channels[42]

    async def flaky_fetch(_mid):
        raise FakeHTTP(503)

    ch.fetch_message = flaky_fetch
    spec = _spec([], op=op)
    spec["delivery"]["message_id"] = "9001"
    out = asyncio.run(w._perform(_claim(spec)))
    assert out == {"result": "not_sent", "error_code": "prefetch_failed"}


def test_ambiguous_channel_fetch_before_thread_create_is_not_sent(tmp_path):
    w, reg, bot = _mkworker(tmp_path)
    channel = bot.channels.pop(42)

    async def timeout(_cid):
        raise TimeoutError("synthetic")

    bot.fetch_channel = timeout
    asyncio.run(w._deliver_parts(_claim(_spec(_chunks(1))), "9001"))
    row = _sent_parts(_state(tmp_path))["thread"]
    assert row["result"] == "not_sent" and row["error_code"] == "prefetch_failed"
    assert channel.threads == []


def test_discord_file_group_members_and_anchor_get_no_wire_attempt(tmp_path):
    """Grouped Discord members ride their surface's HTTP call; the worker
    must not open independent attempts for them or the group anchor."""
    import hashlib
    chunk = _chunks(1)[0]
    manifest = [
        {"part_id": "card", "kind": "card", "index": 0},
        {"part_id": "thread", "kind": "thread", "index": 1,
         "name": "テスト スレッド",
         "sha256": hashlib.sha256("テスト スレッド".encode()).hexdigest()},
        {"part_id": "body:0001", "kind": "body_part", "index": 2,
         "sha256": hashlib.sha256(chunk.encode()).hexdigest(),
         "bytes": len(chunk.encode())},
        {"part_id": "attach:0001", "kind": "attachment_part", "index": 3,
         "sha256": "a" * 64, "bytes": 5, "name": "a.pdf",
         "attachment_id": 1},
        {"part_id": "file_group:body:0001", "kind": "file_group",
         "index": 4, "surface": "body:0001",
         "members": ["attach:0001"], "sha256": "b" * 64},
    ]
    w, reg, bot = _mkworker(tmp_path)
    spec = _spec(_chunks(1), manifest=manifest)
    claim = _claim(spec)

    async def run():
        await w._deliver_parts(claim, "9001")

    asyncio.run(run())
    parts = _sent_parts(_state(tmp_path))
    assert "attach:0001" not in parts
    assert "file_group:body:0001" not in parts
    envs = _receipts(tmp_path / "cmd_int")
    preceipts = {e["part_id"] for e in envs if e["op"] == "part_receipt"}
    assert "attach:0001" not in preceipts
    assert "file_group:body:0001" not in preceipts


def test_file_group_echo_rejects_extra_or_repeated_remote_files():
    from types import SimpleNamespace
    from adapters.discord.delivery import DeliveryWorker
    worker = object.__new__(DeliveryWorker)
    members = [{"part_id": "attach:0001", "name": "one.pdf", "bytes": 11},
               {"part_id": "attach:0002", "name": "two.pdf", "bytes": 22}]
    from adapters.common.spec import discord_file_name
    for member in members:
        member["sha256"] = "a" * 64
    first = SimpleNamespace(id=101, filename=discord_file_name(members[0]), size=11)
    second = SimpleNamespace(id=102, filename=discord_file_name(members[1]), size=22)
    message = SimpleNamespace(attachments=[first, second])
    assert len(worker._files_echo(message, members)) == 2
    message.attachments.append(SimpleNamespace(id=103, filename="extra.pdf", size=33))
    assert worker._files_echo(message, members) is None
    message.attachments.pop()
    second.id = first.id
    assert worker._files_echo(message, members) is None
