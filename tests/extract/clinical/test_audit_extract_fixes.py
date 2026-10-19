"""抽出監査で見つかった主体・否定・時点・検査名の取り違えを完全合成データで検証する。"""
import clinical_scope
import json

import pytest

import clinical_values
import extract
import patient_context
from extract_testkit import _ledger, _message, _hash
import rollup

POSTED = "2026-10-17T09:00:00+09:00"
NAME = "合成太郎"


def _scope(body, needle):
    at = body.index(needle)
    return clinical_scope.patient_source_scope(body, at, at + len(needle))


@pytest.mark.parametrize("person", ["ご主人", "主人", "旦那さん", "奥さん", "嫁", "長男", "次女さん",
                                    "配偶者", "ヘルパーさん", "介護士", "介護職員"])
def test_family_and_care_worker_values_are_not_the_patients(person):
    body = f"{person}の体温38.5℃、血圧160/90でした"
    assert _scope(body, "38.5") in ("family", "other")
    assert clinical_values._vitals_guard(body, {"sbp": 160, "dbp": 90}) == {}
    assert clinical_values.patient_current_vitals({"bt": 38.5}, body, patient_name=NAME) == {}
    assert "vitals" not in extract.extract_message(body, POSTED)
    lab = clinical_values.lab_candidate("Cr", "1.8", "mg/dl", f"{person}のCr 1.8mg/dl")
    assert lab["confirmation"] == "unverified"
    assert _scope("ヘルパーさんは体温38.2℃", "38.2") == "other"


def test_family_report_about_the_patient_still_counts():
    body = "ご家族より、本人の体温37.0℃"
    assert clinical_values.patient_current_vitals({"bt": 37.0}, body, patient_name=NAME) == {"bt": 37.0}
    assert extract.extract_message("ご主人から連絡。体温37.0℃でした。", POSTED)["vitals"] == {"bt": 37.0}
    assert extract.extract_message("体温37.0℃でした。", POSTED)["vitals"] == {"bt": 37.0}
    # The drug-owner and actor readers share the same family vocabulary.
    assert patient_context._DRUG_OWNER.search("ご主人の薬")["who"] == "ご主人"
    assert extract.extract_message("ご主人より連絡あり。", POSTED)["actors"] == ["family"]


@pytest.mark.parametrize("body, expected", [
    ("本日訪問。発熱なし、疼痛なし。", None),
    ("吐き気は見られません。", None),
    ("浮腫(-)、咳は認めず。", None),
    ("発熱あり。疼痛なし。", ["発熱"]),
    ("発熱なし。午後から再び発熱。", ["発熱"]),
    ("痛みが取れない。", ["痛み"]),
    ("発熱・疼痛なし。", None),
    ("発熱、咳なし。", None),
    ("咳と痰なし。", None),
    ("発熱は本日はなし。", None),
    ("発熱あり、疼痛なし。", ["発熱"]),
    ("発熱は本日なし、咳あり。", ["咳"]),
    ("発熱がないわけではない。", ["発熱"]),
    ("食欲不振で食べられない。", ["食欲不振"]),
])
def test_negated_symptoms_are_not_counted(body, expected):
    assert extract.extract_message(body, POSTED).get("symptoms") == expected


@pytest.mark.parametrize("body", ["前回訪問時BP150/90", "昨日は体温38.5℃。", "先日の血圧150/90",
                                  "入院中の体温38.5℃"])
def test_past_measurements_are_not_current(body):
    out = extract.extract_message(body, POSTED)
    assert "vitals" not in out
    assert {m["scope"] for m in out["vital_mentions"]} == {"past"}
    values = {"bt": 38.5} if "体温" in body else {"sbp": 150, "dbp": 90}
    assert clinical_values.patient_current_vitals(values, body, patient_name=NAME) == {}


def test_since_yesterday_stays_current():
    assert extract.extract_message("昨日から体温38.5℃。", POSTED)["vitals"] == {"bt": 38.5}


def test_latest_labs_are_keyed_by_analyte(tmp_path):
    store = _ledger(tmp_path)
    store.ensure_patient(1)
    reports = [(1, "2026-10-01T09:00:00+09:00", "クレアチニン", "2.1", "CRP", "3.0"),
               (2, "2026-10-10T09:00:00+09:00", "Cr", "1.2", "crp", "0.5")]
    for mid, at, cr, cr_value, crp, crp_value in reports:
        body = f"{cr} {cr_value}mg/dL。{crp} {crp_value}mg/dL。"
        store.save_messages([_message(mid=mid, body=body, posted_at=at)])
        store.artifact_add("extract_llm", json.dumps({"labs": [
            {"name": cr, "value": cr_value, "unit": "mg/dL", "subject": "patient",
             "evidence": f"{cr} {cr_value}mg/dL"},
            {"name": crp, "value": crp_value, "unit": "mg/dL", "subject": "patient",
             "evidence": f"{crp} {crp_value}mg/dL"}]}),
            project_id=1, message_id=mid, meta={"hash": _hash(store, mid)})
    try:
        labs = rollup.build_rollup(store.db, 1)["recent_labs"]
    finally:
        store.close()
    assert [(x["name"], x["value"]) for x in labs] == [("Cr", "1.2"), ("crp", "0.5")]


@pytest.mark.parametrize("body, current", [
    ("昨日、体温38.5℃。", None),
    ("前回は、BP150/90でした。", None),
    ("昨日、体温38.5℃。本日体温36.5℃。", {"bt": 36.5}),
    ("昨日、体温38.5℃、本日血圧120/70。", {"sbp": 120, "dbp": 70}),
    ("昨日から、体温38.5℃。", {"bt": 38.5}),
])
def test_sentence_leading_past_word_reaches_every_reading_of_the_sentence(body, current):
    assert extract.extract_message(body, POSTED).get("vitals") == current


def _labs_of_one_post(tmp_path, labs, body):
    store = _ledger(tmp_path)
    store.ensure_patient(1)
    store.save_messages([_message(mid=1, body=body, posted_at="2026-10-11T09:00:00+09:00")])
    store.artifact_add("extract_llm", json.dumps({"labs": labs}), project_id=1, message_id=1,
                       meta={"hash": _hash(store, 1)})
    try:
        return rollup.build_rollup(store.db, 1).get("recent_labs")
    finally:
        store.close()


def test_same_post_labs_choose_the_latest_written_sampling_date(tmp_path):
    body = "2026-10-01 採血 クレアチニン 2.1mg/dL。\n2026-10-10 採血 Cr 1.2mg/dL。"
    labs = [{"name": "クレアチニン", "value": "2.1", "unit": "mg/dL", "subject": "patient",
             "evidence": "2026-10-01 採血 クレアチニン 2.1mg/dL"},
            {"name": "Cr", "value": "1.2", "unit": "mg/dL", "subject": "patient",
             "evidence": "2026-10-10 採血 Cr 1.2mg/dL"}]
    assert [(x["name"], x["value"]) for x in _labs_of_one_post(tmp_path, labs, body)] == [("Cr", "1.2")]


def test_same_post_labs_without_dates_do_not_assert_a_current_value(tmp_path):
    body = "クレアチニン 2.1mg/dL。その後 Cr 1.2mg/dL。"
    labs = [{"name": "クレアチニン", "value": "2.1", "unit": "mg/dL", "subject": "patient",
             "evidence": "クレアチニン 2.1mg/dL"},
            {"name": "Cr", "value": "1.2", "unit": "mg/dL", "subject": "patient",
             "evidence": "Cr 1.2mg/dL"}]
    assert not _labs_of_one_post(tmp_path, labs, body)
