"""Synthetic mcs discord actions contracts with isolated shared fixtures."""
from __future__ import annotations
import self_identity
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from adapters.common import envelopes, paths, text
from adapters.discord import actions as actions_mod
from notify_testkit import _add_request, _llm_extract
from discord_testkit import CFG, MISSING, SETTINGS, FakeHTTP, FakeInteraction, _deliver, _request
from discord_testkit import discord_world as discord_world
from mcs_discord_testkit import _delivered as _delivered, _delivered_source_signal as _delivered_source_signal, _drive_to_confirm as _drive_to_confirm, _pin_wall_clock as _pin_wall_clock


# ---------- interactions ---------------------------------------------------

def test_action_ack_round_trip(world):
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
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
    worker, reg, bot, spec = _delivered(world)
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
    worker, reg, bot, spec = _delivered(world)
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
    worker, reg, bot, spec = _delivered(world)
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
    # user and channel IDs stay out of the operational log
    assert denials[0] == {"reason": "chat_not_allowed"}


def test_confirm_preview_text_per_transport():
    req = {"title": "件" * 250, "reason": "理" * 450,
           "assignee": "担当者A", "due_date": "2026-10-01"}
    tail = (f"内容: {'件' * 250}\n担当: 担当者A\n期限: 2026-10-01\n"
            f"理由: {'理' * 450}\n"
            "確定すると、この投稿にタスクを作成し、あなたの名前と理由を記録します。\n"
            "この確認は10分で無効になります。")
    assert text.preview_text("request", req, True) \
        == "**確認 — タスク作成**\n" + tail
    assert text.preview_text("request", req, False) \
        == "確認 — タスク作成\n" + tail
    assert text.preview_text(
        "request", {"title": "t", "reason": "r"}, False) \
        == ("確認 — タスク作成\n内容: t\n理由: r\n"
            "確定すると、この投稿にタスクを作成し、あなたの名前と理由を記録します。\n"
            "この確認は10分で無効になります。")
    report = {"field": "meds", "reason": "用量が違う"}
    assert text.preview_text("report", report, False).startswith(
        "確認 — 抽出の誤り報告\n箇所: 薬\nメモ: 用量が違う")
    dismiss = {"signal_key": "sig:1", "reason": "r"}
    assert text.preview_text("dismiss", dismiss, True) \
        == ("**確認 — 候補の却下**\n理由: r\n"
            "確定すると、この候補を却下済みにし、あなたの名前と理由を記録します。\n"
            "この確認は10分で無効になります。")
    assert text.preview_text("dismiss", dismiss, False) \
        == ("確認 — 候補の却下\n理由: r\n"
            "確定すると、この候補を却下済みにし、あなたの名前と理由を記録します。\n"
            "この確認は10分で無効になります。")


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


@pytest.mark.parametrize("title, reason", [
    ("服薬確認", "フォロー要"),
    ("合" * 996 + "内容末尾", "理" * 1996 + "理由末尾"),
])
def test_request_modal_full_flow(world, title, reason):
    """request click -> modal -> submit -> preview -> confirm ->
    request.create lands as a human-confirmed command."""
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
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
            {"custom_id": "title", "value": title},
            {"custom_id": "reason", "value": reason},
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
    assert pend["payload"]["title"] == title and pend["payload"]["reason"] == reason
    shown = "".join(part["content"] for part in submit.followup.sent)
    assert title in shown and reason in shown

    # 3. confirm -> the pinned payload is enqueued verbatim
    confirm = FakeInteraction(cid, message_id=msg.id)
    asyncio.run(world.interact(act, confirm))
    rows = world.led.db.execute(
        "SELECT project_id,source_message_id,title,status "
        "FROM requests").fetchall()
    assert len(rows) == 1
    assert rows[0]["source_message_id"] == 100
    assert rows[0]["title"] == title


def test_modal_wrong_actor_and_origin(world):
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
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
    row = json.loads(world.led.db.execute(
        "SELECT content FROM artifacts WHERE kind='signal_v1' "
        "ORDER BY artifact_id DESC LIMIT 1").fetchone()[0])
    # the seeded signal row always exists; the dismissal must have landed
    assert row["state"] == "dismissed"


# ---------- D3: authorization depth (RC01) ---------------------------------

