"""完全合成の再確認候補で対象・引用・時点と通知形式を検証する。"""
import pytest

import alert_view
import notify_render
from notify_testkit import NOW, _msg, _patient, led

__all__ = ["led"]


def test_grounded_reason_and_separate_post_observation_times(led):
    _patient(led)
    _msg(led, 100, body="本人は息苦しいと話しています。担当者へ連絡します。", ts=NOW - 3600)
    checked = {"project_id": 1, "message_id": 100, "subject": "patient",
               "reasons": ["本人は息苦しいと話しています。"], "observed_at": NOW}
    parts = alert_view.render_parts(checked, led.db)
    text = alert_view.render_text(checked, led.db)
    assert "患者本人" in text and "引用: 本人は息苦しいと話しています。" in text
    assert "投稿 " in text and "AI判定 " in text
    assert "記録がないことは未対応・業務完了を意味しません" in text
    # reason before sender/time; the patient name appears once
    assert text.index("引用: ") < text.index("投稿 ")
    assert parts["preview_text"].startswith("🚨緊急度高 ")
    assert "MCSで開く" in text and "投稿 #100" in text
    assert len(text) < 600
    assert "🚨" in notify_render.parts_text(parts, "discord")
    from adapters.slack.cards import render_parts
    blocks = render_parts(parts, silent=True)
    assert blocks[0]["type"] == "header"


@pytest.mark.parametrize("subject", [None, "family", "unknown", "other", "invalid"])
def test_unconfirmed_subject_never_labelled_patient(led, subject):
    _patient(led)
    _msg(led, 100)
    text = alert_view.render_text({"project_id": 1, "message_id": 100, "subject": subject}, led.db)
    assert "対象人物は未確認" in text and "患者本人" not in text


@pytest.mark.parametrize("body,state,pid,reasons", [
    ("合成理由。合成理由。", "full", 1, ["合成理由。"]),
    ("合成理由。", "deleted", 1, ["合成理由。"]),
    ("合成理由。", "full", 2, ["合成理由。"]),
    ("合成理由。", "full", 1, ["捏造理由", "", None]),
])
def test_unavailable_or_cross_scope_reason_never_quoted(led, body, state, pid, reasons):
    _patient(led)
    _msg(led, 100, body=body)
    with led.db:
        led.db.execute("UPDATE messages SET body_state=? WHERE message_id=100", (state,))
    parts = alert_view.render_parts({"project_id": pid, "message_id": 100, "reasons": reasons}, led.db)
    assert not any(p["type"] == "quote" for p in parts["containers"])
    assert "緊急理由の引用は未取得" in alert_view.render_text(
        {"project_id": pid, "message_id": 100, "reasons": reasons}, led.db)


def test_quotes_deduplicated_bounded_and_never_whole_body_fallback(led):
    _patient(led)
    _msg(led, 100, body="理由一。理由二。理由三。PRIVATE-SYNTHETIC-CANARY")
    parts = alert_view.render_parts({"project_id": 1, "message_id": 100,
                                    "reasons": ["理由一。", "理由一。", "理由二。", "理由三。"]}, led.db)
    assert [p["text"] for p in parts["containers"] if p["type"] == "quote"] == ["理由一。", "理由二。"]
    assert "CANARY" not in notify_render.parts_text(parts)


def test_long_reason_never_truncates_negation_or_subject_tail(led):
    _patient(led)
    quote = "合成記述" * 50 + "本人の症状ではありません。"
    _msg(led, 100, body=quote)
    parts = alert_view.render_parts({"project_id": 1, "message_id": 100, "reasons": [quote]}, led.db)
    assert not any(p["type"] == "quote" for p in parts["containers"])
    assert "緊急理由の引用は未取得" in notify_render.parts_text(parts)


@pytest.mark.parametrize("timestamp", [True, float("nan"), -1, 1e30, None])
def test_unknown_observation_time_is_explicit(timestamp):
    text = alert_view.render_text({"project_id": 1, "message_id": 100, "observed_at": timestamp})
    assert "AI判定 不明" in text


def test_source_preview_fields_come_from_same_patient_message_not_checked_overrides(led):
    _patient(led)
    _msg(led, 100, body="本人の息苦しさを確認します。PRIVATE-SYNTHETIC-TAIL")
    with led.db:
        led.db.execute("UPDATE messages SET sender_name='合成発信者',organization='合成所属',posted_at='2026-10-06T08:30:00+09:00' WHERE message_id=100")
    parts = alert_view.render_parts({"project_id": 1, "message_id": 100,
        "subject": "patient", "reasons": ["本人の息苦しさを確認します。"],
        "source_sender": "UNTRUSTED-SENDER", "source_date": "UNTRUSTED-DATE"}, led.db)
    assert "合成発信者（合成所属） / 10-06 08:30" in parts["preview_text"]
    assert "合成発信者（合成所属） · 投稿 " in notify_render.parts_text(parts)
    assert "本人の息苦しさを確認します。" in parts["preview_text"]
    assert "UNTRUSTED" not in notify_render.parts_text(parts)
    assert "PRIVATE-SYNTHETIC-TAIL" not in parts["preview_text"]
