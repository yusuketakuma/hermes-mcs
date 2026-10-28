"""Synthetic source identity boundaries for raw and published context."""
import patient_context_plan as context_plan
from contextlib import closing
from copy import deepcopy

import pytest

import patient_context_refresh as refresh
import patient_context_view as views
from patient_context import extract_context
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of


def _source(db, mid):
    row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
    return {**row, "source": "chat"}


def _seed(db, body, subject=None):
    db.save_messages([_message(1, body=body)], project_id=1)
    items = extract_context(body)
    if subject:
        for item in items:
            item["subject"] = subject
    _artifact(db, "extract_v1", 1, {"patient_context": items}, {"hash": _hash_of(db, 1)})
    source = _source(db, 1)
    facts = context_plan.source_facts(source, items, night_id="2026-10-01", discovery="new_report", found_at=refresh.time.time())
    assert facts and context_plan.validated_source_facts(source, facts) == facts
    return source, facts


def _publish(monkeypatch, facts):
    monkeypatch.setattr(refresh, "context_view_read", lambda *a, **k: ({
        "as_of": 1_790_000_000, "facts": facts, "summary": [], "coverage": {"analyzed": 1}}, {
        "complete": False, "status": "scan_pending", "sources": {"pending": 1}}))


def test_identical_real_source_fact_has_one_visible_row_and_retains_annotations(tmp_path, monkeypatch):
    with closing(_db(tmp_path, ())) as db:
        source, facts = _seed(db, "生活環境: 本人は室内で過ごす。")
        assert len(facts) == 1
        fact = facts[0]
        fact["view_bucket"] = "unknown"
        _publish(monkeypatch, facts)
        ev = views.evidence(db.db, 1)
        rows = [row for row in ev["items"] if row.get("message_id") == 1 and row["category"] == "living"]
        assert len(rows) == 1
        assert rows[0]["quote"] == fact["quote"] == source["body_text"]
        assert rows[0]["discovery"] == fact["discovery"]
        assert rows[0]["temporal"] == fact["temporal"]
        assert rows[0]["view_bucket"] == "unknown"
        assert ev["nightly_facts"][0]["id"] == fact["id"]
        assert ev["coverage"]["nightly"]["analyzed"] == 1


@pytest.mark.parametrize("subject,body", [
    ("patient", "生活環境: 本人は室内で過ごす。"),
    ("family", "生活環境: 家族は室内で過ごす。"),
])
def test_merged_fact_preserves_reported_and_unresolved_subject(tmp_path, monkeypatch, subject, body):
    with closing(_db(tmp_path, ())) as db:
        _, facts = _seed(db, body, subject=subject)
        _publish(monkeypatch, facts)
        rows = [row for row in views.evidence(db.db, 1)["items"] if row["category"] == "living"]
        assert len(rows) == 1
        assert rows[0]["subject"] == "unspecified"
        assert rows[0]["reported_subject"] == subject


def test_foreign_project_fact_is_not_exposed_or_merged(tmp_path, monkeypatch):
    with closing(_db(tmp_path, ())) as db:
        _, facts = _seed(db, "生活環境: 本人は室内で過ごす。")
        fact = deepcopy(facts[0])
        fact["project_id"] = 2
        fact["quote"] = "別患者の合成記載"
        _publish(monkeypatch, [fact])
        ev = views.evidence(db.db, 1)
        assert "別患者の合成記載" not in str(ev)
        assert ev["nightly_facts"] == []


@pytest.mark.parametrize("same_id", [False, True])
def test_foreign_summary_or_colliding_fact_id_does_not_cross_patient_scope(tmp_path, monkeypatch, same_id):
    with closing(_db(tmp_path, ())) as db:
        _, facts = _seed(db, "生活環境: 本人は室内で過ごす。")
        local = facts[0]
        foreign = {**deepcopy(local), "project_id": 2,
                   "id": local["id"] if same_id else "foreign-synthetic-id"}
        summaries = [{"text": "本人の合成要約", "fact_ids": [local["id"]]},
                     {"text": "別患者の合成要約", "fact_ids": [foreign["id"]]}]
        _publish(monkeypatch, [local, foreign])
        monkeypatch.setattr(refresh, "context_view_read", lambda *a, **k: ({
            "as_of": 1_790_000_000, "facts": [local, foreign], "summary": summaries,
            "coverage": {"analyzed": 1}},
            {"complete": False, "status": "scan_pending", "sources": {"pending": 1}}))
        ev = views.evidence(db.db, 1)
        assert "別患者の合成要約" not in str(ev)
        assert [s["text"] for s in ev["summary"]] == ([] if same_id else ["本人の合成要約"])


@pytest.mark.parametrize("difference", ["subject", "span", "quote", "content_hash", "details"])
def test_distinct_assertions_are_not_merged(tmp_path, monkeypatch, difference):
    with closing(_db(tmp_path, ())) as db:
        source, facts = _seed(db, "生活環境: 本人は室内で過ごす。")
        fact = deepcopy(facts[0])
        if difference == "subject":
            fact["reported_subject"] = "family"
        elif difference == "span":
            fact["evidence_span"] = [fact["evidence_span"][0] + 1, fact["evidence_span"][1]]
            fact["evidence"] = source["body_text"][slice(*fact["evidence_span"])]
        elif difference == "quote":
            fact["quote"] = "合成の別の引用"
        elif difference == "content_hash":
            fact["content_hash"] = "0" * 64
        else:
            fact["details"] = [{"key": "value", "value": "異なる合成値", "evidence": fact["evidence"]}]
        _publish(monkeypatch, [fact])
        rows = [row for row in views.evidence(db.db, 1)["items"]
                if row.get("message_id") == 1 and row["category"] == "living"]
        assert len(rows) == 2
        assert sum(row["discovery"] is None for row in rows) == 1


def test_repeated_quote_does_not_gain_a_guessed_source_span(tmp_path, monkeypatch):
    body = "生活環境: 同じ合成記載\n生活環境: 同じ合成記載"
    with closing(_db(tmp_path, ())) as db:
        source, facts = _seed(db, "生活環境: 同じ合成記載")
        db.save_messages([_message(1, body=body)], project_id=1)
        _publish(monkeypatch, facts)
        rows = [row for row in views.evidence(db.db, 1)["items"]
                if row.get("message_id") == 1 and row["category"] == "living"]
        # The earlier published hash is no longer current; neither it nor an
        # ambiguous raw quote can become a current, merged statement.
        assert all(row.get("content_hash") != _hash_of(db, 1) for row in rows)
        assert source["content_hash"] != _hash_of(db, 1)
