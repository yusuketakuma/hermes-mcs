"""完全合成の測定・処方について、原文の所有者と行為者を区別する。"""
import clinical_scope
import extract_validation
import pytest

import clinical_values


@pytest.mark.parametrize("body,surface,period", [
    ("本人は採血もしました。", "採血", "current"),
    ("本人が訪問もしました。", "訪問", "current"),
    ("本人は薬ももらいました。", "薬", "current"),
    ("本人は採血もしていました。", "採血", "current"),
    ("本人は採血もしたと報告しました。", "採血", "current"),
    ("本人は採血もしたため経過を確認します。", "採血", "current"),
    ("本人は採血もしたので経過を確認します。", "採血", "current"),
    ("本人は採血もした後で帰宅しました。", "採血", "current"),
    ("本人は服薬もしなかった。", "服薬", "current"),
    ("本人は採血もして下さい。", "採血", "unknown"),
    ("本人は採血もしたいです。", "採血", "unknown"),
    ("本人は採血もしたことがあります。", "採血", "unknown"),
    ("本人は採血もしなくてはならない。", "採血", "unknown"),
    ("本人は採血もしないといけない。", "採血", "unknown"),
    ("本人は採血もしないとだめ。", "採血", "unknown"),
    ("本人は採血もしないとダメ。", "採血", "unknown"),
    ("本人は採血もしないと駄目。", "採血", "unknown"),
    ("本人は退院もしましたか？", "退院", "unknown"),
    ("本人は退院もしませんか？", "退院", "unknown"),
    ("本人は退院もしましたでしょうか？", "退院", "unknown"),
    ("本人は退院もしますか？", "退院", "unknown"),
    ("本人は退院もしませんでしたか？", "退院", "unknown"),
    ("本人は退院もしました。", "退院", "current"),
    ("本人は採血もしたが、結果は未確認です。", "採血", "current"),
    ("本人は採血もしてもらった。", "採血", "current"),
    ("本人は採血もしていません。", "採血", "current"),
    ("本人は発熱もありません。", "発熱", "current"),
    ("本人は採血もしなくてもよい。", "採血", "conditional"),
    ("もし本人が発熱したら連絡します。", "発熱", "conditional"),
    ("もし、本人に発熱があれば連絡します。", "発熱", "conditional"),
    ("もし、家族に発熱があれば連絡します。", "発熱", "family"),
    ("本人がもしもの時は合成薬Aを使用します。", "合成薬A", "conditional"),
    ("本人は本日もし手足に発熱があれば連絡します。", "発熱", "conditional"),
    ("万が一、本人が発熱したら連絡します。", "発熱", "conditional"),
])
def test_conditional_adverb_is_distinct_from_additive_completed_action(body, surface, period):
    start = body.index(surface)
    subject, phase = ("family", "conditional") if period == "family" else ("patient", period)
    assert clinical_scope.source_axes(body, start, start + len(surface)) == (subject, phase)
    assert clinical_scope.patient_source_scope(body, start, start + len(surface)) == (
        subject if subject != "patient" or period == "current" else period)


@pytest.mark.parametrize("body,surface", [
    ("もし、本人に発熱があれば、本人は採血しました。", "採血"),
    ("もし家族に発熱があれば、本人は受診します。", "受診"),
    ("家族に発熱があれば、本人は受診します。", "受診"),
    ("本人に発熱があったら、薬剤師が訪問します。", "訪問"),
    ("もし本人に発熱があれば、本人の一方の手を採血します。", "採血"),
    ("本人に発熱があれば、本人の一方的な希望で採血します。", "採血"),
    ("もし本人に発熱があれば、右手を使い、もう一方で、看護師は採血します。", "採血"),
])
def test_conditional_effect_keeps_condition_across_comma_and_subject(body, surface):
    start = body.index(surface)
    assert clinical_scope.source_axes(body, start, start + len(surface))[1] == "conditional"


