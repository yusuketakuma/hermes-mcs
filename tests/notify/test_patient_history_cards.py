"""Synthetic cohort cards on the existing journal, with no real transport."""
import json

import pytest

from adapters.common import envelopes
import initial_import_state
import history_walk
import notify_cards
import notify_history
import notify_render
from notify_testkit import (CFG, NOW, _begin, _card, _dispatch, _latest_render,
                            _patient, _receipt, led, pinned_clock)
from notify_lineworks_testkit import LINEWORKS, _apply, _claim, _deliver

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")
SLACK = {"notify": {"interactive": "slack", "route_epoch": 1, "card_thread": True,
                   "slack": {"profile": "mcs", "application_id": "app1", "team_id": "T1", "channel_id": "C1"}}}


def seed(led, count=3):
    _patient(led)
    led.db.executemany("INSERT INTO messages(message_id,project_id,posted_at_ts,body_text,content_hash,body_state,first_seen) "
        "VALUES(?,1,?,'完全合成の履歴',?,'full',?)",
        ((i, int(NOW) - i, f"{i:064x}", NOW - 10) for i in range(1, count + 1)))
    with led.db:
        initial_import_state.start_tx(led, 1, int(NOW), NOW - 20, boundary_source="server")
        initial_import_state.classify_tx(led, 1, list(range(1, count + 1)), NOW)
    return led.db.execute("SELECT * FROM notify_outbox WHERE kind='patient_history' ORDER BY event_id LIMIT 1").fetchone()


def captured(led):
    state = initial_import_state.load(led.db, 1)
    state["capture_phase"] = "complete"
    state["capture_proof"] = {"v":1,"scope":history_walk.scope(1,state["cohort_id"]),"digest":"a"*64}
    led.db.execute("UPDATE patients SET initial_import=? WHERE project_id=1", (json.dumps(state),))
    led.db.commit()


def ready(monkeypatch, done=True):
    monkeypatch.setattr(notify_history.extraction_progress, "message_progress", lambda *a, **kw:
        {"state": "complete" if done else "processing", "completed": int(done), "total": 1})


def spec(led, card_id=1):
    return json.loads(_latest_render(led, card_id)["spec_json"])


@pytest.mark.parametrize("cfg", [CFG, SLACK, LINEWORKS])
def test_history_has_one_card_part_only_and_link_without_source_actions(led, monkeypatch, cfg):
    event = seed(led)
    ready(monkeypatch, False)
    assert _dispatch(led, event, cfg)["cards"] == 1
    rendered = spec(led)
    assert rendered["kind"] == "patient_history"
    assert [part["kind"] for part in rendered["parts"]["manifest"]] == ["card"]
    assert [[b["id"] for b in row] for row in rendered["parts"]["action_rows"]] == [["link"]]
    assert all(k not in rendered["parts"] for k in ("thread_body_parts", "thread_name", "post_source_id", "source_thread"))
    assert "完全合成の履歴" not in json.dumps(rendered, ensure_ascii=False)
    assert _card(led)["layout"] == 5
    assert not led.db.execute("SELECT 1 FROM notification_action_tokens").fetchone()


def test_5000_history_members_are_not_a_display_analysis_limit(led, monkeypatch):
    event = seed(led, 5000)
    counted = []
    monkeypatch.setattr(notify_history.extraction_progress, "message_progress", lambda db, row, **kw:
        counted.append(row["message_id"]) or {"state": "complete", "completed": 1, "total": 1})
    captured(led)
    _dispatch(led, event)
    ui = json.loads(_card(led)["ui_state"])
    assert ui["history_snapshot"]["saved"] == ui["history_snapshot"]["analyzed"] == 5000
    assert set(counted) == set(range(1, 5001))
    assert ui["history_snapshot"]["phase"] == "complete"
    assert len(json.dumps(ui)) < 3000


def test_progress_during_queued_render_keeps_sealed_generation_and_source(led, monkeypatch):
    event = seed(led)
    ready(monkeypatch, False)
    _dispatch(led, event)
    before = _latest_render(led)
    source = _card(led)["source_fp"]
    captured(led)
    ready(monkeypatch)
    notify_cards.sweep(led, CFG, now=NOW + 301)
    after = _latest_render(led)
    assert after["delivery_id"] == before["delivery_id"]
    assert _card(led)["source_fp"] == source
    assert json.loads(_card(led)["ui_state"])["history_latest"]["phase"] == "complete"
    assert _begin(led, after)["granted"]


