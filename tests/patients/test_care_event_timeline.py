"""Synthetic care-event projection and source-boundary checks."""
import json

import care_event_timeline
import pytest


def source(body, *, project_id=1, state="full"):
    return {"project_id": project_id, "message_id": 7, "content_hash": "a" * 64,
            "body_state": state, "body_text": body,
            "posted_at": "2026-10-01T10:00:00+09:00", "posted_at_ts": 1790816400}


def saved(body, kind, subject, status, negated):
    return {"kind": kind, "subject": subject, "status": status, "negated": negated,
            "evidence": body, "span": {"start": 0, "end": len(body)}}


def test_explicit_event_day_is_distinct_from_posted_day_and_only_a_source_reference_is_returned():
    body = "本人は2026年9月1日に退院しました。"
    doc = {"event_records": [saved(body, "discharge", "patient", "current", False)]}
    result = care_event_timeline.project(1, source(body), 42, doc)
    event, = result["events"]
    assert result["coverage"] == {"state": "complete", "records": 1, "invalid": 0, "unverified": 0,
                                  "date_unknown_count": 0}
    assert event["event_day"] == "2026-09-01" and event["posted_at"].startswith("2026-10-01")
    assert (event["project_id"], event["message_id"], event["artifact_id"], event["record_index"]) == (1, 7, 42, 0)
    assert event["span"] == [0, len(body)] and event["content_hash"] == "a" * 64
    assert event["subject"] == "patient" and event["negated"] is False and not event["unverified"]
    assert body not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("body,kind,subject,status,negated", [
    ("家族は2026年9月1日に入院予定です。", "admission", "family", "planned", False),
    ("本人は2026年9月1日に退院していません。", "discharge", "patient", "current", True),
])
def test_family_plans_and_denials_keep_their_source_axes(body, kind, subject, status, negated):
    doc = {"event_records": [saved(body, kind, subject, status, negated)]}
    event, = care_event_timeline.project(1, source(body), 42, doc)["events"]
    assert event["subject"] == subject
    assert event["status"] == status and event["negated"] is negated
    assert not event["unverified"]


@pytest.mark.parametrize("body", [
    "本人は昨日退院しました。",
    "本人は2026年9月1日（昨日）に退院しました。",
    "2026年9月1日の記録です。本人は退院しました。",
    "本人は2026年2月30日に退院しました。",
])
def test_unbound_relative_or_invalid_dates_are_not_inferred(body):
    item = saved(body, "discharge", "patient", "past" if "昨日" in body else "current", False)
    event, = care_event_timeline.project(1, source(body), 42, {"event_records": [item]})["events"]
    assert event["event_day"] is None
    assert body not in json.dumps(event, ensure_ascii=False)


def test_invalid_original_span_stays_unverified_even_if_the_quote_can_be_found():
    body = "本人は2026年9月1日に退院しました。"
    item = saved(body, "discharge", "patient", "current", False)
    item["span"] = {"start": 1, "end": len(body)}
    result = care_event_timeline.project(1, source(body), 42, {"event_records": [item]})
    event, = result["events"]
    assert result["coverage"]["state"] == "incomplete"
    assert event["unverified"] and event["span"] is None and event["event_day"] is None


@pytest.mark.parametrize("body", [
    "次の記載は誤りです。本人は2026年9月1日に退院しました。",
    "家族の話です。本人は2026年9月1日に退院しました。",
    "本人は2026年9月1日に退院しました。その記載を訂正します。",
    "本人は2026年9月1日に退院しましたが、その記載は誤りです。",
    "本人は2026年9月1日に退院しましたが、実際には違います。",
])
def test_surrounding_subject_or_correction_never_promotes_a_fragment(body):
    quote = "本人は2026年9月1日に退院しました"
    start = body.index(quote)
    item = {**saved(quote, "discharge", "patient", "current", False),
            "span": {"start": start, "end": start + len(quote)}}
    result = care_event_timeline.project(1, source(body), 42, {"event_records": [item]})
    event, = result["events"]
    assert event["unverified"] is True and event["subject"] == "unknown"
    assert event["status"] == "unknown" and event["negated"] is None and event["event_day"] is None
    assert result["coverage"]["state"] == "incomplete" and result["coverage"]["unverified"] == 1


@pytest.mark.parametrize("body", [
    "本人は2026年9月1日に退院しましたが、実際には違います。",
    "本人は2026年9月1日に退院しました、実際には違います。",
    "本人は2026年9月1日に退院しました（事実ではありません）。",
])
def test_correction_within_a_full_quoted_sentence_never_confirms_event(body):
    item = saved(body, "discharge", "patient", "current", False)
    event, = care_event_timeline.project(1, source(body), 42, {"event_records": [item]})["events"]
    assert event["unverified"] is True and event["subject"] == "unknown"
    assert event["status"] == "unknown" and event["negated"] is None
    assert event["event_day"] is None


def test_single_full_assertion_remains_verified_when_quote_omits_final_punctuation():
    body = "本人は2026年9月1日に退院しました。"
    quote = body[:-1]
    item = {**saved(quote, "discharge", "patient", "current", False),
            "span": {"start": 0, "end": len(quote)}}
    event, = care_event_timeline.project(1, source(body), 42, {"event_records": [item]})["events"]
    assert not event["unverified"] and event["subject"] == "patient"
    assert event["status"] == "current" and event["event_day"] == "2026-09-01"


