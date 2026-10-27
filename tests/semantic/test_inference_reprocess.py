"""Synthetic replay/currentness contracts; no model-quality or production acceptance."""
import json
import time
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from inference_version import CURRENT_VERSION, VERSION_KEY
import semantic_drain as drain
import semantic_extraction
import semantic_projection
import semantic_policy
import semantic_runtime as runtime
import semantic_store
from semantic_testkit import _cfg, _ledger, _message, _patient


@pytest.fixture
def store(tmp_path):
    ledger = _ledger(tmp_path)
    _patient(ledger)
    try:
        yield ledger
    finally:
        ledger.close()


def _save(store, mid=1, pid=1, parent=None):
    store.save_messages([_message(mid, pid=pid, parent=parent,
                                  body=f'完全合成の記録{mid}です。')], project_id=pid)


def _job(store, mid=1, pid=1):
    return store.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic' "
                             'AND project_id=? AND message_id=?', (pid, mid)).fetchone()


def _replay(store, cfg=None, **kw):
    return drain.reprocess_step(store, cfg or _cfg(loop_mode='off'), **kw)


def _arrival(store):
    return store.outbox_add('new_messages', 1, {'source': 'unread', 'message_ids': [1]})


def test_replay_is_bounded_and_restartable_for_roots_replies_archives_and_null_full(store):
    _patient(store, 2)
    for mid, pid, parent in [(1, 1, None), (2, 1, 1), (3, 2, None), (4, 1, None)]:
        _save(store, mid, pid, parent)
    with store.db:
        store.db.execute('UPDATE patients SET is_archived=1 WHERE project_id=2')
        store.db.execute('UPDATE messages SET body_state=NULL WHERE message_id=4')
    first = _replay(store, limit=2)
    assert first['cursor'] == 2 and first['processed'] == 2 and first['phase'] == 'seed'
    assert _job(store, 1) is not None and _job(store, 3, 2) is None
    # A fresh call reads the persisted cursor, without any in-memory registry.
    second = _replay(store, limit=2)
    assert second['phase'] == 'verify' and second['complete'] is False
    assert json.loads(_job(store)['payload'])['targets'] == [1, 2]
    for mid, pid in [(1, 1), (3, 2), (4, 1)]:
        pl = json.loads(_job(store, mid, pid)['payload'])
        assert pl[VERSION_KEY] == '1.0' and pl['notification_free'] is True
    bundle = semantic_store.thread_bundle(store.db, 1, 4)
    assert bundle['content_quality'] == 'full'
    assert store.db.execute('SELECT body_state FROM messages WHERE message_id=4').fetchone()[0] is None


def test_job_and_cursor_rollback_together_after_seed_failure(store, monkeypatch):
    _save(store)
    original = store.semantic_seed_tx
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic interruption after seed')
    monkeypatch.setattr(store, 'semantic_seed_tx', fail)
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        _replay(store)
    assert _job(store) is None
    assert 'inference_reprocess' not in drain._sched_state(store)
    assert not store.db.in_transaction


