"""Slack wire budgets preserve full source text and re-plan only proven failed parts."""
import asyncio
import hashlib
import json
from html import escape, unescape
from types import SimpleNamespace

import pytest

import notify_cards
import notify_parts
import notify_scope
import notify_spool
import present_slack
from adapters.slack import delivery
from adapters.slack.cards import validate
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread
from slack_card_testkit import _spec
from slack_testkit import ROOT_TS, SCOPE, SLACK, _granted_card, _mkworld
from slack_testkit import isolated_slack_ledger as led

__all__ = ["led"]
pytestmark = pytest.mark.thread_bodies

# Fully synthetic: matches the observed length metadata, not source content.
MULTIBYTE = "あ" * 1658 + "é" + "x" * 240
ESCAPED = "&<>" * 633


class Rejected(Exception):
    def __init__(self, error="msg_too_long"):
        super().__init__(error)
        self.response = SimpleNamespace(status_code=200, headers={},
                                        data={"ok": False, "error": error})


@pytest.fixture
def world(led):
    notify_spool.ensure_dirs(notify_spool.data_root(led))
    world = _mkworld(led)
    assert asyncio.run(world.sender.bind())
    return world


def _source_spec(text, *, prior=None, edit_only=False):
    spec = _spec()
    spec["card_key"] = "synthetic-root|reply:202"
    spec["delivery"].update(SCOPE, thread_id=ROOT_TS)
    spec["parts"]["post_source_id"] = 202
    spec["parts"]["context"] = {"project_id": 1, "source_message_id": 202}
    part = {"kind": "body_part", "part_id": "body:0001", "name": "m:202#1",
            "sha256": notify_parts.text_sha256(text), "bytes": len(text.encode("utf-8"))}
    if prior:
        spec["op"] = "update"
        spec["delivery"]["message_id"] = "1790000000.000020"
        part["prior_remote_id"] = prior
        if edit_only:
            part["edit_only"] = True
    spec["parts"]["manifest"] = [part]
    return spec, part


def _run(world, spec, text, part):
    return asyncio.run(world.worker._body_part(spec, text, {
        "thread_id": ROOT_TS, "history": None, "consumed": set()}, part))


@pytest.mark.parametrize("text", [MULTIBYTE, ESCAPED, "😀" * 1900,
                                  "合成\n" + "&<😀>" * 1400 + "\n" + "本文" * 1600 + "\n末尾"],
                         ids=["multibyte_1899", "escaped", "emoji", "mixed_multiline"])
def test_existing_splitter_bounds_encoded_wire_without_losing_characters(text):
    chunks = notify_parts.split_body_chunks(
        text, wire_limit=present_slack.THREAD_PART_WIRE_LIMIT,
        wire_size=present_slack.thread_part_wire_size)
    assert "".join(chunks) == text
    assert all(0 < len(chunk) <= notify_parts.THREAD_PART_LIMIT for chunk in chunks)
    assert all(len(escape(chunk, quote=False).encode("utf-8")) <= delivery.BODY_WIRE_MAX
               for chunk in chunks)
    assert present_slack.THREAD_PART_WIRE_LIMIT == delivery.BODY_WIRE_MAX == 4000


def test_character_boundaries_stay_unchanged_when_wire_already_fits():
    text = "合成見出し\n" + "x" * 6100 + "\n末尾"
    assert notify_parts.split_body_chunks(text) == notify_parts.split_body_chunks(
        text, wire_limit=4000, wire_size=present_slack.thread_part_wire_size)


@pytest.mark.parametrize("text", [
    "あ" * 1333 + "\n\n",
    "&" * 800 + "\n\n",
    "あ" * 1333 + "\n\n" + "続き" * 900,
    "\n\n" + "あ" * 1333,
    "あ" * 1333 + " " * 1900 + "終わり",
    "あ" * 1333 + "\n\n" + "&" * 800 + "\n\n",
], ids=["multibyte_tail", "escaped_tail", "middle_newlines", "leading_newlines",
        "middle_spaces", "repeated_boundaries"])
def test_wire_boundary_whitespace_is_rebalanced_losslessly(text):
    chunks = notify_parts.split_body_chunks(
        text, wire_limit=4000, wire_size=present_slack.thread_part_wire_size)
    assert "".join(chunks) == text
    assert all(chunk.strip() and len(chunk) <= notify_parts.THREAD_PART_LIMIT
               and present_slack.thread_part_wire_size(chunk) <= 4000 for chunk in chunks)


