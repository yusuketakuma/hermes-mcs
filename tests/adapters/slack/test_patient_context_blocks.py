"""Slack shows the patient-context views as Block Kit; other lists keep their text form."""
from adapters.slack.cards import context_list_blocks

VIEW = {"layout": "patient_context", "title": "合成患者 — 患者さんの全体像（1/2ページ）",
        "head": ["関係者: 看護師 合成看護師", "表示順: 標準"],
        "items": [{"group": "主病名", "project_id": 1, "quotes": ["合成疾患A"], "source": "10/01 09:00 · 看護師 合成看護師 · 投稿#1"},
                  {"group": "主病名", "project_id": 1, "quotes": ["合成疾患<B>"], "source": "10/02 09:00 · 投稿#2"},
                  {"group": "服薬管理", "project_id": 1, "quotes": ["残薬3日分", "家族が管理"], "source": "10/03 · 投稿#3"},
                  {"group": "服薬管理", "project_id": 2, "quotes": ["別患者"], "source": "x"}],
        "notes": [], "empty": "この範囲で該当する記録は見つかりません"}


def test_patient_view_renders_header_groups_quotes_and_sources():
    blocks = context_list_blocks(VIEW, lambda pid: pid == 1)
    assert blocks[0] == {"type": "header", "text": {"type": "plain_text", "text": VIEW["title"]}}
    assert blocks[1]["type"] == "context" and "関係者" in blocks[1]["elements"][0]["text"]
    headings = [b["text"]["text"] for b in blocks if b["type"] == "section" and b["text"]["type"] == "mrkdwn"]
    assert headings == ["*主病名*", "*服薬管理*"]
    quotes = [b["text"]["text"] for b in blocks if b["type"] == "section" and b["text"]["type"] == "plain_text"]
    assert quotes == ["合成疾患A", "合成疾患<B>", "残薬3日分\n家族が管理"]   # plain text: never parsed as markup
    assert "別患者" not in str(blocks)                                       # out of scope, as in the text form
    assert sum(b["type"] == "divider" for b in blocks) == 1


def test_empty_other_and_oversized_views():
    empty = context_list_blocks({**VIEW, "items": []}, lambda pid: True)
    assert empty[-1]["text"]["text"] == VIEW["empty"]
    assert context_list_blocks({**VIEW, "layout": None}, lambda pid: True) is None
    many = {**VIEW, "items": [{"group": f"g{i}", "project_id": 1, "quotes": ["q"], "source": "s"} for i in range(20)]}
    assert context_list_blocks(many, lambda pid: True) is None              # over one message: text answer
