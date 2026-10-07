"""Notification previews identify the grounded primary content without changing full evidence."""
import json

import pytest

from adapters.common import spec as contract
import notify_render
from notify_testkit import CFG, _dispatch, _intent, _latest_render, _seed_thread, led as led


@pytest.mark.parametrize("state", ["pending", "failed", "ready"])
def test_card_without_thread_seals_patient_sender_and_primary_request(led, monkeypatch, state):
    _seed_thread(led)
    raw = "いつもお世話になっています。\n朝の薬の変更を確認してください。\nありがとうございます。"
    with led.db:
        led.db.execute("UPDATE patients SET patient_name='合成患者' WHERE project_id=1")
        led.db.execute("UPDATE messages SET body_text=?,sender_name='合成看護師',organization='合成事業所',posted_at='2026-10-06T08:30:00+09:00' WHERE message_id=101", (raw,))
    monkeypatch.setattr(notify_render, "_extraction_failed", lambda *_args: state == "failed")
    monkeypatch.setattr(notify_render, "_structured_block", lambda *_args: {
        "type": "text", "text": "📋 要約\n・薬剤候補（未確認）: 合成薬\n・依頼候補（未確認）: 朝の薬の変更確認"}
        if state == "ready" else None)
    cfg = {**CFG, "notify": {**CFG["notify"], "card_thread": False}}
    assert _dispatch(led, _intent(led), cfg=cfg)["dispatched"]
    spec = json.loads(_latest_render(led)["spec_json"])
    preview = spec["parts"]["preview_text"]
    assert "朝の薬の変更" in preview[:160]
    assert "合成患者" in preview and "合成看護師" in preview
    assert "合成事業所" in preview and "10-06 08:30" in preview
    if state == "ready":
        assert "依頼候補（未確認）" in preview and "投稿の自動要約" in preview
    else:
        assert ("要約処理待ち" if state == "pending" else "要約作成失敗") in preview
        assert "原文" in preview and "朝の薬の変更を確認してください。" in preview
    assert "thread_body_parts" not in spec["parts"]
    contract.validate(spec)
    spec["parts"]["preview_text"] = "異なる合成プレビュー"
    with pytest.raises(ValueError, match="card_part_sha256"):
        contract.validate(spec)


@pytest.mark.parametrize("body_state", ["deleted", "stub"])
def test_deleted_or_unfetched_body_never_leaks_retained_source_in_preview(led, body_state):
    _seed_thread(led)
    with led.db:
        led.db.execute("UPDATE messages SET body_state=?,body_text='PRIVATE-RETAINED-CANARY' WHERE message_id=101",
                       (body_state,))
    _dispatch(led, _intent(led))
    preview = json.loads(_latest_render(led)["spec_json"])["parts"]["preview_text"]
    assert "PRIVATE-RETAINED-CANARY" not in preview
    assert ("削除済み" if body_state == "deleted" else "未取得") in preview


def test_question_preview_keeps_verbatim_sentence_without_adding_a_clinical_answer():
    raw = "合成の前置きです。\n朝の薬は変更されていますか？\n本人確認の後に回答します。"
    assert notify_render._preview_post(raw) == "朝の薬は変更されていますか？"


def test_unknown_organization_is_explicit_and_not_inferred_from_patient(led):
    _seed_thread(led)
    with led.db:
        led.db.execute("UPDATE messages SET organization=NULL WHERE message_id=101")
    _dispatch(led, _intent(led))
    preview = json.loads(_latest_render(led)["spec_json"])["parts"]["preview_text"]
    assert "所属未取得" in preview


def test_urgent_source_preview_uses_the_same_five_fields_without_claiming_completion(led):
    import notify_urgent
    _seed_thread(led)
    with led.db:
        led.db.execute("UPDATE messages SET sender_name='合成発信者',organization='合成所属',"
                       "posted_at='2026-10-06T08:30:00+09:00' WHERE message_id=101")
    text = notify_urgent.render_text({"message_id": 101, "project_id": 1, "stage": "initial"}, led.db)
    assert "合成発信者（合成所属） / 10-06 08:30" in text
    assert "要約処理待ち" in text and "未対応・業務完了の判定ではありません" in text


def test_long_source_fields_still_put_primary_fact_before_sender_and_time(led, monkeypatch):
    _seed_thread(led)
    with led.db:
        led.db.execute("UPDATE patients SET patient_name=? WHERE project_id=1", ("患" * 30,))
        led.db.execute("UPDATE messages SET sender_name=?,organization=?,posted_at='2026-10-06T08:30:00+09:00' WHERE message_id=101", ("発" * 24, "所" * 24))
    monkeypatch.setattr(notify_render, "_structured_block", lambda *_args: {
        "type": "text", "text": "📋 要約\n・依頼候補（未確認）: 朝の薬の変更確認"})
    _dispatch(led, _intent(led))
    preview = json.loads(_latest_render(led)["spec_json"])["parts"]["preview_text"]
    assert "朝の薬の変更確認" in preview[:80]
    assert "発" * 24 in preview and "所" * 24 in preview and "10-06 08:30" in preview
    assert len(preview) <= 400


@pytest.mark.parametrize("filename", ["合成添付.txt", "架空資料" + "名" * 190 + ".png",
                                     "架空資料" + "名" * 240 + ".png"])
def test_long_attachment_caption_keeps_five_fields_within_wire_limit(led, monkeypatch, filename):
    _seed_thread(led)
    with led.db:
        led.db.execute("UPDATE patients SET patient_name=? WHERE project_id=1", ("患" * 30,))
        led.db.execute("UPDATE messages SET sender_name=?,organization=?,posted_at='2026-10-06T08:30:00+09:00' WHERE message_id=101", ("発" * 24, "所" * 24))
        led.db.execute("INSERT INTO attachments(message_id,name,state,local_path,sha256,bytes) VALUES(101,?,'downloaded','/synthetic/unused',?,1)", (filename, "ab" * 32))
    monkeypatch.setattr(notify_render, "_structured_block", lambda *_args: {
        "type": "text", "text": "📋 要約\n・依頼候補（未確認）: 主要確認事項" + "要" * 500})
    _dispatch(led, _intent(led))
    spec = json.loads(_latest_render(led)["spec_json"])
    part = next(item for item in spec["parts"]["manifest"] if item["kind"] == "attachment_part")
    caption = part["caption"]
    assert "患" * 30 in caption and "主要確認事項" in caption[:80]
    assert "発" * 24 in caption and "所" * 24 in caption and "10-06 08:30" in caption
    assert len(caption) <= 300
    if len(filename) < 100:
        assert filename in caption and part["name"] == filename
    else:
        assert "架空資料" in caption and caption.endswith("….png")
        assert part["name"].endswith(".png") and len(part["name"]) <= 200
    contract.validate(spec)
