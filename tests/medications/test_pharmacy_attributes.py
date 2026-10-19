"""Pharmacy detail attributes and the prescription_inquiry event, on wholly synthetic bodies."""
import extract_schema
import extract_validation
import json

import pytest

import extract_llm
from patient_context import DETAIL_KEYS, context_items, extract_context

NEW_KEYS = ("storage_status", "storage_place", "medication_manager", "inquiry_result",
            "interaction_check")
BODY = ("本人の薬は冷蔵保管で、保管場所は冷蔵庫です。管理は合成家族が行っています。"
        "疑義照会の結果は変更なしでした。重複投薬は確認済みです。")
VALUES = {"storage_status": "冷蔵保管", "storage_place": "冷蔵庫", "medication_manager": "合成家族",
          "inquiry_result": "変更なし", "interaction_check": "確認済み"}


def _item(details, evidence=BODY):
    return {"category": "medication_management", "text": evidence, "evidence": evidence,
            "subject": "patient", "details": details}


def _detail(key, value=None, evidence=BODY):
    return {"key": key, "value": value or VALUES[key], "evidence": evidence}


def test_the_five_planned_keys_are_detail_keys_with_the_unified_manager_name():
    assert set(NEW_KEYS) <= DETAIL_KEYS
    assert "storage_manager" not in DETAIL_KEYS and "duplicate_check" not in DETAIL_KEYS
    props = extract_schema.SCHEMA["schema"]["properties"]
    schema = props["patient_context"]["items"]["properties"]
    assert set(NEW_KEYS) <= set(schema["details"]["items"]["properties"]["key"]["enum"])
    assert "prescription_inquiry" in props["events"]["items"]["enum"]
    assert all(key in extract_llm._PROMPT_HEAD for key in NEW_KEYS)


@pytest.mark.parametrize("key", NEW_KEYS)
def test_a_quoted_attribute_survives_validation(key):
    out = extract_validation.validate({"patient_context": [_item([_detail(key)])]}, BODY)
    assert out["patient_context"][0]["details"] == [_detail(key)]
    assert not out.get("_items_dropped") and not out.get("_evidence_dropped")


@pytest.mark.parametrize("key", NEW_KEYS)
def test_an_attribute_outside_the_source_is_dropped_and_counted(key):
    outside = _detail(key, evidence="合成の別文です。")
    out = extract_validation.validate({"patient_context": [_item([_detail("supporter", "合成家族"), outside])]}, BODY)
    assert [d["key"] for d in out["patient_context"][0]["details"]] == ["supporter"]
    assert out["_items_dropped"] == 1


def test_unconfirmed_or_planned_results_keep_their_source_wording():
    body = "疑義照会の結果は未確認です。相互作用の確認は来週の予定です。"
    details = [{"key": "inquiry_result", "value": "未確認", "evidence": "疑義照会の結果は未確認です。"},
               {"key": "interaction_check", "value": "来週の予定", "evidence": "相互作用の確認は来週の予定です。"}]
    out = extract_validation.validate({"patient_context": [_item(details, body)]}, body)
    assert out["patient_context"][0]["details"] == details


def test_old_context_without_the_new_attributes_reads_unchanged():
    old = {"patient_context": [_item([{"key": "supporter", "value": "合成家族", "evidence": BODY}])]}
    assert context_items(json.loads(json.dumps(old)), BODY) == old["patient_context"]


@pytest.mark.parametrize("body", [
    "処方内容について疑義照会を行いました。",
    "合成薬Aの用量を処方医に照会しました。",
    "主治医へ減量の処方提案をしました。",
    "処方について照会した結果、変更なしです。",
])
def test_prescription_inquiry_needs_a_prescription_inquiry_cue(body):
    out = extract_validation.validate({"events": ["prescription_inquiry"]}, body)
    assert out["events"] == ["prescription_inquiry"]


@pytest.mark.parametrize("body", [
    "担当者会議の日程を事業所に照会しました。",
    "薬剤師に会議の出欠を照会しました。",
    "薬局へ営業時間を照会しました。",
    "請求内容に疑義があります。",
    "処方は変更なし。会議の日程を照会しました。",
])
def test_a_non_prescription_inquiry_is_no_event(body):
    out = extract_validation.validate({"summary": "合成の要約", "events": ["prescription_inquiry"]}, body)
    assert out["events"] == [] and out["_items_dropped"] == 1


def test_prescription_inquiry_bodies_reach_the_model():
    for body in ("本日疑義照会の件", "本日処方提案の件", "本日処方の照会の件"):
        assert not extract_llm._low_signal(body, {"v": 1})


@pytest.mark.parametrize("body", ["照会結果：担当者会議の日程は変更なし。", "照会結果：薬局の営業時間は変更なし。",
                                  "照会結果：薬剤師の会議参加は未確認。"])
