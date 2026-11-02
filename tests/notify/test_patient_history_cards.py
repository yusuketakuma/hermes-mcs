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
                            _intent, _patient, _receipt, _seed_thread, led, pinned_clock)
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
    parts = rendered["parts"]
    assert parts["containers"][1]["rule"] is True
    assert notify_render.display_text(parts).count(notify_render.SECTION_RULE) == 1
    from adapters.discord.cards import _zones
    assert len(_zones(rendered, str)[0]) == 2
    from adapters.slack.presentation import render_parts
    assert [block["type"] for block in render_parts(parts, card_kind="patient_history")][:3] == [
        "header", "divider", "section"]
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


@pytest.mark.parametrize('cfg',[CFG,SLACK])
def test_unprovided_root_body_is_explicitly_not_analyzed_but_can_finish(led,monkeypatch,cfg):
    event = seed(led,1)
    led.db.execute("UPDATE messages SET body_state='unknown',body_text='',content_hash=? WHERE message_id=1", ('a'*64,))
    led.db.commit()
    captured(led)
    monkeypatch.setattr(notify_history.extraction_progress,'message_progress',lambda *a,**kw: pytest.fail('unprovided root was sent to analysis progress'))
    _dispatch(led,event,cfg)
    snapshot = json.loads(_card(led)['ui_state'])['history_snapshot']
    assert snapshot['no_body'] == 1 and snapshot['analyzed'] == 0 and snapshot['unknown'] == 0
    assert snapshot['phase'] == 'complete'
    text = json.dumps(spec(led),ensure_ascii=False)
    assert '本文未提供の親投稿 1件（解析対象外）' in text
    assert '初回履歴の取込み完了' in text and '保存・解析完了' not in text


def test_unknown_reply_and_snippet_are_never_no_body_or_analyzed(led,monkeypatch):
    event = seed(led)
    with led.db:
        led.db.execute("UPDATE messages SET body_state='unknown',parent_id=1 WHERE message_id=2")
        led.db.execute("UPDATE messages SET body_state='snippet' WHERE message_id=3")
    captured(led)
    seen = []
    monkeypatch.setattr(notify_history.extraction_progress,'message_progress',lambda db,row,**kw: seen.append(row['message_id']) or None)
    _dispatch(led,event)
    snapshot = json.loads(_card(led)['ui_state'])['history_snapshot']
    assert snapshot['no_body'] == 0 and snapshot['analyzed'] == 0 and snapshot['unknown'] == 3
    assert snapshot['phase'] == 'analyzing' and seen == [1,2,3]


def test_later_full_body_reopens_no_body_summary_and_invalidates_queued_completion(led,monkeypatch):
    from ingest_testkit import _message
    event = seed(led,1)
    with led.db:
        led.db.execute("UPDATE messages SET body_state='unknown',body_text='',content_hash=? WHERE message_id=1", ('a'*64,))
    captured(led)
    ready(monkeypatch,False)
    _dispatch(led,event)
    original = _latest_render(led)
    assert json.loads(_card(led)['ui_state'])['history_snapshot']['phase'] == 'complete'
    led.save_messages([_message(mid=1,body='合成の後着本文',posted_at='2026-09-24T09:00:00+09:00')],project_id=1)
    assert json.loads(_card(led)['ui_state'])['history_source_dirty']
    assert not _begin(led,original)['granted']
    current = json.loads(_card(led)['ui_state'])['history_snapshot']
    assert current['no_body'] == 0 and current['analyzed'] == 0 and current['phase'] == 'analyzing'
    ready(monkeypatch)
    notify_cards.sweep(led,CFG,now=NOW+301)
    assert json.loads(_card(led)['ui_state'])['history_latest']['phase'] == 'complete'


