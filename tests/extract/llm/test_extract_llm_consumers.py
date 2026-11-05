"""Synthetic extract llm consumers contracts with isolated shared fixtures."""
import json
import extract_llm
from inference_version import CURRENT_VERSION, VERSION_KEY
import mcs_signals
import mcs_stats
import structured_view
import rollup
from extract_testkit import _hash, _ledger, _message
from extract_llm_testkit import _llm_artifact as _llm_artifact


def test_rollup_excludes_negated_family_past_meds(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    reports = ["本人は現行薬を服用しています。", "本人は否定薬を服用していません。",
               "母は家族の薬を服用しています。", "本人は以前、過去薬を中止しました。",
               "本人は明日、予定薬を開始予定です。"]
    db.save_messages([_message(body="".join(reports))])
    _llm_artifact(db, 1, {"meds": [
        {"name": "現行薬", "action": "none", "subject": "patient", "status": "current", "negated": False, "evidence": reports[0]},
        {"name": "否定薬", "action": "none", "subject": "patient", "status": "current", "negated": True, "evidence": reports[1]},
        {"name": "家族の薬", "action": "none", "subject": "family", "status": "current", "negated": False, "evidence": reports[2]},
        {"name": "過去薬", "action": "stop", "subject": "patient", "status": "past", "negated": False, "evidence": reports[3]},
        {"name": "予定薬", "action": "start", "subject": "patient", "status": "planned", "negated": False, "evidence": reports[4]},
    ]}, _hash(db))

    names = [m["name"] for m in
             rollup.build_rollup(db.db, 1).get("medications", [])]
    assert names == ["現行薬"]
    db.close()


def test_rollup_resolved_symptom_cancels_positive(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    first, later = "本人は発熱があります。", "本人の発熱は解消しました。"
    db.save_messages([_message(mid=1, body=first, posted_at="2026-10-01T12:00:00+09:00")])
    _llm_artifact(db, 1, {"symptoms": [{"text": "発熱", "subject": "patient", "status": "ongoing",
        "negated": False, "evidence": first}]}, _hash(db, 1))
    db.save_messages([_message(mid=2, body=later, posted_at="2026-10-02T12:00:00+09:00")])
    _llm_artifact(db, 2, {"symptoms": [{"text": "発熱", "subject": "patient", "status": "resolved",
        "negated": False, "evidence": later}]}, _hash(db, 2))

    names = [s["symptom"] for s in
             rollup.build_rollup(db.db, 1).get("recent_symptoms", [])]
    assert names == []
    state = rollup.build_rollup(db.db, 1)["clinical_state"]
    assert len(state["facts"]) == 2
    assert len(state["groups"]["history"]) == len(state["groups"]["current"]) == 1
    db.close()


def test_rollup_symptom_null_ts_shows_posted_at_not_zero(tmp_path):
    """An unparseable posted_at stores posted_at_ts=NULL; the symptom's
    'last' must come from the message, never the literal string "0"
    (FIX-RU1)."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, body="頭痛が続く",
                               posted_at="bad-date")])
    db.db.execute("UPDATE messages SET posted_at_ts=NULL "
                  "WHERE message_id=1")
    db.db.commit()
    db.artifact_add("extract_v1", json.dumps({"symptoms": ["頭痛"]}),
                    project_id=1, message_id=1, meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(db, 1)})
    out = rollup.build_rollup(db.db, 1)
    assert not out.get("recent_symptoms")
    state = out["clinical_state"]
    assert len(state["groups"]["unknown"]) == len(state["facts"]) == 1
    assert state["facts"][0]["posted_at"] == "bad-date"
    db.close()


def test_med_rows_skip_negated_and_nonpatient(tmp_path):
    db = _ledger(tmp_path)
    reports = ["本人は本日Aを開始しました。", "本人は本日Bを開始していません。",
               "母は本日Cを開始しました。"]
    db.save_messages([_message(body="".join(reports))])
    _llm_artifact(db, 1, {"meds": [
        {"name": "A", "action": "start", "subject": "patient", "status": "current",
         "negated": False, "evidence": reports[0]},
        {"name": "B", "action": "start", "subject": "patient", "status": "current",
         "negated": True, "evidence": reports[1]},
        {"name": "C", "action": "start", "subject": "family", "status": "current",
         "negated": False, "evidence": reports[2]},
    ]}, _hash(db))
    scope = {"since": None, "until": None, "project_id": None}
    meds = [m["name"] for _, _, m, _ in mcs_stats._med_rows(db.db, scope)]
    assert meds == ["A"]
    db.close()


def test_med_followup_ignores_family_and_negated_meds(tmp_path):
    """Family/negated med mentions must not open a follow-up episode —
    but a genuine patient change mention still fires."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    reports = ["母は本日家族薬を変更しました。", "本人は本日否定薬を中止していません。",
               "本人は以前、過去薬を中止しました。", "本人は本日薬Aを変更しました。"]
    db.save_messages([_message(body="".join(reports))])
    _llm_artifact(db, 1, {"meds": [
        {"name": "家族薬", "action": "change", "subject": "family", "status": "current",
         "negated": False, "evidence": reports[0]},
        {"name": "否定薬", "action": "stop", "subject": "patient", "status": "current",
         "negated": True, "evidence": reports[1]},
        {"name": "過去薬", "action": "stop", "subject": "patient", "status": "past",
         "negated": False, "evidence": reports[2]},
        {"name": "薬A", "action": "change", "subject": "patient", "status": "current",
         "negated": False, "evidence": reports[3]}]}, _hash(db))
    now = db.db.execute(
        "SELECT posted_at_ts FROM messages WHERE message_id=1"
    ).fetchone()[0] + 30 * 86400

    res = mcs_signals.evaluate(db, {}, now=now)

    sigs = [s for s in mcs_signals.current_open(db.db)["items"]
            if s["type"] == "med_change_no_followup"]
    assert [s["evidence"]["med"] for s in sigs] == ["薬A"]
    assert res["open"] >= 1
    db.close()


def test_st_med_change_followup_filters(tmp_path):
    """'none' / negated / family / past mentions must not count as
    change mentions needing follow-up."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    reports = ["本人は本日無変更薬を服用中で変更はありません。", "本人は本日否定薬を中止していません。",
               "母は本日家族薬を中止しました。", "本人は以前、過去薬を中止しました。"]
    change = "本人は本日薬Aを中止しました。"
    db.save_messages([_message(mid=1, body="".join(reports)),
                      _message(mid=2, body=change)])
    _llm_artifact(db, 1, {"meds": [
        {"name": "無変更薬", "action": "none", "subject": "patient", "status": "current",
         "negated": False, "evidence": reports[0]},
        {"name": "否定薬", "action": "stop", "subject": "patient", "status": "current",
         "negated": True, "evidence": reports[1]},
        {"name": "家族薬", "action": "stop", "subject": "family", "status": "current",
         "negated": False, "evidence": reports[2]},
        {"name": "過去薬", "action": "stop", "subject": "patient", "status": "past",
         "negated": False, "evidence": reports[3]}]},
        _hash(db, 1))
    _llm_artifact(db, 2, {"meds": [{"name": "薬A", "action": "stop", "subject": "patient", "status": "current",
                                        "negated": False, "evidence": change}]},
                  _hash(db, 2))
    old_ts = db.db.execute(
        "SELECT posted_at_ts FROM messages WHERE message_id=1"
    ).fetchone()[0]
    scope = {"since": None, "until": None, "project_id": None,
             "as_of": old_ts + 30 * 86400, "limit": 20}
    st = mcs_stats.st_med_change_followup(db.db, scope)
    assert st["change_mentions_7d_plus"]["numerator"] == 1
    assert st["no_followup_record"]["items"] == [
        {"project_id": 1, "message_id": 2}]
    db.close()


# ---------- F06/F07/F08/F09 regression ----------

def test_rollup_later_historical_stop_does_not_erase_a_current_use_report(tmp_path):
    """Posting order cannot turn an explicit historical stop into a current stop."""
    import inference_version
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    current = "本人は現在合成薬A5mgを服用中です。"
    historical = "本人は以前合成薬Aを中止していました。"
    db.save_messages([_message(
        mid=1, body=current,
        posted_at="2026-09-19T00:00:00+09:00")])
    db.artifact_add("extract_llm", json.dumps({"meds": [
        {"name": "合成薬A", "dose": "5mg", "action": "none", "status": "current",
         "subject": "patient", "negated": False, "evidence": current}]}), project_id=1, message_id=1,
        meta={"hash": _hash(db, 1), inference_version.VERSION_KEY: inference_version.CURRENT_VERSION})
    assert rollup.build_rollup(db.db, 1)["medications"] == [
        {"name": "合成薬A", "dose": "5mg", "last": "2026-09-19T00:00:00+09:00"}]
    db.save_messages([_message(
        mid=2, body=historical,
        posted_at="2026-09-20T00:00:00+09:00")])
    db.artifact_add("extract_llm", json.dumps({"meds": [
        {"name": "合成薬A", "action": "stop", "status": "past", "negated": False,
         "subject": "patient", "evidence": historical}]}), project_id=1, message_id=2,
        meta={"hash": _hash(db, 2), inference_version.VERSION_KEY: inference_version.CURRENT_VERSION})
    view = rollup.build_rollup(db.db, 1)
    assert view["medications"] == [{"name": "合成薬A", "dose": "5mg", "last": "2026-09-19T00:00:00+09:00"}]
    assert len(view["clinical_state"]["groups"]["history"]) == 1
    assert len(view["clinical_state"]["facts"]) == 2
    db.close()


def test_rollup_v1_only_med_is_unverified_candidate(tmp_path):
    """F06: a rule-extracted name carries no subject/status — a
    candidate bucket, never silently current."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, body="カロナールを内服")])
    db.artifact_add("extract_v1", json.dumps(
        {"medications": [{"name": "カロナール", "dose": "300mg"}]}),
        project_id=1, message_id=1, meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(db, 1)})
    out = rollup.build_rollup(db.db, 1)
    assert out.get("medications", []) == []
    names = [m["name"] for m in out.get("unverified_medications", [])]
    assert names == ["カロナール"]
    db.close()