def test_a_generic_inquiry_heading_is_no_drug_inquiry_result(body):
    assert not any(detail["key"] == "inquiry_result"
                   for item in extract_context(body) for detail in item.get("details", []))


@pytest.mark.parametrize("body,value", [("疑義照会結果：変更なし。", "変更なし。"),
                                        ("処方提案結果：未確認。", "未確認。")])
def test_a_drug_inquiry_heading_keeps_its_result_wording(body, value):
    details = [detail for item in extract_context(body) for detail in item.get("details", [])
               if detail["key"] == "inquiry_result"]
    assert [detail["value"] for detail in details] == [value]


@pytest.mark.parametrize("body", ["保管状況：同意書は鍵付き棚へ保管済みです。", "保管場所：紹介状は書類棚です。"])
def test_a_generic_storage_heading_is_no_drug_storage_detail(body):
    assert not any(detail["key"] in ("storage_status", "storage_place")
                   for item in extract_context(body) for detail in item.get("details", []))


@pytest.mark.parametrize("body,key", [("薬の保管状況：冷蔵保管。", "storage_status"),
                                      ("薬剤保管状況：未確認。", "storage_status"),
                                      ("薬の保管場所：冷蔵庫。", "storage_place"),
                                      ("薬の置き場所：台所の棚。", "storage_place")])
def test_a_drug_storage_heading_keeps_its_detail(body, key):
    assert [detail["key"] for item in extract_context(body) for detail in item.get("details", [])] == [key]


def _kept(category, body, details, subject="patient"):
    item = {"category": category, "text": body, "evidence": body, "subject": subject, "details": details}
    return [detail["key"] for detail in context_items({"patient_context": [item]}, body)[0]["details"]]


def test_a_drug_only_attribute_outside_medication_management_is_dropped():
    body = "同意書は書類棚に保管しています。"
    details = [{"key": "storage_place", "value": "書類棚", "evidence": body},
               {"key": "storage_status", "value": "保管しています", "evidence": body}]
    assert _kept("care_services", body, details) == []
    assert _kept("medication_management", body, details) == ["storage_place", "storage_status"]


@pytest.mark.parametrize("body,value", [
    ("同居の夫の薬は一包化されていますが、本人の薬はシートのままです。", "一包化"),
    ("合成長女さんの薬は冷蔵庫で保管しています。", "冷蔵庫"),
])
def test_another_persons_drug_detail_does_not_stay_on_the_patient(body, value):
    key = "support_method" if value == "一包化" else "storage_place"
    details = [{"key": key, "value": value, "evidence": body}]
    assert _kept("medication_management", body, details) == []
    assert _kept("medication_management", body, details, subject="family") == [key]


@pytest.mark.parametrize("body,key,value", [
    ("本人の薬は家族が一包化して渡しています。", "support_method", "一包化"),
    ("合成薬Aと合成薬Bは一包化して、娘さんが渡しています。", "support_method", "一包化"),
    ("本人の薬は冷蔵庫の扉側で保管しています。", "storage_place", "冷蔵庫の扉側"),
    ("薬の管理は同居の合成長男さんが行っており、飲み忘れはありません。", "medication_manager", "合成長男さん"),
    ("娘さんの薬は別で、本人の薬は娘さんが管理しています。", "medication_manager", "娘さん"),
])
def test_family_support_for_the_patients_own_drugs_is_kept(body, key, value):
    assert _kept("medication_management", body, [{"key": key, "value": value, "evidence": body}]) == [key]


@pytest.mark.parametrize("body,key,value", [
    ("夫の薬は本人が管理しています。", "medication_manager", "本人"),
    ("夫の薬は夫が管理しています。", "medication_manager", "夫"),
    ("同居の母の薬は残薬が4錠ですが、本人の薬の残薬は0錠です。", "residual_quantity", "4錠"),
    ("冷蔵庫で夫の薬を保管し、本人の薬は棚に置いています。", "storage_place", "冷蔵庫"),
])
def test_a_detail_of_another_persons_drug_is_dropped_wherever_the_value_sits(body, key, value):
    assert _kept("medication_management", body, [{"key": key, "value": value, "evidence": body}]) == []


@pytest.mark.parametrize("body,key,value", [
    ("娘さんが本人の薬を管理しています。", "medication_manager", "娘さん"),
    ("同居の母の薬は別ですが、本人の薬の残薬は0錠です。", "residual_quantity", "0錠"),
    ("夫の薬は一包化、本人の薬も一包化しています。", "support_method", "一包化"),
])
def test_a_detail_of_the_patients_own_drug_is_kept_beside_another_persons(body, key, value):
    assert _kept("medication_management", body, [{"key": key, "value": value, "evidence": body}]) == [key]
