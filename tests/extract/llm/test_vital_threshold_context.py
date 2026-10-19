"""Audit repro: the vital threshold family-marker exclusion matches 夫 inside
common words (大丈夫/工夫), silencing the patient's own current reading."""
import extract_validation
import extract_llm

TH = extract_validation._VITAL_THRESHOLDS_DEFAULT


def test_daijoubu_does_not_mark_reading_as_family():
    # synthetic: patient's own current SpO2 below threshold
    body = "SpO2 88%まで低下しています。大丈夫ではない様子です"
    flags = extract_validation.vital_threshold_flags({"spo2": 88.0}, body, TH)
    assert [f["key"] for f in flags] == ["spo2"], flags


def test_kufuu_does_not_mark_reading_as_family():
    body = "体位を工夫しましたがSpO2 87%のままです"
    flags = extract_validation.vital_threshold_flags({"spo2": 87.0}, body, TH)
    assert [f["key"] for f in flags] == ["spo2"], flags


def test_high_policy_raises_urgency_despite_daijoubu(monkeypatch):
    body = "SpO2 88%です。大丈夫と言っていますが呼吸が苦しそうです"
    monkeypatch.setattr(extract_llm, "_llm_call",
                        lambda *a, **k: {"vitals": {"spo2": 88},
                                         "urgency": "routine"})
    high = extract_validation.vital_threshold_policy({"vital_urgency": {"mode": "high"}})
    out = extract_llm.llm_extract(body, vital_policy=high)
    assert out["urgency"] == "high", out


def test_control_same_bodies_without_fu_flag():
    # identical sentences minus the 夫 substring do flag — isolates the cause
    for body, val in (("SpO2 88%まで低下しています。問題ない様子ではないです", 88.0),
                      ("体位を変えましたがSpO2 87%のままです", 87.0)):
        flags = extract_validation.vital_threshold_flags({"spo2": val}, body, TH)
        assert [f["key"] for f in flags] == ["spo2"], body


def test_successful_measurement_is_not_unmeasurable():
    # 測れ is meant for 測れない, but also matches a successful 測れた
    body = "再検でSpO2 86%と測れたので報告します"
    flags = extract_validation.vital_threshold_flags({"spo2": 86.0}, body, TH)
    assert [f["key"] for f in flags] == ["spo2"], flags


def test_successful_sokuteidekita_is_not_unmeasurable():
    body = "SpO2 86%と測定できたので報告します"
    assert extract_validation.vital_threshold_flags({"spo2": 86}, body, TH)


def test_family_and_unmeasurable_markers_still_exclude_flags():
    for body in ("夫のSpO2 86%です", "妻のSpO2 86%です", "SpO2 86%と測れない",
                 "SpO2 86%と測れません", "SpO2 86%と測れず", "SpO2 86%と測定できない"):
        assert extract_validation.vital_threshold_flags({"spo2": 86}, body, TH) == []


def test_family_kanji_inside_a_word_at_the_context_boundary_is_not_excluded():
    for word in ("大丈夫", "工夫"):
        body = word + "あ" * 34 + "SpO2 88%"
        assert extract_validation.vital_threshold_flags({"spo2": 88}, body, TH)