@pytest.mark.parametrize('old_version', [None, '0.1'])
def test_old_generation_requeues_once_and_rejects_old_worker_without_touching_receipts(store, old_version):
    _save(store)
    store.semantic_seed(1, [1], {'source': 'synthetic-old'})
    old = dict(_job(store))
    pl = json.loads(old['payload'])
    if old_version is None:
        pl.pop(VERSION_KEY)
    else:
        pl[VERSION_KEY] = old_version
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET payload=?,state='failed',attempts=6 WHERE job_id=?",
                         (json.dumps(pl), old['job_id']))
    token = runtime.JobToken.from_row(_job(store))
    for kind in ('semantic_usage', 'cloud_usage_reserve', 'notification_command_receipt'):
        store.artifact_add(kind, '{"synthetic_receipt":true}', project_id=1,
                           message_id=1, meta={'reserved': True})
    protected = [tuple(r) for r in store.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    raw = [tuple(r) for r in store.db.execute('SELECT * FROM messages')]
    outbox = [tuple(r) for r in store.db.execute('SELECT * FROM notify_outbox')]
    _replay(store)
    new = _job(store)
    assert new['state'] == 'pending' and new['attempts'] == 0
    assert json.loads(new['payload'])[VERSION_KEY] == CURRENT_VERSION
    assert not runtime.job_matches(store, token)
    assert not runtime.transition(store, token, 'done')
    assert protected == [tuple(r) for r in store.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    assert raw == [tuple(r) for r in store.db.execute('SELECT * FROM messages')]
    assert outbox == [tuple(r) for r in store.db.execute('SELECT * FROM notify_outbox')]


def test_repeated_same_contract_replay_preserves_exhausted_attempts_and_reports_failure(store):
    _save(store)
    _replay(store)
    row = _job(store)
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET state='failed',attempts=6 WHERE job_id=?", (row['job_id'],))
    for _ in range(6):
        report = _replay(store)
    assert _job(store)['attempts'] == 6 and _job(store)['state'] == 'failed'
    assert report['coverage']['counts'] == {'failed': 1}
    assert report['complete'] is False
    assert report['remaining_stages']


def test_enumeration_and_done_job_are_not_coverage_completion(store):
    _save(store)
    first = _replay(store)
    assert first['phase'] == 'verify' and first['complete'] is False
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET state='done' WHERE kind='semantic'")
    verified = _replay(store)
    assert verified['coverage']['counts'] == {'repair_required': 1}
    assert verified['complete'] is False


def test_current_pass_with_empty_projection_is_counted_without_legacy_fallback(store):
    _save(store)
    cfg = _cfg(loop_mode='off')
    scfg, errors = semantic_policy.runtime_config(cfg)
    assert not errors
    # Independent synthetic publication metadata: the test observes the real
    # source/currentness readers, without pretending to measure model accuracy.
    fp = semantic_store.thread_bundle(store.db, 1, 1)['source_fingerprint']
    source = store.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
    policy = semantic_policy.policy_fingerprint(scfg)
    binding = {VERSION_KEY: '1.0', 'fingerprint': fp, 'policy_fingerprint': policy,
               'hash': source, 'engine_version': 4,
               'projection_version': semantic_projection.PROJECTION_VERSION}
    store.artifact_add('semantic_summary', '{"claims":[]}', project_id=1, message_id=1, meta=binding)
    store.artifact_add('semantic_audit', '{"status":"PASS"}', project_id=1, message_id=1, meta=binding)
    store.artifact_add('semantic_facts', '{"facts":[]}', project_id=1, message_id=1, meta=binding)
    store.artifact_add('extract_llm', '{"summary":"obsolete"}', project_id=1, message_id=1,
                       meta={**binding, VERSION_KEY: '0.1'})
    # Noncanonical semantic scope accepts its audited empty summary. The
    # canonical publication selector is separately covered by the read tests.
    _replay(store, cfg)
    report = _replay(store, cfg)
    assert report['coverage']['counts'] == {'current': 1}
    assert report['complete'] is False and _job(store) is None


@pytest.mark.parametrize('proof_version,patient_state', [
    ('1.0', 'rollup_unverified'), ('0.1', 'pending'), (None, 'pending')])
def test_all_stage_join_keeps_source_proofs_but_waits_for_rollup_witness(store, monkeypatch, proof_version, patient_state):
    import patient_context_refresh
    import rollup
    _save(store)
    # This unit tests composition of owner proofs, not model generation.
    monkeypatch.setattr(drain, '_reprocess_target_state', lambda *args: 'current')
    monkeypatch.setattr(patient_context_refresh, 'inference_coverage', lambda *args: {
        VERSION_KEY: proof_version, 'complete': True, 'status': 'complete',
        'publication_eligible': False, 'sources': {key: 0 for key in
            ('total', 'eligible', 'scanned', 'audited', 'pending', 'held', 'failed', 'incomplete', 'excluded')},
        'summary': {'current': True, 'published': False}, 'source_sig': 'synthetic', 'contract': {}})
    monkeypatch.setattr(rollup, 'inference_coverage', lambda *args, **kw: pytest.fail('unproved rollup was used'))
    monkeypatch.setattr(rollup, 'rebuild', lambda *args, **kw: pytest.fail('existing rollup was replaced'))
    for _ in range(3):
        report = _replay(store)
    assert report['coverage']['counts'] == {'current': 1}
    assert report['patient_counts'] == {patient_state: 1}
    assert report['complete'] is False and report['phase'] == 'waiting'
    assert 'rollup_freshness_witness' in report['remaining_stages']
    assert _replay(store)['complete'] is False
    # A new backward-key source still invalidates the cached observation.
    store.save_messages([_message(1, body='完全合成の訂正です。')], project_id=1)
    assert _replay(store)['complete'] is False


@pytest.mark.parametrize('fault,wanted', [
    ('permission', 'permission'), ('scope', 'scope'), ('deleted', 'deleted'),
    ('snippet', 'body_unavailable'), ('unknown', 'body_unavailable'), ('paused', 'paused'),
])
def test_nonprocessable_sources_are_reported_without_admitting_calls_or_jobs(store, fault, wanted):
    _save(store)
    cfg = _cfg(loop_mode='off', project_ids=[] if fault == 'scope' else None)
    with store.db:
        if fault == 'permission':
            store.db.execute("UPDATE patients SET fetch_reason='access_revoked'")
        elif fault in ('deleted', 'snippet', 'unknown'):
            store.db.execute('UPDATE messages SET body_state=?', (fault,))
        elif fault == 'paused':
            store.artifact_add_tx('semantic_control', 'paused', project_id=1)
    _replay(store, cfg)
    verified = _replay(store, cfg)
    assert verified['coverage']['counts'] == {wanted: 1}
    assert _job(store) is None


def test_backlog_alternates_active_and_archived_class_without_http(store):
    _patient(store, 2)
    for mid, pid in [(1, 1), (2, 2)]:
        _save(store, mid, pid)
        store.semantic_seed(pid, [mid], {'source': 'synthetic-replay'})
    with store.db:
        store.db.execute('UPDATE patients SET is_archived=1 WHERE project_id=2')
    chosen = []
    for _ in range(4):
        due = drain._select_due(store, 'backlog', ('semantic',), False, 1)[0]
        chosen.append(due[0]['project_id'])
    assert chosen == [1, 2, 1, 2]
    # Realtime policy still gives active patients the first opportunity.
    assert drain._select_due(store, None, ('semantic',), False, 1)[0][0]['project_id'] == 1


def test_current_empty_cache_never_reuses_legacy_or_future_metadata(store):
    _save(store)
    for version in ('0.1', '1.2'):
        store.artifact_add('semantic_facts', '{"facts":["obsolete"]}',
                           project_id=1, message_id=1,
                           meta={VERSION_KEY: version, 'fingerprint': 'fp'})
    assert semantic_store.current_artifact(store.db, 'semantic_facts', 1, 'fp') is None
    store.artifact_add('semantic_facts', '{"facts":[]}', project_id=1, message_id=1,
                       meta={VERSION_KEY: '1.0', 'fingerprint': 'fp'})
    assert semantic_store.current_artifact(store.db, 'semantic_facts', 1, 'fp')['content'] == {'facts': []}
    old = {'doc_hash': 'doc', 'projection_version': semantic_projection.PROJECTION_VERSION}
    assert not semantic_projection.projection_current(old, 'doc')
    assert semantic_projection.projection_current({**old, VERSION_KEY: '1.0'}, 'doc')
    assert semantic_projection.projection_transition(
        {**old, 'fingerprint': 'fp', 'policy_fingerprint': 'policy'},
        off=False, enabled=True, policy='policy', source_fingerprint='fp', revivable=True)[0] == 'expire'


def test_new_arrival_keeps_notification_eligibility_when_replay_merges(store):
    _save(store)
    event = _arrival(store)
    store.semantic_seed(1, [1], {'source': 'unread', 'event_id': event})
    _replay(store)
    pl = json.loads(_job(store)['payload'])
    assert pl['eligible'] is True and pl['origin']['event_id'] == event
    assert not pl.get('notification_free')
    assert not pl.get('reprocess_only')


def test_replay_checkpoints_skip_card_callback_but_new_arrival_restores_it(store, monkeypatch):
    import extraction_refresh
    _save(store)
    _replay(store)
    callbacks = []
    monkeypatch.setattr(extraction_refresh, 'refresh_extraction_cards',
                        lambda *args, **kw: callbacks.append(args[1:]))
    spec = {'index': 0, 'start': 0, 'end': 2, 'hash': 'synthetic-piece'}
    def persist():
        semantic_extraction._persist_chunk(store, 1, 1, 'synthetic-model',
            'synthetic-fp', 'synthetic-hash', 'synthetic-revision', spec,
            [], 0, 1, generation='synthetic-generation')
    persist()
    assert callbacks == []
    assert not semantic_store.refresh_cards_allowed(store.db, 1, 1)
    store.semantic_seed(1, [1], {'source': 'unread', 'event_id': _arrival(store)})
    persist()
    assert callbacks == [(1, 1)]
    assert semantic_store.refresh_cards_allowed(store.db, 1, 1)
    pl = json.loads(_job(store)['payload'])
    assert not pl.get('reprocess_only') and not pl.get('notification_free')
    assert 'execution_generation' not in pl


def test_off_and_expired_deadline_do_not_advance_or_seed(store):
    _save(store)
    assert _replay(store, _cfg('off'))['status'] == 'off'
    report = _replay(store, deadline=time.monotonic() - 1)
    assert report['cursor'] == 0 and report['processed'] == 0
    assert _job(store) is None


def test_bundle_and_chunk_generations_bind_to_public_version(store, monkeypatch):
    _save(store)
    before = semantic_store.thread_bundle(store.db, 1, 1)['source_fingerprint']
    chunk = semantic_extraction._chunk_generation('synthetic-model', 'synthetic-prompt')
    monkeypatch.setattr(semantic_store, 'CURRENT_VERSION', '1.1')
    monkeypatch.setattr(semantic_extraction, 'CURRENT_VERSION', '1.1')
    assert semantic_store.thread_bundle(store.db, 1, 1)['source_fingerprint'] != before
    assert semantic_extraction._chunk_generation('synthetic-model', 'synthetic-prompt') != chunk


def test_guard_rechecks_permissions_before_commit(store):
    _save(store)
    store.semantic_seed(1, [1], {'source': 'synthetic'})
    token = runtime.JobToken.from_row(_job(store))
    with store.db:
        store.db.execute("UPDATE patients SET fetch_reason='forbidden'")
    with pytest.raises(runtime.RuntimeOff):
        runtime.guard(store, token, deadline=time.monotonic() + 60,
                      expected_config_generation=None, expected_mode='shadow')


def test_guard_cannot_adopt_new_config_under_an_old_execution_domain(store):
    _save(store)
    _replay(store)
    token = runtime.JobToken.from_row(_job(store))
    with pytest.raises(runtime.RuntimeStale, match='execution'):
        runtime.guard(store, token, deadline=time.monotonic() + 60,
                      expected_config_generation=None, expected_mode='shadow',
                      expected_execution_generation='different-contract')


def test_malformed_progress_is_held_without_reset_or_new_job(store):
    _save(store)
    _replay(store)
    sched = drain._sched_state(store)
    sched['inference_reprocess']['cursor'] = 'broken'
    drain._sched_write(store, sched)
    before = dict(_job(store))
    assert _replay(store)['status'] == 'invalid_progress'
    assert drain._sched_state(store)['inference_reprocess']['cursor'] == 'broken'
    assert dict(_job(store)) == before


@pytest.mark.parametrize('changes', [
    {'budget': 1}, {'mode': 'off'}, {'mode': 'assist'}, {'project_ids': []},
    {'attempt_timeout_seconds': 10}, {'job_budget_seconds': 60},
    {'max_attempts_per_try': 1},
])
def test_operational_setting_change_never_grants_a_fresh_attempt_budget(store, changes):
    _save(store)
    base = _cfg(loop_mode='off')
    _replay(store, base)
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET state='failed',attempts=6 WHERE kind='semantic'")
    before = dict(_job(store))
    updated = _cfg(loop_mode='off', **changes)
    old_contract = runtime.execution_generation(semantic_policy.runtime_config(base)[0])
    assert runtime.execution_generation(semantic_policy.runtime_config(updated)[0]) == old_contract
    _replay(store, updated)
    assert dict(_job(store)) == before


@pytest.mark.parametrize('path', ['priority', 'scan'])
def test_legacy_pending_arrival_keeps_real_intent_and_priority_before_full_walk(store, path):
    _save(store, 1)
    _save(store, 999)
    event = store.outbox_add('new_messages', 1, {'source': 'unread', 'message_ids': [999]})
    store.semantic_seed(1, [999], {'source': 'unread', 'event_id': event})
    old = _job(store, 999)
    payload = json.loads(old['payload'])
    payload.pop(VERSION_KEY)
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET payload=? WHERE job_id=?', (json.dumps(payload), old['job_id']))
    token = runtime.JobToken.from_row(_job(store, 999))
    if path == 'priority':
        _replay(store, limit=1)
        assert drain._sched_state(store)['inference_reprocess']['cursor'] == 1
        selected = drain._select_due(store, 'realtime', ('semantic',), False, 1)[0]
        assert selected and selected[0]['message_id'] == 999
        adopted = drain._adopt_pending_arrival(store, selected[0])
        assert adopted is not None
    else:
        _replay(store)
    current = json.loads(_job(store, 999)['payload'])
    assert current[VERSION_KEY] == CURRENT_VERSION
    assert current['eligible'] is True and current['origin']['event_id'] == event
    assert not current.get('notification_free') and not current.get('reprocess_only')
    assert not runtime.job_matches(store, token)


def test_replay_planning_does_not_hold_a_write_transaction(store, monkeypatch):
    _save(store)
    original = semantic_store.thread_bundle
    calls = []
    def checked(*args, **kwargs):
        assert not store.db.in_transaction
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(semantic_store, 'thread_bundle', checked)
    _replay(store)
    assert calls and _job(store) is not None and not store.db.in_transaction


def test_other_scheduler_update_during_planning_is_never_overwritten(store, monkeypatch):
    _save(store)
    _replay(store)
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    original = drain._reprocess_target_state
    def concurrent(*args):
        state = original(*args)
        with sqlite3.connect(path) as other:
            other.execute("UPDATE fetch_jobs SET payload=json_set(payload,'$.other_owner',7) WHERE kind='semantic_sched'")
        return state
    monkeypatch.setattr(drain, '_reprocess_target_state', concurrent)
    before = drain._sched_state(store)['inference_reprocess']
    result = _replay(store)
    assert result['status'] == 'source_changed' and result['processed'] == 0
    state = drain._sched_state(store)
    assert state['other_owner'] == 7 and state['inference_reprocess'] == before


def test_source_changes_with_no_projection_invalidate_backward_coverage_but_retries_do_not(store):
    _save(store, 10)
    _replay(store, limit=1)
    before = drain._sched_state(store)
    assert not store.db.execute("SELECT 1 FROM artifacts WHERE kind='semantic_facts_v4'").fetchone()
    # Silent writes still join source tracking while the replay exists.
    _save(store, 1)
    after = drain._sched_state(store)
    assert after['source_epoch'] > before.get('source_epoch', 0)
    result = _replay(store, limit=1)
    assert result['cursor'] == 1 and result['phase'] == 'verify'
    assert result['seed_cursor'] == 10  # A backward arrival is verified without losing the completed seed walk.
    epoch = drain._sched_state(store)['source_epoch']
    _replay(store, limit=1)
    assert drain._sched_state(store)['source_epoch'] == epoch


def test_karte_revision_changes_epoch_but_same_content_fetch_does_not(store):
    _save(store)
    _replay(store)
    start = drain._sched_state(store).get('source_epoch', 0)
    content = {'comment': '完全合成の原記録です。', 'updated_at': '2026-10-26T10:00:00+09:00'}
    assert store.karte_summary_store(1, 10, content)
    changed = drain._sched_state(store)['source_epoch']
    assert changed > start
    assert not store.karte_summary_store(1, 10, content)
    assert drain._sched_state(store)['source_epoch'] == changed
    result = _replay(store)
    assert result['source_epoch'] == changed and result['complete'] is False


def test_source_epoch_requires_caller_transaction_and_old_rollups_survive(store):
    _save(store)
    _replay(store)
    with pytest.raises(ValueError, match='transaction_required'):
        store.bump_inference_source_epoch_tx()
    old = store.artifact_add('patient_rollup', '{"old":true}', project_id=1,
                             meta={VERSION_KEY: '0.1'})
    current = store.artifact_add('patient_rollup', '{}', project_id=1,
                                 meta={VERSION_KEY: CURRENT_VERSION})
    store.artifact_add('semantic_facts_v4', '{}', project_id=1, message_id=1,
                       meta={VERSION_KEY: CURRENT_VERSION})
    store.save_messages([_message(1, body='合成の訂正です。')], project_id=1)
    assert store.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (old,)).fetchone()[0] == '{"old":true}'
    assert store.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (current,)).fetchone() is None


