"""Vital readings, standing threshold orders and urgency keep their own source scope.
Fully synthetic bodies; no DB, network or model."""
import pytest

import clinical_display
import clinical_urgency
import clinical_values
import extract
import extract_validation
import structured_view

POSTED = "2026-11-13T10:00:00+09:00"
TH = extract_validation._VITAL_THRESHOLDS_DEFAULT


def _rule(body):
    return extract.extract_message(body, POSTED)


@pytest.mark.parametrize("body, llm, line", [
    ("SpO2 98%から88%に低下しています", {"spo2": 88}, "バイタル: SpO2 88"),
    ("体温36.5℃→38.2℃に上昇", {"bt": 38.2}, "バイタル: BT 38.2"),
    ("SpO2 96%、その後90%まで低下", {"spo2": 90}, "バイタル: SpO2 90"),
])
def test_a_changed_reading_shows_its_current_value_not_the_earlier_one(body, llm, line):
    assert "vitals" not in _rule(body)  # the rule never keeps the pre-change value
    assert clinical_display.vital_line(None, _rule(body), body) is None
    assert clinical_display.vital_line({"vitals": llm}, {}, body) == line


def test_the_current_value_keeps_its_threshold_flag_on_the_card(monkeypatch):
    body = "SpO2 98%から88%に低下しています"
    llm = {"vitals": {"spo2": 88}, "vital_flags": [{"key": "spo2", "value": 88, "rule": "spo2_lte_90"}]}
    monkeypatch.setattr(structured_view.mcs_queries, "current_message_extract",
                        lambda db, kind, mid: {} if kind == "extract_v1" else llm)
    monkeypatch.setattr(structured_view.mcs_queries, "current_message_facts", lambda db, mid: llm)
    monkeypatch.setattr(structured_view.clinical_urgency, "message_urgency_details",
                        lambda *a, **k: {"source": None, "reasons": [], "held": False, "kind": "clinical"})
    monkeypatch.setattr(structured_view, "_source_row", lambda db, mid: ((body, "full", None, None), True))
    monkeypatch.setattr(structured_view, "_drug_refs", lambda db, mid: [])
    monkeypatch.setattr(structured_view, "_pharmacy_detail_lines", lambda db, mid: [])
    lines = structured_view.structured_lines(object(), 1)
    assert "バイタル: SpO2 88" in lines and "閾値超過の測定値: SpO2 88" in lines


def test_the_latest_labelled_reading_is_the_current_rule_value():
    # review 2026-11-13: dropping both readings hid a critical evening value
    assert _rule("朝 BT36.5 夕 BT38.0")["vitals"] == {"bt": 38.0}
    assert _rule("朝 血圧 130/80、夕 血圧 70/40")["vitals"] == {"sbp": 70, "dbp": 40}
    assert _rule("BT36.5 本日BT36.5")["vitals"] == {"bt": 36.5}
    assert _rule("SpO2 98%から88%に低下、再検でSpO2 92%")["vitals"] == {"spo2": 92}


def test_a_duration_after_its_reading_is_not_a_change():
    assert _rule("体温37.5度、その後30分安静")["vitals"] == {"bt": 37.5}


@pytest.mark.parametrize("body", ["血圧 10/15 132/80", "BP 10/15は132/80", "血圧 11/30(土) 132/80"])
def test_a_date_between_the_bp_label_and_the_reading_is_not_the_reading(body):
    assert _rule(body)["vitals"] == {"sbp": 132, "dbp": 80}
    assert clinical_display.vital_line(None, _rule(body), body) == "バイタル: BP 132/80"


def test_a_physiologically_impossible_pair_is_not_a_blood_pressure():
    assert "vitals" not in _rule("血圧 10/15")


@pytest.mark.parametrize("body, key", [
    ("SpO2 90%以下で酸素2L開始してください", "spo2"),
    ("BT38.0以上でカロナール頓用", "bt"),
    ("BS70以下でブドウ糖内服", "bs"),
    ("SpO2 88%未満は救急要請", "spo2"),
    ("BP 180/100以上で頓用", "sbp"),
    ("SpO2 88%を切ると酸素開始", "spo2"),
])
def test_standing_threshold_orders_are_not_current_readings(body, key):
    result = _rule(body)
    assert key not in result.get("vitals", {})
    assert "urgency" not in result
    assert clinical_display.vital_line(None, result, body) is None
    values = {"spo2": 88, "bs": 70, "bt": 38.0, "sbp": 180, "dbp": 100}
    assert clinical_values.vital_threshold_flags(values, body, TH) == []


