"""Rule extraction keeps negation, drug/PRN wording and schedule dates out of current symptoms/plans.
Fully synthetic bodies; no DB, network or model."""
import pytest

import extract
import structured_view

POSTED = "2026-11-13T10:00:00+09:00"


def _rule(body):
    return extract.extract_message(body, POSTED)


def _card_symptoms(body, llm=None):
    return structured_view._symptom_line(llm or {}, _rule(body), context=(body, None, "patient"))


@pytest.mark.parametrize("body", [
    "本日訪問。下痢はみられませんでした。",
    "発熱なく、嘔吐も認められなかった。",
    "転倒はなかったとのこと。",
    "便秘は無いです",
    "嘔気は見受けられません。",
    "発疹は出ていません。",
])
def test_negated_rule_symptoms_never_reach_the_card(body):
    assert "symptoms" not in _rule(body)
    assert _card_symptoms(body) is None
    assert _card_symptoms(body, {"symptoms": []}) is None


@pytest.mark.parametrize("body", [
    "発熱時カロナール500mg処方",
    "痛み止めを1日3回使用中",
    "不眠時ゾルピデム5mg",
    "疼痛時ロキソニン60mg頓用で処方",
    "便秘薬を継続します",
    "かゆみ止めの軟膏を処方",
])
def test_prn_conditions_and_drug_class_names_are_not_symptoms(body):
    assert "symptoms" not in _rule(body)
    assert _card_symptoms(body, {"symptoms": []}) is None


def test_body_sites_and_functions_in_normal_findings_are_not_symptoms():
    body = "皮膚状態良好。嚥下良好"
    assert "symptoms" not in _rule(body)
    assert _card_symptoms(body) is None


@pytest.mark.parametrize("body, symptom", [
    ("発熱あり", "発熱"),
    ("転倒時に頭部を打撲しました", "転倒"),
    ("痛みが強く、痛み止めを使用", "痛み"),
    ("嚥下困難あり", "嚥下困難"),
    ("下痢はないわけではない", "下痢"),
])
def test_present_symptoms_stay(body, symptom):
    assert symptom in _rule(body)["symptoms"]


@pytest.mark.parametrize("body, expected", [
    ("次回訪問：11/20→11/27に変更", "2026-11-27"),
    ("次回訪問は11/20(木)→11/27(木)へ変更となりました", "2026-11-27"),
    ("次回訪問 11/20 → 11/27", "2026-11-27"),
    ("次回訪問は11/20から11/27に変更です", "2026-11-27"),
    ("次回訪問は11/20です", "2026-11-20"),
    ("次回訪問 1/8", "2027-01-08"),
])
def test_rescheduled_visit_keeps_the_new_date(body, expected):
    assert _rule(body).get("next_planned") == expected


@pytest.mark.parametrize("body", [
    "次回分は11/10に処方済みです",
    "次回は前回11/1と同じ内容で処方します",
    "次回の採血結果（11/5実施分）を共有します",
    "次回11/10訪問しました",
    "次回訪問11/20変更になりました",
])
def test_done_past_or_changed_dates_never_become_a_shifted_next_plan(body):
    assert "next_planned" not in _rule(body)


@pytest.mark.parametrize("body", ["11/14訪問しました", "12/28訪問しました"])
def test_visit_date_is_not_pulled_back_a_whole_year(body):
    assert "visit_date" not in _rule(body)


def test_year_shift_is_only_a_dec_jan_wrap():
    from datetime import datetime
    posted = datetime.fromisoformat("2027-01-05T10:00:00+09:00")
    assert extract._ymd(12, 28, 2027, posted, "past") == "2026-12-28"
    assert extract._ymd(1, 4, 2026, datetime.fromisoformat("2026-12-30T10:00:00+09:00"), "future") == "2027-01-04"
    assert extract._ymd(11, 10, 2026, datetime.fromisoformat(POSTED), "future") is None
