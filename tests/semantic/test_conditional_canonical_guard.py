"""Synthetic canonical projections cannot turn uncertain source time into fact."""

import pytest

from semantic_projection import project_v2_doc_legacy
from semantic_testkit import v2_doc, v2_fact


def _project(quote, *, kind, subject="patient:1", polarity="affirmed", **extra):
    evidence = {"evidence_id": "e1", "message_id": "m1", "revision": "r1",
                "start": 0, "end": len(quote), "quote": quote, "atom_id": "a1"}
    fields = {"kind": kind, "subject": subject, "statement": quote,
              "polarity": polarity, "workflow_status": "done", "valid_time": "current",
              "evidence_ids": ["e1"], **extra}
    fact = v2_fact("f1", **fields)
    return project_v2_doc_legacy(v2_doc([fact], [evidence]))


@pytest.mark.parametrize("quote,subject,polarity,expected_subject", [
    ("本日、本人は以前、入院もしたことがあります。", "patient:1", "affirmed", "patient"),
    ("本日、家族は以前、入院もしたことがあります。", "role:family", "affirmed", "family"),
    ("本日、本人は以前、入院もしたことがありません。", "patient:1", "negated", "patient"),
])
def test_unknown_event_time_cannot_become_current_from_today(quote, subject, polarity, expected_subject):
    result = _project(quote, kind="care_event", subject=subject, polarity=polarity)
    record, = result["event_records"]
    assert record["kind"] == "admission" and record["subject"] == expected_subject
    assert record["status"] == "unknown" and record["unverified"] is True
    assert record["negated"] is (polarity == "negated")
    assert record["evidence"] == quote and record["span"] == {"start": 0, "end": len(quote)}
    assert "admission" not in result.get("events", [])


def test_current_source_admission_remains_confirmed():
    quote = "本人は本日入院しました。"
    result = _project(quote, kind="care_event")
    record, = result["event_records"]
    assert (record["subject"], record["status"], record["negated"]) == ("patient", "current", False)
    assert not record.get("unverified") and result["events"] == ["admission"]


@pytest.mark.parametrize("quote", [
    "本人は本日アムロジピンの中止もしたい。",
    "本人は本日アムロジピンの中止もしないとだめです。",
    "本人は本日アムロジピンの中止もしませんでした。",
    "本人は本日アムロジピンを中止しましたか？",
    "本人は本日アムロジピンを中止しましたが、その記録は誤りです。",
    "本人はアムロジピンを中止しました。しかし実際には中止していません。",
    "本人はアムロジピンを中止しました、しかし中止していません。",
    "もし発熱したら、本人は本日アムロジピンを中止しました。",
    "家族は本日アムロジピンを中止しました。",
    "アムロジピンを中止しました。",
])
def test_done_stop_does_not_override_uncertain_or_negative_source(quote):
    result = _project(quote, kind="medication_event", action="stop", valid_time="past")
    med, = result["meds"]
    assert med["action"] == "stop" and med["evidence"] == quote
    assert med["status"] is None and med["unverified"] is True


def test_stop_without_action_in_evidence_remains_unverified():
    quote = "本人はアムロジピンを服用しています。"
    result = _project(quote, kind="medication_event", action="stop", valid_time="past")
    med, = result["meds"]
    assert med["status"] is None and med["unverified"] is True
    assert med["evidence"] == quote


def test_cancelled_start_does_not_become_a_verified_past_start():
    quote = "本人は本日アムロジピンを中止しました。"
    result = _project(quote, kind="medication_event", action="start",
                      workflow_status="cancelled", valid_time="past")
    med, = result["meds"]
    assert med["status"] is None and med["unverified"] is True
    assert med["evidence"] == quote


def test_later_denial_does_not_become_a_verified_start():
    quote = "本人は本日アムロジピンを開始しましたが、開始していません。"
    result = _project(quote, kind="medication_event", action="start", valid_time="current")
    med, = result["meds"]
    assert med["status"] is None and med["unverified"] is True
    assert med["evidence"] == quote


@pytest.mark.parametrize("quote", [
    "本人は本日アムロジピンを開始しました、実際には開始しなかった。",
    "本人は本日アムロジピンを開始しましたが、開始していなかった。",
])
def test_later_past_denial_does_not_become_verified_start(quote):
    med, = _project(quote, kind="medication_event", action="start", valid_time="current")["meds"]
    assert med["status"] is None and med["unverified"] is True


@pytest.mark.parametrize("quote", [
    "アムロジピンを服用しています。",
    "家族はアムロジピンを服用しています。",
])
def test_continuation_does_not_infer_patient_from_fact_alone(quote):
    med, = _project(quote, kind="medication_event", action="continue", valid_time="current")["meds"]
    assert med["status"] is None and med["unverified"] is True
    assert med["evidence"] == quote


def test_explicit_patient_continuation_remains_current():
    quote = "本人は現在アムロジピンを服用しています。"
    med, = _project(quote, kind="medication_event", action="continue", valid_time="current")["meds"]
    assert med["status"] == "current" and not med.get("unverified")


def test_another_drugs_continuation_does_not_confirm_target():
    quote = "本人はアセトアミノフェンを中止し、漢方薬を服用しています。"
    med, = _project(quote, kind="medication_event", action="continue", valid_time="current",
                    statement="アセトアミノフェンを継続")["meds"]
    assert med["status"] is None and med["unverified"] is True


def test_family_plan_does_not_confirm_patient_plan():
    quote = "家族はアムロジピンを中止する予定です。"
    med, = _project(quote, kind="medication_event", action="stop", valid_time="planned",
                    workflow_status="planned")["meds"]
    assert med["status"] is None and med["unverified"] is True


def test_explicit_done_stop_remains_past():
    quote = "本人は本日アムロジピンを中止しました。"
    result = _project(quote, kind="medication_event", action="stop", valid_time="past")
    med, = result["meds"]
    assert med["action"] == "stop" and med["status"] == "past"
    assert not med.get("unverified") and med["evidence"] == quote