def test_lineworks_no_body_completion_remains_two_posts_after_later_full_body(led,monkeypatch):
    from ingest_testkit import _message
    event = seed(led,1)
    with led.db:
        led.db.execute("UPDATE messages SET body_state='unknown',body_text='',content_hash=? WHERE message_id=1", ('a'*64,))
    captured(led)
    ready(monkeypatch,False)
    _dispatch(led,event,LINEWORKS)
    _deliver(led,_latest_render(led))
    notify_cards.sweep(led,LINEWORKS,now=NOW+301)
    complete = led.db.execute("SELECT * FROM notify_outbox WHERE kind='patient_history' AND state='pending' ORDER BY event_id DESC LIMIT 1").fetchone()
    assert complete is not None
    _dispatch(led,complete,LINEWORKS,now=NOW+301)
    final_render = _latest_render(led,2)
    claim = _claim(final_render,attempt='c'*16)
    assert _apply(led,envelopes.transport_begin(claim))['granted']
    assert _apply(led,envelopes.transport_receipt(claim,'delivered',message_id='lw:'+'d'*32))['applied']
    final_snapshot = json.loads(_card(led,2)['ui_state'])['history_snapshot']
    assert final_snapshot['no_body'] == 1 and final_snapshot['analyzed'] == 0
    led.save_messages([_message(mid=1,body='合成の後着本文',posted_at='2026-09-24T09:00:00+09:00')],project_id=1)
    notify_cards.sweep(led,LINEWORKS,now=NOW+302)
    assert json.loads(_card(led,2)['ui_state'])['history_latest']['phase'] == 'analyzing'
    ready(monkeypatch)
    notify_cards.rerender_message_cards(led,LINEWORKS,1,1,now=NOW+303)
    notify_cards.sweep(led,LINEWORKS,now=NOW+603)
    for card in led.db.execute("SELECT * FROM notification_cards WHERE kind='patient_history'"):
        ui = json.loads(card['ui_state'])
        assert ui['history_latest']['no_body'] == 0 and ui['history_latest']['phase'] == 'complete'
        assert not notify_history.refresh_needed(led.db,card,NOW+604)
    assert led.db.execute("SELECT COUNT(*) FROM notification_cards WHERE kind='patient_history'").fetchone()[0] == 2
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0] == 2
    assert json.loads(_card(led,2)['ui_state'])['history_snapshot'] == final_snapshot


@pytest.mark.parametrize('normal_due',[False,True])
@pytest.mark.parametrize('held',[False,True])
def test_sweep_limits_heavy_refresh_attempts_even_when_no_spec_is_issued_and_rotates_due_cards(led,monkeypatch,normal_due,held):
    ready(monkeypatch,False)
    for pid in range(1,6):
        _patient(led,pid=pid)
        led.db.execute("INSERT INTO messages(message_id,project_id,posted_at_ts,body_text,content_hash,body_state,first_seen) VALUES(?,?,?,'合成履歴',?,'full',?)",
                       (pid,pid,int(NOW)-1,f'{pid:064x}',NOW-10))
        with led.db:
            initial_import_state.start_tx(led,pid,int(NOW),NOW-20,boundary_source='server')
            initial_import_state.classify_tx(led,pid,[pid],NOW)
        event = led.db.execute("SELECT * FROM notify_outbox WHERE kind='patient_history' AND project_id=? ORDER BY event_id LIMIT 1", (pid,)).fetchone()
        _dispatch(led,event)
        render = _latest_render(led,pid)
        if held:
            led.db.execute("UPDATE notification_renders SET state='held' WHERE delivery_id=?", (render['delivery_id'],))
            led.db.commit()
        else:
            assert _begin(led,render,n=pid)['granted']
            assert _receipt(led,render,f'{pid:016x}',n=pid+10,message_id=f'm-{pid}')['applied']
    if normal_due:
        led.outbox_add('new_messages',99,{'message_ids':[99]})
    observed = []
    original = notify_history.snapshot
    def counted(db,card,cfg,now):
        observed.append(card['card_id'])
        return original(db,card,cfg,now)
    monkeypatch.setattr(notify_history,'snapshot',counted)
    notify_cards.sweep(led,CFG,now=NOW+301)
    assert observed == [1,2]
    assert _card(led,3)['updated_at'] < _card(led,1)['updated_at']
    notify_cards.sweep(led,CFG,now=NOW+602)
    assert observed == [1,2,3,4]
    notify_cards.sweep(led,CFG,now=NOW+903)
    assert observed == [1,2,3,4,5,1]
    assert led.db.execute('SELECT COUNT(*) FROM notification_renders').fetchone()[0] == 5


