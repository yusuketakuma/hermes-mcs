"""Processing freshness keeps acquisition, job completion and validated audit coverage distinct."""
from contextlib import closing
import json

import pytest

from ledger import Ledger
from operational_progress import pipeline_progress
from semantic_runtime import config_generation
from semantic_testkit import _cfg, _message, _patient

NOW = 2_000_000_000.0


def _job(ledger, mid, *, state='done', generation='synthetic-generation'):
    jid = ledger.job_add('semantic', 1, mid, payload={'generation': generation})
    ledger.db.execute('UPDATE fetch_jobs SET state=?,created_at=?,updated_at=?,next_try=0 WHERE job_id=?',
                      (state, NOW - 20, NOW - 10, jid))
    return {'job_id': jid, 'generation': generation, 'status': 'done', 'kind': 'semantic',
            'project_id': 1, 'cohort': 'backfill', 'completed_at': NOW - 10}


def _record(ledger, metrics, *, cfg=None, complete=True):
    observation = {'version': 1, 'started_at': NOW - 200, 'finished_at': NOW - 1,
                   'complete': complete, 'config_generation': config_generation(cfg or _cfg())}
    ledger.artifact_add('semantic_drain_run', json.dumps({
        'observation': observation, 'job_metrics': metrics}))
    ledger.db.execute("UPDATE artifacts SET created_at=? WHERE kind='semantic_drain_run'", (NOW - 1,))


def test_pending_ages_use_first_seen_and_enqueue_time_instead_of_post_time(tmp_path):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _patient(ledger)
        ledger.save_messages([_message(1)], project_id=1)
        _job(ledger, 1, state='pending')
        ledger.db.execute('UPDATE messages SET first_seen=?,posted_at_ts=?', (NOW - 40, NOW - 100000))
        before = ledger.db.total_changes
        report = pipeline_progress(ledger.db, _cfg(), now=NOW)
        slot = report['queues']['backfill']
        assert slot['oldest_enqueued_at'] == NOW - 20 and slot['oldest_age_s'] == 20
        assert slot['oldest_acquired_at'] == NOW - 40 and slot['oldest_acquired_age_s'] == 40
        assert slot['acquisition_time_unknown'] == 0
        assert report['queue_age_basis'] == 'fetch_jobs.created_at'
        assert report['acquisition_age_basis'] == 'messages.first_seen_not_posted_at'
        assert report['slo'] == {'status': 'target_not_defined', 'target_s': None}
        assert ledger.db.total_changes == before


@pytest.mark.parametrize('first_seen', [None, 'invalid', -1, NOW + 1, float('inf')])
def test_missing_or_invalid_acquisition_is_unknown_without_post_time_fallback(tmp_path, first_seen):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _patient(ledger)
        ledger.save_messages([_message(1)], project_id=1)
        _job(ledger, 1, state='pending')
        ledger.db.execute('UPDATE messages SET first_seen=?,posted_at_ts=?', (first_seen, NOW - 100000))
        slot = pipeline_progress(ledger.db, _cfg(), now=NOW)['queues']['backfill']
        assert slot['oldest_acquired_at'] is None and slot['oldest_acquired_age_s'] is None
        assert slot['acquisition_time_unknown'] == 1


def test_last_completion_requires_live_job_generation_and_config(tmp_path):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        valid = _job(ledger, 1)
        obsolete = _job(ledger, 2)
        ledger.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?',
                          (json.dumps({'generation': 'replaced'}), obsolete['job_id']))
        _record(ledger, [{**valid, 'completed_at': NOW - 60}, {**obsolete, 'completed_at': NOW - 2}])
        report = pipeline_progress(ledger.db, _cfg(), now=NOW)
        assert report['queues']['backfill']['throughput']['latest_completed_at'] == NOW - 60
        assert report['last_scheduler_at'] == NOW - 1
        assert report['completion_observation_window_s'] == 3600
        assert report['completion_basis'] == 'recorded_current_job_completion_not_clinical_or_delivery'
        assert report['result_validity']['status'] == 'not_evaluated'
        changed = pipeline_progress(ledger.db, _cfg('off'), now=NOW)
        assert changed['queues']['backfill']['throughput']['latest_completed_at'] is None
        assert changed['last_scheduler_at'] is None


def test_partial_scheduler_pass_does_not_assert_job_completion(tmp_path):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _record(ledger, [_job(ledger, 1)], complete=False)
        report = pipeline_progress(ledger.db, _cfg(), now=NOW)
        assert report['last_scheduler_at'] == NOW - 1
        assert report['queues']['backfill']['throughput']['latest_completed_at'] is None
        assert report['scheduler_basis'] == 'recorded_current_config_drain_pass_not_completion'


def test_observer_reuses_existing_bounded_quality_scan(tmp_path, monkeypatch):
    import semantic_metrics
    import semantic_observe

    path = str(tmp_path / 'synthetic.db')
    Ledger(path).close()
    calls = []

    def quality(ledger, cfg, *, checkpoint):
        calls.append(True)
        checkpoint()
        return {'available': True, 'denominator': 4, 'complete': 3, 'incomplete': 1,
                'audit_statuses': {'PASS': 2, 'NEEDS_REVIEW': 1}}

    monkeypatch.setattr(semantic_metrics, 'current_quality', quality)
    result = semantic_observe.observe(path, _cfg())
    validity = result['pipeline_progress']['result_validity']
    assert calls == [True]
    assert validity == {'status': 'recorded', 'reason': None,
        'basis': 'current_terminal_summary_audit_pair_not_clinical_accuracy',
        'denominator': 4, 'complete': 3, 'incomplete': 1,
        'audit_statuses': {'PASS': 2, 'NEEDS_REVIEW': 1}}


def test_oldest_queue_and_acquisition_can_belong_to_different_jobs(tmp_path):
    with closing(Ledger(str(tmp_path / 'synthetic.db'))) as ledger:
        _patient(ledger)
        ledger.save_messages([_message(1), _message(2)], project_id=1)
        first = _job(ledger, 1, state='pending')
        second = _job(ledger, 2, state='pending')
        ledger.db.execute('UPDATE fetch_jobs SET created_at=? WHERE job_id=?', (NOW - 1000, first['job_id']))
        ledger.db.execute('UPDATE fetch_jobs SET created_at=? WHERE job_id=?', (NOW - 10, second['job_id']))
        ledger.db.execute('UPDATE messages SET first_seen=? WHERE message_id=1', (NOW - 1100,))
        ledger.db.execute('UPDATE messages SET first_seen=? WHERE message_id=2', (NOW - 100000,))
        slot = pipeline_progress(ledger.db, _cfg(), now=NOW)['queues']['backfill']
        assert slot['oldest_age_s'] == 1000
        assert slot['oldest_acquired_age_s'] == 100000
        assert slot['acquisition_time_unknown'] == 0
