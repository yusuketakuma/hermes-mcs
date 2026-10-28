"""Synthetic source-time rollup cases, with independent expected clinical states."""
from contextlib import closing
import json

import pytest

import clinical_scope
import extract_validation
import inference_version as inference
import inference_dependency_loss as dependency_loss
import rollup
from extract_testkit import _hash, _ledger, _message


def _med(body, *, dose="5mg", status="current", action="none", **extra):
    return {"name": "合成薬A", "dose": dose, "status": status, "action": action,
            "subject": "patient", "negated": False, "evidence": body, **extra}


def _sym(body, *, negated=False, status="ongoing", **extra):
    return {"text": "咳", "subject": "patient", "status": status,
            "negated": negated, "evidence": body, **extra}


def _save(ledger, mid, body, doc, day="2026-10-01"):
    ledger.save_messages([_message(mid=mid, body=body, posted_at=day + "T09:00:00+09:00")])
    ledger.artifact_add("extract_llm", json.dumps(doc, ensure_ascii=False), project_id=1, message_id=mid,
        meta={"hash": _hash(ledger, mid), inference.VERSION_KEY: inference.CURRENT_VERSION})


def _doses(view, key):
    return {item["dose"] for item in view.get(key, [])}


def _source_item(ledger, fact):
    ref = fact["source_ref"]
    row = ledger.db.execute("SELECT kind,message_id,meta,content FROM artifacts WHERE artifact_id=?",
                            (ref["artifact_id"],)).fetchone()
    assert row["kind"] == ref["kind"] and row["message_id"] == fact["message_id"]
    assert json.loads(row["meta"])["hash"] == fact["content_hash"]
    return json.loads(row["content"])[ref["field"]][ref["index"]]


def test_current_dose_and_next_week_dose_are_both_retained_without_plan_execution(tmp_path):
    with closing(_ledger(tmp_path)) as ledger:
        current = "本人は現在合成薬A5mgを服用中です。"
        plan = "本人は来週合成薬A10mgを服用する予定です。"
        _save(ledger, 1, current + plan, {"meds": [_med(current), _med(plan, dose="10mg", status="planned")]})
        view = rollup.build_rollup(ledger.db, 1)
        assert _doses(view, "medications") == {"5mg"}
        assert _doses(view, "planned_medications") == {"10mg"}
        assert len(view["clinical_state"]["facts"]) == 2
        assert len(view["clinical_state"]["groups"]["current"]) == 1
        assert len(view["clinical_state"]["groups"]["plan"]) == 1
        assert {_source_item(ledger, f)["evidence"] for f in view["clinical_state"]["facts"]} == {current, plan}
        saved = json.dumps(view["clinical_state"], ensure_ascii=False)
        assert current not in saved and plan not in saved and '"reported"' not in saved


def test_later_past_stop_does_not_cancel_the_current_use_report(tmp_path):
    with closing(_ledger(tmp_path)) as ledger:
        current = "本人は現在合成薬A5mgを服用中です。"
        past = "本人は以前合成薬Aを中止していました。"
        _save(ledger, 1, current, {"meds": [_med(current)]})
        _save(ledger, 2, past, {"meds": [_med(past, dose=None, status="past", action="stop")]}, "2026-10-02")
        view = rollup.build_rollup(ledger.db, 1)
        assert _doses(view, "medications") == {"5mg"}
        assert len(view["clinical_state"]["groups"]["history"]) == 1
        assert len(view["clinical_state"]["facts"]) == 2


def test_later_historical_absence_does_not_cancel_present_symptom(tmp_path):
    with closing(_ledger(tmp_path)) as ledger:
        current = "本人は現在咳があります。"
        past = "本人は先月咳はありませんでした。"
        _save(ledger, 1, current, {"symptoms": [_sym(current)]})
        _save(ledger, 2, past, {"symptoms": [_sym(past, negated=True, status="past")]}, "2026-10-02")
        view = rollup.build_rollup(ledger.db, 1)
        assert [s["symptom"] for s in view.get("recent_symptoms", [])] == ["咳"]
        assert len(view["clinical_state"]["groups"]["history"]) == 1
        assert len(view["clinical_state"]["facts"]) == 2


def test_same_observation_day_different_doses_stay_conflicting_not_last_wins(tmp_path):
    with closing(_ledger(tmp_path)) as ledger:
        a = "本人は現在合成薬A5mgを服用中です。"
        b = "本人は現在合成薬A10mgを服用中です。"
        _save(ledger, 1, a, {"meds": [_med(a)]})
        _save(ledger, 2, b, {"meds": [_med(b, dose="10mg")]})
        view = rollup.build_rollup(ledger.db, 1)
        assert not view.get("medications")
        assert _doses(view, "unverified_medications") == {"5mg", "10mg"}
        assert len(view["clinical_state"]["groups"]["conflict"]) == 2
        assert len(view["clinical_state"]["facts"]) == 2