def test_sweep_expired_normal_defer_is_not_starved_by_four_due_history_cards(led,monkeypatch):
    ready(monkeypatch,False)
    for pid in range(1,5):
        _patient(led,pid=pid)
        led.db.execute("INSERT INTO messages(message_id,project_id,posted_at_ts,body_text,content_hash,body_state,first_seen) VALUES(?,?,?,'合成履歴',?,'full',?)",
                       (pid,pid,int(NOW)-1,f'{pid:064x}',NOW-10))
        with led.db:
            initial_import_state.start_tx(led,pid,int(NOW),NOW-20,boundary_source='server')
            initial_import_state.classify_tx(led,pid,[pid],NOW)
        event = led.db.execute("SELECT * FROM notify_outbox WHERE kind='patient_history' AND project_id=? ORDER BY event_id LIMIT 1", (pid,)).fetchone()
        _dispatch(led,event)
        led.db.execute("UPDATE notification_renders SET state='held' WHERE card_id=?", (pid,))
        led.db.commit()
    _seed_thread(led,pid=99,root=99,mids=(99,))
    _dispatch(led,_intent(led,pid=99,payload={'message_ids':[99]}))
    normal = led.db.execute("SELECT card_id FROM notification_cards WHERE project_id=99 AND kind!='patient_history'").fetchone()[0]
    with led.db:
        led.db.execute("UPDATE notification_cards SET updated_at=? WHERE card_id=?", (NOW+100,normal))
        led.db.execute("INSERT INTO notification_triage(card_id,state,defer_until,updated_at) VALUES(?,'deferred',?,?)", (normal,NOW+1,NOW))
    observed = []
    original = notify_history.snapshot
    def counted(db,card,cfg,now):
        observed.append(card['card_id'])
        return original(db,card,cfg,now)
    monkeypatch.setattr(notify_history,'snapshot',counted)
    monkeypatch.setattr(notify_history,'refresh_needed',lambda *args:True)
    first = notify_cards.sweep(led,CFG,limit=3,now=NOW+301)
    assert first['scanned'] == 4  # One ordinary card plus three cheap history checks.
    assert observed == [1,2]
    triage = led.db.execute('SELECT state,revision FROM notification_triage WHERE card_id=?', (normal,)).fetchone()
    assert tuple(triage) == ('open',1)
    second = notify_cards.sweep(led,CFG,limit=3,now=NOW+602)
    assert second['scanned'] == 4 and observed == [1,2,3,4]
    assert led.db.execute('SELECT revision FROM notification_triage WHERE card_id=?', (normal,)).fetchone()[0] == 1


@pytest.mark.parametrize('cfg',[CFG,SLACK,LINEWORKS])
def test_history_counts_multiple_root_and_reply_attachments_without_delivering_file_parts(led,monkeypatch,cfg):
    from ingest_testkit import _att, _message
    event = seed(led,2)
    root = _message(mid=1,body='完全合成の親投稿',posted_at='2026-09-01T09:00:00+09:00')
    root.attachments = [_att('shared-file','synthetic-root-report.txt'),
                        _att('root-image','synthetic-root-image.png')]
    root.files_present = True
    reply = _message(mid=2,parent_id=1,body='完全合成の返信',posted_at='2026-09-02T09:00:00+09:00')
    reply.attachments = [_att('shared-file','synthetic-reply-report.txt')]
    reply.files_present = True
    root.replies = [reply]
    led.save_messages([root],project_id=1)
    before = [tuple(row) for row in led.db.execute(
        'SELECT message_id,file_id,name,state FROM attachments ORDER BY message_id,file_id')]
    assert before == [(1,'root-image','synthetic-root-image.png','pending'),
                      (1,'shared-file','synthetic-root-report.txt','pending'),
                      (2,'shared-file','synthetic-reply-report.txt','pending')]
    captured(led)
    ready(monkeypatch)
    _dispatch(led,event,cfg)
    snapshot = json.loads(_card(led)['ui_state'])['history_snapshot']
    assert snapshot['saved'] == snapshot['analyzed'] == 2
    assert snapshot['attachments'] == 3  # The same file ID in two source posts is two associations.
    rendered = spec(led)
    encoded = json.dumps(rendered,ensure_ascii=False)
    assert '添付 3件（MCSで確認）' in encoded
    assert [part['kind'] for part in rendered['parts']['manifest']] == ['card']
    assert not any(a.name in encoded for a in [*root.attachments,*reply.attachments])
    assert [tuple(row) for row in led.db.execute(
        'SELECT message_id,file_id,name,state FROM attachments ORDER BY message_id,file_id')] == before
    assert not led.db.execute("SELECT 1 FROM notify_outbox WHERE kind='attachment_followup'").fetchone()


