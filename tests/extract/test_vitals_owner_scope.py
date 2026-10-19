"""Synthetic: another person's vital and an oxygen flow never pass as the patient's current reading."""
import pytest

import clinical_values
import extract

OTHER_PERSON = [
    ("ご家族P80", {"hr": 80}),
    ("ご家族Hr72", {"hr": 72}),
    ("妻hr72", {"hr": 72}),
    ("本日ご家族P80", {"hr": 80}),
    ("訪問時ご家族P80", {"hr": 80}),
    ("訪問時妻P80", {"hr": 80}),
    ("ご家族のP72", {"hr": 72}),
    ("ご家族のBT37.5", {"bt": 37.5}),
    ("ご家族のHR72", {"hr": 72}),
    ("P 妻72", {"hr": 72}),
    ("脈拍 本人72 家族80", {"hr": 80}),
    ("ご主人SpO2 97%", {"spo2": 97}),
]


@pytest.mark.parametrize("body,vitals", OTHER_PERSON)
def test_another_persons_vital_is_dropped_by_writer_and_reader(body, vitals):
    assert clinical_values._vitals_guard(body, dict(vitals)) == {}
    assert extract.patient_vitals(dict(vitals), body) == {}
    assert clinical_values.patient_current_vitals(dict(vitals), body) == {}


@pytest.mark.parametrize("body,vitals", [
    ("脈拍 本人72 家族80", {"hr": 72}),
    ("BT36.5 P72", {"bt": 36.5, "hr": 72}),
    ("家族2名来訪、P72", {"hr": 72}),
    ("本人P72", {"hr": 72}),
    ("SpO2 95% 酸素2L", {"spo2": 95}),
])
def test_the_patients_own_vital_is_kept_by_the_writer(body, vitals):
    assert clinical_values._vitals_guard(body, dict(vitals)) == vitals


@pytest.mark.parametrize("body", ["酸素2L投与中", "酸素 3リットルで経過観察", "酸素2ℓ"])
def test_an_oxygen_flow_is_not_a_saturation_reading(body):
    value = int(next(c for c in body if c.isdigit()))
    assert clinical_values._vitals_guard(body, {"spo2": value}) == {}
    assert clinical_values.patient_current_vitals({"spo2": value}, body) == {}
    assert extract.patient_vitals({"spo2": value}, body) == {}


@pytest.mark.parametrize("body,vitals", [
    ("大丈夫 SpO2 92%", {"spo2": 92}),
    ("体位を工夫しSpO2 87%", {"spo2": 87}),
    ("工夫72", {"hr": 72}),
    ("分母P72", {"hr": 72}),
    ("娘さん3人来訪、P72", {"hr": 72}),
    ("妻65歳、本人P72", {"hr": 72}),
])
def test_words_containing_a_family_kanji_and_counts_keep_the_patients_value(body, vitals):
    assert clinical_values._vitals_guard(body, dict(vitals)) == vitals


def test_a_family_value_saved_before_the_fix_leaves_existing_rollups(tmp_path):
    import json

    import rollup
    from extract_testkit import _hash, _ledger, _message
    store = _ledger(tmp_path)
    store.ensure_patient(1)
    try:
        store.save_messages([_message(body="BT36.5、ご家族P80")])
        # what the old writer guard accepted and stored
        store.artifact_add("extract_llm", json.dumps({"vitals": {"bt": 36.5, "hr": 80}}),
                           project_id=1, message_id=1, meta={"hash": _hash(store)})
        rollup.rebuild(store, 1)
        row = store.db.execute("SELECT artifact_id,content,meta FROM artifacts "
                               "WHERE kind='patient_rollup' AND project_id=1").fetchone()
        old = json.loads(row["content"])
        old["latest_vitals"] = {**old.get("latest_vitals", {}), "hr": 80}
        meta = {**json.loads(row["meta"]), "period_check_version": rollup.PERIOD_CHECK_VERSION - 1}
        store.db.execute("UPDATE artifacts SET content=?, meta=? WHERE artifact_id=?",
                         (json.dumps(old), json.dumps(meta), row["artifact_id"]))
        store.db.commit()
        # a cache from the previous version is read through the current guard ...
        current = rollup.current_cached_refs(store.db, 1, old, meta)
        assert "hr" not in (current.get("latest_vitals") or {})
        # ... and is queued for rebuild, which drops the family value for good
        assert 1 in rollup.dirty_projects(store)
        rollup.rebuild(store, 1)
        rebuilt = json.loads(store.db.execute(
            "SELECT content FROM artifacts WHERE kind='patient_rollup' AND project_id=1 "
            "ORDER BY artifact_id DESC LIMIT 1").fetchone()[0])
        assert rebuilt["latest_vitals"]["bt"] == 36.5
        assert "hr" not in rebuilt["latest_vitals"]
    finally:
        store.close()


@pytest.mark.parametrize("body", ["SpO2 95% 酸素2L", "spo2 95% 酸素2L", "SpO2 95%"])
def test_the_digit_in_a_saturation_label_is_not_a_measurement(body):
    assert clinical_values._vitals_guard(body, {"spo2": 2}) == {}
    assert clinical_values.patient_current_vitals({"spo2": 2}, body) == {}
    assert clinical_values._vitals_guard(body, {"spo2": 95}) == {"spo2": 95}
    assert clinical_values.patient_current_vitals({"spo2": 95}, body) == {"spo2": 95}


def test_a_real_reading_equal_to_the_label_digit_is_preserved():
    body = "SpO2 2% 酸素2L"
    assert clinical_values._vitals_guard(body, {"spo2": 2}) == {"spo2": 2}
    assert clinical_values.patient_current_vitals({"spo2": 2}, body) == {"spo2": 2}


@pytest.mark.parametrize("count", ["2名", "2人", "2回目", "2歳", "2日"])
def test_non_measurement_counts_do_not_resurrect_an_oxygen_flow(count):
    body = "酸素2L投与中、" + count + "で訪問"
    assert clinical_values._vitals_guard(body, {"spo2": 2}) == {}
    assert clinical_values.patient_current_vitals({"spo2": 2}, body) == {}