@pytest.mark.parametrize("mode", ["create", "exact", "legacy"])
@pytest.mark.parametrize("text", [MULTIBYTE, ESCAPED], ids=["multibyte_1899", "escaped"])
def test_old_oversized_sealed_part_is_not_sent_or_split_by_worker(world, mode, text):
    prior = "1790000000.000010"
    world.client.replies[ROOT_TS] = [
        {"ts": prior, "text": "合成旧本文", "bot_id": world.client.bot_id}]
    spec, part = _source_spec(text, prior=prior if mode != "create" else None,
                              edit_only=mode == "exact")
    if mode == "legacy":
        spec["card_key"] = "synthetic-legacy"
        spec["parts"].pop("post_source_id")
    before = len(world.client.calls)
    assert _run(world, spec, text, part) == {
        "result": "not_sent", "error_code": "body_part_wire_too_long"}
    writes = [kind for kind, _ in world.client.calls[before:] if kind in ("create", "update")]
    assert writes == [] and world.client.replies[ROOT_TS][0]["text"] == "合成旧本文"


def test_existing_oversized_exact_prior_can_be_verified_without_resending(world):
    prior = "1790000000.000010"
    world.client.replies[ROOT_TS] = [
        {"ts": prior, "text": escape(MULTIBYTE, quote=False), "bot_id": world.client.bot_id}]
    spec, part = _source_spec(MULTIBYTE, prior=prior)
    assert _run(world, spec, MULTIBYTE, part) == {"result": "delivered", "remote_id": prior}
    assert not [kind for kind, _ in world.client.calls if kind in ("create", "update")]


def _body_parts(spec):
    return [part for part in spec["parts"]["manifest"] if part["kind"] == "body_part"]


def _source_chunks(spec):
    return [spec["parts"]["thread_body_parts"][int(part["part_id"].split(":")[1]) - 1]
            for part in _body_parts(spec)
            if (part.get("name") or "").startswith("m:100#")]


def _old_failed_update(led, monkeypatch, text, *, unknown=False):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    _seed_thread(led, mids=(100,))
    with led.db:
        led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", ("x" * 1899,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)
    world = _mkworld(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    initial = _latest_render(led)
    prior = led.db.execute(
        "SELECT remote_id FROM notification_render_parts WHERE delivery_id=? AND name='m:100#1'",
        (initial["delivery_id"],)).fetchone()[0]
    with led.db:
        led.db.execute("UPDATE messages SET body_text=?,content_hash=? WHERE message_id=100",
                       (text, hashlib.sha256(text.encode()).hexdigest()))
    native_update = world.client.chat_update
    native_post = world.client.chat_postMessage
    rejected_count = 0

    def reject_long(kwargs):
        nonlocal rejected_count
        if len(kwargs["text"].encode("utf-8")) > 4000:
            rejected_count += 1
            if unknown is True or (unknown == "second" and rejected_count == 2):
                raise TimeoutError("synthetic uncertain result")
            raise Rejected()

    async def update(**kwargs):
        reject_long(kwargs)
        return await native_update(**kwargs)

    async def post(**kwargs):
        reject_long(kwargs)
        return await native_post(**kwargs)

    world.client.chat_update = update
    world.client.chat_postMessage = post
    # Generate and execute the previously shipped size policy. This creates
    # a factual old not_sent/unknown receipt, without editing its ledger state.
    with monkeypatch.context() as old:
        old.setattr(present_slack, "THREAD_PART_WIRE_LIMIT", None)
        old.setattr(delivery, "BODY_WIRE_MAX", 100_000)
        with led.db:
            notify_cards.issue_render(led.db, 1, SLACK, NOW + 1, specs := [])
        notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), specs, NOW + 1)
        asyncio.run(_granted_card(world.worker, led, world.root))
    failed = _latest_render(led)
    part = led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? AND name='m:100#1'",
        (failed["delivery_id"],)).fetchone()
    assert part["state"] == ("unknown" if unknown is True else "not_sent")
    assert part["error_code"] == ("timeouterror" if unknown is True else "msg_too_long")
    return world, initial, failed, prior, native_update