def test_guard_does_not_treat_a_missing_patient_as_a_permission_refusal(store):
    # Minimal borrowed schema models the existing direct semantic-job API.
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    try:
        db.executescript('CREATE TABLE patients(project_id,fetch_reason);'
                        'CREATE TABLE artifacts(kind,project_id,artifact_id,content);'
                        'CREATE TABLE fetch_jobs(job_id,kind,project_id,message_id,state,attempts,payload);')
        payload = json.dumps({VERSION_KEY: CURRENT_VERSION})
        db.execute("INSERT INTO fetch_jobs VALUES(1,'semantic',1,1,'pending',0,?)", (payload,))
        token = runtime.JobToken.from_row(db.execute('SELECT * FROM fetch_jobs').fetchone())
        runtime.guard(SimpleNamespace(db=db), token, deadline=time.monotonic() + 60,
                      expected_config_generation=None, expected_mode='shadow')
    finally:
        db.close()


def test_reprocess_exception_isolated_from_existing_due_jobs(store, monkeypatch):
    from semantic_testkit import _FakeJev
    _save(store)
    store.semantic_seed(1, [1], {'source': 'synthetic-existing-job'})
    processed = []
    def broken(*args, **kwargs):
        raise RuntimeError('SYNTHETIC_PRIVATE_MARKER')
    def existing(ledger, scfg, job, *args, **kwargs):
        processed.append(job['job_id'])
        assert runtime.transition(ledger, runtime.JobToken.from_row(job), 'done')
        return 'done'
    monkeypatch.setattr(drain, 'reprocess_step', broken)
    monkeypatch.setattr(drain, '_process_job', existing)
    result = {'errors': []}
    output = drain.run_due(store, _cfg(loop_mode='off'), result, time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=lambda *a, **kw: None, max_jobs=1)
    assert processed == [_job(store)['job_id']] and _job(store)['state'] == 'done'
    assert output['inference_reprocess']['status'] == 'error'
    assert 'semantic_reprocess: RuntimeError' in result['errors']
    assert 'SYNTHETIC_PRIVATE_MARKER' not in json.dumps(result)