def test_source_edit_invalidates_queued_snapshot(led, monkeypatch):
    event = seed(led)
    ready(monkeypatch)
    _dispatch(led, event)
    before = _latest_render(led)
    led.db.execute("UPDATE messages SET content_hash=? WHERE message_id=1", ("e" * 64,))
    led.db.commit()
    assert notify_render.source_fingerprint(led.db, _card(led)) != _card(led)["source_fp"]
    assert not _begin(led, before)["granted"]


def test_slack_discord_update_one_remote_identity_after_phase_change(led, monkeypatch):
    event = seed(led)
    ready(monkeypatch, False)
    _dispatch(led, event)
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    assert _receipt(led, render, "0000000000000001", message_id="stable")["applied"]
    captured(led)
    ready(monkeypatch)
    notify_cards.sweep(led, CFG, now=NOW + 1)
    rendered = spec(led)
    assert rendered["op"] == "update" and rendered["delivery"]["message_id"] == "stable"
    assert [part["kind"] for part in rendered["parts"]["manifest"]] == ["card"]


def test_due_progress_intents_coalesce_without_faking_individual_delivery(led, monkeypatch):
    event = seed(led)
    state = initial_import_state.load(led.db, 1)
    with led.db:
        for _ in range(8):
            initial_import_state.emit_tx(led, 1, state, [1, 2])
    ready(monkeypatch)
    _dispatch(led, event)
    assert led.db.execute("SELECT COUNT(*) FROM notification_cards").fetchone()[0] == 1
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0] == 1
    assert led.db.execute("SELECT COUNT(*) FROM notification_intent_cards").fetchone()[0] == 9
    assert not led.db.execute("SELECT 1 FROM notify_outbox WHERE state='accepted'").fetchone()
    assert not led.db.execute("SELECT 1 FROM notify_outbox WHERE kind='new_messages'").fetchone()


def test_history_in_a_new_messages_payload_cannot_create_individual_card(led):
    seed(led)
    assert notify_cards._resolve_targets(led.db, {"kind": "new_messages", "project_id": 1},
                                         {"message_ids": [1, 2]}) == []


@pytest.mark.parametrize("cfg", [CFG, SLACK])
def test_new_history_page_waits_for_progress_interval_without_fake_receipt(led, monkeypatch, cfg):
    event = seed(led)
    ready(monkeypatch, False)
    _dispatch(led, event, cfg)
    first = _latest_render(led)
    claim = _claim(first)
    assert _apply(led, envelopes.transport_begin(claim), cfg)["granted"]
    assert _apply(led, envelopes.transport_receipt(
        claim, "delivered", message_id="1234.000001"), cfg)["applied"]
    with led.db:
        led.db.execute("INSERT INTO messages(message_id,project_id,posted_at_ts,body_text,"
            "content_hash,body_state,first_seen) VALUES(4,1,?,'合成の追加履歴',?,'full',?)",
            (int(NOW) - 50, "4" * 64, NOW + 1))
        initial_import_state.classify_tx(led, 1, [4], NOW + 1)
        initial_import_state.emit_tx(led, 1, initial_import_state.load(led.db, 1), [4])
    event = led.db.execute("SELECT * FROM notify_outbox WHERE kind='patient_history' ORDER BY event_id DESC LIMIT 1").fetchone()
    _dispatch(led, event, cfg, now=NOW + 1)
    assert _latest_render(led)["delivery_id"] == first["delivery_id"]
    pending = led.db.execute("SELECT state,delivery_id FROM notification_intent_cards WHERE event_id=?", (event["event_id"],)).fetchone()
    assert tuple(pending) == ("pending", None)
    assert led.db.execute("SELECT state FROM notify_outbox WHERE event_id=?", (event["event_id"],)).fetchone()[0] != "accepted"
    notify_cards.sweep(led, cfg, now=NOW + 299)
    assert _latest_render(led)["delivery_id"] == first["delivery_id"]
    notify_cards.sweep(led, cfg, now=NOW + 300)
    assert _latest_render(led)["render_rev"] == first["render_rev"] + 1
    assert json.loads(_card(led)["ui_state"])["history_snapshot"]["saved"] == 4
    pending = led.db.execute("SELECT state,delivery_id FROM notification_intent_cards WHERE event_id=?", (event["event_id"],)).fetchone()
    assert pending["state"] == "pending" and pending["delivery_id"] == _latest_render(led)["delivery_id"]


