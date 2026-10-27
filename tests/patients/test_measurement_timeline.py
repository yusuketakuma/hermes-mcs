"""Synthetic measurement timeline: source time, identity and uncertainty remain separate."""
import hashlib

import measurement_timeline


def _record(mid, measured, posted, value, *, project=1, subject="patient"):
    body = f"本人の{measured}空腹時採血 Cr {value} mg/dL。"
    return {"project_id": project, "message_id": mid, "content_hash": hashlib.sha256(f"synthetic-{mid}".encode()).hexdigest(),
            "kind": "lab", "key": "creatinine", "name": "Cr", "value": value, "unit": "mg/dL", "condition": "空腹時",
            "measured_on": measured, "posted_at": posted, "subject": subject,
            "verification": "quote_supported", "negated": False, "status": "current", "source_body": body, "evidence": body,
            "evidence_span": [0, len(body)], "source_ref": {"artifact_id": mid, "field": "labs", "index": 0}}


def test_old_measurement_posted_later_remains_history_with_source_references():
    a = _record(1, "2026-09-01", "2026-10-05T09:00:00+09:00", "1.0")
    b = _record(2, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.5")
    result = measurement_timeline.project_measurements([a, b], source_complete=True)
    assert [item["state"] for item in result["items"]] == ["history", "current"]
    assert [item["message_id"] for item in result["items"]] == [1, 2]
    assert result["items"][0]["evidence_span"] == [0, len(a["source_body"])]
    assert all("source_body" not in item and "evidence" not in item for item in result["items"])
    assert result["coverage"]["input_count"] == result["coverage"]["patient_count"] == 2


def test_same_date_different_records_and_other_patient_isolation():
    a = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    b = _record(2, "2026-10-01", "2026-10-02T09:00:00+09:00", "1.5")
    c = _record(3, "2026-10-01", "2026-10-02T09:00:00+09:00", "2.0", project=2)
    family = _record(4, "2026-10-01", "2026-10-02T09:00:00+09:00", "9.0", subject="family")
    result = measurement_timeline.project_measurements([a, b, c, family], source_complete=True)
    assert [item["state"] for item in result["items"]] == ["different_records", "different_records", "current"]
    assert [item["value"] for item in result["items"][:2]] == ["1.0", "1.5"]
    assert result["coverage"]["excluded_other"] == 1


def test_missing_time_or_bad_source_blocks_current_without_claiming_complete():
    a = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    b = _record(2, "2026-10-02", "2026-10-02T09:00:00+09:00", "1.5")
    b["evidence_span"] = [0, 2]
    result = measurement_timeline.project_measurements([a, b], source_complete=False)
    by_message = {item["message_id"]: item for item in result["items"]}
    assert by_message[1]["state"] == "observed" and by_message[1]["value"] == "1.0"
    assert by_message[2]["state"] == "unknown" and by_message[2]["value"] is None
    assert result["coverage"]["source_complete"] is False
    assert result["coverage"]["unknown_count"] == 1 and result["coverage"]["observed_count"] == 1


def test_future_date_is_not_a_current_measurement_and_invalid_input_is_counted():
    a = _record(1, "2026-10-03", "2026-10-01T09:00:00+09:00", "1.0")
    result = measurement_timeline.project_measurements([a, None], source_complete=False)
    assert result["items"][0]["state"] == "unknown"
    assert result["coverage"]["invalid_count"] == 1


def test_undated_observation_is_not_replaced_by_newer_posting_time():
    known = _record(1, "2026-10-01", "2026-10-02T09:00:00+09:00", "1.0")
    unknown = _record(2, "2026-10-02", "2026-10-03T09:00:00+09:00", "1.5")
    unknown["measured_on"] = None
    result = measurement_timeline.project_measurements([known, unknown], source_complete=True)
    assert {item["state"] for item in result["items"]} == {"observed", "unknown"}
    assert next(item for item in result["items"] if item["state"] == "observed")["value"] == "1.0"
    assert result["coverage"]["source_complete"] is True
    assert result["coverage"]["unknown_count"] == 1


def test_plan_and_reference_output_keep_no_raw_text():
    a = _record(1, "2026-10-03", "2026-10-01T09:00:00+09:00", "1.0")
    a["status"] = "planned"
    a["source_body"] = a["evidence"] = "本人は2026-10-03に空腹時採血 Cr 1.0 mg/dLの検査予定。"
    a["evidence_span"] = [0, len(a["source_body"])]
    a["source_ref"]["unexpected_body"] = a["source_body"]
    result = measurement_timeline.project_measurements([a], source_complete=True)
    assert result["items"][0]["state"] == "plan"
    assert result["items"][0]["source_ref"] == {"artifact_id": 1, "field": "labs", "index": 0}
    assert a["source_body"] not in str(result)


def test_incomplete_coverage_and_invalid_rows_do_not_claim_a_current_value():
    a = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    incomplete = measurement_timeline.project_measurements([a], source_complete=False)
    assert incomplete["items"][0]["state"] == "observed"
    assert incomplete["items"][0]["value"] == "1.0"
    assert incomplete["coverage"]["source_complete"] is False
    invalid = measurement_timeline.project_measurements([a, {}], source_complete=True)
    assert invalid["items"][0]["state"] == "observed"
    assert invalid["items"][0]["value"] == "1.0"
    assert invalid["coverage"]["invalid_count"] == 1


def test_qualifying_a_different_clause_or_negation_never_creates_a_current_value():
    a = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    body = "本人は2026-10-01に受診。後日の採血 Cr 1.0 mg/dL。"
    a["source_body"] = a["evidence"] = body
    a["evidence_span"] = [0, len(body)]
    result = measurement_timeline.project_measurements([a], source_complete=True)
    assert result["items"][0]["state"] == "unknown"
    b = _record(2, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    b["negated"] = True
    assert measurement_timeline.project_measurements([b], source_complete=True)["items"][0]["state"] == "unknown"


def test_same_day_same_normalized_value_and_unit_is_not_a_conflict():
    a = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    b = _record(2, "2026-10-01", "2026-10-02T09:00:00+09:00", "1.00")
    result = measurement_timeline.project_measurements([a, b], source_complete=True)
    assert [item["state"] for item in result["items"]] == ["current", "current"]
    assert [item["value"] for item in result["items"]] == ["1.0", "1.00"]


def test_paired_absolute_and_relative_dates_are_compared_in_jst():
    for date_text, expected in (("2026-09-30（昨日）", "current"),
                                ("2026-09-01（昨日）", "unknown")):
        measured = date_text[:10]
        a = _record(1, measured, "2026-09-30T16:00:00+00:00", "1.0")
        body = f"本人は{date_text}の空腹時採血 Cr 1.0 mg/dL。"
        a.update(source_body=body, evidence=body, evidence_span=[0, len(body)])
        item = measurement_timeline.project_measurements([a], source_complete=True)["items"][0]
        assert item["state"] == expected
        if expected == "unknown":
            assert item["value"] is None


def test_paired_weekday_disagreement_is_unknown():
    for date_text, expected in (("2026-10-01（木）", "current"),
                                ("2026-10-01（水）", "unknown")):
        a = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
        body = f"本人は{date_text}の空腹時採血 Cr 1.0 mg/dL。"
        a.update(source_body=body, evidence=body, evidence_span=[0, len(body)])
        assert measurement_timeline.project_measurements([a], source_complete=True)["items"][0]["state"] == expected


def test_paired_date_contradiction_does_not_become_a_plan():
    a = _record(1, "2026-10-03", "2026-10-01T09:00:00+09:00", "1.0")
    a["status"] = "planned"
    body = "本人は2026-10-03（明日）に採血 Cr 1.0 mg/dLの検査予定。"
    a.update(source_body=body, evidence=body, evidence_span=[0, len(body)])
    assert measurement_timeline.project_measurements([a], source_complete=True)["items"][0]["state"] == "unknown"


def test_same_value_on_separate_measurement_days_remains_history_and_current():
    old = _record(1, "2026-09-01", "2026-10-01T09:00:00+09:00", "1.0")
    new = _record(2, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    items = measurement_timeline.project_measurements([new, old], source_complete=True)["items"]
    assert [(item["message_id"], item["state"]) for item in items] == [(1, "history"), (2, "current")]


def test_plan_must_bind_the_named_measurement_value_and_unit():
    a = _record(1, "2026-10-03", "2026-10-01T09:00:00+09:00", "1.5")
    a["status"] = "planned"
    body = "本人は2026-10-03に採血 Cr 1.0 mg/dLの検査予定、移動に1.5時間。"
    a["source_body"] = a["evidence"] = body
    a["evidence_span"] = [0, len(body)]
    assert measurement_timeline.project_measurements([a], source_complete=True)["items"][0]["state"] == "unknown"
    a["value"] = "1.0"
    a["key"] = "potassium"
    assert measurement_timeline.project_measurements([a], source_complete=True)["items"][0]["state"] == "unknown"


def test_non_finite_text_is_unknown_and_jst_day_determines_future():
    for value in ("NaN", "+Infinity", "-inf"):
        a = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", value)
        assert measurement_timeline.project_measurements([a], source_complete=True)["items"][0]["state"] == "unknown"
    a = _record(2, "2026-10-02", "2026-10-02T00:10:00+14:00", "1.0")
    assert measurement_timeline.project_measurements([a], source_complete=True)["items"][0]["state"] == "unknown"
    b = _record(3, "2026-10-02", "2026-10-01T16:10:00+00:00", "1.0")
    assert measurement_timeline.project_measurements([b], source_complete=True)["items"][0]["state"] == "current"


def test_qualitative_lab_result_without_unit_is_a_source_bound_observation():
    a = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "陰性")
    body = "本人は2026-10-01の検査で合成検査Q 陰性。"
    a.update(name="合成検査Q", key="合成検査q", unit=None, condition=None, source_body=body, evidence=body,
             evidence_span=[0, len(body)])
    assert measurement_timeline.project_measurements([a], source_complete=True)["items"][0]["state"] == "observed"


def test_untrusted_source_ref_and_unknown_values_never_echo_raw_payload():
    a = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    a["source_ref"].update(kind="合成の本文", unexpected="合成の本文")
    a["value"] = {"secret": "合成の本文"}
    a["unit"] = "合成の本文" * 4
    result = measurement_timeline.project_measurements([a], source_complete=True)
    item = result["items"][0]
    assert item["state"] == "unknown" and item["value"] is None and item["unit"] is None
    assert item["source_ref"] == {"artifact_id": 1, "field": "labs", "index": 0}
    assert "合成の本文" not in str(result)


def test_unknown_lab_key_not_bound_to_the_original_quote_is_not_echoed():
    a = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    a["name"] = "合成機密文字列"
    a["key"] = "合成機密文字列"
    result = measurement_timeline.project_measurements([a], source_complete=True)
    assert result["items"][0]["state"] == "unknown"
    assert result["items"][0]["key"] is None
    assert "合成機密文字列" not in str(result)


def test_invalid_source_fields_or_indexes_are_unknown_without_echoing_payload():
    for source_ref in (
            {"artifact_id": 1, "field": "合成の本文", "index": 0},
            {"artifact_id": 1, "field": "labs", "index": "合成の本文"},
            {"artifact_id": 1, "field": "labs", "index": -1},
            {"artifact_id": 1, "field": "labs", "index": True},
            {"artifact_id": 1, "field": "vitals"}):
        a = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
        a["source_ref"] = source_ref
        result = measurement_timeline.project_measurements([a], source_complete=True)
        assert result["items"][0]["state"] == "unknown"
        assert result["items"][0]["source_ref"] is None
        assert "合成の本文" not in str(result)


def test_vital_reference_points_to_fixed_field_without_a_list_index():
    a = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", 36.5)
    body = "本人は2026-10-01安静時測定の体温36.5℃。"
    a.update(kind="vital", key="bt", unit="℃", condition="安静時", source_body=body, evidence=body,
             evidence_span=[0, len(body)], source_ref={"artifact_id": 1, "field": "vitals"})
    result = measurement_timeline.project_measurements([a], source_complete=True)
    assert result["items"][0]["state"] == "current"
    assert result["items"][0]["source_ref"] == {"artifact_id": 1, "field": "vitals"}
    a["source_ref"]["index"] = 0
    result = measurement_timeline.project_measurements([a], source_complete=True)
    assert result["items"][0]["state"] == "unknown"
    assert result["items"][0]["source_ref"] is None


def test_same_written_condition_and_unit_compare_by_measurement_day():
    old = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    new = _record(2, "2026-10-02", "2026-10-02T09:00:00+09:00", "1.5")
    result = measurement_timeline.project_measurements([new, old], source_complete=True)
    assert [(item["message_id"], item["state"], item["condition"]) for item in result["items"]] == [
        (1, "history", "空腹時"), (2, "current", "空腹時")]


def test_different_or_missing_condition_does_not_make_other_observation_history():
    fasting = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    after_meal = _record(2, "2026-10-02", "2026-10-02T09:00:00+09:00", "1.5")
    body = after_meal["source_body"].replace("空腹時", "食後")
    after_meal.update(condition="食後", source_body=body, evidence=body, evidence_span=[0, len(body)])
    result = measurement_timeline.project_measurements([fasting, after_meal], source_complete=True)
    assert [(item["state"], item["condition"]) for item in result["items"]] == [
        ("observed", "空腹時"), ("observed", "食後")]
    after_meal["condition"] = None
    result = measurement_timeline.project_measurements([fasting, after_meal], source_complete=True)
    assert [item["state"] for item in result["items"]] == ["observed", "observed"]
    assert result["items"][1]["condition"] is None


def test_different_units_are_not_compared_or_normalized_as_one_series():
    first = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    second = _record(2, "2026-10-02", "2026-10-02T09:00:00+09:00", "88")
    body = second["source_body"].replace("mg/dL", "μmol/L")
    second.update(unit="μmol/L", source_body=body, evidence=body, evidence_span=[0, len(body)])
    result = measurement_timeline.project_measurements([first, second], source_complete=True)
    assert [item["state"] for item in result["items"]] == ["observed", "observed"]
    assert [item["unit"] for item in result["items"]] == ["mg/dL", "μmol/L"]


def test_condition_must_be_bound_to_the_same_source_assertion():
    record = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    body = "本人は食後に休息。本人の2026-10-01空腹時採血 Cr 1.0 mg/dL。"
    record.update(condition="食後", source_body=body, evidence=body, evidence_span=[0, len(body)])
    item = measurement_timeline.project_measurements([record], source_complete=True)["items"][0]
    assert item["state"] == "unknown"
    assert item["condition"] is None and item["subject"] == "unknown"


def test_condition_of_another_action_is_not_assigned_to_the_measurement():
    for conjunction in ("し", "し、"):
        record = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
        body = f"本人は空腹時に薬を服用{conjunction}2026-10-01に食後採血 Cr 1.0 mg/dL。"
        record.update(source_body=body, evidence=body, evidence_span=[0, len(body)])
        item = measurement_timeline.project_measurements([record], source_complete=True)["items"][0]
        assert item["state"] == "unknown" and item["condition"] is None
    # A separate, explicitly patient-owned sampling assertion is unambiguous.
    body = "本人は空腹時に薬を服用した。本人は2026-10-01に食後採血 Cr 1.0 mg/dL。"
    record.update(condition="食後", source_body=body, evidence=body, evidence_span=[0, len(body)])
    item = measurement_timeline.project_measurements([record], source_complete=True)["items"][0]
    assert item["state"] == "current" and item["condition"] == "食後"


def test_explicit_past_does_not_become_current_even_when_only_measurement():
    record = _record(1, "2026-10-01", "2026-10-02T09:00:00+09:00", "1.0")
    body = "本人の過去の2026-10-01空腹時採血 Cr 1.0 mg/dL。"
    record.update(status="past", source_body=body, evidence=body, evidence_span=[0, len(body)])
    item = measurement_timeline.project_measurements([record], source_complete=True)["items"][0]
    assert item["state"] == "history" and item["value"] == "1.0"
    assert item["condition"] == "空腹時"


def test_more_recent_past_measurement_does_not_make_older_report_current():
    old = _record(1, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    past = _record(2, "2026-10-02", "2026-10-03T09:00:00+09:00", "1.5")
    body = "本人の過去の2026-10-02空腹時採血 Cr 1.5 mg/dL。"
    past.update(status="past", source_body=body, evidence=body, evidence_span=[0, len(body)])
    items = measurement_timeline.project_measurements([old, past], source_complete=True)["items"]
    assert [(item["message_id"], item["state"]) for item in items] == [(1, "history"), (2, "history")]


def test_subject_and_day_are_not_echoed_when_source_disagrees():
    record = _record(1, "2026-10-01", "2026-10-02T09:00:00+09:00", "1.0")
    family_body = record["source_body"].replace("本人の", "家族の")
    record.update(source_body=family_body, evidence=family_body, evidence_span=[0, len(family_body)])
    item = measurement_timeline.project_measurements([record], source_complete=True)["items"][0]
    assert item["state"] == "unknown" and item["subject"] == "unknown"
    assert item["measured_on"] is None and item["condition"] is None
    record = _record(2, "2026-10-01", "2026-10-02T09:00:00+09:00", "1.0")
    body = record["source_body"].replace("2026-10-01", "2026-09-01")
    record.update(source_body=body, evidence=body, evidence_span=[0, len(body)])
    item = measurement_timeline.project_measurements([record], source_complete=True)["items"][0]
    assert item["state"] == "unknown" and item["measured_on"] is None


def test_direct_projection_holds_patient_when_original_mentions_family_topic():
    record = _record(1, "2026-10-01", "2026-10-02T09:00:00+09:00", "1.0")
    quote = record["evidence"]
    prefix = "家族の話です。"
    record["source_body"] = prefix + quote
    record["evidence_span"] = [len(prefix), len(prefix) + len(quote)]
    item = measurement_timeline.project_measurements([record], source_complete=True)["items"][0]
    assert item["state"] == "unknown" and item["subject"] == "unknown"
    assert item["value"] is None


def test_future_or_missing_measurement_date_is_not_returned_as_a_measured_date():
    future = _record(1, "2026-10-03", "2026-10-01T09:00:00+09:00", "1.0")
    item = measurement_timeline.project_measurements([future], source_complete=True)["items"][0]
    assert item["state"] == "unknown" and item["measured_on"] is None
    missing = _record(2, "2026-10-01", "2026-10-01T09:00:00+09:00", "1.0")
    missing["measured_on"] = None
    item = measurement_timeline.project_measurements([missing], source_complete=True)["items"][0]
    assert item["state"] == "unknown" and item["measured_on"] is None
