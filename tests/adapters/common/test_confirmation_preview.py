"""Human confirmation shows all accepted synthetic fields without active markup."""
import copy

import pytest

from adapters.common import text


@pytest.mark.parametrize("markdown", [False, True])
@pytest.mark.parametrize("action, payload", [
    ("request", {"title": "合" * 996 + "内容末尾", "reason": "理" * 1996 + "理由末尾",
                 "assignee": "担" * 116 + "担当末尾", "due_date": "2026-10-18"}),
    ("report", {"field": "meds", "reason": "合" * 496 + "報告末尾"}),
    ("dismiss", {"reason_code": "other", "reason": "合" * 1996 + "却下末尾"}),
])
def test_preview_keeps_every_accepted_field_and_leaves_payload_unchanged(action, payload, markdown):
    before = copy.deepcopy(payload)
    preview = text.preview_text(action, payload, markdown, target="合成患者・合成投稿")
    for field in ("title", "reason", "assignee", "due_date"):
        if field in payload:
            assert payload[field] in preview
    assert payload == before
    assert "合成患者・合成投稿" in preview and "10分" in preview


@pytest.mark.parametrize("action", ["request", "report", "dismiss"])
def test_discord_preview_input_is_literal_and_plain_transports_keep_original(action):
    value = "## 合成見出し\n||合成の確認事項||\n[合成リンク](https://example.invalid/synthetic) <@1001>"
    payload = {"title": value, "reason": value, "assignee": value, "field": "meds"}
    plain = text.preview_text(action, payload, False)
    discord = text.preview_text(action, payload, True)
    assert value in plain
    assert text.escape_md(value) in discord
    assert "||" not in discord and "[合成リンク]" not in discord
    assert "\n## " not in discord and "<@1001>" in discord
    assert discord.startswith("**確認 — ")
