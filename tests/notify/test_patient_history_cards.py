"""Synthetic cohort cards on the existing journal, with no real transport."""
import json

import pytest

from adapters.common import envelopes
import initial_import_state
import history_walk
import notify_cards
import notify_history
import notify_render
import notify_scope
from notify_testkit import (CFG, NOW, _begin, _card, _dispatch, _latest_render,
                            _intent, _patient, _receipt, led, pinned_clock)
from notify_lineworks_testkit import LINEWORKS, _apply, _claim

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
    return initial_import_state.load(led.db, 1)


def legacy_render(led, state, cfg=CFG, *, pid=1):
    """Build a synthetic pre-upgrade sealed card for receipt and viewer compatibility."""
    payload = {"source": "initial_history", "cohort_id": state["cohort_id"], "message_ids": []}
    event = _intent(led, kind="patient_history", pid=pid, payload=payload)
    scope = notify_scope.delivery_scope(cfg)
    transport = notify_scope.active_transport(cfg)
    phase = "start" if transport == "lineworks" else "progress"
    cohort = state["cohort_id"]
    target = {"card_key": f"v1|patient_history|{pid}|{cohort}|{phase}",
              "kind": "patient_history", "project_id": pid, "coverage": [cohort],
              "anchor": {"source": "initial_history", "cohort_id": cohort, "phase": phase}}
    with led.db:
        cid = notify_cards._card_for(led.db, target, scope, NOW)
        led.db.execute("INSERT INTO notification_intent_batches(event_id,frozen_payload,payload_hash,"
                       "route_epoch,sealed_at,transport,scope_json) VALUES(?,?,?,?,?,?,?)",
                       (event["event_id"], event["payload"], notify_cards.payload_hash(payload), 1, NOW,
                        transport, notify_cards.canonical(scope).decode() if transport != "discord" else None))
        led.db.execute("INSERT INTO notification_intent_cards(event_id,card_id,coverage) VALUES(?,?,?)",
                       (event["event_id"], cid, json.dumps(target["coverage"])))
        card = notify_history.refresh(led.db, _card(led, cid), cfg, NOW)
        content = notify_render.card_content(led.db, card, cfg=cfg)
        card.update(notify_cards._generation_drift(card, content, cfg))
        plan, hold = notify_cards._build_spec(led.db, card, content, "create", 1, cfg, NOW)
        assert hold is None
        notify_cards._persist_render(led.db, card, plan, "create", 1, NOW)
    return event


def legacy_grant(led, render, n=1):
    """Model a send already granted by the previous runner."""
    aid = f"{n:016x}"
    with led.db:
        led.db.execute("INSERT INTO notification_delivery_attempts(attempt_id,delivery_id,"
                       "begin_command_id,state,worker_id,created_at) VALUES(?,?,?,'granted',?,?)",
                       (aid, render["delivery_id"], f"{n:08x}-0000-4000-8000-{n:012x}", "bb" * 8, NOW))
        led.db.execute("UPDATE notification_renders SET state='sending' WHERE delivery_id=?", (render["delivery_id"],))
    return aid


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
    legacy_render(led, event, cfg)
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
    assert _card(led)["layout"] == notify_cards.REPLY_LAYOUT
    assert not led.db.execute("SELECT 1 FROM notification_action_tokens").fetchone()


def test_5000_history_members_are_not_a_display_analysis_limit(led, monkeypatch):
    event = seed(led, 5000)
    counted = []
    monkeypatch.setattr(notify_history.extraction_progress, "message_progress", lambda db, row, **kw:
        counted.append(row["message_id"]) or {"state": "complete", "completed": 1, "total": 1})
    captured(led)
    legacy_render(led, event)
    ui = json.loads(_card(led)["ui_state"])
    assert ui["history_snapshot"]["saved"] == ui["history_snapshot"]["analyzed"] == 5000
    assert set(counted) == set(range(1, 5001))
    assert ui["history_snapshot"]["phase"] == "complete"
    assert len(json.dumps(ui)) < 3000


def test_source_edit_invalidates_queued_snapshot(led, monkeypatch):
    event = seed(led)
    ready(monkeypatch)
    legacy_render(led, event)
    before = _latest_render(led)
    led.db.execute("UPDATE messages SET content_hash=? WHERE message_id=1", ("e" * 64,))
    led.db.commit()
    assert notify_render.source_fingerprint(led.db, _card(led)) != _card(led)["source_fp"]
    assert not _begin(led, before)["granted"]


