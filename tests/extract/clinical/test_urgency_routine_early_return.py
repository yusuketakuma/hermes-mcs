"""A routine verdict returns before rule/source reads; preloaded inputs match the reads."""
import pytest

import clinical_urgency
import mcs_queries
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of


def _seed(tmp_path, body, llm, rule):
    db = _db(tmp_path, ())
    db.save_messages([_message(1, body=body)], project_id=1)
    if llm is not None:
        _artifact(db, "extract_llm", 1, llm, {"hash": _hash_of(db, 1)})
    if rule is not None:
        _artifact(db, "extract_v1", 1, rule, {"hash": _hash_of(db, 1)})
    return db


def test_llm_routine_keeps_result_despite_v1_high_without_rule_or_source_reads(tmp_path, monkeypatch):
    db = _seed(tmp_path, "本人が急変。至急連絡してください。", {"urgency": "routine"}, {"urgency": "high"})
    try:
        real_extract = mcs_queries.current_message_extract

        def extract(conn, kind, mid):
            assert kind != "extract_v1", "routine must not read the rule extraction"
            return real_extract(conn, kind, mid)

        def no_source(*_args):
            raise AssertionError("routine must not read the source row")
        monkeypatch.setattr(mcs_queries, "current_message_extract", extract)
        monkeypatch.setattr(mcs_queries, "message_urgency_source", no_source)
        assert clinical_urgency.message_urgency_details(db.db, 1) == {
            "source": None, "subject": "unknown", "reasons": [], "raw_verdict": "routine",
            "verdict": "routine", "held": False, "kind": "clinical"}
    finally:
        db.close()


@pytest.mark.parametrize("body,llm,rule", [
    ("本人が急変。至急連絡してください。", None, {"urgency": "high"}),
    ("母が急変。本人の記録を至急確認してください。", None, {"urgency": "high"}),
    ("本人が急変、至急確認してください。",
     {"urgency": "high", "urgency_evidence": ["本人が急変、至急確認してください。"]}, None),
    ("本人の記録を至急確認してください。",
     {"urgency": "high", "urgency_evidence": ["本人の記録を至急確認してください。"]}, None),
    ("本人の体温36.5度です。", {"urgency": "unclear"}, {"urgency": "high"}),
    ("本人の体温36.5度です。", {"urgency": "routine"}, {"urgency": "high"}),
    ("本人の体温36.5度です。", None, None),
])
def test_preloaded_inputs_equal_the_default_reads(tmp_path, body, llm, rule):
    db = _seed(tmp_path, body, llm, rule)
    try:
        expected = clinical_urgency.message_urgency_details(db.db, 1)
        preloaded = clinical_urgency.message_urgency_details(
            db.db, 1, facts=mcs_queries.current_message_facts(db.db, 1),
            rule=mcs_queries.current_message_extract(db.db, "extract_v1", 1),
            source=mcs_queries.message_urgency_source(db.db, 1))
        assert preloaded == expected
    finally:
        db.close()


def test_explicit_none_is_an_empty_read_not_an_omission(tmp_path, monkeypatch):
    db = _seed(tmp_path, "本人が急変、至急確認してください。",
               {"urgency": "high", "urgency_evidence": ["本人が急変、至急確認してください。"]},
               {"urgency": "high"})
    try:
        def unread(*_args):
            raise AssertionError("an explicit None must not be re-read")
        # facts=None still falls back to extract_llm like an absent facts row
        assert clinical_urgency.message_urgency_details(db.db, 1, facts=None)["source"] == "llm"
        monkeypatch.setattr(mcs_queries, "message_urgency_source", unread)
        held = clinical_urgency.message_urgency_details(db.db, 1, rule=None, source=None)
        # an unreadable source row holds a raw high for review (owner decision 2026-10-24)
        assert held["source"] is None and held["verdict"] == "unclear" and held["held"]
        assert held["raw_verdict"] == "high" and held["scopes"] == ["unknown"]
    finally:
        db.close()
