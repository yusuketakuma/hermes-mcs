"""Synthetic: vitals the writer guard grounds on a label (P/Hr/体温が/酸素飽和度) must reach the rollup."""
import extract_llm_runner
import extract_llm_source_contract
import extract_llm_store
import json

import pytest

import clinical_values
import extract_llm
import inference_dependency_loss
import rollup
from extract_testkit import _message
from common_testkit import ledger_fixture


db = ledger_fixture(name="db", patients=(1,))


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

@pytest.fixture
def complete_store(db, monkeypatch):
    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm_runner, '_WRITER_STATE', extract_llm._WRITER_STATE)
    monkeypatch.setattr(extract_llm_source_contract, '_WRITER_STATE', extract_llm._WRITER_STATE)
    monkeypatch.setattr(extract_llm, "_rerender_cards", lambda *a, **k: None)
    monkeypatch.setattr(extract_llm_store, '_rerender_cards', extract_llm._rerender_cards)
    with db.db:
        db.db.execute("UPDATE patients SET fetch_state='complete',history_floor=-1 WHERE project_id=1")
    return db


def _save_bound(db, mid, body, vitals):
    db.save_messages([_message(mid=mid, body=body, posted_at="2026-09-19T12:00:00+09:00")])
    source = dict(db.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
    source["_source_binding"] = inference_dependency_loss.source_binding(db.db, 1, mid)
    source["_target"] = ("", extract_llm.resolved_model({}))
    quality = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
               "completeness_chunks": 1,
               "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    assert extract_llm._replace_current(db, source, json.dumps({"vitals": vitals}), integrity=quality)


def test_rollup_latest_vitals_keeps_bare_p_pulse(complete_store):
    _save_bound(complete_store, 1, "本人の2026-09-19測定でBT36.5 P72。", {"bt": 36.5, "hr": 72})
    out = rollup.build_rollup(complete_store.db, 1, cfg={})
    assert out["measurement_coverage"]["complete"]
    assert out["latest_vitals"]["bt"] == 36.5 and out["latest_vitals"]["hr"] == 72
    assert out["measurement_reports"]["items"][0]["measured_on"] == "2026-09-19"


@pytest.mark.parametrize("body,vitals", [
    ("ご家族の2026-09-19測定でP72。", {"hr": 72}),
    ("本人の2026-09-18測定でP80。ご家族の2026-09-19測定でP72。", {"hr": 72}),
    ("本人の2026-09-19測定でご家族のP72。", {"hr": 72}),
    ("本人のP72。", {"hr": 72}),
    ("本人の2026-09-20測定でP72。", {"hr": 72}),
    ("本人の2026-09-19測定でP80。", {"hr": 72}),
    ("本人の2026-09-19測定でP72 P72。", {"hr": 72}),
    ("本人の2026-09-19測定でP72ではありません。", {"hr": 72}),
    ("本人の2026-09-19測定でBP72/60。", {"hr": 72}),
    ("本人の2026-09-19測定でPM72。", {"hr": 72}),
])
def test_pulse_is_not_promoted_without_matching_dated_patient_evidence(complete_store, body, vitals):
    _save_bound(complete_store, 1, body, vitals)
    out = rollup.build_rollup(complete_store.db, 1, cfg={})
    assert "hr" not in out.get("latest_vitals", {})


def test_omitted_new_pulse_does_not_revive_older_latest_panel(complete_store):
    _save_bound(complete_store, 1, "本人の2026-09-18測定でBT36.5。", {"bt": 36.5})
    _save_bound(complete_store, 2, "本人の2026-09-19測定でP72。", {})
    out = rollup.build_rollup(complete_store.db, 1, cfg={})
    assert "latest_vitals" not in out


@pytest.mark.parametrize("body", [
    "看護師が本人の脈拍を2026-09-19測定でP72。",
    "ご家族から本人の脈拍を2026-09-19測定でP72と報告。",
])
def test_staff_or_family_reporter_does_not_replace_the_patient_owner(complete_store, body):
    _save_bound(complete_store, 1, body, {"hr": 72})
    out = rollup.build_rollup(complete_store.db, 1, cfg={})
    assert out["measurement_coverage"]["complete"]
    assert out["latest_vitals"]["hr"] == 72
    assert out["measurement_reports"]["items"][0]["measured_on"] == "2026-09-19"


def test_family_date_does_not_authorize_other_vital_as_patient_measurement(complete_store):
    _save_bound(complete_store, 1, "ご家族の2026-09-19測定でBT36.5。", {"bt": 36.5})
    out = rollup.build_rollup(complete_store.db, 1, cfg={})
    assert "latest_vitals" not in out
