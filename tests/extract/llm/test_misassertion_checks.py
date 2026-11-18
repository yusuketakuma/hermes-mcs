"""Generation-12 misassertion checks: vital_readings, medication
attributes, family blood pressure and event date_text/actor binding."""
import extract_validation as validation


def _validate(body, doc):
    result = validation.validate(doc, body)
    assert result is not None
    return result


def _med(name, evidence, **attrs):
    return {"name": name, "subject": "patient", "status": "current",
            "negated": False, "evidence": evidence, **attrs}


def _reading(name, value, evidence, **extra):
    return {"name": name, "value": value, "evidence": evidence, **extra}


VITALS_BODY = ("【バイタル】血圧 128/76 mmHg、脈拍 72 回/分、"
               "SpO2 96 %、体温 36.8 ℃")


def test_vital_readings_become_vitals_and_never_leak():
    out = _validate(VITALS_BODY, {"vital_readings": [
        _reading("血圧", "128/76", "血圧 128/76 mmHg", unit="mmHg"),
        _reading("脈拍", 72, "脈拍 72 回/分"),
        _reading("SpO2", 96, "SpO2 96 %"),
        _reading("体温", 36.8, "体温 36.8 ℃"),
    ]})
    assert out["vitals"] == {"sbp": 128.0, "dbp": 76.0, "hr": 72.0,
                             "spo2": 96.0, "bt": 36.8}
    assert "vital_readings" not in out
    assert "labs" not in out


def test_vital_readings_family_and_past_are_not_patient_current():
    body = ("ご本人の脈拍は 70 回/分でした。同居の娘さんの血圧は "
            "170/100 mmHg です。昨日の体温は 37.2 ℃ でした。")
    out = _validate(body, {"vital_readings": [
        _reading("脈拍", 70, "脈拍は 70 回/分"),
        _reading("血圧", "170/100", "血圧は 170/100 mmHg"),
        _reading("体温", 37.2, "体温は 37.2 ℃"),
    ]})
    assert out.get("vitals") == {"hr": 70.0}


def test_vital_readings_duplicate_collapses_and_conflict_drops():
    body = "脈拍は 72 回/分、再測定でも 72 回/分でした。"
    out = _validate(body, {"vital_readings": [
        _reading("脈拍", 72, "脈拍は 72 回/分"),
        _reading("脈拍", 72, "72 回/分でした"),
    ]})
    assert out["vitals"] == {"hr": 72.0}
    out = _validate("脈拍は 72 回/分でした。脈拍は 80 回/分でした。",
                    {"vital_readings": [
                        _reading("脈拍", 72, "脈拍は 72 回/分"),
                        _reading("脈拍", 80, "脈拍は 80 回/分"),
                    ]})
    assert "hr" not in (out.get("vitals") or {})


def test_med_attributes_keep_only_source_wording():
    body = "合成薬ABの錠剤が処方されました。朝食後に1錠、14日分です。"
    evidence = body
    out = _validate(body, {"meds": [_med(
        "合成薬AB", evidence, form="錠剤", timing="朝食後",
        days="14日分", period="2週間分")]})
    med = out["meds"][0]
    assert med["form"] == "錠剤"
    assert med["timing"] == "朝食後"
    assert med["days"] == "14日分"
    assert "period" not in med          # 2週間分 is not written
    assert "unverified" not in med


def test_med_attributes_drop_hallucinated_and_cross_drug():
    body = ("合成薬GHは朝食前に飲みます。"
            "合成薬IJは就寝前に飲みます。")
    out = _validate(body, {"meds": [
        _med("合成薬GH", "合成薬GHは朝食前に飲みます。", timing="就寝前"),
        _med("合成薬IJ", "合成薬IJは就寝前に飲みます。", timing="朝食前"),
        _med("合成薬GH", "合成薬GHは朝食前に飲みます。", timing="朝食前"),
    ]})
    gh = [m for m in out["meds"] if m["name"] == "合成薬GH"]
    ij = [m for m in out["meds"] if m["name"] == "合成薬IJ"]
    assert all("timing" not in m for m in gh[:1])
    assert all("timing" not in m for m in ij)
    assert gh[-1].get("timing") == "朝食前"