def test_unknown_lineworks_start_never_creates_completion(led, monkeypatch):
    event = seed(led)
    ready(monkeypatch, False)
    _dispatch(led, event, LINEWORKS)
    render = _latest_render(led)
    claim = _claim(render)
    assert _apply(led, envelopes.transport_begin(claim))["granted"]
    assert _apply(led, envelopes.transport_receipt(claim, "unknown"))["applied"]
    captured(led)
    ready(monkeypatch)
    notify_cards.sweep(led, LINEWORKS, now=NOW + 301)
    assert led.db.execute("SELECT COUNT(*) FROM notification_cards").fetchone()[0] == 1
    assert _card(led)["delivery_state"] == "delivery_unknown"


def test_lineworks_start_and_complete_have_distinct_intents_and_remote_posts(led, monkeypatch):
    event = seed(led)
    ready(monkeypatch, False)
    _dispatch(led, event, LINEWORKS)
    _deliver(led, _latest_render(led))
    captured(led)
    ready(monkeypatch)
    notify_cards.sweep(led, LINEWORKS, now=NOW + 301)
    next_event = led.db.execute("SELECT * FROM notify_outbox WHERE kind='patient_history' "
                                "AND state='pending' ORDER BY event_id DESC LIMIT 1").fetchone()
    assert next_event is not None and next_event["event_id"] != event["event_id"]
    _dispatch(led, next_event, LINEWORKS, now=NOW + 301)
    cards = led.db.execute("SELECT * FROM notification_cards ORDER BY card_id").fetchall()
    assert len(cards) == 2
    assert [json.loads(card["anchor_key"])["phase"] for card in cards] == ["start", "complete"]
    assert _latest_render(led, cards[1]["card_id"])["op"] == "create"


def test_held_or_unknown_analysis_is_never_complete(led, monkeypatch):
    event = seed(led)
    captured(led)
    monkeypatch.setattr(notify_history.extraction_progress, "message_progress", lambda db, row, **kw:
        {"state": "attention", "completed": 0, "total": 1} if row["message_id"] == 1 else None)
    _dispatch(led, event)
    snap = json.loads(_card(led)["ui_state"])["history_snapshot"]
    assert snap["phase"] == "analyzing" and snap["analyzed"] == 0 and snap["held"] == 1 and snap["unknown"] == 2


def test_render_reads_adopted_snapshot_without_rescanning_members(led, monkeypatch):
    event = seed(led)
    ready(monkeypatch)
    _dispatch(led, event)
    monkeypatch.setattr(notify_history, "_members", lambda *a, **kw: pytest.fail("render rescanned history"))
    assert notify_render.card_content(led.db, _card(led), cfg=CFG)["shown_kind"] == "cohort_ids"


def test_existing_urgency_contract_rejects_duplicates_stale_family_and_rule_override(led, monkeypatch):
    from notify_testkit import _extract, _llm_extract
    event = seed(led)
    led.db.execute("UPDATE messages SET body_text='呼吸困難のため至急受診します。' WHERE message_id IN (1,2)")
    led.db.execute("UPDATE messages SET body_text='家族が呼吸困難のため至急受診します。' WHERE message_id=3")
    led.db.commit()
    _extract(led, 1, {"urgency": "high"})
    _extract(led, 1, {"urgency": "high"})
    _extract(led, 2, {"urgency": "high"}, stale=True)
    _extract(led, 3, {"urgency": "high"})
    ready(monkeypatch)
    _dispatch(led, event)
    assert json.loads(_card(led)["ui_state"])["history_snapshot"]["high"] == 1
    _llm_extract(led, 1, {"urgency": "routine"})
    assert notify_render.source_fingerprint(led.db, _card(led)) != json.loads(_card(led)["ui_state"])["history_source_fp"]
    notify_cards.sweep(led, CFG, now=NOW + 301)
    assert json.loads(_card(led)["ui_state"])["history_snapshot"]["high"] == 0


