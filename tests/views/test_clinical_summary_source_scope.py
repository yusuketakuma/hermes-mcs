"""完全合成の原文から構造化要約の対象と時点を確認する。"""
import json
import pytest
import extract_llm
import structured_view
from extract_testkit import _ledger, _message, _hash


@pytest.fixture
def store(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    yield db
    db.close()


def seed(store, body, doc, *, name="患者A", group=False):
    store.save_messages([_message(body=body)])
    with store.db:
        store.db.execute("UPDATE patients SET patient_name=?,project_type=? WHERE project_id=1",
                         (name, "group" if group else "visiting"))
    store.artifact_add("extract_llm", json.dumps(doc), project_id=1, message_id=1,
                       meta={"hash": _hash(store), "extract_version": extract_llm.EXTRACT_VERSION})


def clinical_doc():
    return {"meds": [{"name": "合成薬A", "action": "start", "status": "current", "subject": "patient", "evidence": "合成薬Aを開始"}],
            "symptoms": [{"text": "発熱", "status": "ongoing", "subject": "patient", "evidence": "発熱あり"}],
            "labs": [{"name": "Cr", "value": "2.1", "unit": "mg/dL", "subject": "patient", "evidence": "Cr 2.1mg/dL"}],
            "vitals": {"spo2": 80}, "vital_flags": [{"key": "spo2", "value": 80}]}


def test_family_clipped_quotes_never_appear_as_confirmed_patient_items(store):
    seed(store, "母の状態：\n合成薬Aを開始。発熱あり。Cr 2.1mg/dL。SpO2 80%。", clinical_doc())
    lines = structured_view.structured_lines(store.db, 1, drug_candidates=False)
    assert not any(line.startswith(("薬剤:", "症状:", "検査:", "バイタル:", "閾値超過")) for line in lines)
    confirmed, _ = structured_view.medication_entries(store.db, 1)
    assert confirmed == []


def test_registered_patient_name_preserves_vital_and_mixed_patient_items(store):
    doc = clinical_doc()
    doc["meds"].append({"name": "合成薬B", "subject": "patient", "status": "current", "evidence": "合成薬Bを開始"})
    doc["symptoms"].append({"text": "咳", "subject": "patient", "status": "ongoing", "evidence": "咳あり"})
    doc["labs"].append({"name": "Cr", "value": "0.9", "unit": "mg/dL", "subject": "patient", "evidence": "Cr 0.9mg/dL"})
    doc["vitals"]["bt"] = 36.5
    seed(store, "母の状態：\n合成薬Aを開始。発熱あり。Cr 2.1mg/dL。SpO2 80%。\n架空花子さんは体温36.5℃。合成薬Bを開始。咳あり。Cr 0.9mg/dL。", doc, name="架空花子")
    lines = structured_view.structured_lines(store.db, 1, drug_candidates=False)
    assert "バイタル: BT 36.5" in lines
    assert "症状: 咳" in lines
    assert "検査: Cr 0.9mg/dL" in lines
    assert any("合成薬B" in line and line.startswith("薬剤:") for line in lines)
    assert not any(line.startswith("閾値超過") for line in lines)


def test_group_unqualified_items_are_not_patient_confirmed(store):
    seed(store, "合成薬Aを開始。発熱あり。Cr 2.1mg/dL。SpO2 80%。", clinical_doc(), group=True)
    lines = structured_view.structured_lines(store.db, 1, drug_candidates=False)
    assert not any(line.startswith(("薬剤:", "症状:", "検査:", "バイタル:", "閾値超過")) for line in lines)


def test_family_canonical_finding_corrects_mislabeled_subject_without_erasing_history(store):
    seed(store, "母の状態：\n合成所見あり。", {"canonical_facts": [{"fact_id": "f1", "kind": "other_observation",
        "statement": "合成所見あり", "subject": "patient:1", "evidence_quote": "合成所見あり"}]})
    lines = structured_view.structured_lines(store.db, 1)
    assert any("対象:家族" in line and "合成所見あり" in line for line in lines)
    assert not any("patient:1" in line for line in lines)


def test_family_rule_period_is_not_patients_period(store):
    seed(store, "母の状態：\n内服期間10/1-10/14。", {})
    store.artifact_add("extract_v1", json.dumps({"med_periods": [{"start": "2026-10-01", "end": "2026-10-14", "raw": "10/1-10/14"}]}),
                       project_id=1, message_id=1, meta={"hash": _hash(store)})
    assert not any(line.startswith("服薬期間:") for line in structured_view.structured_lines(store.db, 1))


def test_registered_patient_full_quote_lab_does_not_become_unknown(store):
    body = "架空花子さんはCr 0.9mg/dLです。"
    seed(store, body, {"labs": [{"name": "Cr", "value": "0.9", "unit": "mg/dL",
         "subject": "patient", "evidence": body}]}, name="架空花子")
    assert "検査: Cr 0.9mg/dL" in structured_view.structured_lines(store.db, 1)


def test_nonpatient_past_and_planned_are_distinguished_and_unverified_never_promoted(store):
    body = "母の状態：\nCr 2.1mg/dL。\n本人の状態：\n以前はCr 1.1mg/dL。合成薬Bを開始。"
    seed(store, body, {"labs": [{"name": "Cr", "value": "2.1", "unit": "mg/dL", "evidence": "Cr 2.1mg/dL"},
                               {"name": "Cr", "value": "1.1", "unit": "mg/dL", "evidence": "Cr 1.1mg/dL"}],
                       "meds": [{"name": "合成薬B", "subject": "patient", "status": "current",
                                 "unverified": True, "evidence": "合成薬Bを開始"}]})
    text = "\n".join(structured_view.structured_lines(store.db, 1))
    assert "2.1mg/dL(対象:家族)" in text and "1.1mg/dL(過去の報告)" in text
    assert "薬剤: 合成薬B" not in text


def test_group_explicit_patient_span_preserves_patient_value(store):
    seed(store, "本人は体温36.5℃です。", {"vitals": {"bt": 36.5}}, group=True)
    assert "バイタル: BT 36.5" in structured_view.structured_lines(store.db, 1)


def test_pure_helpers_keep_absent_source_compatibility():
    assert structured_view._vital_line({"vitals": {"bt": 36.5}}, {}) == "バイタル: BT 36.5"
    assert structured_view._symptom_line({"symptoms": [{"text": "咳"}]}, {}) == "症状: 咳"
    assert structured_view._med_lines({"meds": [{"name": "合成薬A"}]}, {}) == ["薬剤: 合成薬A"]


def test_unscoped_body_cannot_resurrect_missing_vital_or_threshold_flag(store):
    seed(store, "通常連絡です。", {"vitals": {"spo2": 80}, "vital_flags": [{"key": "spo2", "value": 80}]})
    lines = structured_view.structured_lines(store.db, 1)
    assert not any(line.startswith(("バイタル:", "閾値超過")) for line in lines)


@pytest.mark.parametrize("heading,label", [("予定", "予定"), ("もし悪化した場合は", "条件・可能性の記載")])
def test_source_plan_or_condition_is_not_current_patient_symptom_or_lab(store, heading, label):
    doc = clinical_doc()
    doc["meds"][0]["status"] = "planned" if heading == "予定" else "current"
    seed(store, heading + "：\n合成薬Aを開始。発熱あり。Cr 2.1mg/dL。SpO2 80%。" if heading == "予定" else
         "本人はもし悪化した場合は合成薬Aを開始。\nもし悪化した場合は発熱あり。\nもし悪化した場合はCr 2.1mg/dL。\nもし悪化した場合はSpO2 80%。", doc)
    lines = structured_view.structured_lines(store.db, 1)
    assert not any(line.startswith(("症状:", "検査:", "閾値超過")) for line in lines)
    assert any(label in line and line.startswith("検査候補") for line in lines)
    if heading == "予定":
        assert any("合成薬A[開始][予定]" in line for line in lines)


def test_patient_negation_still_suppresses_rule_positive(store):
    seed(store, "本人は発熱なし。", {"symptoms": [{"text": "発熱", "subject": "patient", "negated": True,
                                                 "evidence": "発熱なし"}]})
    store.artifact_add("extract_v1", json.dumps({"symptoms": ["発熱"]}), project_id=1, message_id=1,
                       meta={"hash": _hash(store)})
    assert "症状: 発熱なし" in structured_view.structured_lines(store.db, 1)


def test_missing_message_does_not_publish_forged_vital_but_nullable_legacy_row_survives(store, monkeypatch):
    doc = {"vitals": {"spo2": 80}, "vital_flags": [{"key": "spo2", "value": 80}]}
    monkeypatch.setattr(structured_view, "latest_fact_artifact", lambda *args: doc)
    monkeypatch.setattr(structured_view, "latest_artifact", lambda *args: {})
    assert not any("SpO2" in line for line in structured_view.structured_lines(store.db, 999))
    seed(store, "完全合成の旧形式記録", doc)
    with store.db:
        store.db.execute("UPDATE messages SET body_text=NULL WHERE message_id=1")
    assert "バイタル: SpO2 80" in structured_view.structured_lines(store.db, 1)
