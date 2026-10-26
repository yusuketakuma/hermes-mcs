"""Synthetic checks for contradictory dates in one medication assertion."""
from contextlib import closing
from datetime import datetime
import json

import pytest

import extract_validation
import inference_version as inference
import patient_context_state
import rollup
from extract_testkit import _hash, _ledger, _message


def test_disagreeing_written_and_relative_dates_do_not_become_one_event_day(tmp_path):
    with closing(_ledger(tmp_path)) as store:
        for mid, name, day in ((1, "合成薬A", "2026年9月1日"),
                               (2, "合成薬B", "2026年9月30日")):
            body = f"本人は{day}（昨日）に{name}を中止しました。"
            item = {"name": name, "subject": "patient", "status": "past",
                    "action": "stop", "negated": False, "evidence": body}
            doc = extract_validation.validate({"symptoms": [], "meds": [item]}, body)
            assert len(doc["meds"]) == 1 and doc["meds"][0]["evidence"] == body
            store.save_messages([_message(mid=mid, body=body,
                                          posted_at="2026-10-01T10:00:00+09:00")])
            store.artifact_add("extract_llm", json.dumps(doc, ensure_ascii=False),
                               project_id=1, message_id=mid,
                               meta={"hash": _hash(store, mid),
                                     inference.VERSION_KEY: inference.CURRENT_VERSION})
        result = rollup.build_rollup(store.db, 1)
        projection = result["clinical_state"]
        facts = {fact["attributes"]["name"]: fact for fact in projection["facts"]}
        conflicting = facts["合成薬A"]
        assert conflicting["temporal"]["event_day"] is None
        assert conflicting["temporal"]["unknown"] is True
        assert conflicting["temporal"]["period"] == "past"
        assert conflicting["temporal"]["state_day"] is None
        assert conflicting["temporal"]["as_of_day"] is None
        assert conflicting["id"] in projection["groups"]["history"]
        assert conflicting["id"] not in projection["groups"]["current"]
        assert all(m["name"] != "合成薬A" for m in result.get("medications", []))
        assert "2026-09-01" not in json.dumps(conflicting, ensure_ascii=False)
        assert facts["合成薬B"]["temporal"]["event_day"] == "2026-09-30"


def test_paired_date_with_unknown_posted_time_is_not_claimed_as_verified():
    body = "本人は2026年9月1日（昨日）に合成薬Aを中止しました。"
    message = {"project_id": 1, "message_id": 1, "content_hash": "synthetic",
               "body_text": body, "body_state": "full", "posted_at_ts": None}
    item = {"name": "合成薬A", "subject": "patient", "status": "past",
            "action": "stop", "negated": False, "evidence": body}
    fact = patient_context_state.clinical_state_fact(message, item, "medications")
    assert fact["evidence"] == body
    assert fact["temporal"]["event_day"] is None
    assert fact["temporal"]["unknown"] is True


@pytest.mark.parametrize("text, expected", [
    ("2026年10月1日（木）に採血した。", False),
    ("2026-10-01(木曜日)に採血した。", False),
    ("2026年10月1日（金）に採血した。", True),
    ("2026年2月30日（月）に採血した。", True),
    ("2026年10月1日に採血し、金曜日に訪問する。", True),
    ("2026年10月1日（木）に採血し、金曜日に訪問する。", True),
    ("2026年10月1日に採血し、別の予定は（金）。", True),
    ("2026年10月1日に採血した。", False),
])
def test_written_weekday_must_match_its_directly_paired_date(text, expected):
    assert patient_context_state.paired_date_conflict(text, POSTED_DAY) is expected


POSTED_DAY = datetime.fromisoformat("2026-10-02T10:00:00+09:00").timestamp()


@pytest.mark.parametrize("text,expected", [
    ("本日（金曜日）に中止しました。", False),
    ("今日(金)に中止しました。", False),
    ("昨日（木曜）に中止しました。", False),
    ("明日（土）に開始予定です。", False),
    ("翌日（土曜日）に開始予定です。", True),
    ("本日（木曜日）に中止しました。", True),
    ("明日（金）に開始予定です。", True),
    ("本日（金）に中止し、別の予定は月曜日です。", True),
    ("昨日（木）に中止し、本日（金）に再開しました。", True),
    ("2026年10月1日（木）に中止し、本日（金）に再開しました。", True),
    ("昨日に中止し、本日（金）に再開しました。", True),
    ("2026年10月1日（木）に採血し、昨日合成薬Aを中止しました。", True),
    ("2026年10月1日に採血し、昨日合成薬Aを中止しました。", True),
    ("2026年10月2日（本日）に合成薬Aを中止しました。", False),
    ("2026年10月1日（翌日）に合成薬Aを中止しました。", True),
    ("翌日合成薬Aを中止しました。", True),
    ("本日中止し、別の予定は金曜日です。", True),
    ("10月1日に中止し、別の予定は金曜日です。", True),
    ("2026年2月30日に中止し、別の予定は金曜日です。", True),
    ("2026年10月1日と2026年10月2日に服用、金曜日に検査。", True),
    ("毎週月曜日に服用しています。", False),
    ("服用は1回（日）です。", False),
])
def test_relative_weekday_requires_direct_consistent_pair(text, expected):
    assert patient_context_state.paired_date_conflict(text, POSTED_DAY) is expected


