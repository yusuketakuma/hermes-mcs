"""Slack shows the patient-context views as Block Kit; other lists keep their text form."""
import pytest

from adapters.slack.presentation import context_list_blocks

VIEW = {"layout": "patient_context", "title": "合成患者 — 患者さんの全体像（1/2ページ）",
        "head": ["関係者: 看護師 合成看護師", "表示順: 標準"],
        "items": [{"group": "主病名", "project_id": 1, "quotes": ["合成疾患A"], "source": "10/01 看護師 #1", "date": "10/01",
                   "source_rest": "看護師 #1", "url": "https://www.medical-care.net/projects/medical/1"},
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


def test_source_date_is_a_link_to_the_room():
    blocks = context_list_blocks(VIEW, lambda pid: pid == 1)
    linked = [b for b in blocks if b["type"] == "context" and b["elements"][0]["type"] == "mrkdwn"]
    assert linked[0]["elements"][0]["text"] == "<https://www.medical-care.net/projects/medical/1|10/01>"
    assert linked[0]["elements"][1] == {"type": "plain_text", "text": "看護師 #1"}


@pytest.mark.parametrize("url", [
    "http://www.medical-care.net/projects/medical/1",      # not https
    "https://www.medical-care.net/a|b",                    # breaks <url|label>
    "https://www.medical-care.net/a b",                    # whitespace
    "https://www.medical-care.net/a\x01b",                 # control character
    "https://www.medical-care.net/<x>",                    # markup delimiters
    "https:///projects/medical/1",                         # no host
    "https://[::1",                                        # urlsplit raises ValueError
    "javascript:alert(1)", "", None, 7,
])
def test_unsafe_source_url_falls_back_to_the_plain_date_line(url):
    view = {**VIEW, "items": [{**VIEW["items"][0], "url": url}]}
    blocks = context_list_blocks(view, lambda pid: pid == 1)
    contexts = [b for b in blocks if b["type"] == "context"]
    assert all(e["type"] == "plain_text" for b in contexts for e in b["elements"])
    assert contexts[-1]["elements"][0]["text"] == "10/01 看護師 #1"
    assert str(url) not in str(blocks) or url == ""
