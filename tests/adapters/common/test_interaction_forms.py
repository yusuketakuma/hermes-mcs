"""Shared form fields and submitted values retain the runner command vocabulary."""
import copy

import pytest

from adapters.common import forms


def test_task_fields_with_and_without_roster():
    bare = forms.modal_fields("request", None, "佐藤")
    assert [f["id"] for f in bare] == ["task", "assignee", "due_date",
                                       "reason"]
    assert bare[-1]["required"] is False and bare[-1]["default"] == ""
    assert bare[1]["default"] == "佐藤" and bare[0]["default"] == ""
    roster = forms.modal_fields(
        "request", {"hint": "残薬確認", "staff": ["佐藤（みどり薬局）",
                                               "x" * 76]}, "佐藤")
    # Discord modals hold at most 5 components
    assert [f["id"] for f in roster] == ["task", "assignee_pick",
                                        "assignee", "due_date", "reason"]
    assert roster[0]["default"] == "残薬確認"
    # an over-long name can never become an option (Slack 75 chars)
    assert roster[1]["options"] == [("佐藤（みどり薬局）", "佐藤（みどり薬局）")]
    assert roster[1]["default"] == "佐藤（みどり薬局）"
    assert forms.modal_fields("defer") == []

@pytest.mark.parametrize("fields,expect", [
    ({"task": " 残薬確認 ", "assignee_pick": "佐藤（みどり薬局）"},
     {"title": "残薬確認", "assignee": "佐藤（みどり薬局）",
      "reason": forms.TASK_REASON}),
    ({"task": "t", "assignee": "手入力", "assignee_pick": "佐藤"},
     {"title": "t", "assignee": "手入力", "reason": forms.TASK_REASON}),
    ({"task": "t", "due_date": "2026-10-01"},
     {"title": "t", "due_date": "2026-10-01", "reason": forms.TASK_REASON}),
    ({"task": "t", "reason": " 家族から依頼 "},
     {"title": "t", "reason": "家族から依頼"}),
    # an old 依頼 form submitted across a restart still validates
    ({"title": "旧件名", "reason": "旧理由"},
     {"title": "旧件名", "reason": "旧理由"}),
    ({"task": ""}, "タスク内容の入力が必要です。"),
    ({"task": "t", "due_date": "2026-02-30"},
     "期限は YYYY-MM-DD 形式で入力してください。"),
    ({"task": "t", "assignee": "a" * 121},
     "担当者は120文字以内で入力してください。"),
])
def test_task_attrs(fields, expect):
    assert forms.task_attrs(copy.deepcopy(fields)) == expect

def test_feedback_attrs():
    assert forms.feedback_attrs({"field": "meds", "note": " 量 "}) \
        == ("meds", "量")
    assert forms.feedback_attrs({"field": "vitals"}) \
        == ("vitals", "抽出の誤り報告（バイタル）")
    assert forms.feedback_attrs({"field": "x"}) \
        == "誤っている箇所を選択してください。"

def test_feedback_fields_match_the_runner_vocabulary():
    import operation_contract
    assert tuple(v for v, _ in forms.FEEDBACK_FIELDS) \
        == operation_contract.EXTRACT_FEEDBACK_FIELDS
    assert dict(forms.FEEDBACK_FIELDS)["urgency"] == "緊急度"

def test_dismiss_reasons_match_the_runner_vocabulary():
    import operation_contract
    assert tuple(v for v, _ in forms.DISMISS_REASONS) \
        == operation_contract.DISMISS_REASON_CODES
    fields = forms.modal_fields("dismiss")
    assert [f["id"] for f in fields] == ["reason_code", "note"]
    assert fields[0]["required"] and fields[0]["options"]

@pytest.mark.parametrize("fields,expect", [
    ({"reason_code": "duplicate", "note": " 同じ件 "}, ("同じ件", "duplicate")),
    ({"reason_code": "false_positive", "note": ""}, ("誤検知", "false_positive")),
    ({"reason_code": "", "note": "x"}, "却下理由を選択してください。"),
    ({"reason_code": "nope"}, "却下理由を選択してください。"),
    # a modal opened before the upgrade: free text only, no code
    ({"reason": "旧理由"}, ("旧理由", None)),
    ({"reason": ""}, "理由の入力が必要です。"),
])
def test_dismiss_attrs(fields, expect):
    assert forms.dismiss_attrs(fields) == expect

def test_search_query_normalizes():
    assert forms.search_query({"query": "  発熱　 咳 "}) == "発熱 咳"
    assert forms.search_query({"query": "  "}) is None
    assert [f["id"] for f in forms.modal_fields("search")] == ["query"]
    assert [f["id"] for f in forms.modal_fields("drugsearch")] == ["query"]
    assert set(forms.QUERY_FORMS) <= set(forms.VIEW_FORMS) <= set(forms.MODAL_ACTIONS)
