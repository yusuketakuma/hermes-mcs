"""Signal-card evidence changes must invalidate previously rendered actions."""

import json

import pytest

import notify_cards
import notify_flush
import notify_render
from notify_testkit import (
    CFG, NOW, ORIGIN, _begin, _card, _dispatch, _extract, _intent,
    _latest_render, _msg, _notif, _patient, _receipt, _seed_thread,
    _signal_row, _token_for, led,
)

__all__ = ["led"]  # shared isolated-ledger fixture


def test_signal_evidence_cannot_read_another_patient(led):
    _patient(led, 1)
    _patient(led, 2, name="合成患者B")
    _msg(led, 200, pid=2, body="SYNTHETIC-OTHER-PATIENT")
    sig = {"project_id": 1, "type": "med_followup", "note": "合成候補",
           "evidence": {"message_ids": [200]}}
    face = notify_render._signal_compact(led.db, 1, [sig])
    body = notify_render._signal_body(led.db, sig)
    text = notify_flush._signal_text(
        led, {"text": "候補\n場所", "project_id": 1}, sig)
    assert "SYNTHETIC-OTHER-PATIENT" not in json.dumps(face) + body + text


def test_thread_manifest_cannot_read_another_patient(led):
    _patient(led, 1)
    _patient(led, 2, name="合成患者B")
    _msg(led, 100, body="OWN-PATIENT")
    _msg(led, 200, pid=2, parent=100, body="SYNTHETIC-OTHER-PATIENT")
    card = {"kind": "thread", "root_message_id": 100, "project_id": 1}
    _, body = notify_render._card_body_text(led.db, card, {"shown": "[100,200]"})
    assert "OWN-PATIENT" in body and "SYNTHETIC-OTHER-PATIENT" not in body
    before = notify_render._source_fp(led.db, card)
    led.db.execute("UPDATE messages SET content_hash='foreign-edit' WHERE message_id=200")
    assert notify_render._source_fp(led.db, card) == before


def test_late_extract_landing_bumps_thread_source_fp(led):
    """extract_llm lands behind the card (drain queue); the source fp
    must carry fact-readiness or a card rendered early would keep its
    v1-only 構造化 block forever. A stale-hash extraction does not
    count as ready — latest_fact_artifact would reject it too."""
    _seed_thread(led)
    card = {"kind": "thread", "root_message_id": 100, "project_id": 1}
    before = notify_render._source_fp(led.db, card)
    _extract(led, 100, {"v": 1, "symptoms": ["疼痛"]},
             kind="extract_llm", stale=True)
    assert notify_render._source_fp(led.db, card) == before
    _extract(led, 100, {"v": 1, "symptoms": ["疼痛"]}, kind="extract_llm")
    assert notify_render._source_fp(led.db, card) != before


def test_dispatch_does_not_follow_foreign_parent_or_event_member(led):
    _patient(led, 1)
    _patient(led, 2, name="合成患者B")
    _msg(led, 200, pid=2, body="OTHER")
    _msg(led, 100, pid=1, parent=200, body="OWN")
    _dispatch(led, _intent(led, payload={"message_ids": [100, 200]}))
    card = _card(led)
    assert card["project_id"] == 1 and card["root_message_id"] == 100
    assert led.db.execute("SELECT COUNT(*) FROM notification_cards").fetchone()[0] == 1
    content = notify_render._card_content(led.db, card)
    assert content["shown"] == [100]


def test_signal_card_rejects_foreign_artifact_and_context(led):
    _patient(led, 1)
    _patient(led, 2, name="合成患者B")
    _msg(led, 200, pid=2, body="OTHER")
    _signal_row(led, "foreign-signal", pid=2, mids=[200])
    _dispatch(led, _intent(led, "signal", payload={
        "signal_keys": ["foreign-signal"], "project_id": 1}))
    card = _card(led)
    assert notify_render._card_content(led.db, card)["shown"] == []
    assert notify_cards._render_context(led.db, card) == {}


def test_long_thread_title_keeps_spec_deliverable(led):
    from hermes_plugin.mcs_delivery import spec as spec_mod
    _patient(led, 1, name="合成患者名" * 40)
    _msg(led, 100)
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    spec = json.loads(_latest_render(led)["spec_json"])
    assert spec_mod.validate(spec) is spec


@pytest.mark.parametrize("raw", ["[]", '"string"', "1", "[" * 1200],
                         ids=["array", "string", "number", "recursive"])
def test_invalid_card_state_falls_back_safely(raw):
    assert notify_render._anchor_keys({"kind": "signal", "anchor_key": raw}) == []
    assert notify_render._page(raw, 3, default=2) == 2


@pytest.mark.parametrize(
    ("message_ids", "expected"),
    [([100, 101], "後の投稿"), ([100, "invalid"], "退院時の投稿")],
)
def test_signal_face_compact_body_selects_evidence(
        led, message_ids, expected):
    _patient(led)
    _msg(led, 100, body="最初の投稿")
    _msg(led, 101, body="後の投稿")
    _msg(led, 102, body="退院時の投稿")
    sig = {"project_id": 1, "type": "med_followup", "note": "合成候補",
           "evidence": {
        "message_ids": message_ids, "discharge_message_id": 102,
        "message_id": 100}}

    face = notify_render._signal_compact(led.db, 1, [sig])
    body = notify_render._signal_body(led.db, sig)

    assert expected in body
    assert all(other not in body for other in
               ("最初の投稿", "後の投稿", "退院時の投稿") if other != expected)
    face_text = json.dumps(face, ensure_ascii=False)
    assert "合成候補" in face_text
    assert all(t not in face_text for t in
               ("最初の投稿", "後の投稿", "退院時の投稿"))


def test_deleted_signal_evidence_differs_between_card_and_body(led):
    _patient(led)
    _msg(led, 100, body="消された本文")
    led.db.execute(
        "UPDATE messages SET body_state='deleted' WHERE message_id=100")
    sig = {"project_id": 1, "type": "med_followup", "note": "合成候補",
           "evidence": {"message_id": 100}}

    face = notify_render._signal_compact(led.db, 1, [sig])
    body = notify_render._signal_body(led.db, sig)

    assert "消された本文" not in json.dumps(face, ensure_ascii=False)
    assert "（削除済み）" in body and "消された本文" not in body


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_signal_evidence_change_invalidates_old_action(led, change):
    _patient(led)
    _msg(led, 100)
    _signal_row(led, "synthetic-signal", mids=[100])
    _dispatch(led, _intent(led, "signal", payload={
        "signal_keys": ["synthetic-signal"], "project_id": 1,
        "type": "med_followup"}))
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    attempt = _begin(led, render)
    _receipt(led, render, attempt["attempt_id"], message_id="mid-1")
    if change == "edit":
        led.db.execute("UPDATE messages SET content_hash='edited',body_text='更新済み' WHERE message_id=100")
    else:
        led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=100")
    led.db.commit()
    # The signal artifact has not been re-evaluated yet; its evidence
    # nevertheless changed and must invalidate the button immediately.
    request = _notif(_token_for(spec, "assign"))
    request["origin"] = ORIGIN
    result = notify_cards.apply_notification(led, request, CFG, now=NOW + 1)
    assert result["outcome"] == "rejected" and result["error"] == "stale_source"
