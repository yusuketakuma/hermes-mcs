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
    "発熱時カロナールを処方しました",
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
    ("発熱時にカロナールを使用していました", "発熱"),
    ("転倒時に右膝を打撲し、湿布を貼付しました", "転倒"),
    ("疼痛時にロキソニンを内服中", "疼痛"),
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


@pytest.mark.parametrize("posted, month, day, mode, expected", [
    ("2026-11-20T10:00:00+09:00", 2, 15, "future", "2027-02-15"),   # a plan across New Year
    ("2026-12-20T10:00:00+09:00", 3, 15, "future", "2027-03-15"),
    ("2026-03-01T10:00:00+09:00", 12, 25, "past", "2025-12-25"),    # a visit before New Year
    ("2026-11-13T10:00:00+09:00", 11, 20, "past", None),            # not yet reached
])
def test_a_year_crossing_date_resolves_but_a_recent_wrong_side_date_does_not(posted, month, day, mode, expected):
    from datetime import datetime
    when = datetime.fromisoformat(posted)
    assert extract._ymd(month, day, when.year, when, mode) == expected


@pytest.mark.parametrize("body, expected", [
    ("次回訪問は11/27に変更になりました", "2026-11-27"),
    ("次回訪問日を11/27に変更します", "2026-11-27"),
    ("次回は11/27に決まりました", "2026-11-27"),
    ("次回は11/27となりました", "2026-11-27"),
    ("次回は11/27に採血を実施します", "2026-11-27"),
])
def test_an_announced_or_changed_to_date_stays_the_next_plan(body, expected):
    assert _rule(body)["next_planned"] == expected


@pytest.mark.parametrize("body", ["次回訪問11/20変更", "次回訪問は11/20を変更します", "次回は11/10に採血を実施しました"])
def test_a_changed_without_new_date_or_done_date_is_still_dropped(body):
    assert "next_planned" not in _rule(body)


@pytest.mark.parametrize("body", [
    "疼痛時オキノーム使用あり",
    "発熱時カロナール内服し解熱",
    "呼吸困難時にモルヒネ使用、効果あり",
])
def test_a_reported_prn_use_keeps_the_symptom_that_occurred(body):
    assert _rule(body)["symptoms"]


@pytest.mark.parametrize("body", ["疼痛時にオキノームを使用してください", "疼痛時はオキノーム5mg頓用"])
def test_a_prn_instruction_is_still_not_a_symptom(body):
    assert "symptoms" not in _rule(body)


@pytest.mark.parametrize("day, expected", [(19, None), (18, "2027-09-18")])
def test_the_62_and_63_day_boundary_of_a_passed_plan_date(day, expected):
    from datetime import datetime
    when = datetime.fromisoformat("2026-11-20T10:00:00+09:00")
    assert extract._ymd(9, day, 2026, when, "future") == expected