def test_live_reply_to_history_root_creates_canonical_root_only_when_needed(led):
    from notify_testkit import _intent, _msg
    seed(led, 1)
    assert not led.db.execute("SELECT 1 FROM notification_cards").fetchone()
    _msg(led, 10, parent=1, ts=int(NOW) + 1)
    with led.db:
        initial_import_state.classify_tx(led, 1, [10], NOW)
    event = _intent(led, payload={"message_ids": [10]})
    _dispatch(led, event, {**CFG, "notify": {**CFG["notify"], "reply_cards": True}})
    cards = led.db.execute("SELECT kind,root_message_id,anchor_key FROM notification_cards ORDER BY card_id").fetchall()
    assert len(cards) == 2 and all(card["kind"] == "thread" for card in cards)
    assert {card["root_message_id"] for card in cards} == {1}
    assert json.loads(cards[1]["anchor_key"])["source"] == 10


def test_extraction_callbacks_only_mark_dirty_until_one_bounded_adoption(led, monkeypatch):
    event = seed(led, 100)
    ready(monkeypatch, False)
    _dispatch(led, event)
    before = _latest_render(led)
    with monkeypatch.context() as patch:
        patch.setattr(notify_history, '_members', lambda *a, **kw: pytest.fail('callback rescanned cohort'))
        patch.setattr(notify_history, 'fingerprint', lambda *a, **kw: pytest.fail('callback fingerprinted cohort'))
        for mid in range(1, 101):
            notify_cards.rerender_message_cards(led, CFG, 1, mid, now=NOW + 1)
        notify_cards.sweep(led, CFG, now=NOW + 299)
    assert _latest_render(led)['delivery_id'] == before['delivery_id']
    captured(led)
    ready(monkeypatch)
    notify_cards.sweep(led, CFG, now=NOW + 300)
    assert json.loads(_card(led)['ui_state'])['history_latest']['phase'] == 'complete'


def test_completed_delivered_cohort_skips_scans_but_real_source_edit_invalidates(led, monkeypatch):
    from ingest_testkit import _message
    event = seed(led, 1)
    captured(led)
    ready(monkeypatch)
    _dispatch(led, event)
    render = _latest_render(led)
    _begin(led, render)
    _receipt(led, render, '0000000000000001', message_id='stable')
    with monkeypatch.context() as patch:
        patch.setattr(notify_history, '_members', lambda *a, **kw: pytest.fail('stable completed cohort rescanned'))
        notify_cards.sweep(led, CFG, now=NOW + 10000)
    led.save_messages([_message(mid=1, body='合成の訂正本文', posted_at='2026-09-24T09:00:00+09:00')], project_id=1)
    assert json.loads(_card(led)['ui_state'])['history_source_dirty'] is True
    notify_cards.sweep(led, CFG, now=NOW + 10001)
    assert _latest_render(led)['render_rev'] > render['render_rev']


def test_initial_history_high_has_counts_but_no_individual_urgent_delivery_proof(led, monkeypatch):
    import notify_urgent
    from notify_testkit import _llm_extract
    event = seed(led, 1)
    with led.db:
        led.db.execute("UPDATE messages SET body_text='本人が急変し至急確認が必要です。' WHERE message_id=1")
    _llm_extract(led, 1, {'urgency': 'high', 'urgency_evidence': ['本人が急変し至急確認が必要です。']})
    ready(monkeypatch)
    _dispatch(led, event)
    cfg = {**CFG, 'urgency_escalation': {'mode': 'on', 'source': 'llm', 'room_cooldown_min': 1}}
    candidate, reason = notify_urgent._eligible(led, cfg, notify_urgent.settings(cfg), 1, NOW)
    assert candidate is None and reason == 'initial_delivery_unproven'
    assert json.loads(_card(led)['ui_state'])['history_snapshot']['high'] == 1
    assert not led.db.execute("SELECT 1 FROM notify_outbox WHERE kind IN ('new_messages','urgent_notice')").fetchone()


