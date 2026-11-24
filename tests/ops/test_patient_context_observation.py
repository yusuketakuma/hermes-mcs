"""Synthetic coverage and bounded-work checks for patient context observation."""
import json
import sqlite3
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
        (time.time(),)).fetchall()
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


def test_lane_counts_exclude_archived_and_count_invalid_current_json(tmp_path):
    ledger = _ledger(tmp_path)
    db = ledger.db
    db.executemany("INSERT INTO patients(project_id,patient_name,is_archived) VALUES (?,'synthetic',?)", [(1, 0), (2, 1)])
    meta = {VERSION_KEY: CURRENT_VERSION, 'published': True}
    for pid in (1, 2):
        ledger.artifact_add(context.PROGRESS, json.dumps({'failure': 'patient_context_length_stop'}), project_id=pid, meta=meta)
        ledger.artifact_add(context.SCAN, json.dumps({'complete': True, 'split': 1, 'repairs': 1}), project_id=pid,
                            meta={**meta, 'source_key': 1})
        ledger.artifact_add(context.KIND, '{}', project_id=pid, meta=meta)
    expected = context.lane_metrics(db)
    assert expected['published'] == 1 and expected['invalid_rows'] == 0
    assert expected['lanes']['A']['length_stop'] == 1
    assert expected['lanes']['B']['split'] == expected['lanes']['B']['repair'] == 1
    assert expected['quality'] == {'none': 1, 'pending': 0, 'deferred': 0, 'held': 0,
                                   'failed': 0, 'unavailable': 0, 'unknown': 0,
                                   'budget': 0, 'disabled': 0, 'other': 0}
    # Malformed latest rows of other sources (and an archived patient's) are counted, never raised.
    for key, content in ((2, '{'), (3, '[1]'), (4, '"synthetic"')):
        ledger.artifact_add(context.SCAN, content, project_id=1, meta={**meta, 'source_key': key})
    ledger.artifact_add(context.SCAN, '{', project_id=2, meta={**meta, 'source_key': 2})
    result = context.lane_metrics(db)
    assert result['invalid_rows'] == 3
    assert {k: result[k] for k in ('lanes', 'published', 'quality')} == {
        k: expected[k] for k in ('lanes', 'published', 'quality')}
    # A malformed newer row of the same source supersedes the valid one: invalid, not quality none.
    ledger.artifact_add(context.SCAN, '[]', project_id=1, meta={**meta, 'source_key': 1})
    result = context.lane_metrics(db)
    assert result['invalid_rows'] == 4
    assert result['lanes']['B']['split'] == result['lanes']['B']['repair'] == 0
    assert sum(result['quality'].values()) == 0
    assert result['lanes']['A'] == expected['lanes']['A']
    ledger.close()


def test_lane_quality_counts_group_scan_states_and_supersede(tmp_path):
    ledger = _ledger(tmp_path)
    db = ledger.db
    db.executemany("INSERT INTO patients(project_id,patient_name,is_archived) VALUES (?,'synthetic',?)",
                   [(1, 0), (2, 1), (3, 0)])
    meta = {VERSION_KEY: CURRENT_VERSION}
    # pid1: held→deferred の置き換えで最新のみ deferred に集計、suffix は state へ畳む
    ledger.artifact_add(context.SCAN, json.dumps({'complete': True, 'quality_state': 'held'}),
                        project_id=1, meta={**meta, 'source_key': 's1'})
    ledger.artifact_add(context.SCAN, json.dumps({'complete': True, 'quality_state': 'deferred'}),
                        project_id=1, meta={**meta, 'source_key': 's1'})
    ledger.artifact_add(context.SCAN, json.dumps({'complete': True,
                        'quality_state': 'unavailable:patient_summary_quality_source_changed'}),
                        project_id=1, meta={**meta, 'source_key': 's2'})
    # pid2 は archived で除外、pid3 は quality_state なしで none
    ledger.artifact_add(context.SCAN, json.dumps({'complete': True, 'quality_state': 'failed'}),
                        project_id=2, meta={**meta, 'source_key': 's1'})
    ledger.artifact_add(context.SCAN, json.dumps({'complete': True}),
                        project_id=3, meta={**meta, 'source_key': 's1'})
    result = context.lane_metrics(db)
    assert result['quality']['deferred'] == 1
    assert result['quality']['unavailable'] == 1
    assert result['quality']['none'] == 1
    assert result['quality']['held'] == result['quality']['failed'] == 0
    assert sum(v for k, v in result['quality'].items() if k != 'other') == 3
    ledger.close()


def test_health_patient_context_records_invalid_rows_and_recomputes_old_cache(tmp_path, monkeypatch):
    import health_report
    ledger = _ledger(tmp_path)
    db = ledger.db
    db.execute("INSERT INTO patients(project_id,patient_name) VALUES (1,'synthetic')")
    meta = {VERSION_KEY: CURRENT_VERSION}
    ledger.artifact_add(context.SCAN, '{', project_id=1, meta={**meta, 'source_key': 1})
    now = time.time()
    # A cached reading from before invalid_rows existed is recomputed, not reused.
    old = {'status': 'recorded', 'observed_at': now, 'source': 'synthetic',
           'extract_version': extract_llm.EXTRACT_VERSION, 'published': 0, 'active_patients': 1,
           'extraction_complete': 0, 'focus_eligible': 0, 'focus_targets': 0}
    monkeypatch.setattr(health_report, 'prev_health', lambda: {'patient_context': old})
    result = health_report.patient_context_health(db, None, now, 'synthetic')
    assert result['status'] == 'recorded' and result['invalid_rows'] == 1
    ledger.close()


