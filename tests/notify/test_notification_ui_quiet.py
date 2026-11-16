"""Notification UI, 2026-11-16 owner decisions: the processing header only
when something needs attention, a selectable preview privacy level, the
LINE WORKS Flex emphasis and the layout-7 face family. Synthetic only."""
from __future__ import annotations

import pytest

import notify_cards
import notify_render
import processing_route
from adapters.lineworks import cards as lineworks_cards
from adapters.lineworks import client as lineworks_client
from adapters.slack.presentation import render_parts

DONE = {"anthropic": "unknown", "openai": "unknown", "jev": "done", "local": "done"}


def _header(status, states=DONE, progress=None, prefix="09-24〜 · 2投稿"):
    label = notify_render.PROCESSING_STATUS_LABELS[status]
    visible = ["jev", "local"]
    header = {"type": "text", "provider_scope": "header", "provider_states": states,
              "visible_providers": visible, "processing_status": status, "provider_sequence": None}
    if progress is None:
        header["text"] = " · ".join(part for part in (
            prefix, label, "採用結果の処理順: 不明", processing_route.provider_text(states, visible)) if part)
    else:
        header.update(context_progress=progress, text=" · ".join(part for part in (
            prefix, label, processing_route.context_progress_text(progress),
            processing_route.provider_text(states, visible)) if part))
    return header


def _progress(percent=73, stage="sources", held=False):
    return {"proof_version": 1, "percent": percent, "stage": stage, "held": held,
            "source_sig": None if stage == "unknown" else "a" * 64, "observed_at": 1.0}


@pytest.mark.parametrize("header,expected", [
    (_header("complete"), "09-24〜 · 2投稿"),
    (_header("complete", progress=_progress()), "09-24〜 · 2投稿"),
    (_header("processing", progress=_progress(5, "local")), "09-24〜 · 2投稿 · ◐ 未完了"),
    (_header("preparing"), "09-24〜 · 2投稿 · ◐ 未完了"),
    # a problem keeps the whole compact header
    (_header("attention"), "09-24〜 · 2投稿 · ! 要確認 · Jev ✓  💻 ✓"),
    (_header("unknown"), "09-24〜 · 2投稿 · ? 状況未取得 · Jev ✓  💻 ✓"),
    (_header("complete", {**DONE, "jev": "attention"}), "09-24〜 · 2投稿 · ✓ 完了 · Jev !  💻 ✓"),
    (_header("complete", progress=_progress(45, held=True)),
     "09-24〜 · 2投稿 · ✓ 完了 · 👤 45% 保留 · Jev ✓  💻 ✓"),
    (_header("complete", progress=_progress(None, "unknown")),
     "09-24〜 · 2投稿 · ✓ 完了 · 👤 — · Jev ✓  💻 ✓"),
])
def test_header_shows_processing_only_when_something_needs_attention(header, expected):
    assert processing_route.compact_header(header) == expected


def test_a_quiet_header_without_prefix_leaves_no_empty_line_on_any_transport():
    header = _header("complete", prefix="")
    assert processing_route.compact_header(header) == ""
    parts = {"provider_states_version": 4, "processing_display_version": 2,
             "containers": [{"type": "heading", "text": "💬 合成 花子"}, header],
             "footer": [], "action_rows": []}
    assert notify_render.display_text(parts) == "💬 合成 花子"
    blocks = render_parts(parts, card_kind="thread")
    assert not any(block["type"] == "context" for block in blocks)
    assert lineworks_cards._provider_box(header) is None


def test_preview_privacy_masks_names_and_validates_the_setting():
    import mcs_config
    assert notify_render.masked_name("合成 花子") == "合○ 花○"
    assert notify_render.masked_name("project 12") == "project 12"
    assert notify_render.preview_privacy({"notify": {}}) == "full"
    assert notify_render.preview_privacy({"notify": {"preview_privacy": "minimal"}}) == "minimal"
    assert notify_render.preview_privacy({"notify": {"preview_privacy": "x"}}) == "full"
    assert not mcs_config.validate_notify({"preview_privacy": "initial"})
    assert any("preview_privacy" in e for e in mcs_config.validate_notify({"preview_privacy": "none"}))


def test_layout7_is_the_face_family_and_keeps_action_first_features():
    assert notify_cards.CARD_LAYOUT == notify_cards.FACE_LAYOUT == notify_render.QUIET_STAMP_LAYOUT == 7
    assert notify_cards.REPLY_LAYOUT == notify_render.ACTION_FIRST_LAYOUT == 6
    assert [notify_render.reply_layout(n) for n in (None, 1, 3, 4, 5, 6, 7)] == [
        False, False, False, True, True, True, False]


def _flex(nodes, *, header=None):
    bubble = {"type": "bubble", "body": {"type": "box", "layout": "vertical", "contents": nodes}}
    if header is not None:
        bubble = {"type": "bubble", "header": header, **{k: v for k, v in bubble.items() if k != "type"}}
    return {"type": "flex", "altText": "合成", "contents": bubble}


def test_lineworks_validator_accepts_only_the_drawn_emphasis():
    styled = [lineworks_cards._text_node("💬 合成 花子", lineworks_cards.HEADING_STYLE),
              lineworks_cards._text_node("10-01 09:40 合成 職員", lineworks_cards.META_STYLE),
              lineworks_cards.SEPARATOR, lineworks_cards._text_node("要約")]
    lineworks_client._validate_content(_flex(styled))
    band = {"type": "box", "layout": "vertical", "backgroundColor": lineworks_cards.URGENT_BAND,
            "contents": [lineworks_cards._text_node("🚨 合成 花子", lineworks_cards.BAND_STYLE)]}
    lineworks_client._validate_content(_flex(styled[1:], header=band))
    for bad in ({**styled[0], "color": "#123456"}, {**styled[0], "size": "xxl"},
                {"type": "separator", "margin": "lg"}):
        with pytest.raises(lineworks_client.ClientError):
            lineworks_client._validate_content(_flex([bad]))
    with pytest.raises(lineworks_client.ClientError):
        lineworks_client._validate_content(_flex(styled, header={**band, "backgroundColor": "#000000"}))


@pytest.mark.parametrize("privacy,expected", [
    ("full", "再確認候補（患者本人）: 緊急理由の引用は未取得"),
    ("initial", "再確認候補（患者本人）: 原文はMCSで確認"), ("minimal", "MCS: 再確認候補")])
def test_urgent_notice_and_history_previews_follow_preview_privacy(privacy, expected):
    import alert_view
    import notify_history
    cfg = {"notify": {"preview_privacy": privacy}}
    checked = {"project_id": None, "message_id": None, "subject": "patient"}
    preview = alert_view.render_parts(checked, None, cfg)["preview_text"]
    assert preview.startswith("🚨 緊急度高 ") and expected in preview
    assert ("発信者未取得" in preview) is (privacy == "full")
    history = notify_history._preview("合成 花子", "合成 花子 — 初回履歴を取得中", ["保存済み 1件"], cfg)
    assert history == {"full": "合成 花子 — 初回履歴を取得中\n保存済み 1件",
                       "initial": "合○ 花○ — 初回履歴を取得中\n保存済み 1件",
                       "minimal": "MCS: 初回履歴"}[privacy]