@pytest.mark.parametrize("body,surface", [
    ("もし、家族に発熱があれば連絡。本人は採血しました。", "採血"),
    ("もし、本人に発熱があれば連絡します。しかし本人は採血しました。", "採血"),
    ("本人に発熱があれば連絡、ただし本人は採血しました。", "採血"),
    ("もし家族に発熱があれば連絡。一方、本人は採血しました。", "採血"),
    ("もし家族に発熱があれば連絡、一方、本人は採血しました。", "採血"),
    ("もし家族に発熱があれば連絡、一方で、本人は採血しました。", "採血"),
])
def test_conditional_marker_does_not_cross_independent_statement(body, surface):
    start = body.index(surface)
    assert clinical_scope.source_axes(body, start, start + len(surface)) == ("patient", "current")


@pytest.mark.parametrize("body", [
    "本人は退院しましたか？",
    "本人は退院しましたか",
    "本人は退院しました？",
    "本人は退院しませんか？",
    "本人は退院済みですか？",
])
def test_question_does_not_confirm_transition(body):
    start = body.index("退院")
    end = start + len("退院")
    assert clinical_scope.source_axes(body, start, end) == ("patient", "unknown")
    assert clinical_scope.patient_source_scope(body, start, end) == "unknown"
    assert clinical_scope.source_polarity(body, 0, len(body), target_end=end) is None


@pytest.mark.parametrize("body,expected", [
    ("本人は退院しましたが退院していません。", None),
    ("本人は退院していませんが退院しました。", None),
    ("本人は退院しましたが入院していません。", False),
    ("本人は退院しましたが家族は退院していません。", False),
    ("本人は退院しました。本人は退院していません。", False),
    ("本人は退院しました。", False),
    ("本人は退院していません。", True),
])
def test_polarity_conflict_requires_same_subject_target_and_sentence(body, expected):
    target_end = body.index("退院") + len("退院")
    assert clinical_scope.source_polarity(body, 0, len(body), target_end=target_end) is expected


def test_later_question_does_not_replace_a_different_completed_statement():
    body = "本人は退院しましたか？本人は退院しました。"
    first, last = body.index("退院"), body.rindex("退院")
    assert clinical_scope.source_axes(body, first, first + 2) == ("patient", "unknown")
    assert clinical_scope.source_axes(body, last, last + 2) == ("patient", "current")
    assert clinical_scope.source_polarity(body, last, len(body), target_end=last + 2) is False


@pytest.mark.parametrize("body,values,expected", [
    ("本人の血圧120/80を測定しました。", {"sbp": 180, "dbp": 80}, {"dbp": 80}),
    ("本人の血圧120/80を測定しました。", {"sbp": 120, "dbp": 80}, {"sbp": 120, "dbp": 80}),
    ("本人は落ち着いています。", {"sbp": 180}, {"sbp": 180}),
    ("本人の体温36.5℃を測定しました。", {"bt": 38.0}, {}),
    ("本人は落ち着いています。", {"temp": 37.1}, {"temp": 37.1}),
    ("母の体温37.1。本人は落ち着いています。", {"bt": 37.1, "temp": 37.1}, {}),
])
def test_rollup_vitals_keep_only_grounded_supported_fields(body, values, expected):
    assert clinical_values.patient_current_vitals(values, body) == expected


@pytest.mark.parametrize("label", ["体重", "身長", "weight", "height", "Glu", "BNP", "合成薬A", "架空内服A"])
def test_possessive_family_surface_is_not_patient(label):
    body = f"母の{label}45。本人の{label}60。"
    first = body.index(label)
    second = body.rindex(label)
    assert clinical_scope.patient_source_scope(body, first, first + len(label)) == "family"
    assert clinical_scope.patient_source_scope(body, second, second + len(label)) == "patient"


def test_mislabeled_family_weight_stays_unverified_without_losing_patient_weight():
    body = "母の体重45kg。本人の体重60kg。"
    for value, confirmation in [(45, "unverified"), (60, "quote_supported")]:
        item = extract_validation.validate({"labs": [{"name": "体重", "value": value, "unit": "kg",
            "subject": "patient", "evidence": body}]}, body)["labs"][0]
        assert item["normalized"]["confirmation"] == confirmation
        assert bool(item.get("unverified")) == (confirmation == "unverified")


