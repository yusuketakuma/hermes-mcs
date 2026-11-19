"""Card sweep isolation, LINE WORKS part-budget truncation and signals while
the interactive kill switch is off. Synthetic temp DB and stubs only."""
from __future__ import annotations

import json

import notify_cards
import notify_scope
import present_lineworks
from adapters.lineworks import cards as lineworks_cards
from notify_lineworks_testkit import LINEWORKS
from notify_testkit import (CFG, NOW, _dispatch, _intent, _latest_render, _msg, _patient,
                            _row, _seed_thread, _signal_row, led)

__all__ = ["led"]


def test_lineworks_thread_card_over_part_budget_holds_instead_of_raising(led, monkeypatch):
    # A short card limit makes the synthetic face continue in display#k.
    monkeypatch.setattr(present_lineworks, "CARD_LIMIT", 100)
    _patient(led, 1)
    _msg(led, 100, 1, body="合成の親投稿")
    replies = list(range(200, 330))                  # 130 replies > MAX_PARTS budget
    for mid in replies:
        _msg(led, mid, 1, parent=100, body=f"合成返信{mid}")
    cfg = {**LINEWORKS, "notify": {**LINEWORKS["notify"], "card_thread": True}}
    _dispatch(led, _intent(led, payload={"message_ids": [100] + replies}), cfg)
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    names = [p.get("name") for p in spec["parts"]["manifest"]]
    assert render["state"] == "held"
    assert "display#1" in names and names[-1] == "truncated#1"
    assert not any(str(n).startswith("m:") for n in names)
    assert lineworks_cards.validate(spec) is spec
    codes = {r[0] for r in led.db.execute(
        "SELECT error_code FROM notification_render_parts WHERE delivery_id=?",
        (render["delivery_id"],))}
    assert codes == {"part_budget_exceeded"}


def test_one_failing_card_does_not_stop_the_sweep(led, monkeypatch):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    _seed_thread(led, pid=2, root=300, mids=(300, 301))
    _dispatch(led, _intent(led, pid=2, payload={"message_ids": [300, 301]}))
    led.db.execute("UPDATE notification_cards SET updated_at=?", (NOW - 100,))
    led.db.commit()
    real = notify_cards.issue_render
    seen = []

    def issue(db, card_id, *args, **kwargs):
        seen.append(card_id)
        if card_id == 1:
            db.execute("UPDATE notification_cards SET source_generation=source_generation+1 "
                       "WHERE card_id=1")
            raise ValueError("synthetic-poison")
        return real(db, card_id, *args, **kwargs)

    monkeypatch.setattr(notify_cards, "issue_render", issue)
    before = led.db.execute("SELECT source_generation FROM notification_cards WHERE card_id=1").fetchone()[0]
    out = notify_cards.sweep(led, CFG, limit=50, now=NOW)
    assert seen[0] == 1 and len(seen) == out["scanned"] > 1   # the poison card comes first
    assert out["failed"] == [{"card_id": 1, "error": "card_sweep_card_failed", "type": "ValueError"}]
    assert "synthetic-poison" not in json.dumps(out)
    assert not led.db.in_transaction
    rotated = {r[0] for r in led.db.execute("SELECT updated_at FROM notification_cards")}
    assert rotated == {NOW}                  # the window rotates past the poison card
    # the failed card's partial writes were rolled back
    assert led.db.execute("SELECT source_generation FROM notification_cards "
                          "WHERE card_id=1").fetchone()[0] == before


def test_signal_waits_unsealed_while_cards_are_off_and_seals_on_restored_transport(led):
    _patient(led, 1)
    _signal_row(led, "sig-off", pid=1)
    ev = _intent(led, kind="signal", pid=1,
                 payload={"signal_keys": ["sig-off"], "project_id": 1, "type": "med_followup"})
    off = {**LINEWORKS, "notify": {**LINEWORKS["notify"], "interactive": "off"}}
    assert _dispatch(led, ev, off) == {"skipped": True}
    row = _row(led, ev["event_id"])
    assert row["state"] == "pending" and row["attempts"] == 0
    assert json.loads(row["progress"])["wait"]["reason"] == "signals_interactive_off"
    assert "signals_interactive_off" in notify_scope.RESEAT_REASONS
    assert led.db.execute("SELECT COUNT(*) FROM notification_intent_batches").fetchone()[0] == 0
    assert led.db.execute("SELECT COUNT(*) FROM notification_cards").fetchone()[0] == 0
    _dispatch(led, ev, LINEWORKS, now=NOW + notify_cards.RESEAT_S + 5)
    assert [tuple(r) for r in led.db.execute(
        "SELECT transport FROM notification_intent_batches")] == [("lineworks",)]
    assert [tuple(r) for r in led.db.execute(
        "SELECT kind,transport FROM notification_cards")] == [("signal", "lineworks")]
