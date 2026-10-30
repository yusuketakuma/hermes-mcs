"""Invented prose and source-distinct record preservation, without model or network calls."""
import copy
import json
import sqlite3

import pytest

import extract_llm as llm
import extract_quality
import extract_validation as validation
import inference_version as inference
import inference_dependency_loss as dependency_loss
from extract_testkit import _ledger, _message


BODY = "本人の血圧120/80です。"
VITALS = {"sbp": 120.0, "dbp": 80.0}


@pytest.mark.parametrize("field,limit", [("summary", validation.SUMMARY_MAX_CHARS),
                                         ("points", validation.POINT_MAX_CHARS)])
@pytest.mark.parametrize("tail", ["発熱なし", "連絡するのは発熱した場合のみ", "本人ではなく母の症状です"])
def test_overlength_prose_never_turns_a_qualifier_into_an_affirmative_prefix(field, limit, tail):
    text = "合成" * (limit // 2) + tail
    raw = [text] if field == "points" else text
    drops = {}
    out = validation.validate({field: raw, "vitals": VITALS}, BODY, drops)
    assert out["vitals"] == VITALS
    assert out.get(field) in (None, [])
    assert out["_items_dropped"] == 1
    assert field in drops["items"]
    assert any(field in reason for reason in extract_quality.repair_issues(drops, out))
    assert validation._clean_text(text, limit) is None


@pytest.mark.parametrize("field,limit", [("summary", validation.SUMMARY_MAX_CHARS),
                                         ("points", validation.POINT_MAX_CHARS)])
def test_prose_at_the_limit_keeps_the_complete_negation_and_subject(field, limit):
    suffix = "母は発熱なし。本人は発熱時のみ連絡。"
    text = "合" * (limit - len(suffix)) + suffix
    raw = [text] if field == "points" else text
    out = validation.validate({field: raw}, text)
    assert out[field] == raw
    assert not out.get("_items_dropped")


def test_summary_and_point_prompt_limits_come_from_validator_contract():
    assert f'要点を{validation.SUMMARY_MAX_CHARS}字以内' in llm._PROMPT_SPEC
    assert f'各{validation.POINT_MAX_CHARS}字以内' in llm._PROMPT_SPEC
    assert "__SUMMARY_MAX_CHARS__" not in llm._PROMPT_HEAD
    assert "__POINT_MAX_CHARS__" not in llm._PROMPT_HEAD


def test_points_keep_valid_siblings_and_count_every_invalid_value():
    points = ["本人は発熱なし", "母の症状は未確認", "条件成立時だけ連絡", "薬剤師の回答は未取得"]
    drops = {}
    out = validation.validate({"points": [*points, None, "合" * 41], "vitals": VITALS},
                              "。".join(points) + "。" + BODY, drops)
    assert out["points"] == points
    assert out["vitals"] == VITALS and out["_items_dropped"] == 2
    assert "points" in drops["items"]


@pytest.mark.parametrize("field,base,change", [
    ("meds", {"name": "架空薬A", "action": "start", "subject": "patient", "status": "current",
              "negated": False, "dose": "1錠", "freq": "朝", "evidence": "前回は架空薬Aを朝1錠開始。"},
     {"dose": "2錠", "evidence": "今回は架空薬Aを朝2錠開始。"}),
    ("meds", {"name": "架空薬A", "action": "start", "subject": "patient", "status": "past",
              "dose": "1錠", "evidence": "以前に架空薬Aを開始。"},
     {"status": "planned", "evidence": "次回は架空薬Aを開始予定。"}),
    ("meds", {"name": "架空薬A", "action": "none", "subject": "patient", "negated": False,
              "evidence": "本人は架空薬Aを服用中。"},
     {"negated": True, "evidence": "本人は架空薬Aを服用していない。"}),
    ("symptoms", {"text": "発熱", "subject": "patient", "status": "past", "negated": False,
                  "evidence": "先週は本人に発熱。"},
     {"status": "resolved", "evidence": "本日は本人の発熱が消失。"}),
    ("symptoms", {"text": "咳", "subject": "patient", "status": "ongoing", "negated": False,
                  "severity": "mild", "evidence": "朝は本人に軽い咳。"},
     {"severity": "severe", "evidence": "夜は本人に強い咳。"}),
    ("symptoms", {"text": "咳", "subject": "patient", "status": "ongoing", "negated": False,
                  "evidence": "本人に咳。"},
     {"subject": "family", "evidence": "母に咳。"}),
])
def test_merge_keeps_same_name_with_distinct_time_evidence_or_attributes(field, base, change):
    later = {**base, **change}
    inputs = [{field: [base]}, {field: [later, dict(reversed(list(base.items())))]}]
    before = copy.deepcopy(inputs)
    assert validation.merge(inputs)[field] == [base, later]
    assert inputs == before


@pytest.mark.parametrize("field,identifier", [("meds", {"name": "架空薬A"}), ("symptoms", {"text": "咳"})])
def test_merge_does_not_erase_explicit_unknown_or_changed_verification(field, identifier):
    candidate = {**identifier, "unverified": True}
    explicit_unknown = {**identifier, "status": None, "unverified": True}
    checked = {**identifier, "evidence": "完全合成の引用"}
    assert validation.merge([{field: [candidate, explicit_unknown]}, {field: [checked, candidate]}])[field] == [
        candidate, explicit_unknown, checked]


def test_merge_keeps_all_distinct_points_beyond_the_per_prompt_display_selection():
    first = ["本人は発熱なし", "母の症状は未確認", "発熱した場合のみ連絡"]
    later = ["薬剤師の回答は未取得", "次回に訪問予定", first[0]]
    assert validation.merge([{"points": first}, {"points": later}])["points"] == first + later[:2]


@pytest.fixture
def writer(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    ledger.save_messages([_message(body=BODY)])
    monkeypatch.setattr(llm, "load_config", lambda: {})
    monkeypatch.setattr(llm, "_resolved_llm", lambda: ("synthetic", "synthetic"))
    row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = dependency_loss.source_binding(ledger.db, row["project_id"], row["message_id"])
    try:
        yield ledger, row
    finally:
        ledger.close()


def test_failed_scalar_repair_keeps_facts_but_never_reports_complete(writer, monkeypatch):
    ledger, row = writer
    calls = []
    def response(*args, **kwargs):
        calls.append(kwargs.get("kind", "first"))
        return {"symptoms": [], "vitals": VITALS, "summary": "合成" * 31 + "発熱なし"} if len(calls) == 1 else None
    monkeypatch.setattr(llm, "_llm_call", response)
    meta = {}
    out = llm.llm_extract(BODY, meta_out=meta)
    assert calls == ["first", "repair"] and out["vitals"] == VITALS
    assert out["_items_dropped"] == 1 and "summary" not in out
    assert meta["completeness"]["0"]["state"] != "hint_gaps_resolved"
    assert llm._replace_current(ledger, row, json.dumps(out), integrity=meta)
    assert llm.progress_for_message(ledger.db, row, model=llm.MODEL)["state"] == "attention"


def test_repaired_complete_prose_can_finish_without_losing_facts(writer, monkeypatch):
    ledger, row = writer
    calls = []
    def response(*args, **kwargs):
        calls.append(1)
        return {"symptoms": [], "vitals": VITALS, "summary": "合成" * 31 + "発熱なし"} if len(calls) == 1 else {
            "symptoms": [], "vitals": VITALS, "summary": "本人の血圧120/80を記録"}
    monkeypatch.setattr(llm, "_llm_call", response)
    meta = {}
    out = llm.llm_extract(BODY, meta_out=meta)
    assert len(calls) == 2 and out["vitals"] == VITALS
    assert out["summary"] == "本人の血圧120/80を記録"
    assert not out.get("_items_dropped")
    assert meta["completeness"]["0"]["state"] == "hint_gaps_resolved"
    assert llm._replace_current(ledger, row, json.dumps(out), integrity=meta)
    assert llm.progress_for_message(ledger.db, row, model=llm.MODEL)["state"] == "complete"


@pytest.mark.parametrize("record", [None, {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}])
def test_cache_cannot_erase_a_previous_scalar_drop_by_revalidating_filtered_fields(writer, monkeypatch, record):
    monkeypatch.setattr(llm, "_llm_call", lambda *a, **k: pytest.fail("unexpected cached dispatch"))
    cached = {"vitals": VITALS, "_items_dropped": 1}
    if record is not None:
        cached["_quality_result"] = record
    meta, chunks = {}, {}
    out = llm.llm_extract(BODY, chunks_in={0: cached}, chunks_out=chunks, meta_out=meta)
    assert out["vitals"] == VITALS and out["_items_dropped"] == 1
    assert meta["completeness"]["0"]["state"] == "unresolved"
    assert chunks[0]["_quality_result"]["state"] == "unresolved"
    assert not extract_quality.completeness_resolved({inference.VERSION_KEY: inference.CURRENT_VERSION,
                                                     "integrity": meta})


@pytest.mark.parametrize("reply", [False, True])
def test_replay_only_refresh_never_adopts_completed_history_deliveries(writer, monkeypatch, reply):
    ledger, row = writer
    ledger.job_add("semantic", 1, 1, payload={inference.VERSION_KEY: inference.CURRENT_VERSION,
        "reprocess_only": True, "notification_free": True})
    with ledger.db:
        ledger.db.execute("UPDATE fetch_jobs SET state='done' WHERE kind='semantic'")
    if reply:
        ledger.save_messages([_message(mid=2, body=BODY, parent_id=1)])
    before = [tuple(r) for r in ledger.db.execute("SELECT * FROM fetch_jobs")]
    calls = []
    monkeypatch.setattr(llm.extraction_refresh, "refresh_extraction_cards", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(llm, "load_config", lambda: pytest.fail("replay refresh read config"))
    llm._rerender_cards(ledger, 1, 2 if reply else row["message_id"])
    assert calls == [] and before == [tuple(r) for r in ledger.db.execute("SELECT * FROM fetch_jobs")]


@pytest.mark.parametrize("payload", [{}, {"reprocess_only": True}, {"notification_free": True},
                                      {"reprocess_only": False, "notification_free": False}])
def test_pending_live_refresh_preserves_original_notification_eligibility(writer, monkeypatch, payload):
    ledger, _ = writer
    ledger.job_add("semantic", 1, 1, payload=payload)
    calls = []
    monkeypatch.setattr(llm.extraction_refresh, "refresh_extraction_cards",
                        lambda led, pid, mid, **k: calls.append((pid, mid)))
    llm._rerender_cards(ledger, 1, 1)
    assert calls == [(1, 1)]


def test_card_gate_failure_cannot_turn_committed_extraction_into_failure(writer, monkeypatch):
    ledger, row = writer
    def locked(*args):
        raise sqlite3.OperationalError("synthetic locked")
    monkeypatch.setattr(llm.semantic_store, "refresh_cards_allowed", locked)
    monkeypatch.setattr(llm.extraction_refresh, "refresh_extraction_cards", lambda *a, **k: pytest.fail("gate failed"))
    assert llm._replace_current(ledger, row, '{"vitals":{"sbp":120,"dbp":80}}')
    assert llm._current(ledger, 1, row["content_hash"])
