"""v1.0.26 sweep: Slack patient-context blocks and the shared list text."""
from adapters.common import text
from adapters.slack.cards import _CONTEXT_MAX, context_list_blocks

ROW = {"group": "前後14日の記録", "project_id": 1, "quotes": ["病名: 架空病A"], "date": "09/22",
       "source_rest": "sender #1", "url": "https://www.medical-care.net/projects/medical/1"}
VIEW = {"layout": "patient_context", "title": "合成患者", "head": [], "items": [ROW],
        "notes": ["履歴取得: 未完了（完了記録なし）"], "empty": "該当なし"}


def test_notes_are_divided_from_the_last_rows_source_line():
    blocks = context_list_blocks(VIEW, lambda pid: True)
    assert [b["type"] for b in blocks[-2:]] == ["divider", "context"]
    assert blocks[-1]["elements"][0]["text"] == "履歴取得: 未完了（完了記録なし）"
    empty = context_list_blocks({**VIEW, "items": []}, lambda pid: True)
    assert sum(b["type"] == "divider" for b in empty) == 0   # nothing to separate from


def test_source_rest_is_plain_text_and_split_not_cut():
    rest = "_看護_ 出来事の記載: ~10/1~ 予定" + "あ" * _CONTEXT_MAX
    blocks = context_list_blocks({**VIEW, "items": [{**ROW, "source_rest": rest}], "notes": []},
                                 lambda pid: True)
    ctx = [b for b in blocks if b["type"] == "context"]
    assert ctx[0]["elements"][0] == {"type": "mrkdwn", "text": f"<{ROW['url']}|09/22>"}
    assert ctx[0]["elements"][1]["type"] == "plain_text"
    shown = "".join(e["text"] for b in ctx for e in b["elements"][1 if b is ctx[0] else 0:])
    assert shown == rest                                      # markers literal, nothing dropped


def _plain(view):
    return "\n".join(text.list_messages({"list": view}, lambda pid: True, markdown=False))


def test_overflow_counts_the_rows_actually_shown():
    items = [{"project_id": 1, "text": f"・投稿{i}"} for i in range(10)]
    out = _plain({"title": "検索", "items": items, "more": 15})
    assert out.endswith("他15件（ここでは10件まで表示）")


def test_plain_notes_stand_apart_from_the_last_group():
    out = _plain({"title": "経過", "items": [{"project_id": 1, "group": "前後14日の記録",
                                             "text": "病名: 架空病A（09/22 sender #1）"}],
                  "notes": ["履歴取得: 未完了（完了記録なし）"]})
    assert out.endswith("病名: 架空病A（09/22 sender #1）\n\n履歴取得: 未完了（完了記録なし）")
