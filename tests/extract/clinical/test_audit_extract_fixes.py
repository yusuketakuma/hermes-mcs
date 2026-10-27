"""抽出監査で見つかった主体・否定・時点・検査名の取り違えを完全合成データで検証する。"""
import clinical_scope
import json

import pytest

import clinical_values
import extract
import extract_llm
import inference_dependency_loss
import patient_context
from extract_testkit import _ledger, _message
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
    assert clinical_values.grounded_vitals(body, {"sbp": 160, "dbp": 90}) == {}
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


def _bound_lab_store(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm, "_rerender_cards", lambda *args, **kwargs: None)
    store = _ledger(tmp_path)
    store.ensure_patient(1)
    with store.db:
        store.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 "
                         "WHERE project_id=1")
    return store


def _save_bound_labs(store, mid, body, at, labs):
    store.save_messages([_message(mid=mid, body=body, posted_at=at)])
    source = dict(store.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
    source["_source_binding"] = inference_dependency_loss.source_binding(store.db, 1, mid)
    source["_target"] = ("", extract_llm.resolved_model({}))
    integrity = {"validation": {"items_dropped": 0, "evidence_dropped": 0}, "completeness_chunks": 1,
                 "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    assert extract_llm._replace_current(store, source, json.dumps({"labs": labs}, ensure_ascii=False),
                                        integrity=integrity)


def test_latest_labs_are_keyed_by_analyte(tmp_path, monkeypatch):
    store = _bound_lab_store(tmp_path, monkeypatch)
    # A later posting describes the older samples: posting order must not
    # replace the measurement date written in the actual source.
    reports = [(1, "2026-10-12T09:00:00+09:00", "2026-10-01", "クレアチニン", "2.1", "CRP", "3.0"),
               (2, "2026-10-11T09:00:00+09:00", "2026-10-10", "Cr", "1.2", "crp", "0.5")]
    try:
        for mid, posted, day, cr, cr_value, crp, crp_value in reports:
            quotes = [f"本人の{day}採血で{cr} {cr_value}mg/dL", f"本人の{day}採血で{crp} {crp_value}mg/dL"]
            labs = [{"name": name, "value": value, "unit": "mg/dL", "subject": "patient",
                     "status": "current", "measured_on": day, "evidence": quote}
                    for name, value, quote in zip((cr, crp), (cr_value, crp_value), quotes)]
            _save_bound_labs(store, mid, "。".join(quotes) + "。", posted, labs)
        out = rollup.build_rollup(store.db, 1, cfg={})
        assert out["measurement_coverage"]["complete"]
        assert [(x["name"], x["value"]) for x in out["recent_labs"]] == [("Cr", "1.2"), ("crp", "0.5")]
        known = [item for item in out["measurement_reports"]["items"] if item["kind"] == "lab"]
        assert {(item["message_id"], item["value"]) for item in known} == {
            (1, "2.1"), (1, "3"), (2, "1.2"), (2, "0.5")}
        sample = next(item for item in known if item['message_id'] == 1 and item['value'] == '3')
        raw_body, content_hash = store.db.execute(
            'SELECT body_text,content_hash FROM messages WHERE message_id=1').fetchone()
        original = json.loads(store.artifacts('extract_llm', message_id=1)[0]['content'])
        lab = next(item for item in original['labs'] if item['name'] == 'CRP')
        assert sample['content_hash'] == content_hash and lab['value'] == '3.0'
        assert '3.0mg/dL' in lab['evidence'] and lab['evidence'] in raw_body
        with store.db:
            store.db.execute("UPDATE patients SET fetch_state='pending' WHERE project_id=1")
        incomplete = rollup.build_rollup(store.db, 1, cfg={})
        assert not incomplete["measurement_coverage"]["complete"] and not incomplete.get("recent_labs")
        assert {(item["message_id"], item["value"]) for item in incomplete["measurement_reports"]["items"]
                if item["kind"] == "lab"} == {(1, "2.1"), (1, "3"), (2, "1.2"), (2, "0.5")}
    finally:
        store.close()


@pytest.mark.parametrize("body, current", [
    ("昨日、体温38.5℃。", None),
    ("前回は、BP150/90でした。", None),
    ("昨日、体温38.5℃。本日体温36.5℃。", {"bt": 36.5}),
    ("昨日、体温38.5℃、本日血圧120/70。", {"sbp": 120, "dbp": 70}),
    ("昨日から、体温38.5℃。", {"bt": 38.5}),
])
def test_sentence_leading_past_word_reaches_every_reading_of_the_sentence(body, current):
    assert extract.extract_message(body, POSTED).get("vitals") == current


def _labs_of_one_post(tmp_path, monkeypatch, labs, body):
    store = _bound_lab_store(tmp_path, monkeypatch)
    try:
        _save_bound_labs(store, 1, body, "2026-10-11T09:00:00+09:00", labs)
        result = rollup.build_rollup(store.db, 1, cfg={})
        assert result["measurement_coverage"]["complete"]
        return result.get("recent_labs")
    finally:
        store.close()


def test_same_post_labs_choose_the_latest_written_sampling_date(tmp_path, monkeypatch):
    body = "本人の2026-10-01採血でクレアチニン 2.1mg/dL。\n本人の2026-10-10採血でCr 1.2mg/dL。"
    labs = [{"name": "クレアチニン", "value": "2.1", "unit": "mg/dL", "subject": "patient",
             "evidence": "本人の2026-10-01採血でクレアチニン 2.1mg/dL", "measured_on": "2026-10-01"},
            {"name": "Cr", "value": "1.2", "unit": "mg/dL", "subject": "patient",
             "evidence": "本人の2026-10-10採血でCr 1.2mg/dL", "measured_on": "2026-10-10"}]
    assert [(x["name"], x["value"]) for x in _labs_of_one_post(tmp_path, monkeypatch, labs, body)] == [("Cr", "1.2")]


def test_same_post_labs_without_dates_do_not_assert_a_current_value(tmp_path, monkeypatch):
    body = "本人の採血でクレアチニン 2.1mg/dL。その後本人の採血でCr 1.2mg/dL。"
    labs = [{"name": "クレアチニン", "value": "2.1", "unit": "mg/dL", "subject": "patient",
             "evidence": "本人の採血でクレアチニン 2.1mg/dL"},
            {"name": "Cr", "value": "1.2", "unit": "mg/dL", "subject": "patient",
             "evidence": "本人の採血でCr 1.2mg/dL"}]
    assert not _labs_of_one_post(tmp_path, monkeypatch, labs, body)
