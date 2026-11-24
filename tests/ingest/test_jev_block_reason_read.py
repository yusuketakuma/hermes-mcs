"""Synthetic semantic_assess history: the bounded read returns the same row as the full scan (F4)."""
import sqlite3

import pytest

import health_report
import semantic_policy
from common_testkit import ledger_fixture

db = ledger_fixture(name="db")

# The original read, kept here as the independent oracle of the meaning:
# the json_valid semantic_assess row with the highest artifact_id.
ORACLE = ("SELECT json_extract(meta,'$.error_kind') FROM artifacts "
          "WHERE kind='semantic_assess' AND json_valid(meta) ORDER BY artifact_id DESC LIMIT 1")
BLOCKED = '{"technical_status":"error","error_kind":"payment_required"}'
OK = '{"technical_status":"ok"}'


def _add(db, kind, meta, created_at):
    db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) "
                  "VALUES(?,1,NULL,'{}',?,?)", (kind, meta, created_at))


def _expected(db):
    row = db.db.execute(ORACLE).fetchone()
    kind = row[0] if row else None
    return kind if kind in health_report._JEV_BLOCK_KINDS else None


SCENARIOS = {
    # (kind, meta, created_at) in insertion (= artifact_id) order
    "none": [],
    "other_kinds_only": [("semantic_summary", BLOCKED, 100.0), ("extract_llm", BLOCKED, 200.0)],
    "blocked_latest": [("semantic_assess", OK, 100.0), ("semantic_assess", BLOCKED, 200.0)],
    "later_success_clears": [("semantic_assess", BLOCKED, 100.0), ("semantic_assess", OK, 200.0)],
    # the highest id carries the oldest time: time order alone would pick the wrong row
    "time_reversed": [("semantic_assess", OK, 300.0), ("semantic_assess", OK, 200.0),
                      ("semantic_assess", BLOCKED, 100.0)],
    "time_reversed_ok_last": [("semantic_assess", BLOCKED, 300.0), ("semantic_assess", OK, 100.0)],
    "same_time": [("semantic_assess", OK, 100.0), ("semantic_assess", BLOCKED, 100.0),
                  ("semantic_assess", OK, 100.0), ("semantic_assess", BLOCKED, 100.0)],
    "invalid_json_newest": [("semantic_assess", BLOCKED, 100.0), ("semantic_assess", "{broken", 200.0)],
    "invalid_json_run": [("semantic_assess", BLOCKED, 100.0), ("semantic_assess", "{broken", 200.0),
                         ("semantic_assess", "[", 300.0), ("semantic_assess", None, 400.0)],
    "valid_scalar_newest": [("semantic_assess", BLOCKED, 100.0), ("semantic_assess", "1", 200.0)],
    "json_null_newest": [("semantic_assess", BLOCKED, 100.0), ("semantic_assess", "null", 200.0)],
    "only_invalid": [("semantic_assess", "{broken", 100.0), ("semantic_assess", None, 200.0)],
    "many_other_kinds_after": [("semantic_assess", BLOCKED, 100.0)]
                              + [("semantic_summary", OK, 101.0 + i) for i in range(300)],
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_bounded_read_returns_what_the_full_scan_returns(db, name):
    for row in SCENARIOS[name]:
        _add(db, *row)
    db.db.commit()
    assert health_report.jev_block_reason(db.db) == _expected(db)


def test_expected_answers_for_the_named_cases(db):
    """The oracle itself, pinned on the cases whose answer is not obvious."""
    answers = {}
    for name, rows in SCENARIOS.items():
        db.db.execute("DELETE FROM artifacts")
        for row in rows:
            _add(db, *row)
        db.db.commit()
        answers[name] = health_report.jev_block_reason(db.db)
    assert answers["time_reversed"] == "payment_required"
    assert answers["time_reversed_ok_last"] is None
    assert answers["same_time"] == "payment_required"
    assert answers["invalid_json_newest"] == "payment_required"
    assert answers["invalid_json_run"] == "payment_required"
    assert answers["valid_scalar_newest"] is None and answers["json_null_newest"] is None
    assert answers["only_invalid"] is None and answers["none"] is None
    assert answers["later_success_clears"] is None


def test_only_a_completed_qc_with_a_recorded_jev_request_clears_the_previous_block(db):
    _add(db, "semantic_assess", BLOCKED, 300)
    for kind, meta in [
        ("extract_qc", '{"qc":"done"}'),
        ("extract_qc", '{"qc":"done","jev_requests":0}'),
        ("extract_qc", '{"qc":"done","jev_requests":true}'),
        ("extract_qc", '{"qc":"done","jev_requests":"1"}'),
        ("extract_qc", '{"qc":"unevaluated","jev_requests":1}'),
        ("extract_qc", "{broken"),
        (semantic_policy.KIND_JEV_HEALTH_RECOVERY, "{}"),
        (semantic_policy.KIND_JEV_HEALTH_RECOVERY, '{"jev_requests":0}'),
        (semantic_policy.KIND_JEV_HEALTH_RECOVERY, '{"jev_requests":true}'),
        (semantic_policy.KIND_JEV_HEALTH_RECOVERY, '{"jev_requests":1.5}'),
        ("extract_qc_skipped", '{"qc":"done","jev_requests":1}'),
        ("extract_qc_wait", '{"qc":"done","jev_requests":1}'),
    ]:
        _add(db, kind, meta, 400)
        assert health_report.jev_block_reason(db.db) == "payment_required"
    _add(db, "extract_qc", '{"qc":"done","jev_requests":1}', 200)
    assert health_report.jev_block_reason(db.db) is None
    _add(db, semantic_policy.KIND_JEV_HEALTH_RECOVERY, '{"jev_requests":1}', 100)
    assert health_report.jev_block_reason(db.db) is None
    _add(db, "semantic_assess", '{"technical_status":"error","error_kind":"auth_error"}', 100)
    assert health_report.jev_block_reason(db.db) == "auth_error"


def _sent_query(db):
    statements = []
    db.db.set_trace_callback(statements.append)
    try:
        health_report.jev_block_reason(db.db)
    finally:
        db.db.set_trace_callback(None)
    query, = statements
    return query


def test_plan_reads_by_index_and_id_without_a_temporary_sort(db):
    for row in SCENARIOS["many_other_kinds_after"]:
        _add(db, *row)
    db.db.commit()
    plan = " | ".join(row[3] for row in db.db.execute("EXPLAIN QUERY PLAN " + _sent_query(db)))
    assert "idx_artifacts_kind_created" in plan and "INTEGER PRIMARY KEY" in plan
    assert "TEMP B-TREE" not in plan


def _steps(db, sql):
    count = [0]

    def tick():
        count[0] += 1
        return 0
    db.db.set_progress_handler(tick, 1)
    try:
        db.db.execute(sql).fetchall()
    finally:
        db.db.set_progress_handler(None, 0)
    return count[0]


def test_worst_shapes_stay_bounded_by_the_rows_above_the_candidate(db):
    """Many older assessments below the answer: the bounded read never visits them."""
    for index in range(3000):
        _add(db, "semantic_assess", OK, 1000.0 + index)
    _add(db, "semantic_assess", BLOCKED, 9000.0)
    for index in range(20):
        _add(db, "extract_llm", OK, 9001.0 + index)
    db.db.commit()
    full = _steps(db, ORACLE)
    current = _steps(db, _sent_query(db))
    assert health_report.jev_block_reason(db.db) == "payment_required" == _expected(db)
    assert current * 20 < full
    # the reverse worst shape: the newest-by-time row has the smallest id, so the scan
    # reads every row above it — still the same answer as the full scan
    db.db.execute("UPDATE artifacts SET created_at=99999 WHERE artifact_id=(SELECT MIN(artifact_id) "
                  "FROM artifacts WHERE kind='semantic_assess')")
    db.db.commit()
    assert health_report.jev_block_reason(db.db) == _expected(db)


def test_a_missing_kind_time_index_is_an_error_not_a_silent_none(db):
    """idx_artifacts_kind_created is a required schema index; without it the read fails loudly
    and the stall check reports a bounded unknown instead of 'no block'."""
    _add(db, "semantic_assess", BLOCKED, 100.0)
    db.db.execute("DROP INDEX idx_artifacts_kind_created")
    db.db.commit()
    with pytest.raises(sqlite3.OperationalError, match="no such index"):
        health_report.jev_block_reason(db.db)
