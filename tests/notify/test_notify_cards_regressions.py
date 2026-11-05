"""notify_cards regressions kept apart from test_notify_cards.py (whose module-wide
wall-clock pin must not reach them): revoked reuse, LINE WORKS thread overflow,
blank body chunks, Slack scope under the kill switch, legacy defer footer in
JST, root-first thread plan and the card-resolve config path. Synthetic temp
ledger only.
"""
from __future__ import annotations

import notify_recovery

import notify_parts
import notify_scope
import json
import sqlite3
import time
from datetime import datetime

import pytest

import mcs_util
import notify_cards
import notify_card_renders
import notify_digest
from adapters.lineworks import cards as lwcards
from mcs_queries import JST
from notify_testkit import (
    CFG,
    NOW,
    _card,
    _dispatch,
    _intent,
    _msg,
    _seed_thread,
    led,
    _patient,
    _deliver,
    _latest_render,
)
from slack_testkit import SLACK
from adapters.common import envelopes
from notify_lineworks_testkit import (
    LINEWORKS,
    MID,
    _apply,
    _claim,
    _render,
)

__all__ = ["led"]


# Regression tests for notify_cards fixes (revoked reuse, LINE WORKS
# failed thread overflow, blank body chunks, JST digest thread name).
def _revoke_thread_card(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    led.db.commit()
    notify_cards.sweep(led, CFG)
    assert _card(led)["delivery_state"] == "revoked"
    _msg(led, 102, 1, parent=100)


def _event(led, event_id):
    ic = [dict(r) for r in led.db.execute(
        "SELECT card_id,delivery_id,state FROM notification_intent_cards "
        "WHERE event_id=?", (event_id,))]
    ob = led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                        (event_id,)).fetchone()["state"]
    return ic, ob


