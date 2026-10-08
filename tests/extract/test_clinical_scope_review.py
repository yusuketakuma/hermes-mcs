"""完全合成の測定・処方について、原文の所有者と行為者を区別する。"""
import pytest

import clinical_values
import extract
import extract_llm


@pytest.mark.parametrize("label", ["体重", "身長", "weight", "height", "Glu", "BNP", "合成薬A", "架空内服A"])
def test_possessive_family_surface_is_not_patient(label):
    body = f"母の{label}45。本人の{label}60。"
    first = body.index(label)
    second = body.rindex(label)
    assert extract.patient_source_scope(body, first, first + len(label)) == "family"
    assert extract.patient_source_scope(body, second, second + len(label)) == "patient"


def test_mislabeled_family_weight_stays_unverified_without_losing_patient_weight():
    body = "母の体重45kg。本人の体重60kg。"
    for value, confirmation in [(45, "unverified"), (60, "quote_supported")]:
        item = extract_llm._validate({"labs": [{"name": "体重", "value": value, "unit": "kg",
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
    assert extract.patient_vitals(values, body) == expected