def test_analysis_adoption_rechecks_without_reseed_or_new_attempt_budget(store):
    _save(store)
    _replay(store)
    before = dict(_job(store))
    with pytest.raises(ValueError, match='transaction_required'):
        store.bump_inference_analysis_epoch_tx(1)
    old = store.artifact_add('patient_rollup', '{"history":true}', project_id=1,
                             meta={VERSION_KEY: '0.1'})
    current = store.artifact_add('patient_rollup', '{}', project_id=1,
                                 meta={VERSION_KEY: CURRENT_VERSION})
    with store.db:
        store.db.execute('UPDATE fetch_jobs SET attempts=3 WHERE job_id=?', (before['job_id'],))
        store.bump_inference_analysis_epoch_tx(1)
    before = dict(_job(store))
    report = _replay(store)
    assert report['analysis_epoch'] == 1 and report['complete'] is False
    assert report['coverage']['counts'] == {'pending': 1}
    assert dict(_job(store)) == before
    assert store.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (old,)).fetchone()
    assert store.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (current,)).fetchone() is None


def test_publication_reuse_and_diagnostics_do_not_advance_analysis_witness(store):
    import semantic_v4
    from semantic_testkit import v2_doc
    _save(store)
    _replay(store)
    revision = store.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
    fp = semantic_store.thread_bundle(store.db, 1, 1)['source_fingerprint']
    policy = semantic_policy.policy_fingerprint(semantic_policy.runtime_config(_cfg(loop_mode='off'))[0])
    doc = v2_doc([])
    with store.db:
        first = semantic_v4.publish(store, 1, 1, fp, policy, {'revision': revision}, doc)
    assert first is not None
    epoch = drain._sched_state(store)['analysis_epoch']
    with store.db:
        second = semantic_v4.publish(store, 1, 1, fp, policy, {'revision': revision}, doc)
    assert first == second
    store.artifact_add('semantic_usage', '{"calls":1}', project_id=1)
    store.artifact_add('semantic_diagnostic', '{"status":"observed"}', project_id=1)
    assert drain._sched_state(store)['analysis_epoch'] == epoch


