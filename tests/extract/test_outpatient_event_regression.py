"""Fully synthetic source-bound outpatient event regressions."""
import copy
import json

import pytest

import extract
import extract_validation as validation
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY


@pytest.mark.parametrize("body", [
    "本人が本日内科の外来に行ってきました。",
    "ご本人は今日外来へ行きました。",
    "患者本人が本日循環器内科の外来に行きました。",
])
def test_explicit_patient_outpatient_arrival_is_only_a_candidate_in_both_paths(body):
    rule = extract.extract_message(body, "2026-10-26T10:00:00+09:00")
    llm = validation.validate({"events": ["exam"]}, body)
    for result in (rule, llm):
        assert result.get("events", []) == []
        record, = result["event_records"]
        assert (record["subject"], record["status"], record["negated"]) == ("patient", "current", None)
        assert record["unverified"] is True
        assert record["evidence"] == body
        assert body[record["span"]["start"]:record["span"]["end"]] == body
    assert validation.validate(copy.deepcopy(llm), body) == llm


@pytest.mark.parametrize("when", ["昨日", "先日"])
def test_explicit_past_outpatient_report_is_not_projected_as_current(when):
    body = f"本人が{when}内科の外来に行ってきました。"
    for result in (extract.extract_message(body, "2026-10-26T10:00:00+09:00"),
                   validation.validate({"events": ["exam"]}, body)):
        assert "exam" not in result.get("events", [])
        record, = result["event_records"]
        assert (record["subject"], record["status"], record["negated"]) == ("patient", "past", None)
        assert record["evidence"] == body and record["unverified"] is True


def test_short_quote_is_bound_to_its_explicit_patient_source():
    body = "本人が本日内科の外来に行ってきました。"
    quote = "外来に行ってきました"
    result = validation.validate({"event_records": [{"kind": "exam", "evidence": quote}]}, body)
    record, = result["event_records"]
    assert result["events"] == []
    assert record["evidence"] == quote and record["unverified"] is True
    assert body[record["span"]["start"]:record["span"]["end"]] == quote


@pytest.mark.parametrize("body", [
    "本日内科の外来に行ってきました。",
    "母が本日内科の外来に行ってきました。",
    "看護師が本日内科の外来に行ってきました。",
    "本人が明日内科の外来に行ってきました。",
    "本人が本日内科の外来に行く予定です。",
    "本人が本日内科の外来に行ってきませんでした。",
    "本人が本日内科の外来に行ってきましたか。",
    "もし必要なら本人が本日内科の外来に行ってきました。",
    "『本人が本日内科の外来に行ってきました。』と聞きました。",
    "次の記載は誤りです。本人が本日内科の外来に行ってきました。",
    "本人が本日内科の外来に行ってきました。その記載は取り消しました。",
    "本人が本日内科の外来に行ってきました。別の予定があります。",
    "本人が本日内科の外来について説明を聞きました。",
    "看護師が本日本人宅に来てくださいました。",
])
def test_ambiguous_outpatient_or_visit_is_never_confirmed_as_patient_event(body):
    for kind in ("exam", "visit"):
        result = validation.validate({"events": [kind]}, body)
        assert kind not in result["events"]
        assert all(record.get("unverified") or record["subject"] != "patient"
                   or record["status"] != "current" or record["negated"] is not False
                   for record in result["event_records"])
    rule = extract.extract_message(body, "2026-10-26T10:00:00+09:00")
    assert not ({"exam", "visit"} & set(rule.get("events", [])))


def test_partial_chunk_cannot_erase_full_body_context_or_invent_offset():
    piece = "本人が本日内科の外来に行ってきました。"
    body = "家族の話です。" + piece
    result = validation.validate_piece({"events": ["exam"]}, piece, body,
                                       source_offset=len("家族の話です。"))
    assert result["events"] == []
    record, = result["event_records"]
    assert record["unverified"] and record["evidence"] == piece
    assert body[record["span"]["start"]:record["span"]["end"]] == piece
    wrong = validation.validate_piece({"events": ["exam"]}, piece, body, source_offset=0)
    assert wrong["events"] == [] and wrong["event_records"][0]["unverified"]


