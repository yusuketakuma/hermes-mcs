"""Synthetic v1 vitals must not fabricate a prefix of a longer number."""
import pytest

import extract


CASES = [
    ("本人の体温36.55℃", {"bt": 36.5}),
    ("本人の体温３６．５５℃", {"bt": 36.5}),
    ("本人の脈拍1200回", {"hr": 120}),
    ("本人の脈拍１２．５回", {"hr": 12}),
    ("本人の呼吸数200回", {"rr": 20}),
    ("本人の呼吸数２０．５回", {"rr": 20}),
    ("本人の血圧120/8000", {"sbp": 120, "dbp": 800}),
    ("本人の血圧１２０／８０．５", {"sbp": 120, "dbp": 80}),
    ("本人の血糖1200mg/dL", {"bs": 120}),
    ("本人の血糖１２０．５mg/dL", {"bs": 120}),
    ("本人のSpO2９８．５％", {"spo2": 98}),
]


@pytest.mark.parametrize("body,old_values", CASES)
def test_rule_extraction_does_not_take_a_numeric_prefix(body, old_values):
    assert not extract.extract_message(body, "2032-06-01T10:00:00+09:00").get("vitals")


@pytest.mark.parametrize("body,old_values", CASES)
def test_cached_prefix_is_not_grounded_by_a_partial_regex_match(body, old_values):
    # The complete systolic reading remains valid; the invented diastolic
    # prefix must disappear, without manufacturing a complete pair.
    expected = {"sbp": 120} if "sbp" in old_values else {}
    assert extract.patient_vitals(old_values, body) == expected


@pytest.mark.parametrize("body,values", [
    ("本人の体温３６．５℃", {"bt": 36.5}),
    ("本人の脈拍120回", {"hr": 120}),
    ("本人の呼吸数20回", {"rr": 20}),
    ("本人の血圧１２０／８０", {"sbp": 120, "dbp": 80}),
    ("本人の血糖120mg/dL", {"bs": 120}),
    ("本人のSpO2９８％", {"spo2": 98}),
])
def test_supported_complete_readings_remain_available(body, values):
    assert extract.extract_message(body, "2032-06-01T10:00:00+09:00")["vitals"] == values
    assert extract.patient_vitals(values, body) == values


def test_old_rollup_prefix_is_rechecked_readonly_without_erasing_profile(tmp_path):
    import json
    import rollup
    from extract_testkit import _ledger, _message, _hash

    store = _ledger(tmp_path)
    try:
        store.ensure_patient(1)
        store.save_messages([_message(body="本人の体温36.55℃")])
        store.artifact_add("extract_llm", json.dumps({"vitals": {"bt": 36.5}}),
                           project_id=1, message_id=1, meta={"hash": _hash(store)})
        cached = {"latest_vitals": {"at": "old", "bt": 36.5},
                  "patient_context": {"memo": {"living": ["合成プロフィール"]}}}
        before = [tuple(row) for row in store.db.execute("SELECT * FROM artifacts")]
        store.db.execute("PRAGMA query_only=ON")
        result = rollup.current_cached_refs(store.db, 1, cached,
            {"period_check_version": 7, "projection_version": rollup.PROJECTION_VERSION})
        assert "latest_vitals" not in result
        assert result["patient_context"] == cached["patient_context"]
        assert [tuple(row) for row in store.db.execute("SELECT * FROM artifacts")] == before
    finally:
        store.close()
