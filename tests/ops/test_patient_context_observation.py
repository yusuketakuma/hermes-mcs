"""Synthetic coverage and bounded-work checks for patient context observation."""
import json
import time

import pytest

import extract_llm
import patient_context_refresh as context
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY


def _old_counts(db):
    active = db.execute("SELECT COUNT(*) FROM patients WHERE COALESCE(is_archived,0)=0").fetchone()[0]
    rows = db.execute(
        f"SELECT SUM(CASE WHEN ({extract_llm._retry_budget_sql()}) AND COALESCE((SELECT MAX(json_extract(e.meta,'$.next_try')) "
        f"{extract_llm._ERROR_ROWS_SQL}), 0) <= ? AND NOT {extract_llm._FRESH_SQL} THEN 1 ELSE 0 END) "
        "FROM messages m JOIN patients p ON p.project_id=m.project_id "
        f"WHERE COALESCE(p.is_archived,0)=0 AND {extract_llm.pending_pred(db)} GROUP BY m.project_id",
        (extract_llm.KIND, extract_llm.EXTRACT_VERSION, time.time())).fetchall()
    focus = sum(1 for row in rows if row[0])
    return {"active_patients": active, "extraction_complete": max(0, active - len(rows)),
            "focus_eligible": focus, "focus_targets": min(focus, extract_llm._FOCUS_PATIENTS)}


def _steps(db, fn):
    steps = [0]
    def tick():
        steps[0] += 100
        return 0
    db.set_progress_handler(tick, 100)
    try:
        result = fn(db)
    finally:
        db.set_progress_handler(None, 0)
    return result, steps[0]


def test_counts_preserve_retry_and_message_scope(tmp_path):
    ledger = _ledger(tmp_path)
    db = ledger.db
    db.executemany("INSERT INTO patients(project_id,patient_name,is_archived) VALUES (?,?,?)",
                   [(pid, 'synthetic', pid == 5) for pid in range(1, 6)])
    ledger.save_messages([_message(mid=pid, project_id=pid, body='合成の記録') for pid in range(1, 6)])
    for pid in (1, 2, 5):
        ledger.artifact_add(extract_llm.KIND, '{}', project_id=pid, message_id=pid,
                            meta={'hash': _hash(ledger, pid), 'extract_version': extract_llm.EXTRACT_VERSION - 1})
    ledger.artifact_add(extract_llm.KIND, '{}', project_id=2, message_id=2,
                        meta={VERSION_KEY: CURRENT_VERSION, 'hash': _hash(ledger, 2),
                              'extract_version': extract_llm.EXTRACT_VERSION, 'error': True, 'attempts': 5})
    db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=4")
    before = db.total_changes
    expected = {'active_patients': 4, 'extraction_complete': 1, 'focus_eligible': 1, 'focus_targets': 1}
    assert _old_counts(db) == extract_llm.patient_progress_counts(db) == expected
    assert db.total_changes == before
    ledger.close()


def test_large_pending_corpus_uses_fewer_sql_steps(tmp_path):
    ledger = _ledger(tmp_path)
    db = ledger.db
    db.executemany("INSERT INTO patients(project_id,patient_name) VALUES (?,'synthetic')",
                   [(pid,) for pid in range(1, 21)])
    db.executemany("INSERT INTO messages(message_id,project_id,body_text,body_state,content_hash) VALUES (?,?,?,'full','synthetic')",
                   [(pid * 1000 + i, pid, '合成') for pid in range(1, 21) for i in range(200)])
    old, old_steps = _steps(db, _old_counts)
    new, new_steps = _steps(db, extract_llm.patient_progress_counts)
    assert new == old == {'active_patients': 20, 'extraction_complete': 0, 'focus_eligible': 0, 'focus_targets': 0}
    assert new_steps < old_steps, (old_steps, new_steps)
    print(f'SQL VM steps: old={old_steps}, new={new_steps}')
    ledger.close()


def test_lane_counts_exclude_archived_and_reject_invalid_current_json(tmp_path):
    ledger = _ledger(tmp_path)
    db = ledger.db
    db.executemany("INSERT INTO patients(project_id,patient_name,is_archived) VALUES (?,'synthetic',?)", [(1, 0), (2, 1)])
    meta = {VERSION_KEY: CURRENT_VERSION, 'published': True}
    for pid in (1, 2):
        ledger.artifact_add(context.PROGRESS, json.dumps({'failure': 'nightly_length_stop'}), project_id=pid, meta=meta)
        ledger.artifact_add(context.SCAN, json.dumps({'complete': True, 'split': 1, 'repairs': 1}), project_id=pid, meta=meta)
        ledger.artifact_add(context.KIND, '{}', project_id=pid, meta=meta)
    result = context.lane_metrics(db)
    assert result['published'] == 1
    assert result['lanes']['A']['length_stop'] == 1
    assert result['lanes']['B']['split'] == result['lanes']['B']['repair'] == 1
    ledger.artifact_add(context.SCAN, '{', project_id=1, meta=meta)
    with pytest.raises(ValueError, match='invalid patient context observation'):
        context.lane_metrics(db)
    ledger.close()
