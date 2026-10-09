"""Source-scoped alerts retain patient emergencies without inheriting another person's report."""
import json

import pytest

import extract
import structured_view
from test_read_model import _artifact, _db, _hash_of


@pytest.mark.parametrize("body,quote,scope", [
    ("母が意識を失っています。至急対応してください。", "意識を失っています", "family"),
    ("職員本人が意識を失っています。", "意識を失っています", "other"),
    ("他患者が急変しました。", "急変しました", "other"),
    ("家族本人のSpO2 80です。", "SpO2 80", "family"),
    ("母ご本人が救急搬送を必要としています。", "救急搬送", "family"),
    ("架空職員Aさんご本人が意識を失っています。", "意識を失っています", "unknown"),
    ("家族から本人のSpO2 80を報告しました。", "SpO2 80", "patient"),
    ("娘より本人が急変したとの連絡。", "急変した", "patient"),
    ("家族から：\n本人のSpO2 80です。", "SpO2 80", "patient"),
    ("家族より：\nSpO2 80です。", "SpO2 80", "patient"),
    ("母のCr 1.2mg/dLです。", "Cr 1.2", "family"),
    ("母のK 3.2mEq/Lです。", "K 3.2", "family"),
    ("家族 Cr 1.2mg/dLです。", "Cr 1.2", "family"),
    ("家族\nSpO2 80\n本人\nSpO2 95", "SpO2 80", "family"),
    ("家族\nSpO2 80\n本人\nSpO2 95", "SpO2 95", "patient"),
    ("過去\n本人のSpO2 80", "SpO2 80", "past"),
    ("本人が発熱した場合は至急連絡。", "至急連絡", "conditional"),
    ("10時に本人のSpO2 80を測定。至急対応。", "SpO2 80", "patient"),
    ("採血時に本人のK 6.5を測定。", "K 6.5", "patient"),
    ("本人の息苦しさは楽にならない。", "息苦しさ", "patient"),
])
def test_span_scope_inherits_person_and_qualifiers_without_reporter_mask(body, quote, scope):
    start = body.index(quote)
    assert extract.patient_source_scope(body, start, start + len(quote)) == scope


def test_known_patient_name_resolves_named_compound_but_unknown_name_does_not():
    body = "架空花子さんご本人が意識を失っています。"
    start, end = body.index("意識"), len(body)
    assert extract.patient_source_scope(body, start, end) == "unknown"
    assert extract.patient_source_scope(body, start, end, patient_name="架空花子") == "patient"
    assert extract.patient_source_scope("職員本人が意識低下", 5, 9, patient_name="職員") == "other"


def test_rule_vitals_do_not_promote_family_values_and_keep_original_mentions():
    body = "母の血圧180/100、本人の血圧120/80。家族のSpO2 80、本人のSpO2 97。"
    document = extract.extract_message(body, "2026-10-07")
    assert document["vitals"] == {"sbp": 120, "dbp": 80, "spo2": 97}
    assert any(mention["scope"] == "family" and mention["values"].get("sbp") == 180
               for mention in document["vital_mentions"])
    assert extract.patient_vitals({"sbp": 180, "dbp": 100, "spo2": 80}, body) == {}
    assert extract.patient_vitals(document["vitals"], body) == document["vitals"]


def test_family_events_remain_mentions_without_becoming_patient_events():
    document = extract.extract_message("母が退院。母の処方を変更。本人は診察しました。", "2026-10-07")
    assert document["events"] == ["exam"]
    assert {item["event"] for item in document["event_mentions"]} == {"admission", "medication"}
    assert extract.extract_message("本人の過去の入院を確認。", "2026-10-07")["events"] == ["admission"]


@pytest.mark.parametrize("body,source", [
    ("本人が急変。至急連絡してください。", "rule"),
    ("母が急変。本人の記録を至急確認してください。", None),
    ("本人は急変していません。至急記録を確認してください。", None),
    ("本人の過去の急変。現在の記録を至急確認してください。", None),
    ("本人が急変した場合は連絡。至急記録を確認してください。", None),
    ("本人の体温36.5度です。至急書類を確認してください。", None),
])
def test_request_and_patient_sign_across_sentences_keep_independent_source_scope(tmp_path, body, source):
    db = _db(tmp_path, ())
    try:
        from semantic_testkit import _message
        db.save_messages([_message(1, body=body)], project_id=1)
        _artifact(db, "extract_v1", 1, {"urgency": "high"}, {"hash": _hash_of(db, 1)})
        details = structured_view.message_urgency_details(db.db, 1)
        assert details["source"] == source
        if source:
            assert details["reasons"] == ["本人が急変"]
    finally:
        db.close()


