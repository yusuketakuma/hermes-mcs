"""v1.0.26 sweep: care-worker visit reports keep the patient's vitals; 不安定 is not the symptom 不安 (fully synthetic)."""
import pytest

import extract

POSTED = "2026-10-18T09:00:00+09:00"


@pytest.mark.parametrize("body, vitals", [
    ("ヘルパーが入浴介助しました。体温36.8℃、血圧120/80", {"bt": 36.8, "sbp": 120, "dbp": 80}),
    ("【ヘルパー報告】\n体温36.5℃", {"bt": 36.5}),
    ("ヘルパーさんが訪問。体温36.5℃", {"bt": 36.5}),
    ("介護士が排泄介助。体温36.6℃", {"bt": 36.6}),
    ("ヘルパーが体温測定。体温36.8℃", {"bt": 36.8}),
    ("ヘルパーさんが血圧を測定しました。血圧118/76", {"sbp": 118, "dbp": 76}),
])
def test_care_worker_visit_report_keeps_patient_vitals(body, vitals):
    doc = extract.extract_message(body, POSTED)
    assert doc.get("vitals") == vitals
    assert "vital_mentions" not in doc


@pytest.mark.parametrize("body", ["ヘルパーさんは体温38.2℃", "ヘルパーの体温38.5℃", "介護職員 体温38.4℃"])
def test_care_worker_owned_value_stays_other(body):
    doc = extract.extract_message(body, POSTED)
    assert "vitals" not in doc
    assert {m["scope"] for m in doc["vital_mentions"]} == {"other"}


@pytest.mark.parametrize("body, expected", [
    ("歩行不安定あり。", None),
    ("血圧不安定。発熱なし", None),
    ("夜間不安の訴えあり。", ["不安"]),
])
def test_unstable_is_not_the_symptom_anxiety(body, expected):
    assert extract.extract_message(body, POSTED).get("symptoms") == expected