def test_action_denies_wrong_channel_and_project(world):
    """allowed_user_ids alone is not the gate — a click from an
    unlisted channel or for an unlisted project dies before any file
    is written, even though the adapter's own auth may allow-all."""
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
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


def test_confirm_wrong_actor_and_replay(world):
    """Only the preview's own actor may confirm; once consumed the
    confirm id is dead — a replayed click reports expiry."""
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
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
    worker, reg, bot, spec = _delivered(world)
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
    worker, reg, bot, spec = _delivered(world)
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
    worker, reg, bot, spec = _delivered(world)
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


def test_confirm_publish_error_after_the_file_landed_keeps_the_confirm(
        world, monkeypatch):
    """An OSError after os.replace (the directory fsync) is not a send
    failure: the command is queued, so the confirm stays in flight (a
    cancel reads as busy) and the followup still reports the receipt."""
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
    tok = world.token(spec, "request")
    act = world.mkactions(reg, bot)
    msg = bot.channels[42].sent[0]
    cid = _drive_to_confirm(world, act, tok, msg)

    real = paths.fsync_dir

    def fail(d):
        if os.path.samefile(d, world.data / "cmd_int"):
            raise OSError("synthetic fsync failure")
        real(d)
    monkeypatch.setattr(paths, "fsync_dir", fail)
    ok = FakeInteraction(cid, message_id=msg.id)
    asyncio.run(act.on_interaction(ok))
    monkeypatch.setattr(paths, "fsync_dir", real)
    assert not any("送信に失敗" in m["content"] for m in ok.followup.sent)
    assert "受付結果を確認できませんでした" in ok.followup.sent[-1]["content"]
    assert reg.confirm(cid[len("mcs:c:"):]).get("in_flight")
    human = [json.loads(p.read_text())
             for p in (world.data / "cmd_int").glob("*.json")
             if json.loads(p.read_text()).get("human_confirmed")]
    assert len(human) == 1
    assert reg.followup(human[0]["command_id"]) is not None
    cancel = FakeInteraction(cid + ":cancel", message_id=msg.id)
    asyncio.run(act.on_interaction(cancel))
    assert "処理中" in cancel.response.message["content"]


def test_confirm_expiring_before_take_never_publishes(world):
    """The TTL may lapse between the confirm lookup and taking it — the
    take must answer expiry, not queue the command from the stale
    lookup (the old begin_confirm result was ignored)."""
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
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
    worker, reg, bot, spec = _delivered(world)
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


def test_unknown_token_refresh_publish_failure_is_reported(world, monkeypatch):
    """A refresh that never reached the inbox must not promise a card
    update — the user sees the send failure and the journal records it."""
    world.seed()
    world.dispatch()
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    msg = bot.channels[42].sent[0]
    act = world.mkactions(reg, bot)

    def fail(*_a):
        raise OSError("disk full")
    monkeypatch.setattr(envelopes, "publish_command", fail)
    ix = FakeInteraction("mcs:a:" + "ff" * 16, message_id=msg.id)
    asyncio.run(act.on_interaction(ix))
    assert ix.response.message["ephemeral"] is True
    assert "送信に失敗" in ix.response.message["content"]
    assert "更新します" not in ix.response.message["content"]
    outcomes = [f["outcome"] for e, f in world.logs
                if e == "interaction_result" and f["action"] == "refresh"]
    assert outcomes == ["refresh_publish_failed"]


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
    worker, reg, bot, spec = _delivered(world)
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


# ---------- task list + transitions ----------------------------------------

def test_action_tasks_ephemeral_list_and_transition(world):
    """📋 answers with an ephemeral list; a transition button on it
    applies through the same token pipeline even though its origin
    message is the followup, not the card."""
    world.seed()
    _request(world)
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
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
    worker, reg, bot, spec = _delivered(world)
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
    worker, reg, bot, spec = _delivered(world)
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


