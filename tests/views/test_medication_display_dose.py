"""Medication displays retain an uncertain dose alongside the product strength."""
import copy

import pytest

import structured_view


@pytest.mark.parametrize("evidence", [
    "本人は合成薬エス錠5mgを服用しています。",
    "本人は合成薬エス錠5mgを5mg服用しています。",
    "本人は合成薬エス錠5mgを朝夕1回5mg服用しています。",
    "本人は合成薬エス錠5mgを1回5mg頓用。",
    "本人は合成薬エス錠5mgを5mg服用しません。",
    "本人は合成薬エス錠5mgと合成薬ビーを5mg服用しています。",
    "合成の存在しない引用",
    None,
])
def test_repeated_strength_retains_dose_as_uncertain(evidence):
    item = {"name": "合成薬エス錠5mg", "dose": "5mg", "subject": "patient", "status": "current", "negated": False}
    if evidence is not None:
        item["evidence"] = evidence
    before = copy.deepcopy(item)
    assert structured_view._med_display_name(item) == "合成薬エス錠5mg（用量要確認: 5mg）"
    assert item == before


def test_separate_dose_outside_evidence_is_not_hidden():
    evidence = "本人は合成薬エス錠5mgを服用しています。"
    body = evidence + "1回5mgを服用しています。"
    item = {"name": "合成薬エス錠5mg", "dose": "5mg", "evidence": evidence,
            "subject": "patient", "status": "current", "negated": False}
    confirmed, _ = structured_view._med_entries({"meds": [item]}, {}, context=(body, None, "patient"))
    assert confirmed == [("合成薬エス錠5mg（用量要確認: 5mg）", None)]


def test_width_normalization_and_different_amount_do_not_erase_information():
    item = {"name": "合成薬エス錠５ｍｇ", "dose": "5mg"}
    assert structured_view._med_display_name(item) == "合成薬エス錠５ｍｇ（用量要確認: 5mg）"
    assert structured_view._med_display_name({**item, "dose": "1錠"}) == "合成薬エス錠５ｍｇ 1錠"
    assert structured_view._med_display_name({"name": "合成薬25mg", "dose": "5mg"}) == "合成薬25mg 5mg"


@pytest.mark.parametrize("legacy", [False, True])
def test_both_current_and_rule_only_views_reuse_same_display(legacy):
    body = "本人は合成薬エス錠5mgを服用しています。"
    item = {"name": "合成薬エス錠5mg", "dose": "5mg", "evidence": body,
            "subject": "patient", "status": "current", "negated": False}
    llm = {} if legacy else {"meds": [item]}
    v1 = {"medications": [item]} if legacy else {}
    confirmed, unverified = structured_view._med_entries(llm, v1, context=(body, None, "patient"))
    rows = unverified if legacy else confirmed
    assert rows == [("合成薬エス錠5mg（用量要確認: 5mg）", None)]
