"""Material adoption: valid current public-1.0 extraction stays usable while
unrelated QC/thin/semantic reprocessing is only scheduled (patient-context goal)."""
import json

import patient_context_refresh_readiness as readiness
from extract_testkit import _message
from mcs_queries import EXTRACT_FEEDBACK_KIND
from test_patient_context_refresh import CFG, _raw_proof, runtime, step, stamp
from test_patient_context_partial import publish

__all__ = ['runtime']


def _seed_extract_only(runtime, mid=1, pid=1, body='生活環境: 合成の居住記録。',
                       state='full'):
    """Message + current public-1.0 extraction proof, without semantic audit."""
    led, _, _ = runtime
    led.db.execute('INSERT OR IGNORE INTO patients(project_id,patient_name) VALUES (?,?)',
                   (pid, f'synthetic-{pid}'))
    led.db.commit()
    led.save_messages([_message(mid, body, state, project_id=pid)])
    led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=?',
                   (stamp('2026-10-17T22:00:00'), mid))
    led.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,"
                   "history_floor=-1 WHERE project_id=?", (pid,))
    led.db.commit()
    _raw_proof(led, pid, mid)


def test_extract_valid_source_publishes_while_semantic_reprocess_pending(runtime):
    """Semantic audit/summary reprocessing pending must not erase usable
    extract material: the source publishes with extract-derived context and
    honestly lacks the semantic summary."""
    _seed_extract_only(runtime)
    view = publish(runtime)
    assert view is not None
    assert view['coverage']['partial'] is True or view['coverage'].get('unreflected') == 0


def test_extract_valid_source_publishes_while_thin_retry_scheduled(runtime):
    """A scheduled thin-quality retry does not invalidate the current artifact.
    Semantic proof stays current so only the pending_pred thin clause is under test."""
    from test_patient_context_refresh import seed as _seed_full
    led, _, _ = runtime
    _seed_full(led, body='生活環境: ' + '合成の記録。' * 60)
    led.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.thin_retried') "
                   "WHERE kind='extract_llm'")
    led.db.commit()
    view = publish(runtime)
    assert view is not None


def test_human_flagged_source_stays_excluded(runtime):
    """A fresh human report pinned to the current artifact remains a denial."""
    _seed_extract_only(runtime)
    led, _, _ = runtime
    artifact_id = led.db.execute(
        "SELECT artifact_id FROM artifacts WHERE kind='extract_llm' "
        "AND message_id=1").fetchone()[0]
    led.artifact_add(EXTRACT_FEEDBACK_KIND,
                     json.dumps({'artifact_id': artifact_id}),
                     project_id=1, message_id=1, model='synthetic', meta={})
    for _ in range(20):
        result = step(runtime)
        view = readiness.context_view_input(led.db, 1, cfg=CFG)
        if view is not None:
            break
        assert result['status'] in ('progressed', 'deferred'), result
    assert view is None


def test_progress_shows_partial_coverage_not_stale_zero(runtime):
    """A verified partial publication displays honest coverage while
    acquisition is incomplete — never the stale (0, 'inputs') status."""
    import patient_context_progress as progress
    led, _, _ = runtime
    _seed_extract_only(runtime)
    led.save_messages([_message(2, '生活環境: 合成の未解析記録。', 'full', project_id=1)])
    led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=2',
                   (stamp('2026-10-17T22:01:00'),))
    led.db.execute("UPDATE patients SET fetch_state='incomplete',history_floor=NULL WHERE project_id=1")
    led.db.commit()
    view = publish(runtime)
    assert view is not None and view['coverage']['unreflected'] == 1
    safe_cfg = {**CFG, 'cloud_llm': {'mode': 'off', 'provider': 'anthropic'}}
    observed = progress.read(led.db, 1, safe_cfg)
    assert observed['stage'] == 'sources'
    assert 10 <= observed['percent'] < 80


def test_source_without_extract_stays_excluded(runtime):
    """No current extraction artifact: nothing adoptable, still excluded."""
    led, _, _ = runtime
    led.db.execute('INSERT OR IGNORE INTO patients(project_id,patient_name) VALUES (?,?)',
                   (1, 'synthetic-1'))
    led.db.commit()
    led.save_messages([_message(1, '生活環境: 合成の居住記録。', 'full', project_id=1)])
    led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=?',
                   (stamp('2026-10-17T22:00:00'), 1))
    led.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,"
                   "history_floor=-1 WHERE project_id=1")
    led.db.commit()
    for _ in range(20):
        result = step(runtime)
        view = readiness.context_view_input(led.db, 1, cfg=CFG)
        if view is not None:
            break
        assert result['status'] in ('progressed', 'deferred'), result
    assert view is None
