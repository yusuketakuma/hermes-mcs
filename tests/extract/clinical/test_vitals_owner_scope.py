"""Synthetic: another person's vital and an oxygen flow never pass as the patient's current reading."""
import pytest

import clinical_values

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
    assert clinical_values.grounded_vitals(body, dict(vitals)) == {}
    assert clinical_values.patient_vitals(dict(vitals), body) == {}
    assert clinical_values.patient_current_vitals(dict(vitals), body) == {}


@pytest.mark.parametrize("body,vitals", [
    ("脈拍 本人72 家族80", {"hr": 72}),
    ("BT36.5 P72", {"bt": 36.5, "hr": 72}),
    ("家族2名来訪、P72", {"hr": 72}),
    ("本人P72", {"hr": 72}),
    ("SpO2 95% 酸素2L", {"spo2": 95}),
])
def test_the_patients_own_vital_is_kept_by_the_writer(body, vitals):
    assert clinical_values.grounded_vitals(body, dict(vitals)) == vitals


@pytest.mark.parametrize("body", ["酸素2L投与中", "酸素 3リットルで経過観察", "酸素2ℓ"])
def test_an_oxygen_flow_is_not_a_saturation_reading(body):
    value = int(next(c for c in body if c.isdigit()))
    assert clinical_values.grounded_vitals(body, {"spo2": value}) == {}
    assert clinical_values.patient_current_vitals({"spo2": value}, body) == {}
    assert clinical_values.patient_vitals({"spo2": value}, body) == {}


@pytest.mark.parametrize("body,vitals", [
    ("大丈夫 SpO2 92%", {"spo2": 92}),
    ("体位を工夫しSpO2 87%", {"spo2": 87}),
    ("工夫72", {"hr": 72}),
    ("分母P72", {"hr": 72}),
    ("娘さん3人来訪、P72", {"hr": 72}),
    ("妻65歳、本人P72", {"hr": 72}),
])
def test_words_containing_a_family_kanji_and_counts_keep_the_patients_value(body, vitals):
    assert clinical_values.grounded_vitals(body, dict(vitals)) == vitals


def test_a_family_value_saved_before_the_fix_leaves_existing_rollups(tmp_path, monkeypatch):
    import json

    import extract_llm
    import inference_dependency_loss
    import rollup
    from extract_testkit import _hash, _ledger, _message

    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm, "_rerender_cards", lambda *a, **k: None)
    store = _ledger(tmp_path)
    store.ensure_patient(1)
    try:
        body = "本人の2026-09-18測定で体温36.5℃でした。ご家族P80"
        store.save_messages([_message(body=body)])
        with store.db:
            store.db.execute("UPDATE patients SET fetch_state='complete',history_floor=-1 WHERE project_id=1")
        # Simulate the old writer output and retain it as the previous generation.
        legacy = store.artifact_add("extract_llm", json.dumps({"vitals": {"bt": 36.5, "hr": 80}}),
                                    project_id=1, message_id=1, meta={"hash": _hash(store)})
        assert clinical_values.patient_current_vitals({"bt": 36.5, "hr": 80}, body) == {"bt": 36.5}
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
        current = rollup.current_cached_refs(store.db, 1, old, meta, cfg={})
        assert "hr" not in (current.get("latest_vitals") or {})
        assert not current.get("latest_vitals")  # old extraction cannot prove the current generation
        # The stale cache is queued; rebuilding alone must not promote the old output.
        assert 1 in rollup.dirty_projects(store)
        rollup.rebuild(store, 1, cfg={})
        pending = json.loads(store.db.execute(
            "SELECT content FROM artifacts WHERE kind='patient_rollup' AND project_id=1 "
            "ORDER BY artifact_id DESC LIMIT 1").fetchone()[0])
        assert not pending.get("latest_vitals")

        # A current, source-bound extraction can recover the patient's reading.
        row = dict(store.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        row["_source_binding"] = inference_dependency_loss.source_binding(store.db, 1, 1)
        row["_target"] = ("", extract_llm.resolved_model({}))
        quality = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
                   "completeness_chunks": 1,
                   "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
        assert extract_llm._replace_current(store, row, json.dumps({"vitals": {"bt": 36.5}}), integrity=quality)
        assert store.db.execute("SELECT content FROM artifacts WHERE artifact_id=?", (legacy,)).fetchone() is not None
        rollup.rebuild(store, 1, cfg={})
        rebuilt = json.loads(store.db.execute(
            "SELECT content FROM artifacts WHERE kind='patient_rollup' AND project_id=1 "
            "ORDER BY artifact_id DESC LIMIT 1").fetchone()[0])
        assert rebuilt["latest_vitals"]["bt"] == 36.5
        assert "hr" not in rebuilt["latest_vitals"]
    finally:
        store.close()


@pytest.mark.parametrize("body", ["SpO2 95% 酸素2L", "spo2 95% 酸素2L", "SpO2 95%"])
def test_the_digit_in_a_saturation_label_is_not_a_measurement(body):
    assert clinical_values.grounded_vitals(body, {"spo2": 2}) == {}
    assert clinical_values.patient_current_vitals({"spo2": 2}, body) == {}
    assert clinical_values.grounded_vitals(body, {"spo2": 95}) == {"spo2": 95}
    assert clinical_values.patient_current_vitals({"spo2": 95}, body) == {"spo2": 95}


def test_a_real_reading_equal_to_the_label_digit_is_preserved():
    body = "SpO2 2% 酸素2L"
    assert clinical_values.grounded_vitals(body, {"spo2": 2}) == {"spo2": 2}
    assert clinical_values.patient_current_vitals({"spo2": 2}, body) == {"spo2": 2}


@pytest.mark.parametrize("count", ["2名", "2人", "2回目", "2歳", "2日"])
def test_non_measurement_counts_do_not_resurrect_an_oxygen_flow(count):
    body = "酸素2L投与中、" + count + "で訪問"
    assert clinical_values.grounded_vitals(body, {"spo2": 2}) == {}
    assert clinical_values.patient_current_vitals({"spo2": 2}, body) == {}