def test_a_threshold_emergency_order_is_not_a_rule_emergency():
    body = "SpO2 88%以下で救急搬送してください"
    assert "urgency" not in _rule(body)
    assert clinical_urgency.patient_rule_urgency_quotes(body) == []


def test_a_lab_threshold_order_is_not_quote_supported():
    body = "Cr 1.2mg/dL以上で減量"
    assert clinical_values.lab_candidate("Cr", "1.2", "mg/dL", body)["confirmation"] == "unverified"


@pytest.mark.parametrize("body", ["SpO2 85%以下で推移しています", "SpO2 90%を切った", "SpO2 85%"])
def test_a_reported_low_course_stays_a_reading(body):
    assert "spo2" in _rule(body)["vitals"]


@pytest.mark.parametrize("body, vitals, rule", [
    ("訪問の際SpO2 86%、酸素開始", {"spo2": 86.0}, "spo2_lte_90"),
    ("SpO2 85%になりました", {"spo2": 85.0}, "spo2_lte_90"),
    ("原因不明ですがSpO2 85%", {"spo2": 85.0}, "spo2_lte_90"),
    ("入浴の際にふらつきあり。SpO2 86%", {"spo2": 86.0}, "spo2_lte_90"),
    ("BP 200/100 夫同席", {"sbp": 200, "dbp": 100}, "sbp_gte_180"),
])
def test_unrelated_nearby_words_do_not_suppress_a_current_threshold_flag(body, vitals, rule):
    assert [f["rule"] for f in clinical_values.vital_threshold_flags(vitals, body, TH)] == [rule]


@pytest.mark.parametrize("body", ["夫のSpO2 85%", "SpO2 85%なら酸素", "昨日SpO2 85%でした", "SpO2 85%と測定できない"])
def test_other_person_conditional_past_or_unmeasured_readings_still_do_not_flag(body):
    assert clinical_values.vital_threshold_flags({"spo2": 85.0}, body, TH) == []


@pytest.mark.parametrize("body", [
    "救急搬送を希望されていません",
    "救急搬送歴あり",
    "救急搬送後、本日退院しました",
    "10/1に救急搬送され入院していましたが、本日退院しました",
    "救急搬送された経緯があり、今後の対応を相談したいです",
    "搬送の可能性について家族と話し合いました",
])
def test_history_preference_or_possibility_of_transport_is_not_a_current_emergency(body):
    assert "urgency" not in _rule(body)
    assert clinical_urgency.patient_rule_urgency_quotes(body) == []


@pytest.mark.parametrize("body", ["救急搬送してください", "呼吸苦あり至急往診をお願いします"])
def test_a_current_emergency_still_flags(body):
    assert _rule(body)["urgency"] == "high"


@pytest.mark.parametrize("quote", [
    "BT36.5 SpO2 98%で安定、至急、来週分の処方をお願いします",
    "呼吸状態安定、急ぎ書類をお願いします",
    "意識清明、至急処方追加をお願いします",
    "発熱は認めず、至急処方確認をお願いします",
])
def test_a_normal_vital_or_negated_symptom_keeps_an_urgent_request_administrative(quote):
    assert clinical_urgency.patient_rule_urgency_quotes(quote) == [quote]
    assert clinical_urgency.clinical_urgency_quote(quote) is False
    assert clinical_urgency.urgent_request_quote(quote)
    assert clinical_urgency.patient_clinical_quotes(quote) == []


@pytest.mark.parametrize("quote", ["呼吸苦あり、至急往診お願いします", "呼吸状態不安定、至急", "意識レベル低下、至急"])
def test_an_acute_clause_stays_clinical(quote):
    assert clinical_urgency.clinical_urgency_quote(quote) is True


@pytest.mark.parametrize("quote", [
    "呼吸状態は安定していません、至急往診お願いします",
    "SpO2 85%で安定せず、至急",
    "意識は清明ではない、至急",
    "血圧は正常範囲外、至急連絡ください",
    "呼吸状態は良好とは言えない、至急",
])
def test_a_negated_normal_word_keeps_the_acute_cue(quote):
    assert clinical_urgency.clinical_urgency_quote(quote) is True


@pytest.mark.parametrize("body, vitals", [
    ("血圧 48/30", {"sbp": 48, "dbp": 30}),
    ("血圧 40/20 下顎呼吸あり", {"sbp": 40, "dbp": 20}),
])
def test_a_shock_blood_pressure_is_still_a_reading(body, vitals):
    assert _rule(body)["vitals"] == vitals


@pytest.mark.parametrize("body", ["血圧 12/5", "血圧 11/30"])
def test_a_month_day_pair_is_not_a_blood_pressure(body):
    assert "vitals" not in _rule(body)