def test_relative_weekday_uses_jst_and_requires_observed_posting_time():
    utc_late = datetime.fromisoformat("2026-10-01T15:30:00+00:00").timestamp()
    assert not patient_context_state.paired_date_conflict("本日（金）に中止。", utc_late)
    assert patient_context_state.paired_date_conflict("本日（木）に中止。", utc_late)
    assert patient_context_state.paired_date_conflict("本日（金）に中止。", None)
    last_day = datetime.fromisoformat("9999-12-31T12:00:00+09:00").timestamp()
    assert patient_context_state.paired_date_conflict("明日（土）に開始予定。", last_day)


@pytest.mark.parametrize("weekday,expected_day,uncertain", [
    ("金曜日", "2026-10-02", False), ("木曜日", None, True),
])
def test_relative_weekday_is_bound_to_jst_for_medication_fact(weekday, expected_day, uncertain):
    body = f"本人は本日（{weekday}）に合成薬Aを中止しました。"
    message = {"project_id": 1, "message_id": 1, "content_hash": "synthetic",
               "body_text": body, "body_state": "full", "posted_at_ts": POSTED_DAY}
    item = {"name": "合成薬A", "subject": "patient", "status": "past",
            "action": "stop", "negated": False, "evidence": body}
    temporal = patient_context_state.clinical_state_fact(message, item, "medications")["temporal"]
    assert temporal["event_day"] == expected_day and temporal["unknown"] is uncertain


def test_recurring_weekday_never_creates_a_particular_event_day():
    body = "本人は合成薬Aを毎週月曜日に服用しています。"
    message = {"project_id": 1, "message_id": 1, "content_hash": "synthetic",
               "body_text": body, "body_state": "full", "posted_at_ts": POSTED_DAY}
    item = {"name": "合成薬A", "subject": "patient", "status": "current",
            "action": "none", "negated": False, "evidence": body}
    temporal = patient_context_state.clinical_state_fact(message, item, "medications")["temporal"]
    assert temporal["event_day"] is None


def test_conflicting_weekday_does_not_become_a_medication_event_day():
    body = "本人は2026年10月1日（金）に合成薬Aを中止しました。"
    message = {"project_id": 1, "message_id": 1, "content_hash": "synthetic",
               "body_text": body, "body_state": "full", "posted_at_ts": POSTED_DAY}
    item = {"name": "合成薬A", "subject": "patient", "status": "past",
            "action": "stop", "negated": False, "evidence": body}
    fact = patient_context_state.clinical_state_fact(message, item, "medications")
    assert fact["evidence"] == body
    assert fact["temporal"]["event_day"] is None
    assert fact["temporal"]["unknown"] is True


def test_uncertain_later_date_cannot_supersede_a_different_supported_current_value():
    facts = []
    for mid, date, dose, body in (
        (1, "2026-09-30T10:00:00+09:00", "5mg", "本人は現在合成薬A5mgを服用しています。"),
        (2, "2026-10-01T10:00:00+09:00", "10mg",
         "本人は現在合成薬A10mgを2026年9月1日（昨日）から服用しています。"),
    ):
        message = {"project_id": 1, "message_id": mid, "content_hash": str(mid) * 64,
                   "body_text": body, "body_state": "full",
                   "posted_at_ts": datetime.fromisoformat(date).timestamp()}
        item = {"name": "合成薬A", "dose": dose, "subject": "patient", "status": "current",
                "action": "none", "negated": False, "evidence": body}
        facts.append(patient_context_state.clinical_state_fact(message, item, "medications"))
    earlier, uncertain = facts
    assert earlier["temporal"]["unknown"] is False
    assert uncertain["temporal"]["unknown"] is True
    assert uncertain["temporal"]["event_day"] is None
    assert uncertain["temporal"]["state_day"] is None and uncertain["temporal"]["as_of_day"] is None
    projected = patient_context_state.project(facts)
    assert set(projected["groups"]["unknown"]) == {earlier["id"], uncertain["id"]}
    assert not projected["groups"]["history"] and not projected["groups"]["current"]


def test_confirmed_same_day_conflict_survives_an_unknown_and_other_subjects():
    cases = (
        (1, "本人は現在合成薬A5mgを服用しています。", "patient", "5mg"),
        (2, "本人は現在合成薬A15mgを服用しています。", "patient", "15mg"),
        (3, "本人は現在合成薬A10mgを2026年9月1日（昨日）から服用しています。", "patient", "10mg"),
        (4, "家族は現在合成薬A20mgを服用しています。", "family", "20mg"),
        (5, "本人は以前合成薬A25mgを服用していました。", "patient", "25mg"),
    )
    facts = []
    for mid, body, subject, dose in cases:
        date = "2026-10-01T10:00:00+09:00" if mid == 3 else "2026-09-30T10:00:00+09:00"
        message = {"project_id": 1, "message_id": mid, "content_hash": str(mid) * 64,
                   "body_text": body, "body_state": "full",
                   "posted_at_ts": datetime.fromisoformat(date).timestamp()}
        item = {"name": "合成薬A", "dose": dose, "subject": subject, "status": "current",
                "action": "none", "negated": False, "evidence": body}
        facts.append(patient_context_state.clinical_state_fact(message, item, "medications"))
    groups = patient_context_state.project(facts)["groups"]
    assert set(groups["conflict"]) == {facts[0]["id"], facts[1]["id"]}
    assert groups["unknown"] == [facts[2]["id"]]
    assert set(groups["history"]) == {facts[3]["id"], facts[4]["id"]}