@pytest.mark.parametrize('invalid',['deleted','foreign_patient','malformed_artifact'])
def test_history_high_count_excludes_deleted_foreign_and_unreadable_artifacts(led,monkeypatch,invalid):
    import clinical_urgency
    from notify_testkit import _llm_extract
    event = seed(led,2)
    evidence = '本人が急変し至急確認が必要です。'
    with led.db:
        led.db.execute('UPDATE messages SET body_text=? WHERE message_id IN (1,2)',(evidence,))
    _llm_extract(led,1,{'urgency':'high','urgency_evidence':[evidence]})
    bad = _llm_extract(led,2,{'urgency':'high','urgency_evidence':[evidence]})
    with led.db:
        if invalid == 'deleted':
            led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=2")
        elif invalid == 'foreign_patient':
            _patient(led,pid=2,name='完全合成の別患者')
            # Upgraded ledgers can retain older mismatches under the shadow guard.
            led.db.execute("UPDATE ledger_relation_guards SET mode='shadow' WHERE relation='artifacts'")
            led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?',(bad,))
        else:
            led.db.execute("UPDATE artifacts SET content='{broken' WHERE artifact_id=?",(bad,))
    assert clinical_urgency.message_urgency(led.db,1) == 'llm'
    assert clinical_urgency.message_urgency(led.db,2) is None
    captured(led)
    ready(monkeypatch)
    _dispatch(led,event)
    snapshot = json.loads(_card(led)['ui_state'])['history_snapshot']
    assert snapshot['saved'] == 2 and snapshot['high'] == 1
    assert snapshot['deleted'] == int(invalid == 'deleted')
    rendered = json.dumps(spec(led),ensure_ascii=False)
    assert '現行抽出で緊急度が高い投稿 1件（MCSで確認）' in rendered
    assert '現行抽出で緊急度が高い投稿 2件' not in rendered


def test_completed_history_checks_do_not_consume_the_two_render_slots(led, monkeypatch):
    ready(monkeypatch)
    for pid in range(1,5):
        _patient(led,pid=pid)
        led.db.execute("INSERT INTO messages(message_id,project_id,posted_at_ts,body_text,content_hash,body_state,first_seen) VALUES(?,?,?,'合成履歴',?,'full',?)",
                       (pid,pid,int(NOW)-1,f'{pid:064x}',NOW-10))
        with led.db:
            initial_import_state.start_tx(led,pid,int(NOW),NOW-20,boundary_source='server')
            initial_import_state.classify_tx(led,pid,[pid],NOW)
            state = initial_import_state.load(led.db,pid)
            state.update(capture_phase='complete',capture_proof={
                'v':1,'scope':history_walk.scope(pid,state['cohort_id']),'digest':'a'*64})
            led.db.execute('UPDATE patients SET initial_import=? WHERE project_id=?',(json.dumps(state),pid))
        event = led.db.execute("SELECT * FROM notify_outbox WHERE kind='patient_history' AND project_id=? ORDER BY event_id LIMIT 1",(pid,)).fetchone()
        _dispatch(led,event)
        render = _latest_render(led,pid)
        assert _begin(led,render,n=pid)['granted']
        _receipt(led,render,f'{pid:016x}',n=pid+10,message_id=f'm-{pid}')
    # The first three have complete, settled history and truly need no refresh.
    for pid in range(1,4):
        assert not notify_history.refresh_needed(led.db,_card(led,pid),NOW+301)
    with led.db:
        ui=json.loads(_card(led,4)['ui_state'])
        ui['history_dirty_since']=NOW
        led.db.execute('UPDATE notification_cards SET ui_state=? WHERE card_id=4',(json.dumps(ui),))
    observed=[]
    original=notify_history.snapshot
    def counted(db,card,cfg,now):
        observed.append(card['card_id'])
        return original(db,card,cfg,now)
    monkeypatch.setattr(notify_history,'snapshot',counted)
    notify_cards.sweep(led,CFG,limit=4,now=NOW+301)
    assert observed == [4]
    assert notify_history.refresh_needed(led.db,_card(led,4),NOW+302) is False