def test_reactivated_unit_gets_a_fresh_card(led, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    _revoke_thread_card(led)
    led.db.execute("UPDATE patients SET is_archived=0 WHERE project_id=1")
    led.db.commit()
    ev = _intent(led, payload={"message_ids": [102]})
    assert _dispatch(led, ev)["dispatched"]
    ic, _ob = _event(led, ev["event_id"])
    assert ic[0]["card_id"] != 1 and ic[0]["delivery_id"] is not None
    assert _card(led, ic[0]["card_id"])["delivery_state"] != "revoked"
    assert _card(led)["delivery_state"] == "revoked"


def test_still_archived_unit_suppresses_new_intent(led, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    _revoke_thread_card(led)
    ev = _intent(led, payload={"message_ids": [102]})
    _dispatch(led, ev)
    ic, ob = _event(led, ev["event_id"])
    assert ic == [{"card_id": 1, "delivery_id": None, "state": "suppressed"}]
    assert ob == "suppressed"


def test_lineworks_failed_thread_keeps_display_overflow(led, monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    original = notify_card_renders.card_content

    def long_display(*a, **k):
        c = original(*a, **k)
        c["containers"] = [{"type": "text", "text": "合成内容" * 400}]
        return c
    monkeypatch.setattr(notify_card_renders, "card_content", long_display)
    claim = _claim(_render(led))
    lwcards.validate(claim["spec"])
    assert _apply(led, envelopes.transport_begin(claim))["granted"]
    assert _apply(led, envelopes.transport_receipt(
        claim, "delivered", message_id=MID))["applied"]
    thread = next(p for p in claim["spec"]["parts"]["manifest"]
                  if p["kind"] == "thread")
    assert _apply(led, envelopes.part_receipt(
        claim, thread, "not_sent", error_code="scope_mismatch"))["applied"]
    db = notify_cards.connection(led)
    card = notify_scope.card_row(db, 1)
    assert card["thread_state"] == "failed"
    content = notify_card_renders.card_content(db, card)
    card = {**card, **notify_cards._generation_drift(card, content)}
    before_card, before_content = dict(card), dict(content)
    spec, budget_hold = notify_cards._build_spec(
        db, card, content, "update", 99, LINEWORKS, NOW)
    assert budget_hold is None
    assert card == before_card and content == before_content
    lwcards.validate(spec)


@pytest.mark.parametrize("text", [
    "a\n" + (" " * 1000 + "\n") * 3 + "b",
    "x" * 1000 + "\n" + "\n" * 3000 + "b",
    "a" * 1000 + "\n" + " " * 3900,
    "head\n" + "\n" * 4000 + "body",
    "x" * 1900 + " " * 1905,
    "　" * 2500 + "本文",
])
def test_body_chunks_preserve_blank_runs(text):
    chunks = notify_parts.split_body_chunks(text)
    assert all(len(c) <= notify_parts.THREAD_PART_LIMIT for c in chunks)
    assert "".join(chunks) == text


def test_body_chunks_mid_blank_run_stays_lossless():
    for text in ("a\n" + (" " * 1000 + "\n") * 3 + "b",
                 "x" * 1000 + "\n" + "\n" * 3000 + "b"):
        chunks = notify_parts.split_body_chunks(text)
        assert "".join(chunks) == text
        assert all(chunk.strip() for chunk in chunks)


def test_digest_thread_name_uses_jst_day(monkeypatch):
    now = datetime(2026, 10, 5, 8, 0, tzinfo=JST).timestamp()
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        assert notify_cards._digest_thread_name(now).endswith("10-05")
    finally:
        monkeypatch.undo()
        time.tzset()


# Sealed Slack intents survive the kill switch; a queued Slack notice is
# re-aimed after a profile change. Synthetic temp ledger only.
def test_sealed_slack_intent_reseats_with_kill_switch_off(led):
    _seed_thread(led)
    ev = _intent(led)
    assert _dispatch(led, ev, SLACK)["dispatched"]
    off = {**SLACK, "notify": {**SLACK["notify"], "interactive": "off"}}
    out = _dispatch(led, ev, off)
    assert "error" not in out and out.get("resealed")


def test_profile_change_reissues_queued_slack_notice(led):
    cfg = {**SLACK, "daily_digest": {"enabled": True, "hour_jst": 0}}
    _patient(led, 1, name="患者A")
    led.db.commit()
    assert notify_digest.maybe_enqueue(led, cfg, now=NOW) == 1
    ev = led.db.execute("SELECT * FROM notify_outbox WHERE kind="
                        "'daily_digest'").fetchone()
    notify_cards.dispatch_intent(led, dict(ev), cfg, now=NOW)
    moved = {**cfg, "notify": {**cfg["notify"], "slack": {
        **cfg["notify"]["slack"], "profile": "other"}}}
    notify_cards.dispatch_intent(led, dict(ev), moved, now=NOW)
    first, second = notify_cards._notice_renders(led.db, ev["event_id"])
    assert (first["state"], second["state"]) == ("cancelled", "queued")
    assert second["profile"] == "other"


# The legacy 保留中 footer shows defer_until in JST, not host local time.
def test_deferred_footer_uses_jst_on_utc_host(led, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        _seed_thread(led)
        _dispatch(led, _intent(led))
        _deliver(led)
        until = NOW + 3600
        led.db.execute(
            "INSERT INTO notification_triage(card_id,owner,defer_until,"
            "state,revision,last_actor,updated_at) "
            "VALUES(1,NULL,?,'deferred',1,'discord:1',?)", (until, NOW))
        led.db.commit()
        notify_cards.sweep(led, CFG, now=NOW + 1)
        text = json.dumps(_latest_render(led)["spec_json"], ensure_ascii=False)
    finally:
        monkeypatch.undo()
        time.tzset()
    jst = datetime.fromtimestamp(until, JST).strftime("%m-%d %H:%M")
    assert f"保留中（〜{jst}）" in text


# Regression: the thread delivery plan orders root-first like the card
# face, so a reply with a NULL posted_at_ts never precedes the root.
def test_thread_plan_puts_root_before_null_timestamp_reply(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE messages (message_id INTEGER, parent_id "
               "INTEGER, project_id INTEGER, posted_at_ts INTEGER)")
    db.executemany("INSERT INTO messages VALUES (?,?,?,?)",
                   [(100, None, 1, 1000), (101, 100, 1, None),
                    (102, 100, 1, 2000)])
    monkeypatch.setattr(notify_parts, "_announced_ids",
                        lambda db, card: {100, 101, 102})
    monkeypatch.setattr(notify_parts, "delivered_members",
                        lambda db, card, legacy: set())
    card = {"kind": "thread", "root_message_id": 100, "project_id": 1}
    assert notify_parts._thread_plan_ids(db, card, [102]) == [100, 101, 102]


# apply_card_resolve without cfg must read MCS_ROOT's config, not ~/.mcs.
class _Stop(Exception):
    pass


def test_card_resolve_default_cfg_uses_conf_path(tmp_path, monkeypatch):
    seen = []
    conf = str(tmp_path / "root" / "config.json")
    monkeypatch.setattr(mcs_util, "CONF_PATH", conf)
    monkeypatch.setattr(mcs_util, "load_config",
                        lambda path=None: seen.append(path) or {})
    monkeypatch.setattr(notify_cards, "connection", lambda ledger: None)

    def stop(_req):
        raise _Stop

    monkeypatch.setattr(notify_recovery, "payload_hash", stop)
    with pytest.raises(_Stop):
        notify_recovery.apply_card_resolve(object(), {"command_id": "c"})
    assert seen == [conf]


# Recovered efficiency regressions (v9-eff-source-preserving-chunks).
def test_long_single_line_keeps_boundaries_without_repeated_suffix_copies():
    copied = []

    class Segment(str):
        def splitlines(self, keepends=False):
            return [Segment(s) for s in super().splitlines(keepends)]

        def __getitem__(self, key):
            result = super().__getitem__(key)
            if isinstance(key, slice):
                copied.append(len(result))
                return Segment(result)
            return result

    text = "head\n" + "x" * 100_007 + "\nend"
    chunks = notify_parts.split_body_chunks(Segment(text))
    assert chunks == [text[i:i + 1900] for i in range(0, len(text), 1900)]
    assert sum(copied) <= 2 * len(text), "repeated suffix copies exceed linear budget"


@pytest.mark.parametrize("text,limit,expected", [
    ("", 4, []),
    ("abcdef\nend", 4, ["abcd", "ef\n", "end"]),
    ("a\n1234", 4, ["a\n", "1234"]),
    ("a\n12345", 4, ["a\n12", "345"]),
    ("ab\n\n\nc", 4, ["ab\n\n", "\nc"]),
    ("a" + " " * 10 + "b", 4, ["a   ", "    ", "   b"]),
    (" " * 8, 4, [" " * 4, " " * 4]),
])
def test_body_chunks_preserve_exact_text_and_boundaries(text, limit, expected):
    assert notify_parts.split_body_chunks(text, limit) == expected
