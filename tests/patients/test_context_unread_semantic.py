"""Patient context never waits on semantic jobs whose results no feature reads.

Synthetic only: temp ledger, no model or Jev access."""
import patient_context_refresh_readiness as readiness
import semantic_intake_hold as intake_hold
import semantic_policy
import semantic_store
from test_patient_context_refresh import CFG, _raw_proof, runtime, seed

__all__ = ['runtime']


def _cfg(**semantic):
    return {'extract': CFG['extract'],
            'semantic': {'mode': 'assist', 'project_ids': None, 'summary_mode': 'off',
                         'loop_mode': 'off', **semantic}}


def _row(led, mid=1):
    return led.db.execute('SELECT m.*,p.project_id AS patient_id,p.fetch_reason '
        'FROM messages m JOIN patients p ON p.project_id=m.project_id '
        'WHERE m.message_id=?', (mid,)).fetchone()


def _pending_semantic_job(led):
    seed(led, body='生活環境: 合成の居住記録。', proof=False)
    _raw_proof(led, 1, 1)
    led.semantic_seed(1, [1], {'source': 'synthetic-history', 'notification_free': True})
    assert led.db.execute("SELECT state FROM fetch_jobs WHERE kind=?",
                          (semantic_policy.JOB_KIND,)).fetchone()[0] == 'pending'


def _ready(runtime, cfg):
    led, clock, _ = runtime
    policy, errors = semantic_policy.runtime_config(cfg)
    assert not errors
    state = semantic_store.reprocess_target_state(led, _row(led), policy, cfg=cfg)
    return state, readiness._inference_ready(led, 1, cfg, deadline=clock.mono + 600)


def test_pending_unread_job_does_not_hold_patient_context(runtime):
    led, _, _ = runtime
    _pending_semantic_job(led)
    # no primary assessment exists and the job is pending: neither is waited on
    assert _ready(runtime, _cfg()) == ('current', True)


def test_a_reader_waits_on_the_pending_job_again(runtime):
    led, _, _ = runtime
    _pending_semantic_job(led)
    state, ready = _ready(runtime, _cfg(loop_mode='shadow', threshold_mode='calibrated',
                                        calibration_version='synthetic-test-v1'))
    assert state not in ('current', 'current_skipped') and ready is False


def test_confirmed_hold_release_allows_context_without_a_result_reader(runtime):
    import os
    from writer_lock import acquire_run_lock

    led, _, _ = runtime
    _pending_semantic_job(led)
    with led.db:
        intake_hold.hold_tx(led, 1, 1, [1],
                            {'source': 'history_import', 'notification_free': True},
                            source_generation=led._semantic_source_generation(1, 1))
    plan = intake_hold.prepare_release(led, 1, 1)
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]
    fd = acquire_run_lock(os.path.join(os.path.dirname(db_path), 'run.lock'))
    assert fd is not None
    try:
        result = intake_hold.release_root(
            led, 1, 1, expected_job_sha256=plan['job_sha256'],
            expected_hold_epoch=plan['hold_epoch'],
            expected_snapshot_sha256=plan['snapshot_sha256'],
            cfg=_cfg(), run_lock_fd=fd)
    finally:
        os.close(fd)
    assert result['released'] == 1
    assert not intake_hold.has_held_root(led.db, 1, 1)
    assert _ready(runtime, _cfg()) == ('current', True)
