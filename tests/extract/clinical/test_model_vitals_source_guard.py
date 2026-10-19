"""A model's flat vitals keep only the patient's current reading, on wholly synthetic bodies."""
import clinical_display
import extract_quality
import extract_validation
import pytest

import clinical_values
import extract_llm

FAMILY = "本人の脈拍は72です。同居の娘さんの血圧は180/110でした。"
SUPERSEDED = "午前の血圧120/80。現在の収縮期血圧は90、拡張期血圧は測定できませんでした。"


def _validated(body, vitals):
    return extract_validation.validate({"summary": "合成の要約", "vitals": vitals}, body).get("vitals", {})


def test_a_family_members_blood_pressure_is_not_the_patients():
    drops = {}
    assert clinical_values.grounded_vitals(FAMILY, {"hr": 72, "sbp": 180, "dbp": 110}, drops) == {"hr": 72}
    assert any("本人以外" in note for note in drops["vitals"])
    assert _validated(FAMILY, {"hr": 72, "sbp": 180, "dbp": 110}) == {"hr": 72.0}


@pytest.mark.parametrize("vitals,expected", [
    ({"sbp": 120, "dbp": 80}, {}),
    ({"sbp": 90, "dbp": 80}, {"sbp": 90}),
    ({"sbp": 90}, {"sbp": 90}),
])
def test_an_earlier_reading_does_not_pass_as_the_current_one(vitals, expected):
    assert clinical_values.grounded_vitals(SUPERSEDED, dict(vitals)) == expected


def test_a_missing_current_value_is_never_filled_in():
    assert "dbp" not in _validated(SUPERSEDED, {"sbp": 90})


def test_current_vitals_cannot_leak_a_rejected_old_value_through_prose():
    drops = {}
    out = extract_validation.validate({"vitals": {"sbp": 90, "dbp": 80},
        "summary": "血圧測定結果の記録。収縮期90、拡張期80。",
        "points": ["収縮期血圧90", "拡張期血圧80"]}, SUPERSEDED, drops)
    assert out["vitals"] == {"sbp": 90}
    assert "summary" not in out and out["points"] == ["収縮期血圧90"]
    assert out["_items_dropped"] == 2
    assert any("要約・要点" in issue for issue in extract_quality.repair_issues(drops, out))


@pytest.mark.parametrize("body,text", [
    (SUPERSEDED, "午前の血圧120/80。現在は収縮期90。"),
    (SUPERSEDED, "収縮期血圧90"),
    (FAMILY, "本人の脈拍72。同居娘さんの血圧180/110"),
    (FAMILY, "娘さんの血圧180/110と高値"),
    (FAMILY, "本人の脈拍72"),
    ("血圧160を超えたら連絡してください。", "血圧160を超えたら連絡"),
    ("現在の血圧140/90です。", "血圧140/90"),
    ("以前の血圧120/80。現在は140/90です。", "以前の血圧120/80"),
    ("現在の体温36.5℃です。", "体温36度5分"),
    (FAMILY, "血圧安定、合成薬180mgを確認"),
    (SUPERSEDED, "血圧は未測定、体重80kg"),
])
def test_qualified_or_uncontradicted_vital_prose_survives(body, text):
    assert clinical_values.prose_preserves_current_readings(text, body)
    out = extract_validation.validate({"summary": text, "points": [text]}, body)
    assert out["summary"] == text and out["points"] == [text]
    assert "_items_dropped" not in out


@pytest.mark.parametrize("body,text", [
    (FAMILY, "血圧180/110"),
    ("家族の体温38.5℃、本人の体温36.5℃。", "体温38.5℃"),
    ("もしSpO2が90を下回れば連絡してください。", "SpO2は90です"),
    ("過去の脈拍は110でした。現在の脈拍は70です。", "脈拍110"),
])
def test_unqualified_prose_cannot_recast_another_person_or_noncurrent_value(body, text):
    assert not clinical_values.prose_preserves_current_readings(text, body)
    out = extract_validation.validate({"summary": text, "points": [text], "urgency": "routine"}, body)
    assert "summary" not in out and out["points"] == []


@pytest.mark.parametrize("repair_correctly", [False, True])
def test_vital_prose_uses_at_most_one_repair_and_never_saves_the_rejected_value(monkeypatch, repair_correctly):
    calls = []
    invalid = {"summary": "拡張期血圧80", "points": ["拡張期血圧80"], "vitals": {"sbp": 90}}
    valid = {"summary": "収縮期血圧90、拡張期は未測定", "points": ["収縮期血圧90"], "vitals": {"sbp": 90}}

    def model(prompt, **kwargs):
        calls.append(prompt)
        return valid if repair_correctly and len(calls) == 2 else invalid

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    meta = {}
    out = extract_llm.llm_extract(SUPERSEDED, meta_out=meta)
    assert len(calls) == 2 and meta["repairs"] == 1
    assert "80" not in out.get("summary", "") and all("80" not in p for p in out.get("points", []))
    assert out["vitals"] == {"sbp": 90}
    if repair_correctly:
        assert out["summary"] == valid["summary"]