def test_late_history_member_reopens_completed_summary_without_reopening_capture(led, monkeypatch):
    from semantic_testkit import _message
    event = seed(led, 1)
    captured(led)
    ready(monkeypatch)
    _dispatch(led, event)
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    _receipt(led, render, f"{1:016x}")
    led.save_messages([_message(2)], project_id=1)
    assert initial_import_state.load(led.db, 1)["capture_phase"] == "complete"
    assert json.loads(_card(led)["ui_state"])["history_source_dirty"]
    notify_cards.sweep(led, CFG, now=NOW + 1)
    assert json.loads(_card(led)["ui_state"])["history_snapshot"]["saved"] == 2


def test_sweep_reserves_live_due_capacity_and_rotates_history_updates(led, monkeypatch):
    ready(monkeypatch, False)
    for pid in range(1, 4):
        _patient(led, pid=pid)
        led.db.execute("INSERT INTO messages(message_id,project_id,posted_at_ts,body_text,content_hash,body_state,first_seen) "
                       "VALUES(?,?,?,'合成履歴',?,'full',?)", (pid, pid, int(NOW)-1, f"{pid:064x}", NOW-10))
        with led.db:
            initial_import_state.start_tx(led, pid, int(NOW), NOW-20, boundary_source="server")
            initial_import_state.classify_tx(led, pid, [pid], NOW)
        event = led.db.execute("SELECT * FROM notify_outbox WHERE kind='patient_history' AND project_id=? ORDER BY event_id LIMIT 1", (pid,)).fetchone()
        _dispatch(led, event)
        render = _latest_render(led, pid)
        assert _begin(led, render, n=pid)["granted"]
        _receipt(led, render, f"{pid:016x}", n=pid+10, message_id=f"m-{pid}")
        state = initial_import_state.load(led.db, pid)
        state["capture_phase"] = "complete"
        state["capture_proof"] = {"v":1,"scope":history_walk.scope(pid,state["cohort_id"]),"digest":"a"*64}
        led.db.execute("UPDATE patients SET initial_import=? WHERE project_id=?", (json.dumps(state), pid))
        led.db.commit()
    led.outbox_add("new_messages", 99, {"message_ids": [99]})
    ready(monkeypatch)
    notify_cards.sweep(led, CFG, now=NOW+301)
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders WHERE render_rev=2").fetchone()[0] == 2
    notify_cards.sweep(led, CFG, now=NOW+302)
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders WHERE render_rev=2").fetchone()[0] == 3


@pytest.mark.parametrize("wait", [None, "intent_cards_issued", "intent_cards_unsettled", "stale"])
def test_delivered_history_analysis_wait_is_processing_but_unknown_is_attention(led, monkeypatch, wait):
    import notify_scope
    event = seed(led, 1)
    captured(led)
    ready(monkeypatch, False)
    _dispatch(led, event)
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    _receipt(led, render, f"{1:016x}")
    # A new progress intent binds to the already-delivered cohort identity.
    eid = led.outbox_add("patient_history", 1, {"source": "initial_history"})
    progress = {} if wait is None else {"wait": {"reason": "intent_cards_issued" if wait == "stale" else wait,
                                               "at": NOW-1 if wait == "stale" else NOW}}
    led.db.execute("UPDATE notify_outbox SET created_at=?,updated_at=?,progress=? WHERE event_id=?",
                   (NOW-30000, NOW, json.dumps(progress), eid))
    led.db.execute("INSERT INTO notification_intent_cards(event_id,card_id,coverage) VALUES(?,1,?)", (eid, json.dumps([initial_import_state.load(led.db,1)["cohort_id"]])))
    led.db.commit()
    status = notify_scope.pending_breakdown(led.db, NOW)
    assert status["by_reason"]["initial_history_analysis"]["count"] == 1
    assert status["attention"] == 0 and status["oldest_age_s"] is None
    led.db.execute("UPDATE notification_cards SET delivery_state='delivery_unknown' WHERE card_id=1")
    status = notify_scope.pending_breakdown(led.db, NOW)
    assert status["attention"] == 1
    assert "initial_history_analysis" not in status["by_reason"]
