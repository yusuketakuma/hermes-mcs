"""Fully synthetic rule extraction repair and minimum-runtime date regressions."""
import json

import pytest

import extract
from extract_testkit import _hash, _ledger, _message


_BAD_META = ["{broken", "[" * 1500 + "0" + "]" * 1500, "[]", "null"]
_META_IDS = ["malformed", "deep", "array", "null"]
_BODY = "12/31訪問実施。次回1/5訪問予定。処方薬12/31-1/14。"


@pytest.mark.parametrize("raw", _BAD_META, ids=_META_IDS)
@pytest.mark.parametrize("state", ["full", "deleted", "unknown"])
def test_invalid_rule_metadata_cannot_block_repair_or_retain_unavailable_body(tmp_path, raw, state):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body="合成の訪問実施", posted_at="2026-01-02T00:30:00+00:00")])
        old = db.artifact_add("extract_v1", '{"synthetic_old":true}',
                              project_id=1, message_id=1)
        other = db.artifact_add("synthetic-unrelated", "{}", project_id=1, message_id=1)
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (raw, old))
            db.db.execute("UPDATE messages SET body_state=? WHERE message_id=1", (state,))
        source = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        result = extract.run_pending(db)
        rows = db.artifacts("extract_v1", message_id=1)
        if state == "full":
            assert result == {"done": 1, "pids": [1]}
            assert len(rows) == 1 and rows[0]["artifact_id"] != old
            assert json.loads(rows[0]["content"]) == extract.extract_message(
                source["body_text"], source["posted_at"])
            assert json.loads(rows[0]["meta"]) == {"hash": _hash(db),
                                                   "rule_version": extract.RULE_VERSION}
            assert extract.run_pending(db) == {"done": 0, "pids": []}
        else:
            assert result == {"done": 0, "pids": []}
            assert rows == []
        assert dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()) == source
        assert db.db.execute("SELECT artifact_id FROM artifacts WHERE artifact_id=?", (other,)).fetchone()
    finally:
        db.close()


@pytest.mark.parametrize("raw", _BAD_META, ids=_META_IDS)
def test_rule_metadata_repair_keeps_old_artifact_when_deadline_cuts_before_replacement(
        tmp_path, monkeypatch, raw):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body="合成の訪問実施")])
        old = db.artifact_add("extract_v1", '{"synthetic_old":true}',
                              project_id=1, message_id=1)
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (raw, old))
        before = db.artifacts("extract_v1", message_id=1)
        clock = iter((0, 2))
        monkeypatch.setattr(extract.time, "monotonic", lambda: next(clock))
        assert extract.run_pending(db, deadline=1) == {"done": 0, "pids": []}
        assert db.artifacts("extract_v1", message_id=1) == before
        assert extract.run_pending(db) == {"done": 1, "pids": [1]}
    finally:
        db.close()


@pytest.mark.parametrize("stamp", ["2026-01-02T00:30:00", "2026-01-02T00:30:00.123",
                                   "2026-01-02 00:30:00"])
def test_utc_suffix_preserves_the_same_rule_dates_as_explicit_utc_offset(stamp):
    expected = extract.extract_message(_BODY, stamp + "+00:00")
    assert expected["visit_date"] == "2025-12-31"
    assert expected["next_planned"] == "2026-01-05"
    assert expected["med_periods"][0]["start"] == "2025-12-31"
    assert expected["med_periods"][0]["end"] == "2026-01-14"
    assert extract.extract_message(_BODY, stamp + "Z") == expected


@pytest.mark.parametrize("stamp", ["2026-01-02", "2026-01-02T00:30:00"])
def test_date_and_naive_posting_anchors_keep_existing_rule_dates(stamp):
    result = extract.extract_message(_BODY, stamp)
    assert result["visit_date"] == "2025-12-31"
    assert result["next_planned"] == "2026-01-05"
    assert result["med_periods"][0]["start"] == "2025-12-31"


@pytest.mark.parametrize("stamp", [None, "", "synthetic-invalid", "2026-01-02Z",
                                   "2026-01-02TZ", "2026-01-02T00:30:00+09:00Z"])
def test_invalid_posting_anchors_do_not_invent_years_or_medication_dates(stamp):
    result = extract.extract_message(_BODY, stamp)
    assert "visit_date" not in result and "next_planned" not in result
    assert result["med_periods"] == [{"raw": "12/31-1/14"}]


def test_previous_rule_generation_with_matching_hash_is_repaired_once(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body=_BODY, posted_at="2026-01-02T00:30:00Z")])
        old = db.artifact_add("extract_v1", '{"v":1}', project_id=1, message_id=1,
                              meta={"hash": _hash(db), "rule_version": 7})
        assert extract.run_pending(db) == {"done": 1, "pids": [1]}
        rows = db.artifacts("extract_v1", message_id=1)
        assert len(rows) == 1 and rows[0]["artifact_id"] != old
        result = json.loads(rows[0]["content"])
        assert result["visit_date"] == "2025-12-31"
        assert result["next_planned"] == "2026-01-05"
        assert result["med_periods"][0]["start"] == "2025-12-31"
        assert json.loads(rows[0]["meta"])["rule_version"] == extract.RULE_VERSION
        assert extract.run_pending(db) == {"done": 0, "pids": []}
    finally:
        db.close()