def test_confirm_out_of_scope_after_preview_is_denied_but_cancellable(world):
    """Project scope gates only 確定: a project leaving scope after the
    preview answers 権限がありません without taking the confirm, while
    the actor's own 取消 still drops it. An actor who lost user/channel
    authorization can do neither."""
    world.seed()
    world.dispatch()
    worker, reg, bot, spec = _delivered(world)
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
    world.seed()
    _llm_extract(world.led, 100,
                 {"requests": [{"action": "残薬を確認", "to": None}]})
    with world.led.db:
        self_identity.record_station_staff(world.led.db, [
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
    assert preview["content"].startswith("**確認 — タスク作成**\n対象: 患者A · 09-24 08:40の投稿\n内容: 残薬を確認")
    assert "\n理由: 通知カードからタスク作成\n確定すると" in preview["content"]
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
        "summary", "urgency", "meds", "symptoms", "requests", "vitals",
            "patient_context", "labs", "events", "other"]
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


# ---------- 📋 / 🗂 / 🔎 / 🚫 reason code ------------------------------------

def test_my_tasks_uses_display_name_and_project_scope(world):
    world.seed()
    world.signal("synthetic-digest", mids=[100])
    world.dispatch(kind="signal", pid=None, payload={
        "digest": True, "signal_keys": ["synthetic-digest"]})
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
    world.signal("synthetic-digest", mids=[100])
    world.dispatch(kind="signal", pid=None, payload={
        "digest": True, "signal_keys": ["synthetic-digest"]})
    digest = json.loads(world.led.db.execute(
        "SELECT r.spec_json FROM notification_renders r JOIN notification_cards c "
        "USING(card_id) WHERE c.kind='digest' ORDER BY r.render_rev DESC LIMIT 1").fetchone()[0])
    asyncio.run(_deliver(world, worker))
    act = world.mkactions(reg, bot)
    ix = FakeInteraction(f"mcs:a:{world.token(digest, 'unacked')}",
                         message_id=bot.channels[42].sent[-1].id)
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
    assert "「本文」の検索結果" in out and "2件（取得済み原資料・新しい投稿順）" in out
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


@pytest.mark.parametrize("fill", ["合", "🩺"])
def test_confirmation_preview_keeps_full_payload_with_last_only_buttons(world, fill):
    _, reg, bot = world.mkworker()
    act = world.mkactions(reg, bot)
    payload = {"title": fill * 996 + "内容末尾", "reason": fill * 1996 + "理由末尾",
               "assignee": "合" * 116 + "担当末尾", "due_date": "2026-10-18"}
    ix = FakeInteraction("synthetic-preview")
    asyncio.run(act._send_preview(ix, "request", payload, "synthetic-confirm", target="合成患者・合成投稿"))
    sent = ix.followup.sent
    assert len(sent) > 1
    assert all(len(m["content"]) <= 2000 and m["ephemeral"] and not m["pings"] for m in sent)
    assert all(m["view"] is MISSING for m in sent[:-1])
    assert [b.label for b in sent[-1]["view"].items] == ["確定する", "取消"]
    for value in payload.values():
        assert value in "".join(m["content"] for m in sent)


@pytest.mark.parametrize("fail_at", [1, 2])
def test_confirmation_preview_stops_before_buttons_after_followup_failure(world, fail_at):
    _, reg, bot = world.mkworker()
    act = world.mkactions(reg, bot)
    ix = FakeInteraction("synthetic-preview")
    send = ix.followup.send
    attempts = 0
    async def fail(content, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == fail_at:
            raise RuntimeError("synthetic preview failure")
        await send(content, **kwargs)
    ix.followup.send = fail
    payload = {"title": "合" * 1000, "reason": "🩺" * 2000}
    asyncio.run(act._send_preview(ix, "request", payload, "synthetic-confirm"))
    assert attempts == fail_at
    assert all(m["view"] is MISSING for m in ix.followup.sent)
    assert any(event == "followup_failed" for event, _ in world.logs)


def test_confirmation_preview_continuation_cannot_start_active_markdown(world):
    _, reg, bot = world.mkworker()
    act = world.mkactions(reg, bot)
    ix = FakeInteraction("synthetic-preview")
    # The field's # is not originally line-leading, but a hard wrap makes it so.
    reason = "合" * (text.BODY_CHUNK - len("理由: ")) + "# 合成見出し <@1001>"
    payload = {"title": "合成タスク", "reason": reason}
    asyncio.run(act._send_preview(ix, "request", payload, "synthetic-confirm"))
    assert len(ix.followup.sent) > 1
    assert all(not message["content"].startswith("#") for message in ix.followup.sent)
    assert "＃ 合成見出し" in "".join(message["content"] for message in ix.followup.sent)
    assert all(not message["pings"] for message in ix.followup.sent)
