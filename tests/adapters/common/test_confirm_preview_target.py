"""Confirm previews name the render-pinned patient and post (display only)."""
import notify_action_context
import pytest

from adapters.common.text import preview_text

TASK = {"title": "残薬を確認", "reason": "通知カードからタスク作成"}
DISMISS = {"reason": "対応済み", "reason_code": "handled"}
REPORT = {"field": "meds", "reason": "薬名が違う"}


@pytest.mark.parametrize("action,payload", [("request", TASK), ("dismiss", DISMISS), ("report", REPORT)])
def test_every_preview_names_its_target_right_under_the_heading(action, payload):
    text = preview_text(action, payload, False, target="合成 花子 · 10-03 09:00の投稿")
    lines = text.splitlines()
    assert lines[1] == "対象: 合成 花子 · 10-03 09:00の投稿"
    assert "対象:" not in preview_text(action, payload, False)          # unknown target: no line


def test_discord_markdown_in_a_patient_name_stays_literal():
    text = preview_text("request", TASK, True, target="*合成*_花子_ · 投稿（日時不明）")
    assert "対象: ＊合成＊＿花子＿" in text   # escape_md swaps markup for full-width look-alikes


def test_the_card_context_pins_the_target(tmp_path):
    from notify_testkit import _msg, _patient
    from ledger import Ledger
    led = Ledger(str(tmp_path / "m.db"))
    try:
        _patient(led, 1, name="合成 花子")
        _msg(led, 100, pid=1)
        card = {"kind": "thread", "root_message_id": 100, "project_id": 1}
        ctx = notify_action_context.render_context(led.db, card)
        assert ctx["target"].startswith("合成 花子 · ") and ctx["target"].endswith("の投稿")
    finally:
        led.close()