def test_review_held_generation_is_not_revived_by_enumeration(store):
    _save(store)
    _replay(store)
    scfg = semantic_policy.runtime_config(_cfg(loop_mode='off'))[0]
    fp = semantic_store.thread_bundle(store.db, 1, 1)['source_fingerprint']
    store.artifact_add('semantic_audit', '{"status":"NEEDS_REVIEW"}', project_id=1, message_id=1,
        meta={VERSION_KEY: CURRENT_VERSION, 'fingerprint': fp,
              'policy_fingerprint': semantic_policy.policy_fingerprint(scfg)})
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET state='done',attempts=3 WHERE kind='semantic'")
    before = dict(_job(store))
    for _ in range(4):
        report = _replay(store)
    assert report['complete'] is False
    assert dict(_job(store)) == before


def _waiting(store, cfg=None, *, limit=32):
    for _ in range(32):
        report = _replay(store,cfg,limit=limit)
        if report.get('phase') in ('waiting','complete'):
            return report
    pytest.fail('bounded synthetic replay did not reach its waiting/completed bookmark')


def _saved_old_completion(store, *, phase='complete', malformed=False):
    with drain._sched_update(store) as sched:
        old = dict(sched['inference_reprocess'])
        old.update(phase=phase, complete=phase == 'complete',
                   patient_counts={'current': 'bad' if malformed else 1, 'held': 2},
                   waiting_reasons={'messages': {'failed': 1}, 'patients': {'held': 2}},
                   remaining_stages=['source_or_patient_proof'] if phase == 'waiting' else [])
        old.pop('proof_status', None)
        old.pop('proof_reason', None)
        sched['inference_reprocess'] = old
    return old


