"""Shared answer wording (v1.0.26 display audit): rejection codes,
confirm previews, list overflow and long-answer notices."""
from __future__ import annotations

import pytest

from adapters.common import text

CODES = ("card_missing", "signal_not_open", "signal_not_found", "signal_corrupt",
         "project_mismatch", "project_not_found", "payload_changed",
         "some_future_code")


@pytest.mark.parametrize("code", CODES)
def test_rejections_never_show_internal_codes(code):
    msg = text.ja({"outcome": "rejected", "error": code})
    assert code not in msg and "拒否されました" not in msg
    assert "未対応" not in msg


def test_crash_is_one_fixed_sentence_that_claims_nothing_about_the_change():
    a = text.ja({"outcome": "rejected", "error": "crash:KeyError"})
    b = text.ja({"outcome": "rejected", "error": "crash:ValueError"})
    assert a == b and "KeyError" not in a and "crash" not in a
    assert "反映しました" not in a and "反映していません" not in a


def test_dismiss_preview_drops_the_internal_key_and_states_the_effect():
    out = text.preview_text("dismiss", {
        "signal_key": "med_change:42:9001:合成薬", "reason": "対応済み",
        "reason_code": "already_handled"}, markdown=False)
    assert "med_change" not in out and "signal" not in out
    assert out.count("対応済み") == 1
    assert "名前と理由を記録" in out and "10分" in out


def test_task_preview_states_the_effect():
    out = text.preview_text("request", {"title": "残薬確認", "reason": "理由"},
                            markdown=False)
    assert "名前と理由を記録" in out


def test_list_overflow_says_what_was_kept_and_names_hidden_groups():
    items = [{"project_id": 1, "group": "患者A", "text": f"・行{i}"}
             for i in range(text.LIST_SHOW)]
    items += [{"project_id": 1, "group": "患者B", "text": "・隠れた行"}]
    out = "\n".join(text.list_messages(
        {"list": {"title": "一覧", "items": items, "more": 0}},
        lambda pid: True, markdown=False))
    assert f"他1件（ここでは{text.LIST_SHOW}件まで表示）: 患者B" in out


def test_long_list_notice_does_not_send_staff_to_mcs():
    items = [{"project_id": 1, "text": "・" + "あ" * 1800} for _ in range(6)]
    out = "\n".join(text.list_messages(
        {"list": {"title": "一覧", "items": items}}, lambda pid: True,
        markdown=False))
    assert "長文のためここまで表示しています" in out
    assert "MCSで確認" not in out
    body = "\n".join(text.body_messages({"title": "本文", "body": "い\n" * 5000},
                                        plain=True))
    assert "続きはMCSで確認してください" in body


def test_digest_modal_labels_use_japanese_keys():
    query, name = text.modal_fields("digest", clicker="合成 職員")
    assert "mine" not in query["label"] and "all" not in query["label"]
    assert query["default"] == "全体"
    assert "mine" not in name["label"]
