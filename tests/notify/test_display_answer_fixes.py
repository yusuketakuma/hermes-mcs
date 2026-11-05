"""Private answers: digest/unacked/meds wording (v1.0.26 display audit).
Synthetic temp ledger only."""
from __future__ import annotations

import notify_digest
import notify_views
import notify_render
from notify_testkit import (CFG, NOW, _add_request, _at, _delivered_card,
                            _msg, _patient, led, pinned_clock)
import clinical_urgency

__all__ = ["led", "pinned_clock"]

T = _at("2026-10-01", 8, 5)


def _digest(led, scope="all", name=None, now=T):
    view = notify_digest.view(led.db, CFG, scope, name=name, now=now)
    return notify_render.parts_text(view["parts"], "plain")


def test_mine_counts_only_the_clickers_tasks(led):
    _patient(led, 1)
    _msg(led, 100)
    _add_request(led, "自分の確認", assignee="合成 担当", due="2026-10-30")
    for _ in range(3):
        _add_request(led, "他人の確認", assignee="合成 別人", due="2026-09-01")
    text = _digest(led, "担当", name="合成 担当")
    assert "未完了タスク 1件" in text
    assert "期限切れ" not in text


def test_unfetched_stamps_are_not_reported_as_no_reaction(led):
    _patient(led, 1)
    _msg(led, 100, ts=int(T - 60))
    led.db.execute("UPDATE messages SET first_seen=?", (T - 60,))
    led.db.commit()
    text = _digest(led)
    assert "本人反応なし" not in text
    assert "MCS 本人スタンプ: 0投稿" not in text
    assert "スタンプ未取得 1投稿" in text
    assert "取得不正 0" not in text


def test_digest_scope_help_and_errors_use_japanese_keys():
    assert "project" not in notify_digest.SCOPE_HELP
    assert "all" not in notify_digest.SCOPE_HELP
    error = notify_digest.parse_scope("患者:合成")
    assert error.startswith("患者の指定が不正です")
    assert "患者IDは数字で指定してください" in error
    assert "project" not in error
    assert notify_digest._scope_label(notify_digest.parse_scope("患者:1,2"),
                                      None) == "患者ID 1,2"


def test_digest_counts_drop_zero_parts_and_repeated_headers(led, monkeypatch):
    import structured_view
    _patient(led, 1)
    _msg(led, 100, ts=int(T - 60))
    led.db.execute("UPDATE messages SET first_seen=?", (T - 60,))
    led.db.commit()
    _add_request(led, "確認", due="2026-09-01")
    _add_request(led, "確認2", due="2026-09-02")
    monkeypatch.setattr(clinical_urgency, 'message_urgency', lambda db, m: "llm")
    monkeypatch.setattr(structured_view, "urgency_qc_suffix", lambda db, m: "")
    text = _digest(led)
    assert "未完了タスク 2件（期限切れ 2）" in text
    assert "本日期限 0" not in text
    assert "期限切れタスク 2件（1人）: " in text
    # the section heading already says 緊急度高; rows carry only the mark
    assert "🚨 緊急度高" not in text.split("■ 緊急度高", 1)[1]
    assert notify_digest._ago(59 * 60) == "1時間未満"


def test_addressed_window_sits_in_its_own_heading(led):
    _patient(led, 1)
    _msg(led, 100, ts=int(T - 60))
    text = _digest(led, "日数:3")
    assert "直近3日" in text
    assert "■ 自分宛で返信の記録なし（直近7日）\n0件（メンション未判定）" in text


def test_unacked_head_says_unfetched_instead_of_zero(led):
    _delivered_card(led)
    view = notify_views.unacked_view(led.db, "discord", now=NOW)
    assert not any("本人反応あり 0件" in h for h in view["head"])
    assert "MCSで本人反応: 未取得あり" in view["head"]


def test_meds_without_dictionary_explains_once_with_the_real_reason(led):
    _patient(led, 1)
    _msg(led, 100)
    import structured_view
    entries = ([("キラナ 5mg", None)], [])
    orig = structured_view.medication_entries
    structured_view.medication_entries = lambda db, mid, **kwargs: entries
    try:
        view = notify_views.meds_view(led.db, 1, 100, cfg=CFG)
    finally:
        structured_view.medication_entries = orig
    assert notify_views._DICTIONARY_STATE["unconfigured"] in view["head"]
    assert view["items"] == [{"project_id": 1, "text": "・キラナ 5mg"}]
    assert view["head"][0].startswith("09-24 08:40 ")
