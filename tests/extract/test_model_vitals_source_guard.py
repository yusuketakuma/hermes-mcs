"""A model's flat vitals keep only the patient's current reading, on wholly synthetic bodies."""
import pytest

import clinical_values
import extract
import extract_llm

FAMILY = "本人の脈拍は72です。同居の娘さんの血圧は180/110でした。"
SUPERSEDED = "午前の血圧120/80。現在の収縮期血圧は90、拡張期血圧は測定できませんでした。"


def _validated(body, vitals):
    return extract_llm._validate({"summary": "合成の要約", "vitals": vitals}, body).get("vitals", {})


def test_a_family_members_blood_pressure_is_not_the_patients():
    drops = {}
    assert extract_llm._vitals_guard(FAMILY, {"hr": 72, "sbp": 180, "dbp": 110}, drops) == {"hr": 72}
    assert any("本人以外" in note for note in drops["vitals"])
    assert _validated(FAMILY, {"hr": 72, "sbp": 180, "dbp": 110}) == {"hr": 72.0}


@pytest.mark.parametrize("vitals,expected", [
    ({"sbp": 120, "dbp": 80}, {}),
    ({"sbp": 90, "dbp": 80}, {"sbp": 90}),
    ({"sbp": 90}, {"sbp": 90}),
])
def test_an_earlier_reading_does_not_pass_as_the_current_one(vitals, expected):
    assert extract_llm._vitals_guard(SUPERSEDED, dict(vitals)) == expected


def test_a_missing_current_value_is_never_filled_in():
    assert "dbp" not in _validated(SUPERSEDED, {"sbp": 90})


@pytest.mark.parametrize("body,vitals", [
    ("本人の血圧180/110でした。", {"sbp": 180, "dbp": 110}),
    ("母の血圧は180/110、本人の血圧も180/110でした。", {"sbp": 180, "dbp": 110}),
    ("血圧120/80、脈拍72。", {"sbp": 120, "dbp": 80, "hr": 72}),
    ("本人の朝BP180/100、夕BP120/80", {"sbp": 120, "dbp": 80}),
    ("昨日の体温は37.8でした。", {"bt": 37.8}),
    ("本人の収縮期血圧は90でした。", {"sbp": 90}),
])
def test_the_patients_own_or_unscoped_readings_are_kept(body, vitals):
    assert extract_llm._vitals_guard(body, dict(vitals)) == vitals


@pytest.mark.parametrize("body,vitals", [
    ("夫の体温は38.5でした。本人は落ち着いています。", {"bt": 38.5}),
    ("他の利用者の血圧が180/110でした。", {"sbp": 180, "dbp": 110}),
    ("もし体温が38.5を超えたら連絡してください。", {"bt": 38.5}),
])
def test_another_persons_or_conditional_reading_is_dropped(body, vitals):
    assert extract_llm._vitals_guard(body, dict(vitals)) == {}


def test_cached_readers_agree_with_the_writer_on_family_and_superseded_readings():
    assert extract.patient_vitals({"hr": 72, "sbp": 180, "dbp": 110}, FAMILY) == {"hr": 72}
    assert clinical_values.patient_current_vitals({"sbp": 120, "dbp": 80}, SUPERSEDED) == {}


@pytest.mark.parametrize("body", [
    "午前の血圧120/80。現在の血圧90/60で脈拍120です。",          # same number, another vital
    "午前の血圧120/80。現在の血圧は測定できませんでした。",        # current reading not taken
    "現在の脈拍64、午前の血圧120/80。現在の血圧は90/60です。",    # current mark of another clause
])
def test_an_earlier_reading_cannot_borrow_another_current_mark(body):
    assert extract_llm._vitals_guard(body, {"sbp": 120, "dbp": 80}) == {}
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
    assert extract_llm._vitals_guard(body, old) == {}
    assert clinical_values.patient_current_vitals(old, body, patient_name="佐藤") == {}


def test_an_unknown_named_patients_own_current_value_still_has_writer_compatibility():
    body = "現在の田中さんの血圧120/80です。"
    assert extract_llm._vitals_guard(body, {"sbp": 120, "dbp": 80}) == {"sbp": 120, "dbp": 80}


def test_a_current_word_after_the_label_still_marks_the_current_reading():
    body = "午前の血圧120/80。血圧は現在90/60です。"
    assert extract_llm._vitals_guard(body, {"sbp": 120, "dbp": 80}) == {}
    assert clinical_values.patient_current_vitals({"sbp": 120, "dbp": 80}, body) == {}
    assert extract_llm._vitals_guard(body, {"sbp": 90, "dbp": 60}) == {"sbp": 90, "dbp": 60}


def test_a_family_members_current_equal_value_does_not_make_the_patients_old_one_current():
    body = "本人の午前の血圧120/80。現在の本人の血圧90/60。同居の娘さんの現在の血圧120/80。"
    assert extract_llm._vitals_guard(body, {"sbp": 120, "dbp": 80}) == {}
    assert clinical_values.patient_current_vitals({"sbp": 120, "dbp": 80}, body) == {}


def test_every_reader_of_stored_vitals_leaves_out_the_superseded_reading():
    import structured_view
    stored = {"sbp": 120, "dbp": 80}
    assert extract.patient_vitals(dict(stored), SUPERSEDED) == {}
    assert clinical_values.patient_current_vitals(dict(stored), SUPERSEDED) == {}
    assert structured_view._scoped_vitals(dict(stored), (SUPERSEDED, None, "patient")) == {}
