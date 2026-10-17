"""v1.0.26 display audit: signal state wording, evidence-less alerts,
photo-only evidence, unknown dates, the urgency badge in previews and the
Slack urgent-notice link. Synthetic ledger only."""
import json

import alert_view
import notify_render
from adapters.slack import cards as slack_cards
from notify_testkit import (_dispatch, _extract, _msg, _patient, _seed_thread,
                            _signal_row, _spec, _intent, led)

__all__ = ["led"]


def _sig(led, key):
    return notify_render._latest_signals(led.db, [key], 1)[key]["content"]


def _preview(led, keys, kind="signal"):
    return notify_render.notification_preview(
        led.db, {"kind": kind, "project_id": 1}, {"shown": keys})


def test_open_signal_never_reads_unconfirmed_and_state_is_separated(led):
    _patient(led)
    _msg(led, 100)
    _signal_row(led, "s1", mids=[100])
    _signal_row(led, "s2", stype="request_overdue", state="resolved", mids=[100])
    face = "\n".join(c["text"] for c in notify_render._signal_compact(
        led.db, 1, [_sig(led, "s1"), _sig(led, "s2")], plain=True))
    body = notify_render._signal_body(led.db, _sig(led, "s1"))
    preview = _preview(led, ["s2"])
    assert "未確認" not in face + body + _preview(led, ["s1"])
    assert "・【期限超過】note s2 · 解消" in face and "）（" not in face
    assert "note s2 · 解消" in preview
    assert body.splitlines()[0].endswith("note s1")


def test_photo_only_evidence_post_is_kept_in_the_signal_body(led):
    _patient(led)
    _msg(led, 100, body="")
    _signal_row(led, "s1", mids=[100])
    body = notify_render._signal_body(led.db, _sig(led, "s1"))
    assert "↳ 患者A · " in body and "（本文なし）" in body


def test_registry_alert_body_names_the_patient(led):
    _patient(led)
    _signal_row(led, "s1", stype="request_overdue")
    body = notify_render._signal_body(led.db, _sig(led, "s1"))
    assert body.startswith("患者A · 【期限超過】")


def test_alert_without_a_source_post_claims_no_fetch_gap(led):
    _patient(led)
    _signal_row(led, "s1", stype="request_overdue")
    _signal_row(led, "s2", mids=[999])               # cited but not stored
    assert "未取得" not in _preview(led, ["s1"])
    assert _preview(led, ["s1"]).endswith("【期限超過】note s1")
    assert "発信者未取得" in _preview(led, ["s2"])


def test_thread_card_without_a_root_time_shows_no_question_marks(led):
    _seed_thread(led)
    led.db.execute("UPDATE messages SET posted_at=NULL")
    led.db.commit()
    _dispatch(led, _intent(led))
    texts = [c.get("text", "") for c in _spec(led)["parts"]["containers"]]
    assert not any("??" in t for t in texts)


def test_urgency_badge_is_not_repeated_in_the_preview(led):
    _patient(led)
    _msg(led, 100, body="本人が急変し至急確認が必要です。")
    _extract(led, 100, {"urgency": "high",
                        "urgency_evidence": ["本人が急変し至急確認が必要です"]},
             kind="extract_llm")
    preview = _preview(led, [100], kind="thread")
    assert preview.count("緊急度高") == 1 and "急変" in preview


def test_slack_urgent_notice_link_is_clickable(led):
    _patient(led)
    _msg(led, 100, body="本人が急変し至急確認が必要です。")
    parts = alert_view.render_parts(
        {"project_id": 1, "message_id": 100, "subject": "patient",
         "reasons": ["本人が急変し至急確認が必要です"]}, led.db)
    blocks = slack_cards.render_parts(parts)
    footer = [e for b in blocks if b["type"] == "context" for e in b["elements"]
              if "原本確認" in e["text"]]
    assert len(footer) == 1 and footer[0]["type"] == "mrkdwn"
    assert "<https://" in footer[0]["text"] and "|MCSで開く>" in footer[0]["text"]
    assert json.dumps(blocks).count("https://") == 1
