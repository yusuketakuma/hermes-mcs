"""Synthetic private replies preserve literal text and disclose bounded output."""
import asyncio

import pytest

from adapters.common import text
from test_lineworks_adapter import SCOPE, world


@pytest.mark.parametrize("action", ["body", "tasks", "list"])
def test_private_view_uses_plain_headings_and_keeps_literal_special_text(tmp_path, action):
    w = world(tmp_path, action=action)
    literal = '**合成** <@synthetic> & [リンク](https://example.invalid/synthetic) 🧪'
    result = {"outcome": "applied", "action": action, "title": "合成見出し", "body": literal,
              "tasks": [{"request_id": 1, "project_id": 1, "status": "open", "title": literal}],
              "list": {"title": "合成見出し", "items": [
                  {"project_id": 1, "text": literal},
                  {"project_id": 2, "text": "SYNTHETIC-FOREIGN-PROJECT"}]}}
    rec = {"kind": "action", "origin": SCOPE, "actor": "lineworks:40029600:operator"}
    asyncio.run(w.actions._deliver_followup("operator", rec, result))
    assert w.client.calls
    messages = [call[1]["text"] for call in w.client.calls]
    assert messages[0].splitlines()[0] == (
        "📋 タスク（このスレッド）" if action == "tasks" else "合成見出し")
    assert literal in "\n".join(messages)
    assert "SYNTHETIC-FOREIGN-PROJECT" not in "\n".join(messages)
    assert all(call[2] == {"user_id": "operator", "channel_id": None} for call in w.client.calls)
    assert all(len(message) <= 2000 for message in messages)


@pytest.mark.parametrize("action", ["body", "digest", "list"])
@pytest.mark.parametrize("markdown", [False, True])
def test_long_private_answer_retains_its_budget_and_announces_omission(action, markdown):
    body = "合" * (text.BODY_CHUNK * (text.BODY_MAX_CHUNKS + 2))
    result = {"outcome": "applied", "action": action, "body": body, "text": body,
              "title": "合成見出し", "list": {"title": "合成見出し", "notes": [body]}}
    answer = text.view_answer(result, lambda _pid: True, markdown=markdown)
    assert len(answer) == text.BODY_MAX_CHUNKS
    assert all(len(message) <= 2000 for message, _ in answer)
    assert "長文のため" in answer[-1][0]
    assert "MCSで確認" in answer[-1][0]


def test_legacy_chunk_api_keeps_its_explicit_prefix_contract():
    body = "合" * (text.BODY_CHUNK * 6)
    assert text.split_body(body) == ["合" * text.BODY_CHUNK] * text.BODY_MAX_CHUNKS
    assert "".join(text.split_body(body, max_chunks=None)) == body
