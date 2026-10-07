"""旧解析の誤主体ラベルより原文scopeを優先する完全合成シグナル回帰。"""
import json
import pytest
from extract_testkit import _ledger, _message, _hash
import mcs_signals

NOW = 1791000000


@pytest.fixture
def store(tmp_path):
    led = _ledger(tmp_path)
    led.ensure_patient(1)
    yield led
    led.close()


def seed(store, body, doc, *, mid=1, age=10):
    store.save_messages([_message(mid=mid, body=body)])
    with store.db:
        store.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?", (NOW - age * 86400, mid))
    store.artifact_add("extract_llm", json.dumps(doc), project_id=1, message_id=mid,
                       meta={"hash": _hash(store, mid)})


def doc(med="合成薬A", symptom="発熱", *, negated=False):
    return {"meds": [{"name": med, "action": "start", "subject": "patient", "status": "current",
                      "negated": negated, "evidence": med + "を開始"}],
            "symptoms": [{"text": symptom, "subject": "patient", "status": "ongoing",
                          "evidence": symptom + "あり"}]}


def detect(store, detector):
    return list(detector(store.db, NOW, mcs_signals._thresholds(store.db), {}))


def test_family_mislabeled_medication_symptoms_and_adherence_do_not_alert(store):
    seed(store, "母の状態：\n合成薬Aを開始。発熱あり。飲み忘れがあります。", doc())
    for detector in (mcs_signals._med_followup, mcs_signals._symptom_after_med, mcs_signals._adherence_concern):
        assert detect(store, detector) == []


def test_mixed_patient_and_family_only_patient_items_alert(store):
    body = "母の状態：\n合成薬Aを開始。発熱あり。\n本人の状態：\n合成薬Bを開始。咳あり。飲み忘れがあります。"
    mixed = doc()
    own = doc("合成薬B", "咳")
    mixed["meds"] += own["meds"]
    mixed["symptoms"] += own["symptoms"]
    seed(store, body, mixed)
    changes = detect(store, mcs_signals._med_followup)
    assert [s["evidence"]["med"] for _, s in changes] == ["合成薬B"]
    coupled = detect(store, mcs_signals._symptom_after_med)
    assert len(coupled) == 1
    assert coupled[0][1]["evidence"]["meds"] == ["合成薬B"]
    assert coupled[0][1]["evidence"]["symptoms"] == ["咳"]
    assert len(detect(store, mcs_signals._adherence_concern)) == 1


def test_family_discharge_is_not_a_patient_transition_or_notice(store):
    seed(store, "母の状態：\n退院しました。合成薬Aを開始。", {**doc(), "events": ["discharge"]})
    assert detect(store, mcs_signals._transition_reconciliation) == []
    assert detect(store, mcs_signals._discharge_notice) == []


def test_family_later_same_med_does_not_enter_episode_grouping(store):
    seed(store, "本人は合成薬Aを開始。", doc(), mid=1, age=20)
    seed(store, "母は合成薬Aを開始。", doc(), mid=2, age=1)
    found = detect(store, mcs_signals._med_followup)
    assert len(found) == 1 and found[0][1]["evidence"]["message_ids"] == [1]