@pytest.mark.parametrize("body,scope", [
    ("本人に対し、担当医師が合成薬Aを開始しました。", "patient"),
    ("本人への処方として薬剤師が合成薬Aを変更しました。", "patient"),
    ("母に対し、担当医師が合成薬Aを開始しました。", "family"),
    ("担当医師本人が合成薬Aを開始しました。", "other"),
    ("担当医師が自身の合成薬Aを開始しました。", "other"),
    ("担当医師が発熱しました。", "other"),
    ("担当医師は発熱のため合成薬Aを開始しました。本人は安定しています。", "other"),
    ("本人に対し薬の説明。担当医師は発熱のため合成薬Aを開始しました。", "other"),
])
def test_clinical_actor_does_not_replace_explicit_recipient(body, scope):
    surface = "合成薬A" if "合成薬A" in body else "発熱"
    assert clinical_values.patient_item_scope({"evidence": body}, body, surface=surface) == scope


def test_repeated_lab_label_and_value_do_not_inherit_quote_prefix_patient_scope():
    body = "本日の記録：母の体重45kg、本人の体重60kg、確認番号45。"
    item = {"name": "体重", "value": 45, "evidence": body}
    assert clinical_values.patient_item_scope(item, body, surface="体重") == "unknown"


@pytest.mark.parametrize("body,values,expected", [
    ("本人の現在の収縮期血圧は90、拡張期は測定不可です。", {"sbp": 90}, {"sbp": 90}),
    ("本人の拡張期血圧は60、収縮期は測定不可です。", {"dbp": 60}, {"dbp": 60}),
    ("母の収縮期血圧は90、本人の収縮期血圧は120です。", {"sbp": 90}, {}),
    ("本人の以前の収縮期血圧は90です。", {"sbp": 90}, {}),
    ("本人の収縮期血圧は90を下回ったら連絡してください。", {"sbp": 90}, {}),
    ("本人の現在の拡張期血圧は90です。", {"sbp": 90}, {}),
    ("本人の現在の収縮期血圧は90です。", {"dbp": 90}, {}),
    ("本人の脈拍は90です。", {"sbp": 90}, {}),
    ("本人の現在の収縮期血圧は90です。", {"sbp": 90, "dbp": 60}, {"sbp": 90}),
])
def test_single_blood_pressure_remains_grounded_to_patient_label_and_known_side(body, values, expected):
    assert clinical_values.patient_vitals(values, body) == expected


@pytest.mark.parametrize("person", ["お兄様", "兄様", "お姉様", "姉さん", "弟さん", "妹様", "孫さん"])
def test_an_honorific_family_member_is_family(person):
    body = f"ご家族から、{person}が合成薬Aを飲み忘れたと相談がありました。"
    start = body.index("合成薬A")
    assert clinical_scope.patient_source_scope(body, start, start + 4) == "family"


def _meds(body, quote, subject):
    med = {"name": "合成薬A", "dose": None, "action": "none", "status": "current", "subject": subject,
           "negated": False, "evidence": quote}
    return extract_validation.validate({"summary": "合成の要約", "meds": [med]}, body).get("meds", [])


def test_an_honorific_family_medication_is_not_kept_as_the_patients():
    body = "ご家族から、お兄様が合成薬Aを飲み忘れたと相談がありました。患者本人の服薬状況に変更はありません。"
    quote = "お兄様が合成薬Aを飲み忘れたと相談がありました"
    [unverified] = _meds(body, quote, "patient")
    assert unverified["unverified"] is True and "subject" not in unverified
    assert unverified["name"] == "合成薬A" and unverified["evidence"] == quote
    assert [med["subject"] for med in _meds(body, quote, "family")] == ["family"]


def test_a_family_report_keeps_the_patients_own_medication():
    body = "お姉様から、本人は合成薬Aを内服中と連絡がありました。"
    assert [med["subject"] for med in _meds(body, "本人は合成薬Aを内服中", "patient")] == ["patient"]