def test_history_in_a_new_messages_payload_cannot_create_individual_card(led):
    seed(led)
    assert notify_cards._resolve_targets(led.db, {"kind": "new_messages", "project_id": 1},
                                         {"message_ids": [1, 2]}) == []


def test_held_or_unknown_analysis_is_never_complete(led, monkeypatch):
    event = seed(led)
    captured(led)
    monkeypatch.setattr(notify_history.extraction_progress, "message_progress", lambda db, row, **kw:
        {"state": "attention", "completed": 0, "total": 1} if row["message_id"] == 1 else None)
    legacy_render(led, event)
    snap = json.loads(_card(led)["ui_state"])["history_snapshot"]
    assert snap["phase"] == "analyzing" and snap["analyzed"] == 0 and snap["held"] == 1 and snap["unknown"] == 2


def test_render_reads_adopted_snapshot_without_rescanning_members(led, monkeypatch):
    event = seed(led)
    ready(monkeypatch)
    legacy_render(led, event)
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
    legacy_render(led, event)
    assert json.loads(_card(led)["ui_state"])["history_snapshot"]["high"] == 1
    _llm_extract(led, 1, {"urgency": "routine"})
    assert notify_render.source_fingerprint(led.db, _card(led)) != json.loads(_card(led)["ui_state"])["history_source_fp"]
    notify_cards.sweep(led, CFG, now=NOW + 301)
    assert json.loads(_card(led)["ui_state"])["history_snapshot"]["high"] == 0


@pytest.mark.parametrize("cfg", [CFG, SLACK, LINEWORKS])
def test_live_reply_to_history_root_only_publishes_the_live_reply(led, cfg):
    from notify_testkit import _attach, _msg
    from notify_views import patient_search_view

    seed(led, 1)
    _msg(led, 2, parent=1, ts=int(NOW) - 1, body="合成の過去返信")
    _msg(led, 10, parent=1, ts=int(NOW) + 1, body="合成の新着返信")
    with led.db:
        initial_import_state.classify_tx(led, 1, [2, 10], NOW)
    for mid in (1, 2, 10):
        _attach(led, mid, name=f"synthetic-{mid}.txt")
    event = _intent(led, payload={"message_ids": [10]})
    assert _dispatch(led, event, cfg)["cards"] == 1
    assert _card(led)["root_message_id"] == 10
    rendered = json.dumps(spec(led), ensure_ascii=False)
    assert "合成の新着返信" in rendered and "synthetic-10.txt" in rendered
    for old in ("完全合成の履歴", "合成の過去返信", "synthetic-1.txt", "synthetic-2.txt"):
        assert old not in rendered
    original = patient_search_view(led.db, 1, "合成の過去返信", keep_message_id=True)
    assert [item["message_id"] for item in original["items"]] == [2]


@pytest.mark.parametrize("cfg", [CFG, SLACK, LINEWORKS])
def test_pending_legacy_undated_reply_stays_out_of_mixed_live_notification(led, cfg):
    from notify_testkit import _attach, _msg
    import notify_flush

    seed(led, 1)
    _msg(led, 10, body="合成の新着親")
    _msg(led, 11, parent=10, body="合成の日時不明返信")
    with led.db:
        initial_import_state.classify_tx(led, 1, [10, 11], NOW)
        # Simulate a 'live' role written by the earlier version.
        led.db.execute("UPDATE messages SET posted_at_ts=NULL WHERE message_id=11")
    aid = _attach(led, 11, name="synthetic-undated.txt")
    event = _intent(led, payload={"message_ids": [10, 11]})
    assert _dispatch(led, event, cfg)["cards"] == 1
    rendered = json.dumps(spec(led), ensure_ascii=False)
    assert "合成の新着親" in rendered
    assert "合成の日時不明返信" not in rendered and "synthetic-undated.txt" not in rendered
    assert [row["message_id"] for row in notify_flush._msg_rows(led, {"message_ids": [10, 11]}, 1)] == [10]
    followup = {"message_id": 11, "attachment_id": aid}
    with pytest.raises(notify_flush.semantic_send_gate.StaleSend, match="initial_history"):
        notify_flush._followup_files(led, followup, 1)
    event = _intent(led, kind="attachment_followup", payload=followup)
    with led.db:
        led.db.execute("UPDATE notify_outbox SET route='interactive' WHERE event_id=?", (event["event_id"],))
    assert _dispatch(led, event, cfg)["suppressed"]