def test_doctor_states_warn_on_the_rows_lane_metrics_counts_invalid(tmp_path):
    """health.json's invalid_rows and doctor's states.invalid_rows see the same latest rows."""
    import patient_context_status
    ledger = _ledger(tmp_path)
    db = ledger.db
    db.execute("INSERT INTO patients(project_id,patient_name) VALUES (1,'synthetic')")
    meta = {VERSION_KEY: CURRENT_VERSION}
    ledger.artifact_add(context.PROGRESS, '[1]', project_id=1, meta=meta)
    for key, content in ((1, '{'), (2, '"synthetic"')):
        ledger.artifact_add(context.SCAN, content, project_id=1, meta={**meta, 'source_key': key})
    assert context.lane_metrics(db)['invalid_rows'] == 3
    states = patient_context_status.sanitize(patient_context_status.state_counts(db))
    assert states['invalid_rows'] == 3 and patient_context_status.has_problem(states)
    ledger.close()


_CARRIED_COUNTS = {'published': 2, 'active_patients': 3, 'extraction_complete': 1,
                   'focus_eligible': 1, 'focus_targets': 1, 'invalid_rows': 0}


def _context_health(monkeypatch, previous, now, *, fail=None, cfg=None):
    """Run patient_context_health against ``previous`` with a refresh that fails as ``fail``."""
    import health_report
    reads = []

    def read(db, fn, seconds=None, **kwargs):
        reads.append(seconds)
        if fail is not None:
            raise fail
        return {'lanes': {}, 'quality': {}, **_CARRIED_COUNTS, 'published': 9}
    monkeypatch.setattr(health_report, 'prev_health', lambda: {'patient_context': previous})
    monkeypatch.setattr(health_report, 'bounded_health_read', read)
    return health_report.patient_context_health(None, cfg, now, 'synthetic', seconds=30), reads


def _recorded(observed_at, **extra):
    return {**_CARRIED_COUNTS, 'lanes': {}, 'quality': {}, 'publication': True, 'status': 'recorded',
            'observed_at': observed_at, 'source': 'synthetic',
            'extract_version': extract_llm.EXTRACT_VERSION, **extra}


@pytest.mark.parametrize('fail,reason', [(RuntimeError('observation_limit'), 'observation_limit'),
                                         (sqlite3.DatabaseError('synthetic'), 'observation_unavailable')])
def test_failed_refresh_carries_the_last_recorded_counts_as_stale(monkeypatch, fail, reason):
    import health_report
    now = 100_000.0
    observed = now - health_report.COVERAGE_COUNT_S - 5
    cfg = {'extract': {'patient_context': {'publication': False}}}
    result, reads = _context_health(monkeypatch, _recorded(observed), now, fail=fail, cfg=cfg)
    assert reads == [health_report.PATIENT_CONTEXT_S]       # the refresh was attempted within its bound
    assert result['status'] == 'stale_measurement' and result['reason'] == reason
    assert result['observed_at'] == observed and result['age_s'] == round(now - observed, 1)
    assert {k: result[k] for k in _CARRIED_COUNTS} == _CARRIED_COUNTS
    assert result['publication'] is False                   # current config, not the old block


def test_carried_counts_carry_again_with_the_original_time_and_still_retry(monkeypatch):
    import health_report
    now = 100_000.0
    observed = now - 2 * health_report.COVERAGE_COUNT_S
    carried = _recorded(observed, status='stale_measurement', reason='observation_limit', age_s=1.0)
    result, reads = _context_health(monkeypatch, carried, now, fail=RuntimeError('observation_limit'))
    assert len(reads) == 1 and result['status'] == 'stale_measurement'
    assert result['observed_at'] == observed and result['age_s'] == round(now - observed, 1)
    assert result['publication'] is None                    # unchecked config is unknown publication
    # a recent carried value is never reused as fresh: the refresh runs and replaces it
    recent = _recorded(now - 10, status='stale_measurement', reason='observation_limit', age_s=10.0)
    result, reads = _context_health(monkeypatch, recent, now)
    assert len(reads) == 1 and result['status'] == 'recorded' and result['observed_at'] == now
    assert result['published'] == 9


@pytest.mark.parametrize('previous', [
    'too_old', 'other_ledger', 'other_generation', 'unknown', 'missing_count'])
def test_unusable_previous_counts_stay_unknown(monkeypatch, previous):
    import health_report
    now = 100_000.0
    block = {'too_old': _recorded(now - health_report.PATIENT_CONTEXT_CARRY_S - 1),
             'other_ledger': _recorded(now - 4000, source='other'),
             'other_generation': _recorded(now - 4000, extract_version=extract_llm.EXTRACT_VERSION - 1),
             'unknown': {'status': 'unknown', 'observed_at': now - 600, 'reason': 'observation_limit',
                         'publication': True},
             'missing_count': {k: v for k, v in _recorded(now - 4000).items() if k != 'invalid_rows'}}[previous]
    result, _ = _context_health(monkeypatch, block, now, fail=RuntimeError('observation_limit'))
    assert result == {'status': 'unknown', 'observed_at': now, 'reason': 'observation_limit',
                      'publication': None}