@pytest.mark.parametrize("body,vitals", [
    ("本人の血圧180/110でした。", {"sbp": 180, "dbp": 110}),
    ("母の血圧は180/110、本人の血圧も180/110でした。", {"sbp": 180, "dbp": 110}),
    ("血圧120/80、脈拍72。", {"sbp": 120, "dbp": 80, "hr": 72}),
    ("本人の朝BP180/100、夕BP120/80", {"sbp": 120, "dbp": 80}),
    ("昨日の体温は37.8でした。", {"bt": 37.8}),
    ("本人の収縮期血圧は90でした。", {"sbp": 90}),
])
def test_the_patients_own_or_unscoped_readings_are_kept(body, vitals):
    assert clinical_values.grounded_vitals(body, dict(vitals)) == vitals


@pytest.mark.parametrize("body,vitals", [
    ("夫の体温は38.5でした。本人は落ち着いています。", {"bt": 38.5}),
    ("他の利用者の血圧が180/110でした。", {"sbp": 180, "dbp": 110}),
    ("もし体温が38.5を超えたら連絡してください。", {"bt": 38.5}),
])
def test_another_persons_or_conditional_reading_is_dropped(body, vitals):
    assert clinical_values.grounded_vitals(body, dict(vitals)) == {}


def test_cached_readers_agree_with_the_writer_on_family_and_superseded_readings():
    assert clinical_values.patient_vitals({"hr": 72, "sbp": 180, "dbp": 110}, FAMILY) == {"hr": 72}
    assert clinical_values.patient_current_vitals({"sbp": 120, "dbp": 80}, SUPERSEDED) == {}


@pytest.mark.parametrize("body", [
    "午前の血圧120/80。現在の血圧90/60で脈拍120です。",          # same number, another vital
    "午前の血圧120/80。現在の血圧は測定できませんでした。",        # current reading not taken
    "現在の脈拍64、午前の血圧120/80。現在の血圧は90/60です。",    # current mark of another clause
])
def test_an_earlier_reading_cannot_borrow_another_current_mark(body):
    assert clinical_values.grounded_vitals(body, {"sbp": 120, "dbp": 80}) == {}
    assert clinical_values.patient_current_vitals({"sbp": 120, "dbp": 80}, body) == {}


def test_another_named_persons_current_reading_does_not_supersede_the_patients():
    body = "本人の午前の血圧120/80。現在の田中さんの血圧90/60です。"
    assert clinical_values.patient_current_vitals({"sbp": 120, "dbp": 80}, body, patient_name="佐藤") == {
        "sbp": 120, "dbp": 80}


def test_a_named_patients_current_reading_supersedes_the_earlier_one():
    body = "午前の血圧120/80。現在の佐藤さんの血圧90/60です。"
    assert clinical_values.patient_current_vitals({"sbp": 120, "dbp": 80}, body, patient_name="佐藤") == {}


def test_an_unknown_named_persons_equal_current_value_cannot_revive_the_patients_old_reading():
    body = "本人の午前の血圧120/80。現在の本人の血圧90/60。現在の田中さんの血圧120/80。"
    old = {"sbp": 120, "dbp": 80}
    assert clinical_values.grounded_vitals(body, old) == {}
    assert clinical_values.patient_current_vitals(old, body, patient_name="佐藤") == {}


def test_an_unknown_named_patients_own_current_value_still_has_writer_compatibility():
    body = "現在の田中さんの血圧120/80です。"
    assert clinical_values.grounded_vitals(body, {"sbp": 120, "dbp": 80}) == {"sbp": 120, "dbp": 80}


def test_a_current_word_after_the_label_still_marks_the_current_reading():
    body = "午前の血圧120/80。血圧は現在90/60です。"
    assert clinical_values.grounded_vitals(body, {"sbp": 120, "dbp": 80}) == {}
    assert clinical_values.patient_current_vitals({"sbp": 120, "dbp": 80}, body) == {}
    assert clinical_values.grounded_vitals(body, {"sbp": 90, "dbp": 60}) == {"sbp": 90, "dbp": 60}


def test_a_family_members_current_equal_value_does_not_make_the_patients_old_one_current():
    body = "本人の午前の血圧120/80。現在の本人の血圧90/60。同居の娘さんの現在の血圧120/80。"
    assert clinical_values.grounded_vitals(body, {"sbp": 120, "dbp": 80}) == {}
    assert clinical_values.patient_current_vitals({"sbp": 120, "dbp": 80}, body) == {}


def test_every_reader_of_stored_vitals_leaves_out_the_superseded_reading():
    stored = {"sbp": 120, "dbp": 80}
    assert clinical_values.patient_vitals(dict(stored), SUPERSEDED) == {}
    assert clinical_values.patient_current_vitals(dict(stored), SUPERSEDED) == {}
    assert clinical_display.scoped_vitals(dict(stored), (SUPERSEDED, None, "patient")) == {}