@pytest.mark.parametrize("transport", ["discord", "lineworks"])
@pytest.mark.parametrize("card_thread", [False, True])
def test_unsent_history_parent_reroutes_only_the_live_reply(led, transport, card_thread):
    from notify_testkit import _msg

    cfg = ({**LINEWORKS, "notify": {**LINEWORKS["notify"], "card_thread": card_thread}}
           if transport == "lineworks" else
           {**CFG, "notify": {**CFG["notify"], "card_thread": card_thread}})
    _patient(led)
    _msg(led, 100, body="合成の履歴親", ts=int(NOW) - 10)
    _msg(led, 102, parent=100, body="合成の新規返信", ts=int(NOW) + 1)
    event = _intent(led, payload={"message_ids": [100, 102]})
    assert _dispatch(led, event, cfg)["cards"] == 1
    parent_card = _card(led)
    assert parent_card["root_message_id"] == 100
    parent_render = _latest_render(led)

    with led.db:
        initial_import_state.start_tx(led, 1, int(NOW), NOW, boundary_source="server")
        initial_import_state.classify_tx(led, 1, [100], NOW)
    if transport == "lineworks":
        denied = _apply(led, envelopes.transport_begin(_claim(parent_render)), cfg)
    else:
        denied = _begin(led, parent_render, cfg=cfg)
    assert not denied["granted"] and denied["error"] == "denied_history_notification_disabled"

    first = _dispatch(led, event, cfg)
    second = _dispatch(led, event, cfg)
    assert first["resealed"] and second["resealed"]
    cards = led.db.execute(
        "SELECT * FROM notification_cards WHERE kind='thread' ORDER BY card_id").fetchall()
    assert len(cards) == 2
    assert cards[0]["delivery_state"] == "revoked"
    assert cards[1]["root_message_id"] == 102
    assert cards[1]["delivery_state"] == "pending"
    renders = led.db.execute(
        "SELECT r.* FROM notification_renders r JOIN notification_cards c USING(card_id) "
        "WHERE c.root_message_id=102 ORDER BY r.render_rev").fetchall()
    assert len(renders) == 1 and renders[0]["state"] == "queued"
    shown = json.loads(led.db.execute(
        "SELECT shown FROM notification_view_manifests WHERE manifest_id=?",
        (renders[0]["manifest_id"],)).fetchone()[0])
    assert shown == [102]
    assert not led.db.execute("SELECT 1 FROM notification_delivery_attempts WHERE state IN ('granted','delivered','unknown')").fetchone()


@pytest.mark.parametrize("transport", ["discord", "lineworks"])
@pytest.mark.parametrize("card_thread", [False, True])
def test_unknown_parent_result_is_never_rerouted_or_resent(led, transport, card_thread):
    from notify_testkit import _msg

    cfg = ({**LINEWORKS, "notify": {**LINEWORKS["notify"], "card_thread": card_thread}}
           if transport == "lineworks" else
           {**CFG, "notify": {**CFG["notify"], "card_thread": card_thread}})
    _patient(led)
    _msg(led, 100, body="合成の履歴親", ts=int(NOW) - 10)
    _msg(led, 102, parent=100, body="合成の新規返信", ts=int(NOW) + 1)
    event = _intent(led, payload={"message_ids": [100, 102]})
    assert _dispatch(led, event, cfg)["cards"] == 1
    render = _latest_render(led)
    with led.db:
        initial_import_state.start_tx(led, 1, int(NOW), NOW, boundary_source="server")
        initial_import_state.classify_tx(led, 1, [100], NOW)
        led.db.execute("INSERT INTO notification_delivery_attempts(attempt_id,delivery_id,"
                       "begin_command_id,state,worker_id,created_at) VALUES(?,?,?,'unknown',?,?)",
                       ("u" * 16, render["delivery_id"],
                        "11111111-0000-4000-8000-111111111111", "bb" * 8, NOW))
        led.db.execute("UPDATE notification_renders SET state='unknown' WHERE delivery_id=?",
                       (render["delivery_id"],))
    outcome = _dispatch(led, event, cfg)
    assert not outcome.get("dispatched")
    assert led.db.execute("SELECT COUNT(*) c FROM notification_cards").fetchone()["c"] == 1
    assert led.db.execute("SELECT COUNT(*) c FROM notification_renders").fetchone()["c"] == 1


