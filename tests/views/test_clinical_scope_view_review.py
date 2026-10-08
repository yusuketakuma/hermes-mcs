"""臨床レビューの完全合成反例を表示・集約・シグナルの読取りで検証する。"""
import json
import time

import pytest

import extract_llm
import mcs_signals
import rollup
import structured_view
from extract_testkit import _hash, _ledger, _message


@pytest.fixture
def store(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    yield db
    db.close()


def seed(store, body, doc, *, age_days=0):
    store.save_messages([_message(body=body)])
    with store.db:
        store.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=1",
                         (time.time() - age_days * 86400,))
    store.artifact_add("extract_llm", json.dumps(doc), project_id=1, message_id=1,
                       meta={"hash": _hash(store), "extract_version": extract_llm.EXTRACT_VERSION})


@pytest.mark.parametrize("body,high", [
    ("本人の現在の強い胸痛があります。", True),
    ("本人の現在の腹痛があります。", True),
    ("本人の現在のBP 80/50です。", True),
    ("本人の現在のBS 60です。", True),
    ("本人の現在のGlu 60です。", True),
    ("母の強い胸痛があります。", False),
    ("本人の以前の強い胸痛がありました。", False),
    ("本人の胸痛はありません。", False),
    ("本人の胸痛を認めません。", False),
    ("本人の胸痛が出た場合は連絡してください。", False),
    ("本人の記録を至急確認してください。", False),
])
def test_accepted_high_is_readable_without_bypassing_scope_or_denial(store, body, high):
    seed(store, body, extract_llm._validate({"urgency": "high", "urgency_evidence": [body]}, body))
    details = structured_view.message_urgency_details(store.db, 1)
    assert details["source"] == ("llm" if high else None)
    assert details["held"] is not high
    assert (structured_view.message_urgency(store.db, 1) == "llm") is high


@pytest.mark.parametrize("body,vitals", [
    ("本人の現在のBP 80/50です。", {"sbp": 80, "dbp": 50}),
    ("本人の現在のBS 60です。", {"bs": 60}),
    ("本人の現在のGlu 60です。", {"bs": 60}),
])
def test_enabled_vital_high_policy_remains_high_at_read_time(store, body, vitals):
    doc = extract_llm._validate({"urgency": "routine", "vitals": vitals}, body)
    extract_llm._apply_vital_policy(doc, body, extract_llm.vital_threshold_policy({"vital_urgency": {"mode": "high"}}))
    seed(store, body, doc)
    assert structured_view.message_urgency_details(store.db, 1)["source"] == "llm"


@pytest.mark.parametrize("body,text,fields,expected", [
    ("本人は胸部圧迫感があります。", "胸部圧迫感", {}, "llm"),
    ("本人は歩けない状態です。", "歩けない", {}, "llm"),
    ("本人は低血糖の症状があります。至急対応をお願いします。", "低血糖", {}, "llm"),
    ("本人は低血糖ではありません。", "低血糖", {}, None),
    ("本人は低血糖ではない。", "低血糖", {}, None),
    ("本人は低血糖じゃない。", "低血糖", {}, None),
    ("本人は低血糖ではなかった。", "低血糖", {}, None),
    ("本人は胸部圧迫感ではありません。", "胸部圧迫感", {}, None),
    ("母は胸部圧迫感があります。", "胸部圧迫感", {}, None),
    ("本人は以前に胸部圧迫感がありました。", "胸部圧迫感", {}, None),
    ("本人は胸部圧迫感があります。", "胸部圧迫感", {"status": "resolved"}, None),
    ("本人は胸部圧迫感が出た場合は連絡してください。", "胸部圧迫感", {}, None),
    ("本人は胸部圧迫感はありません。", "胸部圧迫感", {}, None),
    ("本人は胸部圧迫感を認めません。", "胸部圧迫感", {}, None),
    ("本人は胸部圧迫感があります。", "胸部圧迫感", {"negated": True}, None),
    ("本人は胸部圧迫感があります。", "胸部圧迫感", {"subject": "other"}, None),
    ("本人は胸部圧迫感があります。", "胸部圧迫感", {"evidence": "原文にない引用"}, None),
    ("本人は胸部圧迫感があります。", "胸部圧迫感", {"unverified": True}, None),
])
def test_positive_typed_patient_symptom_is_not_limited_to_lexical_cues(store, body, text, fields, expected):
    symptom = {"text": text, "subject": "patient", "status": "ongoing", "negated": False, "evidence": body, **fields}
    doc = extract_llm._validate({"urgency": "high", "urgency_evidence": [body], "symptoms": [symptom]}, body)
    if fields.get("unverified"):
        doc["symptoms"][0]["unverified"] = True
    seed(store, body, doc)
    assert structured_view.message_urgency_details(store.db, 1)["source"] == expected


def test_symptom_elsewhere_does_not_turn_request_only_urgency_evidence_into_clinical_high(store):
    body = "本人は胸部圧迫感があります。本人の記録を至急確認してください。"
    symptom = {"text": "胸部圧迫感", "subject": "patient", "status": "ongoing", "negated": False,
               "evidence": "本人は胸部圧迫感があります"}
    seed(store, body, extract_llm._validate({"urgency": "high", "urgency_evidence": ["本人の記録を至急確認してください"],
                                           "symptoms": [symptom]}, body))
    details = structured_view.message_urgency_details(store.db, 1)
    assert details["source"] is None and details["kind"] == "request"


def test_family_weight_never_enters_current_patient_rollup(store):
    body = "母の体重45kg。本人の体重60kg。"
    seed(store, body, extract_llm._validate({"labs": [
        {"name": "体重", "value": value, "unit": "kg", "subject": "patient", "evidence": body}
        for value in (45, 60)]}, body))
    recent = rollup.build_rollup(store, 1)["recent_labs"]
    assert [item["value"] for item in recent] == [60]


def test_physician_prescription_keeps_patient_medication_and_followup(store):
    body = "本人に対し、担当医師が合成薬Aを開始しました。"
    seed(store, body, extract_llm._validate({"meds": [{"name": "合成薬A", "action": "start",
         "subject": "patient", "status": "current", "negated": False, "evidence": body}]}, body), age_days=10)
    lines = structured_view.structured_lines(store.db, 1, drug_candidates=False)
    assert any(line.startswith("薬剤:") and "合成薬A" in line for line in lines)
    mcs_signals.evaluate(store, {}, now=time.time())
    assert any(item["type"] == "med_change_no_followup" and item["evidence"]["med"] == "合成薬A"
               for item in mcs_signals.current_open(store.db)["items"])


def test_partial_bp_keeps_measured_side_without_inventing_missing_side(store):
    body = "本人の現在の収縮期血圧は90、拡張期は測定不可です。"
    seed(store, body, extract_llm._validate({"vitals": {"sbp": 90}}, body))
    line = next(line for line in structured_view.structured_lines(store.db, 1) if line.startswith("バイタル:"))
    assert "BP 90" in line and "/?" in line
    assert "dbp" not in rollup.build_rollup(store, 1)["latest_vitals"]