def test_honorific_family_and_patient_measurements_stay_apart():
    body = "お兄様の体温37.8。本人の体温36.5。"
    first, second = body.index("37.8"), body.index("36.5")
    assert clinical_scope.patient_source_scope(body, first, first + 4) == "family"
    assert clinical_scope.patient_source_scope(body, second, second + 4) == "patient"


@pytest.mark.parametrize("person", ["姉川さん", "兄部様"])
def test_a_named_person_starting_with_a_relation_is_not_family(person):
    body = f"{person}が合成薬Aを飲んでいます。"
    start = body.index("合成薬A")
    assert clinical_scope.patient_source_scope(body, start, start + 4) != "family"


def _scope_of(body, word):
    start = body.index(word)
    return clinical_scope.patient_source_scope(body, start, start + len(word))


@pytest.mark.parametrize("body,word", [
    ("同居の娘さんの血圧は180/110でした。", "180"),
    ("本人の娘さんの体温37.5。", "37.5"),
    ("近所の息子様が合成薬Aを飲んでいます。", "合成薬A"),
])
def test_a_modified_family_member_is_family(body, word):
    assert _scope_of(body, word) == "family"


@pytest.mark.parametrize("body,word", [
    ("田中さんの血圧120/80。", "120"),
    ("娘川さんの体温37.5。", "37.5"),
    ("担当の看護師さんの体温37.5。", "37.5"),
])
def test_a_name_or_non_family_modifier_is_not_made_family(body, word):
    assert _scope_of(body, word) != "family"


def test_a_named_patient_stays_the_patient():
    body = "田中さんの血圧120/80。"
    start = body.index("120")
    assert clinical_scope.patient_source_scope(body, start, start + 3, patient_name="田中") == "patient"


def test_a_modified_family_report_keeps_the_patients_own_value():
    body = "同居の娘さんより、本人の体温は36.8でした。"
    assert _scope_of(body, "36.8") == "patient"


def test_the_same_value_for_a_modified_family_member_and_the_patient_stays_apart():
    body = "同居の娘さんの体温37.5。本人の体温37.5。"
    first, second = body.index("37.5"), body.rindex("37.5")
    assert clinical_scope.patient_source_scope(body, first, first + 4) == "family"
    assert clinical_scope.patient_source_scope(body, second, second + 4) == "patient"


@pytest.mark.parametrize("body,name,expected", [
    ("現在の佐藤さんの血圧90/60。", "佐藤", "patient"),
    ("現在の田中さんの血圧90/60。", "佐藤", "unknown"),
    ("現在の佐藤さんの血圧90/60。", None, "unknown"),
])
def test_a_modified_name_is_matched_by_its_own_name(body, name, expected):
    start = body.index("90")
    assert clinical_scope.patient_source_scope(body, start, start + 2, patient_name=name) == expected


@pytest.mark.parametrize("person,name,expected", [
    ("みのりさん", "り", "unknown"), ("たのうえさん", "うえ", "unknown"),
    ("みのりさん", "みのり", "patient"), ("たのうえさん", "たのうえ", "patient"),
    ("現在のみのりさん", "みのり", "patient"), ("本日の佐藤さん", "佐藤", "patient"),
])
def test_a_no_inside_a_name_is_not_a_modifier_matching_another_patient(person, name, expected):
    body = f"{person}の血圧120/80です。"
    start = body.index("120")
    assert clinical_scope.patient_source_scope(body, start, start + 3, patient_name=name) == expected


@pytest.mark.parametrize("name", ["みのり", "このみ"])
def test_a_no_inside_the_patients_own_name_is_not_a_modifier(name):
    body = f"{name}さんの血圧120/80。"
    start = body.index("120")
    assert clinical_scope.patient_source_scope(body, start, start + 3, patient_name=name) == "patient"
    assert clinical_values.patient_current_vitals({"sbp": 120, "dbp": 80}, body, patient_name=name) == {
        "sbp": 120, "dbp": 80}