def test_queued_pre_join_thread_receives_no_send_grant_after_upgrade(led):
    from notify_testkit import _msg

    _patient(led)
    _msg(led, 1, ts=int(NOW) - 1)
    assert _dispatch(led, _intent(led, payload={"message_ids": [1]}))["cards"] == 1
    render = _latest_render(led)
    with led.db:
        initial_import_state.start_tx(led, 1, int(NOW), NOW, boundary_source="server")
    result = _begin(led, render)
    assert not result["granted"] and result["error"] == "denied_history_notification_disabled"
    assert _latest_render(led)["state"] == "cancelled"


def test_delivered_pre_join_thread_is_not_updated_by_sweep(led):
    from notify_lineworks_testkit import _deliver
    from notify_testkit import _msg

    _patient(led)
    _msg(led, 1, ts=int(NOW) - 1)
    assert _dispatch(led, _intent(led, payload={"message_ids": [1]}), LINEWORKS)["cards"] == 1
    render = _latest_render(led)
    _deliver(led, render)
    with led.db:
        initial_import_state.start_tx(led, 1, int(NOW), NOW, boundary_source="server")
        initial_import_state.classify_tx(led, 1, [1], NOW)
    notify_cards.sweep(led, LINEWORKS, now=NOW + 301)
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]
    assert _card(led)["delivery_state"] == "delivered"


def test_initial_history_high_has_counts_but_no_individual_urgent_delivery_proof(led, monkeypatch):
    import notify_urgent
    from notify_testkit import _llm_extract
    event = seed(led, 1)
    with led.db:
        led.db.execute("UPDATE messages SET body_text='本人が急変し至急確認が必要です。' WHERE message_id=1")
    _llm_extract(led, 1, {'urgency': 'high', 'urgency_evidence': ['本人が急変し至急確認が必要です。']})
    ready(monkeypatch)
    legacy_render(led, event)
    cfg = {**CFG, 'urgency_escalation': {'mode': 'on', 'source': 'llm', 'room_cooldown_min': 1}}
    candidate, reason = notify_urgent._eligible(led, cfg, notify_urgent.settings(cfg), 1, NOW)
    assert candidate is None and reason == 'initial_delivery_unproven'
    assert json.loads(_card(led)['ui_state'])['history_snapshot']['high'] == 1
    assert not led.db.execute("SELECT 1 FROM notify_outbox WHERE kind IN ('new_messages','urgent_notice')").fetchone()


@pytest.mark.parametrize("wait", [None, "intent_cards_issued", "intent_cards_unsettled", "stale"])
def test_delivered_history_analysis_wait_is_processing_but_unknown_is_attention(led, monkeypatch, wait):
    import notify_scope
    event = seed(led, 1)
    captured(led)
    ready(monkeypatch, False)
    legacy_render(led, event)
    render = _latest_render(led)
    legacy_grant(led, render)
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
    legacy_render(led,event,cfg)
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
    legacy_render(led,event)
    snapshot = json.loads(_card(led)['ui_state'])['history_snapshot']
    assert snapshot['no_body'] == 0 and snapshot['analyzed'] == 0 and snapshot['unknown'] == 3
    assert snapshot['phase'] == 'analyzing' and seen == [1,2,3]


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
    legacy_render(led,event,cfg)
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
    legacy_render(led,event)
    snapshot = json.loads(_card(led)['ui_state'])['history_snapshot']
    assert snapshot['saved'] == 2 and snapshot['high'] == 1
    assert snapshot['deleted'] == int(invalid == 'deleted')
    rendered = json.dumps(spec(led),ensure_ascii=False)
    assert '現行抽出で緊急度が高い投稿 1件（MCSで確認）' in rendered
    assert '現行抽出で緊急度が高い投稿 2件' not in rendered