@pytest.mark.parametrize('phase', ['complete', 'waiting'])
def test_old_bookmark_waits_without_losing_results_or_reopening_jobs(store, monkeypatch, phase):
    import patient_context_refresh
    import rollup

    _save(store)
    store.semantic_seed(1, [1], {'source': 'synthetic-old'})
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET state='failed', attempts=6 WHERE kind='semantic'")
    store.artifact_add('patient_rollup', '{"synthetic":"preserve"}', project_id=1,
                       meta={VERSION_KEY: CURRENT_VERSION})
    monkeypatch.setattr(drain, '_reprocess_target_state', lambda *args: 'current')
    monkeypatch.setattr(patient_context_refresh, 'inference_coverage', lambda *args: {
        VERSION_KEY: CURRENT_VERSION, 'complete': True})
    monkeypatch.setattr(rollup, 'rebuild', lambda *args, **kwargs: pytest.fail('rollup must stay unchanged'))
    report = _waiting(store)
    assert report['patient_counts'] == {'rollup_unverified': 1}
    _saved_old_completion(store, phase=phase)
    before_job = dict(_job(store))
    before_rows = [tuple(row) for row in store.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    before_outbox = [tuple(row) for row in store.db.execute('SELECT * FROM notify_outbox ORDER BY event_id')]
    report = _replay(store)
    assert report['phase'] == 'waiting' and report['complete'] is False and report['processed'] == 0
    assert report['patient_counts'] == {'rollup_unverified': 1, 'held': 2}
    assert report['waiting_reasons'] == {'messages': {'failed': 1},
        'patients': {'held': 2, 'rollup_unverified': 1}}
    assert report['proof_status'] == 'rollup_freshness_unverified'
    assert report['proof_reason'] == '患者集約の現行性を確認できません'
    assert 'rollup_freshness_witness' in report['remaining_stages']
    if phase == 'waiting':
        assert 'source_or_patient_proof' in report['remaining_stages']
    for _ in range(3):
        again = _replay(store)
        assert again['phase'] == 'waiting' and again['complete'] is False and again['processed'] == 0
    assert dict(_job(store)) == before_job
    assert [tuple(row) for row in store.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before_rows
    assert [tuple(row) for row in store.db.execute('SELECT * FROM notify_outbox ORDER BY event_id')] == before_outbox


def test_malformed_old_patient_count_is_not_promoted_or_rewritten(store):
    _save(store)
    _waiting(store)
    before = _saved_old_completion(store, malformed=True)
    result = _replay(store)
    assert result == {'status': 'invalid_progress', 'complete': False}
    assert drain._sched_state(store)['inference_reprocess'] == before


def test_old_completion_downgrade_checks_concurrent_epoch(store, monkeypatch):
    _save(store)
    _waiting(store)
    before = _saved_old_completion(store)
    original = drain._sched_update

    @contextmanager
    def race(ledger):
        with original(ledger) as sched:
            sched['source_epoch'] = sched.get('source_epoch', 0) + 1
        with original(ledger) as sched:
            yield sched

    monkeypatch.setattr(drain, '_sched_update', race)
    result = _replay(store)
    assert result == {'status': 'source_changed', 'complete': False, 'processed': 0}
    assert drain._sched_state(store)['inference_reprocess'] == before


def test_empty_scope_cannot_claim_unproved_rollup_completion(store):
    with store.db:
        store.db.execute('DELETE FROM patients')
    report = _waiting(store)
    assert report['phase'] == 'waiting' and report['complete'] is False
    assert report['coverage']['counts'] == {} and report['patient_counts'] == {}
    assert report['proof_status'] == 'rollup_freshness_unverified'


def test_unverified_rollup_does_not_block_existing_semantic_job(store, monkeypatch):
    from semantic_testkit import _FakeJev

    _save(store)
    store.semantic_seed(1, [1], {'source': 'synthetic-existing-job'})
    report = _waiting(store)
    assert report['proof_status'] == 'rollup_freshness_unverified'
    before_attempts = _job(store)['attempts']
    outbox = [tuple(row) for row in store.db.execute('SELECT * FROM notify_outbox ORDER BY event_id')]
    processed = []

    def existing(ledger, _scfg, job, *args, **kwargs):
        processed.append(job['job_id'])
        assert runtime.transition(ledger, runtime.JobToken.from_row(job), 'done')
        return 'done'

    monkeypatch.setattr(drain, '_process_job', existing)
    result = {'errors': []}
    output = drain.run_due(store, _cfg(loop_mode='off'), result, time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=lambda *a, **kw: None, max_jobs=1)
    assert processed == [_job(store)['job_id']] and _job(store)['state'] == 'done'
    assert _job(store)['attempts'] == before_attempts
    assert output['inference_reprocess']['complete'] is False
    assert output['inference_reprocess']['proof_status'] == 'rollup_freshness_unverified'
    assert [tuple(row) for row in store.db.execute('SELECT * FROM notify_outbox ORDER BY event_id')] == outbox


def test_continuous_arrivals_and_adoptions_do_not_starve_the_existing_keyset(store,monkeypatch):
    for mid in range(1,121):
        _save(store,mid)
    monkeypatch.setattr(drain,'_process_job',lambda *a,**k: pytest.fail('planner dispatched an API worker'))
    monkeypatch.setattr(drain,'_reprocess_patient_state',lambda *a: 'pending')
    previous = _replay(store,limit=7)
    initial = dict(_job(store,1))
    assert previous['cursor']==7 and previous['upper']==120
    seed_highwaters = [previous['seed_cursor']]
    for arrival in range(60):
        _save(store,1000+arrival)
        store.db.execute('BEGIN IMMEDIATE')
        with store.db:
            store.bump_inference_analysis_epoch_tx()
        report = _replay(store,limit=7)
        assert report['processed']<=7 and not report['complete']
        if report['phase']==previous['phase'] and report['upper']==previous['upper']:
            assert report['cursor']>=previous['cursor']
        seed_highwaters.append(report['seed_cursor'])
        previous = report
    assert seed_highwaters==sorted(seed_highwaters)
    assert _job(store,120) is not None
    assert dict(_job(store,1))==initial  # No repeated seed or new attempt generation for unchanged input.
    assert report['seed_cursor']>=120 and report['future_upper']==1059
    assert report['tail_pending'] is True
    assert any(_job(store,mid) is not None for mid in range(1000,1060))


@pytest.mark.parametrize('blocked,wanted', [('done','repair_required'),('failed','failed'),('paused','paused')])
def test_same_epoch_waiting_does_not_walk_or_reseed_blocked_inputs(store,monkeypatch,blocked,wanted):
    _save(store)
    _replay(store)
    if blocked=='paused':
        store.artifact_add('semantic_control','paused',project_id=1)
    else:
        token = runtime.JobToken.from_row(_job(store))
        assert runtime.transition(store,token,blocked)
    before = dict(_job(store))
    report = _waiting(store)
    assert report['phase']=='waiting' and report['coverage']['counts']=={wanted:1}
    monkeypatch.setattr(drain,'_reprocess_target_state',lambda *a: pytest.fail('stable waiting rescanned a message'))
    monkeypatch.setattr(drain,'_reprocess_patient_state',lambda *a: pytest.fail('stable waiting rescanned a patient'))
    for _ in range(10):
        same = _replay(store,limit=1)
        assert same['phase']=='waiting' and same['processed']==0 and not same['complete']
    assert dict(_job(store))==before


def test_done_same_input_is_not_reopened_even_by_an_explicit_replay_seed(store):
    _save(store)
    _replay(store)
    assert runtime.transition(store,runtime.JobToken.from_row(_job(store)),'done')
    before = dict(_job(store))
    _waiting(store)
    generation = runtime.execution_generation(semantic_policy.runtime_config(_cfg(loop_mode='off'))[0])
    with store.db:
        store.semantic_seed_tx(1,[1],{'source':'inference_reprocess','notification_free':True,
                                     'inference_generation':generation},
            _source_generation=store._semantic_source_generation(1,1))
    assert dict(_job(store))==before
    assert _replay(store)['waiting_reasons']['messages']=={'repair_required':1}


@pytest.mark.parametrize('change', ['source','meaning'])
def test_only_a_new_source_or_meaning_generation_reopens_an_evaluated_input(store,change):
    _save(store)
    _replay(store)
    with store.db:
        store.db.execute("UPDATE fetch_jobs SET state='failed',attempts=6 WHERE kind='semantic'")
    _waiting(store)
    before = dict(_job(store))
    scoped = _cfg(loop_mode='off',project_ids=[1])
    _waiting(store,scoped)
    assert dict(_job(store))==before
    if change=='source':
        store.save_messages([_message(1,body='完全合成の新しい意味を持つ原文です。')],project_id=1)
        cfg = scoped
    else:
        cfg = _cfg(loop_mode='off',project_ids=[1],calibration_version='synthetic-test-v2')
        assert runtime.execution_generation(semantic_policy.runtime_config(cfg)[0]) != runtime.execution_generation(semantic_policy.runtime_config(scoped)[0])
    _replay(store,cfg)
    current = _job(store)
    assert current['state']=='pending' and current['attempts']==0
    assert json.loads(current['payload'])['generation']!=json.loads(before['payload'])['generation']


@pytest.mark.parametrize('terminal,repairable,wanted', [(1,0,'terminal_gap'),(0,1,'repair_pending')])
def test_patient_dependency_taxonomy_is_not_a_runnable_message_or_current_proof(store,monkeypatch,terminal,repairable,wanted):
    import patient_context_refresh
    _save(store)
    monkeypatch.setattr(drain,'_reprocess_target_state',lambda *a: 'current')
    monkeypatch.setattr(patient_context_refresh,'inference_coverage',lambda *a: {
        VERSION_KEY:CURRENT_VERSION,'complete':False,'publication_eligible':False,'status':'dependency_gap',
        'sources':dict.fromkeys(('total','eligible','scanned','audited','pending','held','failed','incomplete','excluded'),0),
        'dependencies':{'complete':False,'gaps':1,'coverage':{'terminal':terminal,'repairable':repairable,
                        'reasons':{'synthetic_gap':1}}}})
    report = _waiting(store)
    assert report['phase']=='waiting' and not report['complete']
    assert report['patient_counts']=={wanted:1} and report['waiting_reasons']['patients']=={wanted:1}
    assert _job(store) is None
    assert _replay(store)['processed']==0


def test_job_state_epoch_tracks_real_transitions_not_heartbeat_redundancy_or_stale_tokens(store):
    _save(store)
    _replay(store)
    token = runtime.JobToken.from_row(_job(store))
    assert runtime.transition(store,token,'defer')
    assert drain._sched_state(store).get('job_state_epoch',0)==0
    assert runtime.transition(store,runtime.JobToken.from_row(_job(store)),'done')
    assert drain._sched_state(store)['job_state_epoch']==1
    assert not runtime.transition(store,token,'failed')
    assert drain._sched_state(store)['job_state_epoch']==1
    assert runtime.transition(store,runtime.JobToken.from_row(_job(store)),'done')
    assert drain._sched_state(store)['job_state_epoch']==1
    assert runtime.transition(store,runtime.JobToken.from_row(_job(store)),'failed')
    assert drain._sched_state(store)['job_state_epoch']==2
    sched = drain._sched_state(store)
    assert sched.get('source_epoch',0)==0 and sched.get('analysis_epoch',0)==0


def test_job_state_transition_and_epoch_roll_back_together(store):
    _save(store)
    _replay(store)
    token = runtime.JobToken.from_row(_job(store))
    before_job, before_sched = dict(_job(store)), drain._sched_state(store)
    with pytest.raises(RuntimeError,match='synthetic_rollback'):
        with store.db:
            assert runtime.transition_tx(store,token,'done')
            assert drain._sched_state(store)['job_state_epoch']==1
            raise RuntimeError('synthetic_rollback')
    assert dict(_job(store))==before_job and drain._sched_state(store)==before_sched


def test_job_state_change_during_planning_is_fenced_without_losing_the_saved_cursor(store,monkeypatch):
    _save(store)
    _replay(store)
    before = drain._sched_state(store)['inference_reprocess']
    observe = drain._reprocess_target_state
    changed = []
    def concurrent(*args):
        result = observe(*args)
        if not changed:
            assert runtime.transition(store,runtime.JobToken.from_row(_job(store)),'done')
            changed.append(True)
        return result
    monkeypatch.setattr(drain,'_reprocess_target_state',concurrent)
    report = _replay(store)
    assert report['status']=='source_changed' and report['processed']==0
    assert drain._sched_state(store)['inference_reprocess']==before
    assert drain._sched_state(store)['job_state_epoch']==1
    done = dict(_job(store))
    _replay(store)
    assert dict(_job(store))==done


@pytest.mark.parametrize('value', ['broken',True,-1,2**63-1])
def test_invalid_or_overflow_job_epoch_refuses_completion_without_resetting_it(store,value):
    _save(store)
    _replay(store)
    sched = drain._sched_state(store)
    sched['job_state_epoch']=value
    drain._sched_write(store,sched)
    assert runtime.transition(store,runtime.JobToken.from_row(_job(store)),'done')
    after = drain._sched_state(store)
    assert after['job_state_epoch']==value and after['job_state_epoch_invalid'] is True
    assert _replay(store)['status']=='invalid_source_epoch'


@pytest.mark.parametrize('inactive', ['missing','no_replay'])
def test_job_transition_never_creates_a_replay_or_epoch_without_an_active_bookmark(store,inactive):
    _save(store)
    store.semantic_seed(1,[1],{'source':'synthetic-job'})
    if inactive=='no_replay':
        drain._sched_write(store,{'other_owner':7})
    before = drain._sched_state(store)
    assert runtime.transition(store,runtime.JobToken.from_row(_job(store)),'done')
    assert drain._sched_state(store)==before


@pytest.mark.parametrize('start_offset', [60, 12 * 3600, 86400 - 1])
def test_initial_replay_resumes_across_midnight_with_budget_and_deadline(store, monkeypatch, start_offset):
    """The first saved-source pass is resumable at any hour; this is not a daily barrier."""
    from ledger import Ledger
    from semantic_jev import JevError

    for mid in (1, 2, 3):
        _save(store, mid)
    db_path = store.db.execute('PRAGMA database_list').fetchone()[2]
    start_day = semantic_store._jst_day_start(1_800_000_000)
    clock = [start_day + start_offset]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    cfg = _cfg(loop_mode='off', budget=3000)

    store.artifact_add('semantic_usage', '{}', meta={'jev_requests': 2999})
    reserve = runtime.manual_usage_reserver(store, operation='jev_value', daily_request_budget=3000)
    reserve({'synthetic': True}, 1)
    assert semantic_store.jev_usage_today(store) == 3000
    with pytest.raises(JevError) as error:
        reserve({'synthetic': True}, 1)
    assert error.value.kind == 'budget_exceeded' and error.value.detail == 'daily_cap'
    old_usage = [tuple(row) for row in store.db.execute(
        "SELECT artifact_id,meta,created_at FROM artifacts WHERE kind='semantic_usage' ORDER BY artifact_id")]

    first = _replay(store, cfg, limit=1, deadline=time.monotonic() + 60)
    assert first['phase'] == 'seed' and first['cursor'] == first['seed_cursor'] == 1
    assert first['processed'] == 1 and first['complete'] is False
    first_payload = _job(store, 1)['payload']
    store.close()

    clock[0] = start_day + 86400
    reopened = Ledger(db_path)
    try:
        assert drain._sched_state(reopened)['inference_reprocess']['cursor'] == 1
        paused = _replay(reopened, cfg, limit=1, deadline=time.monotonic() - 1)
        assert paused['phase'] == 'seed' and paused['cursor'] == paused['seed_cursor'] == 1
        assert paused['processed'] == 0 and paused['complete'] is False
        assert _job(reopened, 1)['payload'] == first_payload and _job(reopened, 2) is None
        assert semantic_store.jev_usage_today(reopened) == 0
        assert ([tuple(row) for row in reopened.db.execute(
            "SELECT artifact_id,meta,created_at FROM artifacts WHERE kind='semantic_usage' ORDER BY artifact_id")]
            == old_usage)

        resumed = _replay(reopened, cfg, limit=1, deadline=time.monotonic() + 60)
        assert resumed['phase'] == 'seed' and resumed['cursor'] == resumed['seed_cursor'] == 2
        assert resumed['processed'] == 1 and resumed['complete'] is False
        assert _job(reopened, 1)['payload'] == first_payload and _job(reopened, 2) is not None
        next_reserve = runtime.manual_usage_reserver(reopened, operation='jev_value', daily_request_budget=3000)
        next_reserve({'synthetic': True}, 1)
        assert semantic_store.jev_usage_today(reopened) == 1
        assert ([tuple(row) for row in reopened.db.execute(
            "SELECT artifact_id,meta,created_at FROM artifacts WHERE kind='semantic_usage' ORDER BY artifact_id")][:2]
            == old_usage)
    finally:
        reopened.close()
