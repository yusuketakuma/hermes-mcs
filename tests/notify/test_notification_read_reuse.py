"""Read reuse stays within one notification render; tokens and receipts remain durable."""
from collections import Counter

import pytest

import notify_cards
import notify_card_renders
import notify_render
from notify_testkit import (
    CFG, NOW, _begin, _card, _dispatch, _intent, _latest_render,
    _receipt, _seed_thread, led as led,
)


@pytest.mark.parametrize("verdict", [None, "rule", "llm"])
def test_preview_reuses_even_an_absent_urgency_without_mutating_input(led, monkeypatch, verdict):
    _seed_thread(led)
    calls = []
    monkeypatch.setattr(notify_render.clinical_urgency, "message_urgency",
                        lambda _db, mid: calls.append(mid) or verdict)
    monkeypatch.setattr(notify_render, "structured_block", lambda *a, **kw: None)
    cached = {100: verdict, 101: verdict}
    content = {"shown": [100, 101]}
    card = {"kind": "thread", "project_id": 1}
    shared = notify_render.notification_preview(led.db, card, content, _urgencies=cached)
    assert calls == []
    assert cached == {100: verdict, 101: verdict}
    fresh = notify_render.notification_preview(led.db, card, content)
    assert fresh == shared
    assert calls and len(calls) == len(set(calls))


def test_preview_reads_a_cache_miss_and_does_not_leak_it_to_the_caller(led, monkeypatch):
    _seed_thread(led)
    calls = []
    monkeypatch.setattr(notify_render.clinical_urgency, "message_urgency",
                        lambda _db, mid: calls.append(mid) or None)
    monkeypatch.setattr(notify_render, "structured_block", lambda *a, **kw: None)
    cached = {100: None}
    notify_render.notification_preview(led.db, {"kind": "thread", "project_id": 1},
                                       {"shown": [100, 101]}, _urgencies=cached)
    assert calls == [101]
    assert cached == {100: None}


def test_card_passes_only_bounded_shown_urgency_to_preview(led, monkeypatch):
    _seed_thread(led, mids=tuple(range(100, 112)))
    _dispatch(led, _intent(led, payload={"message_ids": list(range(100, 112))}))
    card = {**_card(led), "layout": 1}
    calls = []
    captured = []
    monkeypatch.setattr(notify_render.clinical_urgency, "message_urgency",
                        lambda _db, mid: calls.append(mid) or ("rule" if mid == 100 else None))
    def preview(_db, _card, content, **kw):
        cached = kw["_urgencies"]
        captured.append(dict(cached))
        assert set(cached) <= set(content["shown"])
        assert len(cached) <= notify_render.PAGE_THREAD
        return "preview"
    monkeypatch.setattr(notify_render, "notification_preview", preview)
    first = notify_render.card_content(led.db, card, visible_providers=[])
    assert set(calls) == set(range(100, 112))
    assert first["urgent"]
    assert captured and captured[0]
    calls.clear()
    notify_render.card_content(led.db, card, visible_providers=[])
    assert set(calls) == set(range(100, 112))


@pytest.mark.parametrize("meds_mid, available", [(None, False), (100, False), (100, True)])
def test_build_spec_shares_read_choices_but_mints_fresh_tokens(led, monkeypatch, meds_mid, available):
    _seed_thread(led)
    cfg = {**CFG, "notify": {**CFG["notify"], "card_thread": False}}
    _dispatch(led, _intent(led), cfg=cfg)
    render = _latest_render(led)
    assert _begin(led, render, cfg=cfg)["granted"]
    assert _receipt(led, render, "0000000000000001", cfg=cfg)["applied"]
    card = _card(led)
    content = notify_render.card_content(led.db, card, cfg=cfg)
    calls = Counter()
    def meds(*_args):
        calls["meds"] += 1
        return meds_mid
    def search(*_args):
        calls["search"] += 1
        return available
    monkeypatch.setattr(notify_card_renders.action_context, "meds_message", meds)
    monkeypatch.setattr(notify_card_renders, "drug_search_available", search)
    tokens = []
    for rev in (2, 3):
        with led.db:
            spec, hold = notify_cards._build_spec(led.db, card, content, "update", rev, cfg, NOW)
        assert hold is None
        rows = spec["parts"]["action_rows"]
        ids = {b["id"] for row in rows for b in row}
        assert ("meds" in ids) is (meds_mid is not None)
        assert ("drugsearch" in ids) is available
        tokens.append({b["token"] for row in rows for b in row if b.get("token")})
    assert calls == {"meds": 2, "search": 2}
    assert tokens[0] and tokens[1] and tokens[0].isdisjoint(tokens[1])


def test_receipt_marks_dirty_once_then_terminal_replay_and_conflict_do_not(led, monkeypatch):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    marks = []
    original = notify_cards.mark_snapshot_dirty
    def mark(db):
        marks.append(True)
        return original(db)
    monkeypatch.setattr(notify_cards, "mark_snapshot_dirty", mark)
    assert _receipt(led, render, "0000000000000001")["applied"]
    assert len(marks) == 1
    assert _receipt(led, render, "0000000000000001")["applied"]
    conflict = _receipt(led, render, "0000000000000001", message_id="different")
    assert conflict["conflict"] and not conflict["applied"]
    assert len(marks) == 1
    assert _card(led)["delivery_state"] == "delivered"


def test_rejected_receipt_does_not_dirty_or_change_the_attempt(led, monkeypatch):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    marks = []
    monkeypatch.setattr(notify_cards, "mark_snapshot_dirty", lambda _db: marks.append(True))
    wrong = dict(render)
    wrong["payload_hash"] = "f" * 64
    out = _receipt(led, wrong, "0000000000000001")
    assert out["error"] == "payload_hash_mismatch" and not out["applied"]
    assert marks == []
    assert led.db.execute("SELECT state FROM notification_delivery_attempts").fetchone()[0] == "granted"