def test_family_blood_pressure_enters_neither_vitals_nor_labs():
    body = ("ご本人の脈拍は 70 回/分でした。"
            "同居の娘さんの血圧は 170/100 mmHg です。")
    out = _validate(body, {
        "vital_readings": [
            _reading("脈拍", 70, "脈拍は 70 回/分"),
            _reading("血圧", "170/100", "血圧は 170/100 mmHg"),
        ],
        "labs": [{"name": "血圧", "value": "170/100", "subject": "family",
                  "evidence": "血圧は 170/100 mmHg"}],
    })
    assert out.get("vitals") == {"hr": 70.0}
    assert out.get("labs") == []


def test_lab_named_like_a_claimed_medication_is_not_confirmed():
    body = ("ワルファリンを継続しています。"
            "PT-INR 1.8、ワルファリン 2mg でした。")
    out = _validate(body, {
        "meds": [_med("ワルファリン", "ワルファリンを継続しています。")],
        "labs": [{"name": "ワルファリン", "value": "2mg",
                  "evidence": "ワルファリン 2mg でした。"}],
    })
    labs = [lab for lab in out.get("labs", []) if lab["name"] == "ワルファリン"]
    assert labs and all(lab.get("unverified") for lab in labs)
    assert all(lab["normalized"]["confirmation"] == "unverified" for lab in labs)


def test_event_record_date_text_and_actor_bind_to_own_instance():
    body = "ご本人は10月28日に訪問看護師による創部の処置を受けました。"
    out = _validate(body, {"event_records": [{
        "kind": "care", "subject": "patient", "status": "current",
        "negated": False, "evidence": body,
        "date_text": "10月28日", "actor": "訪問看護師"}]})
    record = out["event_records"][0]
    assert record["date_text"] == "10月28日"
    assert record["actor"] == "訪問看護師"
    assert "unverified" not in record
    assert out["events"] == ["care"]


def test_event_record_unbound_attributes_are_omitted():
    body = "ご本人は10月28日に訪問看護師による創部の処置を受けました。"
    out = _validate(body, {"event_records": [{
        "kind": "care", "subject": "patient", "status": "current",
        "negated": False, "evidence": body,
        "date_text": "2026-10-28", "actor": "ヘルパー"}]})
    record = out["event_records"][0]
    assert "date_text" not in record
    assert "actor" not in record


def test_med_attributes_unlisted_sibling_drug_never_binds():
    body = "合成薬GHは朝食前、合成薬IJは就寝前に服用しています。"
    out = _validate(body, {"meds": [
        _med("合成薬GH", body, timing="就寝前"),
        _med("合成薬GH", body, timing="朝食前"),
    ]})
    gh = [m for m in out["meds"] if m["name"] == "合成薬GH"]
    assert "timing" not in gh[0]
    assert gh[1].get("timing") == "朝食前"


def test_event_record_attributes_must_match_their_role():
    body = "ご本人は10月28日に訪問看護師による創部の処置を受けました。"
    out = _validate(body, {"event_records": [{
        "kind": "care", "subject": "patient", "status": "current",
        "negated": False, "evidence": body,
        "date_text": "訪問看護師", "actor": "創部"}]})
    record = out["event_records"][0]
    assert "date_text" not in record
    assert "actor" not in record


def test_events_derive_from_event_records_without_events_key():
    body = "ご本人は居室で転倒しました。"
    out = _validate(body, {"event_records": [{
        "kind": "fall", "subject": "patient", "status": "current",
        "negated": False, "evidence": body, "date_text": None}]})
    assert out["events"] == ["fall"]
    assert "date_text" not in out["event_records"][0]
