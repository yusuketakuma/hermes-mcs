"""完全合成の投稿で、テキスト通知とスレッド投稿の緊急度表記とスタンプ行を検証する。"""
import json

import notify_flush
import notify_render
import structured_view
from notify_testkit import NOW, _msg, _patient, led

__all__ = ["led"]


def _reactions(led, mid, reactions, error=None):
    content = {"reactions": {"value": reactions, "observed_at": NOW - 60}}
    led.db.execute("INSERT OR REPLACE INTO message_metadata "
                   "(message_id,source,content,checked_at,last_error) VALUES(?,?,?,?,?)",
                   (mid, "capture", json.dumps(content), NOW - 60, error))
    led.db.commit()


def _text_row(led, mid):
    row = notify_flush._msg_rows(led, {"message_ids": [mid]}, 1)[0]
    return notify_flush._fmt_row(led, {"project_id": 1}, {}, row, "", {})


def test_text_post_shows_urgency_once_and_omits_an_observed_zero(led, monkeypatch):
    _patient(led)
    _msg(led, 100, body="合成の本文")
    monkeypatch.setattr(structured_view, "structured_lines",
                        lambda db, mid, plain=False: ["🚨 緊急度高", "合成の要約"])
    monkeypatch.setattr(structured_view, "message_urgency", lambda db, mid: "llm")
    _reactions(led, 100, [])
    text = _text_row(led, 100)
    assert "⚠️" not in text and text.count("🚨") == 1
    assert "スタンプ" not in text


def test_stamp_row_keeps_counts_unfetched_and_failures(led):
    _patient(led)
    _msg(led, 100)
    assert notify_render.thread_stamp_row(led.db, 100, None) == "スタンプ 未取得"
    _reactions(led, 100, [])
    assert notify_render.thread_stamp_row(led.db, 100, None) == ""
    _reactions(led, 100, [], error="synthetic_error")
    assert notify_render.thread_stamp_row(led.db, 100, None).startswith("スタンプ なし")
    _reactions(led, 100, [{"type": "viewed", "count": 2, "self_reacted": False}])
    assert notify_render.thread_stamp_row(led.db, 100, None).startswith("スタンプ 👀2")
    assert "スタンプ 👀2" in _text_row(led, 100)


def test_thread_post_omits_an_observed_zero(led):
    _patient(led)
    _msg(led, 100, body="合成の本文")
    _reactions(led, 100, [])
    m = led.db.execute("SELECT * FROM messages WHERE message_id=100").fetchone()
    post = notify_render._message_post(led.db, 100, m, "合成職員")
    assert "スタンプ" not in post and "合成の本文" in post