def test_start_date_and_later_ongoing_observation_are_not_compared_as_one_day():
    body = "本人は2026年9月1日に合成薬Aを開始し、昨日から服用しています。"
    posted = datetime.fromisoformat("2026-10-01T10:00:00+09:00").timestamp()
    message = {"project_id": 1, "message_id": 1, "content_hash": "synthetic",
               "body_text": body, "body_state": "full", "posted_at_ts": posted}
    item = {"name": "合成薬A", "subject": "patient", "status": "current",
            "action": "start", "negated": False, "evidence": body}
    fact = patient_context_state.clinical_state_fact(message, item, "medications")
    assert fact["evidence"] == body
    assert fact["temporal"]["event_day"] == "2026-09-01"
    assert fact["temporal"]["unknown"] is False


@pytest.mark.parametrize("body", [
    "本人は2026年10月1日（木）に採血し、昨日合成薬Aを中止しました。",
    "本人は2026年10月1日に採血し、昨日合成薬Aを中止しました。",
    "本人は2026年9月1日に合成薬Aを開始し、昨日から合成薬Bを服用しています。",
    "本人は翌日合成薬Aを中止しました。",
    "本人は2026年10月1日（翌日）に合成薬Aを中止しました。",
])
def test_unbound_mixed_or_next_day_does_not_date_a_medication_event(body):
    message = {"project_id": 1, "message_id": 1, "content_hash": "synthetic",
               "body_text": body, "body_state": "full", "posted_at_ts": POSTED_DAY}
    item = {"name": "合成薬A", "subject": "patient", "status": "past",
            "action": "stop", "negated": False, "evidence": body}
    temporal = patient_context_state.clinical_state_fact(message, item, "medications")["temporal"]
    assert temporal["event_day"] is None and temporal["unknown"] is True


def test_symptom_paired_date_conflict_keeps_past_period_and_source():
    body = "本人は2026年9月1日（昨日）に発熱しました。"
    posted = datetime.fromisoformat("2026-10-01T10:00:00+09:00").timestamp()
    message = {"project_id": 1, "message_id": 1, "content_hash": "synthetic",
               "body_text": body, "body_state": "full", "posted_at_ts": posted}
    item = {"text": "発熱", "subject": "patient", "status": "past",
            "negated": False, "evidence": body}
    fact = patient_context_state.clinical_state_fact(message, item, "symptoms")
    assert fact["evidence"] == body and fact["temporal"]["subject"] == "patient"
    assert fact["temporal"]["period"] == "past"
    assert fact["temporal"]["event_day"] is None and fact["temporal"]["unknown"] is True
    assert patient_context_state.project([fact])["groups"]["history"] == [fact["id"]]


def test_relative_date_after_latest_supported_day_is_unknown_without_crashing():
    body = "本人は明日合成薬Aを中止する予定です。"
    posted = datetime.fromisoformat("9999-12-31T12:00:00+09:00").timestamp()
    message = {"project_id": 1, "message_id": 1, "content_hash": "synthetic",
               "body_text": body, "body_state": "full", "posted_at_ts": posted}
    item = {"name": "合成薬A", "subject": "patient", "status": "planned",
            "action": "stop", "negated": False, "evidence": body}
    fact = patient_context_state.clinical_state_fact(message, item, "medications")
    assert fact["evidence"] == body
    assert fact["temporal"]["event_day"] is None
    assert fact["temporal"]["unknown"] is True


@pytest.mark.parametrize("body,subject,expected_day,uncertain", [
    ("家族は2026年10月2日（明日）に合成薬Aを中止する予定です。", "family", "2026-10-02", False),
    ("本人は2026年2月30日（昨日）に合成薬Aを中止しました。", "patient", None, True),
])
def test_paired_future_family_and_invalid_date_remain_distinct(body, subject, expected_day, uncertain):
    posted = datetime.fromisoformat("2026-10-01T10:00:00+09:00").timestamp()
    message = {"project_id": 1, "message_id": 1, "content_hash": "synthetic",
               "body_text": body, "body_state": "full", "posted_at_ts": posted}
    item = {"name": "合成薬A", "subject": subject, "status": "planned" if subject == "family" else "past",
            "action": "stop", "negated": False, "evidence": body}
    fact = patient_context_state.clinical_state_fact(message, item, "medications")
    assert fact["temporal"]["subject"] == subject
    assert fact["temporal"]["event_day"] == expected_day
    assert fact["temporal"]["unknown"] is uncertain
    if subject == "family":
        assert fact["temporal"]["period"] == "planned"