def test_same_day_symptom_and_absence_are_both_retained_as_conflict(tmp_path):
    with closing(_ledger(tmp_path)) as ledger:
        a, b = "本人は現在咳があります。", "本人は本日咳はありません。"
        _save(ledger, 1, a, {"symptoms": [_sym(a)]})
        _save(ledger, 2, b, {"symptoms": [_sym(b, negated=True)]})
        view = rollup.build_rollup(ledger.db, 1)
        assert not view.get("recent_symptoms")
        assert len(view["clinical_state"]["groups"]["conflict"]) == 2
        assert {_source_item(ledger, f)["negated"] for f in view["clinical_state"]["facts"]} == {False, True}


def test_current_reported_stop_supersedes_old_use_but_keeps_both_facts(tmp_path):
    with closing(_ledger(tmp_path)) as ledger:
        a, b = "本人は現在合成薬A5mgを服用中です。", "本人は本日合成薬Aを中止しました。"
        _save(ledger, 1, a, {"meds": [_med(a)]})
        _save(ledger, 2, b, {"meds": [_med(b, dose=None, action="stop")]}, "2026-10-02")
        view = rollup.build_rollup(ledger.db, 1)
        assert not view.get("medications")
        assert len(view["clinical_state"]["groups"]["current"]) == 1
        assert len(view["clinical_state"]["groups"]["history"]) == 1
        assert len(view["clinical_state"]["facts"]) == 2


@pytest.mark.parametrize("drop_cache", [False, True])
def test_deleted_stop_source_never_restarts_old_use_even_when_rollup_cache_was_invalidated(tmp_path, drop_cache):
    with closing(_ledger(tmp_path)) as ledger:
        ledger.ensure_patient(1)
        a, b = "本人は現在合成薬A5mgを服用中です。", "本人は本日合成薬Aを中止しました。"
        _save(ledger, 1, a, {"meds": [_med(a)]})
        _save(ledger, 2, b, {"meds": [_med(b, dose=None, action="stop")]}, "2026-10-02")
        rollup.rebuild(ledger, 1)
        ledger.save_messages([_message(mid=2, body="", state="deleted", posted_at="2026-10-02T09:00:00+09:00")])
        if drop_cache:
            with ledger.db:
                ledger.db.execute("DELETE FROM artifacts WHERE kind=?", (rollup.KIND,))
        view = rollup.build_rollup(ledger.db, 1)
        assert not view.get("medications")
        assert _doses(view, "unverified_medications") == {"5mg"}
        dependencies = dependency_loss.read(ledger.db, 1)
        witnesses = dependencies["losses"]
        assert dependencies["complete"] and dependencies["gaps"] == 0
        assert dependencies["coverage"] == {"terminal": 0, "repairable": 0, "reasons": {}}
        assert witnesses and view["clinical_state"]["dependencies"]["complete"]
        assert all("evidence" not in f and "reported" not in f and "evidence_span" not in f for f in witnesses)
        assert b not in json.dumps(witnesses, ensure_ascii=False)
        assert any(r["type"] == "source_withdrawn" for r in view["clinical_state"]["relations"])


def test_edit_of_the_same_source_is_new_evidence_not_resurrection_of_old_dose(tmp_path):
    with closing(_ledger(tmp_path)) as ledger:
        first, edited = "本人は現在合成薬A5mgを服用中です。", "本人は現在合成薬A10mgを服用中です。"
        _save(ledger, 1, first, {"meds": [_med(first)]})
        rollup.rebuild(ledger, 1)
        _save(ledger, 1, edited, {"meds": [_med(edited, dose="10mg")]})
        view = rollup.build_rollup(ledger.db, 1)
        assert _doses(view, "medications") == {"10mg"}
        assert not view.get("unverified_medications")
        assert all(f["content_hash"] == _hash(ledger, 1) for f in view["clinical_state"]["facts"])


def test_missing_grounding_is_a_candidate_not_patient_current(tmp_path):
    with closing(_ledger(tmp_path)) as ledger:
        _save(ledger, 1, "完全合成の処理記録です。", {"meds": [{"name": "合成薬A", "dose": "5mg",
            "status": "current", "subject": "patient", "action": "none", "negated": False}]})
        view = rollup.build_rollup(ledger.db, 1)
        assert not view.get("medications")
        assert _doses(view, "unverified_medications") == {"5mg"}
        assert len(view["clinical_state"]["groups"]["unknown"]) == 1


def test_family_reports_are_retained_as_history_and_never_change_patient_use(tmp_path):
    with closing(_ledger(tmp_path)) as ledger:
        current, family = "本人は現在合成薬A5mgを服用中です。", "母は本日合成薬Aを中止しました。"
        _save(ledger, 1, current, {"meds": [_med(current)]})
        _save(ledger, 2, family, {"meds": [_med(family, dose=None, action="stop", subject="family")]}, "2026-10-02")
        view = rollup.build_rollup(ledger.db, 1)
        assert _doses(view, "medications") == {"5mg"}
        assert len(view["clinical_state"]["groups"]["history"]) == 1
        assert {f["temporal"]["subject"] for f in view["clinical_state"]["facts"]} == {"patient", "family"}