def test_prior_rule_version_is_reprocessed_once_without_inventing_other_events(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    try:
        monkeypatch.setattr(extract, "_WRITER_STATE", (False, 1))
        monkeypatch.setattr(extract.writer_lock, "updater_state", lambda: (False, 1))
        body = "本人が本日内科の外来に行ってきました。"
        unrelated = "本人は本日内科の外来予約について説明を受けました。"
        db.save_messages([_message(body=body), _message(mid=2, body=unrelated)])
        old_id = db.artifact_add("extract_v1", '{"v":1}', project_id=1, message_id=1,
                                 meta={"hash": _hash(db), VERSION_KEY: CURRENT_VERSION,
                                       "rule_version": extract.RULE_VERSION - 1})
        db.artifact_add("extract_v1", '{"v":1}', project_id=1, message_id=2,
                        meta={"hash": _hash(db, 2), VERSION_KEY: CURRENT_VERSION,
                              "rule_version": extract.RULE_VERSION - 1})
        assert extract.run_pending(db) == {"done": 2, "pids": [1]}
        rows = db.artifacts("extract_v1", message_id=1)
        assert len(rows) == 1 and rows[0]["artifact_id"] != old_id
        document = json.loads(rows[0]["content"])
        assert document.get("events", []) == []
        assert document["event_records"][0]["evidence"] == body
        assert document["event_records"][0]["unverified"] is True
        assert json.loads(rows[0]["meta"])["rule_version"] == extract.RULE_VERSION
        other, = db.artifacts("extract_v1", message_id=2)
        assert "exam" not in json.loads(other["content"]).get("events", [])
        assert extract.run_pending(db) == {"done": 0, "pids": []}
    finally:
        db.close()


@pytest.mark.parametrize("quote", [
    "本人は本日外来に行きました。",
    "本人は本日外来に行きましたし、買い物をしました。",
])
def test_canonical_outpatient_arrival_is_not_a_confirmed_examination(quote):
    from semantic_projection import project_v2_doc_legacy
    from semantic_testkit import v2_doc, v2_fact

    evidence = {"evidence_id": "e1", "message_id": "m1", "revision": "r1",
                "start": 0, "end": len(quote), "quote": quote, "atom_id": "a1"}
    fact = v2_fact("f1", kind="care_event", subject="patient:1", workflow_status="done",
                   statement=quote, polarity="affirmed", valid_time="current", evidence_ids=["e1"])
    result = project_v2_doc_legacy(v2_doc([fact], [evidence]))
    assert result.get("events", []) == []
    record, = result["event_records"]
    assert record["kind"] == "exam" and record["evidence"] == quote
    assert record["span"] == {"start": 0, "end": len(quote)} and record["unverified"] is True


def test_canonical_outpatient_statement_cannot_borrow_an_unrelated_exam_cue():
    from semantic_projection import project_v2_doc_legacy
    from semantic_testkit import v2_doc, v2_fact

    quote = "本人は本日診察をしましたと誤って記載されましたが、外来に行きました。"
    evidence = {"evidence_id": "e1", "message_id": "m1", "revision": "r1",
                "start": 0, "end": len(quote), "quote": quote, "atom_id": "a1"}
    fact = v2_fact("f1", kind="care_event", subject="patient:1", workflow_status="done",
                   statement="本人は本日外来に行きました。", polarity="affirmed",
                   valid_time="current", evidence_ids=["e1"])
    result = project_v2_doc_legacy(v2_doc([fact], [evidence]))
    assert result.get("events", []) == []
    record, = result["event_records"]
    assert record["evidence"] == quote and record["unverified"] is True


def test_explicit_examination_is_still_a_confirmed_patient_event():
    body = "本人が本日内科で診察を受けました。"
    for result in (extract.extract_message(body, "2026-10-26T10:00:00+09:00"),
                   validation.validate({"events": ["exam"]}, body)):
        assert result["events"] == ["exam"]
        record, = result["event_records"]
        assert record["subject"] == "patient" and record["status"] == "current"
        assert record["negated"] is False and not record.get("unverified")