def test_rollup_planned_med_is_separate_bucket(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    body = "本人は明日、トラマドールを開始予定です。"
    db.save_messages([_message(mid=1, body=body)])
    _llm_artifact(db, 1, {"meds": [
        {"name": "トラマドール", "action": None, "status": "planned",
         "subject": "patient", "negated": False, "evidence": body}]}, _hash(db, 1))
    out = rollup.build_rollup(db.db, 1)
    assert out.get("medications", []) == []
    names = [m["name"] for m in out.get("planned_medications", [])]
    assert names == ["トラマドール"]
    db.close()


def test_med_period_same_year_not_year_apart():
    """F07: '9/24-10/7' posted 2026-09-23 resolves inside ONE year —
    the old start=past/end=future logic produced a ~1yr span."""
    import extract
    d = extract.extract_message(
        "内服薬 9/24-10/7の予定", "2026-09-23T10:00:00+09:00")
    assert d["med_periods"] == [
        {"start": "2026-09-24", "end": "2026-10-07",
         "raw": "9/24-10/7"}]


def test_med_period_past_same_year():
    import extract
    d = extract.extract_message(
        "内服薬 9/1-9/14まで投与", "2026-09-23T10:00:00+09:00")
    assert d["med_periods"][0]["start"] == "2026-09-01"
    assert d["med_periods"][0]["end"] == "2026-09-14"


def test_med_period_year_wrap_with_posted_inside():
    """Posted mid-wrap: '12/28-1/10' on 2027-01-05 is the range that
    already started last December."""
    import extract
    d = extract.extract_message(
        "貼付薬 12/28-1/10の間使用", "2027-01-05T10:00:00+09:00")
    assert d["med_periods"][0]["start"] == "2026-12-28"
    assert d["med_periods"][0]["end"] == "2027-01-10"


def test_med_period_upcoming_wrap():
    import extract
    d = extract.extract_message(
        "注射 12/28-1/10の予定", "2026-09-23T10:00:00+09:00")
    assert d["med_periods"][0]["start"] == "2026-12-28"
    assert d["med_periods"][0]["end"] == "2027-01-10"


def test_med_period_requires_med_context():
    """A bare date span with no medication context is not a regimen."""
    import extract
    d = extract.extract_message(
        "勤務予定表は9/1-9/14です", "2026-09-23T10:00:00+09:00")
    assert "med_periods" not in d


def test_unverified_med_is_not_patient_current():
    """F08: an item missing its status/subject basis is a candidate —
    never adopted as the patient's confirmed current med."""
    import mcs_queries
    assert not mcs_queries.med_is_patient_current(
        {"name": "薬A", "unverified": True})
    assert mcs_queries.med_is_patient_current(
        {"name": "薬A", "status": "current", "subject": "patient"})


def test_rollup_ignores_malformed_current_artifact_and_continues(tmp_path):
    db = _ledger(tmp_path)
    for pid in (1, 2):
        db.ensure_patient(pid)
        db.save_messages([_message(mid=pid, project_id=pid)])
    row = db.db.execute(
        "SELECT content_hash FROM messages WHERE message_id=1"
    ).fetchone()
    db.artifact_add("extract_llm", json.dumps({"meds": [7]}),
                    project_id=1, message_id=1,
                    meta={"hash": row["content_hash"]})

    assert rollup.rebuild_many(db, [1, 2]) == 2
    assert db.db.execute(
        "SELECT count(*) FROM artifacts WHERE kind='patient_rollup'"
    ).fetchone()[0] == 2
    db.close()


def test_structured_view_shows_v4_detail(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="合成の本文")])
    chash = _hash(db, 1)
    db.artifact_add("extract_llm", json.dumps({
        "meds": [{"name": "トラマドール", "dose": "25mg",
                  "action": "start", "status": "current",
                  "subject": "patient", "negated": False,
                  "route": "oral", "freq": "1日2回"}],
        "symptoms": [{"text": "疼痛", "negated": False,
                      "status": "ongoing", "subject": "patient",
                      "severity": "moderate", "onset": "昨日から"}],
        "labs": [{"name": "HbA1c", "value": 7.2, "unit": "%",
                  "flag": "high"}],
        "summary": "疼痛コントロール中"}),
        project_id=1, message_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, "hash": chash,
              "extract_version": extract_llm.EXTRACT_VERSION})
    lines = structured_view.structured_lines(db.db, 1)
    joined = "\n".join(lines)
    assert "トラマドール 25mg[開始](内服・1日2回)" in joined
    assert "疼痛(中等度・昨日から)" in joined
    assert "検査候補（未確認）: HbA1c 7.2%(高)" in joined
    db.close()