def test_display_limit_does_not_limit_collected_or_projected_facts(tmp_path):
    with closing(_ledger(tmp_path)) as ledger:
        for index in range(35):
            name = f"合成薬A{index}"
            body = f"本人は現在{name}5mgを服用中です。"
            _save(ledger, index + 1, body, {"meds": [_med(body, name=name)]})
        view = rollup.build_rollup(ledger.db, 1)
        assert len(view["medications"]) == 20
        assert len(view["clinical_state"]["facts"]) == len(view["clinical_state"]["groups"]["current"]) == 35
        assert {f["attributes"]["name"] for f in view["clinical_state"]["facts"]} == {f"合成薬A{i}" for i in range(35)}
        assert {_source_item(ledger, f)["name"] for f in view["clinical_state"]["facts"]} == {f"合成薬A{i}" for i in range(35)}


@pytest.mark.parametrize("tail,expected", [("は消失しました。", True), ("は消退しました。", True),
    ("は治まりました。", True), ("は消失していません。", False), ("は消退を認めず。", False),
    ("は治まっていない。", False), ("は消失したら連絡。", None), ("は消失する予定です。", None),
    ("は消失しましたか。", None), ("は消失しないわけではない。", None), ("はありません。", None)])
def test_resolution_is_source_bound_and_independent_of_symptom_negation(tail, expected):
    body = "本人の咳" + tail
    at = body.index("咳")
    assert clinical_scope.source_resolution(body, at, len(body), target_end=at + 1) is expected
    if expected is True:
        item = {"text": "咳", "subject": "patient", "status": "resolved", "negated": False, "evidence": body}
        out = extract_validation.validate({"symptoms": [item]}, body)["symptoms"][0]
        assert out["status"] == "resolved" and out["negated"] is False and not out.get("unverified")


def test_explicit_current_resolution_is_not_an_ongoing_symptom_or_invented_negation(tmp_path):
    with closing(_ledger(tmp_path)) as ledger:
        a, b = "本人は現在咳があります。", "本人の咳は本日消失しました。"
        _save(ledger, 1, a, {"symptoms": [_sym(a)]})
        _save(ledger, 2, b, {"symptoms": [_sym(b, status="resolved")]}, "2026-10-02")
        view = rollup.build_rollup(ledger.db, 1)
        assert not view.get("recent_symptoms")
        fact = next(f for f in view["clinical_state"]["facts"] if f["message_id"] == 2)
        assert fact["attributes"]["negated"] is False and fact["temporal"]["resolution"] is True
        assert fact["id"] in view["clinical_state"]["groups"]["current"]


@pytest.mark.parametrize("report,period", [("本人の以前の咳は消失しました。", "past"),
    ("本人の咳は来週消失する予定です。", "planned"), ("本人の咳が消失したら連絡します。", "conditional")])
def test_resolution_history_plans_and_conditions_do_not_cancel_present_symptom(tmp_path, report, period):
    with closing(_ledger(tmp_path)) as ledger:
        current = "本人は現在咳があります。"
        _save(ledger, 1, current, {"symptoms": [_sym(current)]})
        _save(ledger, 2, report, {"symptoms": [_sym(report, status="resolved")]}, "2026-10-02")
        view = rollup.build_rollup(ledger.db, 1)
        assert [s["symptom"] for s in view.get("recent_symptoms", [])] == ["咳"]
        fact = next(f for f in view["clinical_state"]["facts"] if f["message_id"] == 2)
        assert fact["temporal"]["period"] == period
        assert fact["id"] not in view["clinical_state"]["groups"]["current"]


def test_plan_deadline_passing_never_establishes_that_a_dose_was_taken(tmp_path, monkeypatch):
    with closing(_ledger(tmp_path)) as ledger:
        current = "本人は現在合成薬A5mgを服用中です。"
        plan = "本人は来週合成薬A10mgを服用する予定です。"
        _save(ledger, 1, current + plan, {"meds": [_med(current), _med(plan, dose="10mg", status="planned")]})
        monkeypatch.setattr(rollup.time, "time", lambda: 1_800_000_000.0)
        view = rollup.build_rollup(ledger.db, 1)
        assert _doses(view, "medications") == {"5mg"}
        assert _doses(view, "planned_medications") == {"10mg"}


def test_unbound_artifact_reference_is_explicit_unknown_not_current(tmp_path, monkeypatch):
    with closing(_ledger(tmp_path)) as ledger:
        body = "本人は現在合成薬A5mgを服用中です。"
        _save(ledger, 1, body, {"meds": [_med(body)]})
        monkeypatch.setattr(rollup, "_clinical_artifact_refs", lambda *a: {})
        view = rollup.build_rollup(ledger.db, 1)
        assert not view.get("medications")
        assert _doses(view, "unverified_medications") == {"5mg"}
        assert len(view["clinical_state"]["groups"]["unknown"]) == 1


def test_resolution_does_not_invent_a_symptom_surface_alias():
    body = "本人は解熱しました。"
    out = extract_validation.validate({"symptoms": [{"text": "発熱", "subject": "patient", "status": "resolved",
        "negated": False, "evidence": body}]}, body)
    assert out["symptoms"][0]["unverified"] is True