def test_event_paired_date_conflict_is_separate_from_event_evidence_validation():
    body = "本人は2026年9月1日（昨日）に退院しました。"
    item = saved(body, "discharge", "patient", "past", False)
    event, = care_event_timeline.project(1, source(body), 42, {"event_records": [item]})["events"]
    assert event["subject"] == "patient" and event["status"] == "past"
    assert event["date_uncertain"] is True and event["event_day"] is None
    assert event["span"] == [0, len(body)]


def test_future_reported_as_completed_is_unknown_but_future_plan_is_preserved():
    complete = "本人は2026年12月1日に退院しました。"
    result = care_event_timeline.project(1, source(complete), 42,
        {"event_records": [saved(complete, "discharge", "patient", "current", False)]})
    event, = result["events"]
    assert event["event_day"] == "2026-12-01"
    assert event["reported_status"] == "current" and event["status"] == "unknown"
    assert event["date_uncertain"] is True
    assert result["coverage"]["date_unknown_count"] == 1
    planned = "本人は2026年12月1日に退院予定です。"
    event, = care_event_timeline.project(1, source(planned), 42,
        {"event_records": [saved(planned, "discharge", "patient", "planned", False)]})["events"]
    assert event["event_day"] == "2026-12-01"
    assert event["status"] == "planned" and event["date_uncertain"] is False


@pytest.mark.parametrize("weekday,expected_day,uncertain", [
    ("火曜", "2026-09-01", False), ("水曜日", None, True),
    ("火", "2026-09-01", False), ("水", None, True),
])
def test_explicit_weekday_pair_must_match_date(weekday, expected_day, uncertain):
    body = f"本人は2026年9月1日（{weekday}）に退院しました。"
    result = care_event_timeline.project(1, source(body), 42,
        {"event_records": [saved(body, "discharge", "patient", "current", False)]})
    event, = result["events"]
    assert event["event_day"] == expected_day and event["date_uncertain"] is uncertain
    assert event["status"] == ("unknown" if uncertain else "current")
    assert result["coverage"]["date_unknown_count"] == int(uncertain)


@pytest.mark.parametrize("weekday,status", [("水曜日", "current"), ("火曜", "current"),
                                           ("火曜", "planned"), ("水", "current"),
                                           ("火", "current"), ("水", "planned")])
def test_weekday_elsewhere_in_quote_does_not_confirm_the_event_day(weekday, status):
    ending = "退院予定です" if status == "planned" else "退院しました"
    body = f"本人は2026年9月1日に{ending}（{weekday}）。"
    result = care_event_timeline.project(1, source(body), 42,
        {"event_records": [saved(body, "discharge", "patient", status, False)]})
    event, = result["events"]
    assert event["event_day"] is None and event["date_uncertain"] is True
    assert event["status"] == ("unknown" if status == "current" else "planned")
    assert result["coverage"]["date_unknown_count"] == 1


def test_malformed_source_fields_do_not_echo_raw_text_and_date_coverage_is_distinct():
    body = "本人は退院しました。"
    msg = source(body)
    msg["posted_at"] = "合成機密値"
    msg["posted_at_ts"] = "合成機密値"
    result = care_event_timeline.project(1, msg, 42,
        {"event_records": [saved(body, "discharge", "patient", "current", False)]})
    event, = result["events"]
    assert result["coverage"]["state"] == "complete"
    assert result["coverage"]["date_unknown_count"] == 1
    assert event["posted_at"] is None and event["posted_at_ts"] is None
    assert "合成機密値" not in json.dumps(result, ensure_ascii=False)
    msg["content_hash"] = "合成機密値"
    assert care_event_timeline.project(1, msg, 42, {"event_records": []})["coverage"]["state"] == "invalid_source"
    msg = source(body)
    msg["project_id"] = True
    assert care_event_timeline.project(1, msg, 42, {"event_records": []})["coverage"]["state"] == "invalid_source"


def test_invalid_record_does_not_hide_valid_sibling_or_claim_complete_coverage():
    body = "本人は2026年9月1日に退院しました。"
    doc = {"event_records": [{"kind": "unknown"}, saved(body, "discharge", "patient", "current", False)]}
    result = care_event_timeline.project(1, source(body), 42, doc)
    assert result["coverage"] == {"state": "incomplete", "records": 1, "invalid": 1, "unverified": 0,
                                  "date_unknown_count": 0}
    assert result["events"][0]["record_index"] == 1


def test_source_scope_deleted_and_unanalyzed_are_not_observed_zero():
    body = "本人は退院しました。"
    doc = {"event_records": [saved(body, "discharge", "patient", "current", False)]}
    assert care_event_timeline.project(2, source(body), 42, doc)["coverage"]["state"] == "invalid_source"
    assert care_event_timeline.project(1, source(body, state="deleted"), 42, doc)["coverage"]["state"] == "deleted"
    assert care_event_timeline.project(1, source(body, state="snippet"), 42, doc)["coverage"]["state"] == "incomplete_source"
    assert care_event_timeline.project(1, source(body), None, None)["coverage"]["state"] == "not_extracted"
    assert care_event_timeline.project(1, source(body), 42, {})["coverage"]["state"] == "not_recorded"
    assert care_event_timeline.project(1, source(body), 42, {"event_records": None})["coverage"]["state"] == "invalid_records"
    empty = care_event_timeline.project(1, source(body), 42, {"event_records": []})
    assert empty["coverage"] == {"state": "complete", "records": 0, "invalid": 0, "unverified": 0,
                                 "date_unknown_count": 0}