@pytest.mark.parametrize("cfg", [CFG, SLACK, LINEWORKS])
def test_initial_history_is_captured_silently_and_old_unsealed_intents_are_suppressed(led, cfg):
    state = seed(led)
    assert state["saved_count"] == 3
    assert not led.db.execute("SELECT 1 FROM notify_outbox").fetchone()
    event = _intent(led, "patient_history", payload={"source": "initial_history", "cohort_id": state["cohort_id"]})
    assert _dispatch(led, event, cfg)["suppressed"]
    assert led.db.execute("SELECT state FROM notify_outbox").fetchone()[0] == "suppressed"
    assert not led.db.execute("SELECT 1 FROM notification_renders").fetchone()


@pytest.mark.parametrize("cfg", [CFG, SLACK, LINEWORKS])
def test_published_history_plan_cannot_gain_a_new_send_grant(led, cfg):
    state = seed(led)
    event = legacy_render(led, state, cfg)
    render = _latest_render(led)
    denied = _apply(led, envelopes.transport_begin(_claim(render)), cfg)
    assert not denied["granted"]
    assert denied["error"] == "denied_history_notification_disabled"
    assert _dispatch(led, event, cfg)["suppressed"]
    assert _latest_render(led)["state"] == "cancelled"
    notify_cards.sweep(led, cfg, now=NOW + 301)
    assert not led.db.execute("SELECT 1 FROM notification_renders WHERE state='queued'").fetchone()


@pytest.mark.parametrize("result", ["delivered", "unknown"])
def test_inflight_history_is_settled_by_real_receipt_and_never_sends_a_successor(led, result):
    state = seed(led)
    event = legacy_render(led, state)
    render = _latest_render(led)
    aid = legacy_grant(led, render)
    assert _dispatch(led, event)["skipped"]
    assert _latest_render(led)["state"] == "sending"
    assert _receipt(led, render, aid, result=result)["applied"]
    assert _latest_render(led)["state"] == result
    captured(led)
    notify_cards.sweep(led, CFG, now=NOW + 301)
    assert led.db.execute("SELECT count(*) FROM notification_renders").fetchone()[0] == 1
    assert led.db.execute("SELECT count(*) FROM notify_outbox").fetchone()[0] == 1


def test_late_history_member_reopens_completed_summary_without_reopening_capture(led, monkeypatch):
    from semantic_testkit import _message
    event = seed(led, 1)
    captured(led)
    ready(monkeypatch)
    legacy_render(led, event)
    render = _latest_render(led)
    legacy_grant(led, render)
    _receipt(led, render, f"{1:016x}")
    led.save_messages([_message(2, posted_at='2026-09-19T00:00:00+09:00')], project_id=1)
    assert initial_import_state.load(led.db, 1)["capture_phase"] == "complete"
    assert json.loads(_card(led)["ui_state"])["history_source_dirty"]
    notify_cards.sweep(led, CFG, now=NOW + 1)
    assert json.loads(_card(led)["ui_state"])["history_snapshot"]["saved"] == 2


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
        legacy_render(led, initial_import_state.load(led.db, pid), pid=pid)
        render = _latest_render(led,pid)
        if held:
            led.db.execute("UPDATE notification_renders SET state='held' WHERE delivery_id=?", (render['delivery_id'],))
            led.db.commit()
        else:
            legacy_grant(led,render,n=pid)
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
    from notify_testkit import _seed_thread

    ready(monkeypatch,False)
    for pid in range(1,5):
        _patient(led,pid=pid)
        led.db.execute("INSERT INTO messages(message_id,project_id,posted_at_ts,body_text,content_hash,body_state,first_seen) VALUES(?,?,?,'合成履歴',?,'full',?)",
                       (pid,pid,int(NOW)-1,f'{pid:064x}',NOW-10))
        with led.db:
            initial_import_state.start_tx(led,pid,int(NOW),NOW-20,boundary_source='server')
            initial_import_state.classify_tx(led,pid,[pid],NOW)
        legacy_render(led, initial_import_state.load(led.db, pid), pid=pid)
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
        legacy_render(led, initial_import_state.load(led.db, pid), pid=pid)
        render = _latest_render(led,pid)
        legacy_grant(led,render,n=pid)
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
