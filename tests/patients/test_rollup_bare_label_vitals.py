"""Synthetic: vitals the writer guard grounds on a label (P/Hr/体温が/酸素飽和度) must reach the rollup."""
import json

import pytest

import clinical_values
import extract_llm
import rollup
from extract_testkit import _hash, _ledger, _message


@pytest.fixture
def db(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.ensure_patient(1)
    yield ledger
    ledger.close()


@pytest.mark.parametrize("body,vitals", [
    ("BT36.5 P72", {"bt": 36.5, "hr": 72}),
    ("本人 Hr 72", {"hr": 72}),
    ("本人の体温が36.5℃", {"bt": 36.5}),
    ("本人の酸素飽和度97%", {"spo2": 97}),
    ("本人の脈拍が72回", {"hr": 72}),
    ("本人の血糖値120", {"bs": 120}),
])
def test_writer_grounded_vitals_survive_reader(body, vitals):
    # The extraction-time guard accepts every key as the patient's current reading ...
    assert clinical_values.grounded_vitals(body, dict(vitals)) == vitals
    # ... so the rollup reader must not discard the same source-consistent value.
    assert clinical_values.patient_current_vitals(dict(vitals), body) == vitals


@pytest.mark.parametrize("body,vitals", [
    ("ご家族のP72", {"hr": 72}),
    ("ご家族のBT37.5", {"bt": 37.5}),
    ("ご家族のHR72", {"hr": 72}),
])
def test_other_person_labelled_vital_is_still_dropped(body, vitals):
    # An owner written right before the label makes the reading someone else's.
    assert clinical_values.patient_current_vitals(dict(vitals), body) == {}


def test_disagreeing_labelled_vital_is_still_dropped():
    # A written patient reading that differs from the stored value still overrides it.
    assert clinical_values.patient_current_vitals({"hr": 80}, "本人 P72") == {}

def test_rollup_latest_vitals_keeps_bare_p_pulse(db):
    db.save_messages([_message(body="BT36.5 P72")])
    db.artifact_add("extract_llm", json.dumps({"vitals": {"bt": 36.5, "hr": 72}}),
                    project_id=1, message_id=1,
                    meta={"hash": _hash(db), "extract_version": extract_llm.EXTRACT_VERSION})
    latest = rollup.build_rollup(db.db, 1)["latest_vitals"]
    assert latest.get("hr") == 72