@pytest.mark.parametrize("body,expected_kind,source", [
    ("本人の記録を至急確認してください。", "request", None),
    ("本人が急変、至急確認してください。", "clinical", "llm"),
    ("本人は急変していません。", "clinical", None),
    ("本人の発熱はありません。", "clinical", None),
    ("本人の意識がない。", "clinical", "llm"),
    ("本人の呼吸がない。", "clinical", "llm"),
])
def test_clinical_emergency_is_distinct_from_request_or_denied_sign(tmp_path, body, expected_kind, source):
    db = _db(tmp_path, ())
    try:
        from semantic_testkit import _message
        db.save_messages([_message(1, body=body)], project_id=1)
        _artifact(db, "extract_llm", 1, {"urgency": "high", "urgency_evidence": [body]},
                  {"hash": _hash_of(db, 1)})
        details = structured_view.message_urgency_details(db.db, 1)
        assert details["source"] == source
        assert details["kind"] == expected_kind
        if source is None:
            assert details["verdict"] == "unclear" and details["held"]
    finally:
        db.close()


@pytest.mark.parametrize("body,high", [
    ("母が急変、至急対応してください。", False),
    ("職員本人が意識低下、至急対応してください。", False),
    ("他患者が急変。至急対応してください。", False),
    ("本人が急変、至急対応してください。", True),
    ("母が急変。本人が呼吸困難、至急対応してください。", True),
    ("家族から本人の急変について連絡、至急対応してください。", True),
    ("母の過去の救急搬送を報告。本人は安定。", False),
    ("本人の状態が悪化した場合は救急搬送。", False),
])
def test_rule_alerts_use_the_same_scoped_predicate(body, high):
    assert (extract.extract_message(body, "2026-10-07").get("urgency") == "high") is high


@pytest.mark.parametrize("kind", ["extract_llm", "canonical_projection", "semantic_facts_v4"])
def test_cached_high_for_family_never_becomes_patient_high_across_read_surfaces(tmp_path, kind):
    db = _db(tmp_path, ())
    try:
        from semantic_testkit import _message
        body = "母が意識を失っています。本人は落ち着いています。"
        db.save_messages([_message(1, body=body)], project_id=1)
        _artifact(db, kind, 1, {"urgency": "high", "urgency_evidence": ["意識を失っています"]},
                  {"hash": _hash_of(db, 1), "engine_version": 4})
        detail = structured_view.message_urgency_details(db.db, 1)
        assert detail["held"] and detail["verdict"] == "unclear"
        assert structured_view.message_urgency(db.db, 1) is None
        for plain in (False, True):
            lines = structured_view.structured_lines(db.db, 1, plain=plain)
            # no high-urgency head on either the legacy or the plain surface
            assert not any(line.startswith("🚨") for line in lines)
            assert any(line.startswith("緊急度: 要確認（対象人物・時点の根拠を確認")
                       for line in lines)
        assert "母" in db.db.execute("SELECT body_text FROM messages WHERE message_id=1").fetchone()[0]
    finally:
        db.close()


def test_mixed_high_quotes_keep_only_current_patient_reason(tmp_path):
    db = _db(tmp_path, ())
    try:
        from semantic_testkit import _message
        body = "母が急変。本人が意識を失っています。"
        db.save_messages([_message(1, body=body)], project_id=1)
        _artifact(db, "extract_llm", 1, {"urgency": "high", "urgency_evidence": ["母が急変", "意識を失っています"]},
                  {"hash": _hash_of(db, 1)})
        details = structured_view.message_urgency_details(db.db, 1)
        assert details["source"] == "llm" and details["subject"] == "patient"
        assert details["reasons"] == ["意識を失っています"]
        assert "母" not in json.dumps(details["reasons"], ensure_ascii=False)
    finally:
        db.close()
