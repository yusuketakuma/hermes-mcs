"""旧抽出と混在原文の主体を完全合成データで集約時に検証する。"""
import json
import pytest

from extract_testkit import _ledger, _message, _hash
import rollup


@pytest.fixture
def store(tmp_path):
    led = _ledger(tmp_path)
    led.ensure_patient(1)
    yield led
    led.close()


def seed(store, body, doc):
    store.save_messages([_message(body=body)])
    store.artifact_add("extract_llm", json.dumps(doc), project_id=1, message_id=1,
                       meta={"hash": _hash(store)})


def test_mislabeled_family_values_never_become_patient_current(store):
    seed(store, "母の状態：\n体温38.5℃。Cr 2.1 mg/dL。合成薬Aを開始。発熱あり。", {
        "vitals": {"bt": 38.5},
        "labs": [{"name": "Cr", "value": "2.1", "unit": "mg/dL", "subject": "patient",
                  "evidence": "Cr 2.1 mg/dL"}],
        "meds": [{"name": "合成薬A", "action": "start", "subject": "patient",
                  "status": "current", "evidence": "合成薬Aを開始"}],
        "symptoms": [{"text": "発熱", "subject": "patient", "status": "ongoing",
                      "evidence": "発熱あり"}]})
    out = rollup.build_rollup(store, 1)
    for key in ("latest_vitals", "recent_labs", "medications", "recent_symptoms"):
        assert not out.get(key), key


def test_mixed_source_keeps_patient_readings_medication_and_symptom(store):
    body = "母の状態：\n体温38.5℃。Cr 2.1 mg/dL。合成薬Aを開始。発熱あり。\n本人の状態：\n体温36.5℃。Cr 0.9 mg/dL。合成薬Bを開始。咳あり。"
    seed(store, body, {"vitals": {"bt": 36.5}, "labs": [
        {"name": "Cr", "value": "2.1", "subject": "patient", "evidence": "Cr 2.1 mg/dL"},
        {"name": "Cr", "value": "0.9", "subject": "patient", "evidence": "Cr 0.9 mg/dL"}],
        "meds": [{"name": f"合成薬{c}", "action": "start", "subject": "patient", "status": "current",
                  "evidence": f"合成薬{c}を開始"} for c in "AB"],
        "symptoms": [{"text": s, "subject": "patient", "status": "ongoing", "evidence": s + "あり"}
                     for s in ("発熱", "咳")]})
    out = rollup.build_rollup(store, 1)
    assert out["latest_vitals"]["bt"] == 36.5
    assert [x["value"] for x in out["recent_labs"]] == ["0.9"]
    assert [x["name"] for x in out["medications"]] == ["合成薬B"]
    assert [x["symptom"] for x in out["recent_symptoms"]] == ["咳"]


def test_cached_clinical_fields_are_rechecked_without_erasing_profile(store):
    seed(store, "母の状態：\n体温38.5℃。合成薬Aを開始。", {"vitals": {"bt": 38.5},
        "meds": [{"name": "合成薬A", "subject": "patient", "status": "current",
                  "evidence": "合成薬Aを開始"}]})
    cached = {"latest_vitals": {"at": "old", "bt": 38.5},
              "medications": [{"name": "合成薬A", "last": "old"}],
              "patient_context": {"memo": {"living": ["合成プロフィール"]}}}
    store.db.execute("PRAGMA query_only=ON")
    out = rollup.current_cached_refs(store.db, 1, cached, {})
    assert not out.get("latest_vitals") and not out.get("medications")
    assert out["patient_context"] == cached["patient_context"]


def test_full_sentence_evidence_inherits_subject_at_item_surface(store):
    seed(store, "母は合成薬Aを開始し発熱あり。本人は合成薬Bを開始し咳あり。", {
        "meds": [{"name": "合成薬A", "subject": "patient", "status": "current",
                  "evidence": "母は合成薬Aを開始し発熱あり。"},
                 {"name": "合成薬B", "subject": "patient", "status": "current",
                  "evidence": "本人は合成薬Bを開始し咳あり。"}],
        "symptoms": [{"text": "発熱", "subject": "patient", "status": "ongoing",
                      "evidence": "母は合成薬Aを開始し発熱あり。"},
                     {"text": "咳", "subject": "patient", "status": "ongoing",
                      "evidence": "本人は合成薬Bを開始し咳あり。"}]})
    out = rollup.build_rollup(store, 1)
    assert [x["name"] for x in out["medications"]] == ["合成薬B"]
    assert [x["symptom"] for x in out["recent_symptoms"]] == ["咳"]


def test_older_unscoped_trusted_output_contract_is_preserved(store):
    seed(store, "合成本文", {"vitals": {"bt": 36.5},
                            "meds": [{"name": "合成薬A", "subject": "patient", "status": "current"}]})
    out = rollup.build_rollup(store, 1)
    assert out["latest_vitals"]["bt"] == 36.5
    assert out["medications"][0]["name"] == "合成薬A"


def test_recent_family_stop_does_not_suppress_older_patient_medication(store):
    seed(store, "本人は合成薬Aを開始。", {"meds": [{"name": "合成薬A", "subject": "patient",
        "status": "current", "action": "start", "evidence": "本人は合成薬Aを開始。"}]})
    store.save_messages([_message(mid=2, body="母の状態：\n以前、合成薬Aを中止。",
                                   posted_at="2026-09-20T00:00:00+09:00")])
    store.artifact_add("extract_llm", json.dumps({"meds": [{"name": "合成薬A", "subject": "patient",
        "status": "past", "action": "stop", "evidence": "以前、合成薬Aを中止。"}]}),
        project_id=1, message_id=2, meta={"hash": _hash(store, 2)})
    assert rollup.build_rollup(store, 1)["medications"][0]["name"] == "合成薬A"
