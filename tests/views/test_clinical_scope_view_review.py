"""臨床レビューの完全合成反例を表示・集約・シグナルの読取りで検証する。"""
import extract_validation
import json
import time

import pytest

import extract_llm
import inference_dependency_loss
import inference_version
import mcs_queries
import mcs_signals
import rollup
import structured_view
from extract_testkit import _message
from common_testkit import ledger_fixture
import clinical_urgency
import clinical_scope
import clinical_values


store = ledger_fixture(name="store", patients=(1,))


def seed(store, body, doc, *, age_days=0, measurement=False, mid=1):
    store.save_messages([_message(mid=mid, body=body)])
    if not measurement:
        with store.db:
            store.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?",
                             (time.time() - age_days * 86400, mid))
    row = dict(store.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(store.db, 1, mid)
    if measurement:
        with store.db:
            store.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 WHERE project_id=1")
        row["_target"] = ("", extract_llm.resolved_model({}))
    integrity = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
                 "completeness_chunks": 1,
                 "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    assert extract_llm._replace_current(store, row, json.dumps(doc, ensure_ascii=False), integrity=integrity)


def test_legacy_urgency_artifact_is_not_current_in_v1(store):
    body = "本人の現在の強い胸痛があります。"
    seed(store, body, {"urgency": "high", "urgency_evidence": [body]})
    with store.db:
        store.db.execute("UPDATE artifacts SET meta=json_set(meta, ?, ?) "
                         "WHERE kind='extract_llm' AND message_id=1",
                         ("$." + inference_version.VERSION_KEY, inference_version.LEGACY_VERSION))
    assert mcs_queries.current_message_extract(store.db, "extract_llm", 1) is None
    detail = clinical_urgency.message_urgency_details(store.db, 1)
    assert detail["source"] is None and detail["raw_verdict"] is None
    assert store.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_llm' AND message_id=1").fetchone()


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
    seed(store, body, extract_validation.validate({"urgency": "high", "urgency_evidence": [body]}, body))
    details = clinical_urgency.message_urgency_details(store.db, 1)
    assert details["source"] == ("llm" if high else None)
    assert details["held"] is not high
    assert (clinical_urgency.message_urgency(store.db, 1) == "llm") is high


@pytest.mark.parametrize("body,vitals", [
    ("本人の現在のBP 80/50です。", {"sbp": 80, "dbp": 50}),
    ("本人の現在のBS 60です。", {"bs": 60}),
    ("本人の現在のGlu 60です。", {"bs": 60}),
])
def test_enabled_vital_high_policy_remains_high_at_read_time(store, body, vitals):
    doc = extract_validation.validate({"urgency": "routine", "vitals": vitals}, body)
    extract_validation.apply_vital_policy(doc, body, extract_validation.vital_threshold_policy({"vital_urgency": {"mode": "high"}}))
    seed(store, body, doc)
    assert clinical_urgency.message_urgency_details(store.db, 1)["source"] == "llm"


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
    doc = extract_validation.validate({"urgency": "high", "urgency_evidence": [body], "symptoms": [symptom]}, body)
    if fields.get("unverified"):
        doc["symptoms"][0]["unverified"] = True
    seed(store, body, doc)
    assert clinical_urgency.message_urgency_details(store.db, 1)["source"] == expected


def test_symptom_elsewhere_does_not_turn_request_only_urgency_evidence_into_clinical_high(store):
    body = "本人は胸部圧迫感があります。本人の記録を至急確認してください。"
    symptom = {"text": "胸部圧迫感", "subject": "patient", "status": "ongoing", "negated": False,
               "evidence": "本人は胸部圧迫感があります"}
    seed(store, body, extract_validation.validate({"urgency": "high", "urgency_evidence": ["本人の記録を至急確認してください"],
                                           "symptoms": [symptom]}, body))
    details = clinical_urgency.message_urgency_details(store.db, 1)
    assert details["source"] is None and details["kind"] == "request"


def test_family_weight_never_enters_current_patient_rollup(store):
    for mid, subject, value in ((1, "本人", 60), (2, "母", 45)):
        body = f"{subject}は2026-09-19測定で体重{value}kg。"
        start = body.index("体重")
        assert clinical_scope.patient_source_scope(body, start, start + len("体重")) == (
            "patient" if subject == "本人" else "family")
        assert clinical_values.written_day(body)[1].isoformat() == "2026-09-19"
        doc = extract_validation.validate({"labs": [{
            "name": "体重", "value": value, "unit": "kg", "subject": "patient",
            "measured_on": "2026-09-19", "evidence": body}]}, body)
        assert [item["value"] for item in doc["labs"]] == [value]
        seed(store, body, doc, measurement=True, mid=mid)
    result = rollup.build_rollup(store.db, 1, cfg={})
    assert result["measurement_coverage"]["complete"] is True
    recent = result["recent_labs"]
    assert [item["value"] for item in recent] == [60]


def test_physician_prescription_keeps_patient_medication_and_followup(store):
    body = "本人に対し、担当医師が合成薬Aを開始しました。"
    seed(store, body, extract_validation.validate({"meds": [{"name": "合成薬A", "action": "start",
         "subject": "patient", "status": "current", "negated": False, "evidence": body}]}, body), age_days=10)
    lines = structured_view.structured_lines(store.db, 1, drug_candidates=False)
    assert any(line.startswith("薬剤:") and "合成薬A" in line for line in lines)
    mcs_signals.evaluate(store, {}, now=time.time())
    assert any(item["type"] == "med_change_no_followup" and item["evidence"]["med"] == "合成薬A"
               for item in mcs_signals.current_open(store.db)["items"])


def test_partial_bp_keeps_measured_side_without_inventing_missing_side(store):
    body = "本人の現在の収縮期血圧は90、拡張期は測定不可です。"
    seed(store, body, extract_validation.validate({"vitals": {"sbp": 90}}, body), measurement=True)
    line = next(line for line in structured_view.structured_lines(store.db, 1) if line.startswith("バイタル:"))
    assert "BP 90" in line and "/?" in line
    result = rollup.build_rollup(store.db, 1)
    assert result["measurement_coverage"]["complete"] is False
    assert "latest_vitals" not in result
    dated_unknown = rollup.build_rollup(store.db, 1, cfg={})
    assert dated_unknown["measurement_coverage"]["complete"] is True
    assert "latest_vitals" not in dated_unknown


def test_dated_patient_bp_pair_is_current_when_source_proof_is_complete(store):
    body = "本人は2026-09-19測定でBP 90 / 60でした。"
    assert clinical_values.written_day(body)[1].isoformat() == "2026-09-19"
    seed(store, body, extract_validation.validate({"vitals": {"sbp": 90, "dbp": 60}}, body),
         measurement=True)
    result = rollup.build_rollup(store.db, 1, cfg={})
    assert result["measurement_coverage"]["complete"] is True
    assert result["latest_vitals"]["sbp"] == 90
    assert result["latest_vitals"]["dbp"] == 60


def test_urgency_uses_borrowed_uncommitted_source_without_view_or_write(store, monkeypatch):
    import sys
    body = "本人の現在の強い胸痛があります。"
    seed(store, body, {"urgency": "high", "urgency_evidence": [body]})
    monkeypatch.setitem(sys.modules, "structured_view", None)
    store.db.execute("UPDATE messages SET body_text=? WHERE message_id=1", ("本人の胸痛はありません。",))
    changes = store.db.total_changes
    assert store.db.in_transaction
    detail = clinical_urgency.message_urgency_details(store.db, 1)
    assert detail["source"] is None and detail["held"] is True
    assert store.db.in_transaction and store.db.total_changes == changes
    store.db.rollback()
    assert clinical_urgency.message_urgency(store.db, 1) == "llm"
