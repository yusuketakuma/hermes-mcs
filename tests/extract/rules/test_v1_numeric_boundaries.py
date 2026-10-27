"""Synthetic v1 vitals must not fabricate a prefix of a longer number."""
import clinical_values
import pytest

import extract
from inference_version import CURRENT_VERSION, VERSION_KEY


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
    assert clinical_values.patient_vitals(old_values, body) == expected


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
    assert clinical_values.patient_vitals(values, body) == values


def test_old_rollup_prefix_is_rechecked_readonly_without_erasing_profile(tmp_path, monkeypatch):
    import json
    import extract_llm
    import inference_dependency_loss
    import rollup
    from extract_testkit import _ledger, _message

    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm, "_rerender_cards", lambda *args, **kwargs: None)
    store = _ledger(tmp_path)
    try:
        store.ensure_patient(1)
        with store.db:
            store.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,"
                             "history_floor=-1,karte_id=100 WHERE project_id=1")
        store.karte_summary_store(1, 100, {"comment": "生活環境：合成プロフィール",
                                         "updated_at": "2032-06-01T09:00:00+09:00"})
        store.save_messages([_message(body="本人の体温36.55℃")])
        source = dict(store.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        source["_source_binding"] = inference_dependency_loss.source_binding(store.db, 1, 1)
        source["_target"] = ("", extract_llm.resolved_model({}))
        integrity = {"validation": {"items_dropped": 0, "evidence_dropped": 0}, "completeness_chunks": 1,
                     "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
        # Simulate a source-bound bad historic prefix even if a writer once
        # marked the extraction complete: the reader must check the source.
        assert extract_llm._replace_current(store, source, json.dumps({"vitals": {"bt": 36.5}}),
                                            integrity=integrity)
        cached = {VERSION_KEY: CURRENT_VERSION, "project_id": 1,
                  "latest_vitals": {"at": "old", "bt": 36.5},
                  "patient_context": {"memo": {"living": ["架空のキャッシュ値"]}}}
        meta = {VERSION_KEY: CURRENT_VERSION, "period_check_version": 7,
                "projection_version": rollup.PROJECTION_VERSION}
        before = [tuple(row) for row in store.db.execute("SELECT * FROM artifacts")]
        store.db.execute("PRAGMA query_only=ON")
        store.db.execute("BEGIN")
        try:
            result = rollup.current_cached_refs(store.db, 1, cached, meta, cfg={})
            assert "latest_vitals" not in result
            assert result["measurement_coverage"]["complete"] is True
            assert result["patient_context"]["memo"]["living"][0]["text"] == "合成プロフィール"
            assert "架空のキャッシュ値" not in json.dumps(result, ensure_ascii=False)
            assert [tuple(row) for row in store.db.execute("SELECT * FROM artifacts")] == before
            assert store.db.in_transaction
            unknown = rollup.current_cached_refs(store.db, 1, cached, meta)
            assert unknown["rollup_freshness"] == "unknown"
            assert not unknown["measurement_coverage"]["complete"]
            assert store.db.in_transaction
        finally:
            store.db.rollback()
    finally:
        store.close()


# Regression tests for v1 rule extraction: full-width decimals,
# negated urgent phrases, and explicit-year next-visit dates (all synthetic).
POSTED = '2026-10-04T10:00:00+09:00'


@pytest.mark.parametrize('body,bt', [
    ('体温３８．２℃', 38.2), ('体温３９．５度', 39.5),
    ('体温38.2℃', 38.2), ('体温３８℃', 38)])
def test_full_width_temperature_keeps_decimal(body, bt):
    assert extract.extract_message(body, POSTED)['vitals']['bt'] == bt


@pytest.mark.parametrize('body,high', [
    ('搬送していません', False), ('救急搬送はしていません', False),
    ('搬送していない', False), ('緊急搬送はしておりません', False),
    ('救急搬送されていません', False), ('搬送されませんでした', False),
    ('至急ではなかった', False),
    ('救急搬送しています', True), ('至急搬送してください', True),
    ('急ぎではありませんが至急ご確認ください', True)])
def test_negated_urgent_forms(body, high):
    assert (extract.extract_message(body, POSTED).get('urgency') == 'high') is high


@pytest.mark.parametrize('body,want', [
    ('次回2026/10/20訪問', '2026-10-20'), ('次回2026/1/5', '2026-01-05'),
    ('次回 2010/12/3', '2010-12-03'), ('次回10/20訪問', '2026-10-20'),
    ('次回2026年10月20日訪問', '2026-10-20')])
def test_next_planned_explicit_year(body, want):
    assert extract.extract_message(body, POSTED).get('next_planned') == want
