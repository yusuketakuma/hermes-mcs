"""Synthetic mcs discord testkit contracts with isolated shared fixtures."""
from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import notify_cards
from notify_testkit import _llm_extract
from discord_testkit import CFG, MISSING, NOW, FakeBot, FakeInteraction, _deliver
from discord_testkit import discord_world as discord_world


@pytest.fixture(autouse=True)
def _pin_wall_clock(monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)


# ---------- delivery -------------------------------------------------------

def _delivered_source_signal(world):
    """Prove the original thread through the fake SDK and deliver the signal there."""
    world.dispatch(payload={"message_ids": [100]})
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    source = world.card(1)
    assert source["delivery_state"] == "delivered" and source["thread_state"] == "created"
    thread = bot.channels[int(source["thread_id"])]
    thread.parent_id = 42
    thread.guild = SimpleNamespace(id=7)
    original_send = thread.send

    async def send(content=None, *, embed=None, view=MISSING, allowed_mentions=None):
        message = await original_send(content, view=view, allowed_mentions=allowed_mentions)
        message.embed = embed
        message.channel = thread
        return message

    thread.send = send  # SDK accepts card embeds and Components in threads.
    world.signal("sig-1", mids=[100])
    world.dispatch(kind="signal", pid=1, payload={
        "signal_keys": ["sig-1"], "project_id": 1, "type": "med_followup"})
    for _ in range(3):
        asyncio.run(_deliver(world, worker))
        ready = world.led.db.execute("SELECT delivery_state FROM notification_cards WHERE kind='signal'").fetchone()
        if ready[0] == "delivered":
            break
    render = world.led.db.execute("SELECT r.* FROM notification_renders r "
        "JOIN notification_cards c ON c.card_id=r.card_id WHERE c.kind='signal' "
        "ORDER BY r.render_rev DESC LIMIT 1").fetchone()
    spec = json.loads(render["spec_json"])
    signal = world.card(render["card_id"])
    assert signal["delivery_state"] == "delivered"
    assert signal["thread_id"] == source["thread_id"] and spec["parts"]["source_thread"] is True
    assert len(bot.channels[42].sent) == 1
    message = next(m for m in thread.messages if str(m.id) == signal["message_id"])
    return worker, reg, bot, spec, message, thread


class SignalBot(FakeBot):
    """FakeBot that signals listener wiring (set) and removal (clear), so
    supervisor tests wait on an event instead of polling a time budget.
    The supervisor wires its listener only after taking the scope lock."""

    def __init__(self):
        super().__init__()
        self.wired = asyncio.Event()

    def add_listener(self, fn, name):
        super().add_listener(fn, name)
        self.wired.set()

    def remove_listener(self, fn, name):
        super().remove_listener(fn, name)
        if not self.listeners:
            self.wired.clear()


# ---------- D3: confirm hardening (RC14) -------------------------------------

def _drive_to_confirm(world, act, tok, msg):
    """request click -> modal -> submit -> returns the confirm id."""
    ix = FakeInteraction(f"mcs:a:{tok}", message_id=msg.id)
    asyncio.run(act.on_interaction(ix))
    modal_id = ix.response.modal.custom_id[len("mcs:m:"):]
    submit = FakeInteraction(
        f"mcs:m:{modal_id}", message_id=msg.id,
        components=[{"components": [
            {"custom_id": "title", "value": "服薬確認"},
            {"custom_id": "reason", "value": "フォロー要"},
            {"custom_id": "assignee", "value": ""},
            {"custom_id": "due_date", "value": ""}]}])
    asyncio.run(world.interact(act, submit))
    preview = submit.followup.sent[-1]
    return next(b.custom_id for b in preview["view"].items
                if b.custom_id.startswith("mcs:c:")
                and not b.custom_id.endswith(":cancel"))


# ---------- card buttons: toggles, names, roles, task form, report --------

def _delivered(world):
    worker, reg, bot = world.mkworker()
    asyncio.run(_deliver(world, worker))
    _, spec = world.spec()
    return worker, reg, bot, spec


def _face(msg):
    if msg.embed is not None:
        return msg.embed.description
    return "\n".join(c.content for c in msg.view.items[0].children
                     if hasattr(c, "content"))


def _drug_thread(world, monkeypatch, *, medication_count=1, older_medication=False):
    import hashlib
    import drug_map
    from medications_testkit import DOCUMENT
    world.seed()
    medication = {"name": "キラナ", "dose": "5mg", "action": "start",
                  "subject": "patient", "status": "current", "negated": False,
                  "unverified": False, "evidence": "fictional quotation only"}
    _llm_extract(world.led, 101, {"meds": [
        {**medication, "name": "キラナ" if i == 0 else f"合成薬{i + 1}"}
        for i in range(medication_count)]})
    if older_medication:
        _llm_extract(world.led, 100, {"meds": [{**medication, "name": "旧投稿薬"}]})
    raw = json.dumps(DOCUMENT, ensure_ascii=False).encode()
    path = world.data / "fictional-drug-map.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    sha = hashlib.sha256(raw).hexdigest()
    drug_map.derive(world.led, drug_map.load(path, expected_sha256=sha))
    monkeypatch.setitem(CFG, "drug_map", {"path": str(path), "sha256": sha})
    world.dispatch()
    _, reg, bot, spec = _delivered(world)
    message = bot.channels[42].sent[0]
    thread = message.threads[0][1]
    body = thread.messages[0]
    assert spec["parts"]["thread_drug_actions"] is True
    return world.mkactions(reg, bot), reg, bot, spec, thread, body
