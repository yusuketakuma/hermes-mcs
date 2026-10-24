"""A raw high whose source body cannot be read is held for review, never promoted (synthetic)."""
import pytest

import clinical_urgency
import structured_view
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of

BODY = "本人が急変、至急確認してください。"


def _seed(tmp_path, llm, rule=None, body_state="snippet", body_text=BODY):
    db = _db(tmp_path, ())
    db.save_messages([_message(1, body=BODY)], project_id=1)
    if llm is not None:
        _artifact(db, "extract_llm", 1, llm, {"hash": _hash_of(db, 1)})
    if rule is not None:
        _artifact(db, "extract_v1", 1, rule, {"hash": _hash_of(db, 1)})
    db.db.execute("UPDATE messages SET body_state=?, body_text=? WHERE message_id=1",
                  (body_state, body_text))
    db.db.commit()
    return db


# a deleted post is excluded before any verdict is read, so it is not a held case
@pytest.mark.parametrize("body_state,body_text", [("snippet", BODY), ("full", None)])
def test_unreadable_source_holds_raw_high_without_promoting_it(tmp_path, body_state, body_text):
    db = _seed(tmp_path, {"urgency": "high", "urgency_evidence": [BODY]},
               body_state=body_state, body_text=body_text)
    try:
        details = clinical_urgency.message_urgency_details(db.db, 1)
        assert details["source"] is None and details["subject"] == "unknown"
        assert details["raw_verdict"] == "high" and details["verdict"] == "unclear"
        assert details["held"] and details["scopes"] == ["unknown"] and details["reasons"] == []
    finally:
        db.close()


def test_missing_source_row_holds_raw_high(tmp_path):
    db = _seed(tmp_path, {"urgency": "high", "urgency_evidence": [BODY]}, body_state="full")
    try:
        details = clinical_urgency.message_urgency_details(db.db, 1, source=None)
        assert details["held"] and details["verdict"] == "unclear" and details["source"] is None
    finally:
        db.close()


def test_snippet_display_shows_review_line_not_high(tmp_path):
    db = _seed(tmp_path, {"urgency": "high", "urgency_evidence": [BODY]})
    try:
        lines = structured_view.structured_lines(db.db, 1)
        assert any("緊急度: 要確認" in line for line in lines)
        assert not any("緊急度: 高" in line for line in lines)
    finally:
        db.close()


@pytest.mark.parametrize("llm,rule", [
    ({"urgency": "routine"}, {"urgency": "high"}),     # routine stays routine
    ({"urgency": "unclear"}, {"urgency": "high"}),     # only a raw high is held
    (None, {"urgency": "high"}),                       # rule-only high is not a model verdict
])
def test_unreadable_source_leaves_non_high_verdicts_unchanged(tmp_path, llm, rule):
    db = _seed(tmp_path, llm, rule)
    try:
        details = clinical_urgency.message_urgency_details(db.db, 1)
        assert not details["held"] and details["source"] is None
        assert details["verdict"] == (llm or {}).get("urgency")
    finally:
        db.close()