@pytest.mark.parametrize("text,gc_spec", [
    (MULTIBYTE, False), (MULTIBYTE, True), (ESCAPED, False),
    ("あ" * 1333 + "\n\n", False), ("&" * 800 + "\n\n", False),
], ids=["multibyte", "multibyte_gc", "escaped", "multibyte_tail", "escaped_tail"])
def test_sweep_replans_known_length_rejection_once_and_preserves_prior(led, monkeypatch, text, gc_spec):
    world, initial, failed, prior, native_update = _old_failed_update(led, monkeypatch, text)
    if gc_spec:
        with led.db:
            led.db.execute("UPDATE notification_renders SET spec_json=NULL WHERE delivery_id=?",
                           (failed["delivery_id"],))
    result = notify_cards.sweep(led, SLACK, now=NOW + 2)
    assert result["updated"] == 1
    repaired = _latest_render(led)
    assert repaired["delivery_id"] != failed["delivery_id"]
    spec = json.loads(repaired["spec_json"])
    validate(spec)
    assert "".join(_source_chunks(spec)) == text
    assert all(present_slack.thread_part_wire_size(chunk) <= 4000 for chunk in _source_chunks(spec))
    first = next(part for part in _body_parts(spec) if part.get("name") == "m:100#1")
    assert first["prior_remote_id"] == prior and first["edit_only"] is True
    world.client.chat_update = native_update
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert led.db.execute("SELECT parts_state FROM notification_renders WHERE delivery_id=?",
                          (repaired["delivery_id"],)).fetchone()[0] == "complete"
    posted = {row["ts"]: unescape(row["text"]) for row in world.client.replies[ROOT_TS]}
    source_rows = led.db.execute(
        "SELECT name,remote_id FROM notification_render_parts WHERE delivery_id=? "
        "AND name LIKE 'm:100#%' ORDER BY idx", (repaired["delivery_id"],)).fetchall()
    assert "".join(posted[row["remote_id"]] for row in source_rows) == text
    notify_cards.sweep(led, SLACK, now=NOW + 3)
    assert _latest_render(led)["delivery_id"] == repaired["delivery_id"]
    # The previous factual records stay intact; repair owns a fresh render.
    assert led.db.execute("SELECT state FROM notification_render_parts WHERE delivery_id=? AND name='m:100#1'",
                          (initial["delivery_id"],)).fetchone()[0] == "delivered"
    assert led.db.execute("SELECT state FROM notification_render_parts WHERE delivery_id=? AND name='m:100#1'",
                          (failed["delivery_id"],)).fetchone()[0] == "not_sent"


def test_unknown_length_attempt_is_never_replanned_or_released(led, monkeypatch):
    world, _, failed, _, _ = _old_failed_update(led, monkeypatch, MULTIBYTE, unknown=True)
    before = len(world.client.calls)
    notify_cards.sweep(led, SLACK, now=NOW + 2)
    assert _latest_render(led)["delivery_id"] == failed["delivery_id"]
    assert len(world.client.calls) == before
    assert led.db.execute("SELECT state FROM notification_render_parts WHERE delivery_id=? AND name='m:100#1'",
                          (failed["delivery_id"],)).fetchone()[0] == "unknown"


def test_known_size_repair_waits_while_another_part_is_unknown(led, monkeypatch):
    _, _, failed, _, _ = _old_failed_update(led, monkeypatch, MULTIBYTE * 2, unknown="second")
    card = notify_scope.card_row(led.db, 1)
    assert notify_parts.needs_wire_replan(led.db, card, failed)
    before = [dict(row) for row in led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? ORDER BY idx",
        (failed["delivery_id"],))]
    assert any(part["state"] == "unknown" for part in before)
    notify_cards.sweep(led, SLACK, now=NOW + 2)
    assert _latest_render(led)["delivery_id"] == failed["delivery_id"]
    assert [dict(row) for row in led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=? ORDER BY idx",
        (failed["delivery_id"],))] == before


def test_missing_gc_spec_without_wire_size_proof_does_not_guess(led, monkeypatch):
    _, _, failed, _, _ = _old_failed_update(led, monkeypatch, ESCAPED)
    with led.db:
        led.db.execute("UPDATE notification_renders SET spec_json=NULL WHERE delivery_id=?",
                       (failed["delivery_id"],))
    notify_cards.sweep(led, SLACK, now=NOW + 2)
    assert _latest_render(led)["delivery_id"] == failed["delivery_id"]
    assert not notify_parts.needs_wire_replan(led.db, notify_scope.card_row(led.db, 1), _latest_render(led))
